# Stop suspicious payments: an alert-closing system

Banks and payment firms raise thousands of transaction-monitoring alerts, and most turn out to have an ordinary explanation. Every one still needs an analyst. This system reads each alert with the customer's profile, transactions and documents, and decides as a careful analyst would:

- **CLOSE**: the documents explain all the activity.
- **RFI**: nothing is suspicious yet, so ask the customer for one specific document.
- **ESCALATE**: there are signs of crime, so refer it to the MLRO without asking the customer.

Every verdict comes with a reason code, a reason that names the case's own people, amounts and documents, and the IDs of the evidence relied on.

Built for the SpaceXSI Financial Crime Hackathon (track: *Stop suspicious payments*, designed by Diligent). The original brief is in [HACKATHON_README.md](HACKATHON_README.md) and [BRIEF.html](BRIEF.html).

## Run it

Python 3 standard library only. Nothing to install.

```bash
system/run institutions/bank output/bank.jsonl          # one institution
python system/check_output.py institutions/bank output/bank.jsonl   # check the format
```

To let Grok read unusual free-text notes (optional):

```bash
export OPENROUTER_API_KEY=sk-or-...
python system/llm_check.py          # confirms key + model (x-ai/grok-4.3)
```

On Windows, run the commands with `python`, for example `python system/run ...`.

## How it decides

```
 case ─► 1 gather ─► 2 read notes ─► 3 warning signs? ──yes──► ESCALATE
                       (rules, Grok        │ no
                        for unusual        ▼
                        wording)     4 internal-list check ──listed party──► ESCALATE
                                           │
                                           ▼
                                     5 match documents to money
                                      all alerts explained ──► CLOSE  (own-account ≥ 5× income ► RFI)
                                      a gap, no suspicion  ──► RFI   (names what to ask for)
```

1. **Gather.** Alerts, flagged transactions, held payment, customer profile, documents, earlier reviews, internal list. Policy settings (higher-risk countries, the own-account multiple) are read from each institution's `POLICY.md`.
2. **Read free-text notes and chats.** Keyword rules recognise known signals. Text they don't recognise is labelled by **Grok 4.3 via OpenRouter**.
3. **Warning signs come first.** Examples: fraud reports from victims' banks, a remote "payments job", one device logging in to several customers, questions about staying under a threshold, relatives investing through the account, invoices for goods unrelated to the business, evasive answers about higher-risk countries, conflicting stories, and money passed through newly formed companies.
4. **Internal-list matches.**
   - The listed person controls the counterparty, or has the same date of birth: escalate.
   - The evidence shows a different party: close.
   - Nothing settles it: RFI.
5. **Match documents to money.** A document explains a transaction only when the amount, the party and the reference agree. Examples: invoices, refund letters, sale receipts, gift letters, statements proving the customer holds an account, takings records, group structures, freight invoices.

### Design choices

- **It never closes on the customer's word alone.** A wrongful close is the costliest mistake, so a close needs a matching document.
- **Grok can raise a concern but can never close a case.** It reads language. It does not pick verdicts or evidence.
- **Deterministic.** Fixed rules, Grok at temperature 0, and cached answers, so all five runs give byte-identical output.
- **Evidence chosen by code.** Every cited ID exists in the files. Each line is validated before it is written.
- **Fail safe.** If anything goes wrong on a case, it is escalated rather than left out.

## Results on the 4,000 development cases

| Institution | CLOSE | RFI | ESCALATE | Format problems | Time |
|---|---|---|---|---|---|
| Bank | 500 | 250 | 250 | 0 | ~2 to 13 s |
| E-money | 500 | 250 | 250 | 0 | ~2 to 16 s |
| Investment | 500 | 250 | 250 | 0 | ~1 to 11 s |
| Payments and FX | 500 | 250 | 250 | 0 | ~1 to 2 s |

Grok was called 2 times in 4,000 cases. A 200-case run takes seconds and costs well under $0.01, against the limits of 30 minutes and about $4.

Example output line:

```json
{"case_id": "BK-CS100112", "alert_ids": ["BK-AL2450697", "BK-AL2445260"], "verdict": "CLOSE",
 "reason_code": "REFUND_OF_EARLIER_PAYMENT",
 "reason": "The £7,702.22 received from Kelstead Hotel and Gardens on 13 August 2026 is a refund of the £7,702.22 paid to Kelstead Hotel and Gardens on 15 June 2026 (BOOKING 59396). EML-8464 from Kelstead Hotel and Gardens confirms the refund of £7,702.22 after the booking was cancelled. The original payment was reviewed in BK-R975884.",
 "evidence": ["EML-8464", "BK-T721472580", "BK-T675712778", "BK-R975884"]}
```

## Repository layout

| Path | What it holds |
|---|---|
| `system/run` | Entry point: `system/run <institution folder> <output file>` |
| `system/README.md` | How it decides, model, time and cost (required) |
| `system/lib/` | `tmdata.py` loads and parses the data, `tmrules.py` holds the decision rules, `tmllm.py` the Grok reader, `tmoutput.py` builds and validates the output |
| `system/llm_check.py` | Checks the OpenRouter key and Grok model |
| `system/check_output.py` | Validates an output file |
| `output/` | The system's predictions for the four development sets |
| `institutions/` | Development data provided by the organisers (all invented) |
