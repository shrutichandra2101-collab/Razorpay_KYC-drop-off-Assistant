"""CLI: process the workbook end-to-end, write exports and validation results.

    python scripts/run_pipeline.py            # uses WORKBOOK_PATH or auto-detects the workbook
    python scripts/run_pipeline.py --workbook path/to/file.xlsx
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recovery import config  # noqa: E402
from recovery.export import write_exports  # noqa: E402
from recovery.ingest import load_workbook  # noqa: E402
from recovery.pipeline import run  # noqa: E402
from recovery.store import Store  # noqa: E402
from recovery.validation import run_validation, write_results  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workbook", help="Path to Buildathon_Student_Data_Pack.xlsx")
    args = ap.parse_args()
    settings = config.load_settings()
    path = Path(args.workbook) if args.workbook else config.resolve_workbook_path(settings)
    ds = load_workbook(path)
    if not ds.report.ok:
        sys.exit(f"Ingestion failed: {ds.report.errors}")
    res = run(ds, settings, Store())
    paths = write_exports(res, config.OUTPUTS_DIR)
    s = res.summary
    print(f"Workbook: {path.name}  sha256={ds.report.sha256[:12]}")
    print(f"Applications {s['total']} | activated {s['activated']} | non-activated {s['non_activated']}")
    print(f"Recover {s['recoverable']} | Close {s['closure']} | Compliance {s['compliance']} | human review {s['human_review']}")
    print(f"Expected monthly TPV linked to recoverable applications: INR {s['tpv_recoverable_lakh']} lakh (expected, not actual revenue)")
    print("Top 3 systemic causes:", ", ".join(f"{t['code']} ({t['applications']})" for t in s["top3"]))
    print("Ship first:", s["ship_first"]["theme"])
    v = run_validation(ds, settings)
    write_results(v)
    print(f"Validation: {v['test_cases']['passed']}/{v['test_cases']['total']} cases passed; "
          f"policy violations {v['metrics']['policy_violations']}")
    for p in paths:
        print("wrote", p)


if __name__ == "__main__":
    main()
