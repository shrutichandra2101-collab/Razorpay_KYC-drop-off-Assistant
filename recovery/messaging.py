# -*- coding: utf-8 -*-
"""Features 5, 6, 9 - multilingual explanation + personalised, context-aware nudges.

Default path: curated templates (recovery/i18n.py) assembled from structured context - deterministic,
catalogue-faithful, available offline. Optional path: the LLM rewrites the grounded draft in the
merchant's language; its output must pass every guardrail or it is discarded.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from .decision import CLOSE, RECOVER, ROUTE, Decision
from .diagnosis import Diagnosis
from .guardrails import check_message, numbers_in
from .i18n import CODE_TEXT, DROPOUT_QUESTION, FRAME, STEP_NAMES, dropout_block
from .ingest import Application


def step_label(language: str, step: str) -> str:
    return STEP_NAMES.get(language, STEP_NAMES["English"]).get(step, step)


def explanation(app: Application, code: str, language: str) -> Dict[str, str]:
    """Merchant-friendly explanation of what went wrong and what to correct (Feature 5)."""
    lang = language if language in CODE_TEXT else "English"
    issue, fix = CODE_TEXT[lang][code]
    step = step_label(lang, app.step_reached)
    return {"language": lang, "issue": issue.format(step=step), "fix": fix.format(step=step)}


def context_signals(app: Application, fsettings: Dict[str, Any]) -> Dict[str, bool]:
    return {
        "repeated_attempts": (app.attempts or 0) >= int(fsettings.get("attempts_high", 3)),
        "many_support_contacts": (app.support_contacts or 0) >= int(fsettings.get("support_high", 2)),
        "long_inactive": (app.days_since_last_activity or 0) >= int(fsettings.get("inactive_high_days", 21)),
    }


def compose_nudge(app: Application, diag: Diagnosis, dec: Decision, channel: Dict[str, Any],
                  settings: Dict[str, Any]) -> Dict[str, Any]:
    lang = app.preferred_language if app.preferred_language in FRAME else "English"
    f = FRAME[lang]
    resume = settings["messaging"].get("resume_link_placeholder", "[RESUME_LINK]")
    ch = channel["channel"]
    out: Dict[str, Any] = {"language": lang, "channel": ch, "subject": "", "body": "", "parts": {},
                           "context_applied": [], "generation_source": "Curated template", "status": "Draft",
                           "violations": []}
    if diag.code is None or dec.decision is None:
        out["status"] = "Withheld - diagnosis awaiting human verification"
        return out

    step = step_label(lang, app.step_reached)
    exp = explanation(app, diag.code, lang)
    fmt = dict(app_id=app.application_id, industry=app.industry, step=step, resume_link=resume)
    bold = (lambda s: f"*{s}*") if ch == "WhatsApp" else (lambda s: s)
    cta = f["cta_whatsapp"] if ch == "WhatsApp" else f["cta_email"]
    sig = context_signals(app, settings["friction"])
    lines: List[str] = [f["greet"].format(**fmt)]
    parts: Dict[str, str] = {"opening": lines[0]}

    if dec.decision == RECOVER:
        if diag.code != "R13":
            lines.append(f["paused"].format(**fmt))
        if sig["many_support_contacts"]:
            lines.append(f["support"])
            out["context_applied"].append(f"{app.support_contacts} support contacts -> acknowledges earlier contact")
        lines.append(f"{bold(f['what_happened'])} {exp['issue']}")
        lines.append(f"{bold(f['what_to_do'])} {exp['fix']}")
        if sig["repeated_attempts"]:
            lines.append(f["guided"])
            out["context_applied"].append(f"{app.attempts} attempts -> offers guided assistance")
        next_line = (f["next_action_r13"] if diag.code == "R13" else f["next_action"]).format(**fmt)
        lines.append(next_line)
        if diag.code == "R13":
            # One-tap reason question so ops learns WHY silent drop-offs happen (optional for the merchant).
            lines.append(dropout_block(lang))
            out["quick_replies"] = DROPOUT_QUESTION[lang]["opts"]
            out["context_applied"].append("abandoned (R13) -> optional one-tap 'why did you stop?' question"
                                          + (" (WhatsApp list message)" if ch == "WhatsApp" else " (reply with a number)"))
        if sig["long_inactive"]:
            lines.append(f["inactive"].format(**fmt))
            out["context_applied"].append(f"{app.days_since_last_activity} days inactive -> low-pressure re-entry line")
        lines.append(cta)
        parts.update(issue=exp["issue"], correction=exp["fix"], next_action=next_line, cta=cta)
        out["subject"] = f["subject_action"].format(**fmt)
    elif dec.decision == CLOSE:
        lines.append(exp["issue"])
        lines.append(exp["fix"])
        lines.append(f["closure_help"])
        parts.update(issue=exp["issue"], correction="(none - not fixable; no workaround offered)",
                     next_action=exp["fix"], cta=f["closure_help"])
        out["subject"] = f["subject_update"].format(**fmt)
    elif dec.decision == ROUTE:
        lines.append(exp["issue"])
        lines.append(exp["fix"])
        parts.update(issue=exp["issue"], correction="(none requested from merchant)", next_action=exp["fix"], cta="")
        out["subject"] = f["subject_update"].format(**fmt)

    lines.append(f["signoff"])
    out["context_applied"].insert(0, f"stage '{app.step_reached}' + industry '{app.industry}' named; language {lang}; channel {ch}")
    out["body"] = "\n\n".join(lines)
    out["parts"] = parts
    out["explanation"] = exp
    if not dec.actionable:
        out["status"] = "Provisional - not approvable until the diagnosis is verified"
    out["violations"] = check_message(out["body"], lang, diag.code, dec.decision, ch, app.whatsapp_opted_in, resume)
    return out


def followup_message(kind: str, app: Application, code: str, channel: str, settings: Dict[str, Any]) -> str:
    lang = app.preferred_language if app.preferred_language in FRAME else "English"
    f = FRAME[lang]
    step = step_label(lang, app.step_reached)
    fix = explanation(app, code, lang)["fix"]
    resume = settings["messaging"].get("resume_link_placeholder", "[RESUME_LINK]")
    text = f[kind].format(app_id=app.application_id, step=step, fix=fix, resume_link=resume)
    return text + "\n\n" + f["signoff"]


def message_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ optional LLM rewrite
def llm_nudge_cache_key(app: Application, code: str, channel: str, template_body: str) -> str:
    return "nudge::" + json.dumps([app.application_id, code, channel, message_hash(template_body)], ensure_ascii=False)


def build_llm_nudge_prompt(app: Application, diag: Diagnosis, dec: Decision, draft: Dict[str, Any],
                           template: str) -> Dict[str, str]:
    system, _, user_tpl = template.partition("---USER---")
    ctx = {
        "application_id": app.application_id, "business_type": app.business_type, "industry": app.industry,
        "city": app.city, "onboarding_stage": app.step_reached, "status": app.application_status,
        "diagnosed_code": diag.code, "decision": dec.decision, "preferred_language": draft["language"],
        "channel": draft["channel"], "days_since_last_activity": app.days_since_last_activity,
        "attempts": app.attempts, "support_contacts": app.support_contacts,
    }
    user = (user_tpl.replace("{{CONTEXT}}", json.dumps(ctx, ensure_ascii=False, indent=2))
            .replace("{{ISSUE}}", draft.get("explanation", {}).get("issue", ""))
            .replace("{{FIX}}", draft.get("explanation", {}).get("fix", ""))
            .replace("{{DRAFT}}", draft["body"])
            .replace("{{LANGUAGE}}", draft["language"])
            .replace("{{CHANNEL}}", draft["channel"]))
    return {"system": system.strip(), "user": user.strip()}


def validate_llm_nudge(raw: Dict[str, Any], app: Application, diag: Diagnosis, dec: Decision,
                       draft: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    body = str(raw.get("body", "")).strip()
    subject = str(raw.get("subject", "")).strip()
    resume = settings["messaging"].get("resume_link_placeholder", "[RESUME_LINK]")
    allowed = numbers_in(draft["body"]) | numbers_in(draft.get("subject", ""))
    violations = check_message(body, draft["language"], diag.code, dec.decision, draft["channel"],
                               app.whatsapp_opted_in, resume, allowed_numbers=allowed)
    if diag.code == "R13" and dec.decision == "Recover":
        missing = [o for o in DROPOUT_QUESTION[draft["language"]]["opts"] if o not in body]
        if missing:
            violations.append("R13 message must keep the dropout-reason options word for word")
    if subject:
        violations += [f"subject: {x}" for x in check_message(
            subject, draft["language"], diag.code, None, draft["channel"], app.whatsapp_opted_in, resume,
            allowed_numbers=allowed) if "resume-link" not in x and "script" not in x]
    return {"body": body, "subject": subject, "violations": violations, "ok": not violations}
