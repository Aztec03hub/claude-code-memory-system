#!/usr/bin/env python3
"""memory-curator.py - the resident memory manager. Runs OFFLINE, never in the hot path.

WHY IT IS SEPARATE FROM THE HOOK
    memory-search.py sits in front of every prompt and must answer in milliseconds. This
    does the expensive judgment work with nobody waiting: labelling whether injected
    memories were actually used, curating the corpus, and eventually tuning the hook's
    constants from accumulated evidence rather than from my hand-picked guesses.

WHAT IT IS FOR
    The entire field's gap, per the 2026 survey (arXiv 2606.30306), is that the literature
    "concentrates more heavily on accumulating and retrieving state than on governing,
    recovering, or relinquishing it." Nobody measures whether retrieval HELPED. turns.jsonl
    plus these labels is that measurement.

DESIGN CONSTRAINTS (Phil's standing directive)
    Batching and per-phase wall-clock timing are built in from the start, so perf work is
    easy later and no bottleneck ever gets ranked on vibes.

SUBCOMMANDS
    judge   label unjudged turns: was each injected memory actually used?
    report  what the labels say about retrieval quality, ranking, and exploration
"""

import argparse
import fcntl
import collections
import json
import os
import random
import re
import sys
import time
import uuid
import sqlite3
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import organ_health   # noqa: E402  (sibling module; see its docstring for why it is shared)

MEMORY_DIR = os.path.expanduser("~/.claude/projects/-home-plafayette/memory")
INDEX_DIR = os.path.join(MEMORY_DIR, ".index")
organ_health.configure(INDEX_DIR)
TURNS_PATH = os.path.join(INDEX_DIR, "turns.jsonl")
LABELS_PATH = os.path.join(INDEX_DIR, "labels.jsonl")
NOTES_PATH = os.path.join(INDEX_DIR, "curator-notes.md")
DB_PATH = os.path.join(INDEX_DIR, "memories.db")
LOCK_PATH = os.path.join(INDEX_DIR, ".curator.lock")
WEIGHTS_PATH = os.path.join(INDEX_DIR, "weights.json")
Q2Q_PATH = os.path.join(INDEX_DIR, "doc2query.json")
Q2Q_BATCH = 8
Q2Q_PER_MEM = 4
# Was 1600 and measurably too tight: 8 names + 32 questions truncated mid-JSON.
# A prior memory had already settled this at 4096 for structured Gemini output.
Q2Q_MAX_TOKENS = 4096
# Per automatic cycle. Small on purpose: new memories trickle in a few per session, and a
# cycle should never become a long API session behind the user's back.
ENRICH_PER_CYCLE = 16
JUDGE_STAMP = os.path.join(INDEX_DIR, ".last-judge")
PROPOSALS_PATH = os.path.join(INDEX_DIR, "supersede-pending.jsonl")
DECISIONS_PATH = os.path.join(INDEX_DIR, "supersede-decided.jsonl")
SUPERSEDE_SLICE = 40    # memories scanned per automatic cycle; the corpus is walked over time
SUPERSEDE_CURSOR = os.path.join(INDEX_DIR, ".supersede-cursor")

# Learned per-memory weights, the thing that actually CLOSES the loop:
#   retrieve -> record turn -> judge usefulness -> reweight -> retrieve better
# Bounded hard, because the failure mode is a memory buried forever on thin evidence.
MAX_SWEEPS = 20         # drain-loop bound: a busy machine re-triggers rather than looping forever
MIN_EVIDENCE = 3        # injections before a memory earns any adjustment at all
W_FLOOR, W_CEIL = 0.80, 1.25
PRIOR_A, PRIOR_B = 1.0, 3.0   # Beta prior: assume ignored until shown otherwise

MODEL = "gemini-3.1-flash-lite"
API = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % MODEL
# MEASURED before these existed: 81 turns cost 31 API calls (2.61 turns/call against a
# ceiling of 6), because a judge fired the instant a single turn landed. 7 runs judged
# exactly ONE turn. The trigger stays eager -- latency to START is good -- but the judge
# now WAITS for a batch to form before spending a call.
BATCH = 10              # turns per API call; the 6-turn prompt was only ~2.4KB
ACCUMULATE_MAX = 45     # seconds to wait for a batch to fill before judging anyway
ACCUMULATE_POLL = 3     # how often to re-check the backlog while waiting
API_TIMEOUT = 60


class Phases:
    """Per-phase wall clock. Standing directive: never vibes-rank a bottleneck."""

    def __init__(self):
        self.t = collections.OrderedDict()
        self._open = None

    def start(self, name):
        self._open = (name, time.time())

    def stop(self):
        if self._open:
            n, t0 = self._open
            self.t[n] = self.t.get(n, 0.0) + (time.time() - t0)
            self._open = None

    def render(self):
        total = sum(self.t.values()) or 1e-9
        return "  ".join("%s=%dms(%.0f%%)" % (n, v * 1000, v / total * 100)
                         for n, v in self.t.items())


ERRORS_PATH = os.path.join(INDEX_DIR, "errors.log")


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


def exclusive(name):
    """Whole-run mutex. Returns an open handle on success, None if another run holds it.

    24 Claude Code processes share this corpus, and every one of them fires SessionEnd.
    Without this, N sessions ending together start N judges over the SAME unjudged turns,
    paying N times for duplicate labels. The cooldown stamp alone cannot prevent that: it
    is check-then-act, and all N pass the check before any of them writes.
    Non-blocking on purpose - a second judge should exit, not queue.
    """
    try:
        os.makedirs(INDEX_DIR, exist_ok=True)
        fh = open(LOCK_PATH, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write("%s %d\n" % (name, os.getpid()))
        fh.flush()
        return fh
    except BlockingIOError:
        # EXPECTED: another run holds the lock and this one correctly declines. Recording
        # it as an error would fill the log with the system working as designed, and an
        # alert that fires on healthy behaviour is an alert that gets ignored.
        return None
    except (OSError, IOError):
        oops("exclusive")
        return None


def append_locked(path, text):
    """Appends here race the hook's appends and each other. See memory-search.py."""
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


def turn_key(t):
    """Stable unique key for a turn. Falls back to ts for rows written before tid existed."""
    return t.get("tid") or t.get("ts")


def judgeable(turns, done):
    """Turns the judge will ACTUALLY process.

    The backlog counter must agree with this or the drain loop cannot terminate: turns with
    no injections are skipped by the judge forever, so counting them as backlog spins the
    loop until MAX_SWEEPS every time. MEASURED before this existed.
    """
    return [t for t in turns if turn_key(t) not in done and t.get("injected")]


LIVE_PATH = "/tmp/claude-memory-curator.live"


def live(msg):
    """Real-time progress for in-flight runs, readable at any moment.

    A judge run is detached and its stdout goes to /dev/null, so without this there is no
    way to tell a slow run from a hung one from a crashed one. Lives in /tmp on purpose:
    it is diagnostic breadcrumbs, not data, and losing it on reboot costs nothing.
    Tagged with pid because several runs can exist at once on a busy machine.
    """
    try:
        with open(LIVE_PATH, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write("%s pid=%-7d %s\n"
                         % (time.strftime("%H:%M:%S"), os.getpid(), msg))
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        oops("live")
        pass


def read_jsonl(path, dedupe_key=None):
    """Read a jsonl file, tolerating torn lines.

    IDEMPOTENCY: pass dedupe_key to collapse duplicate rows, keeping the LAST. Duplicates
    are supposed to be impossible (the exclusive lock plus tid-keying prevent them), but
    "supposed to be impossible" is how 60 duplicate labels happened. Collapsing on read
    makes any future duplicate harmless rather than load-bearing, at no cost.
    """
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue          # a torn final line must not kill the run
    except FileNotFoundError:
        oops("read_jsonl")
        pass
    if dedupe_key:
        seen = {}
        for r in out:
            k = dedupe_key(r)
            if k is not None:
                seen[k] = r
        return list(seen.values())
    return out


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


# Transient failures worth retrying. 400 is NOT here on purpose: a malformed request is
# not fixed by sending it again, and retrying it just burns quota and time.
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
RETRIES = 3
RETRY_BASE = 1.5

# Which organ is calling. Derived from the stack rather than threaded through every caller,
# because every one of the dozen call sites would otherwise have to remember to pass it, and
# the one that forgot would be invisible - the same shape of gap this whole signal exists to
# close. Mirrors the context tags oops() already records, so the two logs line up.
def _organ():
    try:
        import inspect
        for fr in inspect.stack()[1:8]:
            n = fr.function
            if n.startswith("cmd_") or n.startswith("_"):
                return n.lstrip("_")
        return "curator"
    except Exception:
        return "curator"


def call_gemini(prompt, max_tokens=2048, retries=RETRIES):
    """Call the model, retrying transient failures with exponential backoff.

    MEASURED: an enrichment run lost a batch of 8 memories to a single unretried failure.
    A prior finding in this very corpus had already named the cause - "bare-int max_retries
    doesn't retry 503s ... settled: tenacity retry" - so this is a known-solved problem
    that was simply never applied here.

    Callers in the HOT PATH must pass retries=0: a reranker that retries blows the latency
    budget, and failing open instantly is strictly better there.
    """
    key = gemini_key()
    if not key:
        e = RuntimeError("GEMINI_API_KEY not set and %s unreadable" % KEY_FILE)
        organ_health.record(_organ(), False, *organ_health.classify(e))
        raise e
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens},
    }).encode()
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                API, data=body,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
                d = json.loads(r.read())
            try:
                text = d["candidates"][0]["content"]["parts"][0]["text"]
                # Record the SUCCESS, not just failures. The successes are the denominator
                # that makes a failure count interpretable; without them "340 errors" could
                # not be told apart from a busy fortnight, which is what happened.
                organ_health.record(_organ(), True)
                return text
            except (KeyError, IndexError):
                # A response with no candidates is usually a transient safety/quota blip,
                # so it is worth one more try rather than losing the batch.
                raise RuntimeError("no candidates in response: %s" % str(d)[:160])
        # Record only TERMINAL failures, never a retried attempt. A retry that then succeeds
        # is not an outage, and counting it would depress the success rate the signal reads,
        # making a healthy-but-flaky system look down.
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in RETRY_STATUS or attempt == retries:
                organ_health.record(_organ(), False, *organ_health.classify(e))
                raise
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as e:
            last = e
            if attempt == retries:
                organ_health.record(_organ(), False, *organ_health.classify(e))
                raise
        sleep = RETRY_BASE ** attempt + random.random()
        live("retry: attempt %d/%d after %s (%.1fs)"
             % (attempt + 1, retries, type(last).__name__, sleep))
        time.sleep(sleep)
    exhausted = last if last else RuntimeError("call_gemini exhausted retries")
    organ_health.record(_organ(), False, *organ_health.classify(exhausted))
    raise exhausted


def extract_json(text):
    """Models fence JSON in markdown at will. Strip it rather than trusting the format."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    start = min([i for i in (t.find("["), t.find("{")) if i != -1] or [0])
    return json.loads(t[start:])


JUDGE_PROMPT = """You are auditing whether retrieved memories actually helped an AI coding \
assistant answer a user.

For each TURN you get the user's prompt, the memories that were injected into the \
assistant's context, and the response the assistant actually gave.

For EACH injected memory, judge how it was used:
  "used"      the response visibly reflects this memory's content, follows its rule, or \
cites it
  "ignored"   the response does not reflect it; injecting it changed nothing
  "harmful"   the memory was irrelevant or misleading and appears to have degraded the answer

Be strict about "used". Topical overlap is NOT use. If the assistant would plainly have \
written the same response without that memory, it is "ignored". Most injections are \
genuinely "ignored" - say so. A judge that labels everything "used" is worthless.

Also give each turn a one-line `note` naming what retrieval SHOULD have surfaced, if \
anything, when the injections missed.

TURNS:
%s

Return ONLY a JSON array, one object per turn, in the same order:
[{"turn": <index>, "labels": {"<memory name>": "used|ignored|harmful"}, "note": "<short>"}]
"""


def descriptions(paths):
    """Look content up from the index rather than duplicating it into every turn row.
    turns.jsonl is raw evidence and should stay small; the index already has the text."""
    out = {}
    try:
        con = sqlite3.connect(DB_PATH)
        for p in set(paths):
            row = con.execute(
                "SELECT description, substr(body,1,400) FROM mem WHERE path=?", (p,)).fetchone()
            if row:
                out[p] = (row[0] or row[1] or "").strip()
        con.close()
    except Exception:
        oops("descriptions")
        pass
    return out


def build_batch(turns):
    desc = descriptions([m["path"] for t in turns for m in t["injected"]])
    parts = []
    for i, t in enumerate(turns):
        mem = "\n".join(
            "    - %s: %s" % (m["name"], desc.get(m["path"], "")[:220])
            for m in t["injected"])
        parts.append(
            "TURN %d\n  user prompt: %s\n  injected memories:\n%s\n  assistant response: %s"
            % (i, t["prompt"][:400], mem or "    (none)", (t.get("response") or "")[:1200]))
    return JUDGE_PROMPT % "\n\n".join(parts)


def cmd_judge(args):
    lock = exclusive("judge")
    if lock is None:
        print("another curator run holds the lock; exiting")
        return 0
    try:
        return _judge(args)
    finally:
        lock.close()


def _judge(args):
    ph = Phases()
    ph.start("load")
    turns = read_jsonl(TURNS_PATH)
    done = {turn_key(l) for l in read_jsonl(LABELS_PATH, dedupe_key=turn_key)}
    pending = judgeable(turns, done)
    ph.stop()

    if not pending:
        print("nothing to judge (%d turns, %d already labelled)" % (len(turns), len(done)))
        return 0
    if args.limit:
        pending = pending[:args.limit]

    print("judging %d turns in %d batches of %d" %
          (len(pending), (len(pending) + BATCH - 1) // BATCH, BATCH))

    written = failed = 0
    for b in range(0, len(pending), BATCH):
        batch = pending[b:b + BATCH]
        ph.start("build")
        prompt = build_batch(batch)
        ph.stop()
        ph.start("api")
        live("judge: api call, batch of %d" % len(batch))
        try:
            raw = call_gemini(prompt)
        except Exception as e:
            print("  batch %d FAILED: %s" % (b // BATCH, str(e)[:120]))
            failed += len(batch)
            ph.stop()
            continue
        ph.stop()
        ph.start("parse")
        try:
            results = extract_json(raw)
        except Exception as e:
            print("  batch %d UNPARSEABLE: %s" % (b // BATCH, str(e)[:100]))
            failed += len(batch)
            ph.stop()
            continue
        ph.stop()
        ph.start("write")
        buf = []
        for r in results:
                idx = r.get("turn")
                if not isinstance(idx, int) or idx >= len(batch):
                    continue
                t = batch[idx]
                buf.append(json.dumps({
                    # MUST carry the turn's OWN key. Labels used to be keyed on `ts` while
                    # turns were keyed on `tid`, so nothing ever matched: the judge relabelled
                    # the same turns on every sweep, burning an API call each time, and the
                    # backlog never fell. Silent, and expensive.
                    "tid": turn_key(t),
                    "lid": uuid.uuid4().hex[:12],   # unique per label row, independent of tid
                    "ts": t["ts"],
                    "prompt": t["prompt"][:200],
                    "labels": r.get("labels", {}),
                    "note": r.get("note", ""),
                    "explored": (t.get("explored") or {}).get("name"),
                    "judged": time.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                }, ensure_ascii=False) + "\n")
                written += 1
        append_locked(LABELS_PATH, "".join(buf))
        ph.stop()

    print("labelled %d turns, %d failed" % (written, failed))
    print("phases: %s" % ph.render())

    if written:
        labels = read_jsonl(LABELS_PATH, dedupe_key=turn_key)[-written:]
        c = collections.Counter()
        for l in labels:
            c.update((l.get("labels") or {}).values())
        tot = sum(c.values()) or 1
        expl = [l for l in labels if l.get("explored")]
        expl_used = sum(1 for l in expl
                        if (l.get("labels") or {}).get(l["explored"]) == "used")
        body = ["Judged %d turns, %d injections." % (written, tot),
                "used %d (%.0f%%), ignored %d, harmful %d"
                % (c["used"], c["used"] / tot * 100, c["ignored"], c["harmful"])]
        if expl:
            body.append("Exploration arm: %d turns, %d of those injections were used."
                        % (len(expl), expl_used))
            if expl_used and c["used"] / tot < expl_used / max(len(expl), 1):
                body.append("NOTE: explored memories outperformed the BM25 top-3 in this "
                            "batch. If that holds, the ranking is leaving value behind.")
        notes = [l["note"] for l in labels if l.get("note")][:5]
        if notes:
            body.append("")
            body.append("Judge notes on what retrieval missed:")
            body += ["  - " + n[:150] for n in notes]
        append_note("judge run", "\n".join(body))
        print("wrote a curator note")
    return 0


HOOK_PROMPT = """Rewrite index hooks for a developer's personal memory corpus.

Each memory is a hard-won lesson. Its INDEX HOOK is one line in a file the assistant reads \
every session; the hook is all it sees until it opens the file. These hooks were truncated \
mid-word and now carry no information, so the memory is effectively unfindable.

FIRST decide whether each hook actually needs replacing. Many are SHORT BUT FINE - a terse \
complete phrase like "ALWAYS <<'MSG'" or "probe before claiming can't" does its job and must \
be LEFT ALONE. Length is not the defect. The defect is a hook CUT OFF MID-THOUGHT, like \
"multi-KB send-keys VAN" or "MEASURED 5x: chased th", which names no finding at all.

Some entries are annotated "PROVEN cut mid-word". Those are a FLOOR, not a whitelist: they \
definitely need rewriting, and so does any OTHER hook whose thought is left incomplete, \
whether or not it carries the annotation. Judge every entry on its own.

Skip anything already adequate. Bytes in this file are scarce and rewriting a working hook \
spends them for nothing.

For the genuinely broken ones, write a replacement from the description.

RULES
- Maximum %d characters. Hard limit. Shorter is fine if it is still specific.
- It must let a reader decide RELEVANCE: what situation does this apply to, and what is the
  finding? Prefer the surprising specific over the general category.
- Keep a leading marker if the description has one (MEASURED, PHIL-LOCKED, MEASURED 5x).
- Telegraphic is good. Drop articles. This is an index line, not a sentence.
- NEVER use an em-dash. Use a plain hyphen. This is an absolute rule.
- Do not invent anything not supported by the description.

MEMORIES:
%s

Return ONLY a JSON object. Include a memory ONLY if it needs replacing; omit adequate ones.
{"<name>": "<new hook>"}
"""

MAX_HOOK = 66


def find_stubs(loaded_text, full_text, limit=24):
    """A hook this short cannot carry a relevance decision. Matches the checker's rule."""
    pat = re.compile(r"^- \[([^\]]*)\]\(([^)]+\.md)\)(.*)$", re.M)
    # Reads BOTH shapes. Since 2026-09-30 an entry is `- [hook](path)` and group(3) is
    # empty; before that it was `- [display](path) - hook`. Taking group(3) alone would
    # report every entry in the new shape as a stub.
    def _pair(m):
        rest = m.group(3).strip()
        return (m.group(1), rest[2:].strip() if rest.startswith("- ") else m.group(1))
    lo = {m.group(2): _pair(m) for m in pat.finditer(loaded_text)}
    fu = {m.group(2): _pair(m) for m in pat.finditer(full_text)}
    out = []
    for path, (title, hook) in lo.items():
        if len(hook) < limit:
            out.append({"file": path, "title": title, "loaded": hook,
                        "full": fu.get(path, ("", ""))[1]})
    return out


def cmd_curate_hooks(args):
    ph = Phases()
    ph.start("load")
    LOADED = os.path.join(MEMORY_DIR, "MEMORY.md")
    FULL = os.path.join(MEMORY_DIR, "MEMORY-FULL.md")
    loaded_text = open(LOADED, encoding="utf-8").read()
    full_text = open(FULL, encoding="utf-8").read()
    stubs = find_stubs(loaded_text, full_text)
    ph.stop()
    if not stubs:
        print("no stub hooks found")
        return 0

    ph.start("build")
    con = sqlite3.connect(DB_PATH)
    for s in stubs:
        row = con.execute("SELECT name, description FROM mem WHERE path=?",
                          (os.path.join(MEMORY_DIR, s["file"]),)).fetchone()
        s["name"] = (row[0] if row else s["file"][:-3])
        s["desc"] = (row[1] if row else "").strip()
    con.close()
    # DETERMINISTIC truncation test: if the hook is a strict prefix of the description and
    # the description continues with a word character, the hook was cut MID-WORD. That is a
    # fact, not a judgment, so do not leave it to the model -- it demonstrably gets this
    # wrong both ways ('re-dispatch strips gua' was judged "adequate" on one run).
    for s in stubs:
        d = (s.get("desc") or "")
        h = s["loaded"]
        s["truncated"] = bool(h) and d.lower().startswith(h.lower()[:len(h)]) \
            and len(d) > len(h) and d[len(h):len(h) + 1].isalnum()
    usable = [s for s in stubs if s["desc"]]
    blob = "\n\n".join(
        "name: %s\ncurrent hook: %s%s\ndescription: %s"
        % (s["name"], s["loaded"],
           "   <-- PROVEN cut mid-word" if s["truncated"] else "",
           s["desc"][:500]) for s in usable)
    prompt = HOOK_PROMPT % (MAX_HOOK, blob)
    ph.stop()

    ph.start("api")
    try:
        new = extract_json(call_gemini(prompt, max_tokens=2048))
    except Exception as e:
        print("FAILED: %s" % str(e)[:150])
        return 1
    ph.stop()

    ph.start("verify")
    proposals, rejected, skipped = [], [], []
    for s in usable:
        h = (new.get(s["name"]) or "").strip()
        if not h:
            if s.get("truncated"):
                rejected.append((s, "PROVEN truncated mid-word but model declined to rewrite"))
            else:
                # NOT a failure: adequate hooks were meant to be omitted.
                skipped.append(s)
            continue
        if "\u2014" in h:
            rejected.append((s, "contains an em-dash")); continue
        if len(h) > MAX_HOOK:
            rejected.append((s, "%d chars over limit" % (len(h) - MAX_HOOK))); continue
        if len(h) <= len(s["loaded"]):
            rejected.append((s, "not more informative than the stub")); continue
        proposals.append((s, h))
    delta = sum(len(h) - len(s["loaded"]) for s, h in proposals)
    ph.stop()

    print("candidates: %d   rewriting: %d   already adequate: %d   failed validation: %d" %
          (len(stubs), len(proposals), len(skipped), len(rejected)))
    for s, h in proposals:
        print("\n  %s" % s["name"][:70])
        print("    was: %r" % s["loaded"])
        print("    now: %r  (%d chars)" % (h, len(h)))
    if skipped:
        print("\n  LEFT ALONE (judged adequate, costs no bytes):")
        for s in skipped:
            print("    %-52s %r" % (s["name"][:52], s["loaded"]))
    for s, why in rejected:
        print("\n  FAILED VALIDATION %s: %s" % (s["name"][:52], why))

    size = len(loaded_text) + delta
    lines = loaded_text.count("\n")
    print("\nMEMORY.md would go %d -> %d bytes (limit 25000, budget 24000), %d lines"
          % (len(loaded_text), size, lines))
    if size > 24000:
        print("  WARNING: over the 24000 budget. Demote entries to pay for this.")
    print("phases: %s" % ph.render())

    if not args.apply:
        print("\nDRY RUN. Read each rewrite against its memory, then apply the ones that")
        print("survive with:\n  --apply --only %s"
              % ",".join(s["file"][:-3] for s, _h in proposals))
        return 0
    if size > 25000:
        print("\nREFUSING to apply: would exceed the hard 25000 limit.")
        return 1

    # --only is REQUIRED, and it is not bureaucracy. MEASURED on a real run of 9 rewrites:
    # 2 were materially wrong - one invented a clause ("check coverage") that appears
    # nowhere in the memory, and one INVERTED its lesson, blaming an agent for ignoring a
    # retraction that had never been sent to it. Both would have been written to both
    # index tiers. A lexical support check does not catch either: the fabricated hook
    # scored 0.71 token-containment against the memory, HIGHER than a correct rewrite at
    # 0.60, because a wrong claim can be assembled entirely from right words. Fidelity here
    # is not mechanically checkable, so the gate is a human naming what they read.
    only = {x.strip() for x in (getattr(args, "only", "") or "").split(",") if x.strip()}
    if not only:
        print("\nREFUSING to apply without --only. These rewrites are LLM claims about what")
        print("a memory says, and ~2 in 9 were materially wrong on the last real run.")
        print("Read them, then name the ones you verified.")
        return 1
    unknown = only - {s["file"][:-3] for s, _h in proposals}
    if unknown:
        print("\nREFUSING: not proposed in this run: %s" % ", ".join(sorted(unknown)))
        return 1
    dropped = [s["name"] for s, _h in proposals if s["file"][:-3] not in only]
    proposals = [(s, h) for s, h in proposals if s["file"][:-3] in only]
    if dropped:
        print("\n  not applying %d rewrite(s) you did not name: %s"
              % (len(dropped), ", ".join(d[:40] for d in dropped[:4])))

    ts = int(time.time())
    for src in (LOADED, FULL):
        with open(src, encoding="utf-8") as fh:
            open("%s.bak-curate-%d" % (src, ts), "w", encoding="utf-8").write(fh.read())

    def sub(text):
        for s, h in proposals:
            # Entry shape is `- [hook](path)` as of 2026-09-30: the hook IS the link
            # text. Writing the old `- [display](path) - hook` here would reintroduce the
            # display name that cost 26% of the byte-capped loaded tier.
            text = re.sub(r"^- \[[^\]]*\]\(%s\)\s*(?: - .*)?$" % re.escape(s["file"]),
                          lambda m, h=h, f=s["file"]: "- [%s](%s)" % (
                              h.replace("[", "(").replace("]", ")"), f),
                          text, flags=re.M)
        return text
    open(LOADED, "w", encoding="utf-8").write(sub(loaded_text))
    open(FULL, "w", encoding="utf-8").write(sub(full_text))
    print("\napplied %d hooks. backups at *.bak-curate-%d" % (len(proposals), ts))
    return 0


SUPERSEDE_PROMPT = """You are auditing a developer's personal memory corpus for pairs that \
should not both stand as written.

Each PAIR is two memories that use similar language. Most pairs will be RELATED BUT FINE - \
two different lessons about a similar topic. Say so. Only flag a pair when one of these is \
genuinely true:

  "contradicts"  they make incompatible factual claims. One is now WRONG.
  "duplicate"    they record the SAME lesson in different words, no added information.
  "refines"      one is a strictly more precise/corrected version of the other.

Default to "fine". This corpus is deliberately full of related-but-distinct lessons, and a \
false contradiction is worse than a missed one: acting on it would destroy a good memory.

When you flag a pair, say which one should stand (`current`) and why in one line. Prefer the \
one with the later `modified` date ONLY when the claims actually conflict; a newer memory is \
not automatically right about a different subject.

PAIRS:
%s

Return ONLY JSON:
[{"pair": <index>, "verdict": "fine|contradicts|duplicate|refines",
  "current": "<name that should stand, or empty>", "why": "<one line>"}]
"""


def candidate_pairs(limit_mem=0, per_mem=3, offset=0):
    """Generate pairs worth reasoning about, using lexical overlap.

    NOTE on why this is legitimate: arXiv 2606.26511 measured that cosine similarity cannot
    DECIDE contradiction-vs-duplication (AUROC 0.59, near chance). That rules similarity out
    as a judge, not as a CANDIDATE GENERATOR -- "these two talk about the same thing" is a
    question lexical search answers well. The judging is done by a reasoner downstream.
    """
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT name, description, COALESCE(NULLIF(modified,''), fmtime || ' (file mtime)'), path FROM mem "
        "WHERE length(description) > 40 ORDER BY path").fetchall()
    if offset:
        rows = rows[offset:]
    if limit_mem:
        rows = rows[:limit_mem]
    else:
        rows = rows[:SUPERSEDE_SLICE]
    seen, pairs = set(), []
    for name, desc, modified, path in rows:
        terms = [t for t in re.findall(r"[A-Za-z0-9_]{4,}", (desc or "").lower())][:14]
        if len(terms) < 4:
            continue
        q = " OR ".join('"%s"' % t for t in dict.fromkeys(terms))
        try:
            hits = con.execute(
                "SELECT name, description, COALESCE(NULLIF(modified,''), fmtime || ' (file mtime)'), path, "
                "  bm25(mem,3.0,5.0,1.0,0,0,0,0) FROM mem WHERE mem MATCH ? "
                "ORDER BY 5 LIMIT ?", (q, per_mem + 1)).fetchall()
        except Exception:
            oops("candidate_pairs")
            continue
        for h in hits:
            if h[3] == path:
                continue
            key = tuple(sorted((path, h[3])))
            if key in seen:
                continue
            seen.add(key)
            pairs.append(((name, desc, modified), (h[0], h[1], h[2]), h[4]))
    con.close()
    pairs.sort(key=lambda p: p[2])        # strongest lexical overlap first
    return pairs


def pair_id(a_name, b_name):
    """Stable, order-independent id for a pair, so a decision sticks across runs and the
    same pair is never re-proposed after Phil has ruled on it."""
    import hashlib
    return hashlib.sha1("|".join(sorted((a_name, b_name))).encode()).hexdigest()[:12]


def decided_ids():
    return {d["pair_id"] for d in read_jsonl(DECISIONS_PATH)}


def pending_proposals():
    done = decided_ids()
    seen, out = set(), []
    for p in read_jsonl(PROPOSALS_PATH):
        if p["pair_id"] in done or p["pair_id"] in seen:
            continue
        seen.add(p["pair_id"])
        out.append(p)
    return out


def cmd_decide(args):
    """Record Phil's ruling on a proposed pair. This is the ONLY way a pair leaves the queue.

    Supersession is the one thing in this system that is never automated: acting on a false
    contradiction destroys a memory, and the corpus is deliberately full of related-but-
    distinct lessons. Everything else here closes its own loop; this waits for a human.
    """
    pend = {p["pair_id"]: p for p in pending_proposals()}
    if args.pair_id not in pend:
        print("no pending proposal with id %s" % args.pair_id)
        print("open ids: %s" % ", ".join(list(pend)[:10]) or "(none)")
        return 1
    p = pend[args.pair_id]
    append_locked(DECISIONS_PATH, json.dumps({
        "pair_id": args.pair_id, "ruling": args.ruling, "note": args.note or "",
        "a": p["a"], "b": p["b"],
        "decided": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, ensure_ascii=False) + "\n")
    print("recorded: %s -> %s" % (args.pair_id, args.ruling))
    print("  A: %s" % p["a"])
    print("  B: %s" % p["b"])
    if args.ruling in ("keep-a", "keep-b", "merge"):
        loser = (p["b"] if args.ruling == "keep-a"
                 else p["a"] if args.ruling == "keep-b" else "(both - fold one into the other)")
        print("\n  NOT applied automatically. To act on it, fold anything unique across and")
        print("  retire:\n    %s" % loser)
        print("  Then record that you did:  %s applied %s --note '...'"
              % (os.path.basename(__file__), args.pair_id))
        print("  Until then `status` will list it as DECIDED BUT UNAPPLIED. MEASURED: without")
        print("  that, 7 rulings sat unexecuted for 24 days because a recorded ruling and an")
        print("  applied one were indistinguishable.")
    return 0


def cmd_applied(args):
    """Record that an actionable ruling was actually carried out."""
    latest = {}
    for r in read_jsonl(DECISIONS_PATH):
        if r.get("pair_id"):
            latest[r["pair_id"]] = r
    r = latest.get(args.pair_id)
    if not r:
        print("no ruling on record for %s" % args.pair_id)
        return 1
    if r.get("applied"):
        print("%s is already marked applied" % args.pair_id)
        return 0
    append_locked(DECISIONS_PATH, json.dumps({
        "pair_id": args.pair_id, "ruling": r["ruling"], "applied": True,
        "note": args.note or "applied", "a": r.get("a", ""), "b": r.get("b", ""),
        "decided": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, ensure_ascii=False) + "\n")
    print("marked applied: %s (%s)" % (args.pair_id, r["ruling"]))
    return 0


def unapplied_rulings():
    """Rulings that demand an edit and have no `applied` marker.

    THE GAP THIS CLOSES. cmd_decide prints "NOT applied automatically" and then nothing in
    the system ever asks again, so a ruling is advisory by construction. MEASURED
    2026-10-05: five merges and two keep-b decisions from 2026-09-11 had sat unexecuted for
    24 days, with the loser of one still on disk, and nothing anywhere said so. A decision
    that leaves no trace of whether it was carried out is the same silent-failure shape as
    the rest of this file's history, one level up from the code.
    """
    latest = {}
    for r in read_jsonl(DECISIONS_PATH):
        if r.get("pair_id"):
            latest[r["pair_id"]] = r      # later rows win, including corrections
    return [r for r in latest.values()
            if r.get("ruling") in ("keep-a", "keep-b", "merge") and not r.get("applied")]


def cmd_pending(args):
    pend = pending_proposals()
    if not pend:
        print("no pending supersession proposals")
        return 0
    print("%d pending proposal(s) awaiting your ruling:\n" % len(pend))
    for p in pend:
        print("  [%s] %s" % (p["pair_id"], p["verdict"].upper()))
        print("     %s" % p["why"][:100])
        print("     A: %-56s %s" % (p["a"][:56], p.get("a_date", "")))
        print("     B: %-56s %s" % (p["b"][:56], p.get("b_date", "")))
        if p.get("current"):
            print("     model suggests keeping: %s" % p["current"][:60])
        print()
    print("rule on one with:")
    print("  python3 %s decide <id> keep-a|keep-b|merge|both-stand --note '...'"
          % os.path.basename(__file__))
    return 0


def cmd_supersede(args):
    return _supersede(args)


def _supersede(args):
    ph = Phases()
    ph.start("candidates")
    pairs = candidate_pairs(limit_mem=args.scan, per_mem=args.per,
                            offset=getattr(args, 'offset', 0))
    ph.stop()
    if args.limit:
        pairs = pairs[:args.limit]
    if not pairs:
        print("no candidate pairs")
        return 0
    print("scanning %d candidate pairs in batches of %d" % (len(pairs), args.batch))

    flagged = []
    for b in range(0, len(pairs), args.batch):
        chunk = pairs[b:b + args.batch]
        ph.start("build")
        blob = "\n\n".join(
            "PAIR %d\n  A: %s [modified %s]\n     %s\n  B: %s [modified %s]\n     %s"
            % (i, a[0], a[2] or "undated", (a[1] or "")[:320],
               c[0], c[2] or "undated", (c[1] or "")[:320])
            for i, (a, c, _s) in enumerate(chunk))
        ph.stop()
        ph.start("api")
        try:
            res = extract_json(call_gemini(SUPERSEDE_PROMPT % blob, max_tokens=1600))
        except Exception as e:
            print("  batch %d failed: %s" % (b // args.batch, str(e)[:90]))
            ph.stop()
            continue
        ph.stop()
        ph.start("collect")
        for r in res:
            i = r.get("pair")
            if not isinstance(i, int) or i >= len(chunk):
                continue
            if r.get("verdict", "fine") != "fine":
                a, c, _ = chunk[i]
                flagged.append((r, a, c))
        ph.stop()

    print("\nflagged %d of %d pairs" % (len(flagged), len(pairs)))
    by = collections.Counter(r["verdict"] for r, _, _ in flagged)
    for v, n in by.most_common():
        print("  %-12s %d" % (v, n))
    for r, a, c in flagged:
        print("\n  [%s] %s" % (r["verdict"].upper(), r.get("why", "")[:96]))
        print("      A: %-58s %s" % (a[0][:58], a[2] or "undated"))
        print("      B: %-58s %s" % (c[0][:58], c[2] or "undated"))
        if r.get("current"):
            print("      -> should stand: %s" % r["current"][:70])
    ph.start("persist")
    already = decided_ids() | {p["pair_id"] for p in read_jsonl(PROPOSALS_PATH)}
    fresh = []
    for r, a, cc in flagged:
        pid = pair_id(a[0], cc[0])
        if pid in already:
            continue
        already.add(pid)
        fresh.append(json.dumps({
            "pair_id": pid, "verdict": r["verdict"], "why": r.get("why", ""),
            "current": r.get("current", ""),
            "a": a[0], "a_date": a[2] or "", "b": cc[0], "b_date": cc[2] or "",
            "found": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")
    if fresh:
        append_locked(PROPOSALS_PATH, "".join(fresh))
    ph.stop()
    print("\nqueued %d NEW proposal(s) for your ruling (%d already known/decided)"
          % (len(fresh), len(flagged) - len(fresh)))
    print("phases: %s" % ph.render())
    print("\nPROPOSALS ONLY. Nothing was changed. Supersession is a judgment about which")
    print("lesson is still true, and that decision is Phil's.")
    return 0


def append_note(title, body):
    """The curator's own persistent memory.

    It is the only component here that accumulates across runs, so what it learns about
    retrieval has somewhere to live. Append-only and dated: a note is evidence of what was
    true at a point in time, not a fact to be silently rewritten later."""
    try:
        head = ("" if os.path.exists(NOTES_PATH) else
                "# Curator notes\n\nWhat the resident memory manager has observed about "
                "retrieval. Append-only, newest last.\n")
        append_locked(NOTES_PATH, head + "\n## %s - %s\n\n%s\n"
                      % (time.strftime("%Y-%m-%d %H:%M"), title, body.rstrip()))
    except Exception:
        oops("append_note")
        pass


LOCKED = [False]


def cmd_tune(args):
    """Recompute per-memory retrieval weights from judged evidence."""
    lock = exclusive("tune")
    if lock is None:
        print("another curator run holds the lock; exiting")
        return 0
    try:
        return _tune(args)
    finally:
        lock.close()


# Prompts that drive the HARNESS rather than the project: one-turn probe sessions whose
# instruction is to run specific shell commands so that some tool behaviour can be observed.
# They are real turns and the judge's "ignored" labels on them are technically correct, but
# they are a prompt population that will never recur, and letting them into the weight
# learner actively DEMOTES good memories.
#
# MEASURED 2026-10-05, which is why this exists rather than being a guess:
#   reference_headroom_kompress_mangled_tool_output_disabled   5/100 overall
#                                                              4/5   on real turns  = 80%
#     95 harness probes made an 80%-useful memory look 5% useful: a 16x under-weighting.
#   agent_teams_messaging_and_lifecycle   3/150 overall, 3/41 real (109 synthetic)
#   safe-process-termination              0/13, with NO non-synthetic evidence at all
# 170 of 2000 labelled turns matched, and they hit at 0.6% against 35.4% for real work.
HARNESS_PROMPT = re.compile(
    r"\b(seq 1 \d|maxdepth|in parallel, run|issue ALL of these|one call per|"
    r"PARALLEL Bash tool calls|/tmp/claude-1000/|reply with just|"
    r"print the exact|verbatim from its schema)\b", re.I)


def is_harness_turn(label_row, turns_per_session=None):
    """True for a one-off probe that drives the tooling rather than the work.

    Deliberately conservative, requiring BOTH signals, because a false positive here
    discards real evidence: the instrument vocabulary AND a session of at most two judged
    turns, which is the shape of a throwaway probe. A long session that happens to contain
    one such command is real work and is kept.
    """
    p = re.sub(r"\s+", " ", (label_row.get("prompt") or ""))
    if not HARNESS_PROMPT.search(p):
        return False
    if turns_per_session is None:
        return True
    return turns_per_session.get(label_row.get("session") or "?", 99) <= 2


def _tune(args):
    if True:
        labels = read_jsonl(LABELS_PATH, dedupe_key=turn_key)
        if not labels:
            print("no labels yet; nothing to learn from")
            return 0
        # Session turn counts, for the conservative half of the harness test.
        sess = collections.Counter()
        tid2sess = {}
        for t in read_jsonl(TURNS_PATH):
            if t.get("tid"):
                tid2sess[t["tid"]] = t.get("session") or "?"
        for l in labels:
            s = tid2sess.get(l.get("tid"))
            if s:
                sess[s] += 1
        skipped_harness = 0
        stat = collections.defaultdict(lambda: {"used": 0, "n": 0, "harmful": 0})
        for l in labels:
            row = dict(l, session=tid2sess.get(l.get("tid")))
            if is_harness_turn(row, sess):
                skipped_harness += 1
                continue
            for name, verdict in (l.get("labels") or {}).items():
                st = stat[name]
                st["n"] += 1
                if verdict == "used":
                    st["used"] += 1
                elif verdict == "harmful":
                    st["harmful"] += 1

        weights, changed = {}, []
        for name, st in stat.items():
            if st["n"] < MIN_EVIDENCE:
                continue
            # Smoothed used-rate. The prior is deliberately pessimistic (most injections
            # genuinely are ignored), so a memory needs real evidence to get boosted and
            # a little evidence to stop being pushed.
            rate = (st["used"] + PRIOR_A) / (st["n"] + PRIOR_A + PRIOR_B)
            base = (st["used"] + PRIOR_A) / (0 + PRIOR_A + PRIOR_B)
            w = W_FLOOR + (W_CEIL - W_FLOOR) * min(rate / 0.5, 1.0) if rate else W_FLOOR
            if st["harmful"]:
                w = W_FLOOR
            w = round(max(W_FLOOR, min(W_CEIL, w)), 3)
            if abs(w - 1.0) > 0.001:
                weights[name] = w
                changed.append((name, w, st))

        payload = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "min_evidence": MIN_EVIDENCE,
                   "floor": W_FLOOR, "ceil": W_CEIL,
                   "weights": weights}
        if args.apply:
            tmp = "%s.tmp.%d" % (WEIGHTS_PATH, os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, WEIGHTS_PATH)   # atomic: hooks may read this concurrently

        print("labels %d (%d harness turns EXCLUDED)   memories with >=%d injections: %d"
              "   weighted: %d"
              % (len(labels), skipped_harness, MIN_EVIDENCE,
                 sum(1 for st in stat.values() if st["n"] >= MIN_EVIDENCE), len(weights)))
        for name, w, st in sorted(changed, key=lambda x: x[1])[:14]:
            arrow = "DOWN" if w < 1 else "UP  "
            print("  %s %.3f  used %d/%d%s  %s"
                  % (arrow, w, st["used"], st["n"],
                     "  HARMFUL" if st["harmful"] else "", name[:52]))
        if not args.apply:
            print("\nDRY RUN. Re-run with --apply to write weights.json.")
        else:
            append_note("tune", "Reweighted %d memories from %d labelled turns. "
                                "Floor %.2f ceil %.2f, min evidence %d."
                        % (len(weights), len(labels), W_FLOOR, W_CEIL, MIN_EVIDENCE))
            print("\nwrote %s" % WEIGHTS_PATH)
        return 0


# ---------------------------------------------------------------------------
# MEMORY PROPOSALS. The one organ that can add to the corpus.
#
# Everything else here is derived and disposable. This writes .md files, so it is
# deliberately the most conservative thing in the system: a lesson must either be
# approved explicitly, or recur often enough to HEAT past a threshold, before any
# file is written. A single incident never creates a memory on its own.
# ---------------------------------------------------------------------------

MEM_PROPOSALS_PATH = os.path.join(INDEX_DIR, "mem-proposals.jsonl")
PROPOSE_CURSOR = os.path.join(INDEX_DIR, ".propose-cursor")
ROLLUP_PATH = os.path.join(INDEX_DIR, "proposals-open.json")
FULL_INDEX_PATH = os.path.join(MEMORY_DIR, "MEMORY-FULL.md")

# HEAT. Each occurrence adds heat; the FIRST occurrence in a given session is worth six
# times a repeat within that same session. Rationale: a SECOND SESSION independently
# running into the same thing is the real evidence, because it cannot be an artifact of
# one incident; further turns inside one session are mostly that incident restating
# itself. Within-session recurrence still counts, it just cannot finish the job alone.
#
# RECALIBRATED 2026-10-05 against the whole occurrence log, because the old numbers had
# exactly one reachable path and nothing ever reached it:
#
#   749 open proposals   738 seen in ONE session   11 in two   0 in three
#   occurrences per proposal: 436x1  190x2  72x3  30x4  15x5  5x6  1x8   NOTHING AT 9+
#
# The old curve (1.0 / 0.25 / cap 3.0) promoted on 3 sessions or 9 turns in one session.
# No proposal has ever hit either, so 0 of 749 could promote and the queue was a
# graveyard that made the SessionStart banner unreadable - which is how a 2-week-dead API
# key went unnoticed in it. The single-session path is now dropped as the dead code it
# always was: one session CANNOT create, at any depth.
#
# At these numbers: TWO sessions create (1.5 + 1.5). One session runs 1.5 -> 2.5, which
# keeps the queue ordering informative and leaves a deep single-session proposal sitting
# just under the bar where a ruling can see it.
HEAT_FIRST = 1.5
HEAT_REPEAT = 0.25
HEAT_CREATE = 3.0
# No single session may contribute more than this. See heat_of for the measured runaway.
HEAT_SESSION_CAP = 2.5

# UNATTENDED creation needs MORE than eligibility, and this gap was measured the same hour
# the curve above was set. Reviewing the first 11 proposals the new curve lifted to 3.0
# found 3 of them carried a MIS-BUCKETED occurrence - a different lesson entirely, filed
# under the same proposal by the proposer:
#   consulting-resource-front-loading  occ4 was about DroidGuard emulator dead-ends
#   user-shell-hygiene-preference      occ2 was about file permissions, not shells
#   memory-index-integrity-strategy    occ2 was about restoring git receipts
# Because session #2 is now decisive, ONE mis-bucketed occurrence is enough to reach 3.0.
# So 3.0 means "eligible for a ruling" and only HEAT_AUTO creates with nobody looking.
# 4.0 requires two sessions where one saw it REPEATEDLY (2.5 + 1.5), or three sessions.
# Of those first 11, review rejected 5 and held 1; none exceeded 3.5, so this bar would
# have let a human see every one of them - which is the outcome that was wanted.
HEAT_AUTO = 4.0

# A proposal whose LAST occurrence is this old, and which only ever appeared in ONE
# session, is not recurring: it has had the whole window to show up again and did not.
# It is put on `hold`, NEVER `reject`. Hold is re-openable - a later occurrence reopens it
# from scratch - so a lesson from a project Phil returns to in three months comes straight
# back. That is the difference between closing a queue and losing evidence, and it is why
# HEAT_DECAY below is still off: this rule needs no decay curve, only a clock.
STALE_HOLD_DAYS = 14

# REVISE tuning. Evidence is ranked by novelty against the target file and only the top
# slice is sent; below the floor the revision is refused outright, because a model handed
# only restatements will invent something to justify the call it was asked to make.
REVISE_EVIDENCE = 8
REVISE_MIN_NOVELTY = 0.45
# DECAY IS DELIBERATELY OFF. It cannot be designed without a usage signal to reverse
# it, and Phil returns to projects after months away; a decay that silently retired a
# dormant-but-correct proposal would be indistinguishable from it never existing.
# Periodic manual audits handle retirement today. Do not switch this on on its own.
HEAT_DECAY = None

PROPOSE_PER_CYCLE = 24   # turns scanned per cycle
PROPOSE_BATCH = 6        # turns per API call
PROPOSE_LISTING_MAX = 60 # open proposals named in the prompt; see _propose for why
MATCH_J = 0.45           # JACCARD at which two proposals are the SAME lesson
# CONTAINMENT at which the corpus already covers a proposal. Lowered from 0.55 after a
# MEASURED true positive at 0.45: `runwait-for-harness-visibility` scored 0.45 against
# feedback_a_detached_job_never_notifies_me and is word-for-word the same lesson. The error
# is asymmetric in the safe direction - a false positive only downgrades a proposal to
# `refine`, which surfaces for a ruling instead of auto-creating.
COVER_J = 0.45


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _clean(s):
    """Collapse whitespace and strip em-dashes. The corpus has a standing ban on them."""
    return re.sub(r"\s+", " ", (s or "")).replace("—", "-").replace("–", "-").strip()


def _toks(s):
    return set(re.findall(r"[a-z0-9]{4,}", (s or "").lower()))


def jaccard(a, b):
    """Symmetric similarity. Right for proposal-vs-proposal, where both sides are one
    short description and a length mismatch really does mean a different lesson."""
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def containment(small, big):
    """How much of `small` is already inside `big`.

    Used for the corpus coverage check, where Jaccard is the WRONG instrument: an 8-token
    proposal fully contained in a 40-token memory description scores ~0.2 by Jaccard and
    sails through as new. MEASURED: "memory-index-truncation" passed the Jaccard check
    against feedback_memory_index_ceiling_400, which says exactly that.
    """
    if not small or not big:
        return 0.0
    return len(small & big) / float(len(small))


COVER_LIMIT = 12         # candidates the coverage check considers
COVER_TERMS = 14         # query terms taken from the proposal description


def nearest_memories(descriptions, k=3):
    """{description: [memory stem, ...]} using the PRODUCTION searcher, reranker included.

    WHY THIS IS NOT THE COVERAGE CHECK IN cmd_propose. The two want different trade-offs,
    and the measurement is unambiguous (coverage-calibration.json, 19 hand-ruled pairs):

        cmd_propose's lexical candidates, cited memory found    5/19
        production search() WITHOUT the reranker                5/19
        production search() WITH the reranker                  12/19

    The whole gain is the rerank, because a proposal's description is written in generic
    vocabulary ("verify", "strategy", "server-side") while the memory that already covers it
    is written in specific vocabulary ("Retry-After", "511", "msisdn"), and BM25 cannot
    bridge that. So the good instrument costs an LLM call per proposal, which is why it runs
    HERE, on the handful a reviewer actually looks at, and not in cmd_propose on all of them.
    cmd_propose keeps the cheap lexical check for the `refine` downgrade it has to do inline.

    This exists because of what the 19 cases cost by hand: 19 of the 22 hottest proposals
    were duplicates of an existing memory, five of them REFUTED by a memory the same session
    wrote later, and finding that out meant a manual search per proposal. Printing the
    nearest memories next to the proposal turns each of those rulings into a glance.

    Fails open to {}: a missing key or a dead searcher must degrade the display, not break
    the review.
    """
    if not descriptions:
        return {}
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory-search.py")
        spec = importlib.util.spec_from_file_location("memory_search", path)
        ms = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ms)
        ms.ensure_fresh(ms.source_files())
    except Exception:
        oops("nearest_memories.load")
        return {}
    out = {}
    for desc in descriptions:
        try:
            terms = ms.make_terms(desc)
            if len(terms) < 2:
                continue
            hits = ms.search(" OR ".join('"%s"' % t for t in terms), terms,
                             raw_prompt=desc) or []
            names = []
            for h in hits:
                paths = [x for x in h if isinstance(x, str) and x.endswith(".md")]
                if paths:
                    names.append(os.path.basename(paths[0])[:-3])
            out[desc] = names[:k]
        except Exception:
            oops("nearest_memories.search")
    return out


def coverage_candidates(con, desc):
    """Existing memories that might already cover a proposal: (name, description) rows.

    ONE definition, because measure-coverage-check.py scores this exact function against the
    19 hand-ruled pairs in coverage-calibration.json. An inlined copy in the measuring script
    is how a calibration row ends up describing behaviour the code stopped having: the first
    version of that script hardcoded the old term handling and kept reporting 2/19 after the
    fix landed.

    Terms are LONGEST FIRST and that is a bug fix, not tuning: `_toks` returns a set, so the
    old `list(_toks(desc))[:14]` kept whichever terms hash order put first.
    """
    terms = sorted(_toks(desc), key=lambda t: (-len(t), t))[:COVER_TERMS]
    if not terms:
        return []
    return con.execute(
        "SELECT name, description FROM mem WHERE mem MATCH ? "
        "ORDER BY bm25(mem,3.0,5.0,1.0,0,0,0,0,0) LIMIT ?",
        (" OR ".join('"%s"' % t for t in terms), COVER_LIMIT)).fetchall()


def prop_id(name):
    import hashlib
    return hashlib.sha1(re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-").encode()
                        ).hexdigest()[:12]


def heat_of(occurrences):
    """First occurrence per session at full weight, repeats within it at a quarter,
    and NO session may contribute more than HEAT_SESSION_CAP on its own.

    MEASURED 2026-09-30, the reason the cap exists: one proposal reached heat 30.25 from
    118 occurrences in a SINGLE session (1.0 + 0.25*117). The comment above HEAT_REPEAT
    promised "one session alone needs 9 turns" and nothing enforced the other half of that
    sentence - past 9 turns a single session could manufacture arbitrary heat.

    RECALIBRATED 2026-10-05: the cap is now BELOW HEAT_CREATE, so one session cannot
    create at any depth. That is not a tightening, it is deleting an unreachable branch -
    no proposal in the entire log has ever exceeded 8 occurrences in one session, so the
    "9 turns in one session can still create" path had never fired once. See the
    constants above for the measured distribution.
    """
    per_session, order = {}, []
    for o in sorted(occurrences, key=lambda x: x.get("ts") or ""):
        s = o.get("session") or o.get("tid")
        if s not in per_session:
            per_session[s] = 0.0
            order.append(s)
        per_session[s] += HEAT_FIRST if per_session[s] == 0.0 else HEAT_REPEAT
    return round(sum(min(v, HEAT_SESSION_CAP) for v in per_session.values()), 2)


def read_mem_proposals():
    """Reduce the append-only occurrence log into one record per proposed memory.

    Event-sourced like everything else here: `propose` opens it, further `propose` rows
    add evidence, `rule` closes it, `created` records the file. Reducing on read means a
    duplicate row can never corrupt state, only repeat itself, and heat is always
    recomputed from the evidence rather than stored and incremented.
    """
    out = {}
    for r in read_jsonl(MEM_PROPOSALS_PATH):
        pid = r.get("pid")
        if not pid:
            continue
        if r.get("event") == "rule":
            if pid in out:
                out[pid]["ruling"] = r.get("ruling")
                out[pid]["note"] = r.get("note", "")
            continue
        # A `propose` row arriving AFTER a hold re-opens the proposal from scratch. Rows are
        # appended chronologically, so "after" is just "later in the file".
        # MEASURED 2026-09-30: without this, `hold` was permanent, and both its docstring
        # and the README claimed the opposite - "a genuine recurrence re-opens it from
        # scratch" was the entire reason the verb existed, and it had never once worked.
        # `reject` stays permanent on purpose: that is a considered no, not a not-yet.
        if pid in out and out[pid]["ruling"] == "hold":
            out[pid]["ruling"] = None
            out[pid]["occurrences"] = []       # from scratch: pre-hold evidence is spent
        if r.get("event") == "created":
            if pid in out:
                out[pid]["created_path"] = r.get("path", "")
            continue
        if r.get("event") == "escalate":
            if pid in out:
                out[pid]["escalated"] = r.get("ts", "")
            continue
        rec = out.setdefault(pid, {
            "pid": pid, "name": r.get("name", ""), "description": r.get("description", ""),
            "mtype": r.get("mtype", "feedback"), "kind": r.get("kind", "new"),
            "target": r.get("target", ""), "occurrences": [], "ruling": None,
            "created_path": "", "note": "", "escalated": "",
        })
        # The opening row owns the identity; later rows only add evidence, so a
        # slightly-reworded repeat cannot rename the proposal out from under itself.
        if not rec["occurrences"]:
            rec["name"] = r.get("name", rec["name"])
            rec["description"] = r.get("description", rec["description"])
            rec["mtype"] = r.get("mtype", rec["mtype"])
            rec["kind"] = r.get("kind", rec["kind"])
            rec["target"] = r.get("target", rec["target"])
        # Keyed on (session, tid), NOT tid alone. MEASURED: turn_key() falls back to `ts`
        # for rows written before tid existed, and two sessions that wrote a turn in the
        # same second collide. Deduping on tid alone silently merged two real occurrences
        # into one and undercounted heat.
        if (r.get("session"), r.get("tid")) not in {
                (o.get("session"), o.get("tid")) for o in rec["occurrences"]}:
            rec["occurrences"].append({
                "tid": r.get("tid"), "session": r.get("session"), "ts": r.get("ts"),
                "lesson": r.get("lesson", ""), "prompt": r.get("prompt", ""),
                "excerpt": r.get("excerpt", ""),
            })
    for rec in out.values():
        rec["heat"] = heat_of(rec["occurrences"])
    return out


def open_mem_proposals():
    """Proposals still awaiting an outcome: not ruled, not already created."""
    return {p: r for p, r in read_mem_proposals().items()
            if not r["ruling"] and not r["created_path"]}


def write_rollup():
    """Publish open proposals for the SessionStart hook to read verbatim.

    The hook must NOT recompute heat. MEASURED on the first run: an inline copy of the
    formula in the hook read 3.75 where the curator read 3.00, because the two disagreed
    about what counts as one occurrence. One writer of the number, one reader.
    """
    try:
        # BOTH bars are published, because the hook must not restate either. The hook once
        # carried its own copy of the heat formula and read 3.75 where the curator read
        # 3.00; the same class of drift then made the banner advertise "auto-creates at 3.0"
        # for hours after HEAT_AUTO moved to 4.0. One writer of a number, one reader.
        rollup = {"threshold": HEAT_CREATE, "auto": HEAT_AUTO, "written": _now(), "open": [
            {"pid": r["pid"], "name": r["name"], "heat": r["heat"], "kind": r["kind"],
             "target": r["target"], "description": r["description"][:120],
             "occurrences": len(r["occurrences"]),
             "escalated": r.get("escalated", ""),
             "sessions": len({o.get("session") for o in r["occurrences"]})}
            for r in sorted(open_mem_proposals().values(), key=lambda x: -x["heat"])],
            "created": [{"path": os.path.basename(r["created_path"]), "name": r["name"]}
                        for r in read_mem_proposals().values() if r["created_path"]]}
        tmp = "%s.tmp.%d" % (ROLLUP_PATH, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rollup, fh, ensure_ascii=False)
        os.replace(tmp, ROLLUP_PATH)       # atomic; the hook may read this concurrently
    except Exception:
        oops("write_rollup")


PROPOSE_PROMPT = """You are reading turns from an AI coding assistant's session log, \
looking for DURABLE LESSONS worth writing into the developer's permanent memory.

A turn is worth a memory ONLY when it contains something that will still be true and \
useful WEEKS from now, in a DIFFERENT session, and that is not already obvious from \
reading the code or the git history. Almost all turns are not. Default to proposing \
NOTHING.

Propose when a turn shows one of:
  feedback   a correction, a preference, or a way of working the developer asked for,
             or a failure mode worth never repeating. Capture WHY, not just what.
  project    ongoing work, a goal, or a constraint that is not derivable from the repo.
  reference  a concrete external fact: an API shape, a port, a path, a tool's behaviour.
  user       something about who the developer is or how they prefer to work.

Do NOT propose:
  - anything that only matters inside this one conversation
  - a restatement of what the code already says
  - a task that was completed (that is history, not a lesson)
  - vague advice that would apply to any project

Some lessons are ALREADY PROPOSED and waiting. If a turn shows the same lesson as one of \
these, reuse its EXACT name so the evidence accumulates instead of forking:
%s

TURNS:
%s

Return ONLY JSON, an array (possibly empty):
[{"turn": <index>, "name": "<short-kebab-case-slug>", "type": "feedback|project|reference|user",
  "description": "<one line, what the lesson IS>",
  "lesson": "<2-3 sentences: what happened, and what to do differently next time>"}]
"""


def escalate_refinements(dry=False):
    """Move refine proposals that crossed the bar into their terminal state.

    Runs BEFORE the new-turn check, not after it. MEASURED: when this lived at the end of
    _propose it never executed on a quiet machine, because _propose returns early when no
    new turns exist - so the one thing that closes a stuck refinement only ran when there
    was unrelated work to do. Returns the number escalated.
    """
    n = 0
    for rec in open_mem_proposals().values():
        if rec["kind"] != "refine" or rec["heat"] < HEAT_CREATE or rec["escalated"]:
            continue
        if dry:
            print("  would ESCALATE (%.2f): %s -> %s  [--dry]"
                  % (rec["heat"], rec["name"], rec["target"]))
            continue
        append_locked(MEM_PROPOSALS_PATH, json.dumps({
            "event": "escalate", "pid": rec["pid"], "ts": _now(),
            "target": rec["target"], "heat": rec["heat"],
            "occurrences": len(rec["occurrences"]),
        }, ensure_ascii=False) + "\n")
        print("  ESCALATED (%.2f): %s -> needs an edit to %s"
              % (rec["heat"], rec["name"], rec["target"]))
        live("escalated %s -> %s" % (rec["name"], rec["target"]))
        n += 1
    if n:
        write_rollup()
    return n


def cmd_health(args):
    """Print the full organ health table, healthy rows included.

    The SessionStart banner deliberately says nothing when everything is fine, so this is
    the command for asking the question directly. It is also the positive control for the
    banner: if this shows organs succeeding and the banner is silent, the silence is
    trustworthy. Without it, "no banner" and "banner broken" would be indistinguishable,
    which is the exact failure mode the whole signal was built to remove.
    """
    r = organ_health.rollup()
    print("organ health as of %s   (%d rows in the log)" % (r["checked"], r["rows"]))
    if not r["organs"]:
        print("  no LLM calls recorded yet. The log fills as organs run; until then this is\n"
              "  'never observed', NOT 'healthy'.")
        return 0
    print("  %-18s %-9s %-11s %-11s %s" % ("organ", "state", "24h", "7d", "note"))
    for name, v in sorted(r["organs"].items(), key=lambda kv: kv[0]):
        note = v["kind"].upper() if v["kind"] else ""
        if v["billing_suspect"]:
            note += " <- sustained %.0fh, SUSPECT BILLING" % v["streak_hours"]
        elif v["ok24"] == 0 and v["att24"]:
            note += " <- last ok %.0fh ago" % v["down_hours"]
        print("  %-18s %-9s %-11s %-11s %s"
              % (name, v["state"], "%d/%d" % (v["ok24"], v["att24"]),
                 "%d/%d" % (v["ok7d"], v["att7d"]), note))
    print("\noverall: %s" % r["worst"].upper())
    b = organ_health.banner()
    print("\nSessionStart would print:\n%s" % (b if b else "  (nothing - healthy)"))
    return 0


def hold_stale_proposals(dry=False):
    """Put non-recurring single-session proposals on hold once the window has passed.

    WHY A QUEUE NEEDS AN EXIT. MEASURED 2026-10-05: 749 open proposals, 738 of them seen in
    exactly one session, none at threshold. Nothing was wrong with any individual proposal;
    there was simply no path OUT except promotion, and promotion needs recurrence that most
    one-shots will never have. The queue was therefore monotonic, and an ever-growing
    "201 awaiting your ruling / 746 accumulating evidence" banner at SessionStart is one
    nobody reads - which is exactly how 340 API-key failures sat in the same banner for two
    weeks. An unbounded queue does not just cost tokens, it destroys the signal of every
    other thing reported next to it.

    HOLD, NOT REJECT, and the distinction is the whole safety argument. `reject` is
    permanent, a considered no. `hold` is re-opened from scratch by any later occurrence
    (see read_mem_proposals). So this closes the queue without spending evidence: a lesson
    from a project Phil picks back up in three months reopens the moment it recurs. That
    is why this is not the decay HEAT_DECAY deliberately refuses to be - decay retires a
    proposal on a timer and cannot tell dormant from dead, whereas this is reversed by the
    very event that would have proved it alive.

    Only ever holds SINGLE-session proposals. A proposal two independent sessions have run
    into is recurring by definition and is left alone however old it is.
    """
    import datetime
    now = datetime.datetime.now()
    held = 0
    for rec in sorted(open_mem_proposals().values(), key=lambda r: r["name"]):
        if len({o.get("session") for o in rec["occurrences"]}) >= 2:
            continue
        stamps = [o.get("ts") for o in rec["occurrences"] if o.get("ts")]
        if not stamps:
            continue
        try:
            # FIRST occurrence, not last, and the difference decides whether this rule works
            # at all. MEASURED 2026-10-05: with `max(stamps)` the hold would have fired on
            # 0 of 376 open proposals, because every one was single-session and the three
            # sessions holding 240 of them were STILL RUNNING - a weeks-long session keeps
            # re-emitting its own lessons, so `last seen` never ages past today and the queue
            # is monotonic exactly as before. `min(stamps)` asks the question this rule is
            # for: how long has this lesson sat WITHOUT a second session confirming it. On
            # the same data that held 34 immediately and bounds the queue at ~14 days of
            # inflow. Within-session repeats are not independent evidence anyway, which is
            # why HEAT_SESSION_CAP already refuses to count them past 2.5.
            age = (now - datetime.datetime.fromisoformat(min(stamps)[:19])).days
        except Exception:
            continue                      # an unparsable stamp is not grounds to close
        if age < STALE_HOLD_DAYS:
            continue
        if dry:
            print("  would HOLD (%.2f, %dd, 1 session): %s  [--dry]"
                  % (rec["heat"], age, rec["name"][:60]))
            held += 1
            continue
        append_locked(MEM_PROPOSALS_PATH, json.dumps({
            "event": "rule", "pid": rec["pid"], "ts": _now(), "ruling": "hold",
            "note": "auto: one session only, last seen %dd ago (>= %d). Reopens on any "
                    "recurrence." % (age, STALE_HOLD_DAYS),
        }, ensure_ascii=False) + "\n")
        held += 1
    if held and not dry:
        write_rollup()
        live("held %d stale single-session proposals" % held)
    return held


def compact_mem_proposals(dry=False):
    """Shrink mem-proposals.jsonl by dropping redundant evidence rows for CLOSED proposals.

    A BLIND TAIL WOULD CORRUPT THIS FILE. It is append-only and reduced on read, and heat is
    recomputed from the occurrence rows every time. Cutting the oldest bytes drops
    FIRST-occurrence rows while keeping later repeats, so a proposal would either lose heat
    or vanish while its evidence remained - a silently wrong number rather than a missing
    one. cmd_prune's tail_file is therefore deliberately NOT pointed at this file.

    What is safe is semantic: for a proposal that is already closed (created, rejected or
    held) the extra occurrence rows no longer affect any output. Held proposals are the
    clearest case - read_mem_proposals explicitly CLEARS their occurrences when a later row
    reopens them, so that evidence is already spent by design. One opening row is kept per
    closed proposal because it owns the proposal's identity (name, description, type), and
    every `rule`/`created`/`escalate` row is kept because those ARE the state.

    OPEN proposals are left completely untouched: their rows are the live evidence base.
    """
    rows = read_jsonl(MEM_PROPOSALS_PATH)
    allp = read_mem_proposals()
    closed = {p for p, r in allp.items() if r["ruling"] or r["created_path"]}
    kept, seen_open_row, dropped = [], set(), 0
    for r in rows:
        pid = r.get("pid")
        if pid not in closed or r.get("event") != "propose":
            kept.append(r)                      # open proposal, or a state row
            continue
        if pid not in seen_open_row:
            seen_open_row.add(pid)
            kept.append(r)                      # the identity-owning first row
            continue
        dropped += 1
    if not dropped:
        print("  mem-proposals.jsonl: nothing to compact")
        return 0
    before = os.path.getsize(MEM_PROPOSALS_PATH)
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept)
    if dry:
        print("  mem-proposals.jsonl: would drop %d of %d rows, %.0fKB -> %.0fKB  [--dry]"
              % (dropped, len(rows), before / 1024, len(body.encode()) / 1024))
        return dropped
    # VERIFY BEFORE REPLACING, against the live reducer rather than by reasoning about it:
    # every OPEN proposal must come back with the same heat, kind and occurrence count.
    tmp = "%s.tmp.%d" % (MEM_PROPOSALS_PATH, os.getpid())
    open(tmp, "w", encoding="utf-8").write(body)
    saved = MEM_PROPOSALS_PATH
    try:
        globals()["MEM_PROPOSALS_PATH"] = tmp
        after = read_mem_proposals()
    finally:
        globals()["MEM_PROPOSALS_PATH"] = saved
    for pid, r in allp.items():
        if r["ruling"] or r["created_path"]:
            continue
        a = after.get(pid)
        if not a or a["heat"] != r["heat"] or len(a["occurrences"]) != len(r["occurrences"]):
            os.remove(tmp)
            print("  REFUSING to compact: open proposal %s would change (heat %s -> %s)"
                  % (pid, r["heat"], a["heat"] if a else "GONE"))
            return 0
    os.replace(tmp, MEM_PROPOSALS_PATH)
    print("  mem-proposals.jsonl: dropped %d of %d rows, %.0fKB -> %.0fKB "
          "(all %d open proposals verified unchanged)"
          % (dropped, len(rows), before / 1024,
             os.path.getsize(MEM_PROPOSALS_PATH) / 1024,
             sum(1 for r in allp.values() if not r["ruling"] and not r["created_path"])))
    return dropped


def _propose(args):
    """Scan recent turns for lessons worth a memory. Proposes and heats; writes only
    what has crossed the heat threshold."""
    dry = bool(getattr(args, "dry", False))
    escalate_refinements(dry=dry)
    # Before scanning for anything new. Same reasoning as escalate_refinements: _propose
    # returns early when there are no fresh turns, so a step placed after that check only
    # runs when there is unrelated work to do, and the one thing that drains the queue
    # would never fire on a quiet machine.
    n = hold_stale_proposals(dry=dry)
    if n:
        print("  held %d stale single-session proposal(s)" % n)
    turns = read_jsonl(TURNS_PATH)
    try:
        cursor = open(PROPOSE_CURSOR).read().strip()
    except Exception:
        cursor = ""
    fresh = [t for t in turns if (t.get("ts") or "") > cursor and t.get("response")]
    if not fresh:
        print("no new turns to scan for proposals")
        return 0
    fresh = fresh[:getattr(args, "limit", 0) or PROPOSE_PER_CYCLE]
    live("propose: scanning %d turns" % len(fresh))
    print("scanning %d turns for durable lessons" % len(fresh))

    openp = open_mem_proposals()
    # Only the hottest few. MEASURED 2026-09-30 at 848 open proposals: the full listing was
    # 107KB, ~27k tokens, embedded in EVERY propose call and growing without bound. This is
    # a HINT to reuse an exact name; the authoritative match is the Jaccard check below,
    # which still runs against every open proposal, so capping costs no matching accuracy.
    # The hottest are also the ones most likely to recur and so most worth naming.
    listing = "\n".join(
        "  %s: %s" % (r["name"], r["description"][:90])
        for r in sorted(openp.values(), key=lambda x: -x["heat"])[:PROPOSE_LISTING_MAX]
    ) or "  (none)"

    found = []
    for b in range(0, len(fresh), PROPOSE_BATCH):
        chunk = fresh[b:b + PROPOSE_BATCH]
        blob = "\n\n".join(
            "TURN %d\n  developer said: %s\n  assistant replied: %s"
            % (i, (t.get("prompt") or "")[:700], (t.get("response") or "")[:1400])
            for i, t in enumerate(chunk))
        try:
            res = extract_json(call_gemini(PROPOSE_PROMPT % (listing, blob), max_tokens=2048))
        except Exception as e:
            oops("cmd_propose.api")
            print("  batch failed: %s" % str(e)[:80])
            continue
        for r in res or []:
            try:
                t = chunk[int(r["turn"])]
            except Exception:
                continue
            if r.get("name") and r.get("description"):
                found.append((t, r))

    added = heated = covered = skipped_escalated = 0
    con = sqlite3.connect(DB_PATH)
    for t, r in found:
        name = re.sub(r"[^a-z0-9]+", "-", str(r["name"]).lower()).strip("-")[:80]
        if not name:
            continue
        desc = _clean(str(r["description"]))[:400]
        cand = _toks(name.replace("-", " ") + " " + desc)

        # 1. ALREADY PROPOSED? Heat it. This check runs FIRST and on its own, so the
        #    corpus pre-check below can never suppress a heat-up: a proposal that is
        #    also covered by an existing memory still accumulates its evidence.
        pid, kind, target = None, "new", ""
        escalated = False
        for p, rec in openp.items():
            if rec["name"] == name or jaccard(cand, _toks(
                    rec["name"].replace("-", " ") + " " + rec["description"])) >= MATCH_J:
                pid, kind, target = p, rec["kind"], rec["target"]
                escalated = bool(rec.get("escalated"))
                break
        if escalated:
            # Already escalated and waiting on a human. More evidence changes nothing about
            # what happens next, so recording it only grows the log and re-pays the API.
            # This is the half that was missing: matching it stops a duplicate proposal,
            # and skipping the append stops the 118-occurrence accrual.
            skipped_escalated += 1
            continue

        if pid is None:
            # 2. ALREADY IN THE CORPUS? Then this is not a new memory, it is a REFINEMENT
            #    of one that exists. Recorded as such: it still accumulates evidence and
            #    still surfaces, but it never auto-creates a twin of a memory we have.
            pid = prop_id(name)
            try:
                # MEASURED on the 19 hand-ruled pairs in coverage-calibration.json: the
                # deterministic terms and the wider limit move candidate recall 2/19 ->
                # 5/19. The rest of the gap is NOT a threshold problem, so COVER_J is
                # deliberately left alone - measure-coverage-check.py scores
                # coverage_candidates() itself, which is why this is a call and not a query.
                hits = coverage_candidates(con, desc)
            except Exception:
                oops("cmd_propose.cover")
                hits = []
            for hn, hd in hits:
                if containment(cand, _toks(hn + " " + (hd or ""))) >= COVER_J:
                    kind, target = "refine", hn
                    covered += 1
                    break
            added += 1
        else:
            heated += 1

        append_locked(MEM_PROPOSALS_PATH, json.dumps({
            "event": "propose", "pid": pid, "ts": _now(), "tid": turn_key(t),
            "session": t.get("session"), "name": name, "description": desc,
            "mtype": str(r.get("type", "feedback")),
            "kind": kind, "target": target,
            "lesson": _clean(str(r.get("lesson", "")))[:1200],
            "prompt": (t.get("prompt") or "")[:400],
            "excerpt": (t.get("response") or "")[:700],
        }, ensure_ascii=False) + "\n")
        openp.setdefault(pid, {"pid": pid, "name": name, "description": desc, "kind": kind,
                               "target": target, "occurrences": [], "ruling": None,
                               "created_path": ""})
    con.close()

    try:
        open(PROPOSE_CURSOR, "w").write(fresh[-1].get("ts") or "")
    except Exception:
        oops("cmd_propose.cursor")

    print("  %d new proposal(s), %d heated, %d already covered by an existing memory, "
          "%d skipped (already escalated)" % (added, heated, covered, skipped_escalated))
    live("propose: +%d new, %d heated" % (added, heated))

    # AUTO-CREATE needs HEAT_AUTO, not HEAT_CREATE. `refine` never auto-creates: the
    # right action there is editing a memory that exists, which is Phil's call.
    # HEAT_CREATE only makes a proposal ELIGIBLE, which surfaces it for a ruling. The gap
    # between the two bars is where a mis-bucketed occurrence gets caught by a human; see
    # the HEAT_AUTO comment for the 3-in-11 measurement that put it there.
    openp = open_mem_proposals().values()
    hot = [r for r in openp if r["heat"] >= HEAT_AUTO and r["kind"] != "refine"]
    eligible = [r for r in openp
                if HEAT_CREATE <= r["heat"] < HEAT_AUTO and r["kind"] != "refine"]
    if eligible:
        print("  %d proposal(s) eligible at >= %.1f but below the %.1f auto bar - "
              "awaiting a ruling (`proposed`, then `approve <pid> create|reject|hold`):"
              % (len(eligible), HEAT_CREATE, HEAT_AUTO))
        for rec in sorted(eligible, key=lambda x: -x["heat"])[:10]:
            print("    %.2f  %s" % (rec["heat"], rec["name"][:64]))
    if getattr(args, "dry", False):
        for rec in hot:
            print("  HOT (%.2f): %s  [--dry, not written]" % (rec["heat"], rec["name"]))
        write_rollup()
        return 0
    wrote = 0
    for rec in hot:
        print("  HOT (%.2f): %s" % (rec["heat"], rec["name"]))
        if write_memory(rec, reason="heat %.2f >= %.1f" % (rec["heat"], HEAT_AUTO)):
            wrote += 1
    if wrote:
        reindex()
    write_rollup()
    return 0


def cmd_propose(args):
    lock = exclusive("propose")
    if lock is None:
        print("a curator run is already active; proposals will be scanned by it")
        return 0
    try:
        return _propose(args)
    finally:
        lock.close()


MEM_WRITE_PROMPT = """Write ONE memory file for a developer's personal memory corpus, \
from the accumulated evidence below. The same lesson was observed %d time(s).

FORMAT, exactly this, starting with the --- line:

---
name: %s
description: "<ONE line under 300 chars. State the LESSON ITSELF, not that a lesson exists.
  When the count above is 2 or more, begin with the literal word MEASURED, then that count,
  then 'x:' - for a count of 3 that is exactly 'MEASURED 3x:'. Substitute the real number;
  never emit the letter N. When the count is 1, write NO prefix at all: one observation is
  not a measurement, and claiming otherwise inflates the evidence the next reader acts on.>"
metadata:
  type: %s
  auto: true
---

<The body, 100-250 words. Write what is true and what to do about it, addressed to the
person who reads it months from now with no memory of today. Be concrete: name the real
commands, paths, error text and numbers that appear in the evidence. No preamble.>
%s

RULES:
- Write from ALL the evidence, not just the first occurrence. What generalises ACROSS the
  occurrences is the memory; what is specific to one of them is an example at most.
- NEVER use em-dashes. Plain hyphens only.
- NEVER write PHIL-LOCKED, PHIL-ASKED, or any claim that the developer mandated something,
  unless the evidence literally shows him saying it.
- Do not invent numbers. Every figure must appear in the evidence.
- Where one plainly applies, link a related memory as [[its-name]] from: %s

EVIDENCE:
%s

Return the file content only.
"""


def write_memory(rec, reason=""):
    """Write one proposed memory to disk. The only file-creating path in the system.

    Guardrails, all deliberate:
      - lands a pointer in MEMORY-FULL.md, never the loaded MEMORY.md tier, which has a
        hard 200-line / 25KB ceiling and is Phil's curated front page
      - carries `auto: true`, so every machine-written memory is findable in one grep
      - refuses to overwrite an existing file
      - strips em-dashes, and downgrades any PHIL-LOCKED claim it tries to manufacture
    """
    fname = "%s_%s.md" % (rec["mtype"], rec["name"].replace("-", "_"))
    path = os.path.join(MEMORY_DIR, fname)
    if os.path.exists(path):
        print("    refusing to overwrite existing file: %s" % fname)
        return None

    ev = "\n\n".join(
        "OCCURRENCE %d  [session %s, %s]\n  developer said: %s\n  what was learned: %s\n"
        "  assistant excerpt: %s"
        % (i + 1, (o.get("session") or "?")[:8], (o.get("ts") or "")[:19],
           o.get("prompt", ""), o.get("lesson", ""), (o.get("excerpt") or "")[:500])
        for i, o in enumerate(rec["occurrences"]))

    related = ""
    try:
        con = sqlite3.connect(DB_PATH)
        # Ordered, not a sliced set: `_toks` returns a set, so `list(...)[:12]` kept
        # whichever terms hash order put first. Same defect as coverage_candidates had.
        terms = sorted(_toks(rec["name"].replace("-", " ") + " " + rec["description"]),
                       key=lambda t: (-len(t), t))[:12]
        if terms:
            # matcher-guard: display only. The frontmatter `name` is correct HERE, because
            # this list is shown to the writing model so it can emit [[name]] links, which
            # are frontmatter names by the memory format. Nothing is matched on it. Every
            # other use of this column in this repo was a bug; see test_matcher_uses_path.py.
            related = ", ".join(h[0] for h in con.execute(
                "SELECT name FROM mem WHERE mem MATCH ? "
                "ORDER BY bm25(mem,3.0,5.0,1.0,0,0,0,0,0) LIMIT 5",
                (" OR ".join('"%s"' % t for t in terms),)).fetchall())
        con.close()
    except Exception:
        oops("write_memory.related")

    hint = ("\nFollow the body with a **Why:** line and a **How to apply:** line."
            if rec["mtype"] in ("feedback", "project") else "")
    try:
        text = call_gemini(MEM_WRITE_PROMPT % (
            len(rec["occurrences"]), rec["name"], rec["mtype"], hint,
            related or "(none)", ev), max_tokens=2048)
    except Exception:
        oops("write_memory.api")
        print("    generation failed; leaving the proposal open")
        return None

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
    if not text.startswith("---"):
        print("    model did not return a memory file; leaving the proposal open")
        return None
    text = text.replace("—", "-").replace("–", "-")
    # A standing mandate is a statement about what Phil decided. Nothing should be able
    # to manufacture one; downgrade the wording rather than discard a good memory.
    text = text.replace("PHIL-LOCKED", "observed").replace("PHIL-ASKED", "observed")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")
    append_locked(FULL_INDEX_PATH, "- [%s](%s) - %s\n"
                  % (rec["name"].replace("-", " "), fname, rec["description"][:150]))
    append_locked(MEM_PROPOSALS_PATH, json.dumps({
        "event": "created", "pid": rec["pid"], "ts": _now(), "path": path,
        "reason": reason, "occurrences": len(rec["occurrences"]),
    }, ensure_ascii=False) + "\n")
    print("    WROTE %s  (%s)" % (fname, reason))
    live("created memory %s (%s)" % (fname, reason))
    append_note("memory created", "%s\n  reason: %s\n  evidence: %d occurrence(s)"
                % (fname, reason, len(rec["occurrences"])))
    return path


REVISE_PROMPT = """Revise ONE memory file so it absorbs what has been learned since it was \
written. The refinement recurred across %d session(s) and never fitted into a NEW memory, \
because this memory already covers the subject.

RULES, in order of importance:
- PRESERVE every claim in the current file that the evidence does not contradict. This is a
  revision, not a rewrite. Losing a measured fact is worse than failing to add one.
- Keep the SAME `name:` and the same metadata block, and keep any PHIL-LOCKED or PHIL-ASKED
  wording EXACTLY as it stands. Those record what Phil decided; you may not soften, restate
  or remove one.
- Add only what the evidence supports, and say what is new plainly. Where the evidence
  CORRECTS the file, state the correction and what it replaces, in place. Do not append a
  changelog and do not leave the old claim standing next to the new one.
- Do not invent numbers. Every figure must appear in the current file or the evidence.
- Leave any `MEASURED Nx` count in the description EXACTLY as it stands. N counts independent
  occasions, and the evidence below is one batch, not N new ones. Raising it is inflation.
- Add nothing that merely restates the objective in different words. If the evidence carries
  no NEW fact, return the file UNCHANGED. An unchanged file is a correct answer here.
- NEVER use em-dashes. Plain hyphens only.

CURRENT FILE:
%s

EVIDENCE GATHERED SINCE:
%s

Return the complete revised file only, starting with the --- line.
"""


def revise_memory(rec):
    """Fold an escalated refinement into the memory it refines.

    The terminal state `refine` never had. Backs the original up beside itself first: this
    is the only path in the system that OVERWRITES a memory, so it must be reversible.
    """
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT path FROM mem WHERE name = ?", (rec["target"],)).fetchone()
    con.close()
    if not row or not os.path.isfile(row[0]):
        print("    cannot locate the target memory on disk: %s" % rec["target"])
        return None
    path = row[0]
    current = open(path, encoding="utf-8").read()
    # Rank the evidence by NOVELTY against the file, deduped, and send only the top slice.
    # MEASURED on the first real revision: passing occurrences[:12] in list order sent the
    # twelve most REDUNDANT restatements, so the model saw nothing new, invented a filler
    # sentence, and raised MEASURED 9x to 118x. The two genuinely new facts sat at ranks
    # well below 12 and were never shown to it.
    curtok = _toks(current)
    seen, ranked = set(), []
    for o in rec["occurrences"]:
        lesson = (o.get("lesson") or "").strip()
        if not lesson or lesson in seen:
            continue
        seen.add(lesson)
        ranked.append((1.0 - containment(_toks(lesson), curtok), lesson, o))
    ranked.sort(key=lambda x: -x[0])
    if not ranked or ranked[0][0] < REVISE_MIN_NOVELTY:
        print("    nothing novel in %d occurrence(s) (best novelty %.2f < %.2f); not revising"
              % (len(rec["occurrences"]), ranked[0][0] if ranked else 0.0, REVISE_MIN_NOVELTY))
        return None
    ev = "\n\n".join(
        "EVIDENCE %d (novelty %.2f against the current file)\n  developer said: %s\n"
        "  what was learned: %s"
        % (i + 1, nov, (o.get("prompt") or "")[:300], lesson)
        for i, (nov, lesson, o) in enumerate(ranked[:REVISE_EVIDENCE]))
    sessions = len({o.get("session") for o in rec["occurrences"]})
    try:
        text = call_gemini(REVISE_PROMPT % (sessions, current, ev), max_tokens=4096)
    except Exception:
        oops("revise_memory.api")
        print("    generation failed; the proposal stays open")
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
    if not text.startswith("---") or len(text) < 200:
        print("    model did not return a memory file; the proposal stays open")
        return None
    text = text.replace("—", "-").replace("–", "-")
    # A revision that DROPS a standing mandate is the one failure mode worth refusing over.
    for mark in ("PHIL-LOCKED", "PHIL-ASKED"):
        if current.count(mark) > text.count(mark):
            print("    REFUSING: revision drops a %s marker (%d -> %d). Left untouched."
                  % (mark, current.count(mark), text.count(mark)))
            return None
    bak = "%s.bak-revise-%d" % (path, int(time.time()))
    with open(bak, "w", encoding="utf-8") as fh:
        fh.write(current)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")
    append_locked(MEM_PROPOSALS_PATH, json.dumps({
        "event": "created", "pid": rec["pid"], "ts": _now(), "path": path,
        "reason": "revised from %d occurrence(s)" % len(rec["occurrences"]), "backup": bak,
    }, ensure_ascii=False) + "\n")
    print("    REVISED %s  (%d -> %d bytes, backup %s)"
          % (os.path.basename(path), len(current), len(text), os.path.basename(bak)))
    live("revised memory %s" % os.path.basename(path))
    append_note("memory revised", "%s\n  from %d occurrence(s)\n  backup: %s"
                % (os.path.basename(path), len(rec["occurrences"]), bak))
    return path


def reindex():
    try:
        ms = load_hook()
        print("  index rebuilt: %d rows" % ms.build_index(ms.source_files()))
    except Exception:
        oops("reindex")
        print("  index rebuild failed; it rebuilds on the next prompt")


def cmd_approve(args):
    """Rule on a proposed memory: create it now, reject it, or cool it back to zero.

    `hold` exists because "correct, but not yet proven" is a real verdict. It ends the
    current heat without closing the question: a genuine recurrence re-opens the
    proposal from scratch, which is exactly the evidence bar it had failed to clear.
    """
    openp = open_mem_proposals()
    if args.pid not in openp:
        print("no open proposal with id %s" % args.pid)
        print("open ids: %s" % (", ".join(list(openp)[:10]) or "(none)"))
        return 1
    rec = openp[args.pid]
    append_locked(MEM_PROPOSALS_PATH, json.dumps({
        "event": "rule", "pid": args.pid, "ruling": args.ruling,
        "note": args.note or "", "ts": _now(),
    }, ensure_ascii=False) + "\n")
    print("recorded: %s -> %s" % (args.pid, args.ruling))
    if args.ruling == "revise":
        if rec["kind"] != "refine":
            print("  `revise` is for a proposal that REFINES an existing memory; this one "
                  "is kind=%s. Use `create`." % rec["kind"])
            return 1
        if revise_memory(rec):
            reindex()
        write_rollup()
        return 0
    if args.ruling == "create":
        if rec["kind"] == "refine":
            print("  this REFINES an existing memory. Fold it in with `revise`, or edit:")
            print("    %s" % rec["target"])
            for o in rec["occurrences"]:
                print("    evidence: %s" % (o.get("lesson", "")[:150]))
            return 0
        if write_memory(rec, reason="approved"):
            reindex()
    elif args.ruling == "hold":
        print("  heat cleared. A fresh recurrence re-opens it.")
    write_rollup()
    return 0


def cmd_proposed(args):
    """Proposed memories and their heat."""
    openp = open_mem_proposals()
    if not openp:
        print("no open memory proposals")
        return 0
    esc = [r for r in openp.values() if r.get("escalated")]
    if esc:
        print("!! %d ESCALATED refinement(s): an existing memory needs an EDIT. These no "
              "longer\n   accumulate evidence; they are waiting on you.\n" % len(esc))
        for r in sorted(esc, key=lambda x: -x["heat"]):
            print("  [%s] heat %5.2f  %s" % (r["pid"], r["heat"], r["name"]))
            print("       refines: %s" % r["target"])
            print("       %d occurrence(s) across %d session(s)"
                  % (len(r["occurrences"]),
                     len({o.get("session") for o in r["occurrences"]})))
        print("\n  fold one in with:  approve <id> revise --note '...'\n")
    print("%d open memory proposal(s). Auto-creates at heat %.1f.\n"
          % (len(openp), HEAT_CREATE))
    rows = sorted(openp.values(), key=lambda x: -x["heat"])
    limit = getattr(args, "limit", 0) or 0
    nearest = nearest_memories([r["description"] for r in rows[:limit]]) if limit else {}
    for r in rows[:limit] if limit else rows:
        bar = "#" * int(min(r["heat"], HEAT_CREATE) / HEAT_CREATE * 12)
        print("  [%s] heat %4.2f |%-12s| %s%s"
              % (r["pid"], r["heat"], bar, r["name"],
                 "  (REFINES %s)" % r["target"] if r["kind"] == "refine" else ""))
        print("       %s" % r["description"][:96])
        print("       %d occurrence(s) across %d session(s)"
              % (len(r["occurrences"]), len({o.get("session") for o in r["occurrences"]})))
        for n in nearest.get(r["description"], []):
            print("       nearest existing: %s" % n)
    if limit and len(rows) > limit:
        print("\n  ... and %d more. --limit N shows more, and only the shown ones pay for "
              "their\n  `nearest existing` lookup." % (len(rows) - limit))
    print("\nrule on one with:")
    print("  python3 %s approve <id> create|reject|hold --note '...'"
          % os.path.basename(__file__))
    return 0


def cmd_cycle(args):
    """One full turn of the loop: judge new turns, then reweight from all evidence.

    This is what SessionEnd fires. Judging without tuning would leave the labels as a
    report nobody reads; tuning without judging would reweight on stale evidence. They
    belong in one locked run so 24 parallel sessions cannot interleave halves of it.
    """
    live("cycle: start")
    lock = exclusive("cycle")
    if lock is None:
        live("cycle: lock held elsewhere, exiting")
        # Correct outcome, not a failure. The holder runs a DRAIN LOOP: when it finishes a
        # batch it re-checks for new turns and keeps going. So a turn recorded right now is
        # picked up by that running judge on its next pass -- no spawn needed, no wait.
        # This is why the trigger can fire on ANY backlog: the lock does the rate limiting,
        # and contention turns into batching instead of duplicated work.
        print("a judge is already draining; this turn will be picked up by it")
        return 0
    try:
        class A:
            limit = 0
            apply = True

        # DRAIN LOOP. Judge, then look again -- turns written while we were judging are
        # picked up in the same run rather than waiting for another trigger. Bounded so a
        # busy machine cannot keep one process alive forever; the next Stop re-triggers.
        for sweep in range(MAX_SWEEPS):
            done = {turn_key(l) for l in read_jsonl(LABELS_PATH, dedupe_key=turn_key)}
            backlog = len(judgeable(read_jsonl(TURNS_PATH), done))
            if backlog <= 0:
                break
            # ACCUMULATION WINDOW, first sweep only. MEASURED without it: 81 turns cost 31
            # API calls (2.61/call against a ceiling of 6) because a judge fired the instant
            # one turn landed; 7 runs judged exactly ONE turn. Nothing waits on this process,
            # so trading label latency for a fuller batch is free. Exits early the moment a
            # full batch exists, so a busy machine never actually waits.
            if sweep == 0 and backlog < BATCH:
                waited = 0
                while waited < ACCUMULATE_MAX and backlog < BATCH:
                    time.sleep(ACCUMULATE_POLL)
                    waited += ACCUMULATE_POLL
                    done = {turn_key(l) for l in read_jsonl(LABELS_PATH, dedupe_key=turn_key)}
                    backlog = len(judgeable(read_jsonl(TURNS_PATH), done))
                print("    accumulated %ds -> %d turns ready" % (waited, backlog))
                live("accumulate: waited %ds, %d turns ready" % (waited, backlog))
                if backlog <= 0:
                    break
            live("judge: sweep %d, %d unjudged" % (sweep + 1, backlog))
            print("--- judge sweep %d (%d unjudged) ---" % (sweep + 1, backlog))
            _judge(A())
        # Supersession runs automatically but only ever QUEUES proposals. A slice per
        # cycle, cursor persisted, so the whole corpus gets walked over time without one
        # run costing a fortune in API calls.
        try:
            cur = 0
            try:
                cur = int(open(SUPERSEDE_CURSOR).read().strip() or 0)
            except Exception:
                cur = 0
            total = len(read_jsonl(TURNS_PATH)) and 0 or 0
            live("supersede: slice from %d" % cur)
            print("--- supersede slice (from memory %d) ---" % cur)

            class S:
                scan = 0
                per = 3
                limit = 12
                batch = 6
                offset = cur
            _supersede(S())
            con = sqlite3.connect(DB_PATH)
            n = con.execute("SELECT COUNT(*) FROM mem").fetchone()[0]
            con.close()
            nxt = 0 if cur + SUPERSEDE_SLICE >= n else cur + SUPERSEDE_SLICE
            open(SUPERSEDE_CURSOR, "w").write(str(nxt))
        except Exception as e:
            print("  supersede slice skipped: %s" % str(e)[:80])
        # ENRICH NEW MEMORIES. Without this, anything written after the initial backfill is
        # second-class forever: it sits in the index with an empty `queries` column and is
        # reachable only by the terse description, which MEASURED at 45% recall against 75%
        # once enriched. Incremental by construction - it only does what the sidecar lacks.
        # Capped per cycle so one run cannot turn into a long API session.
        try:
            class E:
                limit = ENRICH_PER_CYCLE
            live("enrich: incremental pass")
            print("--- enrich (new memories) ---")
            _enrich(E())
        except Exception:
            oops("cmd_cycle.enrich")
            print("  enrich skipped")

        # PROPOSE. Runs on the same turns the judge just read, so it costs no new evidence
        # collection. It only ever queues and heats; a file is written solely when a lesson
        # has recurred past HEAT_CREATE, or when Phil approves it with `approve`.
        try:
            class P:
                limit = PROPOSE_PER_CYCLE
            live("propose: start")
            print("--- propose (durable lessons) ---")
            _propose(P())
        except Exception:
            oops("cmd_cycle.propose")
            print("  propose skipped")

        live("tune: start")
        print("--- tune ---")
        labels = read_jsonl(LABELS_PATH, dedupe_key=turn_key)
        if not labels:
            print("no labels; skipping tune")
            return 0
        # cmd_tune takes the lock itself, so call the body directly
        saved, LOCKED[0] = LOCKED[0], True
        try:
            rc = _tune(A())
        finally:
            LOCKED[0] = saved
        # Stamp only now, having actually done the work. This is what the hook's cooldown
        # reads, so a run that did nothing must never advance it.
        try:
            os.makedirs(INDEX_DIR, exist_ok=True)
            open(JUDGE_STAMP, "w").close()
        except Exception:
            oops("cmd_cycle")
            pass
        try:
            cmd_prune(None)
        except Exception as e:
            print("  prune skipped: %s" % str(e)[:70])
        live("cycle: done")
        return rc
    finally:
        lock.close()


def load_hook():
    """Import the live hook so trials run the REAL ranking code, not a copy of it.

    A reimplementation here would drift from the hook and then be tuning a different
    system than the one that runs -- the classic wrong-artifact failure.
    """
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory-search.py")
    spec = importlib.util.spec_from_file_location("memsearch", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def cmd_evaluate(args):
    """Replay labelled turns against candidate constants and score them.

    WHAT THIS CAN AND CANNOT MEASURE. Labels exist only for memories that were actually
    injected, so this measures PRECISION honestly and cannot measure recall at all: a
    config that would surface something never injected before has no label to score it
    against. Those show as `unknown`. The exploration arm is what slowly turns unknowns
    into labels, which is why it exists.
    """
    ms = load_hook()
    labels = {}
    for l in read_jsonl(LABELS_PATH, dedupe_key=turn_key):
        labels[turn_key(l)] = l.get("labels") or {}
    turns = [t for t in read_jsonl(TURNS_PATH) if turn_key(t) in labels and t.get("terms")]
    if len(turns) < 5:
        print("only %d labelled turns with recorded terms; need more evidence" % len(turns))
        return 0
    print("replaying %d labelled turns\n" % len(turns))

    grid = []
    for top_k in (2, 3, 4):
        for cov in (2, 3):
            for cut in (0.35, 0.45, 0.60):
                grid.append({"TOP_K": top_k, "MIN_COVERAGE": cov, "REL_CUT": cut})

    cur = {"TOP_K": ms.TOP_K, "MIN_COVERAGE": ms.MIN_COVERAGE, "REL_CUT": ms.REL_CUT}
    rows = []
    for cfg in grid:
        for k, v in cfg.items():
            setattr(ms, k, v)
        used = ignored = unknown = shown = 0
        for t in turns:
            lab = labels[turn_key(t)]
            q = " OR ".join('"%s"' % x for x in t["terms"])
            try:
                hits = ms.search(q, t["terms"])
            except Exception:
                oops("cmd_evaluate")
                continue
            for h in hits:
                shown += 1
                v = lab.get(h[0])
                if v == "used":
                    used += 1
                elif v == "ignored":
                    ignored += 1
                else:
                    unknown += 1
        # Precision over the injections we can actually judge. Ties broken toward fewer
        # injections, because every one spends context.
        judged = used + ignored
        prec = (used / judged) if judged else 0.0
        rows.append((prec, -shown, cfg, used, ignored, unknown, shown))
    # sort on the numeric fields only; a dict tiebreak raises TypeError
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)

    print("  %-34s %7s %6s %8s %8s" % ("config", "prec", "used", "ignored", "shown"))
    for prec, _neg, cfg, u, ig, unk, sh in rows[:8]:
        tag = "  <- CURRENT" if cfg == cur else ""
        print("  TOP_K=%d COV=%d CUT=%.2f%-14s %6.0f%% %6d %8d %8d%s"
              % (cfg["TOP_K"], cfg["MIN_COVERAGE"], cfg["REL_CUT"], "", prec * 100, u, ig, sh, tag))
    best = rows[0]
    print("\n  current : %s" % cur)
    print("  best    : %s  (precision %.0f%%, %d shown)" % (best[2], best[0] * 100, best[6]))
    if best[2] == cur:
        print("\n  Current constants already win on this evidence. No change proposed.")
    else:
        print("\n  PROPOSAL ONLY - constants are not auto-applied. Apply by editing")
        print("  memory-search.py, or re-run when more labels exist to confirm it holds.")
    return 0


RETAIN_TURNS = 2000      # rows; raw evidence, the most valuable thing here
RETAIN_LABELS = 2000
RETAIN_RECALL_MB = 5
RETAIN_NOTES_KB = 400
RETAIN_STATE_DAYS = 3
# errors.log had NO retention at all until 2026-10-05, along with mem-proposals.jsonl (2.9MB,
# now compacted semantically) and supersede-pending.jsonl. Everything else here was already
# sitting exactly at its cap, so the "unrotated logs" were these three and nothing more.
RETAIN_ERRORS_KB = 200


GAPS_PROMPT = """You are reading an AI coding assistant's retrieval failures.

For each turn below, a judge recorded what retrieval SHOULD have surfaced but did not. These \
notes are the only signal available about memories that were never retrieved at all - every \
other measurement in this system can only judge what WAS shown.

Cluster these into recurring GAPS. For each cluster say:
  "gap"     what kind of knowledge retrieval keeps failing to surface
  "count"   roughly how many notes fall in it
  "cause"   the most likely mechanical reason, chosen from:
              "not-in-corpus"   no memory covers this; it would have to be written
              "lexical-miss"    a memory exists but shares no words with how it was asked
              "not-retrievable" the need is conversational/session state, which a memory
                                corpus structurally cannot provide
  "action"  one concrete thing that would fix it

Be conservative. Three real clusters beat ten speculative ones. If most notes are actually \
the same complaint, say so and give one cluster.

NOTES:
%s

Return ONLY JSON: [{"gap": "...", "count": N, "cause": "...", "action": "..."}]
"""


Q2Q_PROMPT = """Write the questions each memory ANSWERS.

A developer will ask these in their own words, not the memory's. Keyword search then matches \
their question against yours, which is the whole point - so REUSE AS LITTLE of the memory's \
own wording as you can and reach for the words someone would actually type.

For each memory give %d short questions. Vary them: one plain ("why is my build slow"), one \
symptom-first ("tests pass locally but fail in CI"), one with the concrete nouns a person \
would use (tool names, file names, error text).

Do NOT restate the memory. Do NOT invent facts it does not contain.

MEMORIES:
%s

Return ONLY JSON mapping each memory's name to its list of questions:
{"<name>": ["q1", "q2", ...]}
"""


def cmd_enrich(args):
    lock = exclusive("enrich")
    if lock is None:
        print("another curator run holds the lock; exiting")
        return 0
    try:
        return _enrich(args)
    finally:
        lock.close()


def _enrich(args):
    """doc2query: index the QUESTIONS each memory answers, not just its terse description.

    WHY. MEASURED recall@3 on paraphrased known-item questions was 33% for BM25 alone; the
    right memory sat in the top-50 83% of the time. Reranking fixes the ORDERING half of
    that. This fixes the REACHABILITY half: a natural question and a terse lesson share
    almost no vocabulary, so we give the memory a set of natural questions to be matched
    against. All cost is offline; the hot path is unchanged.

    Written to a SIDECAR, never into the .md files: enrichment must never mutate the corpus.
    """
    if True:
        ph = Phases()
        ph.start("load")
        try:
            with open(Q2Q_PATH, encoding="utf-8") as fh:
                have = json.load(fh)
        except FileNotFoundError:
            have = {}
        except Exception:
            oops("cmd_enrich.load")
            have = {}
        con = sqlite3.connect(DB_PATH)
        rows = con.execute("SELECT path, name, description FROM mem "
                           "WHERE length(description) > 60 ORDER BY path").fetchall()
        con.close()
        todo = [r for r in rows if r[0] not in have]
        ph.stop()
        if args.limit:
            todo = todo[:args.limit]
        if not todo:
            print("all %d eligible memories already enriched" % len(rows))
            return 0
        print("enriching %d of %d memories, %d per call"
              % (len(todo), len(rows), Q2Q_BATCH))
        live("enrich: %d memories to do" % len(todo))

        added = failed = 0
        for b in range(0, len(todo), Q2Q_BATCH):
            chunk = todo[b:b + Q2Q_BATCH]
            ph.start("build")
            blob = "\n\n".join("name: %s\ndescription: %s" % (n, (d or "")[:400])
                                for _p, n, d in chunk)
            ph.stop()
            ph.start("api")
            res = None
            try:
                res = extract_json(call_gemini(Q2Q_PROMPT % (Q2Q_PER_MEM, blob),
                                               max_tokens=Q2Q_MAX_TOKENS))
            except Exception:
                oops("cmd_enrich.api")
                # A truncated or malformed reply is a SIZE problem, and retrying the same
                # call reproduces it. Halve the batch instead: smaller prompts emit smaller
                # JSON, which is the actual fix. MEASURED: one unretried failure silently
                # cost 8 memories their enrichment.
                res = {}
                half = max(1, len(chunk) // 2)
                for sub in (chunk[:half], chunk[half:]):
                    if not sub:
                        continue
                    sblob = "\n\n".join("name: %s\ndescription: %s" % (n, (d or "")[:400])
                                         for _p, n, d in sub)
                    try:
                        res.update(extract_json(call_gemini(
                            Q2Q_PROMPT % (Q2Q_PER_MEM, sblob), max_tokens=Q2Q_MAX_TOKENS)))
                        print("  batch %d recovered by splitting" % (b // Q2Q_BATCH))
                    except Exception:
                        oops("cmd_enrich.split")
                        failed += len(sub)
            ph.stop()
            ph.start("collect")
            for path, name, _d in chunk:
                qs = res.get(name)
                if isinstance(qs, list) and qs:
                    have[path] = [str(q)[:160] for q in qs if str(q).strip()][:Q2Q_PER_MEM]
                    added += 1
            ph.stop()

        ph.start("write")
        tmp = "%s.tmp.%d" % (Q2Q_PATH, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(have, fh, ensure_ascii=False, indent=0)
        os.replace(tmp, Q2Q_PATH)          # atomic; the hook may read this concurrently
        ph.stop()
        print("enriched %d, failed %d, sidecar now covers %d memories"
              % (added, failed, len(have)))
        print("phases: %s" % ph.render())
        # REBUILD the index, do not merely drop it. MEASURED: dropping left the next
        # curator command reading a nonexistent table ("no such table: mem"), because the
        # index only rebuilds from inside the hook. Leave the system in a valid state.
        ph.start("reindex")
        try:
            ms = load_hook()
            rows_n = ms.build_index(ms.source_files())
            print("index rebuilt: %d rows now carry the queries column" % rows_n)
        except Exception:
            oops("cmd_enrich.reindex")
            print("index rebuild failed; it will rebuild on the next prompt")
        ph.stop()
        append_note("enrich", "Generated doc2query questions for %d memories (%d total "
                              "covered). Rebuilds the index so the `queries` column is live."
                    % (added, len(have)))
        return 0


def cmd_gaps(args):
    """Turn the judge's 'what was missed' notes into recurring gap clusters.

    WHY THIS IS THE ONLY RECALL SIGNAL. Labels judge memories that were injected, so every
    other measurement here is blind to a memory that was never surfaced. These notes are
    written while looking at the response, so they can name what was ABSENT. Nothing was
    consuming them; they were being collected and discarded.
    """
    labels = read_jsonl(LABELS_PATH, dedupe_key=turn_key)
    notes = [l["note"] for l in labels if (l.get("note") or "").strip()]
    if len(notes) < 5:
        print("only %d notes; need more evidence" % len(notes))
        return 0
    # dedupe near-identical notes so one repeated complaint cannot dominate the clustering
    seen, uniq = set(), []
    for n in notes:
        k = n.lower()[:70]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(n)
    print("clustering %d notes (%d after dedupe)\n" % (len(notes), len(uniq)))
    live("gaps: clustering %d notes" % len(uniq))
    try:
        res = extract_json(call_gemini(
            GAPS_PROMPT % "\n".join("- " + n[:200] for n in uniq[-120:]), max_tokens=1200))
    except Exception as e:
        print("failed: %s" % str(e)[:120])
        return 1
    order = {"not-in-corpus": 0, "lexical-miss": 1, "not-retrievable": 2}
    res.sort(key=lambda g: (order.get(g.get("cause"), 9), -g.get("count", 0)))
    body = []
    for g in res:
        print("  [%s] %s  (~%s notes)" % (g.get("cause", "?"), g.get("gap", "")[:66],
                                          g.get("count", "?")))
        print("      -> %s" % g.get("action", "")[:96])
        body.append("- **%s** (%s, ~%s): %s" % (g.get("gap", ""), g.get("cause", ""),
                                                g.get("count", ""), g.get("action", "")))
    append_note("gaps", "Clustered %d retrieval-failure notes.\n\n%s\n\n"
                        "not-in-corpus = a memory should be WRITTEN. lexical-miss = a memory "
                        "exists but cannot be found by the words used. not-retrievable = "
                        "outside what a memory corpus can answer."
                % (len(uniq), "\n".join(body)))
    print("\n  written to curator-notes.md")
    return 0


def prune_doc2query():
    """Drop doc2query entries whose memory file is gone. Returns how many.

    A sidecar entry for a deleted or renamed memory is not merely dead weight, it
    MANUFACTURES A FINDING. The retrievability audit scores each memory's generated
    questions and reports the ones nothing can retrieve; an entry with no file behind it
    fails every question by construction, so it lands at the top of that report looking
    exactly like a real memory that nobody can reach. MEASURED 2026-10-05: 6 such entries
    were the ENTIRE "100% of its own questions fail" tier, and they are the reason the
    report has to check the file exists rather than trusting the key.

    Keyed by absolute path, so a rename leaves the old key behind with no error anywhere.
    """
    if not os.path.exists(Q2Q_PATH):
        return 0
    data = json.loads(open(Q2Q_PATH, encoding="utf-8").read())
    live_keys = {k: v for k, v in data.items() if os.path.exists(k)}
    dropped = len(data) - len(live_keys)
    if dropped:
        tmp = "%s.tmp.%d" % (Q2Q_PATH, os.getpid())
        open(tmp, "w", encoding="utf-8").write(json.dumps(live_keys, ensure_ascii=False))
        os.replace(tmp, Q2Q_PATH)
    return dropped


def cmd_prune(args):
    """Retention. Everything here grows without bound and nothing was trimming it.

    Ordered by what is safe to lose: /tmp breadcrumbs first, then per-session scratch
    state, then log tails. turns.jsonl and labels.jsonl are trimmed LAST and most
    generously -- they are the evidence base everything else is derived from, and unlike
    a weight or a note they cannot be recomputed.
    """
    import glob
    freed = []

    def trim_jsonl(path, keep, label):
        rows = read_jsonl(path)
        if len(rows) <= keep:
            return
        before = os.path.getsize(path)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows[-keep:]:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        freed.append("%s: %d -> %d rows (%.0fKB freed)"
                     % (label, len(rows), keep, (before - os.path.getsize(path)) / 1024))

    def tail_file(path, max_bytes, label):
        try:
            sz = os.path.getsize(path)
        except OSError:
            oops("tail_file")
            return
        if sz <= max_bytes:
            return
        with open(path, encoding="utf-8", errors="ignore") as fh:
            fh.seek(sz - max_bytes)
            fh.readline()                  # drop the partial first line
            rest = fh.read()
        tmp = "%s.tmp.%d" % (path, os.getpid())
        open(tmp, "w", encoding="utf-8").write(rest)
        os.replace(tmp, path)
        freed.append("%s: %.0fKB -> %.0fKB" % (label, sz / 1024, len(rest) / 1024))

    cutoff = time.time() - RETAIN_STATE_DAYS * 86400
    for d, name in ((os.path.join(INDEX_DIR, "seen"), "seen"),
                    (os.path.join(INDEX_DIR, "pending"), "pending")):
        n = 0
        for f in glob.glob(os.path.join(d, "*")):
            try:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f); n += 1
            except OSError:
                oops("tail_file")
                pass
        if n:
            freed.append("%s/: removed %d stale files" % (name, n))

    # orphaned index temp files from a killed rebuild
    n = 0
    for f in glob.glob(DB_PATH + ".tmp.*"):
        try:
            if os.path.getmtime(f) < time.time() - 3600:
                os.remove(f); n += 1
        except OSError:
            oops("tail_file")
            pass
    if n:
        freed.append("removed %d orphaned index temp file(s)" % n)

    try:
        if os.path.getsize(LIVE_PATH) > 1_000_000:
            tail_file(LIVE_PATH, 200_000, "live log")
    except OSError:
        oops("tail_file")
        pass
    tail_file(os.path.join(INDEX_DIR, "recall.log"), RETAIN_RECALL_MB * 1_000_000, "recall.log")
    tail_file(NOTES_PATH, RETAIN_NOTES_KB * 1000, "curator notes")
    # errors.log is a TAIL, not a trim: it is read by the SessionStart banner, which only ever
    # reports the last 24h, so older lines are already invisible to every consumer.
    tail_file(ERRORS_PATH, RETAIN_ERRORS_KB * 1000, "errors.log")
    trim_jsonl(LABELS_PATH, RETAIN_LABELS, "labels")
    trim_jsonl(TURNS_PATH, RETAIN_TURNS, "turns")
    # SEMANTIC, never a tail. See compact_mem_proposals for why a byte cut corrupts this one.
    try:
        compact_mem_proposals()
    except Exception:
        oops("cmd_prune.compact")
    try:
        n = organ_health.tail()
        if n:
            freed.append("organ-health: dropped %d old rows" % n)
    except Exception:
        oops("cmd_prune.health")
    try:
        n = prune_doc2query()
        if n:
            freed.append("doc2query: dropped %d entries for deleted memories" % n)
    except Exception:
        oops("cmd_prune.doc2query")

    if freed:
        for f in freed:
            print("  " + f)
        live("prune: " + "; ".join(freed)[:160])
    else:
        print("  nothing to prune")
    return 0


def cmd_live(args):
    """What in-flight runs are doing RIGHT NOW."""
    running = []
    try:
        import subprocess as sp
        out = sp.run(["pgrep", "-f", "memory-curator.py"], capture_output=True, text=True).stdout
        running = [p for p in out.split() if p and int(p) != os.getpid()]
    except Exception:
        oops("cmd_live")
        pass
    print("curator processes running: %s" % (", ".join(running) or "none"))
    try:
        held = exclusive("probe")
        print("lock: %s" % ("FREE" if held else "HELD by a running cycle"))
        if held:
            held.close()
    except Exception:
        oops("cmd_live")
        pass
    try:
        lines = open(LIVE_PATH, encoding="utf-8").read().splitlines()
        print("\nlast %d live events (%s):" % (min(args.n, len(lines)), LIVE_PATH))
        for l in lines[-args.n:]:
            print("  " + l)
    except OSError:
        print("\nno live log yet at %s" % LIVE_PATH)
    return 0


def cmd_status(args):
    """One glance at the whole system: corpus, index, traffic, labels, curator memory."""
    def count(path):
        return len(read_jsonl(path))
    print("CORPUS")
    try:
        con = sqlite3.connect(DB_PATH)
        n = con.execute("SELECT COUNT(*) FROM mem").fetchone()[0]
        dated = con.execute("SELECT COUNT(*) FROM mem WHERE modified<>''").fetchone()[0]
        con.close()
        idx = os.path.getsize(DB_PATH)
        print("  memories indexed   %d   authored date %d, mtime fallback %d"
              % (n, dated, n - dated))
        print("  index size         %.1f MB" % (idx / 1e6))
    except Exception as e:
        print("  index unreadable: %s" % str(e)[:60])
    loaded = os.path.join(MEMORY_DIR, "MEMORY.md")
    try:
        t = open(loaded, encoding="utf-8").read()
        print("  loaded tier        %d bytes / %d lines  (limits 25000 / 200)"
              % (len(t), t.count("\n")))
    except Exception:
        oops("count")
        pass

    # ORGAN HEALTH, because the whole corpus is downstream of these calls working.
    try:
        h = organ_health.rollup()
        if h["organs"]:
            print("  LLM organs        %s   (%s)"
                  % (h["worst"].upper(),
                     ", ".join("%s %d/%d" % (n, v["ok24"], v["att24"])
                               for n, v in sorted(h["organs"].items()))))
        else:
            print("  LLM organs        never observed (no calls recorded yet)")
    except Exception:
        oops("status.health")
    # DECIDED BUT UNAPPLIED. A ruling that demands an edit and never got one is invisible
    # work; seven of them sat for 24 days before this line existed.
    try:
        un = unapplied_rulings()
        if un:
            print("  rulings DECIDED BUT UNAPPLIED  %d  (fold + retire, then `applied <id>`)"
                  % len(un))
            for r in un[:3]:
                print("      [%s] %s  %s / %s"
                      % (r["pair_id"], r["ruling"], r.get("a", "")[:30], r.get("b", "")[:30]))
    except Exception:
        oops("status.unapplied")

    print("\nTRAFFIC")
    turns = read_jsonl(TURNS_PATH)
    labels = read_jsonl(LABELS_PATH, dedupe_key=turn_key)
    inj = sum(len(t.get("injected", [])) for t in turns)
    expl = sum(1 for t in turns if t.get("explored"))
    print("  turns recorded     %d   injections %d   exploration turns %d" % (len(turns), inj, expl))
    print("  turns labelled     %d   unjudged %d" % (len(labels), len(turns) - len(labels)))
    if turns:
        lat = sorted(t.get("latency_ms", 0) for t in turns)
        print("  hook latency       p50 %dms   max %dms" % (lat[len(lat) // 2], lat[-1]))

    print("\nJUDGMENT")
    if not labels:
        print("  no labels yet. run `judge` once turns accumulate.")
    else:
        # PER-TURN FIRST, because the per-injection number is mostly arithmetic and reads
        # as a catastrophe. MEASURED 2026-10-05 and the identity is exact:
        #     per-injection = per-turn x (used-per-hitting-turn / injected-per-turn)
        #             15.3% = 32.4%    x (1.43 / 3.04)
        # With TOP_K=3 and usually ONE memory being the relevant one, the per-injection
        # ceiling is about 33% BY CONSTRUCTION. Reporting "85% ignored" as the headline
        # invited the conclusion that retrieval was broken; the real figure is that a third
        # of turns get a useful memory, and that a third of them are harness probes.
        c = collections.Counter()
        turns = hits = harness = 0
        for l in labels:
            labs = l.get("labels") or {}
            if not labs:
                continue
            turns += 1
            if is_harness_turn(l):
                harness += 1
            c.update(labs.values())
            if any(v == "used" for v in labs.values()):
                hits += 1
        tot = sum(c.values()) or 1
        print("  turns with a USED memory   %4d of %4d  (%.0f%%)   <- the real figure"
              % (hits, turns, 100.0 * hits / max(turns, 1)))
        print("  memories injected per turn %.2f   used per hitting turn %.2f"
              % (tot / max(turns, 1), c["used"] / max(hits, 1)))
        if harness:
            print("  harness-probe turns        %4d  (excluded from `tune`; they hit at "
                  "~0.6%% and were demoting good memories)" % harness)
        print("  per-INJECTION, for reference only (ceiling is ~1/TOP_K by construction):")
        for v in ("used", "ignored", "harmful"):
            print("      %-14s %4d  (%.0f%%)" % (v, c[v], c[v] / tot * 100))

    print("\nCURATOR MEMORY")
    if os.path.exists(NOTES_PATH):
        notes = open(NOTES_PATH, encoding="utf-8").read()
        heads = re.findall(r"^## (.+)$", notes, re.M)
        print("  %d notes, %d bytes. most recent:" % (len(heads), len(notes)))
        for h in heads[-3:]:
            print("    %s" % h[:78])
    else:
        print("  none yet")
    return 0


def cmd_report(args):
    labels = read_jsonl(LABELS_PATH, dedupe_key=turn_key)
    if not labels:
        print("no labels yet - run `judge` first")
        return 0
    counts = collections.Counter()
    per_mem = collections.defaultdict(collections.Counter)
    explored = collections.Counter()
    for l in labels:
        for name, verdict in (l.get("labels") or {}).items():
            counts[verdict] += 1
            per_mem[name][verdict] += 1
            if l.get("explored") == name:
                explored[verdict] += 1
    total = sum(counts.values()) or 1
    print("turns labelled : %d" % len(labels))
    print("injections     : %d" % total)
    for v in ("used", "ignored", "harmful"):
        print("  %-8s %4d  (%.0f%%)" % (v, counts[v], counts[v] / total * 100))

    if explored:
        et = sum(explored.values())
        print("\nEXPLORATION ARM (memories BM25 did NOT rank top-3):")
        for v in ("used", "ignored", "harmful"):
            print("  %-8s %4d  (%.0f%%)" % (v, explored[v], explored[v] / et * 100))
        base = counts["used"] / total
        exp = explored["used"] / et
        print("  -> explored used-rate %.0f%% vs overall %.0f%%%s" % (
            exp * 100, base * 100,
            "  BM25's ranking is LEAVING VALUE ON THE TABLE" if exp > base else ""))

    worst = [(n, c) for n, c in per_mem.items() if c["used"] == 0 and sum(c.values()) >= 2]
    if worst:
        print("\nNEVER USED despite repeated injection (candidates to demote or fix):")
        for n, c in sorted(worst, key=lambda x: -sum(x[1].values()))[:10]:
            print("  %3dx  %s" % (sum(c.values()), n[:66]))

    notes = [l["note"] for l in labels if l.get("note")]
    if notes:
        print("\nJUDGE NOTES (what retrieval should have surfaced):")
        for n in notes[-8:]:
            print("  - %s" % n[:110])
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("judge", help="label unjudged turns")
    j.add_argument("--limit", type=int, default=0)
    j.set_defaults(func=cmd_judge)
    tu = sub.add_parser("tune", help="recompute retrieval weights from judged evidence")
    tu.add_argument("--apply", action="store_true")
    tu.set_defaults(func=cmd_tune)
    pd = sub.add_parser("pending", help="supersession proposals awaiting your ruling")
    pd.set_defaults(func=cmd_pending)
    ap_ = sub.add_parser("applied", help="mark a ruling as carried out (closes the gap "
                                         "where a decision stays advisory forever)")
    ap_.add_argument("pair_id")
    ap_.add_argument("--note", default="")
    ap_.set_defaults(func=cmd_applied)
    dc = sub.add_parser("decide", help="rule on a proposed pair")
    dc.add_argument("pair_id")
    dc.add_argument("ruling", choices=["keep-a", "keep-b", "merge", "both-stand"])
    dc.add_argument("--note", default="")
    dc.set_defaults(func=cmd_decide)
    pp = sub.add_parser("propose", help="scan turns for durable lessons worth a memory")
    pp.add_argument("--limit", type=int, default=0)
    pp.add_argument("--dry", action="store_true", help="queue and heat, but write nothing")
    pp.set_defaults(func=cmd_propose)
    pl = sub.add_parser("proposed", help="proposed memories and their heat")
    pl.add_argument("--limit", type=int, default=0,
                    help="show only the N hottest, each with the nearest existing memories "
                         "(an LLM rerank per shown row, so the default shows none)")
    pl.set_defaults(func=cmd_proposed)
    hh = sub.add_parser("health", help="LLM organ health: per-organ success RATE and verdict")
    hh.set_defaults(func=lambda a: cmd_health(a))
    hs = sub.add_parser("hold-stale",
                        help="hold single-session proposals not seen for STALE_HOLD_DAYS")
    hs.add_argument("--dry", action="store_true", help="list what would be held")
    hs.set_defaults(func=lambda a: (hold_stale_proposals(dry=a.dry), 0)[1])
    av = sub.add_parser("approve", help="rule on a proposed memory")
    av.add_argument("pid")
    av.add_argument("ruling", choices=["create", "revise", "reject", "hold"])
    av.add_argument("--note", default="")
    av.set_defaults(func=cmd_approve)
    cy = sub.add_parser("cycle", help="judge + tune in one locked run (what SessionEnd fires)")
    cy.set_defaults(func=cmd_cycle)
    ev = sub.add_parser("evaluate", help="replay labelled turns against candidate constants")
    ev.set_defaults(func=cmd_evaluate)
    en = sub.add_parser("enrich", help="doc2query: generate questions each memory answers")
    en.add_argument("--limit", type=int, default=0)
    en.set_defaults(func=cmd_enrich)
    gp = sub.add_parser("gaps", help="cluster what retrieval keeps failing to surface")
    gp.set_defaults(func=cmd_gaps)
    pr = sub.add_parser("prune", help="retention for logs and per-session state")
    pr.set_defaults(func=cmd_prune)
    lv = sub.add_parser("live", help="what in-flight runs are doing right now")
    lv.add_argument("-n", type=int, default=25)
    lv.set_defaults(func=cmd_live)
    st = sub.add_parser("status", help="one glance at the whole system")
    st.set_defaults(func=cmd_status)
    r = sub.add_parser("report", help="what the labels say")
    r.set_defaults(func=cmd_report)
    sp = sub.add_parser("supersede", help="find pairs that contradict or duplicate")
    sp.add_argument("--scan", type=int, default=0, help="only scan first N memories")
    sp.add_argument("--per", type=int, default=3, help="candidates per memory")
    sp.add_argument("--limit", type=int, default=0, help="cap pairs examined")
    sp.add_argument("--batch", type=int, default=8)
    sp.set_defaults(func=cmd_supersede)
    c = sub.add_parser("curate-hooks", help="rewrite uninformative index hooks")
    c.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    c.add_argument("--only", default="", help="comma-separated file stems you verified; REQUIRED with --apply")
    c.set_defaults(func=cmd_curate_hooks)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
