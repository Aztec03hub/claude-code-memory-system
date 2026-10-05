#!/usr/bin/env python3
"""Make an LLM-organ outage impossible to miss.

WHY THIS EXISTS. From 2026-09-21 to 2026-10-05 every offline organ in this system was
failing on a missing API key, 340 times, and nobody noticed for two weeks. The SessionStart
banner reported it correctly and every single session read past it. Two separate defects
made that possible, and this module targets both:

  1. IT REPORTED A COUNT, WHICH IS UNINFORMATIVE. "340 errors" cannot distinguish a dead
     system from a busy one. The successes were never recorded, so nobody could see that
     11 of those 14 days ALSO had successful writes and the failures were a subset of
     spawns. A health signal must report a RATE, which needs the denominator.
  2. IT RENDERED IDENTICALLY EVERY SESSION, SO IT BECAME WALLPAPER. A warning that looks
     the same on day 1 and day 14 is furniture. This escalates with consecutive days down
     and prints NOTHING while healthy, so its appearance is itself information.

CREDIT EXHAUSTION, AND THE HONEST LIMIT HERE. The AI Studio prepaid balance cannot be read
programmatically (confirmed 2026-10-05: no API, no BigQuery field, no community tool, and
Google's docs say balance management is UI-only). So this does not watch a balance; it
watches the organs and classifies WHY they fail. The discriminator for an exhausted balance
is DURATION, not an error string: a 429 rate limit clears in seconds, while a zero balance
keeps returning 429 for hours. That is deliberately not a guess about Google's error text,
which is why `quota` sustained past QUOTA_SUSPECT_HOURS is reported as a billing suspect.

An UNRECOGNISED failure is treated as SERIOUS, never ignored. A classifier that silently
drops what it does not understand would rebuild the exact defect it was written to fix.

Used by memory-curator.py (offline organs, via call_gemini) and memory-search.py (the
hot-path reranker, which fails OPEN and is therefore the most dangerous one to lose
silently: retrieval quality drops with no symptom at all).
"""
from __future__ import annotations

import json
import os
import time

HEALTH_PATH = ""        # set by configure(); kept module-level so both callers agree
RETAIN_ROWS = 4000      # ~a month of organ attempts; tailed in place by prune

# Sustained quota errors past this many hours stop looking like a rate limit and start
# looking like an exhausted balance or a hard cap. Chosen because Gemini per-minute limits
# clear in under a minute and per-day quotas reset within 24h; 2h is comfortably past the
# former and well short of the latter.
QUOTA_SUSPECT_HOURS = 2

# Failure classes, worst first. `unknown` sits ABOVE transient on purpose: something we
# cannot classify must not be filed as background noise.
KINDS = ("no_key", "auth", "billing", "quota", "model", "unknown", "bad_response",
         "transient")

ACTIONABLE = {
    "no_key": "The API key is missing or unreadable. Check GEMINI_API_KEY and "
              "~/.claude/.gemini_key (mode 600).",
    "auth": "The API key is PRESENT but REJECTED - revoked, rotated, or restricted. This "
            "is not the same as a missing key: the file is probably fine.",
    "billing": "Billing is refusing the request. Check the AI Studio balance and that "
               "auto-reload is on: console.cloud.google.com Billing > How you pay.",
    "quota": "Quota or rate limit. If this has persisted for hours it is NOT a rate "
             "limit - check the AI Studio prepaid balance and auto-reload.",
    "model": "The model name was rejected. It may have been deprecated or renamed.",
    "unknown": "UNCLASSIFIED failure. Read the detail below and the error log.",
    "bad_response": "The API answered without usable candidates.",
    "transient": "Network or 5xx. Self-clearing unless the rate stays low.",
}


def configure(index_dir: str) -> None:
    global HEALTH_PATH
    HEALTH_PATH = os.path.join(index_dir, "organ-health.jsonl")


def classify(exc: BaseException) -> tuple[str, str]:
    """Map an exception to (kind, short detail). Never raises.

    Classification reads the HTTP status first and the response body second, because the
    status is stable while Google's error prose is not. Anything unrecognised returns
    `unknown`, which the banner escalates rather than hides.
    """
    try:
        name = type(exc).__name__
        msg = str(exc)[:200]
        code = getattr(exc, "code", None)
        body = ""
        reader = getattr(exc, "read", None)   # HTTPError carries the response body
        if callable(reader):
            try:
                raw = reader()
                body = (raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray))
                        else str(raw))[:400]
            except Exception:
                body = ""
        blob = (msg + " " + body).lower()

        # no_key is ONLY our own local "the key is absent" RuntimeError. It must NOT match
        # the server saying "API key not valid", which is a REJECTED key, a different fault
        # with different advice. MEASURED 2026-10-05 against the real endpoint: an invalid
        # key returns HTTP 400 with "API key not valid" in the body, and a pattern of
        # "api key not" here was greedy enough to catch it and report a present-but-rejected
        # key as a missing one - sending the reader to check a file that was fine.
        if "gemini_api_key not set" in blob:
            return "no_key", msg
        # Google answers an invalid key with 400, NOT 401/403. Status alone is not enough
        # here, which is why the body is read; this was the second half of the same bug.
        if (code in (401, 403) or "permission_denied" in blob
                or "api key not valid" in blob or "api_key_invalid" in blob):
            return "auth", (body or msg)[:200]
        if ("failed_precondition" in blob or "billing" in blob
                or "payment" in blob or "plan expired" in blob):
            return "billing", (body or msg)[:200]
        if code == 429 or "resource_exhausted" in blob or "quota" in blob:
            return "quota", (body or msg)[:200]
        if code == 404 or "not found for api version" in blob or "is not supported" in blob:
            return "model", (body or msg)[:200]
        if "no candidates in response" in blob:
            return "bad_response", msg
        if code in (408, 500, 502, 503, 504) or name in (
                "URLError", "TimeoutError", "OSError", "socket.timeout", "timeout",
                "ConnectionResetError", "IncompleteRead"):
            return "transient", "%s: %s" % (name, msg)
        return "unknown", "%s: %s" % (name, msg)
    except Exception:
        return "unknown", "classify() itself failed"


def record(organ: str, ok: bool, kind: str = "", detail: str = "") -> None:
    """Append one terminal status row. NEVER raises, and never blocks meaningfully.

    Called on EVERY exit path of an LLM call, success included, because the successes are
    the denominator that makes the failures interpretable. One short line appended under an
    exclusive lock; the hot-path reranker has a 2.5s budget and this costs microseconds.
    """
    if not HEALTH_PATH:
        return
    try:
        row = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "organ": organ,
                          "ok": bool(ok), "kind": kind, "detail": (detail or "")[:200]},
                         ensure_ascii=False) + "\n"
        try:
            import fcntl
            with open(HEALTH_PATH, "a", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                fh.write(row)
        except ImportError:
            with open(HEALTH_PATH, "a", encoding="utf-8") as fh:
                fh.write(row)
    except Exception:
        pass            # an error in the health recorder must never become the error


def _rows():
    try:
        with open(HEALTH_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    r["_t"] = time.mktime(time.strptime(r["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
                    yield r
                except Exception:
                    continue
    except OSError:
        return


def rollup(now: float | None = None) -> dict:
    """Reduce the log into per-organ health plus one overall verdict.

    `down_hours` measures from the LAST SUCCESS, not from the first failure, because that is
    the quantity that distinguishes a rate limit from an exhausted balance and a blip from
    an outage. An organ with no successes ever is dated from its first attempt.
    """
    now = now if now is not None else time.time()
    rows = list(_rows())
    organs: dict[str, dict] = {}
    for r in rows:
        o = organs.setdefault(r.get("organ") or "?", {
            "att24": 0, "ok24": 0, "att7d": 0, "ok7d": 0,
            "last_ok": None, "first_seen": r["_t"], "kinds24": {}, "last_detail": "",
            "fails": [],
        })
        o["first_seen"] = min(o["first_seen"], r["_t"])
        if r.get("ok"):
            o["last_ok"] = max(o["last_ok"] or 0, r["_t"])
        else:
            o["fails"].append((r["_t"], r.get("kind") or "unknown"))
        age = now - r["_t"]
        if age <= 86400:
            o["att24"] += 1
            if r.get("ok"):
                o["ok24"] += 1
            else:
                k = r.get("kind") or "unknown"
                o["kinds24"][k] = o["kinds24"].get(k, 0) + 1
                o["last_detail"] = r.get("detail") or o["last_detail"]
        if age <= 7 * 86400:
            o["att7d"] += 1
            if r.get("ok"):
                o["ok7d"] += 1

    out = {"checked": time.strftime("%Y-%m-%dT%H:%M:%S"), "organs": {}, "worst": "ok",
           "rows": len(rows)}
    for name, o in organs.items():
        since = o["last_ok"] if o["last_ok"] else o["first_seen"]
        down_h = (now - since) / 3600.0
        # SILENT is its own state and is NOT ok: the organ stopped being invoked at all.
        # A system that never tries looks exactly like a system with nothing to do, which
        # is the same indistinguishability this whole module exists to break.
        if o["att24"] == 0:
            state = "silent" if o["att7d"] else "idle"
        elif o["ok24"] == 0:
            state = "down"
        elif o["ok24"] / float(o["att24"]) < 0.5 and o["att24"] >= 4:
            state = "degraded"
        else:
            state = "ok"
        worst_kind = ""
        if o["kinds24"]:
            worst_kind = sorted(o["kinds24"].items(),
                                key=lambda kv: (KINDS.index(kv[0]) if kv[0] in KINDS
                                                else len(KINDS), -kv[1]))[0][0]
        # STREAK duration, not time-since-success, and the distinction is the whole credit
        # discriminator. A success 3h ago followed by a 20-minute quota burst is a FRESH
        # rate limit; measuring from the last success called it sustained and accused
        # billing. Caught by the test's "brief quota must not accuse billing" control.
        streak = [t for t, _k in o["fails"] if not o["last_ok"] or t > o["last_ok"]]
        streak_h = (now - min(streak)) / 3600.0 if streak else 0.0
        out["organs"][name] = {
            "state": state, "att24": o["att24"], "ok24": o["ok24"],
            "att7d": o["att7d"], "ok7d": o["ok7d"],
            "down_hours": round(down_h, 1), "streak_hours": round(streak_h, 1),
            "kind": worst_kind,
            "kinds24": o["kinds24"], "detail": o["last_detail"],
            "billing_suspect": bool(worst_kind in ("quota", "billing")
                                    and streak_h >= QUOTA_SUSPECT_HOURS),
        }
    rank = {"down": 3, "silent": 2, "degraded": 1, "ok": 0, "idle": 0}
    if out["organs"]:
        out["worst"] = max(out["organs"].values(), key=lambda v: rank[v["state"]])["state"]
    return out


def banner(now: float | None = None) -> str:
    """Render the signal, or "" when there is nothing wrong.

    Returning "" on a healthy system is the point. A block that prints every session is one
    nobody reads, so this one's mere presence has to mean something.
    """
    r = rollup(now)
    bad = {n: v for n, v in r["organs"].items()
           if v["state"] in ("down", "silent", "degraded")}
    if not bad:
        return ""
    worst = max(bad.values(), key=lambda v: {"down": 3, "silent": 2, "degraded": 1}[v["state"]])
    days = worst["down_hours"] / 24.0
    # ESCALATION. "down 3h" and "down 14 DAYS" must not render the same way; the key bug
    # survived two weeks precisely because they did.
    if worst["state"] == "down" and days >= 1:
        head = ("[memory] LLM ORGANS HAVE BEEN DOWN FOR %.0f DAY%s. "
                "Nothing has been proposed, judged or reranked in that time."
                % (days, "S" if days >= 2 else ""))
    elif worst["state"] == "down":
        head = "[memory] LLM organs are DOWN (no successful call in the last 24h)."
    elif worst["state"] == "silent":
        head = ("[memory] LLM organs have STOPPED BEING CALLED (ran within 7 days, "
                "zero attempts in 24h). Nothing is failing because nothing is trying.")
    else:
        head = "[memory] LLM organs are DEGRADED."
    lines = [head]
    for name, v in sorted(bad.items(), key=lambda kv: -kv[1]["down_hours"]):
        lines.append("  %-18s %-8s %d/%d ok in 24h, %d/%d in 7d%s"
                     % (name, v["state"].upper(), v["ok24"], v["att24"],
                        v["ok7d"], v["att7d"],
                        ", last ok %.0fh ago" % v["down_hours"] if v["ok24"] == 0 else ""))
        if v["kind"]:
            lines.append("      %s  %s" % (v["kind"].upper(), ACTIONABLE.get(v["kind"], "")))
        if v["detail"]:
            lines.append("      detail: %s" % v["detail"][:160])
    if any(v["billing_suspect"] for v in bad.values()):
        lines.append("  >> QUOTA ERRORS SUSTAINED PAST %dh. A rate limit clears in seconds, "
                     "so this is more likely an EXHAUSTED PREPAID BALANCE or a hard cap."
                     % QUOTA_SUSPECT_HOURS)
        lines.append("     The balance is UI-only: console.cloud.google.com > Billing > "
                     "How you pay. Confirm auto-reload is still on.")
    lines.append("  health log: %s" % HEALTH_PATH)
    return "\n".join(lines)


def tail(retain: int = RETAIN_ROWS) -> int:
    """Keep the newest `retain` rows. Safe to tail: each row is independent, unlike the
    event-sourced logs here whose reduction a byte cut would corrupt."""
    try:
        with open(HEALTH_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    if len(lines) <= retain:
        return 0
    tmp = "%s.tmp.%d" % (HEALTH_PATH, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(lines[-retain:])
    os.replace(tmp, HEALTH_PATH)
    return len(lines) - retain
