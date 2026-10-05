#!/usr/bin/env python3
"""Dump open supersession pairs with BOTH sides resolved, for ruling.

Resolution goes through the same normalisation check-memory-index.py uses: case, separator
style and type prefix are all noise, because a pair names its sides by frontmatter `name:`
(hyphenated) while the corpus is filenames (underscored, type-prefixed). Comparing
literally reports almost every pair as FILE NOT FOUND and sends you auditing your own
regex instead of the memories - which is what happened on the first attempt here, and is
the same trap recorded in that script's `_norm` docstring.

    python3 show-pairs.py duplicate            # one verdict
    python3 show-pairs.py contradicts --full   # include each side's full body length
    python3 show-pairs.py refines --from 0 --count 40
"""
import argparse
import glob
import json
import os
import re

D = os.path.expanduser("~/.claude/projects/-home-plafayette/memory/")
PRE = ("feedback_", "project_", "reference_", "user_")


def norm(s):
    s = s.strip().lower().replace("-", "_")
    for p in PRE:
        if s.startswith(p):
            return s[len(p):]
    return s


def corpus():
    idx = {}
    for f in sorted(glob.glob(D + "*.md")):
        b = os.path.basename(f)
        if b.startswith("MEMORY"):
            continue
        t = open(f).read()
        m = re.search(r'^description:\s*"?(.+?)"?\s*$', t, re.M)
        nm = re.search(r'^name:\s*(.+?)\s*$', t, re.M)
        rec = (b, (m.group(1) if m else "(no description)"), len(t))
        for key in {norm(b[:-3])} | ({norm(nm.group(1))} if nm else set()):
            idx.setdefault(key, rec)
    return idx


def open_pairs():
    rows = [json.loads(l) for l in open(D + ".index/supersede-pending.jsonl") if l.strip()]
    dec = {json.loads(l)["pair_id"]
           for l in open(D + ".index/supersede-decided.jsonl") if l.strip()}
    latest = {}
    for r in rows:
        latest[r["pair_id"]] = r
    return [v for k, v in latest.items() if k not in dec]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("verdict", nargs="?", default="all")
    ap.add_argument("--from", dest="start", type=int, default=0)
    ap.add_argument("--count", type=int, default=1000)
    ap.add_argument("--chars", type=int, default=240)
    a = ap.parse_args()

    idx, pairs = corpus(), open_pairs()
    sel = [p for p in pairs if a.verdict in ("all", p["verdict"])]
    sel.sort(key=lambda p: p["pair_id"])
    window = sel[a.start:a.start + a.count]
    print(f"########## {a.verdict.upper()}: {len(sel)} open, showing "
          f"{a.start}..{a.start + len(window) - 1}")
    for i, p in enumerate(window, a.start + 1):
        # A `why` that refers to "Pair N" is the proposer having lost track of which pair
        # in its batch it was judging. Flagged, not hidden: the pairing itself is suspect.
        flag = "  <<< PROPOSER BATCH CONFUSION" if re.search(r"\bPair \d", p["why"]) else ""
        print(f"\n--- {i}. [{p['pair_id']}] {p['verdict']}{flag}\n    why: {p['why'][:170]}")
        for side in ("a", "b"):
            rec = idx.get(norm(p[side]))
            if not rec:
                print(f"  {side.upper()}  {p[side]}\n     *** NOT IN CORPUS ***")
                continue
            print(f"  {side.upper()}  {rec[0][:-3]}  ({rec[2]}B)")
            print(f"     {rec[1][:a.chars]}")
        if p.get("current"):
            print(f"  model suggests keeping: {p['current']}")


if __name__ == "__main__":
    main()
