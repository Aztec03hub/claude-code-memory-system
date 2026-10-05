# Evaluation rubric: what a non-stock memory system has to beat
Derived from failures of the STOCK system MEASURED in this session, 2026-09-10.
Not from a vendor's feature list - from things that actually went wrong on this machine.

## The stock system's measured failure modes

1. **Silent truncation at load.** Only the first 200 lines OR 25KB of `MEMORY.md` loads,
   whichever comes first. Past that: dropped, no marker, no warning. Confirmed by docs and by
   the harness's own post-write hook. A correctly written, correctly indexed memory was
   unreachable purely because of where it sat in the file.
   → **Criterion A: does recall degrade silently, or does the system tell you it dropped something?**

2. **Fixed budget, unbounded corpus.** 553 memory files against a ~25KB loaded index. The index
   is a fixed budget; the corpus only grows. Every new memory spends bytes taken from an
   existing one, and if nobody chooses, the file chooses by dropping the newest.
   → **Criterion B: does it scale past the point where the whole index stops fitting in context?**

3. **Hook rot / undiscoverability.** 10 index entries are abbreviated to the point of carrying no
   information (`" - MEASURED"`, 11 of 105 chars). The memory exists, is indexed, and is still
   effectively unfindable.
   → **Criterion C: is retrieval by RELEVANCE (semantic/graph), or by whatever text fits on one line?**

4. **Recall is all-or-nothing at session start.** Everything loads up front or not at all; there
   is no query-time retrieval. Topic files load only if I happen to decide to read them.
   → **Criterion D: can it retrieve on demand, mid-task, based on what I'm actually doing?**

5. **Compaction does not rescue it.** Auto memory reloads after `/compact` - but from disk, under
   the same 200-line/25KB cut. Compaction is not a second chance at a truncated index.
   → **Criterion E: what happens to memory across compaction and long sessions?**

6. **Wrong-axis calibration went unnoticed for ~3 months.** The limit was believed to be 400
   lines, then <15KB, then 24,291 bytes, before the docs settled it at 200 lines / 25KB. Nothing
   in the system surfaced the real constraint.
   → **Criterion F: is the actual limit observable, or do you have to reverse-engineer it?**

## Cross-cutting questions for every candidate

- **Integration cost**: what does wiring it in actually require, and what breaks when it's down?
- **Locality**: local-only, or does it ship my memories to a third party? (These files contain
  work context and Phil's corrections - non-trivial.)
- **Embedding dependency**: does recall need a running embedding service / API budget?
- **Write path**: who decides what's worth saving - me, a heuristic, or everything?
- **Failure mode**: when it breaks, does it fail loud or quietly return nothing? A memory system
  that silently returns empty is worse than none, because it reads as "no such memory exists".
- **Migration**: can 553 existing markdown memories move in without a rewrite?

## Honest baseline

Stock + disciplined two-tier index is *working* here, with known sharp edges now gated by
`check-memory-index.py`. The bar is not "is there something fancier" - it is "does the candidate
beat a maintained two-tier markdown index enough to justify a dependency in the recall path."
A finding of "no" is a real result.
