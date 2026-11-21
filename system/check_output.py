#!/usr/bin/env python3
"""Check an output file against the required format.

Usage:  python3 system/check_output.py <institution folder> <output file>

Checks: one line per case (none missing, none extra, no duplicates), every
alert of the case listed, verdict and reason code valid and consistent,
reason of 2-4 sentences, evidence IDs that exist in the folder's files.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from tmdata import Case, Institution  # noqa: E402
from tmoutput import check_line       # noqa: E402


def main(folder, path):
    inst = Institution(folder)
    known = inst.known_ids()
    cases = {row["case_id"]: Case(inst, row) for row in inst.cases}
    seen, problems = set(), []
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                problems.append("line %d: not valid JSON" % n)
                continue
            cid = line.get("case_id")
            if cid not in cases:
                problems.append("line %d: unknown case %s" % (n, cid))
                continue
            if cid in seen:
                problems.append("line %d: duplicate case %s" % (n, cid))
            seen.add(cid)
            for e in check_line(line, cases[cid], known):
                problems.append("%s: %s" % (cid, e))
    missing = [c for c in cases if c not in seen]
    for c in missing:
        problems.append("%s: missing from output" % c)
    print("%s: %d cases, %d lines checked, %d problems" % (inst.name, len(cases), len(seen), len(problems)))
    for p in problems[:30]:
        print("  " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
