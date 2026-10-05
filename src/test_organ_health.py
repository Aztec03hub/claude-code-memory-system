#!/usr/bin/env python3
"""Self-check for the LLM organ health signal.

THREE CONTROLS, per Phil's standing requirement that it become impossible for this class of
failure to be missed again: a SUCCESS control, a FAILURE control, and a KILLED control. The
kill control is not ceremony - the last time this standard was applied it caught a harness
that reported DONE ok on a job that had been killed.

The hardest property to test is the one that actually failed in production: a signal that
says nothing must mean "healthy", not "the signal is broken". So every case here asserts
what the banner does NOT say as well as what it does.

    python3 test_organ_health.py
"""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("oh", os.path.join(HERE, "organ_health.py"))
oh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oh)

NOW = time.time()
DAY = 86400


def fresh():
    d = tempfile.mkdtemp()
    oh.configure(d)
    return d


def write(rows):
    """Write rows as (organ, ok, kind, hours_ago)."""
    with open(oh.HEALTH_PATH, "w", encoding="utf-8") as fh:
        for organ, ok, kind, hrs in rows:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(NOW - hrs * 3600)),
                "organ": organ, "ok": ok, "kind": kind, "detail": kind and "detail-" + kind,
            }) + "\n")


def main() -> int:
    # ---- CONTROL 1: SUCCESS. A healthy system must print NOTHING. ----
    # This is the arm that catches a signal which cries wolf, and it is the reason the
    # banner can be trusted to mean something when it does appear.
    fresh()
    write([("propose", True, "", h) for h in (1, 2, 3, 20)])
    assert oh.banner(NOW) == "", "healthy organs must produce NO banner"
    assert oh.rollup(NOW)["worst"] == "ok"

    # A FLAKY but working organ is also healthy: terminal failures mixed with successes
    # above the 50% line must not fire. Otherwise every transient 503 reads as an outage.
    fresh()
    write([("propose", True, "", 1), ("propose", True, "", 2),
           ("propose", True, "", 3), ("propose", False, "transient", 4)])
    assert oh.banner(NOW) == "", "75% success must not fire"

    # ---- CONTROL 2: FAILURE. Each failure mode must be named and actionable. ----
    fresh()
    write([("propose", False, "no_key", h) for h in (1, 5, 10)])
    b = oh.banner(NOW)
    assert "DOWN" in b and "0/3 ok in 24h" in b, b
    assert "NO_KEY" in b and ".gemini_key" in b, "must name the actionable fix"

    # DEGRADED is distinct from DOWN: some calls land, so it is not an outage.
    fresh()
    write([("propose", True, "", 1), ("propose", False, "transient", 2),
           ("propose", False, "transient", 3), ("propose", False, "transient", 4)])
    b = oh.banner(NOW)
    assert "DEGRADED" in b and "DOWN" not in b, b

    # ---- ESCALATION: day 1 and day 14 must NOT render identically. ----
    # This is the precise defect that let a dead system hide for two weeks.
    fresh()
    write([("propose", True, "", 25), ("propose", False, "auth", 1)])
    day1 = oh.banner(NOW)
    fresh()
    write([("propose", True, "", 14 * 24 + 1)] + [("propose", False, "auth", h)
                                                  for h in (1, 5, 10)])
    day14 = oh.banner(NOW)
    assert day1 != day14, "a 1-day outage and a 14-day outage must not read the same"
    assert "14 DAYS" in day14, day14
    assert "DAYS" not in day1, day1

    # ---- SILENT: the organ stopped being CALLED. Nothing failing, nothing trying. ----
    # The other indistinguishability: zero attempts looks exactly like zero work to do.
    fresh()
    write([("propose", True, "", h) for h in (30, 40, 50)])     # ran, but not in 24h
    b = oh.banner(NOW)
    assert "STOPPED BEING CALLED" in b, b
    assert oh.rollup(NOW)["organs"]["propose"]["state"] == "silent"

    # A never-before-seen organ is `idle`, not `silent`: absence of history is not a fault.
    fresh()
    write([("propose", True, "", 1)])
    assert oh.rollup(NOW)["organs"]["propose"]["state"] == "ok"

    # ---- CREDIT EXHAUSTION: the duration discriminator, which is the whole design. ----
    # A rate limit clears in seconds; an exhausted prepaid balance does not. So a BRIEF
    # quota burst must NOT accuse billing, and a SUSTAINED one must.
    fresh()
    write([("rerank", True, "", 3.0)] + [("rerank", False, "quota", h)
                                         for h in (0.1, 0.2, 0.3)])
    b = oh.banner(NOW)
    assert "QUOTA" in b, b
    assert "EXHAUSTED PREPAID BALANCE" not in b, "a fresh quota burst must not accuse billing"
    assert not oh.rollup(NOW)["organs"]["rerank"]["billing_suspect"]

    fresh()
    write([("rerank", True, "", 30.0)] + [("rerank", False, "quota", h)
                                          for h in (1, 4, 8, 20)])
    b = oh.banner(NOW)
    assert "EXHAUSTED PREPAID BALANCE" in b, b
    assert "auto-reload" in b, "must tell the reader what to check"
    assert oh.rollup(NOW)["organs"]["rerank"]["billing_suspect"]

    # ---- UNKNOWN must escalate, never be swallowed as noise. ----
    fresh()
    write([("propose", False, "unknown", h) for h in (1, 2, 3)])
    b = oh.banner(NOW)
    assert "UNCLASSIFIED" in b, b
    assert oh.KINDS.index("unknown") < oh.KINDS.index("transient"), \
        "unknown must outrank transient or it gets filed as background noise"

    # ---- CLASSIFIER, against real exception shapes. ----
    def http(code, body=b"{}"):
        import io
        return urllib.error.HTTPError("u", code, "m", {},  # type: ignore[arg-type]
                                      io.BytesIO(body))
    assert oh.classify(http(429))[0] == "quota"
    assert oh.classify(http(403))[0] == "auth"
    assert oh.classify(http(404))[0] == "model"
    assert oh.classify(http(503))[0] == "transient"
    assert oh.classify(http(400, b'{"error":{"status":"FAILED_PRECONDITION"}}'))[0] == "billing"
    assert oh.classify(RuntimeError("GEMINI_API_KEY not set and x unreadable"))[0] == "no_key"

    # THE REAL SHAPE, captured from the live endpoint on 2026-10-05, and the reason a
    # synthetic-only classifier test is not enough. Google answers an invalid key with
    # HTTP 400 (not 401/403), and an earlier greedy "api key not" pattern on the no_key
    # branch caught "API key not valid" and reported a REJECTED key as a MISSING one -
    # pointing the reader at a key file that was perfectly fine.
    real400 = (b'{"error":{"code":400,"message":"API key not valid. Please pass a valid '
               b'API key.","status":"INVALID_ARGUMENT"}}')
    assert oh.classify(http(400, real400))[0] == "auth", \
        "a rejected key is auth, NOT no_key"
    assert "no_key" != oh.classify(http(400, real400))[0]
    # And the inverse must still hold: a genuinely absent key is no_key, not auth.
    assert oh.classify(RuntimeError("GEMINI_API_KEY not set and /x/.gemini_key unreadable")
                       )[0] == "no_key"
    assert oh.classify(RuntimeError("no candidates in response: {}"))[0] == "bad_response"
    assert oh.classify(TimeoutError("timed out"))[0] == "transient"
    assert oh.classify(ValueError("something nobody anticipated"))[0] == "unknown"

    # ---- CONTROL 3: KILLED. A recorder killed mid-write must not corrupt the log, and ----
    # ---- the rollup must still read every intact row. A truncated last line is the    ----
    # ---- realistic crash shape, and silently dropping the WHOLE file on it would hand ----
    # ---- back a clean "healthy" - a wrong zero that explains itself.                  ----
    d = fresh()
    write([("propose", False, "auth", 1)])
    with open(oh.HEALTH_PATH, "a") as fh:
        fh.write('{"ts": "2026-10-05T12:00:00", "organ": "propo')   # torn write
    b = oh.banner(NOW)
    assert "DOWN" in b, "a torn final line must not hide the intact rows: " + repr(b)
    assert oh.rollup(NOW)["rows"] == 1, "exactly the one intact row"

    # And a real SIGKILL during recording: the process dies, the file stays parseable.
    d = fresh()
    code = (
        "import importlib.util,os,sys,time\n"
        "spec=importlib.util.spec_from_file_location('oh',%r)\n"
        "oh=importlib.util.module_from_spec(spec); spec.loader.exec_module(oh)\n"
        "oh.configure(%r)\n"
        "for i in range(100000): oh.record('killme', False, 'auth', 'x')\n"
        % (os.path.join(HERE, "organ_health.py"), d))
    p = subprocess.Popen([sys.executable, "-c", code])
    time.sleep(0.6)
    p.send_signal(signal.SIGKILL)
    p.wait()
    r = oh.rollup(NOW)
    assert r["rows"] > 0, "the killed writer must have left usable rows"
    assert r["organs"]["killme"]["state"] == "down"
    assert "DOWN" in oh.banner(NOW)

    # record() must survive an unwritable path rather than raising into a hook.
    oh.configure("/nonexistent-dir-%d" % os.getpid())
    oh.record("x", False, "auth", "y")          # must not raise
    assert oh.banner(NOW) == "", "an unreadable log must read as silent, not crash"

    print("OK: 3 controls (success/failure/KILLED incl. real SIGKILL + torn write), "
          "silent when healthy, flaky-but-working does not fire, DOWN vs DEGRADED vs "
          "SILENT distinct, day-1 and day-14 render differently, brief quota does not "
          "accuse billing while sustained quota does, unknown outranks transient, "
          "9 classifier shapes, and record() never raises.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
