#!/usr/bin/env python3
"""Regenerate the HARD reranker eval set against the CURRENT corpus.

WHY IT HAS TO BE REGENERATED, NOT EDITED. The HARD set's whole purpose is to hold cases
where the target sits at lexical rank 4-50: below BM25's top-3 (so the reranker is the only
thing that can surface it) but inside the candidate window (so it CAN). Measured 2026-10-05
against the set written when the corpus held ~876 memories, now 1039:

    still at lexical rank 4-50        10 of 26   (38%)
    now rank 1-3, reranker CANNOT help 4 of 26   BM25 already has it
    now outside BM25's top 120        12 of 26   the answer is not even in its input

So 46% of the set scored the reranker on cases whose answer never reached it, and 15% on
cases already won before it ran. Only ten cases were still measuring the component. That is
instrument decay with no code change, and it is why the set reported a 16-point "regression"
that two controls showed was not real. The band is a property of the corpus, so the set has
to be rebuilt whenever the corpus moves materially.

QUESTIONS COME FROM doc2query, NOT FROM ME. Each memory already has generated questions it
answers, indexed at the highest bm25 weight. Reusing them means the question text is not
tuned by the same hand that is about to tune the prompt, and it costs no API calls.

    python3 build-rerank-eval.py                 # report what the band looks like now
    python3 build-rerank-eval.py --write         # write docs/rerank-eval-hard-v2.json
"""
import argparse
import importlib.util
import json
import os
import sqlite3
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(os.path.dirname(SRC), "docs")
BAND_LO, BAND_HI = 4, 50
CAND_WINDOW = 120          # must match what search() actually pulls before reranking


def load_search():
    spec = importlib.util.spec_from_file_location(
        "memsearch", os.path.join(SRC, "memory-search.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["memsearch"] = m
    spec.loader.exec_module(m)
    return m


def norm(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--per-memory", type=int, default=1,
                    help="questions to keep per memory, to avoid one memory dominating")
    # 557 eligible cases would be ~20 minutes for a two-arm A/B, past the under-10-minute
    # iteration budget in feedback_short_ab_evals_during_development. Sampled EVENLY across
    # the rank band so the set keeps cases from the whole 4-50 range rather than clustering
    # at rank 4, where the reranker has the least work to do.
    ap.add_argument("--cap", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20261005)
    a = ap.parse_args()

    ms = load_search()
    files = ms.source_files()
    if files:
        ms.ensure_fresh(files)
    d2q = {}
    try:
        d2q = json.load(open(os.path.join(
            os.path.expanduser("~/.claude/projects/-home-plafayette/memory/.index"),
            "doc2query.json")))
    except OSError:
        pass
    if not d2q:
        print("no doc2query.json - run `memory-curator.py enrich` first")
        return 1

    con = sqlite3.connect(ms.DB_PATH)
    cases, band = [], {"1-3": 0, "4-50": 0, ">50": 0, "absent": 0, "skipped": 0}
    for key, questions in sorted(d2q.items()):
        # doc2query is keyed by absolute PATH; the eval and the matcher both want the stem.
        stem = os.path.basename(key)
        if stem.endswith(".md"):
            stem = stem[:-3]
        qs = questions if isinstance(questions, list) else (questions or {}).get("queries") or []
        kept = 0
        for q in qs:
            if not isinstance(q, str) or len(q) < 25:
                continue
            terms = ms.make_terms(q)
            if len(terms) < ms.MIN_QUERY_TERMS:
                band["skipped"] += 1
                continue
            expr = " OR ".join('"%s"' % t for t in terms)
            try:
                rows = con.execute(
                    "SELECT name FROM mem WHERE mem MATCH ? "
                    "ORDER BY bm25(mem,3.0,5.0,1.0,0,0,0,0,0) LIMIT ?",
                    (expr, CAND_WINDOW)).fetchall()
            except Exception:
                band["skipped"] += 1
                continue
            t = norm(stem)
            rank = None
            for i, (nm,) in enumerate(rows, 1):
                if t and (t in norm(nm) or norm(nm) in t):
                    rank = i
                    break
            if rank is None:
                band["absent"] += 1
            elif rank <= 3:
                band["1-3"] += 1
            elif rank <= BAND_HI:
                band["4-50"] += 1
                if kept < a.per_memory:
                    cases.append({"q": q, "target": stem, "rank": rank})
                    kept += 1
            else:
                band[">50"] += 1
    con.close()

    tot = sum(band.values()) or 1
    print("doc2query questions scored: %d" % tot)
    for k in ("1-3", "4-50", ">50", "absent", "skipped"):
        print("  %-8s %5d  (%.1f%%)%s" % (k, band[k], 100.0 * band[k] / tot,
                                          "   <- the reranker's zone" if k == "4-50" else ""))
    print("\neligible HARD cases found: %d (capped at %d per memory)"
          % (len(cases), a.per_memory))
    if not a.write:
        for c in cases[:10]:
            print("  rank %-3d %-46s %s" % (c["rank"], c["target"][:46], c["q"][:70]))
        print("\nDRY RUN. Re-run with --write to save docs/rerank-eval-hard-v2.json.")
        return 0
    # Even spread across the band, deterministic so two runs compare like with like.
    import random
    if len(cases) > a.cap:
        cases.sort(key=lambda c: c["rank"])
        buckets = {}
        for c in cases:
            buckets.setdefault(min(c["rank"] // 5, 9), []).append(c)
        rng = random.Random(a.seed)
        per = max(1, a.cap // max(len(buckets), 1))
        picked = []
        for b in sorted(buckets):
            rng.shuffle(buckets[b])
            picked += buckets[b][:per]
        cases = sorted(picked, key=lambda c: c["rank"])[:a.cap]
    out = os.path.join(DOCS, "rerank-eval-hard-v2.json")
    json.dump(cases, open(out, "w"), indent=1)
    print("\nwrote %s with %d cases, ranks %d..%d"
          % (out, len(cases), cases[0]["rank"], cases[-1]["rank"]))
    print("NOTE: v2 has NO `accept` sets yet. Single-label recall UNDERSTATES by about 27pp "
          "on this corpus (measured), so read the misses and add acceptable answers before "
          "treating its number as a quality figure. See "
          "feedback_a_known_item_metric_scores_a_better_answer_as_a_miss.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
