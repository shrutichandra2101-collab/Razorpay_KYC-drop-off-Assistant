"""Consent-aware channel selection (rules only).

WhatsApp is selected ONLY when whatsapp_opted_in is exactly "Yes". Otherwise the permitted
alternative is Email (address not in the dataset -> labelled placeholder) with an in-app
notification as secondary. No contact details are ever invented.
"""
from __future__ import annotations

from typing import Any, Dict

from .ingest import Application


def select_channel(app: Application, msettings: Dict[str, Any]) -> Dict[str, Any]:
    opted_in = (app.whatsapp_opted_in or "").strip() == "Yes"
    if opted_in:
        return {
            "channel": "WhatsApp",
            "secondary": msettings.get("secondary_alternative_channel", "In-app notification"),
            "recipient": "[MERCHANT_WHATSAPP_NUMBER - on file, not in dataset]",
            "reason": "whatsapp_opted_in = Yes (explicit consent recorded).",
        }
    return {
        "channel": msettings.get("default_alternative_channel", "Email"),
        "secondary": msettings.get("secondary_alternative_channel", "In-app notification"),
        "recipient": msettings.get("email_placeholder", "[MERCHANT_EMAIL - not in dataset]"),
        "reason": f"whatsapp_opted_in = '{app.whatsapp_opted_in or 'missing'}' -> WhatsApp not permitted; using Email (+ in-app).",
    }
