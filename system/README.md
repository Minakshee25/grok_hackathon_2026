# Alert-closing system

```
system/run <institution folder> <output file>
```

Python 3 standard library only. It reads only the folder it is given and writes only the output file, plus an LLM answer cache in `system/.cache/` when a model is used. It works from any working directory.

## How it decides

Each case goes through the same five steps. The rules work from the policy and the evidence. Nothing refers to a development case, name, amount or document ID.

1. **Collect.** The customer, every alert in the case, the flagged transactions (plus any held payment), the documents on file, earlier reviews and the internal-list entry. The higher-risk countries and the own-account multiple are read from the folder's `POLICY.md`.

2. **Read the free-text documents.** Chats, staff notes, contact notes and emails are tagged with keyword rules: for example asking about thresholds, a remote "payments job", relatives investing through the account, "a loan from a friend", a private sale, or an evasive answer. Routine notes (lost card, address check) are ignored. Only text the rules do not recognise goes to the language model (see below).

3. **Red flags → ESCALATE.** Checked before anything else, because once there is suspicion the customer must not be asked. Each flag has its own reason code:

   | Flag | Code |
   |---|---|
   | Fraud reports from sending banks, or a "payments assistant" job | `ESC_MULE_ACTIVITY` |
   | One device logging in to several customers | `ESC_ACCOUNT_MISUSE` |
   | Customer asking how to stay under a threshold | `ESC_STRUCTURING` |
   | Relatives or friends using the account to invest | `ESC_ACCOUNT_MISUSE` / `ESC_THIRD_PARTY_FUNDING` |
   | Invoices for goods unrelated to the business | `ESC_INVOICE_MISMATCH` |
   | Evasive about routing money to a higher-risk country, or "family support" there with nothing on file | `ESC_HIGH_RISK_CORRIDOR` |
   | A company payer described as a friend's loan, or conflicting stories | `ESC_UNEXPLAINED_FUNDS` |
   | Newly formed companies with a common director paying money that is passed on | `ESC_PASS_THROUGH` |
   | A payment file to payees who share addresses | `ESC_THIRD_PARTY_FUNDING` |

4. **Internal list (TM22).** The list entry is compared with what the documents say about the counterparty:
   - **ESCALATE** (`ESC_LISTED_PARTY`) if the listed person is an officer or owner of the counterparty on the payment date, or a document gives the same date of birth.
   - **CLOSE** (`LIST_MATCH_DIFFERENT_PARTY`) if the evidence shows a different party: a different date of birth, a different unit in a shared building with other officers, or the listed person had left before the payment.
   - **RFI** (`RFI_COUNTERPARTY_DETAILS`) if nothing settles it.

5. **Explain every alert → CLOSE, otherwise RFI.** A document explains a transaction only when its amount, party and reference agree. Examples:
   - a sale receipt for the cash deposit, signed by the customer as seller;
   - a solicitor's, insurer's or settlement letter for the inflow;
   - a refund letter matching an earlier payment;
   - takings records that match each cash deposit;
   - a booking whose traveller list covers every payer;
   - a statement proving the customer holds the other account;
   - a gift letter from the payer;
   - a group structure, factoring agreement or freight invoices;
   - a supplier invoice with the same amount and the same account ending.

   Routine items need no document: salary, regular top-ups, card spending, small personal transfers, the payments-firm client funding its own account, and payments to established suppliers.

   Policy settings are applied as written:
   - Own-account transfers at 5 times the declared income or volume or more go to `RFI_UPDATED_PROFILE`.
   - An account in the customer's name counts only with a statement on file.
   - An earlier review never explains new or larger activity.
   - A dormant account needs re-verification or a documented source.

   If every alert is explained, the case closes with the reason code of the explanation behind the most serious alert. If not, an RFI is sent with the code and the exact item to ask for that fit the unexplained alert: a solicitor's letter, a statement for the account, a booking confirmation, the payment file, an updated expected volume, and so on.

**Safety defaults.**
- A case is never closed on the customer's word alone.
- A gap never becomes a close.
- If anything fails on a case, it is escalated (`ESC_UNEXPLAINED_FUNDS`) rather than left out.

Each line is checked before it is written: alert IDs, verdict and code pairing, two to four sentences, and evidence IDs that exist.

## Reasons and evidence

Reasons are built from templates filled with the case's own facts: names, amounts, dates, references and document IDs. Each states the ground for the verdict and, for an RFI, what to ask for. `evidence` lists the deciding documents, transactions and list entries, using IDs exactly as they appear in the files.

## Language model

- **Where it is used.** Only free-text documents that the keyword rules do not recognise. On the development data that is 0 to 8 documents per 1,000 cases.
- **What it does.** It labels each such document with one of a fixed set of labels (`structuring`, `mule`, `third_party_control`, `evasive`, `high_risk_routing`, `unverified_claim`, `none`). A concern label can escalate a case. A label can never close one.
- **Model.** xAI Grok (`x-ai/grok-4.3` by default) via the OpenRouter chat completions API, reading the key from `OPENROUTER_API_KEY`. Override with `TM_LLM_MODEL`. Check the key and model with `python3 system/llm_check.py`.
- **Settings.** Temperature 0, seed 7, at most 20 output tokens.
- **Caching.** Answers are cached in `system/.cache/llm_cache.json`, so repeat runs reuse them.
- **No key or failed call.** The document is treated as explaining nothing, so it can never cause a close.

## Run time and cost

| | 200 cases |
|---|---|
| Time | Under 5 seconds (rules: about 2 seconds per 1,000 cases) |
| LLM calls | Typically 0 to 2 |
| Cost | Under $0.01 (0 with no key) |

Runs are deterministic. Running the same folder twice gives byte-identical output.

## Files

| File | Purpose |
|---|---|
| `run` | Entry point |
| `lib/tmdata.py` | Loading and document parsing (fields, tables, amounts, dates) |
| `lib/tmrules.py` | Tags, red flags, internal-list check, explainers, RFI asks |
| `lib/tmllm.py` | Optional OpenRouter reader with cache |
| `lib/tmoutput.py` | Builds and validates each output line |
| `llm_check.py` | Sends one made-up test document to check the OpenRouter key and Grok model |
| `check_output.py` | `python3 system/check_output.py <folder> <output file>` checks an output file's format |
