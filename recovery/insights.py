"""Features 13 & 14 - TPV analytics and product improvement recommendations.

All figures are computed from the pipeline output (which is computed from the workbook).
Expected monthly TPV is the merchant's *declared expectation* - it is NOT actual, recovered or
guaranteed revenue.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd


def by_code(df: pd.DataFrame) -> pd.DataFrame:
    n = len(df)
    g = (df.groupby(["code", "diagnosed_reason", "fixability", "recovery_decision"], dropna=False)
         .agg(applications=("application_id", "count"), expected_tpv_lakh=("expected_monthly_tpv_inr_lakh", "sum"),
              inferred=("code_source", lambda s: int((s == "AI-inferred from reviewer text").sum())))
         .reset_index())
    g["pct_of_non_activated"] = (100 * g["applications"] / max(n, 1)).round(1)
    g["expected_tpv_lakh"] = g["expected_tpv_lakh"].round(1)
    return g.sort_values(["applications", "expected_tpv_lakh"], ascending=False).reset_index(drop=True)


def top_systemic_causes(df: pd.DataFrame, k: int = 3) -> pd.DataFrame:
    """Top fixable *rejection* causes (abandonment R13 is reported separately as a journey problem)."""
    t = by_code(df)
    t = t[(t["recovery_decision"] == "Recover") & (t["code"] != "R13")]
    return t.head(k).reset_index(drop=True)


def abandonment_summary(df: pd.DataFrame) -> Dict[str, Any]:
    a = df[df["code"] == "R13"]
    return {
        "applications": int(len(a)),
        "expected_tpv_lakh": round(float(a["expected_monthly_tpv_inr_lakh"].sum()), 1),
        "pct_of_non_activated": round(100 * len(a) / max(len(df), 1), 1),
        "by_stage": a.groupby("onboarding_stage")["application_id"].count().sort_values(ascending=False).to_dict(),
        "median_days_inactive": float(a["days_since_last_activity"].median()) if len(a) else 0.0,
    }


def _theme_stats(fixable: pd.DataFrame, interventions: Dict[str, Any],
                 preventability_override: Dict[str, float] = None) -> List[Dict[str, Any]]:
    out = []
    for t in interventions["themes"]:
        p = float((preventability_override or {}).get(t["id"], t["preventability"]))
        sub = fixable[fixable["code"].isin(t["codes"])]
        n, tpv = len(sub), float(sub["expected_monthly_tpv_inr_lakh"].sum())
        out.append({"t": t, "p": p, "n": n, "tpv": tpv, "prev_n": n * p, "prev_tpv": tpv * p})
    sn = sum(x["prev_n"] for x in out) or 1e-9
    st = sum(x["prev_tpv"] for x in out) or 1e-9
    for x in out:
        x["score"] = 50 * x["prev_n"] / sn + 50 * x["prev_tpv"] / st
    return out


def product_recommendations(df: pd.DataFrame, interventions: Dict[str, Any]) -> pd.DataFrame:
    """Ship-first score = 50 * share of expected-preventable applications + 50 * share of
    expected-preventable TPV, where expected-preventable = count (or TPV) x assumed preventability.
    Frequency, TPV exposure and the nature of the friction (via preventability) all enter."""
    fixable = df[df["recovery_decision"] == "Recover"]
    rows: List[Dict[str, Any]] = []
    for x in _theme_stats(fixable, interventions):
        t, n, tpv = x["t"], x["n"], x["tpv"]
        rows.append({
            "theme_id": t["id"], "theme": t["name"], "codes": ", ".join(t["codes"]), "applications": n,
            "pct_of_non_activated": round(100 * n / max(len(df), 1), 1),
            "expected_tpv_lakh": round(tpv, 1), "preventability_assumed": x["p"],
            "expected_preventable_apps": round(x["prev_n"], 1), "expected_preventable_tpv_lakh": round(x["prev_tpv"], 1),
            "ship_first_score": round(x["score"], 1), "intervention": t["intervention"],
            "operational_benefit": t["operational_benefit"],
            "nature": "Journey problem (silent drop-off)" if t["codes"] == ["R13"] else "Data-quality / document problem",
        })
    out = pd.DataFrame(rows).sort_values(["ship_first_score", "applications"], ascending=False).reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))
    return out


def break_even_preventability(df: pd.DataFrame, interventions: Dict[str, Any], leader_id: str, challenger_id: str) -> float:
    """Smallest preventability for the challenger theme at which it would overtake the leader (None if never)."""
    fixable = df[df["recovery_decision"] == "Recover"]
    for step in range(0, 101):
        p = step / 100
        stats = {x["t"]["id"]: x["score"] for x in _theme_stats(fixable, interventions, {challenger_id: p})}
        if stats[challenger_id] > stats[leader_id]:
            return p
    return None


def tpv_breakdown(df: pd.DataFrame, by: str) -> pd.DataFrame:
    g = (df.groupby(by).agg(applications=("application_id", "count"),
                            expected_tpv_lakh=("expected_monthly_tpv_inr_lakh", "sum"),
                            recoverable=("recovery_decision", lambda s: int((s == "Recover").sum())))
         .reset_index().sort_values("expected_tpv_lakh", ascending=False))
    g["expected_tpv_lakh"] = g["expected_tpv_lakh"].round(1)
    return g.reset_index(drop=True)
