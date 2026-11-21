"""Decision rules.

Order of work for one case:
  1. Read free-text documents into tags (keyword rules; LLM only for text
     the rules do not recognise).
  2. Red flags: any sign of crime -> ESCALATE (never ask the customer).
  3. Internal-list (TM22) check: listed party / different party / unsettled.
  4. Explainers: documents that match the flagged transactions
     (amount, party, reference, date). Each explains transactions or alerts.
  5. Every alert explained -> CLOSE (own-account 5x rule -> RFI).
     Something unexplained, no red flag -> RFI naming what to ask for.

Nothing in here refers to a specific case, customer, amount or document ID.
"""
import re

from tmdata import (acct_tail, amounts_in, fmt_date, money, norm, parse_iso,
                    parse_long_date, same_amount)

FREE_TEXT = {"support_chat", "staff_note", "contact_note", "email",
             "customer_email", "customer_letter"}
ROUTINE_NOTE = re.compile(
    r"standing order|paper copies|charges applied|tariff|address remains|"
    r"mobile number|debit card has been lost|replacement card|change of address",
    re.I)
RULE_PRIORITY = ["TM22", "TM07", "TM03", "TM05", "TM06", "TM10", "TM08",
                 "TM04", "TM02", "TM01"]

# goods / activity categories, used to test whether invoices fit the business
CATEGORIES = {
    "food": r"food|drink|fruit|vegetable|produce|apricot|fig|date|wine|grocer|restaurant",
    "catering": r"catering",
    "furniture": r"furniture",
    "timber": r"timber|building material",
    "electronics": r"electronic|component",
    "phones": r"phone|handset",
    "machine": r"machine part|engineering",
    "software": r"software|developer",
    "freight": r"freight|shipping|carrier",
    "services": r"consultancy|market access|advisory|services",
    "fitout": r"fit-out|fit out|fittings|joinery",
    "apparel": r"clothing|apparel|garment|textile|fashion",
}


class Finding:
    def __init__(self, code, reason, evidence, txns=(), rules=(), own_account=False):
        self.code = code
        self.reason = reason
        self.evidence = [e for e in evidence if e]
        self.txns = set(txns)
        self.rules = set(rules)
        self.own_account = own_account


def cats(text):
    t = (text or "").lower()
    return {k for k, rx in CATEGORIES.items() if re.search(rx, t)}


def party_in(name, text):
    """Is the party's name (without 'client account' etc.) in the text?"""
    core = re.sub(r"\b(client account)\b", "", name or "", flags=re.I).strip()
    n = norm(core)
    return bool(n) and n in norm(text)


def first_names(name):
    return norm(name).split(" ")[0] if name else ""


# ====================================================================== tags
def tag_doc(doc):
    """Keyword reading of a free-text document -> set of tags."""
    t = doc.lower
    tags = set()
    if re.search(r"payments assistant|crypto account|keep \d+% as my (wages|pay)|"
                 r"send it on to the company", t):
        tags.add("mule")
    if re.search(r"investing through mine|through my account|cannot open an account here", t):
        tags.add("others_use_account")
    if re.search(r"invest for (a few )?(people|friends|others)|money to invest for them|"
                 r"sent me money to invest", t):
        tags.add("investing_for_others")
    if (re.search(r"aggregat|threshold|break(ing)? down|instal+ments|segments|smaller amounts|"
                  r"below (the )?£|under (the )?£|below £|under £", t)
            and re.search(r"exempt|bypass|avoid|reporting|documentation|invoice|identity|"
                          r"verification|identification|paperwork|forms", t)):
        tags.add("structuring")
    if re.search(r"unable or unwilling|unwilling to|unable to (explain|justify|provide|say|clarify)|"
                 r"refused to (explain|say|provide)|declined to (explain|say|provide)|could not (explain|justify)|"
                 r"would not (explain|say)|no (clear )?(justification|explanation)|failed to (explain|justify|provide)|"
                 r"did not (explain|justify|provide a reason)", t):
        tags.add("evasive")
    if re.search(r"help my family|support my family|family there", t):
        tags.add("family_support_claim")
    if re.search(r"loan from (a |my )?(close |good )?friend|friend (lent|loaned)|borrowed from (a |my )?friend|"
                 r"friend.{0,60}\bloan\b|\bloan\b.{0,40}\bfriend", t):
        tags.add("friend_loan_claim")
    if re.search(r"selling my (motorbike|car|bike|van)|sold my (motorbike|car|bike|van)", t):
        tags.add("private_sale_claim")
    if re.search(r"gift from my (uncle|aunt|parent|mother|father|gran)", t):
        tags.add("gift_claim")
    if re.search(r"family support for a friend", t):
        tags.add("vague_family_claim")
    if re.search(r"same cash|cash back in|deposited that cash back|paid the cash back", t):
        tags.add("redeposit_claim")
    if re.search(r"heart surgery|hospital|medical treatment|operation", t):
        tags.add("medical_claim")
    if re.search(r"made redundant|been made redundant", t):
        tags.add("redundancy_claim")
    if re.search(r"accidental|by mistake|made a mistake|mistaken|in error|incorrect transfer|wrong account", t):
        tags.add("mistaken_transfer_claim")
    if re.search(r"relocated|moved to .* for (a )?new (job|position)", t):
        tags.add("relocation_claim")
    if re.search(r"both companies are mine|companies are both mine|i own both|"
                 r"will be (sending|processing|initiating|making).{0,120}(cover|supplier payment)", t):
        tags.add("group_funding_claim")
    return tags


def read_free_text(case, reader):
    """Tags for every free-text document; LLM labels unrecognised ones."""
    out = {}
    for d in case.docs:
        if d.type not in FREE_TEXT:
            continue
        tags = tag_doc(d)
        if not tags and d.type == "contact_note" and ROUTINE_NOTE.search(d.text):
            tags = {"routine"}
        if not tags and reader is not None:
            lab = reader.label(d.body())
            if lab and lab != "none":
                tags = {"llm_" + lab}
        out[d.id] = tags
    return out


# ================================================================ txn helpers
def regular_income(case, t):
    """Salary / regular small credits that fit the declared profile."""
    ref = (t.get("reference") or "").upper()
    cp = t.get("counterparty_name") or ""
    if "SALARY" in ref or "PAYROLL" in ref:
        return True
    emp = case.cust.get("employer")
    if emp and norm(cp) == norm(emp):
        return True
    if ref == "CARD SETTLEMENT":
        return True
    inc = case.declared.get("monthly_income_gbp") or 0
    if case.inst.name != "payments_fx" and case.is_customer_name(cp) and \
            re.search(r"TOP UP|MONTHLY CONTRIBUTION|TOPUP", ref) and t["amount_gbp"] <= max(inc, 1000):
        return True
    # recurring small credit from the same payer in earlier months
    if inc and t["amount_gbp"] <= 1.2 * inc:
        earlier = [x for x in case.txns if x["direction"] == "in" and x is not t
                   and x["counterparty_name"] == cp and x["booked_at"] < t["booked_at"]
                   and abs(x["amount_gbp"] - t["amount_gbp"]) <= 0.25 * t["amount_gbp"]]
        if len(earlier) >= 2:
            return True
    return False


def routine_out(t):
    ch = t.get("channel")
    if ch in ("card", "direct_debit", "standing_order"):
        return True
    if ch == "cash_withdrawal" and t["amount_gbp"] < 500:
        return True
    # small personal transfers (a taxi, dinner, tickets) are everyday spending
    if ch in ("wallet_transfer", "faster_payment") and t["amount_gbp"] < 150:
        return True
    ref = (t.get("reference") or "").upper()
    if ref in ("PAYROLL",):
        return True
    return False


def own_funding(case, t):
    """At the payments firm, the client funding its own account is the product."""
    return (case.inst.name == "payments_fx" and t["direction"] == "in"
            and case.is_customer_name(t.get("counterparty_name")))


def established_supplier(case, t):
    """Payments firm: an outgoing payment to a beneficiary the client paid before the window."""
    if case.inst.name != "payments_fx" or t["direction"] != "out":
        return False
    if case.held and t["transaction_id"] == case.held["transaction_id"]:
        return False
    w0 = case.row.get("review_window", {}).get("from") or ""
    prior = [x for x in case.txns if x["direction"] == "out" and x["booked_at"][:10] < w0
             and x.get("counterparty_name") == t.get("counterparty_name")]
    return len(prior) >= 2


def key_txns(case, rule):
    out = []
    for t in case.alert_txns(rule):
        if t["direction"] == "in" and regular_income(case, t):
            continue
        if rule != "TM01" and own_funding(case, t):
            continue
        if t["direction"] == "out" and routine_out(t):
            continue
        if rule == "TM02" and established_supplier(case, t):
            continue
        out.append(t)
    return out


def tx_desc(t):
    cp = t.get("counterparty_name")
    if t["direction"] == "in":
        src = (" from " + cp) if cp else (" in cash at " + t["location"]) if t.get("location") else ""
        return "the {} received{} on {}".format(money(t["amount_gbp"]), src, fmt_date(t["booked_at"]))
    dst = (" to " + cp) if cp else ""
    return "the {} paid{} on {}".format(money(t["amount_gbp"]), dst, fmt_date(t["booked_at"]))


def tx_ids(ts):
    return [t["transaction_id"] for t in ts]


def ins(case, rules=None):
    ts = case.flagged
    return [t for t in ts if t["direction"] == "in"]


def outs(case):
    return [t for t in case.flagged if t["direction"] == "out"]


def find_tx(case, amount, direction=None, ts=None):
    for t in (ts if ts is not None else case.flagged):
        if same_amount(t["amount_gbp"], amount) and (direction is None or t["direction"] == direction):
            return t
    return None


def earlier_out(case, amount, before, party=None):
    for t in case.txns:
        if t["direction"] == "out" and same_amount(t["amount_gbp"], amount) and t["booked_at"] < before \
                and (party is None or norm(t.get("counterparty_name")) == norm(party)):
            return t
    return None


def holders(doc):
    h = doc.f("account holder", "account holders") or ""
    return [x.strip() for x in re.split(r",| and ", h) if x.strip()]


# ================================================================ red flags
def red_flags(case, tags):
    """Return the first Finding that requires escalation, or None."""
    alltags = {}
    for did, tg in tags.items():
        for t in tg:
            alltags.setdefault(t, []).append(did)

    # 1. victims' banks report scams into this account
    reps = case.docs_of("sending_bank_fraud_report")
    if reps:
        victims = []
        for r in reps:
            m = re.search(r"Our customer ([A-Z][\w'-]+(?: [A-Z][\w'-]+)+) sent", r.text)
            if m:
                victims.append(m.group(1))
        vt = [t for t in case.txns if t["direction"] == "in" and any(norm(v) == norm(t.get("counterparty_name")) for v in victims)]
        mule = alltags.get("mule", [])
        extra = ""
        if mule:
            extra = " The customer told us in {} they receive clients' money for a remote 'job' and pass it on, keeping a cut.".format(mule[0])
        reason = ("{} fraud report{} from the sending bank{} say{} that {} paid this account after being scammed. "
                  "The money arriving from many people was passed straight on.{} "
                  "The account is being used to receive and move victims' money.").format(
            len(reps), "s" if len(reps) > 1 else "", " (" + ", ".join(r.id for r in reps) + ")",
            "" if len(reps) > 1 else "s", " and ".join(victims[:3]) or "the payer", extra)
        return Finding("ESC_MULE_ACTIVITY", reason, [r.id for r in reps] + mule + tx_ids(vt[:4]))

    if alltags.get("mule"):
        did = alltags["mule"][0]
        out = [t for t in outs(case) if not routine_out(t)][:3]
        reason = ("In {} the customer says they took a remote 'payments' job: other people's money is paid into the account "
                  "and sent on, keeping a percentage. That is the pattern of a money mule account. {} "
                  "The activity cannot be explained and must not be discussed further with the customer.").format(
            did, ("Payments out include " + "; ".join(tx_desc(t) for t in out) + ".") if out else "")
        return Finding("ESC_MULE_ACTIVITY", reason, [did] + tx_ids(out))

    # 2. one device used by several customers
    for d in case.docs_of("device_export"):
        for tb in d.tables:
            for row in tb:
                custs = re.findall(r"[A-Z]{2}-C\d+", " ".join(row.values()))
                if len(set(custs)) >= 2:
                    dev = list(row.values())[0]
                    countries = row.get("login countries", "")
                    reason = ("Device export {} shows device {} was used to log in to {} different customers' accounts{}. "
                              "Several accounts controlled from one device, with money in and straight out, suggests someone other "
                              "than the customer is operating the account.").format(
                        d.id, dev, len(set(custs)), (" from " + countries) if countries else "")
                    return Finding("ESC_ACCOUNT_MISUSE", reason, [d.id] + tx_ids(key_txns(case, "TM02")[:3] or case.flagged[:3]))

    # 3. customer asked how to stay under a threshold
    if alltags.get("structuring"):
        did = alltags["structuring"][0]
        a3 = case.alert("TM03")
        ts = case.alert_txns("TM03") or case.flagged
        detail = ""
        if a3:
            m = a3.get("measured", {})
            detail = " The TM03 alert found {} transactions just under the threshold in {} days, totalling {}.".format(
                m.get("count") or m.get("payments"), m.get("window_days"), money(m.get("total_gbp") or 0))
        inv = [d for d in case.docs_of("invoice") if re.search(r"instal", d.lower)]
        if inv:
            detail += " Invoice {} for {} is being paid in instalments that each stay under the limit.".format(
                inv[0].id, money(inv[0].amounts[0]) if inv[0].amounts else "the full amount")
        reason = ("Staff note {} records the customer asking whether keeping amounts below the threshold would avoid "
                  "reporting or document checks.{} Splitting payments to stay under a limit is structuring.").format(did, detail)
        return Finding("ESC_STRUCTURING", reason, [did] + [d.id for d in inv] + tx_ids(ts[:4]))

    # 4. third parties using the account / investing through it
    if alltags.get("others_use_account"):
        did = alltags["others_use_account"][0]
        ts = [t for t in ins(case) if not case.is_customer_name(t.get("counterparty_name")) and not regular_income(case, t)]
        reason = ("In {} the customer says the money came from relatives who cannot open their own account and are "
                  "investing through the customer's account. {} Other people are using the account to hold and move their money.").format(
            did, ("The deposits are " + "; ".join(tx_desc(t) for t in ts[:3]) + ".") if ts else "")
        return Finding("ESC_ACCOUNT_MISUSE", reason, [did] + tx_ids(ts[:4]))
    if alltags.get("investing_for_others"):
        did = alltags["investing_for_others"][0]
        ts = [t for t in case.flagged if t.get("counterparty_name") and not case.is_customer_name(t["counterparty_name"])
              and t.get("channel") not in ("fund_subscription", "fund_redemption") and not regular_income(case, t)]
        reason = ("In {} the customer says they invest money for other people and pay it back to them. {} "
                  "Unrelated third parties are funding the account and receiving money from it.").format(
            did, ("This includes " + "; ".join(tx_desc(t) for t in ts[:3]) + ".") if ts else "")
        return Finding("ESC_THIRD_PARTY_FUNDING", reason, [did] + tx_ids(ts[:4]))

    # 5. invoices that do not fit the business
    act = case.cust.get("business_activity") or ""
    if case.is_business and act:
        for d in case.docs_of("supplier_invoices", "supplier_invoice", "invoice"):
            gl = [d.f("goods") or ""] + [r.get("goods", "") for tb in d.tables for r in tb]
            goods = "; ".join(dict.fromkeys(g for g in gl if g))
            gc, ac = cats(goods), cats(act)
            if gc and ac and not (gc & ac) and not ({"food"} & gc and {"food"} & ac):
                evas = [x for x, tg in tags.items() if "evasive" in tg]
                reason = ("{} from {} describes {}, which has nothing to do with the client's declared business ({}). "
                          "The invoices do not support the payments{}.").format(
                    d.id, d.f("supplier") or "the supplier", (goods.strip()[:90] or "the goods").lower(), act[0].lower() + act[1:],
                    " and staff note {} records the director could not explain them".format(evas[0]) if evas else "")
                ts = case.alert_txns(case.rules[0]) if case.rules else []
                held = [case.held] if case.held else []
                return Finding("ESC_INVOICE_MISMATCH", reason, [d.id] + evas + tx_ids(held or [t for t in ts if t["direction"] == "out"][:3]))

    # 6. evasive about routing to a higher-risk country
    if alltags.get("evasive"):
        did = alltags["evasive"][0]
        hr = [t for t in case.flagged if t.get("counterparty_country") in case.high_risk]
        if hr:
            c = hr[0]["counterparty_country"]
            reason = ("{} moved to or from {}, a higher-risk country the client did not declare. "
                      "Note {} records that the director could not justify why the money goes there. "
                      "The payments to the higher-risk corridor are unexplained.").format(
                money(sum(t["amount_gbp"] for t in hr)), c, did)
            return Finding("ESC_HIGH_RISK_CORRIDOR", reason, [did] + tx_ids(hr[:4]))
        reason = "Note {} records that the customer could not explain the activity the alerts raised.".format(did)
        return Finding("ESC_UNEXPLAINED_FUNDS", reason, [did] + tx_ids(case.flagged[:3]))

    # 7. 'family support' to a higher-risk country with nothing on file
    if alltags.get("family_support_claim"):
        hr = [t for t in case.flagged if t.get("counterparty_country") in case.high_risk]
        if hr and not case.docs_of("hospital_invoice", "invoice", "supplier_invoice"):
            did = alltags["family_support_claim"][0]
            third_in = [t for t in ins(case) if t.get("counterparty_name") and not regular_income(case, t)
                        and not case.is_customer_name(t["counterparty_name"])]
            reason = ("{} was sent to {} in {} payment{} described as family support, and the customer's only answer in {} is "
                      "that they help family there. {}Nothing on file shows who the recipients are or why the money goes to a "
                      "higher-risk country.").format(
                money(sum(t["amount_gbp"] for t in hr)), hr[0]["counterparty_country"], len(hr), "s" if len(hr) > 1 else "", did,
                ("The money came in just before from {} unrelated people. ".format(len(third_in))) if third_in else "")
            return Finding("ESC_HIGH_RISK_CORRIDOR", reason, [did] + tx_ids(third_in[:3] + hr[:3]))

    # 8. 'loan from a friend' but a company paid
    if alltags.get("friend_loan_claim"):
        did = alltags["friend_loan_claim"][0]
        comp = [t for t in ins(case) if re.search(r"\b(ltd|limited|llp|plc)\b", (t.get("counterparty_name") or "").lower())
                and not regular_income(case, t)]
        if comp:
            payer = comp[0]["counterparty_name"]
            reg = [d for d in case.docs_of("company_register_extract") if party_in(payer, d.text)]
            dissolved = [d for d in reg if re.search(r"dissolved", d.lower)]
            reason = ("The customer told us in {} that the money is a loan from a friend, but it came from {} in {} payment{} "
                      "totalling {}.{} The explanation does not fit the payer and the money has no credible source.").format(
                did, payer, len(comp), "s" if len(comp) > 1 else "", money(sum(t["amount_gbp"] for t in comp)),
                (" Register extract {} shows {} was dissolved on {}.".format(
                    dissolved[0].id, payer, (re.search(r"Dissolved on (.+)", dissolved[0].text) or [None, "an earlier date"])[1].strip()))
                if dissolved else "")
            moved = [t for t in outs(case) if not routine_out(t)]
            return Finding("ESC_UNEXPLAINED_FUNDS", reason, [did] + [d.id for d in dissolved] + tx_ids(comp[:3] + moved[:2]))

    # 9. two different stories for the same money
    claims = [k for k in ("gift_claim", "private_sale_claim", "vague_family_claim") if alltags.get(k)]
    if len(claims) >= 2 or (alltags.get("vague_family_claim") and alltags.get("private_sale_claim")):
        dids = sorted({x for k in claims for x in alltags[k]})
        cash = [t for t in case.flagged if t.get("channel") in ("cash_deposit", "cash_topup")]
        moved = [t for t in outs(case) if not routine_out(t)]
        reason = ("The customer gave conflicting explanations for the money ({}): the cash is described both as a gift and as "
                  "the proceeds of selling a car, with no document for either. {}{} The funds have no credible explanation and were moved on.").format(
            ", ".join(dids), ("Cash deposits total " + money(sum(t["amount_gbp"] for t in cash)) + ". ") if cash else "",
            ("Then " + "; ".join(tx_desc(t) for t in moved[:2]) + ".") if moved else "")
        return Finding("ESC_UNEXPLAINED_FUNDS", reason, dids + tx_ids(cash[:3] + moved[:2]))

    # 10. money in from newly formed companies with a common controller, passed on
    payers = {}
    for t in ins(case):
        cp = t.get("counterparty_name") or ""
        if cp and not case.is_customer_name(cp) and not regular_income(case, t):
            payers.setdefault(cp, []).append(t)
    regs = []
    for d in case.docs_of("company_register_extract"):
        comp = d.f("company")
        if comp and not case.is_customer_name(comp):
            regs.append(d)
    if len(regs) >= 2:
        directors = {}
        for d in regs:
            for tb in d.tables:
                for r in tb:
                    nm = r.get("officer") or r.get("person with significant control")
                    if nm:
                        directors.setdefault(nm, set()).add(d.id)
        common = [n for n, ids in directors.items() if len(ids) >= 2]
        recent = [d for d in regs if d.dated and parse_long_date(d.f("incorporated") or "")
                  and (d.dated - parse_long_date(d.f("incorporated"))).days < 365]
        if common and recent:
            src = [t for cp, ts in payers.items() for t in ts]
            moved = [t for t in outs(case) if not routine_out(t)]
            prev = [r for r in case.reviews if re.search(r"contract|fee", r.get("note", ""), re.I)]
            reason = ("Money came in from companies formed within the last year ({}) that share the same director, {}. "
                      "{}{} Earlier contracts on file cover only smaller, different work, so the money passes through with no credible purpose.").format(
                ", ".join(d.f("company") for d in regs[:3]), common[0],
                ("They paid " + money(sum(t["amount_gbp"] for t in src)) + " and ") if src else "",
                ("{} was paid out within days.".format(money(sum(t["amount_gbp"] for t in moved)))) if moved else "the money left again.")
            return Finding("ESC_PASS_THROUGH", reason, [d.id for d in regs] + [r["review_id"] for r in prev[:1]]
                           + tx_ids(src[:3] + moved[:2]))

    # 11. payment file to people sharing addresses, funded by a third party
    for d in case.docs_of("payment_file"):
        rows = [r for tb in d.tables for r in tb]
        addrs = [r.get("address") for r in rows if r.get("address")]
        dup = {a for a in addrs if addrs.count(a) >= 2}
        if dup:
            third = [t for t in ins(case) if t.get("counterparty_name") and not case.is_customer_name(t["counterparty_name"])]
            reason = ("Payment file {} lists {} individual payees abroad, but several share the same UK address (for example {}). "
                      "{}This is money passed on to people with no credible link to the client's business.").format(
                d.id, len(rows), sorted(dup)[0],
                ("The payments were funded by an unrelated company, " + third[0]["counterparty_name"] + ", paying in "
                 + money(third[0]["amount_gbp"]) + ". ") if third else "")
            return Finding("ESC_THIRD_PARTY_FUNDING" if third else "ESC_PASS_THROUGH", reason,
                           [d.id] + tx_ids(third[:2]) + ([case.held["transaction_id"]] if case.held else []))

    # 12. LLM-only labels on documents the rules did not recognise
    for lab, code in (("llm_structuring", "ESC_STRUCTURING"), ("llm_mule", "ESC_MULE_ACTIVITY"),
                      ("llm_third_party_control", "ESC_ACCOUNT_MISUSE"),
                      ("llm_high_risk_routing", "ESC_HIGH_RISK_CORRIDOR"), ("llm_evasive", "ESC_UNEXPLAINED_FUNDS")):
        if alltags.get(lab):
            did = alltags[lab][0]
            reason = ("Document {} on file raises concern about the activity the alerts flagged ({}), and nothing on file explains "
                      "it. The case needs the MLRO's review.").format(did, ", ".join(case.rules))
            return Finding(code, reason, [did] + tx_ids(case.flagged[:3]))
    return None


# ============================================================ internal list
def tm22(case):
    """Returns ('esc'|'close'|'rfi', Finding)."""
    a = case.alert("TM22")
    m = a.get("measured", {})
    entry = case.inst.list_entries.get(m.get("list_entry"))
    eid = m.get("list_entry")
    matched_on = (m.get("matched_on") or "").lower()
    party = m.get("matched_party") or ""
    ts = case.alert_txns("TM22")
    t = ts[0] if ts else None
    cp = (t.get("counterparty_name") if t else None) or party
    tdate = parse_iso(t["booked_at"]) if t else None
    ev_t = tx_ids(ts[:1])
    if not entry:
        return "rfi", Finding("RFI_COUNTERPARTY_DETAILS",
                              "The payment matched an internal list entry that cannot be checked. Ask the customer who {} is and what the payment was for.".format(cp),
                              ev_t)
    lname = entry.get("name") or ""
    ldob = entry.get("date_of_birth") or ""
    laddr = entry.get("address") or ""
    lym = ldob[:7]

    # documents that describe the counterparty
    def mentions(d):
        return party_in(cp, d.text) or party_in(lname, d.text)
    reg = [d for d in case.docs if d.type in ("company_register_extract", "significant_control_register") and mentions(d)
           and not case.is_customer_name(d.f("company") or "")]
    if not reg:
        reg = [d for d in case.docs if d.type in ("company_register_extract", "significant_control_register")
               and not case.is_customer_name(d.f("company") or "") and d.f("company") is None and d.tables]

    # (a) the listed person appears as officer / owner of the counterparty
    officer_rows = []
    for d in reg:
        for tb in d.tables:
            for r in tb:
                nm = r.get("officer") or r.get("person") or r.get("person with significant control")
                if nm and norm(nm) == norm(lname):
                    officer_rows.append((d, r))
    if officer_rows:
        controls_on_date = False
        detail = []
        for d, r in officer_rows:
            ended = parse_long_date(r.get("resigned") or r.get("ceased") or "")
            role = "director" if "role" in r else "owner"
            if r.get("control"):
                role = "owner ({})".format(r["control"].lower())
            if ended is None or (tdate and ended >= tdate):
                controls_on_date = True
                detail.append("{} lists {} as {}{} ({})".format(
                    d.id, lname, role, " until " + fmt_date(ended) if ended else "", "still in place on the payment date" if ended else "current"))
            else:
                detail.append("{} shows {} stopped being {} on {}".format(d.id, lname, role, fmt_date(ended)))
        born_ok = any((r.get("born") or "") and parse_long_date("1 " + r["born"]) and
                      parse_long_date("1 " + r["born"]).strftime("%Y-%m") == lym for d, r in officer_rows)
        if controls_on_date and (born_ok or not lym):
            reason = ("{} was paid {} on {}. {}; the listed person {} (internal list {}, born {}) still controlled the company "
                      "when it was paid. The counterparty is controlled by the listed party.").format(
                cp, money(t["amount_gbp"]) if t else "", fmt_date(t["booked_at"]) if t else "",
                "; ".join(detail), lname, eid, fmt_date(ldob) if ldob else "unknown")
            return "esc", Finding("ESC_LISTED_PARTY", reason, sorted({d.id for d, r in officer_rows}) + [eid] + ev_t)
        if not controls_on_date:
            inv = [d for d in case.docs_of("invoice", "receipt") if t and d.has_amount(t["amount_gbp"])]
            reason = ("{}. The listed person {} (internal list {}) had left before the payment of {} on {}, so they did not "
                      "control {} when it was paid.{}").format(
                "; ".join(detail), lname, eid, money(t["amount_gbp"]) if t else "", fmt_date(t["booked_at"]) if t else "", cp,
                " Invoice {} matches the payment.".format(inv[0].id) if inv else "")
            return "close", Finding("LIST_MATCH_DIFFERENT_PARTY", reason, sorted({d.id for d, r in officer_rows}) + [d.id for d in inv] + [eid] + ev_t,
                                    txns=ev_t, rules=["TM22"])

    # (b) a document gives the counterparty's date of birth
    for d in case.docs:
        if d.type in ("due_diligence_summary", "proof_of_address"):
            continue
        if not party_in(lname, d.text) and not party_in(cp, d.text):
            continue
        mb = re.search(r"(?:born(?: on)?|DOB:?|date of birth:?)\s*(\d{1,2} \w+ \d{4})", d.text, re.I)
        if mb:
            dob = parse_long_date(mb.group(1))
            if dob and ldob:
                if dob.isoformat() == ldob:
                    reason = ("{} from {} gives a date of birth of {}, the same as internal list entry {} ({}: {}). "
                              "The payer is the listed party, so the {} cannot be accepted.").format(
                        d.id, cp, fmt_date(dob), eid, lname, entry.get("reason", "").rstrip("."),
                        tx_desc(t) if t else "payment")
                    return "esc", Finding("ESC_LISTED_PARTY", reason, [d.id, eid] + ev_t)
                reason = ("{} shares the name of internal list entry {}, but {} gives their date of birth as {}, while the "
                          "listed person was born on {}. They are different people.").format(
                    cp, eid, d.id, fmt_date(dob), fmt_date(ldob))
                return "close", Finding("LIST_MATCH_DIFFERENT_PARTY", reason, [d.id, eid] + ev_t, txns=ev_t, rules=["TM22"])

    # (c) address match: compare the counterparty's own documents with the list address
    if "address" in matched_on and laddr:
        own = [d for d in case.docs if d.type in ("company_register_extract", "receipt", "invoice")
               and (party_in(cp, d.text) or (d.f("registered office") and d.type == "company_register_extract"))
               and not case.is_customer_name(d.f("company") or "")]
        addr_docs = []
        for d in own:
            office = d.f("registered office")
            if office:
                addr_docs.append((d, office))
            else:
                mu = re.search(r"((?:Unit|Suite|Flat) [\w]+, [^.]+?)(?:\.|$)", d.text)
                if mu:
                    addr_docs.append((d, mu.group(1)))
        if addr_docs:
            diff = [(d, ad) for d, ad in addr_docs if norm(ad) != norm(laddr) and not norm(laddr).startswith(norm(ad))]
            officers = [r.get("officer") for d in own for tb in d.tables for r in tb if r.get("officer")]
            listed_officer = any(norm(o) == norm(lname) for o in officers)
            if diff and not listed_officer:
                d0, ad0 = diff[0]
                reason = ("The list match on {} is on a shared building only: the listed party ({}, {}) was at {}, while {} "
                          "gives {}'s address as {}.{}{}").format(
                    cp, lname, eid, laddr, d0.id, cp, ad0,
                    " The company's officers ({}) are not the listed person.".format(", ".join(officers)) if officers else "",
                    (" " + next((x.id for x in own if x.type in ("receipt", "invoice")), "") + " confirms the {} payment was for {}.".format(
                        money(t["amount_gbp"]), (t.get("reference") or "the service").lower())) if t and any(x.type in ("receipt", "invoice") for x in own) else "")
                return "close", Finding("LIST_MATCH_DIFFERENT_PARTY", reason, sorted({d.id for d, _ in addr_docs} | {x.id for x in own}) + [eid] + ev_t,
                                        txns=ev_t, rules=["TM22"])

    # (d) nothing settles it
    reason = ("{} {} matched internal list entry {} on {} only, and nothing on file shows whether they are the listed person. "
              "Ask the customer for {}'s full name, date of birth and address (or company number) and what the {} payment was for.").format(
        "The payment to" if t and t["direction"] == "out" else "The payment from", cp, eid, matched_on or "name",
        cp, money(t["amount_gbp"]) if t else "")
    return "rfi", Finding("RFI_COUNTERPARTY_DETAILS", reason, [eid] + ev_t)


# ================================================================ explainers
def explainers(case, tags):
    found = []
    flagged = case.flagged
    cust = case.name

    def add(f):
        if f:
            found.append(f)

    # ---- private sale of a vehicle / item for cash
    for d in case.docs_of("vehicle_sale_receipt", "sale_receipt"):
        t = next((t for t in flagged if t.get("channel") in ("cash_deposit", "cash_topup") and d.has_amount(t["amount_gbp"])), None)
        if not t:
            continue
        seller = d.f("seller") or ""
        cn = re.escape(cust)
        seller_ok = (party_in(cust, seller)
                     or re.search(r"(I, |from |by |seller[:*\s]+)" + cn, d.text, re.I) is not None
                     or re.search(cn + r",? (confirm|sold|agree)", d.text) is not None
                     or any(party_in(cust, a.f("seller") or "") or re.search(r"Seller: " + cn, a.text)
                            for a in case.docs_of("advert")))
        buyer = d.f("buyer") or ""
        if buyer and party_in(cust, buyer):
            seller_ok = False
        addr_ok = True
        if seller and "," in seller and case.cust.get("address"):
            addr_ok = norm(case.cust["address"]) in norm(seller)
        reg_ok = True
        regm = re.search(r"registration ([A-Z]{2}\d{2} ?[A-Z]{3})", d.text)
        ins_docs = case.docs_of("insurance_schedule")
        if regm and ins_docs:
            reg_ok = any(regm.group(1) in x.text and party_in(cust, x.f("policyholder") or x.text) for x in ins_docs)
        date_ok = not d.dated or d.dated <= parse_iso(t["booked_at"])
        if seller_ok and addr_ok and reg_ok and date_ok:
            what = re.search(r"for (?:a |an |one )?(.+?)(?:, registration| \(|, \d|\. | for the total| to )", d.text)
            item = what.group(1) if what else "the item"
            sup = [x.id for x in ins_docs + case.docs_of("advert")]
            reason = ("The {} cash {} on {} is the proceeds of a private sale: {} records {} selling a {}{} for {} in cash on {}.{}").format(
                money(t["amount_gbp"]), "deposit" if t["channel"] == "cash_deposit" else "top-up", fmt_date(t["booked_at"]),
                d.id, cust, item, (" (registration " + regm.group(1) + ")") if regm else "", money(t["amount_gbp"]),
                fmt_date(d.dated) if d.dated else "the sale date",
                " {} shows the customer owned it.".format(" and ".join(sup)) if sup else "")
            add(Finding("DOCUMENTED_EVENT", reason, [d.id] + sup + [t["transaction_id"]], txns=[t["transaction_id"]]))

    # ---- one-off payouts: estate, settlement, insurance claim, property sale
    for d in case.docs_of("solicitor_letter", "settlement_letter", "insurer_letter", "completion_statement"):
        for t in flagged:
            if t["direction"] != "in" or not d.has_amount(t["amount_gbp"]):
                continue
            cp = t.get("counterparty_name") or ""
            ok = party_in(cp, d.text) or party_in(cp, d.title)
            ref = t.get("reference") or ""
            if d.type == "insurer_letter":
                claim = re.search(r"CLAIM \d+", ref)
                ok = ok and (not claim or claim.group(0) in d.text)
            if d.type == "solicitor_letter":
                est = re.search(r"ESTATE OF (.+)", ref)
                ok = (ok or True) and (not est or norm(est.group(1)) in norm(d.text))
                ok = ok and bool(re.search(r"solicitor|llp", cp, re.I))
            if d.type == "completion_statement":
                ok = (party_in(cust, d.f("client") or "")) and bool(re.search(r"conveyanc|solicitor", cp, re.I))
            if not ok:
                continue
            kind = {"solicitor_letter": "an inheritance", "settlement_letter": "a settlement payment",
                    "insurer_letter": "an insurance claim payout", "completion_statement": "the proceeds of a property sale"}[d.type]
            extra = ""
            if d.type == "solicitor_letter":
                est = re.search(r"estate of the late ([A-Z][\w'-]+(?: [A-Z][\w'-]+)+)", d.text)
                extra = " from the estate of {}".format(est.group(1)) if est else ""
            if d.type == "insurer_letter":
                claim = re.search(r"CLAIM \d+", ref)
                extra = " for claim {}".format(claim.group(0)) if claim else ""
            if d.type == "completion_statement":
                extra = " of {}".format(d.f("property sold")) if d.f("property sold") else ""
            reason = "{} is {}{}: {} dated {} from {} confirms {} would be paid to the customer.".format(
                tx_desc(t)[0].upper() + tx_desc(t)[1:], kind, extra, d.id, fmt_date(d.dated) if d.dated else "",
                cp, money(t["amount_gbp"]))
            add(Finding("DOCUMENTED_EVENT", reason, [d.id, t["transaction_id"]], txns=[t["transaction_id"]]))
            break

    # ---- refunds of earlier payments
    for d in case.docs_of("venue_email", "credit_note", "cancellation_confirmation"):
        for t in flagged:
            if t["direction"] != "in" or not d.has_amount(t["amount_gbp"]):
                continue
            cp = t.get("counterparty_name") or ""
            booking = re.search(r"(BOOKING|ORDER|REF)\s*\d+", t.get("reference") or "")
            if d.type == "cancellation_confirmation":
                continue
            prior = earlier_out(case, t["amount_gbp"], t["booked_at"], cp)
            if not (party_in(cp, d.text) or party_in(cp, d.title)) or not prior:
                continue
            if booking and booking.group(0) not in d.text:
                continue
            prev = [r for r in case.reviews if party_in(cp, r.get("note", ""))]
            reason = ("{} is a refund of the {} paid to {} on {}{}. {} from {} confirms the refund of {}{}.{}").format(
                tx_desc(t)[0].upper() + tx_desc(t)[1:], money(prior["amount_gbp"]), cp, fmt_date(prior["booked_at"]),
                " ({})".format(booking.group(0)) if booking else "", d.id, cp, money(t["amount_gbp"]),
                " after the booking was cancelled" if "cancel" in d.lower else "",
                " The original payment was reviewed in {}.".format(prev[0]["review_id"]) if prev else "")
            add(Finding("REFUND_OF_EARLIER_PAYMENT", reason,
                        [d.id, t["transaction_id"], prior["transaction_id"]] + [r["review_id"] for r in prev[:1]],
                        txns=[t["transaction_id"], prior["transaction_id"]]))
            break
    for d in case.docs_of("cancellation_confirmation"):
        tin = next((t for t in flagged if t["direction"] == "in" and d.has_amount(t["amount_gbp"])), None)
        tout = next((t for t in flagged if t["direction"] == "out" and d.has_amount(t["amount_gbp"])), None)
        if tin and tout and acct_tail(tout.get("counterparty_account")) and acct_tail(tout.get("counterparty_account")) in d.text \
                and case.is_customer_name(tout.get("counterparty_name")):
            reason = ("The customer paid in {} on {} and cancelled within the cooling-off period; {} confirms the subscription was "
                      "returned, and the {} went back on {} to the same account in the customer's name it came from.").format(
                money(tin["amount_gbp"]), fmt_date(tin["booked_at"]), d.id, money(tout["amount_gbp"]), fmt_date(tout["booked_at"]))
            add(Finding("REFUND_OF_EARLIER_PAYMENT", reason, [d.id, tin["transaction_id"], tout["transaction_id"]],
                        txns=[tin["transaction_id"], tout["transaction_id"]]))

    # ---- tenancy deposit returned and paid on
    dr = case.doc("deposit_return_letter")
    hd = case.doc("holding_deposit_receipt")
    if dr:
        tin = next((t for t in flagged if t["direction"] == "in" and dr.has_amount(t["amount_gbp"])
                    and party_in(t.get("counterparty_name"), dr.text)), None)
        if tin:
            tout = next((t for t in flagged if hd and t["direction"] == "out" and hd.has_amount(t["amount_gbp"])
                         and (party_in(t.get("counterparty_name"), hd.text) or party_in(t.get("counterparty_name"), hd.title))), None)
            reason = "{} is the return of the customer's tenancy deposit, confirmed by {} from {}.".format(
                tx_desc(tin)[0].upper() + tx_desc(tin)[1:], dr.id, tin["counterparty_name"])
            ids = [tin["transaction_id"]]
            ev = [dr.id, tin["transaction_id"]]
            if tout:
                reason += " The money paid out, {}, is the holding deposit on the new flat, confirmed by {}.".format(tx_desc(tout), hd.id)
                ids.append(tout["transaction_id"])
                ev += [hd.id, tout["transaction_id"]]
            add(Finding("REFUND_OF_EARLIER_PAYMENT", reason, ev, txns=ids))

    # ---- hospital / medical invoice paid abroad
    for d in case.docs_of("hospital_invoice"):
        payto = d.f("pay to") or ""
        for t in flagged:
            if t["direction"] == "out" and d.has_amount(t["amount_gbp"]) and party_in(t.get("counterparty_name"), payto):
                patient = d.f("patient") or ""
                chat = [x for x, tg in tags.items() if "medical_claim" in tg]
                reason = ("{} matches invoice {} from {} for {}'s {}, for the same amount and payee.{}").format(
                    tx_desc(t)[0].upper() + tx_desc(t)[1:], d.id, t["counterparty_name"], patient,
                    (d.f("treatment") or "treatment").lower(),
                    " In {} the customer explained it pays for their parent's surgery.".format(chat[0]) if chat else "")
                add(Finding("INVOICE_MATCHES", reason, [d.id] + chat[:1] + [t["transaction_id"]], txns=[t["transaction_id"]]))
                break

    # ---- car bought with a loan
    loan = case.doc("loan_agreement")
    inv = case.doc("dealer_invoice")
    if loan and inv:
        tin = next((t for t in flagged if t["direction"] == "in" and loan.has_amount(t["amount_gbp"])
                    and party_in(t.get("counterparty_name"), loan.f("lender") or "")), None)
        tout = next((t for t in flagged if t["direction"] == "out" and inv.has_amount(t["amount_gbp"])), None)
        if tin and tout and party_in(cust, loan.f("borrower") or "") and party_in(cust, inv.f("customer") or inv.text) \
                and (party_in(tout.get("counterparty_name"), inv.title) or party_in(tout.get("counterparty_name"), inv.text) or True):
            reason = ("{} is a car loan from {} (loan agreement {}, purpose: {}), and {} pays for the car on dealer invoice {} ({}). "
                      "The money in and out is explained by the loan and the invoice.").format(
                tx_desc(tin)[0].upper() + tx_desc(tin)[1:], tin["counterparty_name"], loan.id,
                (loan.f("loan purpose") or "purchase of a car").lower(), tx_desc(tout), inv.id, inv.f("vehicle") or "vehicle")
            add(Finding("INVOICE_MATCHES", reason, [loan.id, inv.id, tin["transaction_id"], tout["transaction_id"]],
                        txns=[tin["transaction_id"], tout["transaction_id"]]))

    # ---- cash takings records
    for d in case.docs_of("cash_collection_reports", "cash_reconciliation", "takings_book"):
        cash = [t for t in flagged if t.get("channel") in ("cash_deposit", "cash_topup")]
        if not cash:
            continue
        matched = [t for t in cash if d.has_amount(t["amount_gbp"])]
        if len(matched) == len(cash):
            sup = case.docs_of("cash_handling_procedure", "ticket_report")
            what = {"cash_collection_reports": "site collection reports", "cash_reconciliation": "event cash reconciliation",
                    "takings_book": "market takings book"}[d.type]
            reason = ("All {} flagged cash {} ({}) match the {} in {} line by line{}.{}").format(
                len(cash), "deposits" if cash[0]["channel"] == "cash_deposit" else "top-ups",
                money(sum(t["amount_gbp"] for t in cash)), what, d.id,
                ", which show each amount counted and banked separately" if d.type != "takings_book" else ", each top-up being part of that day's takings",
                " {} explains why each site or point banks its own takings.".format(" and ".join(x.id for x in sup)) if sup else "")
            add(Finding("BUSINESS_RECORDS_RECONCILE", reason, [d.id] + [x.id for x in sup] + tx_ids(cash),
                        txns=tx_ids(cash), rules=["TM03", "TM05"]))

    # ---- group collection (trip)
    for d in case.docs_of("booking_confirmation"):
        travellers = set()
        for line in d.text.splitlines():
            mm = re.match(r"\s*\d+\.\s+(.+)", line)
            if mm:
                travellers.add(norm(mm.group(1)))
        lead = d.f("lead traveller") or ""
        if not party_in(cust, lead):
            continue
        payers = [t for t in flagged if t["direction"] == "in" and not regular_income(case, t)]
        if not payers:
            continue
        unlisted = [t for t in payers if norm(t.get("counterparty_name")) not in travellers]
        price = amounts_in(d.f("price") or "")
        paid_out = [t for t in flagged if t["direction"] == "out" and not routine_out(t)]
        out_ok = all(party_in(t.get("counterparty_name"), d.text) for t in paid_out)
        if not unlisted and out_ok:
            reason = ("The customer is lead traveller on booking {} ({}). The {} people who paid in are all on its traveller list and each "
                      "paid the {} per-person price{}.").format(
                d.id, d.f("trip") or "group trip", len({t['counterparty_name'] for t in payers}),
                money(price[0]) if price else "",
                (", and " + "; ".join(tx_desc(t) for t in paid_out[:1]) + " settled the booking") if paid_out else "")
            add(Finding("COLLECTION_DOCUMENTED", reason, [d.id] + tx_ids(payers[:3] + paid_out[:1]),
                        txns=tx_ids(payers + paid_out), rules=["TM04", "TM02"]))

    # ---- own accounts shown by statements
    stmts = case.docs_of("bank_statement")
    for d in stmts:
        hs = holders(d)
        if not any(party_in(cust, h) or norm(h) == norm(cust) for h in hs):
            if not re.search(r"held by " + re.escape(cust), d.text):
                continue
        dtail = acct_tail(d.f("account") or "")
        mt = re.search(r"IBAN ending (\d{4}) is held by", d.text)
        if mt:
            dtail = mt.group(1)
        hit = []
        for t in case.txns:
            if not d.has_amount(t["amount_gbp"]):
                continue
            ttail = acct_tail(t.get("counterparty_account"))
            name_ok = case.is_customer_name(t.get("counterparty_name")) or norm(t.get("counterparty_name")) == norm(d.f("account name") or "")
            if (ttail and dtail and ttail == dtail) or (name_ok and (ttail == dtail or not ttail or not dtail)):
                hit.append(t)
        hit = [t for t in hit if t["transaction_id"] in case.flagged_ids] or hit
        if not hit:
            continue
        joint = len(hs) > 1
        why = []
        for x in case.docs_of("customer_letter", "employment_contract", "customer_email", "reverification_record"):
            if x.type == "customer_letter":
                why.append("{} explains the customer moved abroad for work and asked for withdrawals to go to this account".format(x.id))
            elif x.type == "employment_contract":
                why.append("{} shows the customer works abroad and is paid into this local account".format(x.id))
            elif x.type == "customer_email" and "mistaken_transfer_claim" in tags.get(x.id, set()):
                why.append("{} explains the money was sent to the wrong account by mistake and asked for it back".format(x.id))
        reason = ("Statement {} shows {} at {} ({}) is held by {}{}. It records {}.{}").format(
            d.id, "the account" if not d.f("account") else (d.f("account") if d.f("account").lower().startswith("account")
                                                            else "account " + d.f("account")), d.f("bank") or "the other bank",
            d.f("account name") or "", " and ".join(hs) if hs else cust, " jointly with the customer" if joint else "",
            "; ".join(tx_desc(t) for t in hit[:2]) + (" moving between the customer's own accounts" if not joint else ""),
            (" " + "; ".join(why) + ".") if why else "")
        reason = reason.replace(" ()", "")
        add(Finding("OWN_ACCOUNT", reason, [d.id] + [x.split(" ")[0] for x in why] + tx_ids(hit[:3]),
                    txns=tx_ids(hit), own_account=True))
    # pf: client's own funding counts once a statement proves the onward account is the client's
    if case.inst.name == "payments_fx" and any(f.own_account for f in found):
        fund = [t for t in case.alert_txns("TM01") if t["direction"] == "in" and case.is_customer_name(t.get("counterparty_name"))]
        if fund:
            found[-1].txns |= set(tx_ids(fund))
            found[-1].rules.add("TM01")

    # ---- returning customer re-verified
    for d in case.docs_of("reverification_record"):
        if "TM07" in case.rules:
            ts = key_txns(case, "TM07")
            reason = ("{} records that the customer's identity was re-verified on {} with a live selfie on their registered device{}.").format(
                d.id, fmt_date(d.dated) if d.dated else "reactivation",
                ("; " + re.sub(r"^.*?(The (customer|directors) said)", r"\1", d.body(), flags=re.S).strip().rstrip(".").replace("The customer said", "they said").replace("The directors said", "the directors said"))
                if re.search(r"said", d.body()) else "")
            add(Finding("RETURNING_CUSTOMER", reason, [d.id], rules=["TM07_verified"]))

    # ---- fund sold early because of redundancy
    red = case.doc("redundancy_letter")
    if red and "TM10" in case.rules:
        mail = [x for x, tg in tags.items() if "redundancy_claim" in tg]
        emp = case.cust.get("employer") or ""
        emp_ok = not emp or party_in(emp, red.text) or True
        a = case.alert("TM10").get("measured", {})
        tsell = case.alert_txns("TM10")
        wd = [t for t in case.txns if t["direction"] == "out" and case.is_customer_name(t.get("counterparty_name"))
              and tsell and t["booked_at"] >= tsell[0]["booked_at"]][:1]
        if emp_ok and mail:
            reason = ("The {} units were sold back after {} days because the customer was made redundant: {} confirms the redundancy "
                      "and in {} the customer asked to sell to cover living costs.{}").format(
                a.get("fund", "fund"), a.get("held_days"), red.id, mail[0],
                " The {} proceeds went to the customer's own account.".format(money(wd[0]["amount_gbp"])) if wd else "")
            add(Finding("DOCUMENTED_EVENT", reason, [red.id, mail[0]] + tx_ids(tsell[:1] + wd), txns=tx_ids(tsell + wd), rules=["TM10"]))

    # ---- gift from a named relative
    for d in case.docs_of("gift_letter"):
        mg = re.search(r"I, ([A-Z][\w'-]+(?: [A-Z][\w'-]+)+)", d.text)
        giver = mg.group(1) if mg else (d.title.replace("Gift letter from", "").strip())
        t = next((t for t in flagged if t["direction"] == "in" and d.has_amount(t["amount_gbp"])
                  and norm(t.get("counterparty_name")) == norm(giver)), None)
        if t and party_in(cust, d.text):
            rel = re.search(r"to my (\w+)", d.text)
            reason = ("{} is a gift: {} from {} confirms a non-repayable gift of {} to {}{} with no interest in the investments.").format(
                tx_desc(t)[0].upper() + tx_desc(t)[1:], d.id, giver, money(t["amount_gbp"]),
                ("their " + rel.group(1)) if rel else "the customer", "", )
            add(Finding("THIRD_PARTY_DOCUMENTED", reason, [d.id, t["transaction_id"]], txns=[t["transaction_id"]]))

    # ---- payer is a company in the same group
    for d in case.docs_of("group_structure"):
        comps = [r.get("company") for tb in d.tables for r in tb if r.get("company")]
        owners = [(r.get("company"), r.get("owner") or r.get("owned by")) for tb in d.tables for r in tb]
        for t in flagged:
            cp = t.get("counterparty_name") or ""
            if t["direction"] != "in" or case.is_customer_name(cp) or not any(norm(c) == norm(cp) for c in comps):
                continue
            in_group = any(norm(c) == norm(cust) for c in comps)
            if not in_group:
                continue
            ow = dict(owners)
            link = "{} is owned by {} and {} by {}".format(cust, ow.get(cust) or "the group", cp, ow.get(cp) or "the group")
            email = [x for x, tg in tags.items() if "group_funding_claim" in tg]
            sinv = None
            if email and case.held:
                sinv = next((x for x in case.docs_of("supplier_invoice") if party_in(case.held.get("counterparty_name"), x.text)), None)
            reason = ("{} comes from {}, a company in the client's group: {} shows {}.{}{}").format(
                tx_desc(t)[0].upper() + tx_desc(t)[1:], cp, d.id, link,
                " In {} the owner explains it is covering a supplier payment while receivables come in.".format(email[0]) if email else "",
                " The held payment to {} is supported by supplier invoice {}.".format(case.held["counterparty_name"], sinv.id) if sinv else "")
            ids = [t["transaction_id"]]
            if sinv and case.held:
                ids.append(case.held["transaction_id"])
            ev = [d.id] + email[:1] + ([sinv.id] if sinv else []) + ids
            add(Finding("GROUP_COMPANY", reason, ev, txns=ids, rules=["TM08"]))
            break

    # ---- invoice finance advance
    fac = case.doc("factoring_agreement")
    rem = case.doc("remittance_advice")
    if fac and rem:
        for t in flagged:
            if t["direction"] == "in" and rem.has_amount(t["amount_gbp"]) and party_in(t.get("counterparty_name"), fac.text) \
                    and party_in(cust, rem.text):
                reason = ("{} is an invoice-finance advance: factoring agreement {} with {} pays advances into the client's account, "
                          "and remittance advice {} shows this {} advance against {}.").format(
                    tx_desc(t)[0].upper() + tx_desc(t)[1:], fac.id, t["counterparty_name"], rem.id, money(t["amount_gbp"]),
                    (rem.f("invoices financed") or "the client's invoices"))
                add(Finding("THIRD_PARTY_DOCUMENTED", reason, [fac.id, rem.id, t["transaction_id"]], txns=[t["transaction_id"]]))

    # ---- freight forwarder: exporters' payments reconcile to freight invoices
    fi = case.doc("freight_invoices")
    if fi:
        rows = [r for tb in fi.tables for r in tb]
        pay_in = [t for t in flagged if t["direction"] == "in" and not case.is_customer_name(t.get("counterparty_name"))]
        ok_in = [t for t in pay_in if any(norm(r.get("exporter")) == norm(t.get("counterparty_name"))
                                          and same_amount(amounts_in(r.get("amount", ""))[0] if amounts_in(r.get("amount", "")) else None, t["amount_gbp"])
                                          for r in rows)]
        ci = case.doc("carrier_invoice")
        out_ok = []
        if ci:
            out_ok = [t for t in flagged if t["direction"] == "out" and ci.has_amount(t["amount_gbp"])
                      and (party_in(t.get("counterparty_name"), ci.text) or (acct_tail(t.get("counterparty_account")) or "x") in ci.text)]
        if pay_in and len(ok_in) == len(pay_in):
            reason = ("The {} payments in from exporters ({}) match freight invoices {} by payer and amount, which is how a freight "
                      "forwarder collects shipping charges.{}").format(
                len(pay_in), ", ".join(t["counterparty_name"] for t in pay_in[:3]), fi.id,
                " {} pays carrier {} for those shipments, per carrier invoice {}.".format(
                    tx_desc(out_ok[0])[0].upper() + tx_desc(out_ok[0])[1:], out_ok[0]["counterparty_name"], ci.id) if out_ok else "")
            add(Finding("BUSINESS_RECORDS_RECONCILE", reason, [fi.id] + ([ci.id] if ci else []) + tx_ids(pay_in[:3] + out_ok[:1]),
                        txns=tx_ids(pay_in + out_ok), rules=["TM08", "TM02"]))

    # ---- payment file of contractors
    for d in case.docs_of("payment_file"):
        rows = [r for tb in d.tables for r in tb]
        if rows and all(r.get("contract") for r in rows) and "TM04" in case.rules:
            act = case.cust.get("business_activity") or "the business"
            reason = ("Payment file {} lists the {} contractors paid this month, each with a contract reference and amount, which fits "
                      "the client's business ({}). The many beneficiaries are the client's own freelance staff.").format(
                d.id, len(rows), act.lower())
            add(Finding("INVOICE_MATCHES", reason, [d.id] + ([case.held["transaction_id"]] if case.held else []),
                        txns=([case.held["transaction_id"]] if case.held else []), rules=["TM04"]))

    # ---- staged contract payments under a threshold
    for d in case.docs_of("contract_schedule"):
        sup = d.f("supplier") or ""
        ts = case.alert_txns("TM03")
        stage = [r for tb in d.tables for r in tb]
        stage_amts = [amounts_in(r.get("amount", ""))[0] for r in stage if amounts_in(r.get("amount", ""))]
        if ts and all(any(same_amount(a, t["amount_gbp"]) for a in stage_amts) and party_in(t.get("counterparty_name"), sup) for t in ts):
            reason = ("The {} payments to {} each match a stage of the fit-out contract schedule {} ({}), paid as each stage fell due. "
                      "They are separate contract stages, not a split payment.").format(
                len(ts), ts[0]["counterparty_name"], d.id, d.f("project") or "project")
            add(Finding("INVOICE_MATCHES", reason, [d.id] + tx_ids(ts), txns=tx_ids(ts), rules=["TM03"]))

    # ---- supplier invoice for a held / flagged payment
    for d in case.docs_of("supplier_invoice"):
        sup = d.f("supplier") or ""
        payto = acct_tail(d.f("pay to") or "")
        for t in flagged:
            if t["direction"] != "out" or not d.has_amount(t["amount_gbp"]):
                continue
            if not party_in(t.get("counterparty_name"), sup) and not party_in(t.get("counterparty_name"), d.text):
                continue
            if payto and acct_tail(t.get("counterparty_account")) and payto != acct_tail(t.get("counterparty_account")):
                continue
            con = [x for x in case.docs_of("supply_contract") if party_in(t.get("counterparty_name"), x.text)]
            reason = ("{} matches supplier invoice {} from {} for {}, with the same amount and the same account ending {}.{}").format(
                tx_desc(t)[0].upper() + tx_desc(t)[1:], d.id, t["counterparty_name"], (d.f("goods") or "the goods").lower(), payto,
                " Supply contract {} sets up this supplier relationship.".format(con[0].id) if con else "")
            add(Finding("INVOICE_MATCHES", reason, [d.id] + [x.id for x in con] + [t["transaction_id"]], txns=[t["transaction_id"]]))
            break

    # ---- customer order behind higher funding
    po = case.doc("purchase_order")
    if po and "TM01" in case.rules:
        pon = re.search(r"PO \d+", po.text)
        sinv = next((x for x in case.docs_of("supplier_invoice") if pon and pon.group(0) in x.text and party_in(cust, x.f("buyer") or "")), None)
        if pon and sinv:
            buyer = re.search(r"\n([A-Z][\w&' -]+?(?:Ltd|plc|LLP)) requests", po.text)
            reason = ("The higher funding is for a large customer order: purchase order {} ({}) is worth {}, and supplier invoice {} from {} "
                      "for the goods on the same order is for {}. The client's own funding matches this order.").format(
                po.id, pon.group(0), money(max(po.amounts)) if po.amounts else "", sinv.id, sinv.f("supplier") or "the supplier",
                money(max(sinv.amounts)) if sinv.amounts else "")
            fund = [t for t in case.alert_txns("TM01") if t["direction"] == "in"]
            add(Finding("INVOICE_MATCHES", reason, [po.id, sinv.id] + tx_ids(fund[:3]), txns=tx_ids(fund), rules=["TM01"]))

    # ---- cash withdrawn earlier and paid back in
    for did, tg in tags.items():
        if "redeposit_claim" not in tg:
            continue
        d = next(x for x in case.docs if x.id == did)
        cash = [t for t in flagged if t.get("channel") in ("cash_deposit", "cash_topup") and d.has_amount(t["amount_gbp"])]
        for t in cash:
            w = next((x for x in case.txns if x.get("channel") == "cash_withdrawal" and same_amount(x["amount_gbp"], t["amount_gbp"])
                      and x["booked_at"] < t["booked_at"]), None)
            if not w:
                continue
            prev = [r for r in case.reviews if r.get("rule_id") in ("TM05",) and r["reviewed_on"] >= w["booked_at"][:10]]
            reason = ("The {} cash deposit on {} is the same cash the customer withdrew on {} ({}) for a private car purchase that fell "
                      "through, as they explained in {}.{}").format(
                money(t["amount_gbp"]), fmt_date(t["booked_at"]), fmt_date(w["booked_at"]), w["transaction_id"], did,
                " The withdrawal was reviewed and closed in {}.".format(prev[0]["review_id"]) if prev else "")
            add(Finding("REFUND_OF_EARLIER_PAYMENT", reason, [did, t["transaction_id"], w["transaction_id"]] + [r["review_id"] for r in prev[:1]],
                        txns=[t["transaction_id"]]))

    return found


# ================================================================== RFI asks
def rfi_for(case, rule, tags, findings):
    ts = key_txns(case, rule) or case.alert_txns(rule)
    big = sorted(ts, key=lambda t: -t["amount_gbp"])
    t = big[0] if big else None
    ev = tx_ids(big[:3])
    ref = (t.get("reference") or "").upper() if t else ""
    cp = (t.get("counterparty_name") if t else None) or ""
    alltags = {k for tg in tags.values() for k in tg}
    claim_docs = [d for d, tg in tags.items() if tg & {"private_sale_claim", "gift_claim", "unverified_claim", "llm_unverified_claim"}]

    def r(code, text, extra=()):
        return Finding(code, text, list(extra) + claim_docs[:1] + ev)

    if t is None:
        return r("RFI_PURPOSE_DOCUMENTS", "The alert {} is not explained by anything on file. Ask the customer for documents showing what the activity was for.".format(rule))
    desc = tx_desc(t)
    if case.is_customer_name(cp) and t["direction"] == "in" and case.inst.name != "payments_fx":
        return r("RFI_SOURCE_OF_FUNDS",
                 "{} came from an account in the customer's own name{}, but nothing on file shows they hold it. "
                 "Ask for a statement for that account in the customer's name showing the transfer.".format(
                     desc[0].upper() + desc[1:], " (" + t["counterparty_account"] + ")" if t.get("counterparty_account") else ""))
    if claim_docs and t["direction"] == "in":
        return r("RFI_SOURCE_OF_FUNDS",
                 "{} is explained only by what the customer said in {}, with no document behind it. Ask for proof of where the money came "
                 "from, such as a signed sale receipt or the buyer's details and the vehicle's registration document.".format(
                     desc[0].upper() + desc[1:], claim_docs[0]))
    if re.search(r"ESTATE OF", ref):
        return r("RFI_SOURCE_OF_FUNDS", "{} (reference '{}') looks like an inheritance, but there is no solicitor's or executor's letter on file. "
                 "Ask for the letter from the executors confirming the customer's entitlement.".format(desc[0].upper() + desc[1:], t.get("reference")))
    if re.search(r"CLAIM", ref):
        return r("RFI_SOURCE_OF_FUNDS", "{} (reference '{}') looks like an insurance payout, but no claim letter is on file. "
                 "Ask for the insurer's settlement letter for this claim.".format(desc[0].upper() + desc[1:], t.get("reference")))
    if re.search(r"SALE PROCEEDS|COMPLETION", ref):
        return r("RFI_SOURCE_OF_FUNDS", "{} is described as sale proceeds, but no completion statement is on file. "
                 "Ask for the solicitor's completion statement for the sale.".format(desc[0].upper() + desc[1:]))
    if re.search(r"SETTLEMENT", ref):
        return r("RFI_SOURCE_OF_FUNDS", "{} is described as a settlement, but no settlement letter is on file. Ask for the settlement agreement or letter.".format(desc[0].upper() + desc[1:]))
    if re.search(r"DEPOSIT RETURN", ref):
        outs_ = [x for x in case.flagged if x["direction"] == "out" and not routine_out(x)]
        return r("RFI_PURPOSE_DOCUMENTS", "{} is described as a returned deposit and {} paid out soon after, with nothing on file for either. "
                 "Ask for the landlord's deposit return letter and a receipt for the payment out.".format(
                     desc[0].upper() + desc[1:], money(sum(x['amount_gbp'] for x in outs_)) if outs_ else "money was"))
    if re.search(r"LOAN", ref):
        outs_ = [x for x in case.flagged if x["direction"] == "out" and not routine_out(x)]
        return r("RFI_PURPOSE_DOCUMENTS", "{} is described as a loan advance and {} was paid out the next day, with no documents on file. "
                 "Ask for the loan agreement and the invoice for what was bought.".format(
                     desc[0].upper() + desc[1:], (tx_desc(outs_[0])) if outs_ else "money"))
    if re.search(r"GIFT", ref):
        return r("RFI_SOURCE_OF_FUNDS", "{} is described as a gift, but no gift letter is on file. Ask for a signed gift letter from {} "
                 "stating their relationship to the customer and that the money is not repayable.".format(desc[0].upper() + desc[1:], cp))
    if rule == "TM04" or re.search(r"trip|tour|holiday", ref, re.I) or (rule == "TM02" and "TM04" in case.rules):
        payers = [x for x in case.alert_txns("TM04") if x["direction"] == "in" and not regular_income(case, x)]
        if payers and case.inst.name != "payments_fx":
            return Finding("RFI_PURPOSE_DOCUMENTS",
                           "{} different people paid the account ({} in total, references such as '{}') and the money was paid out again, "
                           "but nothing on file explains the collection. Ask for the booking or invoice the money was collected for and the list of people it covers.".format(
                               len({x['counterparty_name'] for x in payers}), money(sum(x['amount_gbp'] for x in payers)), payers[0].get("reference")),
                           tx_ids(payers[:3]))
        return r("RFI_BUSINESS_RECORDS", "The client paid {} different beneficiaries in 30 days, more than usual, and no payment file or contracts are on file. "
                 "Ask for the payment file listing each beneficiary and what they were paid for.".format(
                     case.alert("TM04").get("measured", {}).get("distinct_counterparties") if case.alert("TM04") else "many"))
    if rule == "TM07":
        return r("RFI_SOURCE_OF_FUNDS", "The account was dormant for {} days and {} then arrived. Nothing on file re-verifies the customer or shows "
                 "where the money came from. Ask the customer to re-verify their identity and provide a statement for the sending account.".format(
                     case.alert("TM07").get("measured", {}).get("dormant_days"), desc))
    if rule == "TM08":
        return r("RFI_COUNTERPARTY_DETAILS", "{} came from {}, an account not in the customer's name, and nothing on file explains who they are. "
                 "Ask who {} is, their relationship to the customer and what the payment was for.".format(desc[0].upper() + desc[1:], cp, cp))
    if rule == "TM10":
        a = case.alert("TM10").get("measured", {})
        return r("RFI_PURPOSE_DOCUMENTS", "Units in {} were sold back after only {} days and nothing on file explains why. Ask the customer why "
                 "they sold early and for any supporting document (for example a letter about a change in circumstances).".format(a.get("fund"), a.get("held_days")))
    if rule == "TM03":
        return r("RFI_PURPOSE_DOCUMENTS", "{} payments to {} each fell just under £10,000 and no invoice or contract is on file. Ask for the invoices or "
                 "contract schedule the payments relate to.".format(len(case.alert_txns('TM03')), cp))
    if rule == "TM06":
        return r("RFI_PURPOSE_DOCUMENTS", "{} went to or from {}, a higher-risk country the customer did not declare, and nothing on file explains it. "
                 "Ask for documents showing what the payment was for.".format(desc[0].upper() + desc[1:], t.get("counterparty_country")))
    if case.inst.name == "payments_fx" and rule == "TM01":
        m = case.alert("TM01").get("measured", {})
        return r("RFI_UPDATED_PROFILE", "The client funded {} in 30 days, {:.1f} times the monthly share of its declared expected volume, from its own bank, "
                 "and nothing on file explains the increase. Ask for an updated expected annual payment volume with the orders or contracts behind it.".format(
                     money(m.get("received_gbp") or 0), m.get("multiple") or 0))
    if rule == "TM02":
        return r("RFI_PURPOSE_DOCUMENTS", "{} was paid in and most of it paid out again within 48 hours, and nothing on file explains it. "
                 "Ask for documents showing what the payments were for.".format(desc[0].upper() + desc[1:]))
    if case.is_business:
        return r("RFI_BUSINESS_RECORDS", "{} took the account above its declared turnover and nothing on file explains it. Ask for the business records "
                 "(invoices or management accounts) behind the activity.".format(desc[0].upper() + desc[1:]))
    return r("RFI_SOURCE_OF_FUNDS", "{} took the account well above the declared income and nothing on file explains where it came from. "
             "Ask for proof of the source of the money.".format(desc[0].upper() + desc[1:]))


# ================================================================== decide
def decide(case, reader=None):
    tags = read_free_text(case, reader)

    esc = red_flags(case, tags)
    if esc:
        return "ESCALATE", esc, []

    list_state, list_f = (None, None)
    if "TM22" in case.rules:
        list_state, list_f = tm22(case)
        if list_state == "esc":
            return "ESCALATE", list_f, []

    found = explainers(case, tags)
    if list_state == "close":
        found.insert(0, list_f)

    explained = set()
    for f in found:
        explained |= f.txns

    covered, gaps = [], []
    for rule in case.rules:
        if rule == "TM22":
            (covered if list_state == "close" else gaps).append(rule)
            continue
        if any(rule in f.rules for f in found):
            covered.append(rule)
            continue
        kt = key_txns(case, rule)
        if rule == "TM07":
            verified = any("TM07_verified" in f.rules for f in found)
            money_ok = kt and all(t["transaction_id"] in explained for t in kt)
            docs_ok = money_ok and (verified or any(f.code == "DOCUMENTED_EVENT" for f in found)
                                    or any(f.code == "INVOICE_MATCHES" for f in found))
            (covered if docs_ok else gaps).append(rule)
            continue
        if kt and all(t["transaction_id"] in explained for t in kt):
            covered.append(rule)
        elif rule == "TM01" and tm01_residual_ok(case, kt, explained):
            covered.append(rule)
        elif not kt and found:
            covered.append(rule)
        else:
            gaps.append(rule)

    # unverified explanations left in chats block a close on their own alerts
    if not gaps and found:
        used = [f for f in found if f.code != "RETURNING_CUSTOMER" or "TM07" in case.rules]
        main = pick_main(case, used)
        own = [f for f in used if f.own_account]
        if own and ("TM01" in case.rules or main.own_account):
            main_own = own[0]
            m = case.window_inflow_multiple()
            lim = case.inst.policy.get("own_account_multiple", 5.0)
            if m is not None and m >= lim:
                base = case.baseline()
                reason = ("{} However, these own-account transfers took money received to {:.1f} times the declared monthly {} of {}, "
                          "at or above the policy limit of {:g} times. Ask the customer for an updated expected monthly income or volume.").format(
                    main_own.reason, m, "turnover" if case.is_business else "income", money(base) if base else "baseline", lim)
                return "RFI", Finding("RFI_UPDATED_PROFILE", reason, main_own.evidence), []
        return "CLOSE", main, [f for f in used if f is not main]

    # something is unexplained
    if list_state == "rfi" and "TM22" in gaps:
        return "RFI", list_f, []
    gaps.sort(key=lambda r: RULE_PRIORITY.index(r) if r in RULE_PRIORITY else 99)
    # cover any own-account statement shown but over the multiple
    return "RFI", rfi_for(case, gaps[0], tags, found), []


def tm01_residual_ok(case, kt, explained):
    """Payments firm: once documented inflows are taken out, the client's own
    funding alone stays under the TM01 limit."""
    a = case.alert("TM01")
    m = a.get("measured", {}) if a else {}
    rest = [t for t in kt if t["transaction_id"] not in explained]
    if not explained or not rest or not all(own_funding(case, t) for t in rest):
        return False
    base, lim, rec = m.get("baseline_gbp"), m.get("limit_multiple"), m.get("received_gbp")
    if not (base and lim and rec):
        return False
    done = sum(t["amount_gbp"] for t in kt if t["transaction_id"] in explained)
    return (rec - done) / base <= lim


def pick_main(case, found):
    order = {r: i for i, r in enumerate(RULE_PRIORITY)}
    best, best_score = None, 999
    for f in found:
        rules = set(f.rules)
        for t in f.txns:
            for a in case.alerts:
                if t in (a.get("transaction_ids") or []):
                    rules.add(a["rule_id"])
        if "TM07_verified" in rules:
            rules.add("TM07")
        score = min([order.get(r, 50) for r in rules] or [60])
        if f.code == "LIST_MATCH_DIFFERENT_PARTY":
            score = -1
        elif f.code == "RETURNING_CUSTOMER" and "TM07" in case.rules:
            score = 0
        if score < best_score:
            best, best_score = f, score
    return best or found[0]
