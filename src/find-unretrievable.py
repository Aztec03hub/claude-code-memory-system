#!/usr/bin/env python3
"""Find memories that their OWN generated questions cannot retrieve, and say why.

A memory that no query reaches is written, indexed, correct and invisible. It fails by never
appearing, which is the one failure mode this system cannot report on itself: a missing
injection looks exactly like a prompt that needed nothing.

THE TEST. Each memory has doc2query questions (generated from its own content, indexed at
the highest BM25 weight). Score each question against the live index and find where its own
memory lands. A memory absent from the candidate window on most of its own questions is
unretrievable BY CONSTRUCTION: outside that window the reranker never sees it and the
learned weights never apply, so no amount of prompt tuning can reach it.

MEASURED 2026-10-05, 1039 memories: 75 fail, and the cause is SIZE rather than query
wording. Median 1656 bytes and a 122-char description against 3250 bytes and 245 chars for
findable ones, while the questions themselves are the same length (5.2 vs 5.6 terms). BM25
scores term overlap, and a document with few terms has few chances to overlap.

    python3 find-unretrievable.py                 # the list, with a diagnosis per memory
    python3 find-unretrievable.py --proxy         # can size alone predict it? (for a gate)
    python3 find-unretrievable.py --json FILE     # write the list for a remediation pass

`--proxy` exists because the write-time gate has to decide in milliseconds, with no index
and no questions. It can only see length, so this prints how well length alone separates the
two populations. A gate on a proxy is honest only if the proxy's separation is published
next to it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
WINDOW = 120             # the candidate window the reranker is shown; outside it is invisible
FAIL_FRACTION = 0.50     # share of a memory's own questions that must fail to flag it
# 0.50, not 0.80: MEASURED 2026-10-05 with a correct matcher, NO memory in the corpus
# fails 80% of its own questions, so an 0.80 bar reports a clean zero on a corpus that
# does have 36 weakly-reachable memories. A bar that can only ever print zero is not a
# reassuring result, it is an instrument with no resolving power.
MIN_QUESTIONS = 3        # fewer than this is not evidence about the memory


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def body_of(path):
    """Everything after the frontmatter: the text a description could be widened FROM."""
    text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    parts = text.split("---", 2)
    return parts[2] if len(parts) > 2 else text


def description_of(path):
    text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    m = (re.search(r'^description:\s*"(.+?)"\s*$', text, re.M | re.S)
         or re.search(r"^description:\s*(.+)$", text, re.M))
    return m.group(1).strip() if m else ""


def rank_of(con, ms, question, stem):
    """Where `stem` lands for `question`, or None if outside the window."""
    terms = ms.make_terms(question)
    if len(terms) < 2:
        return None
    expr = " OR ".join('"%s"' % t for t in terms)
    try:
        rows = con.execute(
            "SELECT path FROM mem WHERE mem MATCH ? "
            "ORDER BY bm25(mem,3.0,5.0,1.0,0,0,0,0,0) LIMIT ?", (expr, WINDOW)).fetchall()
    except Exception:
        return None
    for i, (p,) in enumerate(rows):
        if os.path.basename(p)[:-3] == stem:
            return i + 1
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args()

    ms = load("ms", "memory-search.py")
    import sqlite3
    files = ms.source_files()
    if not files:
        sys.exit("no memories found")
    ms.ensure_fresh(files)
    q2q = json.loads(pathlib.Path(ms.Q2Q_PATH).read_text()) if os.path.exists(ms.Q2Q_PATH) else {}
    if not q2q:
        sys.exit("no doc2query sidecar at %s - run `memory-curator.py enrich` first" % ms.Q2Q_PATH)
    con = sqlite3.connect(ms.DB_PATH)

    bad, good, stale, skipped = [], [], [], 0
    for key, entry in q2q.items():
        qs = entry.get("queries") if isinstance(entry, dict) else entry
        if not isinstance(qs, list) or len(qs) < MIN_QUESTIONS:
            skipped += 1
            continue
        stem = os.path.basename(str(key))
        stem = stem[:-3] if stem.endswith(".md") else stem
        # EXISTENCE FIRST. A sidecar entry for a deleted or renamed memory fails every
        # question by construction and lands at the top of this report looking exactly like
        # a real unreachable memory. Six such entries were the whole "100% fails" tier
        # before this check existed. `memory-curator.py prune` now drops them too.
        path = None
        for f in files:
            if os.path.basename(str(f))[:-3] == stem:
                path = str(f)
                break
        if not path or not os.path.exists(path):
            stale.append(stem)
            continue
        ranks = [rank_of(con, ms, q, stem) for q in qs]
        misses = sum(r is None for r in ranks)
        rec = {"stem": stem, "path": path, "questions": len(qs), "misses": misses,
               "size": os.path.getsize(path), "desc_len": len(description_of(path)),
               "body_len": len(body_of(path).strip())}
        (bad if misses / len(qs) >= FAIL_FRACTION else good).append(rec)
    con.close()

    if args.proxy:
        print("Can LENGTH alone predict unretrievability? The write-time gate sees nothing "
              "else.\n")
        print("%-10s %6s %6s %6s %6s  %s" % ("size<", "caught", "of", "missed", "false+", "precision"))
        for bar in (1200, 1600, 2000, 2500, 3000):
            caught = sum(r["size"] < bar for r in bad)
            fp = sum(r["size"] < bar for r in good)
            prec = caught / float(caught + fp) if caught + fp else 0.0
            print("%-10d %6d %6d %6d %6d  %.2f"
                  % (bar, caught, len(bad), len(bad) - caught, fp, prec))
        print("\nA gate wants high recall and tolerable precision: a false positive only "
              "asks\nfor more text on a memory that was already findable, which is cheap. "
              "A false\nnegative is a memory that will never be retrieved and never reports "
              "it.")
        return 0

    bad.sort(key=lambda r: r["size"])
    print("%d of %d memories with >=%d questions fail >=%d%% of their own questions "
          "(%d skipped, %d stale sidecar entries ignored)\n"
          % (len(bad), len(bad) + len(good), MIN_QUESTIONS, int(FAIL_FRACTION * 100),
             skipped, len(stale)))
    print("%-62s %6s %5s %6s %s" % ("memory", "bytes", "desc", "body", "miss"))
    for r in bad:
        print("%-62s %6d %5d %6d %d/%d"
              % (r["stem"][:62], r["size"], r["desc_len"], r["body_len"],
                 r["misses"], r["questions"]))

    widenable = [r for r in bad if r["body_len"] > 3 * max(r["desc_len"], 1)]
    print("\n%d of %d have a body at least 3x their description, so the description can be "
          "widened\nfrom text the memory ALREADY contains - no new information needed."
          % (len(widenable), len(bad)))
    if bad:
        import statistics
        print("medians: unretrievable %d bytes / %d desc chars | findable %d / %d"
              % (statistics.median(r["size"] for r in bad),
                 statistics.median(r["desc_len"] for r in bad),
                 statistics.median(r["size"] for r in good) if good else 0,
                 statistics.median(r["desc_len"] for r in good) if good else 0))
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(bad, indent=1) + "\n")
        print("\nwrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
