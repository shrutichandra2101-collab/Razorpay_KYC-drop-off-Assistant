"""Merchant Onboarding & KYC Recovery Assistant - core engine.

Module map
----------
ingest      Feature 1  - workbook ingestion + validation (read-only)
diagnosis   Features 3, 8 - catalogue lookup, free-text inference, abandonment, confidence
decision    Feature 4  - catalogue-driven Recover / Close / Route to Compliance
channel     Feature 6  - consent-aware channel selection
messaging   Features 5, 6, 9 - multilingual, context-aware nudges (templates + optional LLM)
guardrails  Section 7  - policy validators applied to every merchant-facing message
friction    Feature 10 - explainable friction scoring
priority    Feature 11 - weighted, explainable recovery prioritisation
followup    Feature 12 - configurable follow-up sequence + stopping rules
insights    Features 13, 14 - TPV analytics and product recommendations
store       Section 7.10 - local state: human reviews, approvals, audit log, LLM cache
pipeline    Orchestrates everything into one application-level output table
"""
