"""Load one institution folder and build a per-case context.

Everything here is plain parsing: no decisions are made in this module.
"""
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONEY_RE = re.compile(r"£\s?([\d,]+(?:\.\d{1,2})?)")
DATE_RE = re.compile(r"\b(\d{1,2}) (" + "|".join(MONTHS) + r") (\d{4})\b")


# ----------------------------------------------------------------- helpers
def money(x):
    return "£{:,.2f}".format(x)


def fmt_date(d):
    if isinstance(d, str):
        d = parse_iso(d)
    return "{} {} {}".format(d.day, MONTHS[d.month - 1], d.year) if d else "?"


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_long_date(s):
    m = DATE_RE.search(s or "")
    if not m:
        return None
    return date(int(m.group(3)), MONTHS.index(m.group(2)) + 1, int(m.group(1)))


def amounts_in(text):
    out = []
    for m in MONEY_RE.finditer(text or ""):
        try:
            out.append(round(float(m.group(1).replace(",", "")), 2))
        except ValueError:
            pass
    return out


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").lower())).strip()


def same_amount(a, b, tol=0.005):
    return a is not None and b is not None and abs(a - b) <= tol


def acct_tail(s):
    """Last four digits of an account / IBAN reference, for matching."""
    digits = re.findall(r"\d+", s or "")
    if not digits:
        return None
    return digits[-1][-4:]


# ---------------------------------------------------------------- documents
class Doc:
    def __init__(self, meta, text):
        self.id = meta["doc_id"]
        self.type = meta["type"]
        self.title = meta.get("title") or ""
        self.dated = parse_iso(meta.get("dated"))
        self.text = text
        self.fields = {}
        for m in re.finditer(r"\*\*([^*]+?):\*\*\s*(.*)", text):
            self.fields.setdefault(m.group(1).strip().lower(), m.group(2).strip())
        self.tables = parse_tables(text)
        self.amounts = amounts_in(text)
        self.lower = text.lower()

    def f(self, *keys):
        for k in keys:
            if k in self.fields:
                return self.fields[k]
        return None

    def has_amount(self, x):
        return any(same_amount(a, x) for a in self.amounts)

    def body(self):
        """Text without the title / ID / dated header lines."""
        t = re.sub(r"^# .*\n", "", self.text)
        t = re.sub(r"\*\*(Document ID|Dated):\*\*.*\n", "", t)
        return t.strip()


def parse_tables(text):
    tables, cur, header = [], None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                continue
            if cur is None:
                header, cur = [c.lower() for c in cells], []
                tables.append(cur)
            else:
                cur.append(dict(zip(header, cells)))
        else:
            cur, header = None, None
    return tables


# ---------------------------------------------------------------- loading
def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def parse_policy(path):
    """Pull the settings we need from POLICY.md so they are not hard-coded."""
    pol = {"high_risk": set(), "text": ""}
    if not os.path.exists(path):
        return pol
    text = open(path, encoding="utf-8").read()
    pol["text"] = text
    m = re.search(r"Higher-risk countries[^:]*:\s*(.+)", text)
    if m:
        for c in m.group(1).rstrip(". ").split(","):
            pol["high_risk"].add(c.strip().rstrip("."))
    m = re.search(r"at (\d+) times the declared income or volume or more", text)
    pol["own_account_multiple"] = float(m.group(1)) if m else 5.0
    return pol


class Institution:
    def __init__(self, folder):
        self.folder = folder
        self.name = os.path.basename(os.path.normpath(folder))
        j = lambda f: read_jsonl(os.path.join(folder, f))
        self.policy = parse_policy(os.path.join(folder, "POLICY.md"))
        self.cases = j("cases.jsonl")
        self.alerts = {a["alert_id"]: a for a in j("alerts.jsonl")}
        self.customers = {c["customer_id"]: c for c in j("customers.jsonl")}
        self.txns = {}
        self.txns_by_cust = defaultdict(list)
        for t in j("transactions.jsonl"):
            self.txns[t["transaction_id"]] = t
            self.txns_by_cust[t["customer_id"]].append(t)
        for v in self.txns_by_cust.values():
            v.sort(key=lambda t: (t["booked_at"], t["transaction_id"]))
        self.docs_by_cust = defaultdict(list)
        self.doc_ids = set()
        for e in j("evidence.jsonl"):
            p = os.path.join(folder, e.get("file") or ("evidence/%s.md" % e["doc_id"]))
            try:
                text = open(p, encoding="utf-8").read()
            except OSError:
                text = ""
            self.docs_by_cust[e["customer_id"]].append(Doc(e, text))
            self.doc_ids.add(e["doc_id"])
        for v in self.docs_by_cust.values():
            v.sort(key=lambda d: d.id)
        self.reviews_by_cust = defaultdict(list)
        for r in j("previous_reviews.jsonl"):
            self.reviews_by_cust[r["customer_id"]].append(r)
        self.list_entries = {e["entry_id"]: e for e in j("internal_list.jsonl")}

    def known_ids(self):
        ids = set(self.doc_ids) | set(self.txns) | set(self.list_entries)
        for v in self.reviews_by_cust.values():
            ids |= {r["review_id"] for r in v}
        return ids


class Case:
    """Everything about one case, with small lookup helpers."""

    def __init__(self, inst, row):
        self.inst = inst
        self.row = row
        self.id = row["case_id"]
        self.alert_ids = list(row.get("alert_ids") or [])
        self.alerts = [inst.alerts[a] for a in self.alert_ids if a in inst.alerts]
        self.cust = inst.customers.get(row["customer_id"], {})
        self.declared = self.cust.get("declared") or {}
        self.name = self.cust.get("name") or ""
        self.is_business = self.cust.get("kind") == "business"
        self.txns = inst.txns_by_cust.get(row["customer_id"], [])
        self.docs = inst.docs_by_cust.get(row["customer_id"], [])
        self.reviews = inst.reviews_by_cust.get(row["customer_id"], [])
        self.held = inst.txns.get(row.get("held_payment_id") or "")
        self.rules = sorted({a["rule_id"] for a in self.alerts})
        self.flagged_ids = []
        for a in self.alerts:
            for t in a.get("transaction_ids") or []:
                if t not in self.flagged_ids and t in inst.txns:
                    self.flagged_ids.append(t)
        if self.held and self.held["transaction_id"] not in self.flagged_ids:
            self.flagged_ids.append(self.held["transaction_id"])
        self.flagged = [inst.txns[t] for t in self.flagged_ids]
        self.high_risk = inst.policy["high_risk"]

    # lookups
    def alert(self, rule):
        for a in self.alerts:
            if a["rule_id"] == rule:
                return a
        return None

    def alert_txns(self, rule):
        out = []
        for a in self.alerts:
            if a["rule_id"] == rule:
                out += [self.inst.txns[t] for t in a.get("transaction_ids") or [] if t in self.inst.txns]
        return out

    def docs_of(self, *types):
        return [d for d in self.docs if d.type in types]

    def doc(self, *types):
        d = self.docs_of(*types)
        return d[0] if d else None

    def is_customer_name(self, name):
        n = norm(name)
        return bool(n) and n == norm(self.name)

    def multiple(self):
        a = self.alert("TM01")
        if a and a.get("measured", {}).get("multiple") is not None:
            return float(a["measured"]["multiple"])
        return None

    def baseline(self):
        a = self.alert("TM01")
        if a and a.get("measured", {}).get("baseline_gbp"):
            return float(a["measured"]["baseline_gbp"])
        d = self.declared
        if d.get("monthly_income_gbp"):
            return float(d["monthly_income_gbp"])
        if d.get("annual_turnover_gbp"):
            return float(d["annual_turnover_gbp"]) / 12.0
        return None

    def window_inflow_multiple(self):
        """Money received in the review window against the declared baseline."""
        m = self.multiple()
        if m is not None:
            return m
        base = self.baseline()
        if not base:
            return None
        w0 = self.row.get("review_window", {}).get("from") or ""
        w1 = self.row.get("review_window", {}).get("to") or "9999"
        tot = sum(t["amount_gbp"] for t in self.txns
                  if t["direction"] == "in" and w0 <= t["booked_at"][:10] <= w1)
        return tot / base
