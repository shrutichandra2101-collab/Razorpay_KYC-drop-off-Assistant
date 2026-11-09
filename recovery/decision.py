"""Feature 4 - fixability and recovery decision engine (pure rules).

Fixability is read from the catalogue's `fixable` column. Hard guardrails (R10 -> Close,
R12 -> Route to Compliance) are asserted on top. Nothing here reads LLM output or TPV, so neither
can override fixability or compliance routing.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .diagnosis import Diagnosis
from .ingest import CatalogEntry

RECOVER, CLOSE, ROUTE = "Recover", "Close", "Route to Compliance"


@dataclass
class Decision:
    decision: Optional[str]          # Recover / Close / Route to Compliance / None
    fixability: str                  # Yes / No / Compliance review / Unknown
    actionable: bool                 # False while the diagnosis awaits human verification
    decision_state: str              # human-readable state
    owner: str                       # who acts next
    recovery_eligible: bool          # may enter the recovery queue + follow-up sequence
    standard_fix: str                # catalogue standard fix (internal wording)
    rule_trace: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def decide(diag: Diagnosis, catalog: Dict[str, CatalogEntry], rules: Dict[str, Any],
           stop_communication: bool = False) -> Decision:
    trace: List[str] = []
    if diag.code is None or diag.code not in catalog:
        trace.append("No catalogue code -> no automated decision.")
        return Decision(None, "Unknown", False, "Blocked - diagnosis needs human verification",
                        "Onboarding reviewer", False, "", trace)

    entry = catalog[diag.code]
    fixable = entry.fixable
    decision = rules["fixable_to_decision"].get(fixable)
    trace.append(f"Catalogue {diag.code}.fixable = '{fixable}' -> {decision}.")
    hard = rules.get("hard_guardrails", {}).get(diag.code)
    if hard:
        if decision != hard:
            trace.append(f"Hard guardrail overrides catalogue mapping for {diag.code}: {hard}.")
        else:
            trace.append(f"Hard guardrail confirms {diag.code} -> {hard}.")
        decision = hard
    if decision is None:
        trace.append(f"Unrecognised fixable value '{fixable}' -> human review.")
        return Decision(None, "Unknown", False, "Blocked - unrecognised catalogue fixability",
                        "Onboarding reviewer", False, entry.standard_fix, trace)

    fix_label = rules.get("fixability_label", {}).get(fixable, fixable)
    if diag.code == "R10":
        fix_label = "No"
    if diag.code == "R12":
        fix_label = "Compliance review"

    actionable = not diag.human_review_required
    owner = {RECOVER: "Onboarding ops", CLOSE: "Onboarding ops (closure)", ROUTE: "Compliance team (authorised humans)"}[decision]
    if not actionable:
        state = "Blocked - verify diagnosis before any action"
        owner = "Onboarding reviewer"
        trace.append("Diagnosis flagged for human verification -> decision is provisional, not actionable.")
    elif decision == RECOVER:
        state = "Ready - draft nudge awaiting ops approval"
    elif decision == CLOSE:
        state = "Close - polite closure note awaiting ops approval; no workaround"
    else:
        state = "Routed to Compliance - neutral 'under review' note only"

    eligible = decision == RECOVER and actionable and not stop_communication
    if stop_communication:
        trace.append("Authorised human recorded stop-communication -> excluded from queue and follow-ups.")
        state += " (communication stopped by human)"
    return Decision(decision, fix_label, actionable, state, owner, eligible, entry.standard_fix, trace)
