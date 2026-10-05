#!/usr/bin/env python3
"""Guard: nothing in this repo may identify a memory by its frontmatter `name`.

THE BUG THIS EXISTS FOR, three times in one session on 2026-10-05:

  1. show-pairs.py reported most supersession pairs as FILE NOT FOUND, because pairs name
     their sides by frontmatter `name` (hyphenated) and the corpus is filenames (underscored,
     type-prefixed). Its own docstring already recorded the mistake.
  2. build-rerank-eval.py selected the `name` column and compared it to the filename stem by
     two-way substring containment. 288 of 542 "absent" readings were a memory sitting in the
     candidate window that the matcher could not recognise, which published a 13.0% absent
     rate that is really 5.5%, and invented a 75-memory "unretrievable" population.
  3. The duplicate-resolution pass hit the same thing on the same day.

A memory's `name:` is hyphenated, usually drops the type prefix, and is sometimes rewritten
after the file is created. The FILENAME is the identity: doc2query is keyed by path, the
labels are keyed by path, the index stores path. So match on path, exactly, always.

This is a static guard rather than a behavioural test on purpose. The failure is silent and
plausible - it degrades a number instead of raising - so the cheap thing that catches a
FOURTH occurrence is refusing the construct, not measuring the consequence again.

    python3 test_matcher_uses_path.py
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
MEM = pathlib.Path("~/.claude/projects/-home-plafayette/memory").expanduser()

# Selecting `name` for DISPLAY is fine. Selecting it as the only column, which is what you
# do when you are about to match on it, is the shape that went wrong three times.
BANNED = re.compile(r"SELECT\s+name\s+FROM\s+mem", re.I)


def main() -> int:
    offenders = []
    for f in sorted(HERE.glob("*.py")):
        if f.name == pathlib.Path(__file__).name:
            continue
        lines = f.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            if not BANNED.search(line):
                continue
            # One legitimate use exists: showing names to the writing model so it can emit
            # [[name]] links, which ARE frontmatter names. It must say so within the five
            # preceding lines, so the exemption is visible at the call site rather than
            # kept in a list here that nobody reads next to the code.
            if any("matcher-guard: display only" in l for l in lines[max(0, i - 6):i]):
                continue
            offenders.append("%s:%d  %s" % (f.name, i, line.strip()))
    if offenders:
        print("FAIL: these identify a memory by its frontmatter `name`, not its path:")
        for o in offenders:
            print("  " + o)
        print("\nSelect `path` and compare os.path.basename(path)[:-3] to the stem.")
        return 1

    # And prove the hazard is real on this corpus rather than asserting it from memory: if
    # every `name` happened to equal its filename the guard would be arguing with nobody,
    # and a guard whose premise has quietly become false is the next thing to mislead me.
    differ = total = 0
    for f in MEM.glob("*.md"):
        if f.name in ("MEMORY.md", "MEMORY-FULL.md"):
            continue
        m = re.search(r"^name:\s*(.+)$", f.read_text(encoding="utf-8", errors="replace"), re.M)
        if not m:
            continue
        total += 1
        if m.group(1).strip() != f.stem:
            differ += 1
    if total == 0:
        print("SKIP: no corpus at %s, static guard passed" % MEM)
        return 0
    assert differ, ("no memory's `name` differs from its filename, so this guard has "
                    "nothing to protect - check the premise before deleting it")

    print("OK: no instrument matches on the frontmatter `name`, and the hazard is real "
          "(%d of %d memories have a `name` that differs from their filename, so a "
          "name-based matcher would silently miss them)." % (differ, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
