# -*- coding: utf-8 -*-
"""Merchant Onboarding & KYC Recovery Assistant - Streamlit UI.

Designed around the onboarding-ops user's journey:
  Home      -> "what needs me today?"  (pick a task)
  Cases     -> work through one task list, one case at a time (detail only on demand)
  Insights  -> pick a question, get one answer (charts/data only if asked)
  Quality & settings -> how it works, test results, thresholds, data, audit log

Run:  streamlit run app.py
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from recovery import config
from recovery.diagnosis import SOURCE_HUMAN, SOURCE_INFERRED
from recovery.export import excel_bytes, output_table
from recovery.i18n import (DROPOUT_REASON_ACTIONS, DROPOUT_REASON_KEYS, DROPOUT_REASON_LABELS, LANGUAGES)
from recovery.ingest import load_workbook
from recovery.insights import (abandonment_summary, break_even_preventability, by_code, product_recommendations,
                               top_systemic_causes, tpv_breakdown)
from recovery.assistant import Reply, apply_filters, ask, describe_filters
from recovery.llm import LLMClient
from recovery.messaging import explanation, followup_message, message_hash
from recovery.pipeline import run, run_llm_diagnosis, run_llm_nudge
from recovery.store import Store
from recovery.validation import run_validation, write_results

st.set_page_config(page_title="Onboarding Recovery Assistant", page_icon="🛟", layout="wide",
                   initial_sidebar_state="expanded")

BLUE = "#2a78d6"
MUTED = "#9aa5b8"
GREEN, RED, AMBER, GREY = "#1a7f4b", "#c0392b", "#b7791f", "#4b5563"
DECISION_COLOR = {"Recover": GREEN, "Close": RED, "Route to Compliance": AMBER, None: GREY}
LEVEL_COLOR = {"High": RED, "Medium": AMBER, "Low": GREY}

SHORT_REASON = {
    "R01": "PAN name doesn't match business name", "R02": "GST details don't match",
    "R03": "Bank account name doesn't match", "R04": "Bank verification failed",
    "R05": "Document unreadable", "R06": "Website missing policy pages", "R07": "Website not live",
    "R08": "Address proof missing or old", "R09": "Signatory ID missing", "R10": "Restricted business type",
    "R11": "Category differs from website", "R12": "Under compliance review", "R13": "Stopped midway (no rejection)",
}

# Task lists - the four jobs an ops user does. Order = order on the Home page.
VIEWS = {
    "review": {"label": "Check AI diagnoses", "icon": ":material/fact_check:",
               "help": "The AI isn't sure enough, or the outcome is serious (closure/compliance). Confirm or correct the reason before anything happens."},
    "ready": {"label": "Send recovery messages", "icon": ":material/send:",
              "help": "Diagnosis is clear and the merchant can fix it. Highest priority first - review the draft, copy it, approve it."},
    "struggling": {"label": "Help struggling merchants", "icon": ":material/support_agent:",
                   "help": "Many attempts, many support calls or long silence. These merchants need a guided, more personal touch."},
    "closing": {"label": "Close or hand off", "icon": ":material/block:",
                "help": "Restricted businesses (polite closure, no workaround) and compliance cases (neutral 'under review' only)."},
    "all": {"label": "All stuck applications", "icon": ":material/list:", "help": "Every non-activated application."},
}

# ----------------------------------------------------------------------------- styles
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, button, input, textarea { font-family: 'Inter', sans-serif; }
.block-container { padding-top: 1.8rem; padding-bottom: 3rem; max-width: 1180px; }
[data-testid="stSidebar"] { background: #0B1B3F; }
[data-testid="stSidebar"] * { color: #E6ECF8 !important; }
[data-testid="stSidebar"] input { color: #0B1B3F !important; }
[data-testid="stSidebarNav"] a[aria-current="page"] { background: rgba(47,107,255,.35); border-radius: 8px; }
.brand { display:flex; align-items:center; gap:10px; margin: 6px 0 4px 0; }
.brand-logo { width:32px; height:32px; border-radius:9px; background: linear-gradient(135deg,#2F6BFF,#5AA0FF);
  display:flex; align-items:center; justify-content:center; font-weight:700; color:white; font-size:16px; }
.brand-title { font-weight:700; font-size:15px; line-height:1.15; }
.brand-sub { font-size:11px; opacity:.7; }
.hello { font-size: 28px; font-weight: 700; color: #0B1B3F; margin-bottom: 2px; }
.sub { color:#52607a; font-size:15px; margin-bottom: 18px; }
.big { background:#fff; border:1px solid #E3E8F2; border-radius:14px; padding:16px 18px; }
.big-v { font-size:30px; font-weight:700; color:#0B1B3F; font-variant-numeric: tabular-nums; line-height:1.1; }
.big-l { font-size:14px; color:#52607a; margin-top:4px; }
.task-count { font-size:34px; font-weight:700; color:#0B1B3F; line-height:1; }
.task-title { font-size:16px; font-weight:600; color:#0B1B3F; margin-top:6px; }
.task-desc { font-size:13px; color:#52607a; margin-top:4px; min-height:38px; }
.pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; margin-right:4px;
  border:1px solid transparent; white-space:nowrap; }
.pill-fact { background:#E8F0FF; color:#1D4ED8; border-color:#C7D7FE; }
.pill-ai { background:#F3E8FF; color:#6B21A8; border-color:#E2CCFB; }
.pill-rule { background:#EEF1F5; color:#334155; border-color:#D6DCE5; }
.pill-rec { background:#E7F6EE; color:#166534; border-color:#BFE6CF; }
.pill-assume { background:#FFF7E0; color:#8A5A00; border-color:#F5DFA3; }
.pill-human { background:#FFE9E3; color:#9A3412; border-color:#F9C9B8; }
.summary { background:#fff; border:1px solid #E3E8F2; border-radius:14px; padding:16px 20px; margin: 8px 0 14px 0; font-size:15px; line-height:1.6; }
.summary b { color:#0B1B3F; }
.callout { border-left:4px solid #2F6BFF; background:#F0F5FF; padding:12px 14px; border-radius:8px; font-size:14px; margin: 6px 0 12px 0; }
.callout.warn { border-color:#b7791f; background:#FFF8E8; } .callout.bad { border-color:#c0392b; background:#FFF1EF; }
.callout.good { border-color:#1a7f4b; background:#EEF9F2; }
.kv { display:grid; grid-template-columns: 180px 1fr; row-gap:7px; font-size:14px; }
.kv .k { color:#6b7895; } .kv .v { color:#0B1B3F; font-weight:500; }
.step { border-left:3px solid #2F6BFF; padding:7px 12px; margin:8px 0; background:#fff; border-radius:0 8px 8px 0; font-size:14px; }
.step.internal { border-color:#b7791f; } .step.off { border-color:#9aa5b8; opacity:.7; }
.small { font-size:12px; color:#6b7895; }
.case-head { font-size:24px; font-weight:700; color:#0B1B3F; }
.case-sub { color:#52607a; font-size:14px; margin: 2px 0 6px 0; }
</style>
""", unsafe_allow_html=True)


# ----------------------------------------------------------------------------- data
@st.cache_data(show_spinner=False)
def _load_dataset(raw: bytes, name: str, apps_sheet: str, cat_sheet: str):
    return load_workbook(raw, apps_sheet, cat_sheet, source_name=name)


def get_dataset():
    settings = config.load_settings()
    if st.session_state.get("uploaded_bytes"):
        raw, name = st.session_state["uploaded_bytes"], st.session_state.get("uploaded_name", "uploaded.xlsx")
    else:
        path = config.resolve_workbook_path(settings)
        raw, name = path.read_bytes(), path.name
    return _load_dataset(raw, name, settings["workbook"]["applications_sheet"], settings["workbook"]["catalog_sheet"])


@st.cache_data(show_spinner=False)
def _run_pipeline(sha: str, settings_json: str, store_version: str, _ds):
    return run(_ds, json.loads(settings_json), Store())


def get_results():
    settings = config.load_settings()
    ds = get_dataset()
    store = Store()
    res = _run_pipeline(ds.report.sha256, json.dumps(settings, sort_keys=True), store.version(), ds)
    return ds, res, settings, store


def refresh():
    _run_pipeline.clear()
    st.rerun()


def actor() -> str:
    return st.session_state.get("actor") or "ops.reviewer"


def view_rows(df: pd.DataFrame, view: str) -> pd.DataFrame:
    if view == "review":
        out = df[df["human_review_required"]]
    elif view == "ready":
        out = df[df["recovery_eligible"]]
    elif view == "struggling":
        out = df[df["recovery_eligible"] & (df["friction_level"] == "High")]
    elif view == "closing":
        out = df[df["recovery_decision"].isin(["Close", "Route to Compliance"]) & ~df["human_review_required"]]
    else:
        out = df
    if view == "struggling":
        return out.sort_values(["friction_points", "priority_score"], ascending=False)
    return out.sort_values(["priority_rank", "application_id"], na_position="last")


def next_step(r) -> str:
    if r["human_review_required"]:
        return "Confirm the reason"
    if r["nudge_status"].startswith("Approved"):
        return "Approved - ready to send"
    if r["recovery_decision"] == "Recover":
        return f"Send {r['channel']} message"
    if r["recovery_decision"] == "Close":
        return "Send closure note"
    if r["recovery_decision"] == "Route to Compliance":
        return "Hand to Compliance"
    return "Investigate"


# ----------------------------------------------------------------------------- ui helpers
def pill(text, kind):
    return f'<span class="pill pill-{kind}">{text}</span>'


def color_pill(text, color):
    return f'<span class="pill" style="background:{color}1A;color:{color};border-color:{color}55">{text}</span>'


def source_pill(source: str) -> str:
    if source == SOURCE_INFERRED:
        return pill("AI-inferred", "ai")
    if source == SOURCE_HUMAN:
        return pill("Human-verified", "human")
    if source.startswith("Workbook"):
        return pill("From workbook", "fact")
    if source.startswith("Rule"):
        return pill("Rule-based", "rule")
    return pill("Undetermined", "human")


def big_number(col, value, label):
    col.markdown(f'<div class="big"><div class="big-v">{value}</div><div class="big-l">{label}</div></div>', unsafe_allow_html=True)


def style_fig(fig, height=320):
    fig.update_layout(template="plotly_white", height=height, margin=dict(l=10, r=40, t=10, b=10),
                      font=dict(family="Inter, sans-serif", size=13, color="#1f2a44"), showlegend=False,
                      bargap=0.35, hoverlabel=dict(bgcolor="white", font_size=12))
    fig.update_xaxes(gridcolor="#EEF1F6", zeroline=False, showticklabels=False)
    fig.update_yaxes(gridcolor="rgba(0,0,0,0)", zeroline=False)
    return fig


def hbar(labels, values, colors=None, fmt="{:,.0f}", height=None, hover_suffix=""):
    d = pd.DataFrame({"l": labels, "v": values, "c": colors or [BLUE] * len(labels)}).sort_values("v")
    fig = go.Figure(go.Bar(x=d["v"], y=d["l"], orientation="h", marker=dict(color=d["c"]),
                           text=[fmt.format(v) for v in d["v"]], textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}: %{x:,.1f}" + hover_suffix + "<extra></extra>"))
    return style_fig(fig, height or max(160, 34 * len(d) + 30))


def fmt_tpv(v):
    return f"₹{v:,.1f}L"


def open_case(aid: str, view: str):
    st.session_state["case"] = aid
    st.session_state["view"] = view
    st.switch_page(PAGES["cases"])


def go_view(view: str):
    st.session_state["view"] = view
    st.session_state.pop("case", None)
    st.switch_page(PAGES["cases"])


# ============================================================================= ASK THE ASSISTANT
EXAMPLES = ["How many are stuck at KYC?", "Why are merchants getting stuck?", "Who needs a human decision?"]


def run_action(a: dict):
    if a["type"] == "open_cases":
        st.session_state["preset"] = {"filters": a.get("filters") or {}}
        st.session_state["view"] = "all"
        st.session_state.pop("case", None)
        st.session_state.pop("case_list", None)
        st.session_state["list_nonce"] = st.session_state.get("list_nonce", 0) + 1
        st.switch_page(PAGES["cases"])
    elif a["type"] == "open_case":
        st.session_state["case"] = a["application_id"]
        st.session_state["view"] = "all"
        st.session_state.pop("case_list", None)
        st.switch_page(PAGES["cases"])
    elif a["type"] == "open_insight":
        st.session_state["topic"] = a.get("topic", "why")
        st.switch_page(PAGES["insights"])


def current_question(store):
    """The ONE current question + answer. Kept in the session and saved per operator, so it is still there after
    moving between pages or refreshing the browser. Asking a new question replaces it; nothing older is kept."""
    if "current_q" not in st.session_state:
        saved = store.current_question(actor())
        try:
            st.session_state["current_q"] = Reply(**saved) if saved else None
        except TypeError:
            st.session_state["current_q"] = None
    return st.session_state["current_q"]


def set_current_question(store, rep):
    st.session_state["current_q"] = rep
    if rep is None:
        store.clear_current_question(actor())
    else:
        store.save_current_question(actor(), asdict(rep))


def ask_box(res, settings, store, where: str):
    cur = current_question(store)
    with st.form(f"ask_{where}", clear_on_submit=True, border=False):
        c = st.columns([7, 1])
        q = c[0].text_input("Ask a question", placeholder="Ask anything about stuck applications - e.g. “How many are stuck at KYC?”",
                            label_visibility="collapsed")
        go = c[1].form_submit_button("Ask", type="primary", icon=":material/auto_awesome:", use_container_width=True)
    question = q.strip() if go and q.strip() else st.session_state.pop("pending_q", None)
    if question:
        with st.spinner("Looking it up in the data..."):
            cur = ask(question, [cur] if cur else [], res, settings, LLMClient(settings["llm"]), store, actor())
        set_current_question(store, cur)
        st.session_state["q_fresh"] = where
    if cur is None:
        if where == "home":
            ex_key = f"ex_{where}"

            def _pick():
                if st.session_state.get(ex_key):
                    st.session_state["pending_q"] = st.session_state[ex_key]
                    st.session_state[ex_key] = None

            st.pills("Try", EXAMPLES, key=ex_key, on_change=_pick, label_visibility="collapsed")
        return
    if where == "home":
        answer_card(cur, store, where)
    else:  # on other pages keep it folded so the page's own content stays in front
        with st.expander(f"Your question: {cur.question}", icon=":material/chat:",
                         expanded=st.session_state.get("q_fresh") == where):
            answer_card(cur, store, where)


def answer_card(cur, store, where: str):
    with st.container(border=True):
        top = st.columns([10, 1.2])
        top[0].markdown(f"<div class='small'>You asked: <b>{cur.question}</b></div>", unsafe_allow_html=True)
        if top[1].button("Clear", icon=":material/close:", key=f"clear_{where}", type="tertiary",
                         help="Remove this question"):
            set_current_question(store, None)
            st.rerun()
        st.markdown(cur.answer)
        if cur.actions:
            cols = st.columns(len(cur.actions) + 1)
            for i, a in enumerate(cur.actions):
                if cols[i].button(a["label"], key=f"act_{where}_{i}", icon=":material/arrow_forward:",
                                  type="primary" if i == 0 else "secondary", use_container_width=True):
                    run_action(a)
        if cur.follow_ups:
            st.markdown("<div class='small' style='margin-top:6px'>You could also ask:</div>", unsafe_allow_html=True)
            cols = st.columns(len(cur.follow_ups))
            for i, fq in enumerate(cur.follow_ups):
                if cols[i].button(fq, key=f"fu_{where}_{i}", use_container_width=True, type="tertiary"):
                    st.session_state["pending_q"] = fq
                    st.rerun()


# ============================================================================= HOME
def page_home():
    ds, res, settings, store = get_results()
    s, df = res.summary, res.df
    st.markdown('<div class="hello">What needs your attention today?</div>'
                f'<div class="sub">{s["non_activated"]} merchants are stuck in onboarding. Pick a task to start - '
                'everything else stays out of the way until you ask for it.</div>', unsafe_allow_html=True)
    ask_box(res, settings, store, "home")
    st.write("")

    counts = {v: len(view_rows(df, v)) for v in VIEWS}
    desc = {
        "review": "AI needs a human decision before anything happens.",
        "ready": f"{int((view_rows(df, 'ready')['priority'] == 'High').sum())} are high priority. Drafts are ready in each merchant's language.",
        "struggling": "Repeated attempts, many support calls or long silence.",
        "closing": (lambda c: f"{int((c['recovery_decision'] == 'Close').sum())} restricted closures · "
                              f"{int((c['recovery_decision'] == 'Route to Compliance').sum())} compliance hand-offs.")(view_rows(df, "closing")),
    }
    cols = st.columns(4)
    for col, v in zip(cols, ["review", "ready", "struggling", "closing"]):
        with col.container(border=True):
            st.markdown(f'<div class="task-count">{counts[v]}</div><div class="task-title">{VIEWS[v]["label"]}</div>'
                        f'<div class="task-desc">{desc[v]}</div>', unsafe_allow_html=True)
            if st.button("Start", key=f"home_{v}", icon=":material/arrow_forward:", use_container_width=True,
                         type="primary" if v == "review" and counts[v] else "secondary", disabled=counts[v] == 0):
                go_view(v)

    st.write("")
    st.markdown("##### At a glance")
    c = st.columns(3)
    big_number(c[0], s["non_activated"], f"stuck applications (of {s['total']})")
    big_number(c[1], s["recoverable"], "can be recovered by the merchant fixing something")
    big_number(c[2], fmt_tpv(s["tpv_recoverable_lakh"]), "expected monthly TPV in recoverable cases · declared, not actual revenue")

    st.write("")
    st.markdown("##### Want to know more?")
    q = st.columns(3)
    for col, (topic, label, sub) in zip(q, [
        ("why", "Why are merchants getting stuck?", "Top causes in plain words"),
        ("fix", "What should product fix first?", "One recommendation, with reasoning"),
        ("tpv", "How much business is at stake?", "Expected TPV, broken down your way"),
    ]):
        with col.container(border=True):
            st.markdown(f"**{label}**  \n<span class='small'>{sub}</span>", unsafe_allow_html=True)
            if st.button("Show me", key=f"q_{topic}", use_container_width=True):
                st.session_state["topic"] = topic
                st.switch_page(PAGES["insights"])


# ============================================================================= CASES
def page_cases():
    ds, res, settings, store = get_results()
    if st.session_state.get("case") in set(res.df["application_id"]):
        case_view(ds, res, settings, store)
    else:
        st.session_state.pop("case", None)
        case_list(ds, res, settings, store)


def case_list(ds, res, settings, store):
    df = res.df
    view = st.session_state.setdefault("view", "ready")
    st.markdown('<div class="hello">Cases</div>', unsafe_allow_html=True)
    ask_box(res, settings, store, "cases")
    options = list(VIEWS)
    labels = {v: f"{VIEWS[v]['label']} ({len(view_rows(df, v))})" for v in options}
    has_preset = bool(st.session_state.get("preset"))
    seg_key = f"seg_{view}_{has_preset}"

    def _on_seg():
        v = st.session_state.get(seg_key)
        if v:
            st.session_state["view"] = v
            st.session_state.pop("preset", None)

    st.segmented_control("Task", options, default=None if has_preset else view, format_func=lambda v: labels[v],
                         key=seg_key, on_change=_on_seg, label_visibility="collapsed")
    if not has_preset:
        st.markdown(f"<div class='sub'>{VIEWS[view]['help']}</div>", unsafe_allow_html=True)

    rows = view_rows(df, view)
    preset = st.session_state.get("preset")
    if preset:
        rows = apply_filters(df, preset["filters"]).sort_values(["priority_rank", "application_id"], na_position="last")
        b = st.columns([5, 1])
        b[0].markdown(f"<div class='callout'><b>From your question:</b> {describe_filters(preset['filters'])} "
                      f"· {len(rows)} cases</div>", unsafe_allow_html=True)
        if b[1].button("Clear", icon=":material/close:", use_container_width=True):
            st.session_state.pop("preset", None)
            st.rerun()
    c1, c2 = st.columns([2, 3])
    q = c1.text_input("Find an application", placeholder="Search by ID, e.g. A3022", label_visibility="collapsed")
    with c2.popover("Filters", icon=":material/filter_list:"):
        lang = st.multiselect("Language", LANGUAGES)
        stage = st.multiselect("Onboarding stage", sorted(df["onboarding_stage"].unique()))
        reason = st.multiselect("Reason", sorted(rows["code"].dropna().unique()), format_func=lambda c: f"{c} · {SHORT_REASON.get(c, c)}")
        prio = st.multiselect("Priority", ["High", "Medium", "Low"])
    if q:
        rows = rows[rows["application_id"].str.contains(q.strip(), case=False)]
    if lang:
        rows = rows[rows["preferred_language"].isin(lang)]
    if stage:
        rows = rows[rows["onboarding_stage"].isin(stage)]
    if reason:
        rows = rows[rows["code"].isin(reason)]
    if prio:
        rows = rows[rows["priority"].isin(prio)]

    if rows.empty:
        if not q:
            if lang or stage or reason or prio:
                st.info("No cases match these filters. Clear a filter and try again.", icon=":material/filter_list_off:")
            else:
                st.success("Nothing here - all done for this task.", icon=":material/task_alt:")
            return
        qid = q.strip().upper()
        if qid in set(df["application_id"]):
            home = next((v for v in ["review", "ready", "struggling", "closing"] if qid in set(view_rows(df, v)["application_id"])), "all")
            st.warning(f"{qid} isn't in this list. You'll find it under **{VIEWS[home]['label']}**.", icon=":material/info:")
            if st.button(f"Open {qid}", icon=":material/arrow_forward:"):
                st.session_state["case_list"] = list(view_rows(df, home)["application_id"])
                open_case(qid, home)
        elif qid in set(res.activated_df.get("application_id", pd.Series(dtype=str))):
            st.info(f"{qid} is already activated, so it doesn't need recovery.", icon=":material/verified:")
        else:
            st.error(f"No application found for “{q.strip()}”. Check the ID (e.g. A3022) and try again.", icon=":material/search_off:")
        return
    table = pd.DataFrame({
        "Priority": rows["priority"].fillna("—"),
        "Application": rows["application_id"],
        "Merchant": rows["business_type"] + " · " + rows["industry"] + " · " + rows["city"],
        "What's wrong": rows["code"].map(lambda c: SHORT_REASON.get(c, "Unclear - needs review") if c else "Unclear - needs review"),
        "Next step": rows.apply(next_step, axis=1),
        "Language": rows["preferred_language"],
    })
    st.caption(f"{len(table)} cases · click a row to open it")
    nonce = st.session_state.get("list_nonce", 0)
    ev = st.dataframe(table, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
                      key=f"list_{view}_{nonce}", height=min(35 * len(table) + 42, 560),
                      column_config={"Merchant": st.column_config.TextColumn(width="medium"),
                                     "What's wrong": st.column_config.TextColumn(width="medium")})
    sel = ev.selection.rows if ev and hasattr(ev, "selection") else []
    if sel:
        st.session_state["case_list"] = list(rows["application_id"])
        st.session_state["case"] = rows.iloc[sel[0]]["application_id"]
        st.rerun()
    with st.expander("Download results", icon=":material/download:"):
        c = st.columns(2)
        c[0].download_button("This list (CSV)", output_table(type("R", (), {"df": rows})()).to_csv(index=False).encode("utf-8-sig"),
                             f"cases_{view}.csv", "text/csv", use_container_width=True)
        c[1].download_button("Full output (Excel, all sheets)", excel_bytes(res), "recovery_output.xlsx",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)


def dropout_panel(aid, row, nudge, store):
    """R13 only: preview the one-tap reason question and record the merchant's answer."""
    with st.container(border=True):
        st.markdown("**Why did they stop?** <span class='small'>· optional question included in this first message</span>",
                    unsafe_allow_html=True)
        opts = nudge.get("quick_replies") or []
        if row["channel"] == "WhatsApp":
            st.caption("On WhatsApp this goes as a list message - the merchant just taps one option:")
            st.markdown(" ".join(pill(f"{i}. {o}", "rule") for i, o in enumerate(opts, 1)), unsafe_allow_html=True)
        else:
            st.caption("By email the merchant replies with the number (1-4).")
        current = row.get("dropout_reason")
        current = None if current is None or (isinstance(current, float) and pd.isna(current)) else current
        if current:
            st.markdown(f"<div class='callout good'><b>Merchant replied:</b> {DROPOUT_REASON_LABELS.get(current, current)}<br>"
                        f"<b>So we:</b> {DROPOUT_REASON_ACTIONS.get(current, '')}</div>", unsafe_allow_html=True)
        st.caption("When the merchant answers, record it here. The answer goes to the Razorpay team's dropout insights and "
                   "adjusts the follow-up plan.")
        c = st.columns([3, 1])
        pick = c[0].selectbox("Merchant's answer", DROPOUT_REASON_KEYS, format_func=lambda k: DROPOUT_REASON_LABELS[k],
                              index=DROPOUT_REASON_KEYS.index(current) if current in DROPOUT_REASON_KEYS else None,
                              placeholder="No reply yet", key=f"dr_{aid}", label_visibility="collapsed")
        if c[1].button("Record reply", disabled=pick is None or pick == current, use_container_width=True, key=f"drb_{aid}"):
            store.record_dropout_reason(aid, pick, actor())
            st.toast("Reply recorded")
            refresh()


def summary_text(row, d, dec, app) -> str:
    if d.code is None:
        return "<b>What's wrong:</b> unclear - the AI could not match the reviewer note to a known reason.<br><b>What happens next:</b> you decide the reason below."
    if d.human_review_required:
        return (f"<b>AI's reading (needs your confirmation):</b> {SHORT_REASON.get(d.code, d.code).lower()}. "
                f"If you confirm, the catalogue says: <b>{dec.decision}</b>.<br><b>Until then:</b> nothing is sent and the case "
                "stays out of the recovery queue.")
    exp_en = explanation(app, d.code, "English")
    lang, ch = row["preferred_language"], row["channel"]
    if dec.decision == "Recover":
        what = f"Send a {lang} {ch} message explaining the fix, with a link to continue."
        if row["friction_level"] == "High":
            what += " This merchant is struggling, so the message offers guided help and follow-ups end with a person reaching out."
        return f"<b>What's wrong:</b> {exp_en['issue']}<br><b>What the merchant must do:</b> {exp_en['fix']}<br><b>What we'll do:</b> {what}"
    if dec.decision == "Close":
        return ("<b>What's wrong:</b> the business type is one we cannot support.<br>"
                f"<b>What we'll do:</b> send a polite {lang} closure note. No workaround is offered and no follow-ups are sent.")
    return ("<b>What's wrong:</b> the application needs a compliance review. The reason is never shared with the merchant.<br>"
            f"<b>What we'll do:</b> hand it to the Compliance team. The merchant only hears (in {lang}) that it is under review.")


def case_view(ds, res, settings, store):
    aid = st.session_state["case"]
    view = st.session_state.get("view", "all")
    order = st.session_state.get("case_list") or list(view_rows(res.df, view)["application_id"])
    if aid not in order:
        order = list(res.df["application_id"])
    idx = order.index(aid)
    row = res.df.set_index("application_id").loc[aid]
    app, d, dec, nudge, fr, seq = (res.apps[aid], res.diagnoses[aid], res.decisions[aid], res.nudges[aid],
                                   res.frictions[aid], res.sequences[aid])

    def goto(i):
        st.session_state["case"] = order[i]
        st.rerun()

    nav = st.columns([1.3, 4, 1, 1])
    if nav[0].button("Back to list", icon=":material/arrow_back:"):
        st.session_state.pop("case", None)
        st.session_state["list_nonce"] = st.session_state.get("list_nonce", 0) + 1
        st.rerun()
    nav[1].markdown(f"<div class='small' style='padding-top:9px'>{VIEWS.get(view, VIEWS['all'])['label']} · case {idx + 1} of {len(order)}</div>",
                    unsafe_allow_html=True)
    if nav[2].button("Previous", disabled=idx == 0, use_container_width=True):
        goto(idx - 1)
    if nav[3].button("Next", disabled=idx >= len(order) - 1, use_container_width=True):
        goto(idx + 1)

    pills = color_pill(dec.decision or "No decision yet", DECISION_COLOR.get(dec.decision, GREY))
    if row["priority"]:
        pills += color_pill(f"{row['priority']} priority", LEVEL_COLOR[row["priority"]])
    if fr.level == "High":
        pills += color_pill("Struggling", RED)
    st.markdown(f"<div class='case-head'>{aid} · {SHORT_REASON.get(d.code, 'Needs review') if d.code else 'Needs review'}</div>"
                f"<div class='case-sub'>{app.business_type} · {app.industry} · {app.city} · prefers {app.preferred_language} · "
                f"stopped at {app.step_reached}, {app.days_since_last_activity} days ago</div>{pills}", unsafe_allow_html=True)
    st.markdown(f"<div class='summary'>{summary_text(row, d, dec, app)}</div>", unsafe_allow_html=True)

    # ---- decision needed first, if any
    if d.human_review_required:
        with st.container(border=True):
            st.markdown("**Your decision is needed before anything is sent**")
            for r_ in d.review_reasons:
                st.markdown(f"- {r_}")
            codes = list(ds.catalog)
            if d.code:
                st.markdown(f"AI suggests **{d.code} · {ds.catalog[d.code].reviewer_reason}** "
                            f"(confidence {d.confidence:.2f}) based on: *“{d.evidence}”*")
            note = st.text_input("Note (optional)", key=f"note_{aid}", placeholder="Why you agree or disagree")
            b = st.columns([1.2, 1.4, 2])
            if d.code and b[0].button(f"Confirm {d.code}", type="primary", use_container_width=True):
                store.record_review(aid, "confirmed", d.code, actor(), note, d.ai_code or d.code, d.confidence)
                st.toast(f"Confirmed {d.code}")
                refresh()
            with b[1].popover("Choose a different reason", use_container_width=True):
                other = st.selectbox("Correct reason", codes, format_func=lambda c: f"{c} · {ds.catalog[c].reviewer_reason}",
                                     key=f"other_{aid}")
                if st.button("Save correction", type="primary", key=f"save_{aid}"):
                    store.record_review(aid, "corrected", other, actor(), note, d.ai_code or d.code, d.confidence)
                    refresh()
            st.caption("Changing the reason never changes the catalogue's rules - closure and compliance outcomes still follow the catalogue.")

    t_msg, t_why, t_follow, t_merchant = st.tabs(["Message to merchant", "Why we think this", "Follow-up plan", "Merchant details"])

    # ---- message
    with t_msg:
        if not nudge["body"]:
            st.info("No message yet - confirm the reason first.")
        else:
            approved = store.approvals().get(aid)
            is_approved = bool(approved and approved.get("message_hash") == message_hash(nudge["body"]))
            st.markdown(f"**{row['channel']}** to the merchant in **{nudge['language']}** "
                        f"<span class='small'>· {row['channel_reason']}</span>", unsafe_allow_html=True)
            if row["channel"] != "WhatsApp" and row["draft_subject"]:
                st.caption(f"Subject: {row['draft_subject']}")
            st.code(nudge["body"], language=None, wrap_lines=True)
            st.caption("Copy with the icon at the top-right of the message. Nothing is ever sent from this app.")
            if nudge["language"] != "English" and d.code:
                with st.expander("What does this say? (English)"):
                    en = explanation(app, d.code, "English")
                    st.write(f"{en['issue']} {en['fix']}")
            if nudge["violations"]:
                st.error("Blocked by safety checks: " + "; ".join(nudge["violations"]))
            b = st.columns([1.4, 1.4, 1])
            can = dec.actionable and not nudge["violations"]
            if is_approved:
                b[0].success("Approved (not sent)")
                if b[1].button("Undo approval", use_container_width=True):
                    store.revoke_approval(aid, actor())
                    refresh()
            else:
                if b[0].button("Approve & next", type="primary", icon=":material/check:", disabled=not can, use_container_width=True):
                    store.approve_nudge(aid, actor(), message_hash(nudge["body"]), row["channel"])
                    st.toast(f"{aid} approved")
                    if idx < len(order) - 1:
                        st.session_state["case"] = order[idx + 1]
                    refresh()
                llm = LLMClient(settings["llm"])
                if dec.actionable and b[1].button("Rewrite with AI", icon=":material/auto_awesome:", disabled=not llm.available,
                                                  use_container_width=True, help="AI rewrites it more naturally; kept only if it passes every safety check."):
                    with st.spinner("Rewriting..."):
                        r = run_llm_nudge(app, res, store, llm, settings)
                    st.session_state["flash"] = "AI version passed the safety checks and is now shown." if r["ok"] else (r.get("error") or "AI rewrite failed.")
                    refresh()
            with b[2].popover("More", use_container_width=True):
                flagged = aid in store.flags()
                if st.button("Remove review flag" if flagged else "Flag for human review", use_container_width=True):
                    store.set_flag(aid, not flagged, actor(), "Flagged from case view")
                    refresh()
                stopped = any(e["event"] == "stop_communication" for e in store.followup_events().get(aid, []))
                if not stopped and st.button("Stop all messages to this merchant", use_container_width=True):
                    store.add_followup_event(aid, "stop_communication", actor(), "From case view")
                    refresh()
            if st.session_state.get("flash"):
                st.info(st.session_state.pop("flash"))
            if "LLM" in nudge["generation_source"]:
                st.caption("Polished by AI · passed every safety check.")
            if d.code == "R13" and dec.decision == "Recover":
                dropout_panel(aid, row, nudge, store)

    # ---- why
    with t_why:
        entry = ds.catalog.get(d.code) if d.code else None
        st.markdown(f"**Reason:** {d.code or '—'} · {entry.reviewer_reason if entry else 'undetermined'} {source_pill(d.source)}",
                    unsafe_allow_html=True)
        st.markdown(f"**Evidence:** {d.evidence or '—'}")
        st.markdown(f"**Confidence:** {d.confidence:.2f}")
        st.progress(min(max(d.confidence, 0.0), 1.0))
        st.markdown(f"**Decision:** {dec.decision or '—'} because the catalogue says this reason is fixable = "
                    f"*{dec.fixability}*. {pill('Rule', 'rule')}", unsafe_allow_html=True)
        if fr.indicators:
            st.markdown("**Signs of struggle:** " + "; ".join(fr.indicators))
            st.markdown(f"**Suggested help:** {fr.intervention}")
        if row["priority_reason"]:
            st.markdown(f"**Priority:** {row['priority_reason']}")
        with st.expander("Technical detail"):
            st.write(f"Method: {d.method}")
            st.write(d.reasoning)
            st.write("Rule trace: " + " ".join(dec.rule_trace))
            if d.alternatives:
                st.write("Other possible reasons: " + ", ".join(f"{c} ({s:.2f})" for c, s in d.alternatives))
            if d.llm:
                st.write(f"AI: {d.llm.get('code')} ({d.llm.get('confidence')}) - {d.llm.get('reasoning', '')}")
            st.write("Standard fix (catalogue): " + (dec.standard_fix or "—"))
        llm = LLMClient(settings["llm"])
        if app.rejection_code is None and app.rejection_message and not d.llm and llm.available:
            if st.button("Ask AI for a second opinion", icon=":material/auto_awesome:"):
                with st.spinner("Asking AI..."):
                    run_llm_diagnosis(app, ds, store, llm, actor())
                refresh()

    # ---- follow-up
    with t_follow:
        if not seq["steps"]:
            st.markdown(f"<div class='callout warn'>No follow-ups: {seq['stop_reason']}.</div>", unsafe_allow_html=True)
        else:
            st.caption(f"{seq['cadence']} cadence · Day 0 is when you approve the first message · stops automatically on "
                       "activation, closure, compliance routing, a 'stop' request, or when the merchant resumes.")
            if seq["stop_reason"]:
                st.markdown(f"<div class='callout warn'>{seq['stop_reason']}</div>", unsafe_allow_html=True)
            for s_ in seq["steps"]:
                cls = "internal" if s_["channel"] == "Internal" else ("off" if s_["status"] != "Planned" else "")
                st.markdown(f"<div class='step {cls}'><b>Day {s_['day']} · {s_['step']}</b> - {s_['purpose']} "
                            f"<span class='small'>· {s_['channel']} · {s_['status']}</span></div>", unsafe_allow_html=True)
            with st.expander("Read the follow-up messages"):
                for s_ in seq["steps"][1:]:
                    st.markdown(f"**{s_['step']}**")
                    st.code(s_["message"], language=None, wrap_lines=True)
            with st.expander("Try the stopping rules (simulation)"):
                b = st.columns(4)
                for i, (e, label) in enumerate([("merchant_resumed", "Merchant resumed"), ("activated", "Activated"),
                                                ("stop_communication", "Stop messages"), ("reset", "Reset")]):
                    if b[i].button(label, key=f"ev_{e}", use_container_width=True):
                        if e == "reset":
                            store.clear_followup_events(aid, actor())
                        else:
                            store.add_followup_event(aid, e, actor(), "simulated")
                        refresh()

    # ---- merchant
    with t_merchant:
        st.markdown(f"""<div class="kv">
<div class="k">Business</div><div class="v">{app.business_type} · {app.industry}</div>
<div class="k">City</div><div class="v">{app.city}</div>
<div class="k">Preferred language</div><div class="v">{app.preferred_language}</div>
<div class="k">WhatsApp opt-in</div><div class="v">{app.whatsapp_opted_in}</div>
<div class="k">Expected monthly TPV</div><div class="v">{fmt_tpv(app.expected_monthly_tpv_inr_lakh or 0)} <span class="small">(declared, not actual)</span></div>
<div class="k">Signed up</div><div class="v">{app.signup_date}</div>
<div class="k">Status / stage</div><div class="v">{app.application_status} at {app.step_reached}</div>
<div class="k">Last activity</div><div class="v">{app.days_since_last_activity} days ago</div>
<div class="k">Attempts / support calls</div><div class="v">{app.attempts} / {app.support_contacts}</div>
<div class="k">Rejection code on file</div><div class="v">{app.rejection_code or 'none (blank)'}</div>
<div class="k">Reviewer note</div><div class="v">{app.rejection_message or 'none'}</div>
</div>""", unsafe_allow_html=True)
        st.caption("All of the above comes straight from the workbook.")
        log = [e for e in store.audit_log(2000) if e["application_id"] == aid]
        if log:
            with st.expander(f"History ({len(log)} actions)"):
                st.dataframe(pd.DataFrame(log)[["ts", "event", "actor"]], hide_index=True, use_container_width=True)


# ============================================================================= INSIGHTS
TOPICS = {
    "why": "Why are merchants getting stuck?",
    "fix": "What should product fix first?",
    "tpv": "How much business is at stake?",
    "struggle": "Where are merchants struggling?",
}


def page_insights():
    ds, res, settings, store = get_results()
    df, s = res.df, res.summary
    st.markdown('<div class="hello">Insights</div><div class="sub">Pick a question. You get one clear answer; '
                'the numbers behind it are there if you want them.</div>', unsafe_allow_html=True)
    ask_box(res, settings, store, "insights")
    topic = st.session_state.get("topic", "why")
    picked = st.pills("Question", list(TOPICS), default=topic, format_func=lambda t: TOPICS[t],
                      key=f"pills_{topic}", label_visibility="collapsed")
    if picked and picked != topic:
        st.session_state["topic"] = picked
        st.rerun()
    st.write("")

    if topic == "why":
        bc = by_code(df)
        ab = abandonment_summary(df)
        top3 = top_systemic_causes(df, 3)
        names = " · ".join(f"{SHORT_REASON[t['code']]} ({t['applications']})" for t in top3.to_dict("records"))
        st.markdown(f"<div class='callout'><b>The short answer:</b> {ab['applications']} merchants "
                    f"({ab['pct_of_non_activated']:.0f}%) simply stopped midway - no rejection at all. Among real rejections, "
                    f"the three biggest fixable causes are <b>{names}</b>.</div>", unsafe_allow_html=True)
        labels = [SHORT_REASON.get(c, c) for c in bc["code"]]
        colors = [MUTED if c == "R13" else (RED if c in ("R10", "R12") else BLUE) for c in bc["code"]]
        st.plotly_chart(hbar(labels, list(bc["applications"]), colors, hover_suffix=" applications"),
                        use_container_width=True, config={"displayModeBar": False})
        st.caption("Blue: fixable rejections · grey: stopped without a rejection (a journey problem, not a document problem) · "
                   "red: cannot be recovered (restricted / compliance).")
        with st.expander("See the numbers"):
            st.dataframe(bc[["code", "diagnosed_reason", "applications", "pct_of_non_activated", "expected_tpv_lakh", "inferred"]]
                         .rename(columns={"code": "Code", "diagnosed_reason": "Reason", "applications": "Applications",
                                          "pct_of_non_activated": "% of stuck", "expected_tpv_lakh": "Expected TPV (₹L/mo)",
                                          "inferred": "Diagnosed by AI"}), hide_index=True, use_container_width=True)
        st.markdown("##### What stopped merchants tell us")
        n_r13 = int((df["code"] == "R13").sum())
        replies = df["dropout_reason"].dropna() if "dropout_reason" in df else pd.Series(dtype=str)
        if replies.empty:
            st.markdown(f"<div class='callout'>No replies yet. Each of the {n_r13} merchants who stopped midway gets an optional "
                        "one-tap question in their first message (<i>process confusing · documents not ready · chose another "
                        "provider · not needed now</i>). Answers recorded on a case appear here.</div>", unsafe_allow_html=True)
        else:
            vc = replies.value_counts()
            st.caption(f"{len(replies)} of {n_r13} stopped merchants have answered so far. Early signal, not yet representative.")
            st.plotly_chart(hbar([DROPOUT_REASON_LABELS.get(k, k) for k in vc.index], list(vc.values), hover_suffix=" replies"),
                            use_container_width=True, config={"displayModeBar": False})
        if st.button("See these merchants", icon=":material/arrow_forward:"):
            go_view("all")

    elif topic == "fix":
        interventions = config.load_rules("product_interventions.json")
        recs = product_recommendations(df, interventions)
        sf, ru = recs.iloc[0], recs.iloc[1]
        be = break_even_preventability(df, interventions, sf["theme_id"], ru["theme_id"])
        st.markdown(f"<div class='callout good'><b>Ship first: {sf['theme']}.</b><br>{sf['intervention']}</div>",
                    unsafe_allow_html=True)
        c = st.columns(3)
        big_number(c[0], sf["applications"], f"merchants affected ({sf['pct_of_non_activated']:.0f}% of stuck)")
        big_number(c[1], fmt_tpv(sf["expected_tpv_lakh"]), "expected monthly TPV involved")
        big_number(c[2], "2 stages", "name mismatches hit both KYC and bank verification")
        st.write("")
        st.markdown("**Why this one?** It is the largest cause that can be caught while the merchant is typing, before a "
                    "reviewer ever sees it. Stopped-midway merchants are more numerous, but we don't know *why* each one "
                    "stopped, so a product fix is less certain to help.")
        st.markdown("**Runners-up**")
        for _, r in recs.iloc[1:3].iterrows():
            st.markdown(f"- **{r['theme']}** - {r['applications']} merchants, {fmt_tpv(r['expected_tpv_lakh'])}")
        with st.expander("How the ranking works (and how sure we are)"):
            st.markdown(f"Each theme's score = half its share of merchants it could realistically prevent + half its share of "
                        f"expected TPV it could prevent. 'Could prevent' uses an **assumed** preventability per theme. "
                        f"Sensitivity: '{ru['theme']}' would take first place "
                        f"only if its preventability were ≥ {be} (we assumed {ru['preventability_assumed']}).")
            st.dataframe(recs[["rank", "theme", "codes", "applications", "expected_tpv_lakh", "preventability_assumed",
                               "ship_first_score", "operational_benefit"]], hide_index=True, use_container_width=True)
            st.caption(interventions.get("non_recoverable_note", ""))

    elif topic == "tpv":
        st.markdown(f"<div class='callout'><b>The short answer:</b> stuck merchants expect about "
                    f"<b>{fmt_tpv(s['tpv_non_activated_lakh'])}</b> a month in payments; <b>{fmt_tpv(s['tpv_recoverable_lakh'])}</b> "
                    f"of that sits with merchants who can still be recovered. These are the merchants' own declared "
                    f"expectations - not actual or recovered revenue.</div>", unsafe_allow_html=True)
        c = st.columns(3)
        big_number(c[0], fmt_tpv(s["tpv_recoverable_lakh"]), f"recoverable · {s['recoverable']} merchants")
        big_number(c[1], fmt_tpv(s["tpv_closure_lakh"]), f"closing (restricted) · {s['closure']} merchants")
        big_number(c[2], fmt_tpv(s["tpv_compliance_lakh"]), f"with Compliance · {s['compliance']} merchants")
        st.write("")
        dims = {"Reason": "code", "Onboarding stage": "onboarding_stage", "Industry": "industry",
                "Business type": "business_type", "Language": "preferred_language", "City": "city", "Priority": "priority"}
        by = st.selectbox("Break it down by", list(dims), index=1)
        col = dims[by]
        t = tpv_breakdown(df.dropna(subset=[col]) if col == "priority" else df, col)
        labels = [SHORT_REASON.get(x, x) for x in t[col]] if col == "code" else list(t[col])
        st.plotly_chart(hbar(labels, list(t["expected_tpv_lakh"]), fmt="₹{:,.1f}L", hover_suffix=" lakh / month"),
                        use_container_width=True, config={"displayModeBar": False})
        with st.expander("See the numbers"):
            st.dataframe(t, hide_index=True, use_container_width=True)

    else:  # struggle
        fs = settings["friction"]
        hi = df[df["friction_level"] == "High"]
        by_stage = hi.groupby("onboarding_stage")["application_id"].count().sort_values(ascending=False)
        top_stage = by_stage.index[0] if len(by_stage) else "-"
        st.markdown(f"<div class='callout warn'><b>The short answer:</b> {len(hi)} merchants show strong signs of struggle; "
                    f"the most are at <b>{top_stage}</b>. They need guided help rather than another generic reminder.</div>",
                    unsafe_allow_html=True)
        c = st.columns(3)
        big_number(c[0], int((df["attempts"] >= fs["attempts_high"]).sum()), f"tried {fs['attempts_high']}+ times")
        big_number(c[1], int((df["support_contacts"] >= fs["support_high"]).sum()), f"called support {fs['support_high']}+ times")
        big_number(c[2], int((df["days_since_last_activity"] >= fs["inactive_high_days"]).sum()), f"silent {fs['inactive_high_days']}+ days")
        st.write("")
        st.plotly_chart(hbar(list(by_stage.index), list(by_stage.values), hover_suffix=" struggling merchants"),
                        use_container_width=True, config={"displayModeBar": False})
        if st.button("Help these merchants", type="primary", icon=":material/arrow_forward:"):
            go_view("struggling")
        with st.expander("How 'struggling' is measured"):
            st.markdown(f"Points for repeated attempts (≥{fs['attempts_medium']}), support contacts (≥{fs['support_medium']}) and "
                        f"inactivity (≥{fs['inactive_medium_days']} days); **High** at {fs['level_high_min_points']}+ points. "
                        "These thresholds are prototype assumptions, not Razorpay policy - change them in Quality & settings.")
            piv = (df.pivot_table(index="onboarding_stage", columns="friction_level", values="application_id",
                                  aggfunc="count", fill_value=0).reindex(columns=["High", "Medium", "Low"], fill_value=0))
            st.dataframe(piv, use_container_width=True)


# ============================================================================= QUALITY & SETTINGS
def page_quality():
    ds, res, settings, store = get_results()
    st.markdown('<div class="hello">Quality & settings</div><div class="sub">How the assistant decides, how well it '
                'performs, and the knobs you can turn.</div>', unsafe_allow_html=True)
    t1, t2, t3, t4 = st.tabs(["How it works", "Test results", "Settings", "Data & audit log"])
    with t1:
        how_it_works(settings)
    with t2:
        test_results(ds, settings)
    with t3:
        settings_panel(ds, settings, store)
    with t4:
        data_panel(ds, store)


def how_it_works(settings):
    st.markdown("""
**Who decides what**

| Step | Who / what | Why |
|---|---|---|
| Read the workbook | Code (read-only) | Facts, never altered |
| Reason when a code is recorded | Catalogue lookup | Source of truth |
| Reason when only a reviewer note exists | Rules + AI, with a confidence score | Free text needs judgement; low confidence → a person |
| Recover / close / compliance | Fixed rules from the catalogue | AI and TPV can never override this |
| Channel | Rule: WhatsApp only with opt-in | Consent |
| Message wording | Templates in 6 languages, optionally polished by AI | Every message passes safety checks |
| Priority, struggle signals, follow-ups | Transparent scoring rules | Explainable, adjustable |
| Anything serious or uncertain | **A person** | Closures, compliance, low confidence |
""")
    st.markdown("**Safety checks on every message:** no WhatsApp without opt-in · no promised approvals or dates · no made-up "
                "links, emails or phone numbers · no workaround for restricted businesses · compliance reasons never revealed · "
                "written in the merchant's script · nothing is ever sent automatically.")
    st.markdown("**Labels you'll see:** " + pill("From workbook", "fact") + pill("AI-inferred", "ai") + pill("Rule", "rule")
                + pill("Recommendation", "rec") + pill("Assumption", "assume") + pill("Human-verified", "human"), unsafe_allow_html=True)


def test_results(ds, settings):
    path = config.OUTPUTS_DIR / "test_results.json"
    if not path.exists():
        write_results(run_validation(ds, settings))
    r = json.loads(path.read_text(encoding="utf-8"))
    m = r["metrics"]
    st.markdown(f"<div class='callout warn'>{r['disclaimer']}</div>", unsafe_allow_html=True)
    c = st.columns(4)
    big_number(c[0], f"{r['test_cases']['passed']}/{r['test_cases']['total']}", "test cases passed")
    big_number(c[1], m["policy_violations"], f"safety violations in {m['messages_checked']} messages")
    big_number(c[2], f"{m['paraphrase_generalisation']['correct']}/{m['paraphrase_generalisation']['total']}",
               "unseen notes read correctly by rules alone")
    lp = config.OUTPUTS_DIR / "llm_eval.json"
    if lp.exists():
        le = json.loads(lp.read_text(encoding="utf-8"))
        big_number(c[3], f"{le['paraphrases']['correct']}/{le['paraphrases']['total']}", "unseen notes read correctly with AI")
    else:
        big_number(c[3], "—", "AI not evaluated yet")
    st.write("")
    with st.expander("All measures"):
        st.dataframe(pd.DataFrame([
            ("Catalogue lookup (recorded codes)", f"{m['path_a_catalogue_lookup']['correct']}/{m['path_a_catalogue_lookup']['total']}", ""),
            ("Rules on recorded notes, code hidden", f"{m['leave_code_out_lexicon_in_sample']['correct']}/{m['leave_code_out_lexicon_in_sample']['total']}", m["leave_code_out_lexicon_in_sample"]["note"]),
            ("Blank-code rows vs manual labels", f"{m['blank_code_vs_manual_labels']['correct']}/{m['blank_code_vs_manual_labels']['total']}", "Builder's labels, not the hidden key"),
            ("Unseen paraphrases (rules)", f"{m['paraphrase_generalisation']['correct']}/{m['paraphrase_generalisation']['total']}", f"{m['paraphrase_generalisation']['silent_errors']} silent errors"),
            ("Recover / close / compliance decisions", f"{m['recoverability_decisions']['correct']}/{m['recoverability_decisions']['total']}", "Matches catalogue"),
            ("Compliance & restricted handling", "OK" if m["compliance_handling_ok"] and m["restricted_handling_ok"] else "FAIL", ""),
            ("Channel consent violations", m["channel_violations"], ""),
        ], columns=["Measure", "Result", "Note"]), hide_index=True, use_container_width=True)
    with st.expander("Test cases"):
        st.dataframe(pd.DataFrame([{"ID": c_["id"], "Application": c_["application_id"], "Scenario": c_["category"],
                                    "Result": "PASS" if c_["passed"] else "FAIL"} for c_ in r["cases"]]),
                     hide_index=True, use_container_width=True)
    with st.expander("Where it went wrong (kept, not hidden)"):
        st.dataframe(pd.DataFrame(r["paraphrases"])[["text", "expected", "pred", "confidence", "outcome"]],
                     hide_index=True, use_container_width=True)
        if lp.exists():
            st.caption("With AI:")
            st.dataframe(pd.DataFrame(le["paraphrases"]["rows"])[["text", "expected", "llm", "llm_confidence", "lexicon"]],
                         hide_index=True, use_container_width=True)
    ap = config.OUTPUTS_DIR / "assistant_eval.json"
    if ap.exists():
        ae = json.loads(ap.read_text(encoding="utf-8"))
        with st.expander("Ask-the-assistant evaluation (25 questions)"):
            st.markdown(" · ".join(f"**{k}**: {m['correct']}/{m['total']} correct, avg {m['avg_seconds']}s"
                                   for k, m in ae["modes"].items()))
            cm = ae["modes"].get("claude")
            if cm and cm.get("fell_back_to_rules"):
                st.warning(f"{ae['model']}: {cm['total'] - cm['fell_back_to_rules']} questions answered by AI; "
                           f"{cm['fell_back_to_rules']} fell back to the rules (API unavailable during the run - credit ran out).")
            st.caption("Expected answers are computed straight from the data, separately from the assistant. The rules fallback "
                       "was tuned on these same questions (it started at 22/25), so its score is in-sample. "
                       "Re-run: python scripts/eval_assistant.py")
            first = next(iter(ae["modes"].values()))["rows"]
            st.dataframe(pd.DataFrame({"Question": [r["question"] for r in first],
                                       **{k: ["PASS" if r["passed"] else "FAIL" for r in m["rows"]] for k, m in ae["modes"].items()}}),
                         hide_index=True, use_container_width=True)
    if st.button("Re-run tests"):
        with st.spinner("Running..."):
            write_results(run_validation(ds, settings))
        st.rerun()


def settings_panel(ds, settings, store):
    st.markdown(pill("Assumption", "assume") + " These are prototype assumptions, not Razorpay policy. Defaults stay in "
                "config/settings.json; your changes are saved separately.", unsafe_allow_html=True)
    new = copy.deepcopy(settings)
    with st.form("thresholds"):
        with st.expander("Diagnosis", expanded=True):
            c = st.columns(2)
            new["diagnosis"]["auto_accept_confidence"] = c[0].slider("Send to a person below this confidence", 0.5, 0.95,
                                                                     float(settings["diagnosis"]["auto_accept_confidence"]), 0.01)
            new["diagnosis"]["abandonment_min_inactive_days"] = c[1].number_input("Treat as 'stopped midway' after (days inactive)", 0, 60,
                                                                                  int(settings["diagnosis"]["abandonment_min_inactive_days"]))
        with st.expander("Struggle signals"):
            c = st.columns(4)
            labels = {"attempts_medium": "Attempts: some", "attempts_high": "Attempts: many", "support_medium": "Support calls: some",
                      "support_high": "Support calls: many", "inactive_medium_days": "Silent days: some", "inactive_high_days": "Silent days: long",
                      "inactive_severe_days": "Silent days: very long", "level_high_min_points": "Points for 'High'"}
            for i, (k, lab) in enumerate(labels.items()):
                new["friction"][k] = c[i % 4].number_input(lab, 0, 90, int(settings["friction"][k]))
        with st.expander("Priority"):
            c = st.columns(7)
            w = {k: c[i].number_input(k, 0.0, 1.0, float(v), 0.025) for i, (k, v) in enumerate(settings["priority"]["weights"].items())}
            tot = sum(w.values()) or 1
            new["priority"]["weights"] = {k: round(v / tot, 4) for k, v in w.items()}
            c = st.columns(2)
            new["priority"]["high_min_score"] = c[0].number_input("High priority from score", 0, 100, int(settings["priority"]["high_min_score"]))
            new["priority"]["medium_min_score"] = c[1].number_input("Medium priority from score", 0, 100, int(settings["priority"]["medium_min_score"]))
        with st.expander("Follow-up timing (days after approval)"):
            c = st.columns(4)
            for i, k in enumerate(["follow_up_1", "follow_up_2", "final"]):
                new["followup"]["standard_offsets_days"][k] = c[i].number_input(k.replace("_", " ").capitalize(), 1, 60,
                                                                                 int(settings["followup"]["standard_offsets_days"][k]))
            new["followup"]["max_automated_messages"] = c[3].number_input("Max automated messages", 1, 6, int(settings["followup"]["max_automated_messages"]))
        saved = st.form_submit_button("Save", type="primary")
    if saved:
        config.save_settings_override(config.diff_from_defaults(new))
        st.toast("Saved - everything recalculated")
        refresh()
    if st.button("Reset to defaults"):
        config.reset_settings_override()
        refresh()
    with st.expander("AI layer (Claude)"):
        llm = LLMClient(settings["llm"])
        st.markdown(f"Status: **{llm.status()}**. Used only to read free-text reviewer notes and to polish message wording. "
                    "It never decides closure, compliance, channel or priority. Set `ANTHROPIC_API_KEY` in `.env` to enable.")
        blank = [a for a in ds.applications if a.application_status != "Activated" and not a.rejection_code and a.rejection_message]
        c = st.columns(2)
        if c[0].button(f"Ask Claude about all {len(blank)} blank-code cases", disabled=not llm.available):
            for a in blank:
                run_llm_diagnosis(a, ds, store, llm, actor())
            refresh()
        if c[1].button("Clear Claude's cached answers"):
            store.clear_llm_cache()
            refresh()


def data_panel(ds, store):
    rep = ds.report
    st.markdown(f"**Workbook:** {rep.source} · {rep.application_rows} applications · {rep.catalog_rows} reason codes · "
                f"{'no errors' if rep.ok else str(len(rep.errors)) + ' errors'} · IDs unique: {'yes' if not rep.duplicate_ids else 'no'}")
    for e in rep.errors:
        st.error(e)
    for w in rep.warnings:
        st.warning(w)
    c = st.columns(2)
    if c[0].button("Reload workbook from disk"):
        st.session_state.pop("uploaded_bytes", None)
        _load_dataset.clear()
        refresh()
    up = c[1].file_uploader("Or load another copy (kept in memory; original untouched)", type=["xlsx"])
    if up is not None and st.session_state.get("uploaded_name") != up.name:
        st.session_state["uploaded_bytes"], st.session_state["uploaded_name"] = up.getvalue(), up.name
        refresh()
    with st.expander("Missing values per column"):
        st.dataframe(pd.DataFrame([rep.missing_values]).T.rename(columns={0: "missing"}), use_container_width=True)
        st.caption("Blank rejection codes/messages are kept as missing information, never filled in.")
    with st.expander("Reason catalogue (source of truth)"):
        st.dataframe(ds.catalog_df, hide_index=True, use_container_width=True)
    log = store.audit_log(1000)
    with st.expander(f"Audit log ({len(log)} events)"):
        if log:
            st.dataframe(pd.DataFrame(log), hide_index=True, use_container_width=True)
        rv = store.reviews()
        if rv:
            st.markdown("**Human corrections vs AI suggestions**")
            st.dataframe(pd.DataFrame(rv).T, use_container_width=True)
    with st.expander("Reset demo state"):
        st.caption("Clears human reviews, flags, approvals, simulated events and the saved assistant question. The audit log is kept.")
        if st.button("Reset"):
            for name in ("reviews.json", "flags.json", "approvals.json", "followup_events.json", "assistant_current.json"):
                p = config.STATE_DIR / name
                if p.exists():
                    p.unlink()
            store.audit("state_reset", None, actor(), {})
            st.session_state.pop("current_q", None)
            refresh()


# ============================================================================= navigation
PAGES = {
    "home": st.Page(page_home, title="Home", icon=":material/home:", url_path="home", default=True),
    "cases": st.Page(page_cases, title="Cases", icon=":material/inbox:", url_path="cases"),
    "insights": st.Page(page_insights, title="Insights", icon=":material/insights:", url_path="insights"),
    "quality": st.Page(page_quality, title="Quality & settings", icon=":material/tune:", url_path="quality"),
}

try:
    _ds = get_dataset()
    _settings = config.load_settings()
except FileNotFoundError as e:
    st.error(str(e))
    up = st.file_uploader("Upload Buildathon_Student_Data_Pack.xlsx", type=["xlsx"])
    if up is not None:
        st.session_state["uploaded_bytes"], st.session_state["uploaded_name"] = up.getvalue(), up.name
        st.rerun()
    st.stop()

nav = st.navigation(list(PAGES.values()))
with st.sidebar:
    st.markdown('<div class="brand"><div class="brand-logo">R</div><div><div class="brand-title">Onboarding Recovery</div>'
                '<div class="brand-sub">KYC drop-off assistant</div></div></div>', unsafe_allow_html=True)
    st.write("")
    if "actor" not in st.session_state:
        st.session_state["actor"] = "ops.reviewer"
    st.text_input("Signed in as", key="actor")
    _llm = LLMClient(_settings["llm"])
    st.caption(("● AI assist on" if _llm.available else "○ AI assist off (rules only)") + " · nothing is ever sent")
nav.run()
