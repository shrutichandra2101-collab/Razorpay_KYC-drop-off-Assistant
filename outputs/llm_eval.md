# Claude layer evaluation (claude-sonnet-5-5, effort low)

Generated 2026-11-09 11:44 UTC. Labels are the builder's manual labels, not the hidden answer key. Paraphrases are author-written.

- Blank-code rows vs manual labels: **4/4**
- Unseen paraphrases: **26/26** (ambiguous notes count as correct only if confidence < 0.75)
- LLM nudge rewrites passing all guardrails: **12/12**

| Paraphrase | Expected | Claude | Conf. | Lexicon |
|---|---|---|---|---|
| Name on PAN is not same as the firm name | R01 | R01 | 0.95 | R01 |
| PAN holder name mismatch with entity | R01 | R01 | 0.93 | R01 |
| GSTIN status cancelled on portal | R02 | R02 | 0.93 | R02 |
| GST certificate belongs to another legal entity | R02 | R02 | 0.93 | R02 |
| Cheque shows a different account holder name | R03 | R03 | 0.88 | R03 |
| Beneficiary name in bank does not match business | R03 | R03 | 0.9 | R03 |
| Penny drop failed, please recheck IFSC | R04 | R04 | 0.95 | R04 |
| Account number galat hai, test credit fail | R04 | R04 | 0.96 | R04 |
| Uploaded PDF is password protected | R05 | R05 | 0.95 | R05 |
| Image is blurry, upload again | R05 | R05 | 0.95 | R05 |
| Refund policy and contact us page not found on site | R06 | R06 | 0.95 | R06 |
| Terms and privacy pages absent from app | R06 | R06 | 0.93 | R06 |
| Website under maintenance, unable to review | R07 | R07 | 0.9 | R07 |
| Products and prices visible only after login | R07 | R07 | 0.9 | R07 |
| Utility bill older than 3 months | R08 | R08 | 0.93 | None |
| Address on rent agreement differs from application | R08 | R08 | 0.85 | R08 |
| Director DOB does not match ID | R09 | R09 | 0.85 | R09 |
| Authorised signatory KYC missing | R09 | R09 | 0.9 | R09 |
| Online betting services not supported | R10 | R10 | 0.95 | R10 |
| Sells prohibited goods | R10 | R10 | 0.93 | R10 |
| Website sells electronics but category chosen is apparel | R11 | R11 | 0.96 | R11 |
| Category mismatch with site content | R11 | R11 | 0.93 | R11 |
| Name matched a watchlist during screening | R12 | R12 | 0.93 | R12 |
| Applicant never completed the form | R13 | R13 | 0.93 | None |
| Please check the details and resubmit | None | R13 | 0.2 | R05 |
| Documents pending | None | R13 | 0.35 | None |

| Nudge | Language | Decision | Guardrails |
|---|---|---|---|
| A3004 | Hindi | Recover | PASS |
| A3007 | Tamil | Recover | PASS |
| A3001 | Marathi | Recover | PASS |
| A3020 | Kannada | Recover | PASS |
| A3015 | English | Recover | PASS |
| A3012 | Hinglish | Recover | PASS |
| A3094 | Tamil | Route to Compliance | PASS |
| A3082 | English | Route to Compliance | PASS |
| A3039 | English | Close | PASS |
| A3093 | Tamil | Close | PASS |
| A3023 | Kannada | Recover | PASS |
| A3005 | English | Recover | PASS |
