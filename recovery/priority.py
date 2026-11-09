"""Feature 11 - explainable recovery prioritisation (weighted scoring).

Only applications whose decision is Recover AND whose diagnosis is verified/high-confidence are
scored. R10 / R12 / Close / Route / unverified cases never enter the queue, regardless of TPV.

Score = 100 * sum(weight_i * component_i), components normalised to 0..1:
  tpv         log-scaled expected monthly TPV (log scale so small merchants are not crushed)
  inactivity  min(days, cap) / cap  - urgency of re-engaging before permanent drop-off
  friction    Low 0 / Medium 0.5 / High 1
  attempts    (attempts - 1) / 3
  support     support_contacts / 3
  confidence  diagnostic confidence
  quick_fix   1 - typical_days_to_fix / 3  (catalogue)
A High-friction merchant is floored at Medium priority so struggling small merchants are not ignored.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

LABELS = {
    "tpv": "expected TPV", "inactivity": "days inactive", "friction": "friction level",
    "attempts": "repeated attempts", "support": "support contacts", "confidence": "diagnostic confidence",
    "quick_fix": "quick catalogue fix",
}
FRICTION_VAL = {"Low": 0.0, "Medium": 0.5, "High": 1.0}


def score_queue(rows: List[Dict[str, Any]], ps: Dict[str, Any]) -> None:
    """Mutates eligible rows in place, adding priority_* fields."""
    w = ps["weights"]
    cap = float(ps.get("inactivity_cap_days", 30))
    eligible = [r for r in rows if r["recovery_eligible"]]
    max_tpv = max([r["expected_monthly_tpv_inr_lakh"] or 0 for r in eligible] + [1.0])
    for r in rows:
        r.update(priority_score=None, priority=None, priority_reason="", priority_components={})
    for r in eligible:
        tpv = r["expected_monthly_tpv_inr_lakh"] or 0.0
        comps = {
            "tpv": math.log1p(tpv) / math.log1p(max_tpv),
            "inactivity": min(float(r["days_since_last_activity"] or 0), cap) / cap,
            "friction": FRICTION_VAL.get(r["friction_level"], 0.0),
            "attempts": min(max((r["attempts"] or 1) - 1, 0) / 3.0, 1.0),
            "support": min((r["support_contacts"] or 0) / 3.0, 1.0),
            "confidence": float(r["confidence"] or 0),
            "quick_fix": 1.0 - min(float(r.get("typical_days_to_fix") or 0), 3.0) / 3.0,
        }
        contrib = {k: 100.0 * float(w.get(k, 0)) * v for k, v in comps.items()}
        score = round(sum(contrib.values()), 1)
        level = "High" if score >= ps["high_min_score"] else "Medium" if score >= ps["medium_min_score"] else "Low"
        floor_note = ""
        if r["friction_level"] == "High" and level == "Low" and ps.get("high_friction_floor") == "Medium":
            level, floor_note = "Medium", " Raised to Medium: high friction (struggling merchant) regardless of TPV."
        top = sorted(contrib.items(), key=lambda kv: kv[1], reverse=True)[:3]
        why = ", ".join(f"{LABELS[k]} (+{v:.0f})" for k, v in top)
        detail = (f"TPV ₹{tpv:.1f}L/mo, {r['days_since_last_activity']}d inactive, {r['attempts']} attempt(s), "
                  f"{r['support_contacts']} support contact(s), friction {r['friction_level']}, "
                  f"confidence {float(r['confidence']):.2f}.")
        r.update(priority_score=score, priority=level,
                 priority_reason=f"Score {score}: driven by {why}. {detail}{floor_note}",
                 priority_components={k: round(v, 1) for k, v in contrib.items()})
    ranked = sorted(eligible, key=lambda r: (-r["priority_score"], r["application_id"]))
    for i, r in enumerate(ranked, 1):
        r["priority_rank"] = i
