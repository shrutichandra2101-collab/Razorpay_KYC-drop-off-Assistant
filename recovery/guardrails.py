# -*- coding: utf-8 -*-
"""Section 7 guardrails - validators applied to EVERY merchant-facing message (template or LLM).

A message with any violation is never shown as approvable; LLM output that fails is discarded and
the curated template is used instead.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Set

PROMISE_PATTERNS = [
    r"guarantee", r"\bwill (definitely |surely )?be (approved|activated)\b", r"approval is (assured|certain|confirmed)",
    r"\bactivated (within|by)\b", r"within \d+ ?(hours?|days?|working days)", r"\bpakka\b", r"गारंटी",
    r"\b100 ?%", r"\bzaroor (approve|activate)", r"instant(ly)? approv", r"\bapproved (today|tomorrow)\b",
]
CONTACT_PATTERNS = [
    r"https?://", r"\bwww\.", r"[\w.+-]+@[\w-]+\.[a-z]{2,}", r"(?<!\d)\d{10}(?!\d)", r"\+91", r"\b1800[- ]?\d",
]
# Full month names (except the ambiguous English word "may"), or any abbreviation next to a day number.
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
MONTHS = (r"\b(january|february|march|april|june|july|august|september|october|november|december)\b"
          rf"|\b\d{{1,2}}(st|nd|rd|th)?\s+{_MON}\b|\b{_MON}\s+\d{{1,2}}\b")
R12_FORBIDDEN = [
    r"sanction", r"screening", r"watch ?list", r"negative list", r"\baml\b", r"\bpep\b", r"black ?list", r"fraud",
    r"compliance", r"money laundering", r"\bhit\b", r"प्रतिबंध", r"स्क्रीनिंग", r"सैंक्शन", r"ஸ்கிரீனிங்", r"தடைப்பட்டியல்",
    r"ಸ್ಕ್ರೀನಿಂಗ್", r"ನಿರ್ಬಂಧ", r"निर्बंध", r"अनुपालन", r"कंप्लायंस",
]
R10_WORKAROUND = [
    r"re-?apply", r"different (business )?category", r"change (your|the) (business )?category", r"another category",
    r"alternative", r"work ?around", r"\binstead\b", r"other account", r"new application", r"dobara apply",
    r"category badal", r"दोबारा आवेदन", r"श्रेणी बदल", r"மீண்டும் விண்ணப்", r"ಮತ್ತೆ ಅರ್ಜಿ", r"पुन्हा अर्ज", r"\bresubmit\b",
]
SCRIPT_RANGES = {
    "Hindi": r"[ऀ-ॿ]", "Marathi": r"[ऀ-ॿ]",
    "Tamil": r"[஀-௿]", "Kannada": r"[ಀ-೿]",
}
INDIC_ANY = r"[ऀ-෿]"


def numbers_in(text: str) -> Set[str]:
    return set(re.findall(r"\d+", text or ""))


def check_message(text: str, language: str, code: Optional[str], decision: Optional[str], channel: str,
                  whatsapp_opted_in: str, resume_placeholder: str = "[RESUME_LINK]",
                  allowed_numbers: Optional[Iterable[str]] = None) -> List[str]:
    v: List[str] = []
    t = text or ""
    low = t.lower()
    if not t.strip():
        return ["Empty message"]
    if channel == "WhatsApp" and (whatsapp_opted_in or "").strip() != "Yes":
        v.append("WhatsApp selected without explicit opt-in")
    for p in PROMISE_PATTERNS:
        if re.search(p, low, re.I):
            v.append(f"Promise/guarantee language matched '{p}'")
    if re.search(MONTHS, low, re.I):
        v.append("Mentions a calendar month - possible invented date")
    scrub = t.replace(resume_placeholder, "")
    for p in CONTACT_PATTERNS:
        if re.search(p, scrub, re.I):
            v.append(f"Invented contact detail / URL matched '{p}'")
    if decision == "Recover" and resume_placeholder not in t:
        v.append("Recover message is missing the labelled resume-link placeholder")
    if decision in ("Close", "Route to Compliance") and resume_placeholder in t:
        v.append(f"{decision} message must not contain a resume link")
    if code == "R12" or decision == "Route to Compliance":
        for p in R12_FORBIDDEN:
            if re.search(p, low, re.I):
                v.append(f"R12 message reveals screening/compliance detail ('{p}')")
    if code == "R10" or decision == "Close":
        for p in R10_WORKAROUND:
            if re.search(p, low, re.I):
                v.append(f"Closure message suggests a workaround ('{p}')")
    rng = SCRIPT_RANGES.get(language)
    if rng:
        letters = re.findall(r"\w", t)
        native = re.findall(rng, t)
        if not letters or len(native) / max(1, len(letters)) < 0.3:
            v.append(f"Message does not appear to be written in {language} script")
    elif language in ("English", "Hinglish") and re.search(INDIC_ANY, t):
        v.append(f"{language} message contains non-Latin Indic script")
    if allowed_numbers is not None:
        extra = numbers_in(t) - set(allowed_numbers)
        if extra:
            v.append(f"Contains numbers not present in the grounded facts: {sorted(extra)}")
    return v
