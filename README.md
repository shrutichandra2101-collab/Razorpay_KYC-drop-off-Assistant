# Merchant Onboarding & KYC Recovery Assistant

Razorpay Campus AI Build-a-thon, **Problem 3: Merchant onboarding and KYC drop-off recovery**.

For every application that has not been activated, this prototype:

1. works out why it stalled,
2. explains the fix in plain language in the merchant's preferred language,
3. decides whether to **Recover**, **Close** or **Route to Compliance**, and
4. drafts the right nudge on a channel the merchant has agreed to.

It also flags struggling merchants early, ranks the recovery queue, previews the follow-up messages, and tells the product team which fix to ship first.

It runs on the full synthetic workbook. It works offline using rules and curated templates. Claude is an optional, validated layer on top. No message is ever sent from the prototype.

---

## Quick start

```bash
cd PS-3
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional: add ANTHROPIC_API_KEY to enable the AI layer
streamlit run app.py            # opens http://localhost:8501
```

The workbook is found automatically: any `Buildathon_Student_Data_Pack*.xlsx` in this folder or in `data/`. You can also set `WORKBOOK_PATH` in `.env`, or upload a copy on the Settings page. The original file is opened read-only and never modified.

From the command line, without the UI:

```bash
python scripts/run_pipeline.py   # processes all rows, writes outputs/recovery_output.xlsx + .csv + test results
pytest -q                        # 12 tests, including the 33-case validation suite
```

## Results on the supplied workbook (all computed live, nothing hard-coded)

| | |
|---|---|
| Applications | 100 (25 activated and excluded, **75 non-activated**: 67 stalled, 8 rejected) |
| Recover / Close / Compliance | **67 / 5 / 3** |
| Expected monthly TPV linked to recoverable applications | **₹933.9 lakh**. This is the merchants' *declared expected* TPV, not actual or recovered revenue. |
| Blank rejection codes | 4 with reviewer text only (diagnosed from the text); 22 with no code and no text (abandonment, R13) |
| Top 3 systemic fixable causes | **R01** PAN name mismatch (9), **R06** missing website policy pages (7, ₹128.1L), **R03** bank name mismatch (7, ₹77.3L) |
| Abandonment (journey problem, reported separately) | 22 applications (29.3%), ₹307.6L; 10 of them stopped at *Business details* |
| Ship first | **Early name-match validation (PAN + bank)**: covers R01 and R03, 16 applications |
| Human review pending | 1: **A3051**, a restricted category inferred from text, held until a person confirms it |

## How the work is split between AI, rules and humans

| Component | Approach | Where |
|---|---|---|
| Ingestion and validation | Python / pandas, read-only | `recovery/ingest.py` |
| Application status, code lookup | Rules | `recovery/diagnosis.py` |
| Missing-code diagnosis | Explainable lexicon over the 13 catalogue codes, **plus optional Claude cross-check** | `rules/diagnosis_lexicon.json`, `prompts/diagnose_missing_code.md` |
| Confidence | Signal strength + margin over runner-up + stage consistency; LLM agreement raises it, disagreement sends the case to a human | `recovery/diagnosis.py` |
| Fixability and decision | Catalogue `fixable` column, plus hard guardrails (R10 → Close, R12 → Compliance) | `recovery/decision.py`, `rules/decision_rules.json` |
| Channel | Rule: WhatsApp only if `whatsapp_opted_in == "Yes"`; otherwise Email (placeholder) + in-app | `recovery/channel.py` |
| Explanations and nudges | Curated six-language templates built from structured context; **optional Claude rewrite** that must pass every guardrail | `recovery/i18n.py`, `recovery/messaging.py`, `prompts/nudge_generation.md` |
| Guardrails | Validators run on every merchant-facing message | `recovery/guardrails.py` |
| Friction | Explainable points model | `recovery/friction.py` |
| Prioritisation | Weighted score with written explanation | `recovery/priority.py` |
| Follow-ups | Configurable sequence with stopping rules | `recovery/followup.py` |
| Product insights | Data analysis + stated preventability assumptions | `recovery/insights.py`, `rules/product_interventions.json` |
| Human actions | Verify/correct diagnosis, flag, approve (never send), stop communication; everything audit-logged | `recovery/store.py` → `state/` |

**The AI never decides** fixability, closure, compliance routing, channel, priority or stopping rules. Its outputs are checked before use:

- a code must exist in the catalogue;
- the quoted evidence must appear word for word in the reviewer message;
- a rewritten message must pass all guardrails, otherwise the curated template is kept.

Default model is `claude-sonnet-5-5` (configurable via `LLM_MODEL`), called through the official `anthropic` SDK at low effort with server-side refusal fallback.

Every value in the UI is tagged as one of: **Workbook fact**, **AI-inferred**, **Rule-based decision**, **Recommendation**, **Assumption** or **Human-verified**.

## Guardrails (all enforced in code and tested)

- **No invented reasons or requirements.** Explanations are plain-language versions of the catalogue's meaning and standard fix. An unknown code, or a rejection with no code and no message, goes to human review.
- **R10 (restricted category):** a polite closure. No workaround, no "re-apply", no resume link, no follow-ups, never queued.
- **R12 (compliance):** routed to the Compliance team. The merchant only hears that the application is under review. Words like *screening*, *sanction*, *watchlist* or *compliance* are blocked in every language.
- **An inferred R10 or R12** (not recorded in the workbook) always needs a human to confirm it before anything is actionable.
- **High TPV can never override** closure or compliance routing.
- **WhatsApp only with explicit opt-in.** No invented emails, phone numbers, URLs, dates or approval promises. `[RESUME_LINK]` is a clearly labelled placeholder.
- **Low confidence or disagreement** means no actionable recommendation until a person verifies it.
- **Approval is recorded, never sent.** AI outputs and human corrections are written to `state/audit_log.jsonl`.

## Ask the assistant

There's an ask bar at the top of **Home** and **Cases**. Ask in plain words, e.g.:

- "How many are stuck at KYC?"
- "Which of these are high priority?"
- "Why is A3051 waiting?"
- "What should product fix first?"

Each reply has three parts:

1. **The answer first**, e.g. "**32 applications** are stuck at KYC docs…"
2. **Buttons to where the data lives.** They open the Cases list already filtered (with a "From your question" banner), a specific case, or an Insights topic.
3. **Up to three suggested follow-up questions.** "These" in a follow-up keeps the previous filters.

**Your current question stays with you.** Only the latest question and answer are kept, saved per operator in `state/assistant_current.json`. When you follow a button to another page, it shows there folded ("Your question: …"). When you come back to Home, the full answer and its follow-ups are still there to continue from, even after a browser refresh. Asking a new question replaces it, and **Clear** removes it. No other history is stored.

Which data lookups produced each answer isn't shown on screen, to keep it clean, but it is recorded in the audit log (Quality & settings → Data & audit log) for traceability.

How it stays correct (`recovery/assistant.py`, `prompts/assistant.md`):

- **Claude only reads.** It answers using read-only data functions (`count_cases`, `list_cases`, `get_case`, `top_causes`, `tpv_breakdown`, `friction_summary`, `product_recommendation`). These are computed from the same data as the dashboard.
- **Every number is checked.** Each number in Claude's answer must appear in what those functions returned. If one doesn't, the answer is replaced by a fallback built from simple rules.
- **Every button is checked.** Filters and application IDs are validated, and an invalid filter never silently widens to "all cases".
- **It can't take actions.** Requests to approve, send, close or change something get "I can only read".
- **Off-topic questions are declined.**
- **Without an API key**, the same rules-based fallback answers common questions offline.
- **Every question is logged** in the audit log.

**Evaluation** (`python scripts/eval_assistant.py` → `outputs/assistant_eval.md`). There are 25 questions, and each expected answer is computed directly from the data, separately from the assistant:

| Mode | Score | Average time |
|---|---|---|
| Claude Opus 5.5 (first build) | **25/25** | about 4.7 s |
| Claude Sonnet 5.5 (current default) | **25/25** | about 3.6 s |
| Rules fallback | 25/25 | instant |

The rules fallback's score is in-sample: it started at 22/25 and I fixed its three gaps using these same questions.

## Learning why merchants abandon (R13)

22 merchants stopped with no rejection, so nobody knows why. Their first reminder ends with a tiny optional question in their own language: *"what made you pause?"*

1. the process felt long or confusing
2. I didn't have the documents ready
3. I chose another payment provider
4. I don't need it right now

On WhatsApp it goes as a tap-to-reply list; by email the merchant replies with a number. Ops records the answer on the case. Each answer:

- is counted in **Insights → Why are merchants getting stuck?**, alongside the other causes;
- is included in the export;
- changes the follow-up plan:
  - *confusing* → guided help, ending in a hand-off to a specialist
  - *documents not ready* → standard reminders continue
  - *another provider* → reminders stop
  - *not now* → only the final reminder is sent

The question appears only in the first R13 message and in no other code's messages. A Claude rewrite must keep the options word for word, or it is rejected.

## Assumptions introduced by this application (editable in Settings)

All thresholds live in `config/settings.json`; your edits are saved to `state/settings_override.json`. None of these values are Razorpay policy.

- **Abandonment (R13):** status *Stalled*, no code, no message, and inactive for at least 3 days. Under 3 days the case is held for human review, because the merchant may still be working on it.
- **Auto-accept confidence for inferred codes:** 0.75.
- **Friction points:**
  - attempts: 2 → 1 point, 3 → 2, 4 → 3
  - support contacts: 1 → 1, 2 → 2, 3 → 3
  - inactivity: 14 days → 1, 21 → 2, 30 → 3
  - level: **High** at 5 points or more, **Medium** at 3 or more
  - The data records only the current stage, so "repeated activity at the same stage" means 2 or more attempts while still stalled there.
- **Priority weights:** TPV .35 (log-scaled), inactivity .20, friction .15, attempts .075, support .075, confidence .10, quick fix .05. High ≥ 60, Medium ≥ 42. A high-friction merchant is never ranked Low.
- **Follow-up timing:**
  - standard: Day 0 / 3 / 7 / 14
  - high friction: Day 0 / 2 / 5 / 10, ending with a hand-off to a person
  - at most 4 automated messages; Day 0 is when ops approves the first nudge
- **Product "preventability"** per theme (`rules/product_interventions.json`). The ship-first ranking depends on these numbers, so the app also shows a sensitivity check: abandonment fixes would overtake name-match validation only if their preventability were 0.47 or higher (assumed 0.35).

## Testing: what is and is not measured

The hidden answer key is **not** in the workbook, so none of the figures below are hidden-answer accuracy. Full report: `outputs/test_results.md`, and the **Test Results** page in the app.

| Measure | Result | Honest note |
|---|---|---|
| Targeted test cases (27 real rows + 1 activated + 5 made-up edge cases) | 33/33 | Cover all 13 codes, blank codes with and without text, R10/R12, all 6 languages, WhatsApp opted in and out, high and low TPV, repeated attempts, many support contacts, long inactivity |
| Blank-code rows vs **my manual labels** | 4/4 | A3022→R05, A3028→R01, A3051→R10 (held for human), A3081→R09 |
| Lexicon on the 49 recorded rows with the code hidden | 49/49 | **In-sample and optimistic**: I wrote the lexicon after reading these messages |
| **Unseen paraphrases I wrote** | **24/26** | Missed "Utility bill older than 3 months" (no word "address") and "Applicant never completed the form". Both were sent to human review, so **0 silent errors**. |
| Recover/Close/Route vs the catalogue | 75/75 | Rule consistency |
| Channel-consent violations / policy violations | 0 / 0 across 253 messages | Includes all follow-up messages |
| Prioritisation | deterministic, higher TPV never lowers a score, R10/R12 never queued, high friction never Low | |

**Claude layer** (`python scripts/eval_llm.py` → `outputs/llm_eval.md`; `claude-sonnet-5-5`, low effort, about 38 API calls; Opus 5.5 scored the same on the first build):

| Measure | Claude | Rules alone |
|---|---|---|
| Blank-code rows vs manual labels | 4/4, evidence quoted word for word | 4/4 |
| Unseen paraphrases | **26/26**, including the two the rules missed; both vague notes got confidence ≤ 0.45, so they still go to human review | 24/26 |
| Message rewrites passing every guardrail (6 languages, includes R10 and R12) | 12/12 | n/a |

In the combined system:

- When the rules and Claude agree, confidence rises (A3022 goes from 0.86 to 0.95).
- When they disagree, the case goes to a person.
- When the rules find no strong signal, Claude's answer is used but capped at 0.85.
- An inferred R10 or R12 still always needs a person to confirm it.

**Known limitations:**

- The rules on their own are the weak point on unseen wording, and Claude covers that gap. If the API is unavailable, the app still works, but those cases fall back to human review.
- Non-English templates were written by hand, not reviewed by native speakers. Do that before any real use.
- `days_since_last_activity` is the only time signal, so follow-up dates are relative.

## Using the app (the ops user's journey)

The interface is built around tasks, not data. Detail appears only when you ask for it.

| Page | What you do there |
|---|---|
| **Home** | Ask a question, or see what needs you today: four task cards (*Check AI diagnoses*, *Send recovery messages*, *Help struggling merchants*, *Close or hand off*), three headline numbers, and three questions you can ask. |
| **Cases** | Work through one task list. Each row shows only priority, ID, merchant, the problem in plain words, and the next step. Click a row to open the case. Search and filters sit behind a small button. |
| **Case view** | Starts with a short summary of what's wrong, what the merchant must do, and what we'll do. If a person needs to decide, that comes first ("Confirm R10" / "Choose a different reason"). Four tabs hold the rest: *Message* (copy, *Approve & next*, *Rewrite with AI*), *Why we think this* (evidence, confidence, technical detail folded away), *Follow-up plan*, *Merchant details*. Previous/Next buttons move through the list. |
| **Insights** | Pick one question at a time: why merchants get stuck, what product should fix first, how much TPV is at stake (break it down your way), or where merchants struggle. Each gives a short answer and one chart; the numbers are in an expander. |
| **Quality & settings** | How it works, test results, adjustable thresholds, data checks, and the audit log. |

## Demo workflow (real IDs)

1. **Home**: 75 stuck, 67 recoverable, ₹933.9L expected TPV, four task cards.
2. **Check AI diagnoses → A3051**: blank code; the AI reads "Business category not supported by policy" as R10 (0.84, rules and Claude agree; Claude alone was more cautious at 0.72). Nothing can be sent until you click *Confirm R10*. After that, it becomes a polite closure with no workaround.
3. **Send recovery messages**: highest priority first. **A3023** (₹59.9L, 3 attempts, 3 support calls) gets a Kannada WhatsApp message offering guided help. *Approve & next* moves to the next case. **A3004**: R01 from the workbook, Hindi message with `[RESUME_LINK]`. **A3022**: blank code; "Doc blurred and cropped" is diagnosed as R05 (0.95). Use *Why we think this* to see the evidence.
4. **Close or hand off**: **A3094**, R12 in Tamil, routed to Compliance; the merchant only hears it's under review, with no follow-ups. **A3096**: R12 with ₹48L TPV, still kept out of the queue.
5. **Help struggling merchants → A3021**: *Follow-up plan* shows a compressed cadence ending in a human hand-off. Try *Merchant resumed* or *Stop messages* to see the stopping rules work.
6. **Insights**: *What should product fix first?* → name-match validation, with the reasoning and a sensitivity check. *Why are merchants getting stuck?* → abandonment is shown as a separate journey problem.
7. **Quality & settings → Test results**: 33/33 cases pass, 0 safety violations; unseen notes are read 24/26 by the rules alone and 26/26 with Claude. Failures are listed, not hidden.

## Project layout

```
app.py                      Streamlit UI (Home, Cases, Insights, Quality & settings)
recovery/                   engine: ingest, diagnosis, decision, channel, i18n, messaging, guardrails,
                            friction, priority, followup, insights, llm, store, pipeline, export, validation
rules/                      decision rules, diagnosis lexicon, product interventions (JSON)
prompts/                    LLM prompts (diagnosis, nudge rewrite)
config/settings.json        default thresholds and weights (assumptions)
tests/test_engine.py        pytest suite
scripts/run_pipeline.py     CLI: process, export, validate
outputs/                    recovery_output.xlsx/.csv, test_results.json/.md
state/                      runtime human actions, LLM cache, audit log (git-ignored)
```

All merchant data is synthetic and comes from the provided Data Pack. No real merchant or customer data is used.
