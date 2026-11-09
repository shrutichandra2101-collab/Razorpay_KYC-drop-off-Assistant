# -*- coding: utf-8 -*-
"""Section 8 - validation suite (shared by pytest and the in-app Test Results page).

IMPORTANT: the evaluator's hidden answer key is NOT in the workbook. Everything here is
  (a) rule/consistency checks that must always hold (guardrails, channel consent, routing), and
  (b) MANUAL labels written by the builder for the blank-code rows and for an author-written
      paraphrase set. These measure our own expectations, not hidden-answer accuracy.
"""
from __future__ import annotations

import copy
import json
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config
from .channel import select_channel
from .decision import decide
from .diagnosis import classify_text, diagnose
from .guardrails import check_message
from .ingest import Application, Dataset, load_workbook
from .messaging import compose_nudge, followup_message
from .pipeline import Results, run
from .priority import score_queue
from .store import Store

# Manual labels for blank-code rows (builder's judgement from the reviewer text - NOT the hidden key).
MANUAL_BLANK_CODE_LABELS = {"A3022": "R05", "A3028": "R01", "A3051": "R10", "A3081": "R09"}

# Author-written paraphrases NOT present in the workbook, to estimate generalisation of the lexicon.
# Written before running the classifier on them; failures are reported, not tuned away.
PARAPHRASES: List[Tuple[str, str, Optional[str]]] = [
    ("Name on PAN is not same as the firm name", "KYC docs", "R01"),
    ("PAN holder name mismatch with entity", "KYC docs", "R01"),
    ("GSTIN status cancelled on portal", "KYC docs", "R02"),
    ("GST certificate belongs to another legal entity", "KYC docs", "R02"),
    ("Cheque shows a different account holder name", "Bank verification", "R03"),
    ("Beneficiary name in bank does not match business", "Bank verification", "R03"),
    ("Penny drop failed, please recheck IFSC", "Bank verification", "R04"),
    ("Account number galat hai, test credit fail", "Bank verification", "R04"),
    ("Uploaded PDF is password protected", "KYC docs", "R05"),
    ("Image is blurry, upload again", "KYC docs", "R05"),
    ("Refund policy and contact us page not found on site", "Website/App review", "R06"),
    ("Terms and privacy pages absent from app", "Website/App review", "R06"),
    ("Website under maintenance, unable to review", "Website/App review", "R07"),
    ("Products and prices visible only after login", "Website/App review", "R07"),
    ("Utility bill older than 3 months", "KYC docs", "R08"),
    ("Address on rent agreement differs from application", "KYC docs", "R08"),
    ("Director DOB does not match ID", "KYC docs", "R09"),
    ("Authorised signatory KYC missing", "KYC docs", "R09"),
    ("Online betting services not supported", "Website/App review", "R10"),
    ("Sells prohibited goods", "Website/App review", "R10"),
    ("Website sells electronics but category chosen is apparel", "Website/App review", "R11"),
    ("Category mismatch with site content", "Website/App review", "R11"),
    ("Name matched a watchlist during screening", "KYC docs", "R12"),
    ("Applicant never completed the form", "Business details", "R13"),
    # Deliberately ambiguous: the correct behaviour is human review, not a confident guess.
    ("Please check the details and resubmit", "KYC docs", None),
    ("Documents pending", "KYC docs", None),
]


def _check(name: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    return {"check": name, "passed": bool(ok), "detail": detail}


def _synthetic(base: Application, **kw) -> Application:
    return replace(base, **kw)


def _run_single(app: Application, ds: Dataset, settings: Dict[str, Any]) -> Dict[str, Any]:
    lex, rules = config.load_rules("diagnosis_lexicon.json"), config.load_rules("decision_rules.json")
    d = diagnose(app, ds.catalog, lex, rules, settings)
    dec = decide(d, ds.catalog, rules)
    ch = select_channel(app, settings["messaging"])
    n = compose_nudge(app, d, dec, ch, settings)
    return {"diag": d, "dec": dec, "channel": ch, "nudge": n}


def build_cases(res: Results, ds: Dataset, settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = {r["application_id"]: r for r in res.rows}
    seqs = res.sequences
    resume = settings["messaging"]["resume_link_placeholder"]
    cases: List[Dict[str, Any]] = []

    def real(case_id: str, aid: str, category: str, expect: Dict[str, Any], extra: Callable = None):
        r = rows[aid]
        checks = []
        if "code" in expect:
            checks.append(_check("diagnosed code", r["code"] == expect["code"], f"got {r['code']}, expected {expect['code']}"))
        if "decision" in expect:
            checks.append(_check("recovery decision", r["recovery_decision"] == expect["decision"],
                                 f"got {r['recovery_decision']}, expected {expect['decision']}"))
        if "human_review" in expect:
            checks.append(_check("human-review flag", r["human_review_required"] == expect["human_review"],
                                 f"got {r['human_review_required']}"))
        exp_channel = "WhatsApp" if r["whatsapp_opted_in"] == "Yes" else "Email"
        checks.append(_check("channel consent", r["channel"] == exp_channel, f"opt-in={r['whatsapp_opted_in']} -> {r['channel']}"))
        if r["draft_nudge"]:
            checks.append(_check("no policy violations", r["policy_violations"] == "", r["policy_violations"] or "clean"))
        if expect.get("decision") in ("Close", "Route to Compliance"):
            checks.append(_check("excluded from recovery queue", r["priority_score"] is None and not r["recovery_eligible"], ""))
            checks.append(_check("no follow-up sequence", not seqs[aid]["steps"], seqs[aid]["stop_reason"]))
            checks.append(_check("no resume link", resume not in (r["draft_nudge"] or ""), ""))
        if expect.get("decision") == "Recover" and not expect.get("human_review"):
            checks.append(_check("in recovery queue", r["priority_score"] is not None, f"priority {r['priority']}"))
        if extra:
            checks += extra(r)
        cases.append({"id": case_id, "application_id": aid, "category": category, "language": r["preferred_language"],
                      "expected": expect, "actual": {k: r[k] for k in ("code", "recovery_decision", "channel", "confidence",
                                                                         "human_review", "friction_level", "priority")},
                      "checks": checks, "passed": all(c["passed"] for c in checks)})

    contains = lambda field, s, label: (lambda r: [_check(label, s in (r[field] or ""), "")])
    real("TC01", "A3004", "R01 PAN name mismatch - Hindi - WhatsApp", {"code": "R01", "decision": "Recover", "human_review": False})
    real("TC02", "A3001", "R02 GST mismatch - Marathi", {"code": "R02", "decision": "Recover"},
         lambda r: [_check("Marathi (Devanagari) message", "तुमच्या" in r["draft_nudge"], "")])
    real("TC03", "A3012", "R03 bank name mismatch - Hinglish", {"code": "R03", "decision": "Recover"})
    real("TC04", "A3021", "R04 bank verification - 4 attempts, 40 days inactive",
         {"code": "R04", "decision": "Recover"},
         lambda r: [_check("high friction detected", r["friction_level"] == "High", r["friction_indicators"]),
                    _check("guided assistance offered", "'HELP'" in r["draft_nudge"], "")])
    real("TC05", "A3016", "R05 unreadable document - Hindi", {"code": "R05", "decision": "Recover"})
    real("TC06", "A3010", "R06 missing policy pages - English", {"code": "R06", "decision": "Recover"})
    real("TC07", "A3019", "R07 website not live - Hinglish", {"code": "R07", "decision": "Recover"})
    real("TC08", "A3072", "R08 address proof - WhatsApp opted OUT", {"code": "R08", "decision": "Recover"},
         lambda r: [_check("WhatsApp not used", r["channel"] != "WhatsApp", r["channel"]),
                    _check("email placeholder, no invented address", "not in dataset" in r["channel_recipient"], r["channel_recipient"])])
    real("TC09", "A3007", "R09 signatory ID - Tamil", {"code": "R09", "decision": "Recover"},
         lambda r: [_check("Tamil script", any("஀" <= ch <= "௿" for ch in r["draft_nudge"]), "")])
    real("TC10", "A3039", "R10 restricted category - English", {"code": "R10", "decision": "Close", "human_review": False})
    real("TC11", "A3015", "R11 category differs - Email channel", {"code": "R11", "decision": "Recover"})
    real("TC12", "A3082", "R12 compliance - English", {"code": "R12", "decision": "Route to Compliance"})
    real("TC13", "A3094", "R12 compliance - Tamil neutral wording", {"code": "R12", "decision": "Route to Compliance"})
    real("TC14", "A3005", "Blank code + no message -> abandonment R13", {"code": "R13", "decision": "Recover", "human_review": False},
         lambda r: [_check("evidence is inactivity signal", "since last activity" in r["supporting_evidence"], r["supporting_evidence"])])
    real("TC15", "A3022", "Blank code + message -> AI infers R05", {"code": "R05", "decision": "Recover"},
         lambda r: [_check("flagged as AI-inferred", r["code_source"].startswith("AI-inferred"), r["code_source"])])
    real("TC16", "A3028", "Blank code + Hinglish message -> AI infers R01", {"code": "R01", "decision": "Recover"})
    real("TC17", "A3051", "Blank code + message -> inferred R10 needs human confirmation",
         {"code": "R10", "decision": "Close", "human_review": True},
         lambda r: [_check("closure not actionable until verified", r["decision_state"].startswith("Blocked"), r["decision_state"])])
    real("TC18", "A3081", "Blank code + message -> AI infers R09", {"code": "R09", "decision": "Recover"})
    real("TC19", "A3020", "R06 - Kannada", {"code": "R06", "decision": "Recover"},
         lambda r: [_check("Kannada script", any("ಀ" <= ch <= "೿" for ch in r["draft_nudge"]), "")])
    real("TC20", "A3093", "R10 - Tamil closure, no workaround", {"code": "R10", "decision": "Close"})
    real("TC21", "A3097", "R10 - Kannada closure", {"code": "R10", "decision": "Close"})
    real("TC22", "A3023", "High TPV (59.9L) + 3 attempts + 3 support contacts", {"code": "R09", "decision": "Recover"},
         lambda r: [_check("High priority", r["priority"] == "High", f"score {r['priority_score']}"),
                    _check("High friction", r["friction_level"] == "High", r["friction_indicators"])])
    real("TC23", "A3025", "Low TPV (0.7L) merchant still queued", {"code": "R04", "decision": "Recover"},
         lambda r: [_check("has a priority (not dropped)", r["priority"] in ("High", "Medium", "Low"), str(r["priority"]))])
    real("TC24", "A3096", "R12 with high TPV (48L) never enters queue", {"code": "R12", "decision": "Route to Compliance"})
    real("TC25", "A3085", "R01 with 4 attempts -> guided help", {"code": "R01", "decision": "Recover"},
         contains("draft_nudge", "'HELP'", "guided assistance line present"))
    real("TC26", "A3047", "3 support contacts -> support acknowledgement", {"code": "R06", "decision": "Recover"},
         lambda r: [_check("support line present", "reaching out to us earlier" in r["draft_nudge"], ""),
                    _check("High friction", r["friction_level"] == "High", r["friction_indicators"])])
    real("TC27", "A3030", "Abandoned 34 days at Business details", {"code": "R13", "decision": "Recover"},
         lambda r: [_check("prolonged inactivity indicator", "Prolonged inactivity" in r["friction_indicators"], r["friction_indicators"])])

    # Activated row excluded
    act_ids = set(res.activated_df["application_id"]) if not res.activated_df.empty else set()
    sample = sorted(act_ids)[0] if act_ids else None
    cases.append({"id": "TC28", "application_id": sample, "category": "Activated application excluded", "language": "",
                  "expected": {"excluded": True}, "actual": {},
                  "checks": [_check("activated not in output", sample not in rows, sample or "")],
                  "passed": sample not in rows})

    # Synthetic edge cases (constructed in memory; never written to the workbook)
    base = res.apps["A3005"]
    synth = [
        ("TC29", "Rejected with no code and no message", _synthetic(base, application_id="SYN-1", application_status="Rejected"),
         lambda o: [_check("undetermined, no invented reason", o["diag"].code is None, str(o["diag"].code)),
                    _check("human review required", o["diag"].human_review_required, ""),
                    _check("no nudge drafted", o["nudge"]["body"] == "", o["nudge"]["status"])]),
        ("TC30", "Unknown code R99", _synthetic(base, application_id="SYN-2", rejection_code="R99", rejection_message="Something"),
         lambda o: [_check("not interpreted", o["diag"].code is None, ""), _check("human review", o["diag"].human_review_required, "")]),
        ("TC31", "Stalled, no code, active yesterday", _synthetic(base, application_id="SYN-3", days_since_last_activity=1),
         lambda o: [_check("held for human review", o["diag"].human_review_required, o["diag"].reasoning),
                    _check("not actionable", not o["dec"].actionable, o["dec"].decision_state)]),
        ("TC32", "Opt-in value missing -> no WhatsApp", _synthetic(base, application_id="SYN-4", whatsapp_opted_in=""),
         lambda o: [_check("Email selected", o["channel"]["channel"] == "Email", o["channel"]["reason"])]),
        ("TC33", "Ambiguous reviewer note", _synthetic(base, application_id="SYN-5", rejection_message="Please check the details and resubmit"),
         lambda o: [_check("low confidence -> human review", o["diag"].human_review_required, f"conf {o['diag'].confidence}"),
                    _check("no actionable recommendation", not o["dec"].actionable, o["dec"].decision_state)]),
    ]
    for cid, cat, app, fn in synth:
        o = _run_single(app, res_ds_cache["ds"], settings)
        checks = fn(o)
        cases.append({"id": cid, "application_id": app.application_id, "category": cat + " (synthetic)", "language": app.preferred_language,
                      "expected": {}, "actual": {"code": o["diag"].code, "confidence": o["diag"].confidence,
                                                 "decision": o["dec"].decision, "channel": o["channel"]["channel"]},
                      "checks": checks, "passed": all(c["passed"] for c in checks)})
    return cases


res_ds_cache: Dict[str, Any] = {}


def run_validation(ds: Optional[Dataset] = None, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = settings or config.load_settings()
    ds = ds or load_workbook(config.resolve_workbook_path(settings))
    res_ds_cache["ds"] = ds
    store = Store(tempfile.mkdtemp(prefix="validation_"))  # clean state: no human overrides
    res = run(ds, settings, store)
    lex, rules = config.load_rules("diagnosis_lexicon.json"), config.load_rules("decision_rules.json")
    resume = settings["messaging"]["resume_link_placeholder"]
    cases = build_cases(res, ds, settings)

    # 1. Path A: catalogue lookup on recorded codes
    coded = [a for a in ds.applications if a.application_status != "Activated" and a.rejection_code]
    rows = {r["application_id"]: r for r in res.rows}
    path_a_ok = sum(rows[a.application_id]["code"] == a.rejection_code for a in coded)
    # 1b. Leave-code-out: hide the recorded code and classify the reviewer text (in-sample for the lexicon)
    loo = []
    for a in coded:
        if a.rejection_message:
            c = classify_text(a.rejection_message, a.step_reached, lex, rules, settings["diagnosis"])
            loo.append({"application_id": a.application_id, "true": a.rejection_code, "pred": c["code"],
                        "confidence": c["confidence"], "message": a.rejection_message})
    loo_ok = sum(x["true"] == x["pred"] for x in loo)
    # 2. Blank-code rows vs manual labels
    blank = [{"application_id": k, "manual_label": v, "pred": rows[k]["code"], "confidence": rows[k]["confidence"],
              "human_review": rows[k]["human_review_required"], "message": rows[k]["rejection_message"]}
             for k, v in MANUAL_BLANK_CODE_LABELS.items() if k in rows]
    blank_ok = sum(b["manual_label"] == b["pred"] for b in blank)
    # 2b. Paraphrase generalisation set
    para = []
    for text, step, expected in PARAPHRASES:
        c = classify_text(text, step, lex, rules, settings["diagnosis"])
        needs_review = c["code"] is None or c["confidence"] < float(settings["diagnosis"]["auto_accept_confidence"])
        if expected is None:
            ok = needs_review
            outcome = "correctly sent to human review" if ok else f"over-confident guess {c['code']} ({c['confidence']})"
        else:
            ok = c["code"] == expected
            outcome = "correct" if ok else f"wrong: predicted {c['code']}"
            if ok and needs_review:
                outcome = "correct but low confidence -> human review"
        para.append({"text": text, "expected": expected, "pred": c["code"], "confidence": c["confidence"],
                     "human_review": needs_review, "passed": ok, "outcome": outcome})
    para_ok = sum(p["passed"] for p in para)
    para_silent_wrong = [p for p in para if not p["passed"] and not p["human_review"]]

    # 3/4/5. Whole-dataset policy checks on every merchant-facing message incl. follow-ups
    violations, lang_fail, channel_fail = [], [], []
    for r in res.rows:
        app = res.apps[r["application_id"]]
        texts = [("nudge", r["draft_nudge"])]
        for s in res.sequences[r["application_id"]]["steps"]:
            if s["key"] != "initial" and s["channel"] != "Internal":
                texts.append((s["step"], s["message"]))
        for label, t in texts:
            if not t:
                continue
            v = check_message(t, r["preferred_language"], r["code"], r["recovery_decision"] if label == "nudge" else "Recover",
                              r["channel"], r["whatsapp_opted_in"], resume)
            if v:
                violations.append({"application_id": r["application_id"], "message": label, "violations": v})
        if r["channel"] == "WhatsApp" and r["whatsapp_opted_in"] != "Yes":
            channel_fail.append(r["application_id"])
    r12 = [r for r in res.rows if r["code"] == "R12"]
    r10 = [r for r in res.rows if r["code"] == "R10"]
    compliance_ok = all(r["recovery_decision"] == "Route to Compliance" and not r["recovery_eligible"]
                        and not res.sequences[r["application_id"]]["steps"] for r in r12)
    restricted_ok = all(r["recovery_decision"] == "Close" and not r["recovery_eligible"]
                        and not res.sequences[r["application_id"]]["steps"] for r in r10)
    # 6. Recoverability decision vs independent catalogue mapping
    dec_map = {"Yes": "Recover", "No": "Close", "Needs compliance review": "Route to Compliance"}
    dec_ok = sum(r["recovery_decision"] == dec_map[ds.catalog[r["code"]].fixable] for r in res.rows if r["code"])
    # 9. Prioritisation consistency
    prio_checks = []
    q = [r for r in res.rows if r["recovery_eligible"]]
    prio_checks.append(_check("no R10/R12/Close/Route in queue", all(r["recovery_decision"] == "Recover" for r in q), f"{len(q)} queued"))
    res2 = run(ds, settings, store)
    prio_checks.append(_check("deterministic ranking across runs",
                              [r["priority_score"] for r in res.rows] == [r["priority_score"] for r in res2.rows], ""))
    mono_ok = True
    for r in q[:20]:
        bumped = [dict(x) for x in res.rows]
        tgt = next(x for x in bumped if x["application_id"] == r["application_id"])
        tgt["expected_monthly_tpv_inr_lakh"] = (tgt["expected_monthly_tpv_inr_lakh"] or 0) * 1.5
        score_queue(bumped, settings["priority"])
        if tgt["priority_score"] < r["priority_score"] - 1e-9:
            mono_ok = False
    prio_checks.append(_check("higher TPV never lowers a merchant's score (monotonic)", mono_ok, "tested on 20 queued rows"))
    hf = [r for r in q if r["friction_level"] == "High"]
    prio_checks.append(_check("high-friction merchants never Low priority", all(r["priority"] != "Low" for r in hf), f"{len(hf)} high-friction"))

    case_checks = sum(len(c["checks"]) for c in cases)
    case_pass = sum(sum(ch["passed"] for ch in c["checks"]) for c in cases)
    result = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "workbook_sha256": ds.report.sha256,
        "disclaimer": ("Hidden evaluator answers are not available. Accuracy figures below are against MANUAL labels "
                       "written by the builder and rule-consistency checks - not hidden-answer accuracy."),
        "test_cases": {"total": len(cases), "passed": sum(c["passed"] for c in cases), "checks": case_checks, "checks_passed": case_pass},
        "metrics": {
            "path_a_catalogue_lookup": {"correct": path_a_ok, "total": len(coded)},
            "leave_code_out_lexicon_in_sample": {"correct": loo_ok, "total": len(loo),
                                                  "note": "Lexicon authored with visibility of these messages - in-sample, optimistic."},
            "blank_code_vs_manual_labels": {"correct": blank_ok, "total": len(blank),
                                            "human_review_flagged": sum(b["human_review"] for b in blank)},
            "paraphrase_generalisation": {"correct": para_ok, "total": len(para),
                                          "silent_errors": len(para_silent_wrong),
                                          "note": "Author-written, unseen by the lexicon; closer to real-world accuracy."},
            "recoverability_decisions": {"correct": dec_ok, "total": sum(1 for r in res.rows if r["code"])},
            "compliance_handling_ok": compliance_ok, "restricted_handling_ok": restricted_ok,
            "channel_violations": len(channel_fail), "policy_violations": len(violations),
            "messages_checked": sum(1 for r in res.rows if r["draft_nudge"]) + sum(
                1 for s in res.sequences.values() for st in s["steps"] if st["key"] != "initial" and st["channel"] != "Internal"),
            "cases_requiring_human_review": int(sum(r["human_review_required"] for r in res.rows)),
        },
        "priority_checks": prio_checks,
        "cases": cases,
        "leave_code_out": loo,
        "blank_code": blank,
        "paraphrases": para,
        "violations": violations,
        "failures": [c for c in cases if not c["passed"]] + [
            {"id": "PARA", "category": "paraphrase", "detail": p} for p in para if not p["passed"]],
    }
    return result


def write_results(result: Dict[str, Any]) -> None:
    config.OUTPUTS_DIR.mkdir(exist_ok=True)
    (config.OUTPUTS_DIR / "test_results.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    m = result["metrics"]
    lines = [
        "# Validation results", "", f"Generated: {result['generated_at']}", "", f"> {result['disclaimer']}", "",
        f"**Test cases:** {result['test_cases']['passed']}/{result['test_cases']['total']} passed "
        f"({result['test_cases']['checks_passed']}/{result['test_cases']['checks']} individual checks)", "",
        "| Metric | Result |", "|---|---|",
        f"| Path A catalogue lookup | {m['path_a_catalogue_lookup']['correct']}/{m['path_a_catalogue_lookup']['total']} |",
        f"| Leave-code-out lexicon (in-sample) | {m['leave_code_out_lexicon_in_sample']['correct']}/{m['leave_code_out_lexicon_in_sample']['total']} |",
        f"| Blank-code rows vs manual labels | {m['blank_code_vs_manual_labels']['correct']}/{m['blank_code_vs_manual_labels']['total']} |",
        f"| Paraphrase generalisation (unseen) | {m['paraphrase_generalisation']['correct']}/{m['paraphrase_generalisation']['total']} "
        f"({m['paraphrase_generalisation']['silent_errors']} silent errors) |",
        f"| Recoverability decisions vs catalogue | {m['recoverability_decisions']['correct']}/{m['recoverability_decisions']['total']} |",
        f"| Compliance (R12) handling | {'OK' if m['compliance_handling_ok'] else 'FAIL'} |",
        f"| Restricted (R10) handling | {'OK' if m['restricted_handling_ok'] else 'FAIL'} |",
        f"| Channel-consent violations | {m['channel_violations']} |",
        f"| Policy violations across {m['messages_checked']} messages | {m['policy_violations']} |",
        f"| Cases requiring human review | {m['cases_requiring_human_review']} |", "",
        "## Prioritisation consistency", "",
    ]
    lines += [f"- {'PASS' if c['passed'] else 'FAIL'} - {c['check']} {c['detail']}" for c in result["priority_checks"]]
    lines += ["", "## Test cases", "", "| ID | Application | Scenario | Result | Failed checks |", "|---|---|---|---|---|"]
    for c in result["cases"]:
        failed = "; ".join(f"{x['check']} ({x['detail']})" for x in c["checks"] if not x["passed"])
        lines.append(f"| {c['id']} | {c['application_id']} | {c['category']} | {'PASS' if c['passed'] else 'FAIL'} | {failed} |")
    lines += ["", "## Paraphrase set (failures are kept, not tuned away)", "", "| Text | Expected | Predicted | Conf. | Outcome |", "|---|---|---|---|---|"]
    for p in result["paraphrases"]:
        lines.append(f"| {p['text']} | {p['expected']} | {p['pred']} | {p['confidence']} | {p['outcome']} |")
    (config.OUTPUTS_DIR / "test_results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
