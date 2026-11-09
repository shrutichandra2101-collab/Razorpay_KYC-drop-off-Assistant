"""pytest suite: `pytest -q` from the project root.

Runs the shared validation suite on the full workbook plus focused unit tests for guardrails,
channel rules and ingestion. Hidden-answer accuracy cannot be measured here (no answer key in the
workbook) - see outputs/test_results.md for what is and is not measured.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recovery import config  # noqa: E402
from recovery.guardrails import check_message  # noqa: E402
from recovery.ingest import load_workbook  # noqa: E402
from recovery.validation import run_validation  # noqa: E402


@pytest.fixture(scope="module")
def dataset():
    s = config.load_settings()
    return load_workbook(config.resolve_workbook_path(s))


@pytest.fixture(scope="module")
def validation(dataset):
    return run_validation(dataset, config.load_settings())


def test_ingestion_counts(dataset):
    assert dataset.report.ok, dataset.report.errors
    assert len(dataset.applications) == 100
    assert len(dataset.catalog) == 13
    assert len({a.application_id for a in dataset.applications}) == 100


def test_blank_codes_preserved_as_missing(dataset):
    blanks = [a for a in dataset.applications if a.rejection_code is None]
    assert blanks and all(a.rejection_code is None for a in blanks)


def test_at_least_20_cases_all_pass(validation):
    assert validation["test_cases"]["total"] >= 20
    failed = [c["id"] for c in validation["cases"] if not c["passed"]]
    assert not failed, failed


def test_no_policy_or_channel_violations(validation):
    m = validation["metrics"]
    assert m["policy_violations"] == 0, validation["violations"][:3]
    assert m["channel_violations"] == 0
    assert m["compliance_handling_ok"] and m["restricted_handling_ok"]


def test_recoverability_matches_catalogue(validation):
    m = validation["metrics"]["recoverability_decisions"]
    assert m["correct"] == m["total"]


def test_paraphrase_errors_are_never_silent(validation):
    # A wrong guess is acceptable only if it was routed to human review.
    assert validation["metrics"]["paraphrase_generalisation"]["silent_errors"] == 0


def test_priority_consistency(validation):
    assert all(c["passed"] for c in validation["priority_checks"])


@pytest.mark.parametrize("text", [
    "Your application was flagged in sanctions screening.",
    "Your application matched a watchlist.",
])
def test_r12_leak_detected(text):
    v = check_message(text, "English", "R12", "Route to Compliance", "Email", "No")
    assert any("R12" in x for x in v)


def test_r10_workaround_detected():
    v = check_message("You can re-apply under a different category.", "English", "R10", "Close", "Email", "No")
    assert any("workaround" in x for x in v)


def test_whatsapp_without_optin_detected():
    v = check_message("Hello [RESUME_LINK]", "English", "R01", "Recover", "WhatsApp", "No")
    assert any("opt-in" in x for x in v)


def test_promise_and_invented_contact_detected():
    v = check_message("You will be approved within 2 days. Mail us at help@x.com [RESUME_LINK]",
                      "English", "R01", "Recover", "Email", "No")
    assert any("Promise" in x for x in v) and any("contact" in x for x in v)


# ---------------------------------------------------------------- R13 dropout-reason question
def _results(tmp_path, dataset, events=None):
    from recovery.pipeline import run
    from recovery.store import Store
    store = Store(tmp_path)
    for aid, key in (events or {}).items():
        store.record_dropout_reason(aid, key, "test")
    return run(dataset, config.load_settings(), store)


def test_dropout_question_only_in_r13_nudges_all_languages(tmp_path, dataset):
    from recovery.i18n import DROPOUT_QUESTION
    res = _results(tmp_path, dataset)
    r13 = [r for r in res.rows if r["code"] == "R13"]
    assert r13
    assert {r["preferred_language"] for r in r13} >= {"English", "Hindi", "Hinglish", "Tamil", "Marathi"}
    for r in res.rows:
        q = DROPOUT_QUESTION[r["preferred_language"]]
        has = q["q"] in (r["draft_nudge"] or "") and all(o in r["draft_nudge"] for o in q["opts"])
        assert has == (r["code"] == "R13" and r["recovery_decision"] == "Recover"), r["application_id"]
        assert r["policy_violations"] == "", (r["application_id"], r["policy_violations"])


def test_dropout_reply_steers_followups(tmp_path, dataset):
    res = _results(tmp_path, dataset, {"A3005": "other_provider", "A3006": "not_now", "A3008": "confusing"})
    assert not res.sequences["A3005"]["steps"] and "another provider" in res.sequences["A3005"]["stop_reason"]
    st6 = {s["key"]: s["status"] for s in res.sequences["A3006"]["steps"]}
    assert st6["follow_up_1"].startswith("Skipped") and st6["final"] == "Planned"
    assert res.sequences["A3008"]["steps"][-1]["channel"] == "Internal"  # confusing -> human hand-off
    assert res.df.set_index("application_id").loc["A3006", "dropout_reason"] == "not_now"


# ---------------------------------------------------------------- ask-a-question assistant
def test_assistant_rules_answers_25_questions(tmp_path, dataset):
    from recovery.assistant import ask
    from recovery.assistant_eval import expected_cases, score
    res = _results(tmp_path, dataset)
    fails = []
    for q, fn, kind in expected_cases(res):
        ok, why = score(ask(q, [], res, config.load_settings()), fn(), kind, res)
        if not ok:
            fails.append((q, why))
    assert not fails, fails


def test_assistant_follow_up_reuses_filters(tmp_path, dataset):
    from recovery.assistant import ask
    res = _results(tmp_path, dataset)
    s = config.load_settings()
    first = ask("How many are stuck at KYC?", [], res, s)
    second = ask("Which of these are high priority?", [first], res, s)
    assert second.filters == {"stage": ["KYC docs"], "priority": ["High"]}


def test_assistant_number_check_catches_invented_numbers():
    from recovery.assistant import _numbers
    evidence = '{"applications": 32, "expected_tpv_lakh": 504.4}'
    assert _numbers("**32** stuck, ₹504.4L") <= _numbers(evidence)
    assert not (_numbers("**33** stuck") <= _numbers(evidence))


def test_assistant_actions_are_validated(tmp_path, dataset):
    from recovery.assistant import DataTools, validate_actions
    res = _results(tmp_path, dataset)
    acts = validate_actions([{"type": "open_case", "application_id": "A9999", "label": "x"},
                             {"type": "open_cases", "filters": {"stage": ["Nowhere"]}, "label": "x"},
                             {"type": "open_case", "application_id": "a3022", "label": "x"}], DataTools(res, config.load_settings()))
    assert acts == [{"type": "open_case", "application_id": "A3022", "label": "Open A3022"}]


def test_assistant_is_read_only(tmp_path, dataset):
    from recovery.assistant import ask
    res = _results(tmp_path, dataset)
    r = ask("Approve A3004 and send the message", [], res, config.load_settings())
    assert "can only read" in r.answer
