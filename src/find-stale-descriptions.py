#!/usr/bin/env python3
"""Find memories whose DESCRIPTION still asserts what their own BODY has already refuted.

WHY THIS IS THE HIGHEST-VALUE AUDIT IN THE CORPUS. The description is the recall surface:
it is what the search hook injects into a session. A memory whose body carries "REFUTED"
while its description carries the refuted claim is therefore not merely untidy, it is
ACTIVELY SERVING A WRONG CLAIM, and the file looks fixed to anyone who opens it. Measured
instances: feedback_check_play_protect_certification_before_debugging_integrity did this for
23 days, and feedback_memory_file_size_management answered a direct question from Phil with
a wrong line limit while its body had the right one.

PRECISION MATTERS MORE THAN RECALL HERE, because the fix is a hand edit and a wrong "fix"
rewrites a correct description. The first pass of this scan reported 99 candidates of which
5 of the 7 I checked were false positives, all from three causes now excluded:

  1. the marker sat inside a [[wikilink]] to another memory's name
  2. the marker refuted a SUB-hypothesis, not the headline claim
  3. the marker CONFIRMED the description ("...is not sufficient" / "the hypothesis is DEAD")

Only (1) and parts of (3) are mechanically detectable; (2) needs a human. So this prints the
marker's own sentence beside the description and makes no edits. It is a reading list.

    python3 find-stale-descriptions.py            # the triage list
    python3 find-stale-descriptions.py --count    # just the number, for a regression check
"""
import glob
import os
import re
import sys

MEM = os.path.expanduser("~/.claude/projects/-home-plafayette/memory/")

# REVERSAL words only. "CORRECTED"/"CORRECTION" are deliberately excluded: in this corpus
# they usually mark a self-correction made DURING an investigation that the description
# already reflects, and including them put the false-positive rate over half.
MARK = re.compile(r"\b(REFUTED|SUPERSEDED|RETRACTED|OBSOLETE|WAS WRONG|IS WRONG|"
                  r"NO LONGER TRUE|TITLE REFUTED)\b")
# The DESCRIPTION side is matched case-insensitively and on more inflections, because the
# question there is only "does this description already warn the reader?" - and a fixed
# description legitimately says "refuted the same night" or "REFUTES the fix I recorded".
# Matching the description as strictly as the body made this script's own count useless as a
# regression metric: it kept reporting files I had just corrected.
DESC_MARK = re.compile(r"\b(refut\w*|supersed\w*|retract\w*|obsolete|dead design|"
                       r"was wrong|is wrong|no longer true|wrong twice|both wrong|"
                       r"corrected \d{4}-\d{2}-\d{2}|does not follow)\b", re.I)
WIKI = re.compile(r"\[\[[^\]]*\]\]")
# A marker in a trailing Related/See-also/Merged block is about ANOTHER memory, not this one.
TAIL = re.compile(r"^\s*(Related:|See also:|See \[\[|\*Merged from|## Merged from)", re.M)


CLEARED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "stale-desc-cleared.txt")


def cleared():
    """Files already opened, read and judged not defects. See that file's header.

    Without this the count can never reach zero - it re-reports the same noise every run and
    trains the reader to skim past it, which is the wallpaper failure this whole system keeps
    rediscovering. With it, the number means "candidates nobody has looked at yet".
    """
    out = set()
    try:
        for line in open(CLEARED_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line.split()[0])
    except OSError:
        pass
    return out


def candidates():
    out, skip = [], cleared()
    for path in sorted(glob.glob(MEM + "*.md")):
        base = os.path.basename(path)
        if base.startswith("MEMORY") or base[:-3] in skip:
            continue
        text = open(path, encoding="utf-8").read()
        m = re.search(r'^description:\s*"?(.+?)"?\s*$', text, re.M)
        if not m:
            continue
        desc, body = m.group(1), text[m.end():]
        if DESC_MARK.search(desc):
            continue                      # already warns the reader where it counts
        # Strip what is demonstrably not a statement about THIS memory's claim.
        clean = WIKI.sub("", body)
        cut = TAIL.search(clean)
        if cut:
            clean = clean[:cut.start()]
        hit = MARK.search(clean)
        if not hit:
            continue
        # The marker's own sentence, which is what a reader needs to judge it.
        start = clean.rfind("\n\n", 0, hit.start()) + 2
        end = clean.find("\n\n", hit.end())
        sentence = re.sub(r"\s+", " ", clean[start:end if end > 0 else len(clean)]).strip()
        out.append({
            "file": base, "mark": hit.group(1),
            "pct": round(100 * hit.start() / max(len(clean), 1)),
            "desc": desc, "sentence": sentence,
        })
    out.sort(key=lambda c: -c["pct"])
    return out


def main() -> int:
    c = candidates()
    if "--count" in sys.argv:
        print(len(c))
        return 0
    print("%d candidate(s): description carries no reversal marker, body does.\n"
          "pct = how far into the body the marker sits (high = appended at the end).\n"
          "THIS IS A READING LIST, NOT A DEFECT LIST. Open each before editing.\n" % len(c))
    for i, x in enumerate(c, 1):
        print("--- %d. [%s %d%%] %s" % (i, x["mark"], x["pct"], x["file"][:-3]))
        print("    DESC: %s" % x["desc"][:190])
        print("    BODY: %s\n" % x["sentence"][:330])
    return 0


if __name__ == "__main__":
    sys.exit(main())
