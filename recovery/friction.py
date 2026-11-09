"""Feature 10 - explainable merchant friction detection.

Uses only the touchpoint signals present in the workbook: attempts, support_contacts,
days_since_last_activity, step_reached, application_status. Points per signal are summed into a
Low / Medium / High level. All thresholds are configurable ASSUMPTIONS (config/settings.json), not
Razorpay policy. Note: the dataset records only the current stage, so "repeated activity at the same
stage" is inferred as attempts >= 2 while still stalled at that stage.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

from .ingest import Application

STAGE_HELP = {
    "Business details": "step-specific guidance on the business details form",
    "KYC docs": "guided KYC document assistance",
    "Bank verification": "a bank-verification walkthrough",
    "Website/App review": "a website readiness checklist walkthrough",
}


@dataclass
class Friction:
    points: int
    level: str
    indicators: List[str] = field(default_factory=list)
    pain_point: str = ""
    intervention: str = ""
    proactive_kind: Optional[str] = None  # "fu2" (assist) / "fu1" (reminder) / None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def assess(app: Application, code: Optional[str], decision: Optional[str], fs: Dict[str, Any]) -> Friction:
    a, s, d = app.attempts or 0, app.support_contacts or 0, app.days_since_last_activity or 0
    pts, ind = 0, []
    if a >= int(fs["attempts_high"]):
        pts += 2 + (1 if a >= int(fs["attempts_high"]) + 1 else 0)
        ind.append(f"High attempts ({a}) - repeated difficulty completing '{app.step_reached}'")
    elif a >= int(fs["attempts_medium"]):
        pts += 1
        ind.append(f"Repeated activity at the same stage ({a} attempts at '{app.step_reached}')")
    if s >= int(fs["support_high"]):
        pts += 2 + (1 if s >= int(fs["support_high"]) + 1 else 0)
        ind.append(f"Multiple support contacts ({s}) - merchant needs clarification")
    elif s >= int(fs["support_medium"]):
        pts += 1
        ind.append(f"Contacted support ({s})")
    if d >= int(fs["inactive_severe_days"]):
        pts += 3
        ind.append(f"Prolonged inactivity ({d} days) - likely disengaged")
    elif d >= int(fs["inactive_high_days"]):
        pts += 2
        ind.append(f"Prolonged inactivity ({d} days) - possible disengagement")
    elif d >= int(fs["inactive_medium_days"]):
        pts += 1
        ind.append(f"Inactive for {d} days")

    level = "High" if pts >= int(fs["level_high_min_points"]) else "Medium" if pts >= int(fs["level_medium_min_points"]) else "Low"

    struggling = a >= int(fs["attempts_high"]) or s >= int(fs["support_high"])
    disengaged = d >= int(fs["inactive_high_days"])
    stage_help = STAGE_HELP.get(app.step_reached, "step-specific guidance")
    if struggling and disengaged:
        pain = f"Struggled at '{app.step_reached}' and then went quiet"
    elif struggling:
        pain = f"Repeated difficulty at '{app.step_reached}'"
    elif disengaged:
        pain = f"Disengaged after stopping at '{app.step_reached}'"
    elif ind:
        pain = f"Early signs of friction at '{app.step_reached}'"
    else:
        pain = "No notable friction signals"

    if decision in ("Close", "Route to Compliance"):
        return Friction(pts, level, ind, pain, "None - not eligible for recovery outreach (closed / compliance).", None)
    if decision is None:
        return Friction(pts, level, ind, pain, "Hold - verify diagnosis before any proactive outreach.", None)

    acts = []
    if a >= int(fs["attempts_high"]):
        acts.append(f"Offer {stage_help}")
    if s >= int(fs["support_high"]):
        acts.append("Assign a dedicated support interaction (one owner, one call/chat)")
    if disengaged:
        acts.append("Send a step-specific reminder with the resume link")
    if not acts and a >= int(fs["attempts_medium"]):
        acts.append(f"Send {stage_help} in the nudge")
    if not acts:
        acts.append("Standard nudge with exact fix and resume link")
    kind = "fu2" if struggling else "fu1" if disengaged else None
    return Friction(pts, level, ind, pain, "; ".join(acts), kind)
