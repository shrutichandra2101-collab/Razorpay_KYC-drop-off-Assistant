"""Evaluate the ask-a-question assistant on 25 questions whose answers are computed directly from the data.

    python scripts/eval_assistant.py            # rules + Claude (needs ANTHROPIC_API_KEY; ~50 low-effort calls)
    python scripts/eval_assistant.py --rules    # offline rules only

Writes outputs/assistant_eval.json and outputs/assistant_eval.md. Uses a throw-away state dir.
"""
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recovery import config  # noqa: E402
from recovery.assistant import ask  # noqa: E402
from recovery.assistant_eval import expected_cases, score  # noqa: E402
from recovery.ingest import load_workbook  # noqa: E402
from recovery.llm import LLMClient  # noqa: E402
from recovery.pipeline import run  # noqa: E402
from recovery.store import Store  # noqa: E402


def main():
    s = config.load_settings()
    ds = load_workbook(config.resolve_workbook_path(s))
    res = run(ds, s, Store(tempfile.mkdtemp(prefix="asst_eval_")))
    llm = LLMClient(s["llm"])
    modes = [("rules", None)] + ([] if "--rules" in sys.argv or not llm.available else [("claude", llm)])
    out = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "model": llm.model, "modes": {}}
    for name, client in modes:
        rows = []
        for q, fn, kind in expected_cases(res):
            r = ask(q, [], res, s, client)
            ok, why = score(r, fn(), kind, res)
            rows.append({"question": q, "kind": kind, "passed": ok, "detail": why, "answer": r.answer, "source": r.source,
                         "seconds": r.seconds, "buttons": [a["label"] for a in r.actions], "issues": r.issues})
        out["modes"][name] = {"correct": sum(x["passed"] for x in rows), "total": len(rows),
                              "fell_back_to_rules": sum(1 for x in rows if x["source"].startswith("rules")) if name == "claude" else None,
                              "avg_seconds": round(sum(x["seconds"] for x in rows) / len(rows), 1), "rows": rows}
    config.OUTPUTS_DIR.mkdir(exist_ok=True)
    (config.OUTPUTS_DIR / "assistant_eval.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    md = [f"# Assistant evaluation ({out['generated_at']})", "",
          "Expected answers are computed directly from the pipeline data, independently of the assistant's tools.", ""]
    for name, m in out["modes"].items():
        extra = f", {m['fell_back_to_rules']} answers replaced by the rules fallback" if m["fell_back_to_rules"] is not None else ""
        md.append(f"- **{name}**: {m['correct']}/{m['total']} correct, avg {m['avg_seconds']}s{extra}")
    md += ["", "| Question | " + " | ".join(out["modes"]) + " |", "|---|" + "---|" * len(out["modes"])]
    for i, (q, _, _) in enumerate(expected_cases(res)):
        md.append(f"| {q} | " + " | ".join(("PASS" if m["rows"][i]["passed"] else "FAIL - " + m["rows"][i]["detail"]) for m in out["modes"].values()) + " |")
    (config.OUTPUTS_DIR / "assistant_eval.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[:6 + len(out["modes"])]))
    for name, m in out["modes"].items():
        for x in m["rows"]:
            if not x["passed"]:
                print(f"[{name}] FAIL: {x['question']} | {x['detail']} | {x['answer'][:160]} | {x['issues']}")


if __name__ == "__main__":
    main()
