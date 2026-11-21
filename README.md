# Suspicious payment alert review

We built a Python system that reviews suspicious payment alerts for a bank, an e-money firm, an investment firm and a payments and FX firm. One verdict covers all alerts in a case. The system can **CLOSE** explained activity, **request information (RFI)** to fill a specific gap, or **ESCALATE** a concern to the institution's money laundering reporting officer.

## How it works

| Stage | What happens |
| --- | --- |
| Assemble the case | The loader reads the institution's policy settings and joins the customer's alerts, transactions, held payment, documents and previous reviews. |
| Decide | Rules check warning signs and internal-list matches first, then look for evidence explaining the flagged payments. |
| Explain | The writer emits one JSON line per case with every alert ID, the verdict, a reason code, a short explanation and the supporting evidence IDs. |

For example, to close a refund alert, the system can trace the incoming money to an earlier payment to the same business and cite the letter confirming the refund amount.

## When the Grok fallback runs

Before deciding a case, the rules try to label each chat, staff or contact note, email and customer letter. **A live Grok call happens only when the rules leave the document unlabelled, the cache has no answer and `OPENROUTER_API_KEY` is set.** The reader calls `x-ai/grok-4.3` by default. A concern label can trigger escalation. If no label is available, the rules continue assessing the other evidence. [Trigger](system/lib/tmrules.py#L122-L138) | [Grok reader](system/lib/tmllm.py#L72-L86)

## Run it

From the repository root with Python 3:

```sh
python system/run institutions/bank review/bank.jsonl
python system/check_output.py institutions/bank review/bank.jsonl
```

The same commands work for `emoney`, `investment` and `payments_fx`. The standard library is sufficient.

## Verification

Five offline runs per institution reproduced all **4,000 submitted predictions** with zero format errors. In the run summary, `llm calls` counts model responses and `fallbacks` counts cases escalated after a processing error. Both were zero. The development cases have no answer key. Three altered cases still closed despite a mismatched dealer payee, traveller payment amount or employer.

[Hackathon brief](HACKATHON_README.md) | [Decision rules](system/lib/tmrules.py) | [Development predictions](output/)
