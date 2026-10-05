# Memory System

Query-time retrieval and a self-improving curator over Claude Code's auto-memory corpus.

Built 2026-09-10/11. Every number in this document was measured on the running system.

This is one person's working system, published as-is rather than packaged. It is five hook
entries, three Python files with no dependencies beyond the standard library, and a Gemini
API key it can run without. There is no installer; the numbers, the design arguments and
the recorded mistakes are the useful part.

## Running it yourself

1. Point the hooks at `src/memory-search.py` - copy-paste JSON in
   [`docs/hooks-wiring.md`](docs/hooks-wiring.md).
2. `cp src/loaded-tier.config.example.json src/loaded-tier.config.json` and put your own
   slugs in it. That file is which of YOUR memories must apply unconditionally; the example
   explains the one selection question that decides it.
3. Optional: put a Gemini key in `~/.gemini-key` (mode 600) for the reranker and the
   curator's offline organs. Without it the lexical hot path runs unchanged - every LLM call
   in this system fails open, and `organ_health.py` tells you at `SessionStart` when one is
   down rather than letting it rot silently.
4. The eval sets in `docs/` are keyed to the author's corpus, so their recall numbers mean
   nothing against yours. `src/build-rerank-eval.py` regenerates an equivalent set from any
   corpus; read the noise-floor note in `src/cand-terse.txt` before believing a delta.

Two caveats worth knowing before you read on: paths are hardcoded for a single machine, and
the whole thing is tuned against one 1000-memory corpus, so the design arguments generalise
further than the thresholds do.

---

## The problem it solves

Claude Code loads only the **first 200 lines or 25KB** of `MEMORY.md`, whichever comes first:

```
2,464,932 bytes of memory on disk   (559 files)
   24,012 bytes reach a session
        = 1%
```

The other 99% is written, correct, indexed, and **never offered**. That has teeth: a
PHIL-LOCKED rule ("never use em-dashes") lived only in the unloaded tier and was violated
for an entire session because nothing could surface it.

**Markdown files remain the single source of truth.** Everything here is derived and
disposable: delete `.index/` and it rebuilds.

---

## What it achieves

Measured on fresh paraphrased known-item questions over the full corpus:

Measured through the production `search()`, on paraphrased known-item questions generated
from memories the question never quotes.

**Current, 2026-09-11**, corpus at 579 memories. Two populations, because one is not
enough (see gap 1):

| configuration | general n=40 | HARD n=26 | median |
|---|---|---|---|
| lexical only, `MEMORY_RERANK=0` | 67.5% | - | 27ms |
| rerank, first prompt | 77.5% | - | 656ms |
| rerank, + "specific over general" + no-single-word | 85.0% | 76.9% | ~730ms |
| + doc2query questions shown to the reranker | **85.0%** | **80.8%** | ~750ms |

The last two rows were measured back-to-back; rows from different times are NOT comparable,
because the curator rewrites `weights.json` on every Stop hook and the corpus grows under
the harness. Always A/B in one run.

**Earlier run, n=20**, which is what this document used to report: 45% BM25 baseline, 75%
with doc2query, 85% with rerank. Those numbers do not reproduce on the larger sample and the
n=40 figures above supersede them. The n=20 set was too small to separate a 10-point effect
from sampling, and the corpus has since grown by 14 memories, which adds competition. The
one row not re-measured is the no-doc2query baseline: the ablation needs an index rebuilt
without the `queries` column, which would disturb the live index for a number nothing rests
on. Treat 45% as indicative, not current.

What survives both runs is the shape. **doc2query fixes reachability** (does the memory enter
the candidate list at all) and does the larger share of the work at zero hot-path cost.
**Reranking fixes ordering** (does it reach the top 3) and buys ~10 points for ~600ms.
Neither subsumes the other.

The ceiling is known: BM25 recall@50 was 83%, so reordering a candidate list is close to
exhausted. Further gains need a wider net, not better ranking.

---

## Flow

```
  you type a prompt
        |
   [UserPromptSubmit]  src/memory-search.py
        |  reject machine turns / sub-3-term prompts
        |  FTS5 BM25 over 559 memories, incl. the doc2query `queries` column
        |  apply learned per-memory weights
        |  LLM rerank the top 50  (fails open to lexical order)
        |  occasionally inject an EXPLORATION pick
        v
   up to 3 memories injected
        |
   Claude answers
        |
   [Stop]  record the turn, trigger the curator on any backlog
        v
   [memory-curator.py cycle]   offline, detached, nobody waiting
        |  accumulate up to 45s so batches fill
        |  judge    did the response actually USE each memory?
        |  supersede queue contradiction proposals (corpus walked in slices)
        |  propose  did this turn contain a lesson? queue it, or HEAT an existing one
        |  enrich   doc2query for memories written since last cycle
        |  tune     recompute per-memory weights
        |  prune    retention
        v
   weights.json + doc2query.json  ->  feed the next retrieval
   proposals-open.json            ->  surfaced at SessionStart
```

---

## Layout

```
~/claude_projects/memory-system/
  README.md
  src/memory-search.py            <- the hot path; settings.json points here
  src/memory-curator.py           <- the resident; offline only
  src/organ_health.py             <- shared LLM-outage signal (curator + reranker)
  src/rebuild-loaded-tier.py      <- rebuilds MEMORY.md by function, not by rank
  src/loaded-tier.config.json     <- WHICH memories are unconditional (yours, gitignored)
  src/test_*.py                   <- the self-checks; each runs standalone
  docs/                           plan, rubric, hook wiring, eval sets
  artifacts/                      source of the published research page
  private/                        local only, never committed
```

The index guard lives with the data it guards, not here:
`<memory-dir>/check-memory-index.py` plus its `test_index_guard.py`.

State lives under `<memory-dir>/.index/`:

| file | what | recoverable? |
|---|---|---|
| `memories.db` | FTS5 index | yes, from files + sidecar |
| `doc2query.json` | generated questions per memory | yes, by re-running `enrich` |
| `weights.json` | learned per-memory multipliers | yes, from labels |
| `turns.jsonl` | **raw evidence**: prompt, injected, response | **NO** |
| `labels.jsonl` | judge verdicts per injection | **NO** |
| `supersede-decided.jsonl` | rulings, permanent | **NO** |
| `supersede-pending.jsonl` | proposals awaiting a ruling | no |
| `mem-proposals.jsonl` | **raw evidence**: proposed memories and their occurrences | **NO** |
| `proposals-open.json` | rollup the hook renders; the curator owns the heat number | yes |
| `curator-notes.md` | what the curator has learned | no |
| `errors.log` | recorded faults | low value |
| `recall.log`, `seen/`, `pending/` | scratch | disposable |
| `/tmp/claude-memory-curator.live` | live progress | disposable by design |

Five hook entries in `~/.claude/settings.json` (backups as `settings.json.bak-*`):
`UserPromptSubmit` retrieves, `Stop` records and triggers, `SessionStart` surfaces organ
health, pending proposals and recent errors, `SessionEnd` is a tail-catcher, and
`PostToolUse` enforces the index invariants. Copy-paste JSON in
[`docs/hooks-wiring.md`](docs/hooks-wiring.md).

---

## Commands

```bash
C=~/claude_projects/memory-system/src/memory-curator.py

python3 $C status        # corpus, traffic, judgment, curator memory
python3 $C live          # what in-flight runs are doing; lock HELD or FREE
python3 $C report        # what the labels say, incl. exploration-arm comparison

python3 $C cycle         # judge + supersede + enrich + tune + prune (what hooks fire)
python3 $C judge         # label unjudged turns
python3 $C enrich        # doc2query for memories lacking it (incremental)
python3 $C tune --apply  # recompute weights

python3 $C gaps          # cluster what retrieval keeps FAILING to surface
python3 $C evaluate      # replay labelled turns against candidate constants
python3 $C curate-hooks  # rewrite index hooks cut mid-thought (--apply to write)
python3 $C prune         # retention

python3 $C pending       # supersession proposals
python3 $C decide <id> keep-a|keep-b|merge|both-stand --note '...'

python3 $C propose       # scan turns for durable lessons (--dry writes nothing)
python3 $C proposed      # proposed memories and their heat
python3 $C approve <id> create|revise|reject|hold --note '...'
```

`MEMORY_RERANK=0` disables the hot-path reranker without editing code.

---

## Design decisions, and the evidence

**Lexical, not embeddings.** `pond`'s blinded A/B over 1,126 real calls (fts 61% vs vector
37%, McNemar p<0.05); `vshulcz`'s six-system benchmark ("BM25 basically ties embeddings at
1/100th the cost"); and arXiv 2606.26511, which measured cosine similarity separating a
*contradicted* fact from a *duplicated* one at **AUROC 0.59, near chance**. Embeddings are
structurally blind to the distinction that matters most in a corpus corrected in place.

**doc2query over query rewriting.** The failure was that a natural question shares almost no
vocabulary with a terse lesson. Rewriting the query at request time costs latency on every
prompt; generating the questions each memory *answers*, offline, costs nothing at request
time and fixes the same thing. Questions live in a **sidecar**, never in the `.md` files, so
enrichment cannot mutate the corpus.

**Rerank before the gates.** The coverage and relative-cut gates were tuned against BM25's
ordering and would discard exactly the candidates the reranker exists to rescue. Measured:
those gates roughly halve recall@3.

**The hot path always exits 0.** `UserPromptSubmit` exit code 2 *blocks the prompt and erases
it*. Failing open costs relevance; failing closed costs a turn.

**Nothing is silently swallowed.** 38 handlers record type, message, failing `file:line` and
a context tag to `errors.log`, and `SessionStart` surfaces faults from the last 24h. Expected
absences (`FileNotFoundError`, lock contention) are filtered: an alert that fires on healthy
behaviour is an alert that gets ignored.

**Retries where waiting is free, none where it is not.** Offline calls retry transient
failures (408/429/500/502/503/504, URLError, timeouts) with exponential backoff, never on
400. The hot-path reranker passes `retries=0` and fails open instantly. A truncated JSON
reply is a *size* problem, so it recovers by **splitting the batch**, not by retrying the
same call.

**Coverage gate, not a score floor.** An absolute BM25 floor failed its negative control:
"what is the weather in tokyo today" returned 6 memories matching on `today` alone. Scores
are not comparable across queries.

**Machine turns rejected.** 35% of firings were task notifications and cross-session
messages arriving through `UserPromptSubmit`, injecting the same junk repeatedly.

**Repeat injections collapse to a pointer.** 51% of injections were redundant. A repeat costs
~90 bytes instead of ~700, and the full text returns after compaction, since compaction
replaces the conversation with a summary.

**Exploration is deliberate.** 15% rank-based plus 5% random, each tagged. Without it the
usefulness record could only judge what BM25 already chose, a closed loop that confirms
whatever it does.

**Weights bounded `[0.80, 1.25]`, 3-injection minimum.** No evidence can bury a memory
outright, and exploration keeps resurfacing demoted ones.

**The lock is the rate limiter.** Not a threshold, not a cooldown. A spawn that cannot take
the lock exits in ~50ms; contention becomes batching.

---

## Autonomy boundary

Two things can change the corpus itself. Neither does so on one observation.

**Supersession never auto-applies.** Acting on a false contradiction destroys a memory, and
this corpus is deliberately full of related-but-distinct lessons. Proposals queue and wait
for a ruling.

**Memory creation is gated on HEAT, not on a single judgment.** The `propose` organ reads
the same turns the judge reads and asks whether any contained a durable lesson. It never
writes on the spot. Each occurrence adds heat: the first in a given session is worth 1.0,
each repeat within that same session 0.25. At 3.0 the memory is written automatically.

That asymmetry is the whole design. A problem that persists across turns is real evidence,
so within-session repeats must count for something; but a single long session about one
incident must not manufacture a memory out of that incident restated eight times. At these
numbers three separate sessions create; one session on its own needs nine turns.

A proposal accumulates evidence rather than a draft. The body is generated from **every**
occurrence at creation time, never from the first one: what generalises across occurrences
is the memory, and what is specific to one of them is an example at most.

Before anything is queued, two checks run, in this order and never merged:

1. **Already proposed?** Match it and heat it. This runs first and alone, so the corpus
   check below can never suppress a heat-up.
2. **Already in the corpus?** Then it is a *refinement* of a memory that exists, recorded as
   `refine` against that memory. Refinements never auto-create; the right action is editing
   the existing file, which is a human call.

The corpus check uses **containment**, not Jaccard. MEASURED on the first run: an 8-token
proposal fully contained in a 40-token memory description scores ~0.2 by Jaccard and sails
through as new. `memory-index-truncation` passed that check against
`feedback_memory_index_ceiling_400`, which says exactly the same thing.

Four verbs, because "correct but not yet proven" and "this belongs in a memory we already
have" are both real verdicts: `create` writes it now, `revise` folds a refinement into the
memory it refines, `reject` closes it permanently, and `hold` clears the heat without closing
the question, so a genuine recurrence re-opens it from scratch.

**`refine` has a terminal state.** At `HEAT_CREATE` a refinement ESCALATES: it stops
accumulating evidence, stops costing an API call per cycle, and is surfaced as an EDIT
somebody needs to make. MEASURED 2026-09-30, why this exists: one refinement reached 118
occurrences and heat 30.25, re-proposed every cycle for weeks, and never once asked anyone
to do anything, because `refine` never auto-created (correct) and nothing closed it either.

`revise` sends the evidence ranked by NOVELTY against the target file, not in list order, and
refuses outright below `REVISE_MIN_NOVELTY`. MEASURED on the first real revision: passing
`occurrences[:12]` handed the model the twelve most REDUNDANT restatements, so it invented a
filler sentence, raised `MEASURED 9x` to `MEASURED 118x`, and missed both genuinely new
facts. Ranked by novelty the same call added exactly those two facts and changed nothing
else. It also backs the file up before writing and refuses any revision that drops a
PHIL-LOCKED or PHIL-ASKED marker.

**No single session can run away with it.** A session contributes at most
`HEAT_SESSION_CAP`. MEASURED: without the cap one session produced heat 30.25 from 118
occurrences (1.0 + 0.25x117), which is ten times the threshold from a single sitting. The cap
preserves both documented bars exactly - nine turns in one session still reaches 3.0, three
separate sessions still reach 3.0 - and only removes the runaway above them.

**Decay is deliberately off.** Heat only accumulates. Decay cannot be designed without a
usage signal to reverse it, and Phil returns to projects after months away; a decay that
silently retired a dormant-but-correct proposal would be indistinguishable from it never
having existed. Periodic manual audits handle retirement today.

**As of 2026-09-11, Phil delegated supersession rulings and memory approvals to Claude
until further notice.** 24 have
been ruled: 17 `both-stand`, 5 `merge`, 2 `keep-b`. The 71% both-stand rate is the point -
the model's *detection* is good but its *adjudication* is weak, so rulings are made by
reading both memories, not by accepting the suggestion. Two standing rules: never collapse
two PHIL-LOCKED rules, and when merging, name what must survive the merge.

---

## Safety properties

- Hook always exits 0.
- All shared appends use `flock`. 24 Claude processes share this corpus and rows exceed the
  4096-byte `PIPE_BUF` atomicity limit. Verified: 12 writers x 25 rows x 6000B, 300/300 intact.
- Index rebuild writes a **per-pid** temp then `os.replace`. A fixed name let concurrent
  rebuilds corrupt each other.
- Curator runs hold an exclusive non-blocking lock; a second run exits rather than queues.
- Labels deduped on read by turn id. A duplicate row can never again cause a re-judge loop.
- Unique `tid` per turn; microsecond timestamps.
- `enrich` rebuilds the index rather than dropping it, so the system is never left invalid.
- **Memory creation is the one write path, and it is fenced.** It refuses to overwrite an
  existing file; it lands its pointer in `MEMORY-FULL.md`, never the loaded `MEMORY.md`
  tier with its hard 200-line / 25KB ceiling; it stamps `auto: true` so every machine-written
  memory is one grep away; it strips em-dashes; and it downgrades any `PHIL-LOCKED` or
  `PHIL-ASKED` wording it tries to manufacture. A standing mandate is a statement about
  what Phil decided, and nothing should be able to invent one.
- Proposal occurrences are keyed on `(session, tid)`, not `tid`. MEASURED: `turn_key()`
  falls back to `ts` for rows written before `tid` existed, and two sessions that wrote a
  turn in the same second collided, silently merging two real occurrences into one.
- The heat number has exactly one writer. MEASURED on the first run: an inline copy of the
  formula in the hook read 3.75 where the curator read 3.00. The curator now publishes
  `proposals-open.json` and the hook only renders it.

---

## Known gaps

1. **Constants are hand-picked, and `evaluate` cannot settle them.** It proposes
   `TOP_K=2, MIN_COVERAGE=3, REL_CUT=0.60` over 71 labelled turns. `TOP_K=2` was applied and
   then reverted, which is worth recording:

   | set | TOP_K=2 | TOP_K=3 |
   |---|---|---|
   | general, n=40 | 80.0% | 77.5% |
   | HARD, n=26 (target at lexical rank 4-50) | 61.5% | **76.9%** |
   | pooled, n=66 | 48 hits | **51 hits** |

   A randomly drawn eval set is mostly easy questions, where the answer is already near the
   top and the third slot is dead weight. The component under test only acts when the answer
   is NOT near the top, and on that population the third slot is where it lands. The general
   sample cannot see the thing being measured.

   `evaluate` points the same wrong way for a related reason: it scores precision with no
   recall term, and any such metric always prefers showing less. Fixing that needs labels for
   memories that were NOT injected, which is what the exploration arm is slowly producing.
2. **Reranker cost/benefit is still a real question.** It buys ~10 points for ~600ms on
   every qualifying prompt; doc2query buys its share for nothing.

   Its prompt was tuned once, and the two measurements disagree about how much that was
   worth. On a 26-question HARD set (target at lexical rank 4-50, the only cases a reranker
   can change) dropping "a short list is the right answer" moved rescue 73.1% -> 80.8% at an
   unchanged average list length of 3.0, so it recovers hits rather than padding. On the
   general n=40 set, through the production path, the same change is +1 question: 75.0% ->
   77.5%. Both point the same way and the hard set is the sensitive instrument, but +1 of 40
   is not an effect anyone should quote. Showing each candidate's doc2query questions changed
   nothing alone and cost a point on top; not adopted.

   `RERANK_PROMPT` is a module constant precisely so an A/B swaps it and still runs the real
   `rerank()` and `search()`. Eval sets: `docs/rerank-eval-hard.json`, `docs/eval-endtoend.json`.
3. **Session/working memory.** The `not-retrievable` gap cluster: "keep going" needs
   conversational state, which a memory corpus structurally cannot answer.
3b. **The reranker's semantic failure: diagnosed, and largely fixed.** The case that
   exposed it:

   ```
   prompt   "why do my background jobs finish without telling me anything"
   rank 0   feedback_a_detached_job_never_notifies_me
   picked   agent_briefs_must_mandate_a_completion_report_via_sendmessage,
            agent_teams_messaging_and_lifecycle
   ```

   It read "telling me anything" as messaging. Two guesses failed before anything worked: a
   doc2query-coverage gate (no discrimination, 0.33 vs 0.18 median) and a lexical-order
   prior (+1 hard, -2 general, did not fix the case). Guessing stopped when I enumerated
   ALL 11 failures and read them.

   **Six of the eleven were not failures.** For "why did my search commands say those files
   didn't exist", the gold was `an_absence_is_a_claim_about_where_i_looked` but the reranker
   returned `bare-grep-is-claude-codes-ugrep-shim`, which literally explains why a search
   misses files. That pick is better than the gold. Known-item recall assumes one correct
   memory per question; this corpus is deliberately full of overlapping lessons, so the
   metric UNDERSTATES the reranker and tuning against it optimises toward an arbitrary label.

   The genuine misses shared one pattern: the reranker drifts to the **general principle**
   and misses the **specific mechanism** asked about. It picked `idempotency-by-design` over
   recoverability for "state permanently broken"; general cost advice over the prewarm
   project for a named 1.5s latency. Two changes, both measured back-to-back on both sets:

   - naming that trap in the prompt ("a general principle is worth less than the SPECIFIC
     mechanism the message names; do not match on one salient word"): **+4/26 hard,
     +2/40 general**, reproduced across 3 reps with zero spread
   - showing the reranker each candidate's doc2query questions: **+1/26 hard**, neutral on
     general, +18ms. Previously rejected, correctly, under a different prompt and corpus;
     re-testing a rejected idea after its conditions change is not thrashing.

   Both original genuine misses now land in the top 3. Still open: the exact phrasing "why
   do my background jobs finish without telling me anything" puts the right memory third
   rather than first, behind two messaging memories.

4. **Hook curation is human-gated, and that is now enforced.** `curate-hooks --apply`
   REFUSES without `--only <stems>` naming the rewrites you read. MEASURED on a real run of
   9: two were materially wrong. One invented a clause ("check coverage") appearing nowhere
   in the memory; one INVERTED its lesson, blaming an agent for ignoring a retraction that
   had never been sent to it. Both would have been written to both index tiers.

   A lexical support check does not catch either, and was tested and rejected: the
   fabricated hook scored 0.71 token-containment against its memory, HIGHER than a correct
   rewrite at 0.60. A wrong claim assembles perfectly well out of right words, so fidelity
   here is not mechanically checkable.

   Nine hooks were repaired by hand this pass. The loaded tier sits at 23,890 bytes and 184
   lines, inside both the 24,000 budget and the hard 200-line / 25KB ceiling.

   A related trap, hit twice: a loaded hook must be a PREFIX of its full-index hook, so a
   loaded hook cannot be improved by rewriting it alone. Fix `MEMORY-FULL.md` first, then
   the loaded tier carries its first clause. `curate-hooks --apply` writes both files and
   is safe; hand-editing `MEMORY.md` alone is what breaks it.

   **2026-09-30: the index entry shape changed and `curate-hooks` moved with it.** An entry
   is now `- [hook](path)`; the hook IS the link text. The old shape carried a display name
   that restated the filename and cost 36 bytes an entry, 26% of a byte-capped file, leaving
   a median hook of 20 characters and 132 of 163 too short to judge relevance by. Dropping it
   took the median to 63 and left the file smaller. `find_stubs` and the `--apply` writer
   read and emit the new shape; both still accept the old one, because MEMORY-FULL.md is
   large and may be hand-edited for a while yet.
5. **`evaluate` measures precision only.** Labels exist only for injected memories.
   Exploration slowly converts unknowns into labels.
6. **Proposal quality, first full ruling pass (23 proposals over 62 turns):** 13 created,
   9 rejected, 1 held. Of the 9 rejects, 6 were restatements of what this system's own code
   and README already say, and 2 were word-for-word duplicates of memories that exist. So
   the signal rate is ~57%, and the dominant failure is self-description, not invention.
   Caveat: the sample is drawn from sessions largely about building this system, which is
   unusually self-referential. Expect the restatement share to fall on ordinary work.
   The cover check caught 1 of the 3 corpus duplicates on its own; one scored 0.45 (which
   moved `COVER_J` down to 0.45) and one was not in BM25's top 3 at all, so the check is
   bounded by candidate generation, not only by its threshold.
7. **Heat constants are hand-picked.** 1.0 / 0.25 / 3.0 was reasoned, not measured. The
   honest test needs proposals observed across many real sessions.
8. **Recurrence is the wrong gate for a `reference`.** The whole mechanism asks "did this
   lesson recur", which is the right question for a `feedback` lesson and the wrong one for a
   concrete external fact. You learn once that DroidGuard uses `dl_iterate_phdr` to detect
   injected libraries; it does not need to happen twice to be true. As of 2026-09-30 that is
   385 of the 638 open proposals, and nothing in the design will ever promote them. They are
   deliberately left open rather than shelved, and the fix is a separate gate for
   `reference`, not a lower threshold for everything.
9. **A backlog is not free.** The propose prompt named every open proposal, which at 848 was
   107KB, roughly 27k tokens, in EVERY call and growing without bound. Now capped at
   `PROPOSE_LISTING_MAX` by heat, 7.6KB, with no loss of matching accuracy: the listing is a
   hint to reuse an exact name, while the authoritative match is a Jaccard check in code that
   still runs against every open proposal.

---

## Current measurements

```
corpus      577 memories indexed, 14 of them written by the propose organ
recall@3    77.5% with rerank, 67.5% without   (n=40, 2026-09-11)
hot path    27ms median lexical, 656ms median when the reranker fires
judgment    ~7% used, ~93% ignored, 0% harmful
proposals   23 ruled: 13 created, 9 rejected, 1 held
```

**93% ignored is not a failure.** It is the baseline nobody in this field has, because nobody
measures whether retrieval helped. It is what everything else has to beat.

---

## Debugging

```bash
python3 ~/claude_projects/memory-system/src/memory-curator.py live
tail -f /tmp/claude-memory-curator.live
tail -20 <memory-dir>/.index/errors.log
tail -5  <memory-dir>/.index/recall.log
```

`live` reports whether the lock is HELD or FREE, which distinguishes a run that is working
from one that is stuck.

## Rollback

Remove the `memory-search.py` entries from `~/.claude/settings.json` and `rm -rf
<memory-dir>/.index/`.

That leaves the corpus untouched with one exception: memories the `propose` organ wrote.
Those are real files and they survive. Find every one of them with:

```bash
command grep -rl 'auto: true' <memory-dir>/*.md
```

Each also has a pointer line in `MEMORY-FULL.md` to remove alongside it.
