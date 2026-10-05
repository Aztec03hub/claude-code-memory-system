#!/usr/bin/env python3
"""memory-search.py - query-time retrieval over the auto-memory corpus.

WHY THIS EXISTS
    Claude Code loads only the first 200 lines / 25KB of MEMORY.md. Measured 2026-09-10:
    2,464,932 bytes across 553 memory files on disk, 24,012 bytes reach a session. 1%.
    The other 99% is written, correct, indexed -- and never offered.

    This hook searches the whole corpus lexically on each prompt and injects the few
    memories that bear on it. Files stay the source of truth; the index is derived and
    disposable (delete .index/ and it rebuilds).

WHY LEXICAL AND NOT EMBEDDINGS
    pond's working paper (1,126 real calls, 63 days, blinded A/B): fts 61% vs vector 37%.
    vshulcz's 6-system bench: "BM25 basically ties embeddings here at 1/100th the cost."
    arXiv 2606.26511: cosine separates a CONTRADICTED fact from a DUPLICATED one at
    AUROC 0.59 -- near chance. Embeddings are structurally blind to supersession.

SAFETY -- THE CRITICAL PROPERTY
    UserPromptSubmit exit code 2 BLOCKS THE PROMPT AND ERASES IT. A stray traceback here
    would eat what the user typed. Therefore: the entire body runs under a BaseException
    catch and this script ALWAYS exits 0, emitting nothing on any failure. Degrading to
    "no memories" is always correct; blocking a turn never is.
"""

import contextlib
import fcntl
import io
import json
import pathlib
import uuid
import os
import re
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import organ_health   # noqa: E402  (sibling module, shared with memory-curator.py)

MEMORY_DIR = os.path.expanduser("~/.claude/projects/-home-plafayette/memory")
INDEX_DIR = os.path.join(MEMORY_DIR, ".index")
organ_health.configure(INDEX_DIR)
DB_PATH = os.path.join(INDEX_DIR, "memories.db")
LOG_PATH = os.path.join(INDEX_DIR, "recall.log")
SEEN_DIR = os.path.join(INDEX_DIR, "seen")
PENDING_DIR = os.path.join(INDEX_DIR, "pending")
TURNS_PATH = os.path.join(INDEX_DIR, "turns.jsonl")
WEIGHTS_PATH = os.path.join(INDEX_DIR, "weights.json")
Q2Q_PATH = os.path.join(INDEX_DIR, "doc2query.json")
PROPOSALS_PATH = os.path.join(INDEX_DIR, "supersede-pending.jsonl")
ROLLUP_PATH = os.path.join(INDEX_DIR, "proposals-open.json")
DECISIONS_PATH = os.path.join(INDEX_DIR, "supersede-decided.jsonl")
LABELS_PATH = os.path.join(INDEX_DIR, "labels.jsonl")
CURATOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory-curator.py")
JUDGE_STAMP = os.path.join(INDEX_DIR, ".last-judge")
# MEASURED FAILURE that killed the previous value: a judge drained and exited, three turns
# landed immediately after, and the cooldown then blocked the re-trigger -- stranding a
# backlog of 6 with nothing running. The lock is the real rate limiter: a spawn that cannot
# take it exits in ~50ms having cost nothing. So this is now only a tight-loop guard.
JUDGE_COOLDOWN = 5
# Trigger on ANY unjudged turn. A threshold was the wrong tool: with two long-running
# sessions it made turns wait for a queue that fills slowly, for no benefit. The LOCK is
# the rate limiter -- if a judge is already draining, this turn is picked up by it, and
# contention becomes batching rather than duplicated work or delay.
JUDGE_BACKLOG = 1

# EXPLORATION. Until a judging agent exists, selection is BM25's alone, and the usefulness
# record can only ever judge what BM25 chose to surface -- a closed loop that would confirm
# whatever it already does. These two arms deliberately inject things BM25 did NOT pick, so
# the record contains counter-evidence from day one.
#   rank  : tests whether the TOP-3 CUTOFF is right (inject something ranked 4-12)
#   random: tests whether BM25 MISSES entirely (inject something it did not match at all)
# A judging agent later replaces random choice with reasoned choice; the labelled data it
# needs to do that is being collected now.
EXPLORE_RANK_RATE = 0.15
EXPLORE_RANDOM_RATE = 0.05

# MEASURED 2026-09-11 through the production search(), TWO populations, one code path:
#
#   set                                      TOP_K=2      TOP_K=3
#   general (n=40, docs/eval-endtoend.json)   80.0%        77.5%
#   HARD (n=26, target at lexical rank 4-50)  61.5%        76.9%
#   pooled (n=66)                             48 hits      51 hits
#
# I set this to 2 on the general set alone and had to put it back. The general set's +1 of
# 40 is noise; the hard set's -4 of 26 is a real effect on exactly the population a reranker
# EXISTS for -- when the answer is not already near the top, the third slot is where it
# lands. A sample drawn at random is mostly easy questions, so it cannot see that.
# `evaluate` also proposes 2, and it is wrong for a related reason: it scores precision with
# no recall term, and any such metric always prefers showing less.
TOP_K = 3
EXCERPT_CHARS = 600
TOTAL_CAP = 3000
MAX_TERMS = 12
MIN_TERM_LEN = 3
# A doc must contain at least this many distinct query terms. This -- not a score floor --
# is what stops a single common word from looking like a match.
MIN_COVERAGE = 2
# A prompt this short carries too little signal to retrieve on. MEASURED from recall.log:
# 11 firings had fewer than 3 content terms ("read", "first", "signed", "keep goin") and
# injected 32 memories between them, all noise. Below this, emit nothing at all.
MIN_QUERY_TERMS = 3
# bm25() returns NEGATIVE scores; more negative = better. Keep only hits within this
# fraction of the best score, so a weak tail never rides behind one strong match.
REL_CUT = 0.45

# Deliberately small. A long list starts discarding domain words that matter here
# ("memory", "index", "hook" are all real search terms in this corpus).
STOPWORDS = {
    "the", "and", "for", "you", "are", "但", "that", "this", "with", "from", "have", "has",
    "was", "were", "what", "when", "where", "which", "how", "why", "can", "could", "would",
    "should", "did", "does", "done", "get", "got", "our", "out", "its", "it's", "your",
    "not", "but", "all", "any", "some", "into", "than", "then", "them", "they", "there",
    "here", "just", "like", "make", "made", "now", "one", "two", "also", "about", "over",
    "see", "say", "said", "let", "lets", "want", "need", "use", "using", "used", "way",
}

# Never index these: already loaded into every session by stock auto-memory, so a hit on
# them is guaranteed to be content Claude already has in context.
EXCLUDE = {"MEMORY.md", "MEMORY-FULL.md"}


ERRORS_PATH = os.path.join(INDEX_DIR, "errors.log")


def _index_sync():
    """Load check-memory-index.py's own sync machinery instead of reimplementing it.

    The repair rule (take the hook text from the file's own `description`, never invent one,
    never rewrite an existing line) lives there and is tested by test_index_guard.py. A second
    copy here would be a second thing to keep correct, and the one that drifts is the one
    nobody runs. The filename is hyphenated, hence the explicit spec load.
    """
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "check-memory-index.py")
    # It derives MEM from CLAUDE_MEMORY_DIR, falling back to its OWN directory - which is
    # this repo, not the corpus. Set it, or the sweep reads a MEMORY-FULL.md that does not
    # exist, finds every memory missing, and "repairs" an index into the source tree.
    os.environ.setdefault("CLAUDE_MEMORY_DIR", MEMORY_DIR)
    spec = importlib.util.spec_from_file_location("check_memory_index", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {"sync": mod.sync, "indexed": mod.indexed, "FULL": mod.FULL, "LOADED": mod.LOADED}


def oops(context):
    """Record a swallowed exception instead of discarding it.

    WHY WE STILL SWALLOW: UserPromptSubmit exit code 2 BLOCKS THE PROMPT AND ERASES IT, so
    an exception must never reach a nonzero exit. That is a hard constraint, not a choice.
    WHAT CHANGED: it is no longer silent. Every catch records type, message, the failing
    line, and a context tag, so a fault is investigable after the fact instead of looking
    identical to "nothing matched" -- which is the single most common bug signature here.

    Never raises. An error in the error handler must not become the error.
    """
    try:
        import traceback
        et, ev, tb = sys.exc_info()
        if et is None:
            return
        # A missing optional file is an EXPECTED state, not a fault: no weights yet, no
        # doc2query sidecar yet, no seen-file for a fresh session. Recording those would
        # flood the log and make the SessionStart warning cry wolf, which is how a real
        # alert gets ignored. Genuine faults still land.
        if et is FileNotFoundError:
            return
        last = "?"
        for fr in traceback.extract_tb(tb) or []:
            last = "%s:%d" % (os.path.basename(fr.filename), fr.lineno)
        line = "%s pid=%-7d %-26s %s: %s  at %s\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%S"), os.getpid(), context,
            et.__name__, str(ev)[:160].replace("\n", " "), last)
        os.makedirs(INDEX_DIR, exist_ok=True)
        with open(ERRORS_PATH, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(line)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        # DELIBERATELY the only silent handler in the system. The bulk instrumentation that
        # added oops() to every other catch also added it HERE, which makes the error
        # recorder recurse into itself the moment it fails. An error in the error handler
        # must not become the error.
        pass


def append_locked(path, text):
    """Append under an exclusive lock.

    MEASURED CONTEXT: 24 Claude Code processes share this corpus on this machine. A plain
    O_APPEND write is only atomic below PIPE_BUF (4096 bytes on Linux), and these rows carry
    up to 4000 chars of response plus 1000 of prompt plus paths, so they WILL cross that
    line and interleave into corrupt JSON. flock costs microseconds and removes the race.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(text)
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        oops("append_locked")
        pass


def parse_frontmatter(text):
    """Minimal YAML-ish reader. No PyYAML dependency -- stdlib only is the whole point.

    Handles the shape this corpus actually uses: a --- fenced block of `key: value`,
    with a nested `metadata:` mapping. Anything unparseable degrades to empty, and the
    caller falls back to the filename.
    """
    out = {}
    if not text.startswith("---"):
        return out, text
    end = text.find("\n---", 3)
    if end == -1:
        return out, text
    head, body = text[3:end], text[end + 4:]
    for line in head.splitlines():
        m = re.match(r"^\s{0,4}([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        v = v.strip('"').strip("'")
        if v and k not in out:
            out[k] = v
    return out, body


def load_q2q():
    try:
        with open(Q2Q_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        oops("load_q2q")
        return {}


def build_index(files):
    os.makedirs(INDEX_DIR, exist_ok=True)
    q2q = load_q2q()
    # Unique per process. A FIXED tmp name lets two concurrent rebuilds write the
    # same file and corrupt each other; os.replace at the end is still atomic.
    tmp = "%s.tmp.%d" % (DB_PATH, os.getpid())
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute(
        # `queries` holds generated questions each memory ANSWERS (doc2query), written by
        # `memory-curator.py enrich` into a sidecar and indexed here.
        # WHY: MEASURED recall@3 on paraphrased natural questions was 2/12 (83% miss).
        # The memories existed; BM25 could not reach them because a question shares almost
        # no vocabulary with a terse lesson description. Indexing the QUESTIONS lets a
        # natural question match a natural question, and keeps all the cost offline.
        "CREATE VIRTUAL TABLE mem USING fts5("
        "name, description, body, queries, path UNINDEXED, mtype UNINDEXED, "
        "modified UNINDEXED, fmtime UNINDEXED,"
        "tokenize='porter unicode61')"
    )
    con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    rows = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                raw = fh.read()
        except OSError:
            oops("build_index")
            continue
        fm, body = parse_frontmatter(raw)
        rows.append((
            fm.get("name") or os.path.basename(path)[:-3],
            fm.get("description", ""),
            body,
            # doc2query sidecar: generated questions this memory answers. Kept OUT of the
            # .md files so enrichment never mutates the corpus, and rebuilt into the index
            # from the sidecar. Empty until `memory-curator.py enrich` runs.
            " ".join(q2q.get(path, [])),
            path,
            fm.get("type", ""),
            (fm.get("modified", "") or "")[:10],
            # MEASURED: 58% of the corpus (325 of 556) predates Claude Code's `modified`
            # frontmatter field, so it has no authored date at all. The filesystem still
            # knows when each file was last written and that spread is real (Mar-Sep 2026),
            # which makes it a usable RECENCY FALLBACK for supersession judgments.
            # Kept in its OWN column, never merged into `modified`, so a filesystem
            # timestamp can never be mistaken for a date the author actually set.
            time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path))),
        ))
    con.executemany("INSERT INTO mem VALUES (?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT INTO meta VALUES ('built', ?)", (str(time.time()),))
    con.commit()
    con.close()
    os.replace(tmp, DB_PATH)  # atomic: a torn build never becomes the live index
    return len(rows)


def source_files():
    out = []
    for fn in os.listdir(MEMORY_DIR):
        if fn.endswith(".md") and fn not in EXCLUDE:
            out.append(os.path.join(MEMORY_DIR, fn))
    return sorted(out)


def ensure_fresh(files):
    """Rebuild when any source file is newer than the index. Full rebuild is sub-second
    at this corpus size, so incremental updating would be complexity with no payoff."""
    newest = max((os.path.getmtime(f) for f in files), default=0)
    if os.path.exists(DB_PATH):
        try:
            con = sqlite3.connect(DB_PATH)
            built = float(con.execute("SELECT v FROM meta WHERE k='built'").fetchone()[0])
            con.close()
            if built >= newest:
                return 0
        except Exception:
            pass  # unreadable/corrupt index -> fall through and rebuild
    return build_index(files)


# Turns that are NOT a human asking something. Task notifications, cross-session messages
# and tool-output blobs all arrive through UserPromptSubmit, and they retrieve on generic
# tokens (message, session, user, output, file), injecting the same junk every time.
# MEASURED from recall.log after one hour live: 13 of 37 firings (35%) were machine turns,
# and they pushed "Anthropic rate-limit response headers" into context 9 times for nothing.
MACHINE_MARKERS = (
    "<task-notification", "task notification", "toolu_",
    "<cross-session-message", "cross-session message", "uds:/run/user/",
    "<system-reminder", "<local-command-stdout", "cc-socks/",
)

# An opaque identifier carries no retrieval signal but counts toward term coverage, which
# is how a machine turn sneaks past the coverage gate. Drop anything that looks like an id.
ID_LIKE = re.compile(r"^(?=.*\d)[a-z0-9_]{8,}$")


def seen_path(sid):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(sid))[:64] or "nosession"
    return os.path.join(SEEN_DIR, safe + ".json")


def load_seen(sid):
    """Paths already injected IN FULL during this compaction window.

    MEASURED before this existed: 51% of all injections were redundant -- the same
    memory pushed into context up to 9 times. Once the text is in context, re-sending
    the body buys nothing.

    Scoped to the compaction window, not the session, because compaction replaces the
    conversation with a summary and the injected text does not survive it verbatim. So
    a SessionStart with source=compact clears this and everything becomes fully
    injectable again.
    """
    try:
        with open(seen_path(sid), encoding="utf-8") as fh:
            return set(json.load(fh))
    except Exception:
        oops("load_seen")
        return set()


def save_seen(sid, seen):
    try:
        os.makedirs(SEEN_DIR, exist_ok=True)
        with open(seen_path(sid), "w", encoding="utf-8") as fh:
            json.dump(sorted(seen), fh)
    except Exception:
        oops("save_seen")
        pass


def reset_seen(sid):
    try:
        os.remove(seen_path(sid))
    except Exception:
        oops("reset_seen")
        pass


def prune_seen(days=3):
    """State files are per-session and accumulate forever otherwise."""
    try:
        cutoff = time.time() - days * 86400
        for fn in os.listdir(SEEN_DIR):
            fp = os.path.join(SEEN_DIR, fn)
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
    except Exception:
        oops("prune_seen")
        pass


def pending_path(sid):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(sid))[:64] or "nosession"
    return os.path.join(PENDING_DIR, safe + ".json")


def write_pending(sid, rec):
    """Stash what this turn injected, so the Stop hook can pair it with the response.

    UserPromptSubmit knows WHAT was injected; only Stop knows what the model then SAID.
    Neither half is evidence alone -- the pair is."""
    try:
        os.makedirs(PENDING_DIR, exist_ok=True)
        with open(pending_path(sid), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
    except Exception:
        oops("write_pending")
        pass


def close_turn(sid, response):
    """Pair the pending injection record with the response and append one turn row.

    Deliberately records NO judgment. Whether the response actually drew on a memory is a
    reasoning task for a later agent; guessing it here with a keyword heuristic would bake
    a weak proxy into the ground truth everything else gets tuned against."""
    try:
        with open(pending_path(sid), encoding="utf-8") as fh:
            rec = json.load(fh)
        os.remove(pending_path(sid))
    except Exception:
        oops("close_turn")
        return
    rec["response"] = (response or "")[:4000]
    rec["response_len"] = len(response or "")
    rec["closed"] = time.strftime("%Y-%m-%dT%H:%M:%S.") + ("%06d" % (time.time() % 1 * 1e6))
    try:
        append_locked(TURNS_PATH, json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        oops("close_turn")
        pass


def explore(con, chosen_paths, terms):
    """Return (extra_hit, kind) or (None, None). See EXPLORE_* for why this exists."""
    try:
        r = random.random()
        if r < EXPLORE_RANDOM_RATE:
            row = con.execute(
                "SELECT name, description, mtype, modified, path, '', 0.0 FROM mem "
                "WHERE path NOT IN (%s) ORDER BY RANDOM() LIMIT 1"
                % ",".join("?" * len(chosen_paths)), list(chosen_paths)).fetchone()
            return (row, "random") if row else (None, None)
        if r < EXPLORE_RANDOM_RATE + EXPLORE_RANK_RATE:
            q = " OR ".join('"%s"' % t for t in terms)
            rows = con.execute(
                "SELECT name, description, mtype, modified, path, '', "
                "  bm25(mem,3.0,5.0,1.0,6.0,0,0,0,0) FROM mem WHERE mem MATCH ? "
                "ORDER BY 7 LIMIT 12", (q,)).fetchall()
            pool = [x for x in rows if x[4] not in chosen_paths]
            return (random.choice(pool), "rank") if pool else (None, None)
    except Exception:
        oops("explore")
        pass
    return None, None


def maybe_judge():
    """Fire the curator's judge in the BACKGROUND when a session ends.

    This is what makes the curator resident rather than a CLI someone remembers to run:
    turns get labelled while they are fresh, so the evidence base grows on its own.

    Three safety properties, because this runs inside a hook:
      - fully detached, output discarded: the hook never waits on it and never sees it fail
      - cooldown: a judge run costs API calls, so at most one per JUDGE_COOLDOWN
      - wrapped: any failure here degrades to "no judging", never to a broken hook
    """
    try:
        if not os.path.exists(CURATOR):
            return
        now = time.time()
        try:
            if now - os.path.getmtime(JUDGE_STAMP) < JUDGE_COOLDOWN:
                return
        except OSError:
            oops("maybe_judge")
            pass
        # Count what is actually UNJUDGED, not total turns. Comparing turns to labels is
        # the real backlog; counting all turns meant the threshold stopped applying after
        # the third turn ever recorded.
        # Must agree with the curator's definition of judgeable, or the hook triggers on a
        # backlog the judge will not process and the spawn is wasted every single turn.
        def keys(p, need_inj):
            out = set()
            try:
                with open(p, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            d = json.loads(line)
                        except ValueError:
                            oops("keys")
                            continue
                        if need_inj and not d.get("injected"):
                            continue
                        out.add(d.get("tid") or d.get("ts"))
            except OSError:
                oops("keys")
                pass
            return out
        if len(keys(TURNS_PATH, True) - keys(LABELS_PATH, False)) < JUDGE_BACKLOG:
            return
        # NOTE: the cooldown stamp is deliberately NOT written here. This process only
        # SPAWNS the worker; it may lose the lock race and do nothing. Stamping on spawn
        # meant 23 of 24 losers burned a 30-minute cooldown having done no work, deferring
        # every turn recorded after the winner's read. The worker stamps on completion.
        import subprocess
        subprocess.Popen(
            [sys.executable, CURATOR, "cycle"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        oops("keys")
        pass


def is_machine_turn(prompt):
    low = prompt.lower()
    return any(m in low for m in MACHINE_MARKERS)


def make_terms(prompt):
    """Reduce prose to FTS5-safe terms.

    The raw prompt CANNOT go to MATCH: FTS5 treats " * : ^ - AND OR NOT as syntax and
    raises on ordinary English. Strip to [a-z0-9_], drop short/stop words, quote each
    term so nothing is reinterpreted, OR them together.
    """
    toks = re.findall(r"[A-Za-z0-9_]+", (prompt or "").lower())
    seen, terms = set(), []
    for t in toks:
        if len(t) < MIN_TERM_LEN or t in STOPWORDS or t in seen or ID_LIKE.match(t):
            continue
        seen.add(t)
        terms.append(t)
        if len(terms) >= MAX_TERMS:
            break
    return terms


# RERANKER. MEASURED on 18 paraphrased known-item questions:
#     recall@3  33%   <- BM25 alone, what ranking gives you
#     recall@50 83%   <- the right memory IS in the candidate list, just ranked low
# So the failure is RANKING, not retrieval, and reordering 50 candidates is worth ~2.5x.
# A reranker can never exceed recall@DEPTH, which is why the depth is measured not guessed.
RERANK = os.environ.get("MEMORY_RERANK", "1") != "0"
RERANK_DEPTH = 50        # 100 buys only +6% for double the prompt
RERANK_MIN_CANDIDATES = 6   # below this, BM25's top-3 IS nearly the whole list
RERANK_TIMEOUT = 2.5     # per-socket-operation timeout passed to urlopen
# TRUE wall-clock ceiling for the whole rerank, enforced by a thread join. urlopen's own
# timeout is per-operation, which let one measured turn reach 3,308ms.
RERANK_DEADLINE = 2.0
RERANK_MODEL = "gemini-3.1-flash-lite"


# MEASURED over 30 paraphrased questions: when the top hit dominates rank 4 by this much,
# lexical ranking was ALREADY correct every time. Skipping the LLM there saves 23% of calls
# and cost zero recall on that sample. Tuned conservatively on purpose: 0.40 saved 33% but
# lost one rescue, 0.30 saved 60% and lost two. Revisit with more data.
CONFIDENT_DOMINANCE = 0.50


def lexically_confident(kept):
    """True when BM25's top hit is so far ahead that the reranker has nothing to fix.

    This does NOT disable the reranker -- it stops paying 500ms on the half of prompts where
    reranking measurably changed nothing. When in doubt it returns False and the LLM runs.
    """
    try:
        if len(kept) < 4:
            return False
        top, fourth = kept[0][6], kept[3][6]
        return abs(top - fourth) / max(abs(top), 1e-9) >= CONFIDENT_DOMINANCE
    except Exception:
        oops("lexically_confident")
        return False


def _with_deadline(fn, seconds):
    """Run fn under a HARD wall-clock deadline, returning None if it overruns.

    MEASURED: a turn took 3,308ms despite RERANK_TIMEOUT=2.5. urlopen's timeout bounds each
    SOCKET OPERATION, not total elapsed time, so a slow multi-read response overruns it
    freely. On a hook that sits in front of every prompt, "usually about 2.5s" is not a
    guarantee - this makes it one.

    The abandoned thread is a daemon: it cannot block interpreter exit, and the hook is a
    short-lived process anyway.
    """
    import threading
    box = {}

    def run():
        try:
            box["v"] = fn()
        except Exception:
            box["v"] = None

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        return None          # overran: fail open to the lexical ranking
    return box.get("v")


# The reranker's instruction, lifted to a constant so a prompt A/B can swap it and still
# exercise the PRODUCTION rerank() and search(). A harness that reimplements the call is
# measuring the harness. The closing clause is load-bearing: the earlier wording, "most
# candidates are irrelevant; a short list is the right answer", MEASURED 73.1% rescue on a
# 26-question hard set against 80.8% for this one, at an unchanged average list length of
# The key used to come from os.environ ONLY. MEASURED 2026-09-21..10-05: 340 calls died with
# "GEMINI_API_KEY not set" because it is exported from ~/.bashrc, which bash sources for
# INTERACTIVE shells. The hook spawns this file with Popen(start_new_session=True) and no
# env= argument, so it inherits whatever the Claude Code process happened to have - which
# depends on how that session was launched. Sessions started outside an interactive bash had
# no key and every LLM organ in them failed, while the Bash tool in the same session had it,
# which is why this looked intermittent and impossible to reproduce by hand.
# The file is the fallback because it does not depend on a launcher. NOT settings.json: that
# is mode 644, world-readable, and this is a credential.
KEY_FILE = os.path.expanduser("~/.claude/.gemini_key")


def gemini_key():
    """Env first (so an explicit export still wins), then the key file. Never raises."""
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(KEY_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


# 3.0 - so it recovers hits rather than padding.
RERANK_PROMPT = (
    "Pick the memories that would genuinely help answer this developer's message.\n\n"
    "MESSAGE: %s\n\nCANDIDATES:\n%s\n\n"
    "Two traps. A memory that states a general principle about the same area is worth less "
    "than one about the SPECIFIC mechanism the message names. And do not match on one "
    "salient word: decide what the developer is actually doing, then pick for that.\n\n"
    "Return ONLY a JSON array of at most %d indices, most useful first. Judge by "
    "whether the memory would CHANGE the answer, not by topical overlap. Return [] "
    "if none would. Include any memory that plausibly bears on the message - a missed "
    "relevant memory costs more than an extra one.")


# Show each candidate's doc2query questions to the reranker, not just its description.
# The questions are already generated, already indexed at the highest bm25 weight, and were
# being hidden from the one component whose whole job is matching a question to a memory.
RERANK_SHOW_QUERIES = True


def _candidate_line(i, c):
    line = "%d. %s: %s" % (i, c[0], re.sub(r"\s+", " ", (c[1] or c[5] or ""))[:200])
    if RERANK_SHOW_QUERIES and len(c) > 7 and c[7]:
        line += "\n   also answers: %s" % re.sub(r"\s+", " ", c[7])[:200]
    return line


def rerank(prompt, candidates, k):
    """Reorder BM25 candidates with an LLM. Returns None to mean 'use BM25's order'.

    FAILS OPEN, ALWAYS. Any error, timeout, missing key or unparseable reply returns None
    and the caller keeps the lexical ranking. This sits in front of every prompt, so a
    degraded reranker must cost relevance, never a turn.
    """
    key = gemini_key()
    if not key or len(candidates) < RERANK_MIN_CANDIDATES:
        return None
    try:
        import urllib.request
        listing = "\n".join(
            _candidate_line(i, c) for i, c in enumerate(candidates))
        body = json.dumps({"contents": [{"parts": [{"text":
            RERANK_PROMPT % (prompt[:500], listing, k)}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 80}}).encode()
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
            % RERANK_MODEL, data=body,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=RERANK_TIMEOUT) as r:
            d = json.loads(r.read())
        txt = d["candidates"][0]["content"]["parts"][0]["text"]
        m = re.search(r"\[.*?\]", txt, re.S)
        if not m:
            organ_health.record("rerank", False, "bad_response", "no JSON array in reply")
            return None
        idx = [i for i in json.loads(m.group(0))
               if isinstance(i, int) and 0 <= i < len(candidates)]
        organ_health.record("rerank", True)
        return [candidates[i] for i in idx[:k]]
    except Exception as e:
        # THE MOST DANGEROUS ORGAN TO LOSE QUIETLY. This function fails OPEN by design: the
        # caller keeps the lexical ranking and the turn is unaffected, so a dead reranker
        # has NO symptom. Retrieval quality just drops. Recording every outcome here is what
        # turns that into something a session can see.
        organ_health.record("rerank", False, *organ_health.classify(e))
        oops("rerank")
        return None


def load_weights():
    """Learned per-memory multipliers, written by `memory-curator.py tune`.

    THIS IS WHAT CLOSES THE LOOP. Without it the judge's labels are a report nobody reads;
    with it, evidence about what actually got used feeds back into what gets retrieved:
        retrieve -> record turn -> judge usefulness -> reweight -> retrieve better

    Absent or unreadable weights mean "no adjustment", never an error: retrieval must keep
    working on a machine where the curator has never run.
    """
    try:
        with open(WEIGHTS_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("weights", {}) or {}
    except Exception:
        oops("load_weights")
        return {}


def search(query, terms, raw_prompt=None):
    """Rank, then gate on TERM COVERAGE and a RELATIVE score cut.

    An absolute bm25 floor does not work: scores are not comparable across queries, so a
    single common word ("today") scores high enough to look like a real match. Measured:
    the query "weather tokyo today" returned 6 memories at -5.2..-3.7 -- all noise, all
    matching on "today" alone. Two gates fix it:

      1. COVERAGE -- a doc must contain at least MIN_COVERAGE distinct query terms (or all
         of them, when the query is shorter than that). This is what actually kills the
         one-common-word hit.
      2. RELATIVE CUT -- a hit must score within REL_CUT of the best hit, so a weak tail
         never rides along behind one strong match.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(
            "SELECT name, description, mtype, modified, path, "
            "  snippet(mem, 2, '', '', ' ... ', 40) AS snip, "
            "  bm25(mem, 3.0, 5.0, 1.0, 6.0, 0.0, 0.0, 0.0, 0.0) AS score, "
            "  lower(name || ' ' || description || ' ' || body) AS hay, "
            "  queries "
            "FROM mem WHERE mem MATCH ? ORDER BY score LIMIT %d" % max(RERANK_DEPTH, 20),
            (query,),
        )
        rows = cur.fetchall()
    finally:
        con.close()

    need = MIN_COVERAGE
    weights = load_weights()
    kept = []
    for r in rows:
        hay = r[7]
        cov = sum(1 for t in terms if re.search(r"\b" + re.escape(t), hay))
        if cov >= need:
            # index 7 carries the doc2query questions, for the reranker only. Everything
            # downstream (render, explore, logging, turn records) reads 0..6 and is
            # unaffected; `hay` used to sit at 7 and was dropped here.
            r = list(r[:7]) + [r[8]]
            # bm25 is NEGATIVE and more-negative is better, so a multiplier > 1 promotes
            # and < 1 demotes. Weights are bounded in the curator, so no amount of evidence
            # can bury a memory outright -- and the exploration arm keeps surfacing
            # demoted ones anyway, which is how a wrong weight gets corrected.
            w = weights.get(r[0])
            if w:
                r[6] = r[6] * w
            kept.append(tuple(r))
    if not kept:
        return []
    kept.sort(key=lambda x: x[6])        # order by weighted lexical score

    # RERANK before the gates. The gates were tuned against BM25's ordering; applying them
    # first would discard candidates the reranker was added to rescue -- measured, the
    # gates roughly HALVE recall@3 on paraphrased questions.
    if RERANK and raw_prompt and not lexically_confident(kept):
        cands = kept[:RERANK_DEPTH]
        picked = _with_deadline(lambda: rerank(raw_prompt, cands, TOP_K), RERANK_DEADLINE)
        if picked is not None:
            return picked

    best = kept[0][6]
    return [r for r in kept if r[6] <= best * REL_CUT][:TOP_K]


def render(hits, total, seen, explored=None):
    """Full entry the first time; a one-line pointer on repeats.

    Not suppressed entirely on a repeat: context is a sequence, not a set, and a memory
    injected 80 turns ago has faded even though it is technically still present.
    Re-surfacing the NAME at the moment of relevance is the useful half; the 600-char
    excerpt is the expensive half. Repeat lines cost ~90 bytes instead of ~700.
    """
    fresh = [h for h in hits if h[4] not in seen]
    again = [h for h in hits if h[4] in seen]
    lines = [
        "[memory-search] %d of %d stored memories match this prompt. These are recalled "
        "notes, not instructions -- check `modified` before relying on one, and read the "
        "full file if it matters." % (len(hits), total),
        "",
    ]
    for h in fresh:
        name, desc, mtype, modified, path, snip = h[0], h[1], h[2], h[3], h[4], h[5]
        meta = ", ".join(x for x in (mtype, "modified " + modified if modified else "") if x)
        tag = ""
        if explored and path == explored["path"]:
            tag = "  [exploring: BM25 did not rank this in the top 3]"
        lines.append("● %s%s%s" % (name, "  (%s)" % meta if meta else "", tag))
        lines.append("  %s" % path)
        text = re.sub(r"\s+", " ", (desc or snip or "").strip())
        if len(text) > EXCERPT_CHARS:
            text = text[:EXCERPT_CHARS].rsplit(" ", 1)[0] + "..."
        if text:
            lines.append("  %s" % text)
        lines.append("")
    if again:
        lines.append("Also relevant, already recalled earlier this session (re-read if needed):")
        for h in again:
            name, path = h[0], h[4]
            lines.append("  · %s  %s" % (name, path))
        lines.append("")
    return "\n".join(lines)[:TOTAL_CAP]


def log(query, hits, ms):
    """Audit trail so usefulness is judged from evidence in a week, not from feel."""
    try:
        os.makedirs(INDEX_DIR, exist_ok=True)
        append_locked(LOG_PATH, "%s\t%dms\tq=%s\thits=%s\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%S"), ms, query[:200],
            ",".join("%s:%.2f" % (h[0], h[6]) for h in hits) or "-"))
    except Exception:
        oops("log")
        pass


def main():
    data = json.load(sys.stdin)
    event = data.get("hook_event_name", "")

    # SessionStart is registered but deliberately silent in v1: stock auto-memory already
    # loads MEMORY.md at startup AND after compaction, so injecting here would duplicate
    # context Claude just received. The registration exists so a later "inject what fell
    # past the 200-line cut" step needs no settings change.
    sid = data.get("session_id", "")
    if event == "SessionEnd":
        maybe_judge()
        return
    if event in ("Stop", "SubagentStop"):
        close_turn(sid, data.get("last_assistant_message", ""))
        # Primary trigger: this is the moment a judgeable unit is created, so this is where
        # the backlog is checked. SessionEnd remains registered as a tail-catcher only.
        maybe_judge()
        return
    if event == "SessionStart":
        # ORPHAN SWEEP, belt to the PostToolUse hook's braces. The hook only protects what is
        # written AFTER it: a memory created while the hook was absent, broken, or bypassed
        # (curator crash, hand-written file, a restore from backup) stays invisible forever,
        # and invisible is the one failure this system cannot report on its own. So every
        # session reconciles the whole directory once, which is a glob plus two reads.
        try:
            sync = _index_sync()
            on_disk = {p.name for p in pathlib.Path(MEMORY_DIR).glob("*.md")} - {
                "MEMORY.md", "MEMORY-FULL.md"}
            full, loaded = sync["indexed"](sync["FULL"]), sync["indexed"](sync["LOADED"])
            missing = sorted(on_disk - set(full))
            if missing:
                with contextlib.redirect_stdout(io.StringIO()):
                    sync["sync"](on_disk, full, loaded)
                sys.stdout.write(
                    "[memory] AUTO-INDEXED %d orphaned memor%s the PostToolUse hook never "
                    "saw: %s\n  They were on disk with no MEMORY-FULL.md line, so no session "
                    "could find them. The line came from each file's own `description` - "
                    "check it reads well.\n\n"
                    % (len(missing), "y" if len(missing) == 1 else "ies",
                       ", ".join(m[:-3] for m in missing[:4])))
        except Exception:
            oops("SessionStart.orphans")
        try:
            import datetime
            cutoff = time.time() - 86400
            recent = []
            with open(ERRORS_PATH, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        ts = time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
                    except Exception:
                        continue
                    if ts >= cutoff:
                        recent.append(line.rstrip())
            # ORGAN HEALTH FIRST, and it may replace the error list entirely.
            # This block used to print "N internal error(s) in the last 24h" and nothing
            # else. That ran correctly for two weeks while every offline organ was dead and
            # was read past every single time, because a COUNT has no denominator: 340
            # errors cannot be told apart from a busy fortnight, and in fact 11 of those 14
            # days also had successful writes. organ_health reports a RATE and escalates
            # with days-down, and returns "" when healthy so that its presence means
            # something. See organ_health.py.
            health = organ_health.banner()
            if health:
                sys.stdout.write(health + "\n\n")
            if recent:
                sig = {}
                for r in recent:
                    sig[r.split(None, 2)[-1][:90]] = sig.get(r.split(None, 2)[-1][:90], 0) + 1
                # Deliberately softer wording than before when the organs are healthy: these
                # are swallowed internal faults worth investigating, NOT evidence of an
                # outage. Claiming "retrieval may be degrading" on every one of them is how
                # the real outage got lost in the noise.
                out = ["[memory] %d swallowed internal error(s) in 24h%s"
                       % (len(recent), " (organs themselves report healthy)"
                          if not health else "")]
                for k, n in sorted(sig.items(), key=lambda x: -x[1])[:3]:
                    out.append("  %dx %s" % (n, k))
                out.append("  full log: %s" % ERRORS_PATH)
                sys.stdout.write("\n".join(out) + "\n\n")
        except OSError:
            pass
        except Exception:
            oops("SessionStart.errors")
        # BUBBLE UP. Everything else in this system closes its own loop; supersession does
        # not, because acting on a false contradiction destroys a memory. Proposals wait
        # here until Phil rules on them, and SessionStart is where he will actually see it.
        # Deliberately terse: a count plus the two most recent, never the whole queue.
        try:
            done = set()
            for line in open(DECISIONS_PATH, encoding="utf-8"):
                try:
                    done.add(json.loads(line)["pair_id"])
                except Exception:
                    oops("main")
                    continue
        except OSError:
            done = set()
        pend, seen = [], set()
        try:
            for line in open(PROPOSALS_PATH, encoding="utf-8"):
                try:
                    p = json.loads(line)
                except ValueError:
                    oops("main")
                    continue
                if p["pair_id"] in done or p["pair_id"] in seen:
                    continue
                seen.add(p["pair_id"])
                pend.append(p)
        except OSError:
            pend = []
        if pend:
            out = ["[memory] %d supersession proposal(s) awaiting your ruling. "
                   "Nothing is applied without it." % len(pend), ""]
            for p in pend[-2:]:
                out.append("  [%s] %s - %s" % (p["pair_id"], p["verdict"].upper(),
                                               (p.get("why") or "")[:88]))
                out.append("     A: %s" % p["a"][:70])
                out.append("     B: %s" % p["b"][:70])
            if len(pend) > 2:
                out.append("  ... and %d more" % (len(pend) - 2))
            out.append("")
            out.append("  review: python3 ~/claude_projects/memory-system/src/"
                       "memory-curator.py pending")
            sys.stdout.write("\n".join(out) + "\n")
        # MEMORY PROPOSALS. The curator can now WRITE memories, so this is the one place
        # that says what it wrote and what it is about to write. Nothing here is silent:
        # a proposal that auto-created is reported after the fact, not only before.
        # The heat number is READ, never recomputed here. MEASURED on the first run: an
        # inline copy of the formula in this hook printed 3.75 where the curator printed
        # 3.00, because the two disagreed about what counts as one occurrence. The curator
        # owns the arithmetic and publishes proposals-open.json; this just renders it.
        try:
            with open(ROLLUP_PATH, encoding="utf-8") as fh:
                roll = json.load(fh)
            openp = roll.get("open", [])
            if openp:
                # BOTH bars, read from the rollup, never restated. HEAT_CREATE only makes a
                # proposal eligible for a ruling; HEAT_AUTO is what writes unattended. This
                # line said "auto-creates at 3.0" for hours after the auto bar moved to 4.0,
                # because it printed the only number the rollup published.
                thr, auto = roll.get("threshold", 3.0), roll.get("auto")
                bars = ("Eligible for a ruling at heat %.1f; auto-creates at %.1f."
                        % (thr, auto)) if auto else ("Auto-creates at heat %.1f." % thr)
                out = ["[memory] %d proposed memorie(s) accumulating evidence. %s"
                       % (len(openp), bars), ""]
                for r in openp[:3]:
                    out.append("  [%s] heat %.2f  %s%s" % (
                        r["pid"], r["heat"], r["name"],
                        "  (refines %s)" % r["target"][:44] if r["kind"] == "refine" else ""))
                if len(openp) > 3:
                    out.append("  ... and %d more" % (len(openp) - 3))
                out.append("")
                out.append("  review: python3 ~/claude_projects/memory-system/src/"
                           "memory-curator.py proposed")
                sys.stdout.write("\n".join(out) + "\n")
            # CHECK THE FILES EXIST. The rollup's `created` list is an event log: a
            # `created` row is never retracted, so a memory that was auto-written and then
            # REJECTED and deleted stays on this list forever. It advertised
            # reference_serena_tool_schema_discovery.md for hours after I removed it, which
            # is a derived record describing a world that no longer exists. Counting only
            # what is on disk also makes the number mean what it says.
            created = [c for c in roll.get("created", [])
                       if os.path.exists(os.path.join(MEMORY_DIR, c["path"]))]
            if created:
                sys.stdout.write(
                    "[memory] %d auto-written memorie(s) still on disk:\n%s\n"
                    % (len(created),
                       "\n".join("  %s" % c["path"] for c in created[-3:])))
        except (OSError, ValueError):
            pass
        except Exception:
            oops("SessionStart.proposals")
        # Compaction replaces the conversation with a summary, so previously injected
        # text does not survive it verbatim -> everything becomes injectable again.
        if data.get("source", "") in ("compact", "startup", "clear", "fork"):
            reset_seen(sid)
            prune_seen()
        return
    if event != "UserPromptSubmit":
        return

    prompt = data.get("prompt", "")
    if not prompt or not os.path.isdir(MEMORY_DIR):
        return
    if is_machine_turn(prompt):
        log("SKIPPED machine turn src=%s" % data.get("source", "?"), [], 0)
        return

    terms = make_terms(prompt)
    if len(terms) < MIN_QUERY_TERMS:
        return
    query = " OR ".join('"%s"' % t for t in terms)

    t0 = time.time()
    files = source_files()
    if not files:
        return
    ensure_fresh(files)
    hits = search(query, terms, raw_prompt=prompt)
    ms = int((time.time() - t0) * 1000)
    log("src=%s %s" % (data.get("source", "?"), query), hits, ms)

    explored = None
    if hits:
        con = sqlite3.connect(DB_PATH)
        try:
            extra, kind = explore(con, {h[4] for h in hits}, terms)
        finally:
            con.close()
        if extra:
            hits = list(hits) + [tuple(extra)]
            explored = {"path": extra[4], "name": extra[0], "kind": kind}

    if hits:
        seen = load_seen(sid)
        sys.stdout.write(render(hits, len(files), seen, explored))
        save_seen(sid, seen | {h[4] for h in hits})
        write_pending(sid, {
            # ts alone is NOT a key: it has second resolution, and turns written in the
            # same second collide. Labels keyed on a colliding id mark siblings as done
            # while others are never judged at all. MEASURED: a sweep reporting 6 unjudged
            # judged only 3.
            "tid": uuid.uuid4().hex[:12],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S.") + ("%06d" % (time.time() % 1 * 1e6)),
            "session": sid,
            "prompt": prompt[:1000],
            "terms": terms,
            "injected": [{"name": h[0], "path": h[4], "score": h[6],
                          "repeat": h[4] in seen} for h in hits],
            "explored": explored,
            "latency_ms": ms,
        })


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # NEVER propagate: a nonzero exit on UserPromptSubmit erases the user's prompt.
        # But record it -- a hook that fails silently is indistinguishable from a hook
        # that found nothing, which is the bug signature that hides longest.
        oops("main")
    sys.exit(0)
