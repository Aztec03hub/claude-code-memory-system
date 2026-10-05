#!/usr/bin/env python3
"""Rebuild MEMORY.md as a FUNCTION-SELECTED front page, not a ranked list of memories.

WHY THIS EXISTS (measured 2026-10-05, 1031 memories, 167 loaded entries):

    loaded-tier memories     1,129 injections   197 used   17.4%
    everything else          4,942 injections   736 used   14.9%

Being listed in MEMORY.md bought 2.5 percentage points. The FTS5+rerank hook retrieves
those memories anyway, so enumerating them in the loaded tier was almost pure redundancy:
94 of the 167 listed entries had NEVER once been judged useful, while 258 memories that
HAD been useful were not listed at all.

Meanwhile the real cost: 18 of 41 PHIL-LOCKED standing rules were NOT in the loaded tier.
Those are the one class query-time search cannot reach, because a rule like "implementation
agents use sonnet" shares no vocabulary with the prompt that violates it. The 200-line cap
was evicting exactly the memories that have to be unconditional.

So the loaded tier stops competing with search and carries only what search cannot:

  ALWAYS  unconditional conduct rules. Selected by FUNCTION, not by recency or rank.
  MAP     one line per live domain, pointing at the doc that indexes it. Not per-memory.

THE PREFIX INVARIANT. check-memory-index.py requires every loaded hook to be a prefix of
its MEMORY-FULL.md hook: the loaded tier may say LESS, never something different. So hooks
here are TRUNCATED FROM FULL at a word boundary, never rewritten. That is also why no new
prose is invented - a fresh hook would be an LLM claim about what a memory says, and 2 in 9
of those were materially wrong on the last measured run.

    python3 rebuild-loaded-tier.py            # dry run, prints the new file and a diff
    python3 rebuild-loaded-tier.py --apply    # write it (backs up first)
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

MEM = pathlib.Path(os.path.expanduser(
    "~/.claude/projects/-home-plafayette/memory"))
LOADED, FULL = MEM / "MEMORY.md", MEM / "MEMORY-FULL.md"
LINK = re.compile(r"^- \[(.*)\]\((.*)\)\s*$", re.M)

HOOK_MAX = 150   # generous: at ~56 entries the budget is nowhere near binding

# Selection criterion, applied to every candidate: WOULD QUERY-TIME SEARCH FIND THIS WHEN
# IT IS NEEDED? If the prompt that should trigger the rule contains the rule's own
# vocabulary, search finds it and it does not belong here. If it does not - a scheduling
# preference, a model choice, a name spelling, a person - search cannot help and the rule
# must be unconditional. Technical references were deliberately NOT carried over: they are
# what the searcher is good at, which the usefulness numbers above confirm.
CONFIG = pathlib.Path(__file__).resolve().parent / "loaded-tier.config.json"


def load_selection() -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Read the curation list from JSON beside this script, not from source.

    It lives out of tree because it is one user's list, not the tool's: it names their
    standing rules, their live projects, and in places real people and clients. A tool
    whose selection list is a literal in its own source cannot be published or reused,
    and redacting such a literal before each publish is a step that WILL be skipped.
    loaded-tier.config.example.json shows the shape.
    """
    if not CONFIG.exists():
        sys.exit("no %s - copy loaded-tier.config.example.json and edit it" % CONFIG.name)
    cfg = json.loads(CONFIG.read_text())
    always = [(s["section"], list(s["memories"])) for s in cfg["ALWAYS"]]
    return always, list(cfg["MAP"])


ALWAYS, MAP = load_selection()

PREAMBLE = """# Memory Index

**This is not a list of memories.** A query-time hook searches all {total} of them on every
prompt and injects the relevant ones, so enumerating them here was measured as redundant:
listed entries were judged useful 17.4% of the time against 14.9% for everything else, and
94 of 167 listed entries had never been useful once. This file carries only what
query-time search CANNOT: rules that must apply even when the prompt shares no vocabulary
with them.

Hard load limit: the first **200 lines** or **25,000 bytes**, whichever comes first. The
tail is dropped SILENTLY. A PostToolUse hook now gates this on every write; the derivation
of both axes is in `check-memory-index.py`. Everything is in `MEMORY-FULL.md`; a loaded
hook is always a PREFIX of its full hook, so widen FULL before rewording one here.

## ALWAYS - unconditional, regardless of what the prompt mentions
"""

MAP_HEADER = """
## MAP - live domains and where to start reading

## Everything else

`MEMORY-FULL.md`, same directory, is the complete index of all {total} memories. Read it
when a task touches an area not named above. Do not expect to find a technical memory
listed here: the searcher retrieves those well, and that is the measured division of
labour this file is built on.
"""


FULL_MAX = 320   # MEMORY-FULL.md is not load-capped; this is just to keep lines readable


def hooks(path: pathlib.Path) -> dict[str, str]:
    return {m.group(2): m.group(1).strip() for m in LINK.finditer(path.read_text())}


def clip(hook: str, limit: int = HOOK_MAX) -> str:
    """Truncate at a word boundary. A truncation of the full hook IS a prefix of it.

    Both tiers clip with this one function, and the loaded tier clips the ALREADY-CLIPPED
    full hook, so the prefix invariant holds by construction rather than by checking.
    """
    if len(hook) <= limit:
        return hook
    cut = hook[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit // 2 else cut).rstrip(" ,;:-")


def sanitise(s: str) -> str:
    """Make a string safe as markdown link text, and obey the em-dash rule.

    Square brackets would end the link early. Em-dashes are a PHIL-LOCKED standing rule
    (feedback_no_em_dashes_hyphens_ok), and some older descriptions still carry them, so
    they are normalised to hyphens here rather than silently propagated into the one file
    that is loaded into every session.
    """
    return (s.replace("—", "-").replace("–", "-")
             .replace('\\"', '"').replace("\\'", "'")
             .replace("[", "(").replace("]", ")").strip())


def description(name: str) -> str:
    """The memory's own `description:` frontmatter: the AUTHOR's words, never invented.

    A rewritten hook is an LLM claim about what a memory says, and 2 in 9 were materially
    wrong on the last measured run of that path (see cmd_curate_hooks in memory-curator.py).
    Lifting the description avoids that class entirely. It is also the recall surface the
    searcher already injects, so the index and the search agree on what a memory is about.
    """
    body = (MEM / (name + ".md")).read_text()
    m = re.search(r'^description:\s*"?(.+?)"?\s*$', body, re.M)
    return sanitise(m.group(1) if m else "")


def widen_full(selected: list[str], full: dict[str, str]) -> tuple[str, int]:
    """Replace starved MEMORY-FULL.md hooks for selected entries with their descriptions.

    The old entry shape spent 86% of a byte-capped budget on markup, so hooks were cut to
    whatever fitted - a median of 20 characters, 132 of 163 too short to judge relevance
    by. Those cuts are still in MEMORY-FULL.md, which was never byte-capped and never
    needed them. The loaded tier may only ABBREVIATE the full tier, so FULL has to be
    widened first or the better hook cannot be shown where it matters.
    """
    text = FULL.read_text()
    n = 0
    for name in selected:
        fh, desc = full[name + ".md"], description(name)
        if len(desc) <= len(fh) + 20:
            continue
        # Three repairable cases, all damage from the old byte-capped entry shape:
        #   STARVED    cut short to fit, under 90 chars.
        #   TRUNCATED  a strict PREFIX of the description, so it was cut from it.
        #   MID-WORD   ends on a word fragment. The old cuts had no word-boundary logic,
        #              so a hand-written hook can still end "...AND LOOK AT IT. A uiaut".
        # Detected by asking whether the hook's last token is a real word: it must end in
        # punctuation, or appear whole in the description. A hook that is none of these is
        # a deliberate, distinct summary and is left alone.
        last = re.split(r"[\s]+", fh)[-1] if fh else ""
        clean_end = (fh[-1:] in ".!?)\"'" or not last
                     or re.search(r"\b%s\b" % re.escape(last), desc) is not None)
        if len(fh) >= 90 and not desc.startswith(fh[:len(fh) - 1]) and clean_end:
            continue
        new = clip(desc, FULL_MAX)
        if not new or new == fh:
            continue
        text = re.sub(r"^- \[[^\]]*\]\(%s\.md\)\s*$" % re.escape(name),
                      lambda _m, v=new, f=name: "- [%s](%s.md)" % (v, f),
                      text, flags=re.M)
        n += 1
    return text, n


def main(apply: bool) -> int:
    full, loaded = hooks(FULL), hooks(LOADED)
    on_disk = {p.name for p in MEM.glob("*.md")} - {LOADED.name, FULL.name}
    total = len(on_disk)

    selected = [n for _s, names in ALWAYS for n in names] + MAP
    dupes = {n for n in selected if selected.count(n) > 1}
    missing_file = [n for n in selected if n + ".md" not in on_disk]
    missing_full = [n for n in selected if n + ".md" not in full]
    if dupes or missing_file or missing_full:
        for n in sorted(dupes):
            print(f"  DUPLICATE in selection: {n}")
        for n in missing_file:
            print(f"  NO SUCH MEMORY FILE: {n}.md")
        for n in missing_full:
            print(f"  NOT IN MEMORY-FULL.md (would break the tier invariant): {n}.md")
        print("\nREFUSING to build. Fix the selection above.")
        return 1

    # FULL first, ALWAYS: the loaded hook may only abbreviate the full hook, so a better
    # hook cannot reach the loaded tier until MEMORY-FULL.md carries it.
    full_text, widened = widen_full(selected, full)
    full = {m.group(2): m.group(1).strip() for m in LINK.finditer(full_text)}
    print(f"widened {widened} starved hook(s) in MEMORY-FULL.md from their own descriptions")

    out = [PREAMBLE.format(total=total)]
    for section, names in ALWAYS:
        out.append(f"\n### {section}")
        for n in names:
            out.append(f"- [{clip(full[n + '.md'])}]({n}.md)")
    out.append(MAP_HEADER.format(total=total).split("## Everything else")[0].rstrip())
    for n in MAP:
        out.append(f"- [{clip(full[n + '.md'])}]({n}.md)")
    out.append("\n## Everything else"
               + MAP_HEADER.format(total=total).split("## Everything else")[1].rstrip()
               + "\n")
    text = "\n".join(out)

    lines, size = text.count("\n"), len(text.encode())
    print(f"new MEMORY.md: {lines}/200 lines, {size}/25000 bytes, "
          f"{len(selected)} entries (was {len(loaded)})")
    if lines > 190 or size > 24000:
        print("REFUSING: the rebuild is itself over budget.")
        return 1

    kept = set(selected) & {p[:-3] for p in loaded}
    added = set(selected) - {p[:-3] for p in loaded}
    dropped = {p[:-3] for p in loaded} - set(selected)
    print(f"  kept {len(kept)}   newly promoted {len(added)}   demoted to FULL only {len(dropped)}")
    # Demotion is only safe because FULL is the COMPLETE index and the two tiers overlap
    # by design. Verified here rather than asserted: a previous session inferred that a
    # memory belongs to exactly one tier and deleted 154 lines from FULL.
    unreachable = [d for d in dropped if d + ".md" not in full]
    if unreachable:
        print(f"  REFUSING: {len(unreachable)} demoted entries are in NO full-index line: "
              f"{unreachable[:5]}")
        return 1
    print(f"  all {len(dropped)} demoted entries verified still listed in MEMORY-FULL.md")

    if not apply:
        print("\n--- new file ---")
        print(text)
        print("--- end ---\nDRY RUN. Re-run with --apply to write it.")
        return 0

    d = MEM / ".backups"
    d.mkdir(exist_ok=True)
    tag = "pre-rebuild-%d" % os.getpid()
    (d / f"MEMORY.md.{tag}").write_text(LOADED.read_text())
    (d / f"MEMORY-FULL.md.{tag}").write_text(FULL.read_text())
    FULL.write_text(full_text)
    LOADED.write_text(text)
    print(f"\nwritten both tiers. backups at {d}/*.{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
