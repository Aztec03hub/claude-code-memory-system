#!/usr/bin/env python3
"""Self-check for the MEMORY.md load-limit gate in check-memory-index.py --hook.

The thing under test is a WARNING, and a warning that does not fire is
indistinguishable from a file that is fine - which is exactly how the index
overflowed silently for weeks. So both arms are asserted: it must shout on an
over-limit file AND stay quiet on a healthy one. A test that only checked the
shout would pass just as happily on a hook that shouts at everything.

    python3 test_index_guard.py
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "check-memory-index.py"


def run(memdir: pathlib.Path, file_path: str) -> str:
    """Invoke the hook against a throwaway memory dir, return its stdout."""
    script = memdir / "check-memory-index.py"
    ev = {"tool_name": "Write", "tool_input": {"file_path": file_path}}
    p = subprocess.run([sys.executable, str(script), "--hook"],
                       input=json.dumps(ev), capture_output=True, text=True)
    assert p.returncode == 0, f"hook must always exit 0, got {p.returncode}: {p.stderr}"
    return p.stdout.strip()


def ctx(out: str) -> str:
    if not out:
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def case(lines: int, width: int, memories=()):
    """Build a temp memory dir whose MEMORY.md has `lines` lines of `width` bytes.

    `memories` is a list of (stem, description) written as real memory FILES, so the
    orphan invariant can be exercised against something the checker will actually glob.
    """
    d = pathlib.Path(tempfile.mkdtemp())
    shutil.copy(SCRIPT, d / "check-memory-index.py")
    body = "".join("- [%s](x_%d.md)\n" % ("h" * width, i) for i in range(lines))
    (d / "MEMORY.md").write_text(body)
    (d / "MEMORY-FULL.md").write_text(body)
    for stem, desc in memories:
        (d / (stem + ".md")).write_text(
            '---\nname: %s\ndescription: "%s"\nmetadata:\n  type: feedback\n---\n\nbody\n'
            % (stem, desc))
    return d


def full_index(d):
    return (d / "MEMORY-FULL.md").read_text()


def main() -> int:
    # 1. Healthy file: silent. The arm that catches a hook that cries wolf.
    d = case(100, 50)
    assert run(d, str(d / "MEMORY.md")) == "", "healthy index must produce NO output"

    # 2. Over the 200-LINE limit. This is the axis that actually keeps binding, and the
    #    one four earlier calibrations got wrong, so it is pinned with bytes held low.
    d = case(250, 20)
    assert (d / "MEMORY.md").stat().st_size < 25000, "fixture must isolate the line axis"
    m = ctx(run(d, str(d / "MEMORY.md")))
    assert "OVER THE LOAD LIMIT" in m and "250 LINES" in m, m
    assert "Fix this before ending the turn" in m, m

    # 3. Over the 25,000-BYTE limit with lines well under 200: the other axis, alone.
    d = case(120, 300)
    assert (d / "MEMORY.md").read_text().count("\n") < 200, "fixture must isolate bytes"
    m = ctx(run(d, str(d / "MEMORY.md")))
    assert "OVER THE LOAD LIMIT" in m and "BYTES" in m, m

    # 4. Inside the limits but past the budget: a note, NOT the hard wording. If these
    #    two rendered identically the gate would be useless at the moment it matters.
    d = case(195, 20)
    m = ctx(run(d, str(d / "MEMORY.md")))
    assert "approaching" in m and "OVER THE LOAD LIMIT" not in m, m
    assert "195 lines" in m and "5 from the hard 200 limit" in m, m

    # 5. A write to some OTHER file must not fire, however broken MEMORY.md is.
    d = case(250, 300)
    assert run(d, str(d / "notes.md")) == "", "must only fire for MEMORY.md"

    # 6. MultiEdit-shaped input (edits[].file_path) is still seen.
    d = case(250, 20)
    ev = {"tool_name": "Edit",
          "tool_input": {"edits": [{"file_path": str(d / "MEMORY.md")}]}}
    p = subprocess.run([sys.executable, str(d / "check-memory-index.py"), "--hook"],
                       input=json.dumps(ev), capture_output=True, text=True)
    assert "OVER THE LOAD LIMIT" in p.stdout, p.stdout

    # 7. Malformed stdin must not break the session.
    p = subprocess.run([sys.executable, str(SCRIPT), "--hook"],
                       input="not json", capture_output=True, text=True)
    assert p.returncode == 0 and not p.stdout.strip(), p

    # ---- INVARIANT 2: no orphans, and the hook REPAIRS rather than warns. ----
    # An orphan is invisible by construction: it fails by never appearing, so a warning
    # that can be skipped is not enough. These assert the repair actually lands on disk.
    d = case(5, 20, memories=[("feedback_brand_new_lesson", "a real description here")])
    assert "feedback_brand_new_lesson" not in full_index(d), "fixture must start orphaned"
    m = ctx(run(d, str(d / "feedback_brand_new_lesson.md")))
    assert "AUTO-INDEXED" in m and "brand_new_lesson" in m, m
    idx = full_index(d)
    assert "(feedback_brand_new_lesson.md)" in idx, "the line must be ON DISK, not just announced"
    assert "a real description here" in idx, \
        "the hook must take the hook text from the file's OWN description, never invent one"

    # Idempotent: a second write must not duplicate the line or re-announce.
    before = full_index(d)
    assert run(d, str(d / "feedback_brand_new_lesson.md")) == "", "second pass must be silent"
    assert full_index(d) == before, "must not append a duplicate line"

    # It must repair an orphan even when the write was to a DIFFERENT memory, because the
    # orphan may have been created by something that never triggers a hook at all.
    d = case(5, 20, memories=[("feedback_one", "desc one"), ("feedback_two", "desc two")])
    m = ctx(run(d, str(d / "feedback_one.md")))
    assert "AUTO-INDEXED" in m, m
    assert "(feedback_one.md)" in full_index(d) and "(feedback_two.md)" in full_index(d), \
        "a single write should reconcile the whole directory, not just the file written"

    # Writing a NON-memory file must not touch the index at all.
    d = case(5, 20, memories=[("feedback_three", "desc three")])
    before = full_index(d)
    assert run(d, str(d / "notes.txt")) == "", "a non-memory write must be silent"
    assert full_index(d) == before, "a non-memory write must not alter the index"

    # BOTH invariants in one write must produce ONE message carrying both, because a hook
    # emits a single object and a second print would be silently discarded.
    d = case(250, 20, memories=[("feedback_four", "desc four")])
    m = ctx(run(d, str(d / "MEMORY.md")))
    assert "OVER THE LOAD LIMIT" in m, m
    assert "AUTO-INDEXED" in m, "the orphan repair must not be lost behind the size report"

    print("OK: all 11 cases pass. Size: silent when healthy, loud on both axes, budget "
          "distinct from limit, scoped to MEMORY.md, crash-safe. Orphans: repaired on disk "
          "from the file's own description, idempotent, reconciles the whole directory, "
          "ignores non-memory writes, and both invariants share one message.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
