"""Orchestrates ingestion -> diagnosis -> decision -> channel -> messaging -> friction -> priority ->
follow-ups into one application-level output, plus summaries. Deterministic given the workbook,
settings and recorded human state; LLM outputs are only used from the cache after validation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from . import config
from .channel import select_channel
from .decision import CLOSE, RECOVER, ROUTE, decide
from .diagnosis import (SOURCE_INFERRED, build_llm_diagnosis_prompt, diagnose, llm_cache_key,
                        validate_llm_diagnosis)
from .followup import build_sequence
from .friction import assess
from .ingest import Application, Dataset
from .insights import abandonment_summary, by_code, product_recommendations, top_systemic_causes
from .llm import LLMClient
from .messaging import (build_llm_nudge_prompt, compose_nudge, llm_nudge_cache_key, message_hash,
                        validate_llm_nudge)
from .priority import score_queue
from .store import Store


@dataclass
class Results:
    rows: List[Dict[str, Any]]
    df: pd.DataFrame                 # non-activated applications (the processed set)
    activated_df: pd.DataFrame
    summary: Dict[str, Any]
    apps: Dict[str, Application]
    diagnoses: Dict[str, Any]
    decisions: Dict[str, Any]
    nudges: Dict[str, Any]
    frictions: Dict[str, Any]
    sequences: Dict[str, Any]


def run(dataset: Dataset, settings: Dict[str, Any], store: Store) -> Results:
    lexicon = config.load_rules("diagnosis_lexicon.json")
    rules = config.load_rules("decision_rules.json")
    interventions = config.load_rules("product_interventions.json")
    reviews, flags, approvals = store.reviews(), store.flags(), store.approvals()
    events_all, cache = store.followup_events(), store.llm_cache()

    rows: List[Dict[str, Any]] = []
    act_rows: List[Dict[str, Any]] = []
    diags, decs, nudges, fricts, seqs = {}, {}, {}, {}, {}
    apps = {a.application_id: a for a in dataset.applications}

    for app in dataset.applications:
        if app.application_status == "Activated":
            act_rows.append({**app.to_dict(), "note": "Activated - excluded from recovery queue"})
            continue
        llm_diag = cache.get(llm_cache_key(app))
        d = diagnose(app, dataset.catalog, lexicon, rules, settings, review=reviews.get(app.application_id),
                     llm_result=llm_diag.get("validated") if llm_diag else None)
        if app.application_id in flags and d.source != "Human-verified":
            d.human_review_required = True
            d.review_reasons.append(f"Manually flagged for human review: {flags[app.application_id].get('reason') or 'no reason given'}")
        events = events_all.get(app.application_id, [])
        stop_comm = any(e["event"] == "stop_communication" for e in events)
        dec = decide(d, dataset.catalog, rules, stop_communication=stop_comm)
        ch = select_channel(app, settings["messaging"])
        nudge = compose_nudge(app, d, dec, ch, settings)
        template_body = nudge["body"]
        # Use a cached, guardrail-validated LLM rewrite if one exists for exactly this draft.
        if nudge["body"]:
            cached = cache.get(llm_nudge_cache_key(app, d.code or "", ch["channel"], template_body))
            if cached and cached.get("validated", {}).get("ok"):
                nudge["template_body"] = template_body
                nudge["body"] = cached["validated"]["body"]
                nudge["subject"] = cached["validated"].get("subject") or nudge["subject"]
                nudge["generation_source"] = f"LLM ({cached.get('model', 'Claude')}) - guardrail-validated"
        fr = assess(app, d.code, dec.decision if dec.actionable else None, settings["friction"])
        entry = dataset.catalog.get(d.code) if d.code else None
        appr = approvals.get(app.application_id)
        row = {
            "application_id": app.application_id,
            "application_status": app.application_status,
            "onboarding_stage": app.step_reached,
            "diagnosed_reason": entry.reviewer_reason if entry else "Undetermined - needs human review",
            "plain_language_meaning": entry.plain_language_meaning if entry else "",
            "code": d.code,
            "rejection_code_recorded": app.rejection_code,
            "code_source": d.source,
            "diagnosis_method": d.method,
            "supporting_evidence": d.evidence,
            "diagnosis_reasoning": d.reasoning,
            "fixability": dec.fixability,
            "recovery_decision": dec.decision,
            "decision_state": dec.decision_state,
            "decision_owner": dec.owner,
            "decision_trace": " ".join(dec.rule_trace),
            "standard_fix": dec.standard_fix,
            "typical_days_to_fix": entry.typical_days_to_fix if entry else None,
            "preferred_language": app.preferred_language,
            "channel": ch["channel"],
            "channel_secondary": ch["secondary"],
            "channel_recipient": ch["recipient"],
            "channel_reason": ch["reason"],
            "draft_subject": nudge["subject"],
            "draft_nudge": nudge["body"],
            "nudge_status": ("Approved by ops (NOT sent - prototype)" if appr and appr.get("message_hash") == message_hash(nudge["body"])
                             else nudge["status"]),
            "nudge_generation": nudge["generation_source"],
            "nudge_context": "; ".join(nudge["context_applied"]),
            "policy_violations": "; ".join(nudge["violations"]),
            "confidence": round(float(d.confidence), 2),
            "human_review_required": d.human_review_required,
            "human_review": "Required" if d.human_review_required else "Not required",
            "review_reasons": " | ".join(d.review_reasons),
            "ai_suggested_code": d.ai_code,
            "recovery_eligible": dec.recovery_eligible,
            "friction_level": fr.level,
            "friction_points": fr.points,
            "friction_indicators": "; ".join(fr.indicators),
            "pain_point": fr.pain_point,
            "recommended_intervention": fr.intervention,
            "expected_monthly_tpv_inr_lakh": app.expected_monthly_tpv_inr_lakh,
            "business_type": app.business_type,
            "industry": app.industry,
            "city": app.city,
            "whatsapp_opted_in": app.whatsapp_opted_in,
            "signup_date": app.signup_date,
            "days_since_last_activity": app.days_since_last_activity,
            "attempts": app.attempts,
            "support_contacts": app.support_contacts,
            "rejection_message": app.rejection_message,
            "flagged": app.application_id in flags,
            "dropout_reason": next((e.get("note") for e in reversed(events) if e["event"] == "dropout_reason"), None),
            "priority_rank": None,
        }
        rows.append(row)
        diags[app.application_id], decs[app.application_id] = d, dec
        nudges[app.application_id], fricts[app.application_id] = nudge, fr

    score_queue(rows, settings["priority"])
    for r in rows:
        seq = build_sequence(r, apps[r["application_id"]], settings, events_all.get(r["application_id"], []))
        seqs[r["application_id"]] = seq
        r["followup_status"] = ("Active - " + seq["cadence"]) if seq["active"] else (seq["stop_reason"] or "Not started")
        r["followup_plan"] = " -> ".join(f"Day {s['day']}: {s['step']}" for s in seq["steps"]) if seq["steps"] else "None"

    df = pd.DataFrame(rows)
    activated_df = pd.DataFrame(act_rows)
    summary = summarise(df, activated_df, len(dataset.applications), interventions)
    return Results(rows, df, activated_df, summary, apps, diags, decs, nudges, fricts, seqs)


def summarise(df: pd.DataFrame, activated_df: pd.DataFrame, total: int, interventions: Dict[str, Any]) -> Dict[str, Any]:
    if df.empty:
        return {"total": total, "activated": len(activated_df), "non_activated": 0}
    rec = df[df["recovery_decision"] == RECOVER]
    top3 = top_systemic_causes(df, 3)
    recs = product_recommendations(df, interventions)
    return {
        "total": total,
        "activated": int(len(activated_df)),
        "non_activated": int(len(df)),
        "stalled": int((df["application_status"] == "Stalled").sum()),
        "rejected": int((df["application_status"] == "Rejected").sum()),
        "recoverable": int(len(rec)),
        "recover_ready": int(df["recovery_eligible"].sum()),
        "closure": int((df["recovery_decision"] == CLOSE).sum()),
        "compliance": int((df["recovery_decision"] == ROUTE).sum()),
        "undetermined": int(df["recovery_decision"].isna().sum()),
        "human_review": int(df["human_review_required"].sum()),
        "blank_code_with_message": int(((df["rejection_code_recorded"].isna()) & df["rejection_message"].notna()).sum()),
        "blank_code_no_message": int(((df["rejection_code_recorded"].isna()) & df["rejection_message"].isna()).sum()),
        "ai_inferred": int((df["code_source"] == SOURCE_INFERRED).sum()),
        "tpv_recoverable_lakh": round(float(rec["expected_monthly_tpv_inr_lakh"].sum()), 1),
        "tpv_queue_lakh": round(float(df[df["recovery_eligible"]]["expected_monthly_tpv_inr_lakh"].sum()), 1),
        "tpv_non_activated_lakh": round(float(df["expected_monthly_tpv_inr_lakh"].sum()), 1),
        "tpv_closure_lakh": round(float(df[df["recovery_decision"] == CLOSE]["expected_monthly_tpv_inr_lakh"].sum()), 1),
        "tpv_compliance_lakh": round(float(df[df["recovery_decision"] == ROUTE]["expected_monthly_tpv_inr_lakh"].sum()), 1),
        "top3": top3.to_dict("records"),
        "abandonment": abandonment_summary(df),
        "ship_first": recs.iloc[0].to_dict() if len(recs) else None,
        "policy_violations": int((df["policy_violations"] != "").sum()),
        "priority_counts": df["priority"].value_counts().to_dict(),
    }


# ------------------------------------------------------------------ on-demand LLM actions
def run_llm_diagnosis(app: Application, dataset: Dataset, store: Store, llm: LLMClient, actor: str = "ops-user") -> Dict[str, Any]:
    template = config.load_prompt("diagnose_missing_code.md")
    system, user = build_llm_diagnosis_prompt(app, dataset.catalog, template)
    raw, meta = llm.complete_json(system, user)
    if raw is None:
        return {"ok": False, "error": meta.get("error")}
    validated = validate_llm_diagnosis(raw, app.rejection_message or "", dataset.catalog)
    store.put_llm(llm_cache_key(app), {"raw": raw, "validated": validated, "model": meta.get("model_served", llm.model)})
    store.audit("llm_diagnosis", app.application_id, f"llm:{llm.model}", {"raw": raw, "validated": validated})
    return {"ok": True, "validated": validated}


def run_llm_nudge(app: Application, results: Results, store: Store, llm: LLMClient, settings: Dict[str, Any]) -> Dict[str, Any]:
    d, dec, nudge = results.diagnoses[app.application_id], results.decisions[app.application_id], results.nudges[app.application_id]
    template_body = nudge.get("template_body", nudge["body"])
    if not template_body:
        return {"ok": False, "error": "No grounded draft available (diagnosis not verified)."}
    draft = dict(nudge, body=template_body)
    prompt = build_llm_nudge_prompt(app, d, dec, draft, config.load_prompt("nudge_generation.md"))
    raw, meta = llm.complete_json(prompt["system"], prompt["user"])
    if raw is None:
        return {"ok": False, "error": meta.get("error")}
    validated = validate_llm_nudge(raw, app, d, dec, draft, settings)
    store.put_llm(llm_nudge_cache_key(app, d.code or "", nudge["channel"], template_body),
                  {"raw": raw, "validated": validated, "model": meta.get("model_served", llm.model)})
    store.audit("llm_nudge", app.application_id, f"llm:{llm.model}",
                {"ok": validated["ok"], "violations": validated["violations"]})
    return {"ok": validated["ok"], "validated": validated,
            "error": None if validated["ok"] else "LLM draft failed guardrails - curated template kept."}
