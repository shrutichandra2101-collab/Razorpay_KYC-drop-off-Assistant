# Validation results

Generated: 2026-11-09 10:41 UTC

> Hidden evaluator answers are not available. Accuracy figures below are against MANUAL labels written by the builder and rule-consistency checks - not hidden-answer accuracy.

**Test cases:** 33/33 passed (181/181 individual checks)

| Metric | Result |
|---|---|
| Path A catalogue lookup | 49/49 |
| Leave-code-out lexicon (in-sample) | 49/49 |
| Blank-code rows vs manual labels | 4/4 |
| Paraphrase generalisation (unseen) | 24/26 (0 silent errors) |
| Recoverability decisions vs catalogue | 75/75 |
| Compliance (R12) handling | OK |
| Restricted (R10) handling | OK |
| Channel-consent violations | 0 |
| Policy violations across 253 messages | 0 |
| Cases requiring human review | 1 |

## Prioritisation consistency

- PASS - no R10/R12/Close/Route in queue 67 queued
- PASS - deterministic ranking across runs 
- PASS - higher TPV never lowers a merchant's score (monotonic) tested on 20 queued rows
- PASS - high-friction merchants never Low priority 15 high-friction

## Test cases

| ID | Application | Scenario | Result | Failed checks |
|---|---|---|---|---|
| TC01 | A3004 | R01 PAN name mismatch - Hindi - WhatsApp | PASS |  |
| TC02 | A3001 | R02 GST mismatch - Marathi | PASS |  |
| TC03 | A3012 | R03 bank name mismatch - Hinglish | PASS |  |
| TC04 | A3021 | R04 bank verification - 4 attempts, 40 days inactive | PASS |  |
| TC05 | A3016 | R05 unreadable document - Hindi | PASS |  |
| TC06 | A3010 | R06 missing policy pages - English | PASS |  |
| TC07 | A3019 | R07 website not live - Hinglish | PASS |  |
| TC08 | A3072 | R08 address proof - WhatsApp opted OUT | PASS |  |
| TC09 | A3007 | R09 signatory ID - Tamil | PASS |  |
| TC10 | A3039 | R10 restricted category - English | PASS |  |
| TC11 | A3015 | R11 category differs - Email channel | PASS |  |
| TC12 | A3082 | R12 compliance - English | PASS |  |
| TC13 | A3094 | R12 compliance - Tamil neutral wording | PASS |  |
| TC14 | A3005 | Blank code + no message -> abandonment R13 | PASS |  |
| TC15 | A3022 | Blank code + message -> AI infers R05 | PASS |  |
| TC16 | A3028 | Blank code + Hinglish message -> AI infers R01 | PASS |  |
| TC17 | A3051 | Blank code + message -> inferred R10 needs human confirmation | PASS |  |
| TC18 | A3081 | Blank code + message -> AI infers R09 | PASS |  |
| TC19 | A3020 | R06 - Kannada | PASS |  |
| TC20 | A3093 | R10 - Tamil closure, no workaround | PASS |  |
| TC21 | A3097 | R10 - Kannada closure | PASS |  |
| TC22 | A3023 | High TPV (59.9L) + 3 attempts + 3 support contacts | PASS |  |
| TC23 | A3025 | Low TPV (0.7L) merchant still queued | PASS |  |
| TC24 | A3096 | R12 with high TPV (48L) never enters queue | PASS |  |
| TC25 | A3085 | R01 with 4 attempts -> guided help | PASS |  |
| TC26 | A3047 | 3 support contacts -> support acknowledgement | PASS |  |
| TC27 | A3030 | Abandoned 34 days at Business details | PASS |  |
| TC28 | A3002 | Activated application excluded | PASS |  |
| TC29 | SYN-1 | Rejected with no code and no message (synthetic) | PASS |  |
| TC30 | SYN-2 | Unknown code R99 (synthetic) | PASS |  |
| TC31 | SYN-3 | Stalled, no code, active yesterday (synthetic) | PASS |  |
| TC32 | SYN-4 | Opt-in value missing -> no WhatsApp (synthetic) | PASS |  |
| TC33 | SYN-5 | Ambiguous reviewer note (synthetic) | PASS |  |

## Paraphrase set (failures are kept, not tuned away)

| Text | Expected | Predicted | Conf. | Outcome |
|---|---|---|---|---|
| Name on PAN is not same as the firm name | R01 | R01 | 0.66 | correct but low confidence -> human review |
| PAN holder name mismatch with entity | R01 | R01 | 0.41 | correct but low confidence -> human review |
| GSTIN status cancelled on portal | R02 | R02 | 0.97 | correct |
| GST certificate belongs to another legal entity | R02 | R02 | 0.68 | correct but low confidence -> human review |
| Cheque shows a different account holder name | R03 | R03 | 0.86 | correct |
| Beneficiary name in bank does not match business | R03 | R03 | 0.86 | correct |
| Penny drop failed, please recheck IFSC | R04 | R04 | 0.88 | correct |
| Account number galat hai, test credit fail | R04 | R04 | 0.51 | correct but low confidence -> human review |
| Uploaded PDF is password protected | R05 | R05 | 0.68 | correct but low confidence -> human review |
| Image is blurry, upload again | R05 | R05 | 0.97 | correct |
| Refund policy and contact us page not found on site | R06 | R06 | 0.93 | correct |
| Terms and privacy pages absent from app | R06 | R06 | 0.97 | correct |
| Website under maintenance, unable to review | R07 | R07 | 0.86 | correct |
| Products and prices visible only after login | R07 | R07 | 0.58 | correct but low confidence -> human review |
| Utility bill older than 3 months | R08 | None | 0.3 | wrong: predicted None |
| Address on rent agreement differs from application | R08 | R08 | 0.86 | correct |
| Director DOB does not match ID | R09 | R09 | 0.86 | correct |
| Authorised signatory KYC missing | R09 | R09 | 0.6 | correct but low confidence -> human review |
| Online betting services not supported | R10 | R10 | 0.68 | correct but low confidence -> human review |
| Sells prohibited goods | R10 | R10 | 0.68 | correct but low confidence -> human review |
| Website sells electronics but category chosen is apparel | R11 | R11 | 0.61 | correct but low confidence -> human review |
| Category mismatch with site content | R11 | R11 | 0.91 | correct |
| Name matched a watchlist during screening | R12 | R12 | 0.86 | correct |
| Applicant never completed the form | R13 | None | 0.0 | wrong: predicted None |
| Please check the details and resubmit | None | R05 | 0.68 | correctly sent to human review |
| Documents pending | None | None | 0.3 | correctly sent to human review |
