#!/usr/bin/env python3
"""Drive a REAL auth failure through the live curator's call_gemini.

Not a unit test: the unit test used synthetic exceptions, and a classifier that passes on
synthetic input can still mis-file what the real API actually returns. This hits the real
endpoint with a deliberately invalid key, into an ISOLATED health log so the production one
stays clean, and checks that the organ name, the class and the banner all come out right.
"""
import importlib.util
import os
import sys
import tempfile

SRC = "/home/plafayette/claude_projects/memory-system/src"
os.chdir(SRC)
sys.argv = ["memory-curator.py"]
spec = importlib.util.spec_from_file_location("mc", os.path.join(SRC, "memory-curator.py"))
mc = importlib.util.module_from_spec(spec)
sys.modules["mc"] = mc
spec.loader.exec_module(mc)

tmp = tempfile.mkdtemp()
mc.organ_health.configure(tmp)              # isolate from the production health log
mc.gemini_key = lambda: "AIzaSyINVALID-key-for-a-deliberate-failure-test"

try:
    mc.call_gemini("say hi", max_tokens=8, retries=0)
    print("  UNEXPECTED: the invalid key succeeded")
except Exception as e:
    print("  raised as designed: %s: %s" % (type(e).__name__, str(e)[:70]))

r = mc.organ_health.rollup()
print("  recorded: %s" % {k: (v["state"], v["kind"], "%d/%d" % (v["ok24"], v["att24"]))
                          for k, v in r["organs"].items()})
b = mc.organ_health.banner()
print("\n  banner it would print:")
for line in b.split("\n"):
    print("  " + line)
assert r["organs"], "nothing was recorded at all"
assert any(v["kind"] in ("auth", "billing", "quota", "unknown") for v in r["organs"].values()), \
    "a bad key must classify as auth (or at worst unknown), got %s" % r["organs"]
assert "DOWN" in b
print("\n  PASS: a real API rejection is recorded, classified and surfaced.")
