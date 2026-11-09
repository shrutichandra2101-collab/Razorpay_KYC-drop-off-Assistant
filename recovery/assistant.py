# -*- coding: utf-8 -*-
"""Ask-a-question assistant for ops users.

Design (answer first, then a way to get there):
  * Read-only DATA TOOLS compute every number from the same pipeline output the dashboard shows.
  * Claude (optional) picks the tools, then writes a short answer via the `answer` tool.
    Every number in its answer must appear in a tool result or the question - otherwise the answer is
    rejected and the deterministic summary from the tools is shown instead.
  * Navigation buttons open the Cases list with the exact filters used, a single case, or an Insights topic.
  * An offline rule-based parser handles common questions when no API key is set.
  * The assistant can only READ. It never approves, sends, closes or re-diagnoses anything.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from . import config
from .i18n import DROPOUT_REASON_LABELS
from .insights import break_even_preventability, product_recommendations

SHORT_REASON = {
    "R01": "PAN name doesn't match business name", "R02": "GST details don't match",
    "R03": "Bank account name doesn't match", "R04": "Bank verification failed",
    "R05": "Document unreadable", "R06": "Website missing policy pages", "R07": "Website not live",
    "R08": "Address proof missing or old", "R09": "Signatory ID missing", "R10": "Restricted business type",
    "R11": "Category differs from website", "R12": "Under compliance review", "R13": "Stopped midway (no rejection)",
}

# ------------------------------------------------------------------ filters
FILTER_FIELDS = {
    # key: (dataframe column, kind)
    "status": ("application_status", "cat"),
    "stage": ("onboarding_stage", "cat"),
    "code": ("code", "cat"),
    "decision": ("recovery_decision", "cat"),
    "language": ("preferred_language", "cat"),
    "channel": ("channel", "cat"),
    "priority": ("priority", "cat"),
    "friction_level": ("friction_level", "cat"),
    "business_type": ("business_type", "cat"),
    "industry": ("industry", "cat"),
    "city": ("city", "cat"),
    "human_review": ("human_review_required", "bool"),
    "in_recovery_queue": ("recovery_eligible", "bool"),
    "min_tpv_lakh": ("expected_monthly_tpv_inr_lakh", "min"),
    "max_tpv_lakh": ("expected_monthly_tpv_inr_lakh", "max"),
    "min_days_inactive": ("days_since_last_activity", "min"),
    "max_days_inactive": ("days_since_last_activity", "max"),
    "min_attempts": ("attempts", "min"),
    "min_support_contacts": ("support_contacts", "min"),
}
FILTER_LABEL = {
    "status": "status", "stage": "stage", "code": "reason", "decision": "decision", "language": "language",
    "channel": "channel", "priority": "priority", "friction_level": "friction", "business_type": "business type",
    "industry": "industry", "city": "city", "human_review": "needs human review", "in_recovery_queue": "in recovery queue",
    "min_tpv_lakh": "expected TPV ≥ ₹{}L", "max_tpv_lakh": "expected TPV ≤ ₹{}L", "min_days_inactive": "inactive ≥ {} days",
    "max_days_inactive": "inactive ≤ {} days", "min_attempts": "attempts ≥ {}", "min_support_contacts": "support contacts ≥ {}",
}


def clean_filters(df: pd.DataFrame, filters: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """Validate filter keys/values against the data. Returns (clean_filters, problems)."""
    out, problems = {}, []
    for k, v in (filters or {}).items():
        if v in (None, "", []):
            continue
        if k not in FILTER_FIELDS:
            problems.append(f"unknown filter '{k}'")
            continue
        col, kind = FILTER_FIELDS[k]
        if kind == "cat":
            vals = v if isinstance(v, list) else [v]
            allowed = {str(x).lower(): x for x in df[col].dropna().unique()}
            good = [allowed[str(x).lower()] for x in vals if str(x).lower() in allowed]
            bad = [x for x in vals if str(x).lower() not in allowed]
            if bad:
                problems.append(f"{k}: no data for {bad}")
            if good:
                out[k] = good
        elif kind == "bool":
            out[k] = bool(v) if not isinstance(v, str) else v.lower() in ("true", "yes", "1")
        else:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                problems.append(f"{k}: '{v}' is not a number")
    return out, problems


def apply_filters(df: pd.DataFrame, filters: Dict[str, Any]) -> pd.DataFrame:
    out = df
    for k, v in filters.items():
        col, kind = FILTER_FIELDS[k]
        if kind == "cat":
            out = out[out[col].isin(v)]
        elif kind == "bool":
            out = out[out[col] == v]
        elif kind == "min":
            out = out[out[col] >= v]
        else:
            out = out[out[col] <= v]
    return out


def describe_filters(filters: Dict[str, Any]) -> str:
    if not filters:
        return "all stuck (non-activated) applications"
    parts = []
    for k, v in filters.items():
        lab = FILTER_LABEL[k]
        if "{}" in lab:
            parts.append(lab.format(_n(v)))
        elif isinstance(v, bool):
            parts.append(lab if v else "not " + lab)
        else:
            vals = [f"{x} ({SHORT_REASON[x]})" if k == "code" and x in SHORT_REASON else str(x) for x in v]
            parts.append(f"{lab} = {' or '.join(vals)}")
    return "; ".join(parts)


def _n(x) -> str:
    x = float(x)
    return str(int(x)) if x.is_integer() else f"{x:.1f}"


def plural(n: int, word: str = "application") -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _tpv(s) -> float:
    return round(float(pd.to_numeric(s, errors="coerce").fillna(0).sum()), 1)


# ------------------------------------------------------------------ data tools
class DataTools:
    def __init__(self, res, settings: Dict[str, Any]):
        self.res, self.df, self.settings = res, res.df, settings

    # each tool returns a dict with at least: summary (deterministic sentence) + nav (navigation action) + data
    def overview(self) -> Dict[str, Any]:
        s = self.res.summary
        return {"summary": (f"{s['non_activated']} of {s['total']} applications are stuck ({s['stalled']} stalled, "
                            f"{s['rejected']} rejected); {s['activated']} are activated. {s['recoverable']} can be recovered, "
                            f"{s['closure']} must be closed and {s['compliance']} are with Compliance. Expected monthly TPV in "
                            f"recoverable cases: ₹{s['tpv_recoverable_lakh']}L (declared, not actual revenue). "
                            f"{s['human_review']} need a human decision."),
                "data": {k: s[k] for k in ("total", "activated", "non_activated", "stalled", "rejected", "recoverable",
                                           "recover_ready", "closure", "compliance", "human_review", "tpv_recoverable_lakh",
                                           "tpv_non_activated_lakh")},
                "nav": {"type": "open_cases", "filters": {}, "label": "Open all stuck applications"}}

    def count_cases(self, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        f, problems = clean_filters(self.df, filters)
        sub = apply_filters(self.df, f)
        n, total = len(sub), len(self.df)
        pct = round(100 * n / max(total, 1), 1)
        by_reason = sub["code"].value_counts().head(5)
        reasons = [{"code": c, "reason": SHORT_REASON.get(c, c), "applications": int(v)} for c, v in by_reason.items()]
        dec = {k: int(v) for k, v in sub["recovery_decision"].fillna("No decision").value_counts().items()}
        prio = {k: int(v) for k, v in sub["priority"].dropna().value_counts().items()}
        top = (f" Most common reason: {reasons[0]['reason']} ({reasons[0]['applications']})." if reasons else "")
        summary = (f"{plural(n)} {'matches' if n == 1 else 'match'} ({describe_filters(f)}) - {pct}% of the {total} stuck applications, "
                   f"with ₹{_tpv(sub['expected_monthly_tpv_inr_lakh'])}L expected monthly TPV.{top}")
        if problems:
            summary += f" (Ignored: {'; '.join(problems)}.)"
        return {"summary": summary,
                "data": {"applications": n, "pct_of_stuck": pct, "stuck_total": total,
                         "expected_tpv_lakh": _tpv(sub["expected_monthly_tpv_inr_lakh"]),
                         "by_reason": reasons, "by_decision": dec, "by_priority": prio,
                         "filters_used": describe_filters(f), "ignored": problems},
                "nav": {"type": "open_cases", "filters": f, "label": "Open this case" if n == 1 else f"Open these {n} cases"} if n else None}

    def list_cases(self, filters: Optional[Dict[str, Any]] = None, sort_by: str = "priority", limit: int = 5) -> Dict[str, Any]:
        f, problems = clean_filters(self.df, filters)
        sub = apply_filters(self.df, f)
        key = {"priority": ("priority_score", False), "tpv": ("expected_monthly_tpv_inr_lakh", False),
               "days_inactive": ("days_since_last_activity", False), "friction": ("friction_points", False)}.get(sort_by, ("priority_score", False))
        sub = sub.sort_values(key[0], ascending=key[1], na_position="last")
        limit = max(1, min(int(limit or 5), 10))
        rows = [{"application_id": r.application_id, "reason": SHORT_REASON.get(r.code, "needs review") if r.code else "needs review",
                 "priority": r.priority or "-", "expected_tpv_lakh": round(float(r.expected_monthly_tpv_inr_lakh or 0), 1),
                 "days_inactive": int(r.days_since_last_activity or 0), "language": r.preferred_language,
                 "decision": r.recovery_decision or "No decision"} for r in sub.head(limit).itertuples()]
        names = ", ".join(f"{x['application_id']} ({x['reason']}, ₹{x['expected_tpv_lakh']}L)" for x in rows)
        summary = (f"{plural(len(sub))} {'matches' if len(sub) == 1 else 'match'} ({describe_filters(f)}). Top {len(rows)} by {sort_by.replace('_', ' ')}: {names}."
                   if rows else f"No applications match ({describe_filters(f)}).")
        return {"summary": summary, "data": {"total_matching": len(sub), "sorted_by": sort_by, "cases": rows,
                                             "filters_used": describe_filters(f), "ignored": problems},
                "nav": {"type": "open_cases", "filters": f, "label": f"Open all {len(sub)}"} if len(sub) else None}

    def get_case(self, application_id: str) -> Dict[str, Any]:
        aid = (application_id or "").strip().upper()
        if aid not in set(self.df["application_id"]):
            act = set(self.res.activated_df.get("application_id", pd.Series(dtype=str)))
            msg = f"{aid} is already activated - nothing to recover." if aid in act else f"No application {aid} in the data."
            return {"summary": msg, "data": {"found": False, "activated": aid in act}, "nav": None}
        r = self.df.set_index("application_id").loc[aid]
        app = self.res.apps[aid]
        nxt = ("confirm the diagnosis" if r["human_review_required"] else
               {"Recover": f"send the {r['preferred_language']} {r['channel']} message",
                "Close": "send a polite closure note (no workaround)",
                "Route to Compliance": "hand to Compliance (merchant only hears it's under review)"}.get(r["recovery_decision"], "investigate"))
        reason = SHORT_REASON.get(r["code"], "unclear") if r["code"] else "unclear"
        summary = (f"{aid} ({app.business_type}, {app.industry}, {app.city}) stopped at {app.step_reached} "
                   f"{app.days_since_last_activity} days ago. Reason: {reason} [{r['code_source']}, confidence "
                   f"{float(r['confidence']):.2f}]. Decision: {r['recovery_decision'] or 'none yet'}. "
                   f"Priority: {r['priority'] or 'not queued'}; friction {r['friction_level']}. Next: {nxt}.")
        return {"summary": summary,
                "data": {"application_id": aid, "business_type": app.business_type, "industry": app.industry, "city": app.city,
                         "stage": app.step_reached, "status": app.application_status, "days_inactive": app.days_since_last_activity,
                         "attempts": app.attempts, "support_contacts": app.support_contacts,
                         "expected_tpv_lakh": app.expected_monthly_tpv_inr_lakh, "language": app.preferred_language,
                         "whatsapp_opted_in": app.whatsapp_opted_in, "rejection_code_on_file": app.rejection_code,
                         "reviewer_note": app.rejection_message, "diagnosed_code": r["code"], "reason": reason,
                         "diagnosis_source": r["code_source"], "confidence": round(float(r["confidence"]), 2),
                         "decision": r["recovery_decision"], "needs_human_review": bool(r["human_review_required"]),
                         "priority": r["priority"], "priority_score": r["priority_score"], "friction": r["friction_level"],
                         "friction_signals": r["friction_indicators"], "channel": r["channel"], "next_step": nxt,
                         "follow_up": r["followup_status"],
                         "dropout_reason": DROPOUT_REASON_LABELS.get(r.get("dropout_reason"), None) if isinstance(r.get("dropout_reason"), str) else None},
                "nav": {"type": "open_case", "application_id": aid, "label": f"Open {aid}"}}

    def top_causes(self, filters: Optional[Dict[str, Any]] = None, k: int = 5) -> Dict[str, Any]:
        f, problems = clean_filters(self.df, filters)
        sub = apply_filters(self.df, f)
        g = sub.groupby("code").agg(applications=("application_id", "count"), tpv=("expected_monthly_tpv_inr_lakh", "sum"))
        g = g.sort_values(["applications", "tpv"], ascending=False).head(max(1, min(int(k or 5), 13)))
        items = [{"code": c, "reason": SHORT_REASON.get(c, c), "applications": int(r.applications),
                  "pct": round(100 * r.applications / max(len(sub), 1), 1), "expected_tpv_lakh": round(float(r.tpv), 1)}
                 for c, r in g.iterrows()]
        summary = (f"Top causes among {len(sub)} applications ({describe_filters(f)}): " +
                   "; ".join(f"{x['reason']} - {x['applications']} ({x['pct']}%)" for x in items) + ".")
        return {"summary": summary, "data": {"applications": len(sub), "causes": items, "filters_used": describe_filters(f),
                                             "note": "R13 'stopped midway' has no rejection - a journey problem, not a document problem."},
                "nav": {"type": "open_insight", "topic": "why", "label": "See causes in Insights"}}

    def tpv_breakdown(self, by: str = "stage", filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        f, problems = clean_filters(self.df, filters)
        sub = apply_filters(self.df, f)
        col = {"stage": "onboarding_stage", "reason": "code", "industry": "industry", "business_type": "business_type",
               "language": "preferred_language", "city": "city", "decision": "recovery_decision", "priority": "priority"}.get(by, "onboarding_stage")
        g = sub.groupby(col).agg(applications=("application_id", "count"), tpv=("expected_monthly_tpv_inr_lakh", "sum"))
        g = g.sort_values("tpv", ascending=False)
        items = [{"group": SHORT_REASON.get(i, i) if col == "code" else i, "applications": int(r.applications),
                  "expected_tpv_lakh": round(float(r.tpv), 1)} for i, r in g.iterrows()]
        total = _tpv(sub["expected_monthly_tpv_inr_lakh"])
        summary = (f"Expected monthly TPV for {describe_filters(f)}: ₹{total}L (declared, not actual revenue). By {by}: " +
                   "; ".join(f"{x['group']} ₹{x['expected_tpv_lakh']}L ({x['applications']})" for x in items[:6]) + ".")
        return {"summary": summary, "data": {"total_expected_tpv_lakh": total, "by": by, "groups": items,
                                             "filters_used": describe_filters(f)},
                "nav": {"type": "open_insight", "topic": "tpv", "label": "Explore TPV in Insights"}}

    def friction_summary(self, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        f, problems = clean_filters(self.df, filters)
        sub = apply_filters(self.df, f)
        fs = self.settings["friction"]
        lv = {k: int(v) for k, v in sub["friction_level"].value_counts().items()}
        hi = sub[sub["friction_level"] == "High"]
        st = {k: int(v) for k, v in hi["onboarding_stage"].value_counts().items()}
        data = {"by_level": lv, "high_by_stage": st,
                "many_attempts": int((sub["attempts"] >= fs["attempts_high"]).sum()), "attempts_threshold": fs["attempts_high"],
                "many_support_contacts": int((sub["support_contacts"] >= fs["support_high"]).sum()), "support_threshold": fs["support_high"],
                "long_inactive": int((sub["days_since_last_activity"] >= fs["inactive_high_days"]).sum()), "inactive_threshold_days": fs["inactive_high_days"],
                "filters_used": describe_filters(f)}
        top_stage = next(iter(st), "-")
        summary = (f"{lv.get('High', 0)} high-friction, {lv.get('Medium', 0)} medium, {lv.get('Low', 0)} low ({describe_filters(f)}). "
                   f"Most high-friction cases are at {top_stage}. {data['many_attempts']} tried {fs['attempts_high']}+ times, "
                   f"{data['many_support_contacts']} contacted support {fs['support_high']}+ times, {data['long_inactive']} silent "
                   f"{fs['inactive_high_days']}+ days. Thresholds are prototype assumptions.")
        return {"summary": summary, "data": data,
                "nav": {"type": "open_cases", "filters": {**f, "friction_level": ["High"]}, "label": f"Open the {lv.get('High', 0)} struggling cases"}}

    def product_recommendation(self) -> Dict[str, Any]:
        interventions = config.load_rules("product_interventions.json")
        recs = product_recommendations(self.df, interventions)
        sf, ru = recs.iloc[0], recs.iloc[1]
        be = break_even_preventability(self.df, interventions, sf["theme_id"], ru["theme_id"])
        summary = (f"Ship first: {sf['theme']} ({sf['codes']}) - {sf['applications']} applications, "
                   f"₹{sf['expected_tpv_lakh']}L expected TPV. Runner-up: {ru['theme']} ({ru['applications']} applications); "
                   f"it would only lead if its assumed preventability were ≥ {be} (assumed {ru['preventability_assumed']}).")
        return {"summary": summary,
                "data": {"ship_first": {k: (v.item() if hasattr(v, "item") else v) for k, v in sf[["theme", "codes", "applications", "pct_of_non_activated", "expected_tpv_lakh", "intervention"]].items()},
                         "runners_up": [{"theme": r["theme"], "applications": int(r["applications"]), "expected_tpv_lakh": float(r["expected_tpv_lakh"])}
                                        for _, r in recs.iloc[1:3].iterrows()], "break_even_preventability": be},
                "nav": {"type": "open_insight", "topic": "fix", "label": "See the recommendation"}}

    def activated_count(self) -> Dict[str, Any]:
        n = len(self.res.activated_df)
        return {"summary": f"{n} of {self.res.summary['total']} applications are activated and need no recovery.",
                "data": {"activated": n, "total": self.res.summary["total"]}, "nav": None}

    def call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        fn: Callable = {"overview": self.overview, "count_cases": self.count_cases, "list_cases": self.list_cases,
                        "get_case": self.get_case, "top_causes": self.top_causes, "tpv_breakdown": self.tpv_breakdown,
                        "friction_summary": self.friction_summary, "product_recommendation": self.product_recommendation,
                        "activated_count": self.activated_count}.get(name)
        if fn is None:
            return {"summary": f"Unknown tool {name}", "data": {}, "nav": None}
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return {"summary": f"Bad arguments for {name}: {e}", "data": {}, "nav": None}


# ------------------------------------------------------------------ tool schemas for Claude
_FILTER_SCHEMA = {
    "type": "object",
    "description": "Optional filters. Omit to cover all stuck (non-activated) applications.",
    "properties": {
        "status": {"type": "array", "items": {"type": "string", "enum": ["Stalled", "Rejected"]}},
        "stage": {"type": "array", "items": {"type": "string", "enum": ["Business details", "KYC docs", "Bank verification", "Website/App review"]}},
        "code": {"type": "array", "items": {"type": "string", "enum": [f"R{i:02d}" for i in range(1, 14)]},
                 "description": "R01 PAN name mismatch, R02 GST, R03 bank account name, R04 bank verification failed, R05 unreadable doc, "
                                "R06 website policy pages, R07 website not live, R08 address proof, R09 signatory ID, R10 restricted business, "
                                "R11 category vs website, R12 compliance review, R13 stopped midway / abandoned (no rejection)"},
        "decision": {"type": "array", "items": {"type": "string", "enum": ["Recover", "Close", "Route to Compliance"]}},
        "language": {"type": "array", "items": {"type": "string", "enum": ["English", "Hindi", "Hinglish", "Tamil", "Kannada", "Marathi"]}},
        "channel": {"type": "array", "items": {"type": "string", "enum": ["WhatsApp", "Email"]}},
        "priority": {"type": "array", "items": {"type": "string", "enum": ["High", "Medium", "Low"]}},
        "friction_level": {"type": "array", "items": {"type": "string", "enum": ["High", "Medium", "Low"]}},
        "business_type": {"type": "array", "items": {"type": "string"}},
        "industry": {"type": "array", "items": {"type": "string"}},
        "city": {"type": "array", "items": {"type": "string"}},
        "human_review": {"type": "boolean"},
        "in_recovery_queue": {"type": "boolean"},
        "min_tpv_lakh": {"type": "number"}, "max_tpv_lakh": {"type": "number"},
        "min_days_inactive": {"type": "number"}, "max_days_inactive": {"type": "number"},
        "min_attempts": {"type": "number"}, "min_support_contacts": {"type": "number"},
    },
    "additionalProperties": False,
}
TOOLS = [
    {"name": "overview", "description": "Headline numbers for the whole dataset.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "count_cases", "description": "Count stuck applications matching filters; also returns % of stuck, expected TPV, top reasons, decisions and priorities.",
     "input_schema": {"type": "object", "properties": {"filters": _FILTER_SCHEMA}}},
    {"name": "list_cases", "description": "List the top matching applications (max 10).",
     "input_schema": {"type": "object", "properties": {"filters": _FILTER_SCHEMA,
                      "sort_by": {"type": "string", "enum": ["priority", "tpv", "days_inactive", "friction"]},
                      "limit": {"type": "integer", "minimum": 1, "maximum": 10}}}},
    {"name": "get_case", "description": "Everything about one application ID, e.g. A3022.",
     "input_schema": {"type": "object", "properties": {"application_id": {"type": "string"}}, "required": ["application_id"]}},
    {"name": "top_causes", "description": "Most frequent diagnosed reasons, optionally within filters.",
     "input_schema": {"type": "object", "properties": {"filters": _FILTER_SCHEMA, "k": {"type": "integer", "minimum": 1, "maximum": 13}}}},
    {"name": "tpv_breakdown", "description": "Expected monthly TPV (INR lakh, declared - not actual revenue) grouped by a dimension.",
     "input_schema": {"type": "object", "properties": {"by": {"type": "string", "enum": ["stage", "reason", "industry", "business_type", "language", "city", "decision", "priority"]},
                                                        "filters": _FILTER_SCHEMA}}},
    {"name": "friction_summary", "description": "Which merchants are struggling (attempts, support contacts, inactivity).",
     "input_schema": {"type": "object", "properties": {"filters": _FILTER_SCHEMA}}},
    {"name": "product_recommendation", "description": "Which product fix to ship first and why.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "activated_count", "description": "How many applications are already activated.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "answer", "description": "ALWAYS finish by calling this exactly once with the final reply to the user.",
     "input_schema": {"type": "object", "properties": {
         "answer": {"type": "string", "description": "Short markdown answer, max ~80 words. Lead with the number. Only use numbers that appear in tool results."},
         "follow_ups": {"type": "array", "items": {"type": "string"}, "maxItems": 3, "description": "Up to 3 short follow-up questions the user might ask next."},
         "actions": {"type": "array", "maxItems": 3, "description": "Up to 3 navigation buttons.", "items": {"type": "object", "properties": {
             "type": {"type": "string", "enum": ["open_cases", "open_case", "open_insight"]},
             "label": {"type": "string"}, "filters": _FILTER_SCHEMA, "application_id": {"type": "string"},
             "topic": {"type": "string", "enum": ["why", "fix", "tpv", "struggle"]}}, "required": ["type", "label"]}}},
         "required": ["answer"]}},
]


# ------------------------------------------------------------------ result type
@dataclass
class Reply:
    question: str
    answer: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    follow_ups: List[str] = field(default_factory=list)
    trace: List[str] = field(default_factory=list)       # "how I got this"
    source: str = "rules"                                  # "claude" | "rules" | "rules (Claude answer failed checks)"
    filters: Dict[str, Any] = field(default_factory=dict)  # last filters used (for "these" follow-ups)
    seconds: float = 0.0
    issues: List[str] = field(default_factory=list)


def _numbers(text: str) -> set:
    out = set()
    for m in re.findall(r"\d+(?:[.,]\d+)*", text or ""):
        m = m.replace(",", "")
        out.add(m)
        try:
            f = float(m)
            out.add(_n(f))
            if f.is_integer():
                out.add(str(int(f)))
        except ValueError:
            pass
    return out


def validate_actions(actions: List[Dict[str, Any]], tools: DataTools) -> List[Dict[str, Any]]:
    good = []
    for a in (actions or [])[:3]:
        t = a.get("type")
        if t == "open_cases":
            f, problems = clean_filters(tools.df, a.get("filters") or {})
            n = len(apply_filters(tools.df, f))
            if n == 1 and not problems:
                aid = apply_filters(tools.df, f).iloc[0]["application_id"]
                good.append({"type": "open_case", "application_id": aid, "label": f"Open {aid}"})
            elif n and not problems:  # never silently widen a button to a different set of cases
                good.append({"type": t, "filters": f, "label": f"Open these {n} cases" if f else f"Open all {n} stuck applications"})
        elif t == "open_case":
            aid = str(a.get("application_id", "")).upper()
            if aid in set(tools.df["application_id"]):
                good.append({"type": t, "application_id": aid, "label": f"Open {aid}"})
        elif t == "open_insight" and a.get("topic") in ("why", "fix", "tpv", "struggle"):
            good.append({"type": t, "topic": a["topic"], "label": str(a.get("label") or "Open Insights")[:40]})
    seen, unique = set(), []
    for g in good:
        k = json.dumps({x: g.get(x) for x in ("type", "application_id", "filters", "topic")}, sort_keys=True, default=str)
        if k not in seen:
            seen.add(k)
            unique.append(g)
    return unique


# ------------------------------------------------------------------ Claude path
def ask_claude(question: str, history: List[Reply], tools: DataTools, llm, max_turns: int = 6) -> Reply:
    t0 = time.time()
    system = config.load_prompt("assistant.md")
    msgs: List[Dict[str, Any]] = []
    for h in history[-3:]:
        msgs.append({"role": "user", "content": h.question})
        ctx = f" (filters used: {describe_filters(h.filters)})" if h.filters else ""
        msgs.append({"role": "assistant", "content": h.answer + ctx})
    msgs.append({"role": "user", "content": question})
    evidence_text, trace, navs, last_filters = question, [], [], {}
    client = llm._get_client()
    final = None
    for _ in range(max_turns):
        resp = client.messages.create(model=llm.model, max_tokens=4000, system=system, messages=msgs, tools=TOOLS,
                                      tool_choice={"type": "auto"}, output_config={"effort": llm.effort})
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined")
        msgs.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if not uses:
            text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
            final = {"answer": text}
            break
        results = []
        for u in uses:
            if u.name == "answer":
                final = dict(u.input)
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": "ok"})
                continue
            args = dict(u.input or {})
            out = tools.call(u.name, args)
            evidence_text += " " + json.dumps(out, ensure_ascii=False, default=str)
            trace.append(f"{u.name}({json.dumps(args, ensure_ascii=False)}) → {out['summary']}")
            if out.get("nav"):
                navs.append(out["nav"])
            if isinstance(args.get("filters"), dict):
                last_filters = clean_filters(tools.df, args["filters"])[0]
            results.append({"type": "tool_result", "tool_use_id": u.id,
                            "content": json.dumps({"summary": out["summary"], "data": out["data"]}, ensure_ascii=False, default=str)})
        if final is not None:
            break
        msgs.append({"role": "user", "content": results})
    if final is None or not final.get("answer"):
        raise RuntimeError("no answer produced")
    # ---- verification: every number must come from tool results or the question
    unsupported = sorted(_numbers(final["answer"]) - _numbers(evidence_text))
    actions = validate_actions(final.get("actions") or [], tools) or [n for n in navs if n][:2]
    follow = [str(x)[:90] for x in (final.get("follow_ups") or [])][:3]
    rep = Reply(question, final["answer"].strip(), actions, follow, trace, "claude", last_filters, round(time.time() - t0, 1))
    if unsupported:
        rep.issues.append(f"numbers not found in the data: {unsupported}")
    return rep


# ------------------------------------------------------------------ offline rule-based path
_STAGES = [("business details", "Business details"), ("kyc", "KYC docs"), ("bank verification", "Bank verification"),
           ("bank step", "Bank verification"), ("bank stage", "Bank verification"), ("website", "Website/App review"),
           ("app review", "Website/App review")]
_CODES = [(r"\bpan\b", "R01"), (r"\bgst", "R02"), (r"(bank|account) (holder )?name", "R03"), (r"bank verification fail|penny|ifsc", "R04"),
          (r"unreadable|blurr|not readable", "R05"), (r"policy page|missing polic", "R06"), (r"website (is )?(not live|down)", "R07"),
          (r"address", "R08"), (r"signatory", "R09"), (r"restricted", "R10"), (r"category", "R11"),
          (r"abandon|stopped|dropped|drop[- ]?off|midway|silent", "R13")]
_LANGS = ["english", "hindi", "hinglish", "tamil", "kannada", "marathi"]


def parse_filters(q: str, df: pd.DataFrame) -> Dict[str, Any]:
    ql = q.lower()
    f: Dict[str, Any] = {}
    stages = sorted({v for k, v in _STAGES if k in ql})
    codes = sorted({c for p, c in _CODES if re.search(p, ql)})
    if "R03" in codes and "R01" in codes and "bank" in ql:
        codes.remove("R01")
    if stages and not codes:
        f["stage"] = stages
    elif stages and codes:
        f["stage"], f["code"] = stages, codes
    elif codes:
        f["code"] = codes
    if re.search(r"\bcompliance\b", ql):
        f["decision"] = ["Route to Compliance"]
    elif re.search(r"\bclos(e|ed|ure|ing)\b", ql):
        f["decision"] = ["Close"]
    elif re.search(r"recoverable|can be recovered|recover", ql):
        f["decision"] = ["Recover"]
    langs = [l.capitalize() for l in _LANGS if re.search(rf"\b{l}\b", ql)]
    if langs:
        f["language"] = langs
    m = re.search(r"\b(high|medium|low)[- ]priority", ql)
    if m:
        f["priority"] = [m.group(1).capitalize()]
    if re.search(r"struggl|high friction|high-friction", ql):
        f["friction_level"] = ["High"]
    if re.search(r"human review|need(s)? (a )?(human|review|decision)|uncertain|verify", ql):
        f["human_review"] = True
    if re.search(r"\brejected\b", ql):
        f["status"] = ["Rejected"]
    elif re.search(r"\bstalled\b", ql):
        f["status"] = ["Stalled"]
    if "whatsapp" in ql:
        f["channel"] = ["WhatsApp"]
    elif re.search(r"\bemail\b", ql):
        f["channel"] = ["Email"]
    for col, key in (("business_type", "business_type"), ("industry", "industry"), ("city", "city")):
        hits = [v for v in df[col].dropna().unique() if v.lower() in ql or v.split(" ")[0].lower() + " " in ql + " " and len(v.split(" ")[0]) > 4]
        if hits:
            f[key] = sorted(set(hits))
    m = re.search(r"(more than|over|above|at least|>=|>)\s*(\d+)\s*days", ql)
    if m:
        strict = m.group(1) in ("more than", "over", "above", ">")
        f["min_days_inactive"] = float(m.group(2)) + (1 if strict else 0)
    m = re.search(r"(?:tpv|lakh)[^\d]{0,20}(?:more than|over|above|at least|>=?)\s*₹?\s*(\d+(?:\.\d+)?)|(?:more than|over|above|at least)\s*₹?\s*(\d+(?:\.\d+)?)\s*(?:l\b|lakh)", ql)
    if m:
        f["min_tpv_lakh"] = float(m.group(1) or m.group(2))
    m = re.search(r"(\d+)\+? (?:or more )?attempts|attempts\s*(?:>=?|more than|over|at least)\s*(\d+)", ql)
    if m:
        f["min_attempts"] = float(m.group(1) or m.group(2))
    return f


def _default_follow_ups(intent: str, f: Dict[str, Any]) -> List[str]:
    base = {"count": ["Which of these are high priority?", "What's the expected TPV for these?", "Show the top 5"],
            "list": ["How many are struggling?", "Why are merchants getting stuck?", "What should product fix first?"],
            "case": ["Show similar cases", "How many need a human decision?", "What should product fix first?"],
            "causes": ["What should product fix first?", "How many stopped midway?", "Where is the most TPV at stake?"],
            "tpv": ["Break it down by reason", "Which high-TPV cases are in the queue?", "Why are merchants getting stuck?"],
            "friction": ["Show the top 5 struggling merchants", "Which stage has most struggling merchants?", "How many need a human decision?"],
            "product": ["Why are merchants getting stuck?", "How many have a PAN name mismatch?", "How many stopped midway?"],
            "overview": ["How many are stuck at KYC?", "Why are merchants getting stuck?", "How many need a human decision?"]}
    return base.get(intent, base["overview"])


def ask_rules(question: str, history: List[Reply], tools: DataTools) -> Reply:
    t0 = time.time()
    q = question.strip()
    ql = q.lower()
    m = re.search(r"\b[a][-\s]?(\d{4})\b", ql)
    if re.search(r"^(please |can you |could you )?(approve|send|close|reject|delete|change|mark|stop|email|message|contact)\b", ql):
        acts = [{"type": "open_case", "application_id": "A" + m.group(1), "label": f"Open A{m.group(1)}"}] if m and ("A" + m.group(1)) in set(tools.df["application_id"]) else []
        return Reply(q, "I can only read the data - I can't approve, send, close or change anything. "
                        + ("You can do that from the case screen." if acts else "Open a case to take action."),
                     acts, _default_follow_ups("overview", {}), ["read-only guardrail"], "rules", {}, round(time.time() - t0, 2))
    domain = (r"merchant|application|applicant|\bcases?\b|stuck|kyc|tpv|onboard|priorit|struggl|friction|review|compliance|"
              r"activat|reject|stall|recover|clos|abandon|stopped|midway|drop|reason|cause|fix|\bpan\b|gst|bank|website|"
              r"document|signatory|address|language|whatsapp|email|inactive|attempt|support|queue|work on|how many|count|number of")
    if not m and not re.search(domain, ql):
        return Reply(q, "I can only answer questions about the stuck onboarding applications in this workbook - "
                        "counts, reasons, TPV, priorities, struggling merchants or a specific application ID.",
                     [], _default_follow_ups("overview", {}), ["no matching data question"], "rules", {}, round(time.time() - t0, 2))
    if m:
        out = tools.get_case("A" + m.group(1))
        return Reply(q, out["summary"], [out["nav"]] if out.get("nav") else [], _default_follow_ups("case", {}),
                     [f"get_case → {out['summary']}"], "rules", {}, round(time.time() - t0, 2))
    if re.search(r"\bactivated\b", ql) and not re.search(r"not activated|non[- ]activated", ql):
        out = tools.activated_count()
        return Reply(q, out["summary"], [], _default_follow_ups("overview", {}), [f"activated_count → {out['summary']}"], "rules")
    f = parse_filters(q, tools.df)
    if history and re.search(r"\b(these|those|them|of them|this list)\b", ql):
        f = {**history[-1].filters, **f}
    by = next((v for k, v in [("stage", "stage"), ("reason", "reason"), ("cause", "reason"), ("industry", "industry"),
                              ("business type", "business_type"), ("language", "language"), ("city", "city"),
                              ("decision", "decision"), ("priority", "priority")] if re.search(rf"\bby {k}", ql)), None)
    if re.search(r"(highest|biggest|largest|most|top)\b.{0,25}(tpv|value)", ql):
        intent, out = "list", tools.list_cases(f, "tpv", 3)
    elif re.search(r"work on (first|next)|start with|most urgent|first case|next case", ql):
        intent, out = "list", tools.list_cases({**f, "in_recovery_queue": True}, "priority", 3)
    elif re.search(r"fix first|ship|product (team|fix|change|improvement)|what should product", ql):
        intent, out = "product", tools.product_recommendation()
    elif re.search(r"\btpv\b|revenue|money|worth|at stake|value|lakh|business at stake", ql) and not re.search(r"how many|count|number of", ql):
        intent, out = "tpv", tools.tpv_breakdown(by or "stage", f)
    elif re.search(r"\bwhy\b|cause|reasons?\b|top (problems|issues)", ql) and not re.search(r"how many|count|number of", ql):
        intent, out = "causes", tools.top_causes(f)
    elif re.search(r"struggl|friction", ql) and not re.search(r"how many|count|number of|list|show|which|top \d", ql):
        intent, out = "friction", tools.friction_summary({k: v for k, v in f.items() if k != "friction_level"})
    elif re.search(r"\b(list|show|which|top \d+|who are|give me)\b", ql):
        n = re.search(r"top (\d+)", ql)
        sort = "tpv" if re.search(r"tpv|biggest|largest|value", ql) else "days_inactive" if "inactive" in ql else "priority"
        intent, out = "list", tools.list_cases(f, sort, int(n.group(1)) if n else 5)
    elif f or re.search(r"how many|count|number of", ql):
        intent, out = "count", tools.count_cases(f)
    elif re.search(r"merchant|application|case|stuck|onboard|overview|summary|status|dashboard|queue|total", ql):
        intent, out = "overview", tools.overview()
    else:
        return Reply(q, "I can only answer questions about the stuck onboarding applications in this workbook - "
                        "counts, reasons, TPV, priorities, struggling merchants or a specific application ID.",
                     [], _default_follow_ups("overview", {}), ["no matching data question"], "rules", {}, round(time.time() - t0, 2))
    actions = [out["nav"]] if out.get("nav") else []
    return Reply(q, out["summary"], actions, _default_follow_ups(intent, f), [f"{intent} → {out['summary']}"], "rules",
                 f if intent in ("count", "list", "causes", "tpv", "friction") else {}, round(time.time() - t0, 2))


# ------------------------------------------------------------------ entry point
def ask(question: str, history: List[Reply], res, settings: Dict[str, Any], llm=None, store=None, actor: str = "ops") -> Reply:
    tools = DataTools(res, settings)
    rep = None
    if llm is not None and llm.available:
        try:
            rep = ask_claude(question, history, tools, llm)
            if rep.issues:
                fallback = ask_rules(question, history, tools)
                fallback.source = "rules (Claude's answer failed the data check)"
                fallback.issues = rep.issues
                fallback.trace = rep.trace + fallback.trace
                rep = fallback
        except Exception as e:  # network, refusal, malformed - never break the UI
            rep = ask_rules(question, history, tools)
            rep.source = "rules (Claude unavailable)"
            rep.issues.append(str(e)[:160])
    if rep is None:
        rep = ask_rules(question, history, tools)
    if store is not None:
        store.audit("assistant_question", None, actor, {"question": question, "answer": rep.answer, "source": rep.source,
                                                         "trace": rep.trace, "issues": rep.issues})
    return rep
