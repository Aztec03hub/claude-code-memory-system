#!/usr/bin/env python3
"""Verify the two memory indexes against the memory files on disk.

Written 2026-08-27 after an audit found MEMORY-FULL.md - the index that claims to
list every memory - was missing six of them, and both headers carried counts that
had been true when written and were not true any more.

A memory that no index line reaches is not merely mis-described: it is invisible
in every future session, because the index is what gets loaded. That is the class
this catches. Run it after adding or deleting memories.

    python3 check-memory-index.py            # verify, exit 1 on any defect
    python3 check-memory-index.py --fix-counts   # rewrite the header counts
    python3 check-memory-index.py --sync         # add any memory missing from the FULL index

Deliberately NOT checked: whether a hook accurately describes its file. That is a
judgment call no script settles, and a script that pretended to would be the more
dangerous artifact - a green tick over an unread claim.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import sys

# The memory directory. CLAUDE_MEMORY_DIR wins so this file can live in the published repo
# with the data elsewhere; the fallback is the directory it sits in, which is how it ran for
# six weeks and is what test_index_guard.py relies on (it copies the script into a temp dir
# and expects that dir to BE the memory dir, which is the only way to test the hook without
# touching the real corpus).
MEM = pathlib.Path(os.environ.get("CLAUDE_MEMORY_DIR")
                   or pathlib.Path(__file__).resolve().parent).expanduser().resolve()
LOADED, FULL = MEM / "MEMORY.md", MEM / "MEMORY-FULL.md"
LINK = re.compile(r"^- \[([^\]]*)\]\(([^)]+)\)(.*)$", re.M)

# The load limits, and the budgets that leave room for the next few memories. These are
# module-level because THREE callers need the same numbers: main(), the PostToolUse hook
# below, and memory-curator.py. The long derivation of these two axes - four wrong
# calibrations, and the measured truncation that settled it - is in main(), where the
# gate lives. Do not re-derive them anywhere else.
LINE_LIMIT, BYTE_LIMIT = 200, 25000
LINE_BUDGET, BYTE_BUDGET = 190, 24000


def entries(text):
    """Yield (path, hook) for both index shapes.

    2026-09-30: the index moved to `- [hook](path)`. The old shape was
    `- [display](path) - hook`, where the display name duplicated the path and cost 36
    bytes an entry - 26% of a byte-capped file - leaving a MEDIAN HOOK OF 20 CHARACTERS
    and 132 of 163 loaded hooks too short to judge relevance by. Both shapes are read
    here because MEMORY-FULL.md is large and may be edited by hand for a while yet.
    """
    for m in LINK.finditer(text):
        bracket, path, rest = m.group(1), m.group(2), m.group(3).strip()
        hook = rest[2:].strip() if rest.startswith("- ") else ""
        yield path, (hook or bracket)


PREFIXES = ("feedback_", "project_", "reference_", "user_")


def _norm(s: str) -> str:
    """Slug identity: case, separator style and type prefix are all noise.

    Comparing literally instead of normalising is what made the first version of
    this audit report 122 name mismatches and 109 broken links that were almost
    all hyphen-vs-underscore - auditing my own regex rather than the memories.
    """
    s = s.strip().lower().replace("-", "_")
    for p in PREFIXES:
        if s.startswith(p):
            return s[len(p):]
    return s


def indexed(p: pathlib.Path) -> dict[str, str]:
    return dict(entries(p.read_text()))


def sync(on_disk: set[str], full: dict[str, str], loaded: dict[str, str]) -> int:
    """Add any memory missing from MEMORY-FULL.md, using the loaded tier's hook or the file's own.

    This exists because the failure is structural, not careless. MEMORY.md is the file a session
    has LOADED, so it is the one a session edits; MEMORY-FULL.md is not in context, so it is not in
    mind. Four separate sessions drifted the same way in a single day. Advice loses to a missing
    context window, so the two-file update becomes one action instead of a thing to remember.

    A hook is never invented here. It is taken from the loaded tier if the memory is indexed there,
    otherwise from the file's own `description:` - both are the author's words, not this script's.
    """
    missing = sorted(on_disk - set(full))
    if not missing:
        return 0
    text = FULL.read_text()
    if not text.endswith("\n"):
        text += "\n"
    added = []
    for fname in missing:
        if fname in loaded:
            title = re.search(r"^- \[([^\]]*)\]\(%s\)" % re.escape(fname),
                              LOADED.read_text(), re.M)
            line = f"- [{title.group(1)}]({fname}) - {loaded[fname]}" if title else None
        else:
            line = None
        if line is None:
            body = (MEM / fname).read_text()
            d = re.search(r'^description:\s*"?(.+?)"?\s*$', body, re.M)
            hook = (d.group(1) if d else "no description")[:160]
            title = fname[:-3].split("_", 1)[-1].replace("_", " ")
            line = f"- [{hook}]({fname})"
        text += line + "\n"
        added.append(fname)
    FULL.write_text(text)
    for f in added:
        print(f"  synced into MEMORY-FULL.md: {f}")
    return len(added)


def main(fix: bool) -> int:
    on_disk = {p.name for p in MEM.glob("*.md")} - {LOADED.name, FULL.name}
    full, loaded = indexed(FULL), indexed(LOADED)

    if "--sync" in sys.argv:
        n = sync(on_disk, full, loaded)
        if n:
            full, loaded = indexed(FULL), indexed(LOADED)
        else:
            print("  nothing to sync")
    problems: list[str] = []

    for f in sorted(on_disk - set(full)):
        problems.append(f"UNREACHABLE: {f} is on disk but in no FULL index line")
    for f in sorted(set(full) - on_disk):
        # A relative path that escapes this directory is a deliberate reference to a
        # file kept elsewhere (the psrig rig notes, for instance). `on_disk` is built by
        # globbing THIS directory only, so those read as missing when they are present.
        # MEASURED 2026-09-30: 3 of 5 remaining problems were this false positive, and a
        # report that is mostly false positives is a report nobody reads.
        if "/" in f and (MEM / f).exists():
            continue
        problems.append(f"DANGLING: MEMORY-FULL.md points at missing file {f}")
    for f in sorted(set(loaded) - on_disk):
        problems.append(f"DANGLING: MEMORY.md points at missing file {f}")
    for f in sorted(set(loaded) - set(full)):
        problems.append(f"LOADED-ONLY: {f} is in MEMORY.md but not in the FULL index")
    # THE LOADED HOOK MAY ABBREVIATE; IT MAY NOT CONTRADICT.
    # This used to demand the two hooks be IDENTICAL, which is incompatible with
    # the tier design and fired on 95 of 168 shared entries - so the checker
    # exited 1 unconditionally and could never fail usefully on a real problem.
    # MEMORY.md is byte-capped (see the load limits below) and MEMORY-FULL.md is
    # not, so the loaded hook is DELIBERATELY the shorter one. The invariant that
    # actually matters is that it is a PREFIX: the loaded tier can say less than
    # the full tier, never something different.
    # Mid-word cuts USED to be defensible: when every byte was markup, a ragged
    # " - MEASURED: skipped/failed" carried more than a clean " - MEASURED". That
    # premise died on 2026-09-30 when the display name was dropped from the entry
    # shape, freeing 5,971 bytes and taking the median loaded hook from 20 chars to
    # 63. There is now room to end on a word, so a mid-word cut is just damage.
    # A hook too short to judge relevance by is a REAL defect, but its only fix is
    # demotion, not rewording - reported separately below, not as a hard failure.
    STUB = 24   # a hook this short cannot carry a relevance decision
    stubs = []
    for f in sorted(set(loaded) & set(full)):
        lo, fu = loaded[f].strip(), full[f].strip()
        if lo and fu and not fu.startswith(lo):
            problems.append(
                f"HOOK CONTRADICTS the full index (not a prefix of it): {f}\n"
                f"      loaded: {lo[:80]}\n"
                f"      full  : {fu[:80]}")
        elif len(lo) < STUB and len(fu) > len(lo):
            stubs.append((f, lo, fu))

    # Counts stated in prose, which decay silently on every write.
    stated = {}
    for p in (LOADED, FULL):
        for m in re.finditer(r"\b(\d{2,4})\b", p.read_text()):
            stated.setdefault(p.name, []).append(int(m.group(1)))
    actual_total, actual_loaded = len(on_disk), len(loaded)

    if fix:
        s = LOADED.read_text()
        s = re.sub(r"Loaded tier: \d+ of \d+",
                   f"Loaded tier: {actual_loaded} of {actual_total}", s)
        s = re.sub(r"PARTIAL INDEX - \d+ of \d+",
                   f"PARTIAL INDEX - {actual_loaded} of {actual_total}", s)
        s = re.sub(r"THE OTHER \d+ ARE IN", f"THE OTHER {actual_total - actual_loaded} ARE IN", s)
        s = re.sub(r"the other \d+ are in", f"the other {actual_total - actual_loaded} are in", s)
        LOADED.write_text(s)
        f = FULL.read_text()
        f = re.sub(r"FULL \(all \d+ memories\)", f"FULL (all {actual_total} memories)", f)
        f = re.sub(r"Complete index\.", "Complete index.", f)
        FULL.write_text(f)
        print(f"counts rewritten: {actual_loaded} loaded of {actual_total} total")

    print(f"memories on disk: {actual_total}")
    # NOT the same number as the file's line count printed further down: this
    # counts INDEX ENTRIES, that one counts FILE LINES, and the limit is on file
    # lines. Labelling both "lines" once had me report 168 when the file was 187.
    print(f"MEMORY.md entries:{actual_loaded}")
    print(f"MEMORY-FULL.md:   {len(full)}")
    # Wikilinks. Two very different cases, and conflating them is why 148 broken
    # citations sat unnoticed:
    #   - a link to a name some memory declares in its `aliases:` frontmatter is a
    #     RENAME that was never propagated. That is a defect: the successor exists,
    #     and the link silently goes nowhere.
    #   - a link to a name nothing claims is a FORWARD MARKER. The memory spec says
    #     writing one is fine - it records a thought worth writing up later.
    #     "Fixing" those deletes the only trace of the thought.
    stems, aliases = set(), {}
    for f in on_disk:
        stem = f[:-3]
        stems.add(stem)
        head = (MEM / f).read_text()[:1200]
        am = re.search(r"^aliases:\s*\[(.*?)\]", head, re.M | re.S)
        if am:
            for a in am.group(1).split(","):
                a = a.strip().strip("'\"")
                if a:
                    aliases[_norm(a)] = stem
    norm_stems = {_norm(s) for s in stems}

    stale, forward = {}, set()
    for f in sorted(on_disk):
        for link in set(re.findall(r"\[\[([^\]\[]+)\]\]", (MEM / f).read_text())):
            n = _norm(link)
            if n in norm_stems:
                continue
            if n in aliases:
                stale.setdefault(link, []).append(f)
            else:
                forward.add(link)
    for link, citers in sorted(stale.items()):
        problems.append(f"STALE LINK: [[{link}]] -> renamed to {aliases[_norm(link)]}, "
                        f"cited by {len(citers)}")
    print(f"forward-marker links (no target yet, by design): {len(forward)}")

    # Size is reported, NOT gated. The "15000 byte ceiling" carried in memory is a
    # number nobody measured. Gating on the guess would have forced a demotion of
    # real memories to satisfy a limit that does not demonstrably exist.
    #
    # Two direct observations, each raising the floor of what is known to work:
    #   2026-08-27  16244 bytes  loaded intact, tail included
    #   2026-08-28  19205 bytes  loaded intact - verified by finding this file's
    #               OWN LAST LINE (the runnable-probe hook) present in the loaded
    #               index at session start, which is the only check that
    #               distinguishes "it all arrived" from "the part I happened to
    #               look at arrived".
    #
    # That verification method is the point. A truncated index is silent: it ends
    # in a plausible place and nothing announces the cut. Confirming the TAIL is
    # the only cheap positive control, so raise this number only after doing it.
    # TRUNCATION WAS OBSERVED, 2026-09-10. This is now a GATE, as the note below
    # always said it should become.
    #   2026-09-10  25216 bytes  TRUNCATED ON LOAD. The loaded copy ended after
    #               byte 24291 and the last THREE entries were missing, silently,
    #               with no marker. Bounded by the next line ending at 24292, so
    #               the ceiling is 24291 retained bytes, not a round number and
    #               not the ~24.4KB guessed earlier.
    # The dropped entries were the three NEWEST, including one written that same
    # session, which is the shape the split was built to prevent: truncation cuts
    # the tail and the tail is where the newest lessons live.
    #   2026-08-28  ~20255 bytes loaded intact - same control: MEMORY.md's own last
    #               line (the coherent-evidence hook) was present as the final entry
    #               of the loaded index at session start. NOTE the size here is
    #               RECONSTRUCTED by subtracting the line added mid-session from a
    #               measured 20545, so treat it as ~20.2KB rather than exact. It is
    #               recorded because the previous figure was a full KB stale and was
    #               about to force demotions to satisfy a limit already disproved.
    # THE AXIS WAS WRONG AGAIN, AND THE OFFICIAL DOCS SETTLE IT (2026-09-10).
    # code.claude.com/docs/en/memory, "Auto memory > How it works", verbatim:
    #   "The first 200 lines of MEMORY.md, or the first 25KB, whichever comes
    #    first, are loaded at the start of every conversation."
    # TWO limits, whichever binds first. The 24291 "measured byte ceiling" above
    # was NOT a byte ceiling: at 25216 bytes the next line ended at 24292, and
    # BOTH candidate byte limits (25*1024=25600, 25*1000=25000) sit far above
    # 24292, so a byte limit cannot have made that cut. The 200-LINE limit did.
    # 24291 is just where line 200 happened to end in that one file - an artifact
    # of that file's line lengths, not a constant. Hard-coding it was the FOURTH
    # wrong-axis calibration in this file's history (400 lines -> "no, bytes" ->
    # <15KB -> 23800/24291 -> it was lines all along, at 200 not 400).
    # 25KB IS DECIMAL, PINNED BY THE HARNESS ITSELF (2026-09-10). Claude Code's
    # own post-write reminder fired on a 23,982-byte file saying: "The memory
    # index at MEMORY.md is 23.4KB, approaching the 24.4KB read limit."
    #   23,982 / 1024 = 23.4 KiB  -> matches its "23.4KB" exactly, so it prints KiB
    #   its "24.4KB" limit  = 24.4 * 1024 ~= 25,000  -> the limit is 25*1000
    #   25*1024 = 25,600 would have printed as "25.0KB", which it did not
    # So the docs' "25KB" is DECIMAL. This also retires the "~24.4KB" that the
    # history above calls an earlier guess: it was never a guess, it was this
    # hook's KiB rendering of 25,000, mistaken for a byte count.
    # The same hook also demands compaction to "under 17.1KB". That is the
    # generic built-in nag, NOT the operative rule - see Phil's standing intent
    # in feedback_memory_index_ceiling_400.md. Gate on the real limits below.
    # LINE_LIMIT / BYTE_LIMIT / LINE_BUDGET / BYTE_BUDGET are module constants now, so the
    # PostToolUse hook gates on the same numbers this report prints.
    size = LOADED.stat().st_size
    lines = LOADED.read_text().count("\n")
    print(f"MEMORY.md lines:  {lines}/{LINE_LIMIT}   (budget {LINE_BUDGET})")
    print(f"MEMORY.md bytes:  {size}/{BYTE_LIMIT}  (budget {BYTE_BUDGET})")
    if lines > LINE_LIMIT:
        problems.append(
            f"MEMORY.md is {lines} lines, ABOVE the documented {LINE_LIMIT}-line "
            f"load limit. Everything past line {LINE_LIMIT} is dropped at load "
            f"RIGHT NOW, silently. Demote entries to MEMORY-FULL.md.")
    elif lines > LINE_BUDGET:
        print(f"  NOTE: {lines - LINE_BUDGET} over line budget, "
              f"{LINE_LIMIT - lines} lines from the hard limit. Demote before adding more.")
    if size > BYTE_LIMIT:
        problems.append(
            f"MEMORY.md is {size} bytes, ABOVE the documented {BYTE_LIMIT}-byte "
            f"load limit. The tail is being dropped at load RIGHT NOW, silently. "
            f"Demote entries to MEMORY-FULL.md.")
    elif size > BYTE_BUDGET:
        print(f"  NOTE: {size - BYTE_BUDGET} over byte budget, "
              f"{BYTE_LIMIT - size} bytes from the hard limit. Demote before adding more.")

    # Advisory, not a failure: these hooks are faithful abbreviations but are too
    # short to decide relevance by, so the memory is effectively unfindable from
    # the loaded tier. The only real fix is demoting entries to buy back bytes -
    # which is Phil's call, so this reports and does not gate.
    if stubs:
        print(f"\n{len(stubs)} STUB HOOK(S) - faithful but too short to judge relevance by.")
        print("  Not a failure. The fix is demotion (buy back bytes), not rewording.")
        for f, lo, fu in sorted(stubs, key=lambda t: len(t[1]))[:10]:
            print(f"  - {f}\n      loaded {len(lo):>3}/{len(fu):<3} chars: {lo!r}")
        if len(stubs) > 10:
            print(f"  ... and {len(stubs) - 10} more")

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print("  -", p)
        return 1
    print("\nOK: every memory is reachable, no dangling entries, "
          "and no loaded hook contradicts the full index.")
    return 0


def _emit(messages: list[str]) -> None:
    """Emit at most one hook object. Silence when there is nothing to say is the point:
    a block that prints on every write is one nobody reads."""
    if not messages:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "\n".join(messages)}}))


def hook() -> None:
    """PostToolUse guard for the two index invariants: no orphans, and MEMORY.md fits.

    This exists because the overflow is SILENT. Claude Code loads the first 200 lines or
    25,000 bytes and drops the rest with no marker, so the only signal was a session
    happening to notice - which took weeks, repeatedly, and cost the three NEWEST entries
    each time. The checker that knows the limits was wired to nothing at all; it was a
    script somebody had to remember to run, which is the same failure one level up.

    Deliberately PostToolUse, not PreToolUse: an Edit's resulting size cannot be known
    without applying the edit, and a guess would either block good writes or miss bad
    ones. The file is already on disk when this fires, but the truncation only bites at
    the NEXT session load, so catching it one tool call later closes the whole gap. It
    reports and never blocks: refusing the write would strand the memory being recorded,
    and a demotion is a judgment about which entries to move, not a thing to automate.
    """
    ev = json.load(sys.stdin)
    paths = []
    inp = ev.get("tool_input") or {}
    for k in ("file_path", "notebook_path"):
        if inp.get(k):
            paths.append(str(inp[k]))
    for e in (inp.get("edits") or []):
        if isinstance(e, dict) and e.get("file_path"):
            paths.append(str(e["file_path"]))

    # INVARIANT 1, AUTO-REPAIRED: every memory on disk has a full-index line.
    # A memory with no index line is INVISIBLE - the index is what a session loads, so the
    # lesson is written, stored, and reaches nobody, and it fails by never appearing. One
    # appeared on 2026-10-05 written by a session through the Write tool (auto:false, so not
    # the curator, which does index what it writes).
    # This REPAIRS rather than warns, which is safe only because sync() is non-destructive:
    # it appends the missing line and takes the hook from the FILE'S OWN `description`,
    # never inventing one. A warning here would be the wrong tool - the action needs no
    # judgement, so asking a human for it just adds a step that can be skipped.
    # Any .md write INSIDE the memory dir triggers the sweep, the index files included: an
    # edit to MEMORY-FULL.md is exactly when a line can get dropped, and the sweep is a glob
    # plus two reads, so there is no reason to be selective about which write pays for it.
    touched_mem_dir = any(
        pathlib.Path(p).suffix == ".md" and pathlib.Path(p).resolve().parent == MEM
        for p in paths)
    repaired = []
    if touched_mem_dir:
        try:
            on_disk = {p.name for p in MEM.glob("*.md")} - {LOADED.name, FULL.name}
            full, loaded = indexed(FULL), indexed(LOADED)
            missing = sorted(on_disk - set(full))
            if missing:
                # sync() prints progress for humans; a hook's stdout must be ONLY the
                # JSON object, so that chatter goes to stderr or it corrupts the message.
                with contextlib.redirect_stdout(sys.stderr):
                    sync(on_disk, full, loaded)
                repaired = missing
        except Exception:
            repaired = []            # never let a repair failure break the write

    # ONE message, both invariants. A hook emits a single JSON object, so these accumulate
    # rather than printing as they are found; two prints would make the second invisible.
    out = []
    if repaired:
        out.append(
            "[memory-index] AUTO-INDEXED %d orphaned memor%s: %s\n"
            "  A memory with no MEMORY-FULL.md line is invisible to every future session, so "
            "the line was added from the file's own `description`. Check it reads well; if "
            "the description was thin, widen it now rather than later."
            % (len(repaired), "y" if len(repaired) == 1 else "ies",
               ", ".join(r[:-3] for r in repaired[:4])))

    # INVARIANT 2, REPORTED NOT REPAIRED: MEMORY.md fits inside the load limits.
    # Not repaired because the fix is a judgement about WHICH entries to demote, unlike the
    # orphan case where the action is mechanical.
    if not any(pathlib.Path(p).name == LOADED.name for p in paths) or not LOADED.exists():
        _emit(out)
        return
    size = LOADED.stat().st_size
    lines = LOADED.read_text().count("\n")
    over = []
    if lines > LINE_LIMIT:
        over.append(f"{lines} LINES, past the hard {LINE_LIMIT}-line load limit: "
                    f"everything after line {LINE_LIMIT} is being dropped at load right "
                    f"now, silently, newest entries first")
    elif lines > LINE_BUDGET:
        over.append(f"{lines} lines, {LINE_LIMIT - lines} from the hard {LINE_LIMIT} limit")
    if size > BYTE_LIMIT:
        over.append(f"{size} BYTES, past the hard {BYTE_LIMIT}-byte load limit: the tail "
                    f"is being dropped at load right now, silently")
    elif size > BYTE_BUDGET:
        over.append(f"{size} bytes, {BYTE_LIMIT - size} from the hard {BYTE_LIMIT} limit")
    if not over:
        _emit(out)
        return
    hard = lines > LINE_LIMIT or size > BYTE_LIMIT
    out.append("[memory-index] %s MEMORY.md is %s.\n"
               "  %s Demote entries to MEMORY-FULL.md (which is NOT capped and is the "
               "complete index, so a demoted entry stays reachable - it is already listed "
               "there, duplication across tiers is correct by design).\n"
               "  Full report: python3 %s"
               % ("OVER THE LOAD LIMIT:" if hard else "approaching the load limit:",
                  " and ".join(over),
                  "Fix this before ending the turn." if hard else "Do not add more entries.",
                  __file__))
    _emit(out)


if __name__ == "__main__":
    if "--hook" in sys.argv:
        try:
            hook()
        except Exception as e:  # never break the session on a guard bug
            print(f"check-memory-index --hook: {e}", file=sys.stderr)
        sys.exit(0)
    sys.exit(main("--fix-counts" in sys.argv))
