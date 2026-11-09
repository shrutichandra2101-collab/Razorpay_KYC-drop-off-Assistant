"""25 questions with expected numbers computed DIRECTLY from the pipeline dataframe (independently of the
assistant's tools), used by pytest (rules path) and scripts/eval_assistant.py (Claude path)."""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Tuple

import pandas as pd


def expected_cases(res) -> List[Tuple[str, Callable[[], Any], str]]:
    """(question, expected_value_fn, kind). kind: 'number' must appear in the answer; 'id' must appear;
    'nav' = an open_cases action whose filters select exactly the expected count; 'refuse' = no numbers allowed."""
    df, s = res.df, res.summary
    n = lambda mask: int(mask.sum())
    tpv = lambda mask: round(float(df[mask]["expected_monthly_tpv_inr_lakh"].sum()), 1)
    top_queue = df[df["recovery_eligible"]].sort_values("priority_rank").iloc[0]["application_id"]
    biggest = df.sort_values("expected_monthly_tpv_inr_lakh", ascending=False).iloc[0]["application_id"]
    return [
        ("How many applications are stuck?", lambda: s["non_activated"], "number"),
        ("How many accounts are stuck in KYC?", lambda: n(df["onboarding_stage"] == "KYC docs"), "number"),
        ("How many are stuck at bank verification?", lambda: n(df["onboarding_stage"] == "Bank verification"), "number"),
        ("How many are stuck at website review?", lambda: n(df["onboarding_stage"] == "Website/App review"), "number"),
        ("How many merchants stopped midway without a rejection?", lambda: n(df["code"] == "R13"), "number"),
        ("How many have a PAN name mismatch?", lambda: n(df["code"] == "R01"), "number"),
        ("How many GST problems are there?", lambda: n(df["code"] == "R02"), "number"),
        ("How many cases are with compliance?", lambda: n(df["recovery_decision"] == "Route to Compliance"), "number"),
        ("How many applications must be closed?", lambda: n(df["recovery_decision"] == "Close"), "number"),
        ("How many applications are recoverable?", lambda: n(df["recovery_decision"] == "Recover"), "number"),
        ("How many need a human decision?", lambda: n(df["human_review_required"]), "number"),
        ("How many high priority cases are there?", lambda: n(df["priority"] == "High"), "number"),
        ("How many merchants are struggling?", lambda: n(df["friction_level"] == "High"), "number"),
        ("How many Tamil-speaking merchants are stuck?", lambda: n(df["preferred_language"] == "Tamil"), "number"),
        ("How many Hindi merchants are stuck at KYC?", lambda: n((df["preferred_language"] == "Hindi") & (df["onboarding_stage"] == "KYC docs")), "number"),
        ("How many rejected applications are there?", lambda: n(df["application_status"] == "Rejected"), "number"),
        ("How many applications are activated?", lambda: s["activated"], "number"),
        ("How many stuck merchants have been inactive for more than 30 days?", lambda: n(df["days_since_last_activity"] > 30), "number"),
        ("How much expected TPV is in recoverable cases?", lambda: s["tpv_recoverable_lakh"], "number"),
        ("What is the expected TPV of applications stuck at KYC?", lambda: tpv(df["onboarding_stage"] == "KYC docs"), "number"),
        ("Which case should I work on first?", lambda: top_queue, "id"),
        ("Which stuck merchant has the highest expected TPV?", lambda: biggest, "id"),
        ("Tell me about A3022", lambda: "unreadable", "text"),
        ("Show me the KYC cases", lambda: n(df["onboarding_stage"] == "KYC docs"), "nav"),
        ("What's the weather in Bengaluru today?", lambda: None, "refuse"),
    ]


def score(reply, expected, kind, res) -> Tuple[bool, str]:
    text = reply.answer
    nums = {x.replace(",", "") for x in re.findall(r"\d+(?:[.,]\d+)?", text)}
    if kind == "number":
        e = expected
        ok = (str(e) in nums) or (isinstance(e, float) and (f"{e:.1f}" in nums or (e.is_integer() and str(int(e)) in nums)))
        return ok, f"expected {e}"
    if kind == "id":
        return expected in text, f"expected {expected}"
    if kind == "text":
        return expected.lower() in text.lower(), f"expected mention of '{expected}'"
    if kind == "nav":
        from .assistant import apply_filters
        for a in reply.actions:
            if a["type"] == "open_cases" and len(apply_filters(res.df, a.get("filters") or {})) == expected:
                return True, ""
        return False, f"expected a button opening {expected} cases"
    if kind == "refuse":
        return not nums, "should decline without inventing numbers"
    return False, "unknown kind"
