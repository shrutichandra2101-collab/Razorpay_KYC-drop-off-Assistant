"""Feature 12 - configurable follow-up sequence with stopping rules (preview only - nothing is sent).

Timing is relative ("Day 0 = when ops approves the initial nudge"); the dataset contains no follow-up
schedule, so offsets are configurable assumptions. High-friction merchants get a compressed cadence and
are escalated to a human at the final step.

Stopping rules (checked before every step):
  activation, closure, compliance routing, human stop-communication, no consent for any channel,
  merchant resumed (pause automated reminders), max-message cap, and no duplicate step.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .messaging import followup_message

STOP_EVENTS = {
    "activated": "Application activated - stop all onboarding communication",
    "closed": "Application closed - stop communication",
    "routed_compliance": "Routed to Compliance - stop automated communication",
    "stop_communication": "Authorised human recorded stop-communication",
    "merchant_resumed": "Merchant resumed the application - pause automated reminders",
}
STEP_ORDER = ["initial", "follow_up_1", "follow_up_2", "final"]
STEP_TITLE = {"initial": "Initial nudge", "follow_up_1": "Follow-up 1", "follow_up_2": "Follow-up 2", "final": "Final action"}


def build_sequence(row: Dict[str, Any], app, settings: Dict[str, Any], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    fu = settings["followup"]
    seq: Dict[str, Any] = {"active": False, "stop_reason": "", "steps": [], "cadence": ""}
    ev_names = [e["event"] for e in events]

    # Hard stops derived from the decision (never generate follow-ups)
    if row["application_status"] == "Activated":
        seq["stop_reason"] = STOP_EVENTS["activated"]
        return seq
    if row["recovery_decision"] == "Close":
        seq["stop_reason"] = "Decision is Close - no follow-ups are generated"
        return seq
    if row["recovery_decision"] == "Route to Compliance":
        seq["stop_reason"] = "Routed to Compliance - no follow-ups are generated"
        return seq
    if not row["recovery_eligible"]:
        seq["stop_reason"] = "Not eligible - diagnosis awaiting human verification" if row["human_review_required"] \
            else "Not eligible for recovery"
        return seq
    for e in ("activated", "closed", "routed_compliance", "stop_communication"):
        if e in ev_names:
            seq["stop_reason"] = STOP_EVENTS[e]
            return seq

    # R13 merchants may answer the one-tap "why did you stop?" question; their answer steers the plan.
    reasons = [e.get("note") for e in events if e["event"] == "dropout_reason"]
    reason = reasons[-1] if reasons else None
    if reason == "other_provider":
        seq["stop_reason"] = "Merchant said they chose another provider - reminders stopped (respecting their choice)"
        return seq

    high = row["friction_level"] == "High" or reason == "confusing"
    offsets = fu["high_friction_offsets_days"] if high else fu["standard_offsets_days"]
    seq["cadence"] = "Compressed (high friction)" if high else "Standard"
    channel = row["channel"]
    sent = [e for e in ev_names if e.startswith("sent_")]
    paused = "merchant_resumed" in ev_names
    max_msgs = int(fu.get("max_automated_messages", 4))
    escalate = high or (row["attempts"] or 0) >= int(settings["friction"]["attempts_high"]) \
        or (row["support_contacts"] or 0) >= int(settings["friction"]["support_high"])

    for i, step in enumerate(STEP_ORDER):
        day = int(offsets[step])
        if step == "initial":
            text, purpose = row["draft_nudge"], "Explain the issue and the correction required"
        elif step == "follow_up_1":
            text, purpose = followup_message("fu1", app, row["code"], channel, settings), "Remind if still incomplete"
        elif step == "follow_up_2":
            text, purpose = followup_message("fu2", app, row["code"], channel, settings), "Offer guided assistance"
        else:
            if escalate:
                text = "(Internal task - no automated message) Assign an onboarding specialist to contact the merchant " \
                       "through the consented channel; stop automated reminders."
                purpose = "Human intervention (high friction / repeated difficulty)"
            else:
                text = followup_message("final", app, row["code"], channel, settings)
                purpose = "Last reminder, then stop automated communication"
        status = "Planned"
        if step == "initial" and reason:
            status = "Sent - merchant replied"
        elif reason == "not_now" and step in ("follow_up_1", "follow_up_2"):
            status = "Skipped - merchant said not right now"
        elif f"sent_{step}" in sent:
            status = "Marked sent (simulated)"
        elif paused:
            status = "Paused - merchant resumed"
        elif i >= max_msgs:
            status = "Suppressed - message cap reached"
        seq["steps"].append({"step": STEP_TITLE[step], "key": step, "day": day, "channel": channel if not (step == "final" and escalate) else "Internal",
                             "purpose": purpose, "message": text, "status": status,
                             "condition": "Only if the application is still incomplete and no stop rule applies" if step != "initial" else "Requires ops approval"})
    seq["active"] = not paused
    if paused:
        seq["stop_reason"] = STOP_EVENTS["merchant_resumed"]
    return seq
