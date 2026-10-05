#!/usr/bin/env python3
"""A/B a candidate RERANK_PROMPT against the live one, through PRODUCTION search().

WHY IT GOES THROUGH search() AND NOT A COPY OF THE RANKING CODE. An earlier version of this
measurement reimplemented the candidate pipeline and scored a path that production never
runs, which is the failure recorded in feedback_a_branch_that_never_ran_is_documentation_not
_behaviour. So this imports memory-search.py as a module, swaps the module-level
RERANK_PROMPT constant, and calls the real search() - the same function the hook calls.

TWO SETS, AND BOTH ARE REQUIRED. docs/eval-endtoend.json is a general sample;
docs/rerank-eval-hard.json holds cases where the target sits at lexical rank 4-50, which is
the only population the reranker can act on at all. MEASURED: TOP_K=2 scored +2.5pp on the
general set and -15.4pp on the HARD set, so a change judged on the general set alone can be
a regression in the only place the component matters. See
feedback_a_random_eval_sample_cannot_measure_a_component_that_only_acts_on_hard_cases.

Runs ~66 queries per arm, about 2 minutes, which keeps it inside the under-10-minute
iteration budget in feedback_short_ab_evals_during_development.

    python3 ab-rerank.py                      # baseline only, both sets
    python3 ab-rerank.py --cand cand.txt      # A/B the prompt in that file
    python3 ab-rerank.py --reps 3             # repeat to see the spread
"""
import argparse
import importlib.util
import json
import os
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(os.path.dirname(SRC), "docs")


def load_search():
    spec = importlib.util.spec_from_file_location(
        "memsearch", os.path.join(SRC, "memory-search.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["memsearch"] = m
    spec.loader.exec_module(m)
    return m


def norm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def score(ms, cases, k=3):
    """recall@k against the case's ACCEPTABLE ANSWER SET, not one gold label.

    WHY A SET. MEASURED 2026-10-05: scoring one target per question put HARD recall at
    61.5%, and reading all ten misses showed SEVEN returned a memory that genuinely answers
    the question - for "why did my search say those files didn't exist when I was just
    writing to them" it returned the ugrep-shim memory, which is a BETTER answer than the
    gold label. Known-item recall assumes one correct answer, and in a corpus of 1000+
    overlapping lessons that assumption is false and gets worse as the corpus grows. Tuning
    against the single-label score optimises toward an arbitrary label; see
    feedback_a_known_item_metric_scores_a_better_answer_as_a_miss.

    `accept` lists alternatives judged by reading the question and the memory together. The
    three cases with no alternatives are REAL misses and still score as such.

    Target matching is normalised and SUBSTRING-based in both directions, because the eval
    files name targets by slug, by frontmatter name, and occasionally by a prose label. A
    stricter equality check reported misses that were hits, which is an instrument bug that
    looks exactly like a quality regression.
    """
    hits, misses = 0, []
    for c in cases:
        # Reproduce the hook's four lines exactly (memory-search.py around line 991):
        # make_terms, the MIN_QUERY_TERMS gate, the FTS5 OR expression, then search().
        # The first argument is a MATCH expression, NOT the raw prompt - passing the prompt
        # raised "fts5: syntax error near ?" and would have scored a path production never
        # takes. Keep ensure_fresh too, or the index can be stale for the whole run.
        terms = ms.make_terms(c["q"])
        if len(terms) < ms.MIN_QUERY_TERMS:
            misses.append((c["q"][:70], "BELOW MIN_QUERY_TERMS - hook would not fire"))
            continue
        query = " OR ".join('"%s"' % t for t in terms)
        got = ms.search(query, terms, raw_prompt=c["q"]) or []
        names = []
        for row in got:
            names.append(norm(row[0] if not isinstance(row, dict) else row.get("name", "")))
            if not isinstance(row, dict) and len(row) > 4:
                names.append(norm(os.path.basename(str(row[4]))[:-3]))
        wanted = [norm(c["target"])] + [norm(x) for x in c.get("accept", [])]
        if any(t and (t in n or n in t) for t in wanted if t for n in names if n):
            hits += 1
        else:
            misses.append((c["q"][:70], c["target"][:50]))
    return hits, len(cases), misses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cand", help="file holding a candidate RERANK_PROMPT (python %%s format)")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--show-misses", action="store_true")
    a = ap.parse_args()

    sets = {"general": json.load(open(os.path.join(DOCS, "eval-endtoend.json"))),
            "HARD": json.load(open(os.path.join(DOCS, "rerank-eval-hard.json")))}
    # HARDv2 is rebuilt against the CURRENT corpus by build-rerank-eval.py, so every case
    # is genuinely in the reranker's zone (target at lexical rank 4-50 right now). The v1
    # set had decayed to only 10 of 26 still in band and is kept for continuity, not trust.
    v2 = os.path.join(DOCS, "rerank-eval-hard-v2.json")
    if os.path.exists(v2):
        sets["HARDv2"] = json.load(open(v2))
    arms = {"baseline": None}
    if a.cand:
        arms["candidate"] = open(a.cand, encoding="utf-8").read()

    results = {}
    for arm, prompt in arms.items():
        for rep in range(a.reps):
            ms = load_search()           # fresh module per rep: no cached state between arms
            if prompt:
                ms.RERANK_PROMPT = prompt
            files = ms.source_files()
            if files:
                ms.ensure_fresh(files)   # the hook does this before every search
            for name, cases in sets.items():
                h, n, misses = score(ms, cases)
                results.setdefault((arm, name), []).append((h, n))
                print("  %-10s %-8s rep%d  %d/%d = %.1f%%"
                      % (arm, name, rep + 1, h, n, 100.0 * h / n))
                if a.show_misses and misses:
                    for q, t in misses[:8]:
                        print("        MISS  %-70s -> %s" % (q, t))

    print("\nSUMMARY")
    for (arm, name), reps in sorted(results.items()):
        rates = [100.0 * h / n for h, n in reps]
        print("  %-10s %-8s %s%s"
              % (arm, name, "  ".join("%.1f%%" % r for r in rates),
                 "   spread %.1fpp" % (max(rates) - min(rates)) if len(rates) > 1 else ""))
    if "candidate" in arms:
        print("\n  DELTA (candidate minus baseline), and the HARD set is the one that counts:")
        for name in sets:
            b = sum(h for h, _ in results[("baseline", name)]) / max(
                sum(n for _, n in results[("baseline", name)]), 1) * 100
            c = sum(h for h, _ in results[("candidate", name)]) / max(
                sum(n for _, n in results[("candidate", name)]), 1) * 100
            print("    %-8s %+.1fpp  (%.1f%% -> %.1f%%)" % (name, c - b, b, c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
