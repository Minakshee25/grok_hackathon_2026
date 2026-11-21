"""Turn a decision into the required JSON line, and check the format."""
import re

CODES = {
    "CLOSE": ["COLLECTION_DOCUMENTED", "BUSINESS_RECORDS_RECONCILE", "DOCUMENTED_EVENT", "INVOICE_MATCHES",
              "OWN_ACCOUNT", "GROUP_COMPANY", "THIRD_PARTY_DOCUMENTED", "REFUND_OF_EARLIER_PAYMENT",
              "RETURNING_CUSTOMER", "LIST_MATCH_DIFFERENT_PARTY", "WITHIN_PROFILE"],
    "RFI": ["RFI_PURPOSE_DOCUMENTS", "RFI_BUSINESS_RECORDS", "RFI_SOURCE_OF_FUNDS", "RFI_COUNTERPARTY_DETAILS",
            "RFI_UPDATED_PROFILE"],
    "ESCALATE": ["ESC_MULE_ACTIVITY", "ESC_STRUCTURING", "ESC_UNEXPLAINED_FUNDS", "ESC_PASS_THROUGH",
                 "ESC_INVOICE_MISMATCH", "ESC_HIGH_RISK_CORRIDOR", "ESC_ACCOUNT_MISUSE", "ESC_THIRD_PARTY_FUNDING",
                 "ESC_EARLY_REDEMPTION", "ESC_LISTED_PARTY"],
}
CODE_TO_VERDICT = {c: v for v, cs in CODES.items() for c in cs}

CLOSING = {
    "CLOSE": "Together these documents explain all the activity the alerts raised ({}).",
    "RFI": "Nothing on file is suspicious so far, and this one item from the customer should settle the alerts ({}).",
    "ESCALATE": "This is referred to the MLRO; the customer is not to be asked about it ({}).",
}


def sentences(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z£0-9(])", text)
    return [p.strip() for p in parts if p.strip()]


def build_line(case, verdict, main, extra, known_ids):
    if CODE_TO_VERDICT.get(main.code) != verdict:
        raise ValueError("reason code %s does not belong to %s" % (main.code, verdict))
    sents = sentences(main.reason)
    for f in extra:
        for s in sentences(f.reason)[:1]:
            if len(sents) < 4 and s not in sents:
                sents.append(s)
    if len(sents) < 2:
        sents.append(CLOSING[verdict].format(", ".join(case.rules)))
    reason = " ".join(sents[:4])

    ev, seen = [], set()
    for f in [main] + list(extra):
        for e in f.evidence:
            if e in known_ids and e not in seen:
                ev.append(e)
                seen.add(e)
    return {
        "case_id": case.id,
        "alert_ids": list(case.alert_ids),
        "verdict": verdict,
        "reason_code": main.code,
        "reason": reason,
        "evidence": ev[:12],
    }


def fallback_line(case, why):
    """Used only if the rules crash on a case: escalate, the safe default."""
    return {
        "case_id": case.id,
        "alert_ids": list(case.alert_ids),
        "verdict": "ESCALATE",
        "reason_code": "ESC_UNEXPLAINED_FUNDS",
        "reason": ("The alerts ({}) could not be assessed automatically, so the activity is unexplained. "
                   "It is referred to the MLRO for manual review.").format(", ".join(case.rules) or "none"),
        "evidence": list(case.flagged_ids[:5]),
    }


def check_line(line, case, known_ids):
    """Return a list of format problems (empty when the line is valid)."""
    errs = []
    keys = ["case_id", "alert_ids", "verdict", "reason_code", "reason", "evidence"]
    for k in keys:
        if k not in line:
            errs.append("missing " + k)
    if errs:
        return errs
    if line["case_id"] != case.id:
        errs.append("case_id mismatch")
    if sorted(line["alert_ids"]) != sorted(case.alert_ids):
        errs.append("alert_ids do not match the case")
    if line["verdict"] not in CODES:
        errs.append("bad verdict")
    elif line["reason_code"] not in CODES[line["verdict"]]:
        errs.append("reason_code does not belong to verdict")
    n = len(sentences(line["reason"]))
    if not 2 <= n <= 4:
        errs.append("reason has %d sentences" % n)
    if not isinstance(line["evidence"], list) or not line["evidence"]:
        errs.append("evidence empty")
    else:
        bad = [e for e in line["evidence"] if e not in known_ids]
        if bad:
            errs.append("unknown evidence ids " + ",".join(bad))
    return errs
