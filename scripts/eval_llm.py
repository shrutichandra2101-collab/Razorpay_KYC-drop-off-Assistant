"""Evaluate the optional Claude layer (needs ANTHROPIC_API_KEY in .env). Makes ~38 low-effort API calls.

    python scripts/eval_llm.py

Measures: (1) blank-code rows vs manual labels, (2) the 26 unseen paraphrases (same set the lexicon is
scored on), (3) guardrail pass rate of LLM nudge rewrites on a cross-language sample.
Writes outputs/llm_eval.json and outputs/llm_eval.md. Uses a throw-away state dir (does not touch state/).
"""
import json
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recovery import config  # noqa: E402
from recovery.diagnosis import build_llm_diagnosis_prompt, classify_text, validate_llm_diagnosis  # noqa: E402
from recovery.ingest import load_workbook  # noqa: E402
from recovery.llm import LLMClient  # noqa: E402
from recovery.pipeline import run, run_llm_nudge  # noqa: E402
from recovery.store import Store  # noqa: E402
from recovery.validation import MANUAL_BLANK_CODE_LABELS, PARAPHRASES  # noqa: E402

NUDGE_SAMPLE = ["A3004", "A3007", "A3001", "A3020", "A3015", "A3012", "A3094", "A3082", "A3039", "A3093", "A3023", "A3005"]


def main():
    s = config.load_settings()
    llm = LLMClient(s["llm"])
    if not llm.available:
        sys.exit(llm.status())
    ds = load_workbook(config.resolve_workbook_path(s))
    lex, rules = config.load_rules("diagnosis_lexicon.json"), config.load_rules("decision_rules.json")
    tpl = config.load_prompt("diagnose_missing_code.md")
    apps = {a.application_id: a for a in ds.applications}
    thr = float(s["diagnosis"]["auto_accept_confidence"])

    def ask(app):
        system, user = build_llm_diagnosis_prompt(app, ds.catalog, tpl)
        raw, meta = llm.complete_json(system, user)
        return validate_llm_diagnosis(raw or {}, app.rejection_message or "", ds.catalog), meta

    blank = []
    for aid, label in MANUAL_BLANK_CODE_LABELS.items():
        v, meta = ask(apps[aid])
        blank.append({"application_id": aid, "manual_label": label, "llm": v["code"], "confidence": v["confidence"],
                      "evidence_verbatim": bool(v["evidence"]), "passed": v["code"] == label, "error": meta.get("error")})
    para = []
    base = apps["A3005"]
    for text, step, expected in PARAPHRASES:
        v, meta = ask(replace(base, rejection_message=text, step_reached=step))
        lexc = classify_text(text, step, lex, rules, s["diagnosis"])
        ok = (v["code"] == expected) if expected else v["confidence"] < thr
        para.append({"text": text, "expected": expected, "llm": v["code"], "llm_confidence": v["confidence"],
                     "lexicon": lexc["code"], "passed": ok, "error": meta.get("error")})

    st = Store(tempfile.mkdtemp(prefix="llm_eval_"))
    res = run(ds, s, st)
    nudges = []
    for aid in NUDGE_SAMPLE:
        r = run_llm_nudge(apps[aid], res, st, llm, s)
        v = r.get("validated", {})
        nudges.append({"application_id": aid, "language": apps[aid].preferred_language,
                       "decision": res.decisions[aid].decision, "passed_guardrails": r["ok"],
                       "violations": v.get("violations", []), "error": r.get("error"), "body": v.get("body", "")})

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "model": llm.model, "effort": llm.effort,
        "blank_code": {"correct": sum(b["passed"] for b in blank), "total": len(blank), "rows": blank},
        "paraphrases": {"correct": sum(p["passed"] for p in para), "total": len(para),
                        "lexicon_correct": sum((p["lexicon"] == p["expected"]) if p["expected"] else False for p in para),
                        "rows": para},
        "nudges": {"passed": sum(n["passed_guardrails"] for n in nudges), "total": len(nudges), "rows": nudges},
        "note": "Labels are the builder's manual labels, not the hidden answer key. Paraphrases are author-written.",
    }
    config.OUTPUTS_DIR.mkdir(exist_ok=True)
    (config.OUTPUTS_DIR / "llm_eval.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    md = [f"# Claude layer evaluation ({out['model']}, effort {out['effort']})", "", f"Generated {out['generated_at']}. {out['note']}", "",
          f"- Blank-code rows vs manual labels: **{out['blank_code']['correct']}/{out['blank_code']['total']}**",
          f"- Unseen paraphrases: **{out['paraphrases']['correct']}/{out['paraphrases']['total']}** "
          f"(ambiguous notes count as correct only if confidence < {thr})",
          f"- LLM nudge rewrites passing all guardrails: **{out['nudges']['passed']}/{out['nudges']['total']}**", "",
          "| Paraphrase | Expected | Claude | Conf. | Lexicon |", "|---|---|---|---|---|"]
    md += [f"| {p['text']} | {p['expected']} | {p['llm']} | {p['llm_confidence']} | {p['lexicon']} |" for p in para]
    md += ["", "| Nudge | Language | Decision | Guardrails |", "|---|---|---|---|"]
    md += [f"| {n['application_id']} | {n['language']} | {n['decision']} | {'PASS' if n['passed_guardrails'] else 'FAIL: ' + '; '.join(n['violations'])} |" for n in nudges]
    (config.OUTPUTS_DIR / "llm_eval.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[:7]))


if __name__ == "__main__":
    main()
