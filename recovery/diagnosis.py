"""Features 3 & 8 - diagnosis engine with confidence.

Path A  rejection_code present  -> catalogue lookup (workbook fact, confidence 1.0).
Path B  code blank, message     -> explainable lexicon classifier over the 13 catalogue codes,
                                   optionally cross-checked by an LLM (cached); confidence score;
                                   low confidence / disagreement -> human verification.
Path C  code blank, no message  -> abandonment (R13) when the inactivity signal supports it.
Human   an authorised reviewer's verification always wins and is recorded.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from .ingest import Application, CatalogEntry

SOURCE_WORKBOOK = "Workbook fact (rejection_code)"
SOURCE_INFERRED = "AI-inferred from reviewer text"
SOURCE_ABANDON = "Rule-based abandonment signal"
SOURCE_HUMAN = "Human-verified"
SOURCE_UNKNOWN = "Undetermined"


@dataclass
class Diagnosis:
    code: Optional[str]
    source: str
    method: str
    confidence: float
    evidence: str
    reasoning: str
    human_review_required: bool
    review_reasons: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)
    alternatives: List[Tuple[str, float]] = field(default_factory=list)
    ai_code: Optional[str] = None
    ai_confidence: Optional[float] = None
    llm: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- lexicon classifier
def score_message(message: str, lexicon: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    text = (message or "").lower()
    out: Dict[str, Dict[str, Any]] = {}
    for code, signals in lexicon.items():
        if code.startswith("_"):
            continue
        score, hits = 0.0, []
        for s in signals:
            if re.search(s["pattern"], text, re.I):
                score += float(s["weight"])
                hits.append(f"{s['label']} ({s['weight']:+.2f})")
        out[code] = {"score": round(score, 3), "hits": hits}
    return out


def classify_text(message: str, step: Optional[str], lexicon: Dict[str, Any], rules: Dict[str, Any],
                  dsettings: Dict[str, Any]) -> Dict[str, Any]:
    scores = score_message(message, lexicon)
    ranked = sorted(scores.items(), key=lambda kv: kv[1]["score"], reverse=True)
    top_code, top = ranked[0]
    second_score = ranked[1][1]["score"] if len(ranked) > 1 else 0.0
    s1 = max(0.0, min(1.0, top["score"]))
    margin = max(0.0, min(1.0, top["score"] - second_score))
    conf = 0.6 * s1 + 0.4 * margin
    stage_note = ""
    expected = rules.get("expected_stage_by_code", {}).get(top_code, [])
    if step and expected:
        if step in expected:
            conf += float(dsettings.get("stage_consistency_bonus", 0.08))
            stage_note = f"Onboarding stage '{step}' is consistent with {top_code}."
        else:
            conf -= float(dsettings.get("stage_mismatch_penalty", 0.15))
            stage_note = f"Onboarding stage '{step}' is unusual for {top_code} (expected {', '.join(expected)})."
    conf = max(0.0, min(float(dsettings.get("inferred_confidence_cap", 0.97)), conf))
    if top["score"] <= 0.3:
        top_code, conf = None, min(conf, 0.3)
    return {
        "code": top_code,
        "confidence": round(conf, 3),
        "signals": top["hits"] if top_code else [],
        "alternatives": [(c, v["score"]) for c, v in ranked[1:4] if v["score"] > 0],
        "stage_note": stage_note,
        "top_score": top["score"],
        "margin": round(margin, 3),
    }


# ---------------------------------------------------------------- LLM prompt helpers
def build_llm_diagnosis_prompt(app: Application, catalog: Dict[str, CatalogEntry], template: str) -> Tuple[str, str]:
    cat_lines = "\n".join(
        f"- {c.code}: {c.reviewer_reason} | meaning: {c.plain_language_meaning} | fixable: {c.fixable}"
        for c in catalog.values()
    )
    system, _, user_tpl = template.partition("---USER---")
    user = (user_tpl.replace("{{CATALOG}}", cat_lines)
            .replace("{{MESSAGE}}", app.rejection_message or "")
            .replace("{{STEP}}", app.step_reached or "")
            .replace("{{STATUS}}", app.application_status or ""))
    return system.strip(), user.strip()


def validate_llm_diagnosis(raw: Dict[str, Any], message: str, catalog: Dict[str, CatalogEntry]) -> Dict[str, Any]:
    code = str(raw.get("code", "")).strip().upper()
    issues = []
    if code not in catalog:
        issues.append(f"LLM returned code '{code}' which is not in the catalogue")
        code = None
    try:
        conf = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    evidence = str(raw.get("evidence", "")).strip()
    if evidence and evidence.lower() not in (message or "").lower():
        issues.append("LLM evidence is not a verbatim quote from the reviewer message - discarded")
        evidence = ""
    return {"code": code, "confidence": conf, "evidence": evidence,
            "reasoning": str(raw.get("reasoning", ""))[:600], "issues": issues}


# ---------------------------------------------------------------- main entry
def diagnose(app: Application, catalog: Dict[str, CatalogEntry], lexicon: Dict[str, Any], rules: Dict[str, Any],
             settings: Dict[str, Any], review: Optional[Dict[str, Any]] = None,
             llm_result: Optional[Dict[str, Any]] = None) -> Diagnosis:
    ds = settings["diagnosis"]
    threshold = float(ds.get("auto_accept_confidence", 0.75))
    always_confirm = set(ds.get("always_human_confirm_inferred_codes", ["R10", "R12"]))
    msg = app.rejection_message
    code = app.rejection_code

    # ---- machine diagnosis first (kept even when a human overrides, for the audit trail)
    if code and code in catalog:
        d = Diagnosis(code=code, source=SOURCE_WORKBOOK, method="catalogue_lookup", confidence=1.0,
                      evidence=msg or "(no reviewer message)",
                      reasoning=f"rejection_code {code} recorded in the workbook; definition and fix taken from P3_RejectionCatalog.",
                      human_review_required=False)
        if msg:
            chk = classify_text(msg, app.step_reached, lexicon, rules, ds)
            if chk["code"] and chk["code"] != code and chk["confidence"] >= 0.6:
                d.human_review_required = True
                d.review_reasons.append(
                    f"Reviewer text reads like {chk['code']} but the recorded code is {code} - verify (code kept as recorded).")
    elif code and code not in catalog:
        d = Diagnosis(code=None, source=SOURCE_UNKNOWN, method="catalogue_lookup", confidence=0.0,
                      evidence=msg or "", reasoning=f"Code '{code}' is not in the catalogue; not interpreted.",
                      human_review_required=True, review_reasons=[f"Unknown rejection code '{code}'"])
    elif msg:
        c = classify_text(msg, app.step_reached, lexicon, rules, ds)
        reasoning = [f"Matched signals: {'; '.join(c['signals']) or 'none'}."]
        if c["stage_note"]:
            reasoning.append(c["stage_note"])
        if c["alternatives"]:
            reasoning.append("Runner-up codes: " + ", ".join(f"{a} ({s:.2f})" for a, s in c["alternatives"]) + ".")
        d = Diagnosis(code=c["code"], source=SOURCE_INFERRED, method="lexicon", confidence=c["confidence"],
                      evidence=msg, reasoning=" ".join(reasoning), human_review_required=False,
                      signals=c["signals"], alternatives=c["alternatives"])
        if llm_result:
            v = llm_result
            d.llm = v
            if v.get("code") and c["code"] is None:
                # Lexicon abstained (no evidence either way) - not a disagreement. Use the LLM, capped.
                d.method = "llm (lexicon abstained)"
                d.code = v["code"]
                d.confidence = round(min(v["confidence"], 0.85), 3)
                d.source = SOURCE_INFERRED
                d.reasoning += f" Lexicon found no strong signal; LLM selected {v['code']} ({v['confidence']:.2f}): {v.get('reasoning', '')}"
                if v.get("evidence"):
                    d.evidence = v["evidence"]
            elif v.get("code") and v["code"] == c["code"]:
                d.method = "lexicon+llm (agree)"
                d.confidence = round(min(float(ds.get("inferred_confidence_cap", 0.97)),
                                         (c["confidence"] + v["confidence"]) / 2 + 0.05), 3)
                d.reasoning += f" LLM independently selected {v['code']} ({v['confidence']:.2f}): {v.get('reasoning', '')}"
            elif v.get("code"):
                d.method = "lexicon+llm (disagree)"
                d.confidence = round(min(c["confidence"], v["confidence"], 0.5), 3)
                d.review_reasons.append(f"Lexicon chose {c['code']} but LLM chose {v['code']} - human verification needed.")
                d.alternatives = [(v["code"], v["confidence"])] + d.alternatives
                d.reasoning += f" LLM disagreed and selected {v['code']}: {v.get('reasoning', '')}"
        if d.code is None:
            d.source = SOURCE_UNKNOWN
            d.review_reasons.append("No catalogue code matched the reviewer text with enough evidence.")
        if d.confidence < threshold:
            d.review_reasons.append(f"Confidence {d.confidence:.2f} is below the auto-accept threshold {threshold:.2f}.")
        if d.code in always_confirm:
            d.review_reasons.append(f"{d.code} was inferred (not recorded); closure/compliance outcomes must be confirmed by an authorised human.")
        d.human_review_required = bool(d.review_reasons)
        d.ai_code, d.ai_confidence = d.code, d.confidence
    else:
        min_days = int(ds.get("abandonment_min_inactive_days", 3))
        days = app.days_since_last_activity
        if app.application_status == "Stalled" and days is not None and days >= min_days:
            d = Diagnosis(code="R13", source=SOURCE_ABANDON, method="abandonment_rule", confidence=0.95,
                          evidence=(f"No rejection code or reviewer message; stopped at '{app.step_reached}' "
                                    f"with {days} day(s) since last activity and {app.attempts} attempt(s)."),
                          reasoning=(f"Status is Stalled, there is no rejection on file, and inactivity ({days}d) "
                                     f">= abandonment threshold ({min_days}d) -> R13 Abandoned application."),
                          human_review_required=False)
        elif app.application_status == "Stalled":
            d = Diagnosis(code="R13", source=SOURCE_ABANDON, method="abandonment_rule", confidence=0.6,
                          evidence=f"No rejection on file; last activity {days} day(s) ago at '{app.step_reached}'.",
                          reasoning=f"Inactivity below the {min_days}-day abandonment threshold - merchant may still be in progress.",
                          human_review_required=True,
                          review_reasons=["Recently active - may not be abandoned yet; confirm before nudging."])
        else:
            d = Diagnosis(code=None, source=SOURCE_UNKNOWN, method="none", confidence=0.0, evidence="",
                          reasoning=f"Status '{app.application_status}' with no code or message - cannot diagnose without inventing a reason.",
                          human_review_required=True, review_reasons=["Rejected without a code or reviewer message."])

    # ---- authorised human verification overrides the machine diagnosis (never the catalogue rules)
    if review and review.get("verified_code") in catalog:
        d.ai_code = d.ai_code or d.code
        d.ai_confidence = d.ai_confidence if d.ai_confidence is not None else d.confidence
        d.code = review["verified_code"]
        d.source = SOURCE_HUMAN
        d.method = f"human_{review.get('action', 'verified')}"
        d.confidence = 1.0
        d.human_review_required = False
        d.review_reasons = []
        d.reasoning = (f"{review.get('action', 'verified').capitalize()} by {review.get('reviewer', 'reviewer')} "
                       f"on {review.get('ts', '')}: {review.get('note', '')}. Machine suggestion was {d.ai_code} "
                       f"({(d.ai_confidence or 0):.2f}).")
    return d


def llm_cache_key(app: Application) -> str:
    return "diag::" + json.dumps([app.application_id, app.rejection_message, app.step_reached], ensure_ascii=False)
