#!/usr/bin/env python3
"""Measure the corpus-coverage pre-check in cmd_propose against hand-ruled ground truth.

WHY. On 2026-10-05 the open-proposal queue held 376 entries. I read the top 22, cited an
existing memory for 19 of them, and rejected all 19: the corpus already carried the lesson,
and in five cases the SAME SESSION had later measured the proposal false. Every one of those
19 should have been caught by the `covered` check in cmd_propose, which downgrades a proposal
to `refine` when an existing memory already says it. That check had fired on 12 of 376.

Two separate failures, and the second is the one that matters:

    the check FIRED on                        4 / 19
    the cited memory was even a CANDIDATE on  2 / 19

So the threshold was never the binding constraint. The candidate query cannot see the right
memory, which means lowering COVER_J only buys false positives. Any change to the check has
to move the second number first, and that is what this script measures.

    python3 measure-coverage-check.py             # current behaviour
    python3 measure-coverage-check.py --widen     # candidate query variants, side by side

The controls matter as much as the cases: three proposals ruled HOLD (genuinely new, nothing
in the corpus covers them) must NOT fire, or a wider check just downgrades everything and
the `refine` label stops meaning anything.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
MEM = pathlib.Path(os.path.expanduser(
    os.environ.get("CLAUDE_MEMORY_DIR", "~/.claude/projects/-home-plafayette/memory")))


def load_curator():
    spec = importlib.util.spec_from_file_location("mc", HERE / "memory-curator.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def description_of(stem: str) -> str:
    """The memory's own description line, which is what the DB indexes at weight 5."""
    try:
        text = (MEM / (stem + ".md")).read_text(encoding="utf-8")
    except OSError:
        return ""
    m = (re.search(r'^description:\s*"(.+?)"\s*$', text, re.M | re.S)
         or re.search(r"^description:\s*(.+)$", text, re.M))
    return m.group(1) if m else ""


def candidates(con, mc, desc, n_terms, limit, longest_first):
    """Candidates for one proposal description.

    The LIVE configuration calls mc.coverage_candidates() itself rather than reproducing it,
    so the row labelled `live` can never describe behaviour the curator stopped having. The
    first version of this script did inline the query, and went on reporting the old 2/19
    after the fix had landed - a measuring instrument quietly describing a previous version
    of the thing it measures.

    The variant rows exist only to show what the knobs buy, and `longest_first=False` is
    kept because it reproduces the original defect: `_toks` returns a SET, so the old
    `list(_toks(desc))[:14]` kept whichever terms hash order put first and dropped the
    discriminating words at random, differently on every run.
    """
    if n_terms is None:                      # the live code path, knobs and all
        try:
            return mc.coverage_candidates(con, desc)
        except sqlite3.Error:
            return []
    toks = mc._toks(desc)
    terms = sorted(toks, key=lambda t: (-len(t), t)) if longest_first else list(toks)
    terms = terms[:n_terms]
    if not terms:
        return []
    try:
        return con.execute(
            "SELECT name, description FROM mem WHERE mem MATCH ? "
            "ORDER BY bm25(mem,3.0,5.0,1.0,0,0,0,0,0) LIMIT ?",
            (" OR ".join('"%s"' % t for t in terms), limit)).fetchall()
    except sqlite3.Error:
        return []


def run(mc, con, cfg, n_terms, limit, longest_first, bar):
    """Return (fired, in_candidates, control_fires) for one configuration."""
    fired = in_cand = 0
    for c in cfg["cases"]:
        cand = mc._toks(c["name"].replace("-", " ") + " " + c["description"])
        hits = candidates(con, mc, c["description"], n_terms, limit, longest_first)
        names = [h for h, _ in hits]
        if any(c["cited"] == h or c["cited"].endswith(h) or h in c["cited"] for h in names):
            in_cand += 1
        best = max((mc.containment(cand, mc._toks(h + " " + (d or "")))
                    for h, d in hits), default=0.0)
        fired += best >= bar
    ctrl = 0
    for c in cfg["controls"]:
        if "description" not in c:
            continue
        cand = mc._toks(c["name"].replace("-", " ") + " " + c["description"])
        hits = candidates(con, mc, c["description"], n_terms, limit, longest_first)
        best = max((mc.containment(cand, mc._toks(h + " " + (d or "")))
                    for h, d in hits), default=0.0)
        ctrl += best >= bar
    return fired, in_cand, ctrl


def main() -> int:
    mc = load_curator()
    cfg = json.loads((HERE / "coverage-calibration.json").read_text())
    n_cases = len(cfg["cases"])
    n_ctrl = sum(1 for c in cfg["controls"] if "description" in c)
    if not os.path.exists(mc.DB_PATH):
        sys.exit("no index at %s - run the searcher once first" % mc.DB_PATH)
    con = sqlite3.connect(mc.DB_PATH)

    print("ground truth: %d duplicates that SHOULD fire, %d new lessons that must NOT"
          % (n_cases, n_ctrl))
    print("corpus: %s\n" % mc.DB_PATH)

    print("%-34s %-14s %-14s %s" % ("configuration", "cited in cand", "check fires", "false +"))
    rows = [("LIVE: coverage_candidates()", None, None, True, mc.COVER_J)]
    if "--widen" in sys.argv:
        rows += [
            ("the old defect (unordered, LIMIT 3)", 14, 3, False, mc.COVER_J),
            ("deterministic terms, LIMIT 3", 14, 3, True, mc.COVER_J),
            ("all terms, LIMIT 12", 99, 12, True, mc.COVER_J),
            ("all terms, LIMIT 12, bar 0.35", 99, 12, True, 0.35),
            ("all terms, LIMIT 12, bar 0.30", 99, 12, True, 0.30),
            ("all terms, LIMIT 25, bar 0.30", 99, 25, True, 0.30),
        ]
    for label, nt, lim, det, bar in rows:
        f, ic, ctrl = run(mc, con, cfg, nt, lim, det, bar)
        print("%-34s %2d/%-11d %2d/%-11d %d/%d" % (label, ic, n_cases, f, n_cases, ctrl, n_ctrl))

    print("\nRead it in this order: a configuration cannot fire on a case whose memory is "
          "not\nin its candidate set, so `cited in cand` is the ceiling for `check fires`. "
          "A bar\nchange moves the second column only, and buys false positives in the "
          "third.")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
