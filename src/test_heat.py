#!/usr/bin/env python3
"""Self-check for the recalibrated heat curve and the stale-hold rule.

Both arms matter here. The old curve was not wrong in a way any single assertion would
have caught - it was wrong in that NOTHING COULD EVER PROMOTE, which looks exactly like a
quiet system with nothing worth promoting. So these cases pin the promotion boundary from
both sides: two sessions must cross it, and one session must not, at any depth.

    python3 test_heat.py
"""
import datetime
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("mc", "memory-curator.py")
mc = importlib.util.module_from_spec(spec)
sys.modules["mc"] = mc
spec.loader.exec_module(mc)


def occ(session, n, days_ago=0):
    """n occurrences inside one session, stamped `days_ago` days back."""
    t = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    return [{"session": session, "tid": f"{session}-{i}",
             "ts": (t + datetime.timedelta(seconds=i)).isoformat(timespec="seconds")}
            for i in range(n)]


def main() -> int:
    H, C = mc.heat_of, mc.HEAT_CREATE

    # --- the promotion boundary, from both sides ---
    assert H(occ("s1", 1)) == 1.5, H(occ("s1", 1))
    assert H(occ("s1", 2)) == 1.75, H(occ("s1", 2))
    # ONE session can never promote, however deep. This is the branch that was deleted:
    # the deepest proposal in the real log had 8 occurrences, and 118 was once possible.
    for n in (5, 9, 20, 118):
        assert H(occ("s1", n)) < C, f"one session with {n} occurrences promoted: {H(occ('s1', n))}"
    assert H(occ("s1", 20)) == mc.HEAT_SESSION_CAP, "deep single session must sit at the cap"

    # TWO independent sessions promote on one occurrence each. This is the signal the
    # whole mechanism exists to measure, and under the old curve it did not suffice.
    two = occ("s1", 1) + occ("s2", 1)
    assert H(two) >= C, f"two sessions must promote, got {H(two)}"

    # Within-session recurrence must still COUNT (Phil rejected a per-session-only curve),
    # so a deep first session must outrank a shallow one at equal session count.
    assert H(occ("s1", 4) + occ("s2", 1)) > H(occ("s1", 1) + occ("s2", 1))

    # Ordering inside the single-session population must stay informative, or the queue
    # cannot be triaged by heat at all.
    singles = [H(occ("s1", n)) for n in (1, 2, 3, 4)]
    assert singles == sorted(singles) and len(set(singles)) == 4, singles

    # --- the two bars must be distinct and ordered ---
    # If these ever collapse, unattended creation silently resumes at the 2-session line
    # and the review gap that caught 3 mis-bucketed proposals in 11 disappears.
    assert mc.HEAT_CREATE < mc.HEAT_AUTO, "auto-create must need MORE than eligibility"
    assert H(occ("s1", 1) + occ("s2", 1)) >= mc.HEAT_CREATE, "2 sessions must be eligible"
    assert H(occ("s1", 1) + occ("s2", 1)) < mc.HEAT_AUTO, \
        "2 shallow sessions must NOT auto-create: one mis-bucketed occurrence reaches it"
    # Two sessions where one saw it repeatedly, or three sessions, may auto-create.
    assert H(occ("s1", 5) + occ("s2", 1)) >= mc.HEAT_AUTO
    assert H(occ("s1", 1) + occ("s2", 1) + occ("s3", 1)) >= mc.HEAT_AUTO

    # --- the stale-hold rule ---
    # A dict-shaped record is all hold_stale_proposals reads, so drive it through the real
    # reducer instead: write rows to a temp log and check what the reduction says.
    import json
    import os
    import tempfile
    d = tempfile.mkdtemp()
    log = os.path.join(d, "mem-proposals.jsonl")
    saved_path, saved_rollup = mc.MEM_PROPOSALS_PATH, mc.ROLLUP_PATH
    mc.MEM_PROPOSALS_PATH = log
    mc.ROLLUP_PATH = os.path.join(d, "rollup.json")
    try:
        old = (datetime.datetime.now()
               - datetime.timedelta(days=mc.STALE_HOLD_DAYS + 1)).isoformat(timespec="seconds")
        new = datetime.datetime.now().isoformat(timespec="seconds")
        rows = [
            # stale, one session -> must be held
            {"event": "propose", "pid": "aaa", "name": "stale-one-session", "ts": old,
             "session": "s1", "tid": "t1", "description": "d", "mtype": "feedback"},
            # fresh, one session -> must NOT be held
            {"event": "propose", "pid": "bbb", "name": "fresh-one-session", "ts": new,
             "session": "s1", "tid": "t2", "description": "d", "mtype": "feedback"},
            # stale but TWO sessions -> recurring, must NOT be held however old
            {"event": "propose", "pid": "ccc", "name": "stale-two-sessions", "ts": old,
             "session": "s1", "tid": "t3", "description": "d", "mtype": "feedback"},
            {"event": "propose", "pid": "ccc", "name": "stale-two-sessions", "ts": old,
             "session": "s2", "tid": "t4", "description": "d", "mtype": "feedback"},
        ]
        with open(log, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

        assert mc.hold_stale_proposals(dry=True) == 1, "dry run must find exactly one"
        assert len(mc.open_mem_proposals()) == 3, "a dry run must change nothing"

        assert mc.hold_stale_proposals() == 1
        openp = mc.open_mem_proposals()
        assert "aaa" not in openp, "stale single-session proposal must be closed"
        assert "bbb" in openp and "ccc" in openp, "fresh and recurring must survive"

        # THE SAFETY PROPERTY: hold is re-openable. A recurrence months later must bring
        # the proposal back, or this rule would be destroying evidence rather than
        # draining a queue.
        with open(log, "a") as fh:
            fh.write(json.dumps({"event": "propose", "pid": "aaa",
                                 "name": "stale-one-session", "ts": new, "session": "s9",
                                 "tid": "t9", "description": "d", "mtype": "feedback"}) + "\n")
        back = mc.open_mem_proposals()
        assert "aaa" in back, "a recurrence MUST reopen a held proposal"
        assert len(back["aaa"]["occurrences"]) == 1, "reopened from scratch, old evidence spent"

        # And it must be idempotent: running twice must not double-write or reopen.
        assert mc.hold_stale_proposals() == 0, "nothing left to hold on a second pass"
    finally:
        mc.MEM_PROPOSALS_PATH, mc.ROLLUP_PATH = saved_path, saved_rollup

    print("OK: promotion needs 2 sessions and 1 can never reach it; within-session "
          "recurrence still counts and still orders the queue; stale single-session "
          "proposals are held, recurring and fresh ones are not, and a hold reopens.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
