"""Tests for tools/desk_decide.py - the numbers plus the news -> the desk's passes. Offline, no subprocess
beyond the tool itself: it reads a hand-written desk_check.py output and news files."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, TempHomeTestCase, run_cli, tc  # noqa: E402

TOOL = ROOT / "tools" / "desk_decide.py"


def idea(sym, passes, best="atr15", plan_file=None, reasons=None):
    return {"symbol": sym, "side": "long", "setup": "orb", "level": None, "market": "us", "live": passes,
            "trigger": {"status": "TRIGGERED", "triggered_at": "09:55", "minutes_since": 8},
            "entry_used": 101.9, "best": best, "passes_numbers": passes, "reasons": reasons or [],
            "stops": [{"name": "atr15", "stop": 100.4, "target": 106.4, "expected_r": 0.31 if passes else -0.2,
                       "target_first_pct": 34.0, "stop_first_pct": 60.0, "open_at_close_pct": 6.0,
                       "verdict": "CLOSE TO A COIN FLIP", "shares": 160, "plan_file": plan_file}]}


class DecideTests(TempHomeTestCase):

    def setUp(self):
        super().setUp()
        tc.save_settings({"daily_max_loss": 1000})
        self.plans = self.home / "throwaway" / "plans"
        self.plans.mkdir(parents=True)
        (self.plans / "AAA.json").write_text(json.dumps({"symbol": "AAA", "entry": 101.9, "stop": 100.4}))
        (self.plans / "BBB.json").write_text(json.dumps({"symbol": "BBB", "entry": 101.9, "stop": 100.4}))
        (self.plans / "CCC.json").write_text(json.dumps({"symbol": "CCC", "entry": 101.9, "stop": 100.4}))
        self.check = self.home / "check.json"
        self.check.write_text(json.dumps({"ideas": [
            idea("AAA", True, plan_file=str(self.plans / "AAA.json")),
            idea("BBB", True, plan_file=str(self.plans / "BBB.json")),
            idea("CCC", True, plan_file=str(self.plans / "CCC.json")),
            idea("DDD", False, reasons=["not live (trigger 22 min old)"]),
        ]}))
        self.news0 = self.home / "news-0.json"
        self.news0.write_text(json.dumps({"date": "2026-11-02", "group": 0, "symbols": [
            {"symbol": "AAA", "red_flag": False, "red_flag_reason": "none", "catalyst": "earnings beat Friday", "sources": ["https://x/1"]},
            {"symbol": "bbb", "red_flag": True, "red_flag_reason": "guidance cut Oct 30", "sources": ["https://x/2", "https://x/3"]},
        ]}))
        self.passes = self.home / "passes.json"
        self.cands = self.home / "candidates.json"

    def run_tool(self, *args, expect=0):
        r = run_cli(TOOL, "--check", str(self.check), *args)
        self.assertEqual(r.returncode, expect, r.stderr + r.stdout)
        return r

    def run_json(self, *args, expect=0):
        out = json.loads(self.run_tool(*args, "--json", expect=expect).stdout)
        self.assertEqual((out["mode"], out["real_money"]), ("paper", False))
        return out

    def test_pass_skip_hold(self):
        out = self.run_json("--news", str(self.news0), "--desk", str(self.home), "--passes", str(self.passes),
                            "--candidates", str(self.cands))
        self.assertEqual((out["passes"], out["skips"], out["holds"]), (["AAA"], ["BBB", "DDD"], ["CCC"]))
        by = {d["symbol"]: d for d in out["decisions"]}
        self.assertEqual(by["AAA"]["copied_to"], str(self.home / "plans" / "AAA.json"))
        self.assertTrue((self.home / "plans" / "AAA.json").is_file())
        self.assertFalse((self.home / "plans" / "BBB.json").exists())     # a red flag copies nothing
        self.assertFalse((self.home / "plans" / "CCC.json").exists())     # a hold copies nothing
        self.assertIn("red flag: guidance cut Oct 30 (https://x/2, https://x/3)", by["BBB"]["reason"])
        self.assertIn("no news row", by["CCC"]["reason"])
        self.assertIn("--mark CCC=ok", by["CCC"]["reason"])
        self.assertEqual(by["DDD"]["reason"], "not live (trigger 22 min old)")
        self.assertIn("best stop atr15: +0.31R expected; news: no red flag (earnings beat Friday)", by["AAA"]["reason"])
        self.assertEqual(json.loads(self.passes.read_text()), [{"symbol": "AAA", "side": "long", "setup": "orb", "note": by["AAA"]["reason"]}])
        rows = {r["symbol"]: r for r in json.loads(self.cands.read_text())}
        self.assertEqual({s: rows[s]["decision"] for s in rows}, {"AAA": "pass", "BBB": "skip", "CCC": "pending news", "DDD": "skip"})
        self.assertEqual((rows["AAA"]["entry"], rows["AAA"]["ev_after_fees"], rows["AAA"]["verdict"]), (101.9, 0.31, "CLOSE TO A COIN FLIP"))
        self.assertEqual(out["news_files"], [{"file": str(self.news0), "symbols": 2}])
        self.assertEqual(out["news_problems"], [])

    def test_marks_resolve_holds_and_override_nothing_else(self):
        out = self.run_json("--news", str(self.news0), "--mark", "CCC=ok", "--mark", "ddd=flag:halted at 09:40",
                            "--desk", str(self.home))
        self.assertEqual((out["passes"], out["holds"]), (["AAA", "CCC"], []))
        by = {d["symbol"]: d for d in out["decisions"]}
        self.assertEqual(by["CCC"]["news"]["marked_by"], "the desk")
        self.assertEqual(by["DDD"]["decision"], "skip")              # the numbers failed first; the mark is moot
        out = self.run_json("--news", str(self.news0), "--mark", "CCC=flag:offering priced overnight")
        self.assertEqual(out["skips"], ["BBB", "CCC", "DDD"])
        self.assertIn("red flag: offering priced overnight", {d["symbol"]: d for d in out["decisions"]}["CCC"]["reason"])

    def test_missing_news_file_is_reported_not_fatal(self):
        out = self.run_json("--news", str(self.home / "nope-*.json"), str(self.news0))
        self.assertEqual(len(out["news_problems"]), 1)
        self.assertIn("nope-*.json", out["news_problems"][0])
        self.assertEqual(out["passes"], ["AAA"])
        out = self.run_json()                                         # no news at all: every numbers pass holds
        self.assertEqual((out["passes"], out["holds"]), ([], ["AAA", "BBB", "CCC"]))

    def test_text_output(self):
        r = self.run_tool("--news", str(self.news0), "--passes", str(self.passes))
        self.assertTrue(r.stdout.startswith("DESK DECIDE · 4 idea(s) · pass 1 · skip 2 · hold 1"), r.stdout)
        self.assertIn("pass AAA", r.stdout)
        self.assertIn(f"--ideas {self.passes}", r.stdout)
        self.assertIn("Holds need the desk's own news read", r.stdout)
        self.assertIn("Not financial advice", r.stdout)

    def test_bad_input(self):
        r = run_cli(TOOL, "--check", str(self.home / "missing.json"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("check file", r.stderr)
        r = self.run_tool("--mark", "CCC=maybe", expect=1)
        self.assertIn("SYM=ok or SYM=flag:why", r.stderr)
        r = self.run_tool("--desk", str(self.home / "nothome"), expect=1)
        self.assertIn("holds no settings.json", r.stderr)
        (self.home / "bad.json").write_text(json.dumps({"ideas": "no"}))
        r = run_cli(TOOL, "--check", str(self.home / "bad.json"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("no ideas list", r.stderr)


if __name__ == "__main__":
    unittest.main()
