"""Tests for tools/desk_check.py - the runbook's three-stop check of a batch of ideas. Offline.

The tool runs entry.py, exit_plan.py, time_machine.py and risk.py as real subprocesses in throwaway
homes, on Yahoo fixtures in $DAYTRADER_FIXTURES with $DAYTRADER_NOW pinned to 10:05 ET on a Friday
whose opening range broke at 09:55 (the entry skill's own ORB_ROWS). The scanner call is switched
off with --min-dollar-volume 0 (liquidity comes from the sweep's own ranking in the jobs).
"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (ROOT, TempHomeTestCase, et_ts, random_walk_history, run_cli, tc, write_fixture,  # noqa: E402
                     yahoo_chart_url, yahoo_payload)

TOOL = ROOT / "tools" / "desk_check.py"
_spec = importlib.util.spec_from_file_location("desk_check", TOOL)
dc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dc)

DAY, PREV = "2026-10-30", "2026-10-29"      # Friday, Thursday
ORB_ROWS = [                                 # the entry skill's hand-checked opening-range break
    (100.0, 101.0, 99.0, 100.5),
    (100.5, 101.5, 99.5, 101.0),
    (101.0, 101.2, 100.0, 100.8),
    (100.8, 101.4, 100.6, 101.3),            # 09:45 - inside the range
    (101.3, 102.0, 101.2, 101.9),            # 09:50 - close 101.9 > 101.5 -> trigger, closes 09:55
    (101.9, 102.5, 101.5, 102.2),
]


def mk(day, rows, start="09:30", volume=100_000):
    t0 = et_ts(day, start)
    return [tc.Bar(t0 + i * 300, o, h, l, c, volume) for i, (o, h, l, c) in enumerate(rows)]


class DeskCheckTestCase(TempHomeTestCase):
    """A desk in the temp $DAYTRADER_HOME and TSLA fixtures: 20 past sessions plus today's break."""

    NOW = f"{DAY}T10:05"

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fx"
        tc.save_settings({"daily_max_loss": 1000, "max_open_risk": 1000})
        history = random_walk_history(PREV, 20, start_price=100.0, seed=7)
        today = mk(DAY, ORB_ROWS)
        write_fixture(self.fx, yahoo_chart_url("TSLA", "5m", "5d"), yahoo_payload(history[-2 * 78:] + today, "TSLA"))
        write_fixture(self.fx, yahoo_chart_url("TSLA", "5m", "60d"), yahoo_payload(history + today, "TSLA"))
        self.out = self.home / "homes"
        self.env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(self.fx), "DAYTRADER_NOW": self.NOW}

    def tool(self, *args, expect=0, env=None):
        e = dict(self.env)
        e.update(env or {})
        r = run_cli(TOOL, "--home", str(self.home), "--out", str(self.out), *args, env=e)
        self.assertEqual(r.returncode, expect, r.stderr + r.stdout)
        return r

    def run_json(self, *args, expect=0, env=None):
        out = json.loads(self.tool(*args, "--json", expect=expect, env=env).stdout)
        self.assertEqual((out["mode"], out["real_money"], out["news_checked"]), ("paper", False, False))
        return out

    def ideas(self, rows):
        p = self.home / "ideas.json"
        p.write_text(json.dumps(rows), encoding="utf-8")
        return str(p)


class LiveTriggerTests(DeskCheckTestCase):

    def test_three_stops_from_a_live_trigger(self):
        cands = self.home / "candidates.json"
        out = self.run_json("TSLA", "--setup", "orb", "--min-dollar-volume", "0", "--candidates", str(cands))
        self.assertEqual((out["rr"], out["min_r"], out["live_minutes"], out["stops"]), (3.0, 0.2, 15.0, ["atr15", "struct", "atr4"]))
        [r] = out["ideas"]
        self.assertEqual((r["symbol"], r["side"], r["setup"], r["market"]), ("TSLA", "long", "orb", "us"))
        self.assertTrue(r["live"], r)
        t = r["trigger"]
        self.assertEqual((t["status"], t["triggered_at"], t["entry"], t["trigger_bar"]["start"]), ("TRIGGERED", "09:55", 101.9, "09:50"))
        self.assertEqual(t["minutes_since"], 10)
        self.assertEqual((r["entry_used"], r["entry_source"]), (101.9, "the trigger bar's close (09:55)"))
        self.assertEqual([s["name"] for s in r["stops"]], ["atr15", "struct", "atr4"])
        for s in r["stops"]:
            self.assertNotIn("error", s, s)
            self.assertLess(s["stop"], 101.9)
            self.assertAlmostEqual(s["target"], 101.9 + 3 * (101.9 - s["stop"]), places=2)
            self.assertIsInstance(s["expected_r"], float)
            self.assertIn(s["verdict"], ("ODDS LEAN AGAINST YOU", "CLOSE TO A COIN FLIP", "ODDS FAVOR YOU"))
            self.assertGreaterEqual(s["target_first_pct"] + s["stop_first_pct"] + s["open_at_close_pct"], 99.0)
            self.assertIsInstance(s["shares"], int)
            plan = json.loads(Path(s["plan_file"]).read_text(encoding="utf-8"))
            self.assertEqual(Path(s["plan_file"]).name, "TSLA.json")
            self.assertEqual(Path(s["home"]).name, f"TSLA-{s['name']}")
            for section in ("entry_signal", "exit_plan", "time_machine", "sizing"):
                self.assertIn(section, plan, (s["name"], sorted(plan)))
            self.assertEqual((plan["entry"], plan["stop"], plan["target"]), (101.9, s["stop"], s["target"]))
        # the desk itself holds no plan: everything happened in the throwaway homes
        self.assertEqual(list((self.home / "plans").glob("*.json")), [])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [".cache", "TSLA-atr15", "TSLA-atr4", "TSLA-struct"])
        # the best stop and the verdict follow the numbers
        best = max(r["stops"], key=lambda s: s["expected_r"])
        self.assertEqual(r["best"], best["name"])
        expect = best["expected_r"] >= 0.2 and best["verdict"] != "ODDS LEAN AGAINST YOU" and best["shares"] > 0
        self.assertEqual(r["passes_numbers"], expect, r["reasons"])
        self.assertEqual(out["passed_numbers"], ["TSLA"] if expect else [])
        [row] = json.loads(cands.read_text(encoding="utf-8"))
        self.assertEqual((row["symbol"], row["entry"], row["stop"], row["ev_after_fees"], row["verdict"]),
                         ("TSLA", 101.9, best["stop"], best["expected_r"], best["verdict"]))
        self.assertEqual(row["decision"], dc.PASS if expect else dc.SKIP)
        self.assertEqual(row["setup"], "orb, 09:55 ET")
        self.assertEqual(r["liquidity"], {"dollar_volume_20d": None, "liquid": None, "trend": None,
                                          "reason": "not checked (--min-dollar-volume 0)"})

    def test_candidates_file_merges_by_symbol(self):
        cands = self.home / "candidates.json"
        cands.write_text(json.dumps([{"symbol": "TSLA", "decision": "old"}, {"symbol": "NVDA", "decision": "keep"}]))
        self.run_json("TSLA", "--setup", "orb", "--min-dollar-volume", "0", "--candidates", str(cands))
        rows = json.loads(cands.read_text(encoding="utf-8"))
        self.assertEqual([r["symbol"] for r in rows], ["NVDA", "TSLA"])
        self.assertNotEqual(rows[1]["decision"], "old")

    def test_text_table(self):
        r = self.tool("TSLA", "--setup", "orb", "--min-dollar-volume", "0")
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].startswith("DESK CHECK · 1 idea(s) ·"), lines[0])
        self.assertIn("paper only, no orders", lines[0])
        self.assertIn("TRIGGERED 09:55 (10m)", r.stdout)
        self.assertTrue(any(ln.strip().startswith(("atr15", "struct", "atr4")) for ln in lines[3:]), r.stdout)
        self.assertRegex(r.stdout, r"Numbers pass: (TSLA|none)\.")
        self.assertIn("Not financial advice", r.stdout)


class WatchAndSkipTests(DeskCheckTestCase):

    def test_a_stale_trigger_is_a_watch(self):
        out = self.run_json("TSLA", "--setup", "orb", "--min-dollar-volume", "0", env={"DAYTRADER_NOW": f"{DAY}T10:25"})
        [r] = out["ideas"]
        self.assertEqual((r["trigger"]["status"], r["trigger"]["minutes_since"]), ("TRIGGERED", 30))
        self.assertFalse(r["live"])
        self.assertFalse(r["passes_numbers"])
        self.assertTrue(any(x.startswith("not live (trigger 30 min old)") for x in r["reasons"]), r["reasons"])
        self.assertEqual(len(r["stops"]), 3)           # the odds are still recorded for the candidate row

    def test_an_unreadable_brief_leaves_liquidity_unknown(self):
        out = self.run_json("TSLA", "--setup", "orb")          # the default floor: analyst.py brief, no daily fixture
        [r] = out["ideas"]
        self.assertIsNone(r["liquidity"]["liquid"])
        self.assertIn("liquidity unknown", r["liquidity"]["reason"])
        self.assertEqual(len(r["stops"]), 3)                   # unknown liquidity is not a fail
        self.assertFalse(any("liquid" in x for x in r["reasons"]), r["reasons"])

    def test_a_wider_live_window_counts_it(self):
        out = self.run_json("TSLA", "--setup", "orb", "--min-dollar-volume", "0", "--live-minutes", "45",
                            env={"DAYTRADER_NOW": f"{DAY}T10:25"})
        self.assertTrue(out["ideas"][0]["live"])

    def test_no_trigger_replays_the_ideas_entry(self):
        out = self.run_json("TSLA", "--setup", "orb", "--min-dollar-volume", "0", "--entry", "101.3",
                            env={"DAYTRADER_NOW": f"{DAY}T09:52"})      # the 09:50 bar is still forming
        [r] = out["ideas"]
        self.assertEqual(r["trigger"]["status"], "WAITING")
        self.assertFalse(r["live"])
        self.assertEqual((r["entry_used"], r["entry_source"]), (101.3, "the idea's entry (no trigger yet)"))
        self.assertEqual(len(r["stops"]), 3)
        self.assertIn("not live", r["reasons"][0])

    def test_no_trigger_and_no_entry_stops_early(self):
        out = self.run_json("TSLA", "--setup", "orb", "--min-dollar-volume", "0", env={"DAYTRADER_NOW": f"{DAY}T09:52"})
        [r] = out["ideas"]
        self.assertEqual(r["stops"], [])
        self.assertEqual(r["reasons"], ["no entry price: entry.py says WAITING and the idea gives none"])
        self.assertIsNone(r["best"])

    def test_crypto_short_is_skipped_without_a_call(self):
        out = self.run_json("--ideas", self.ideas([{"symbol": "btc-usdt", "setup": "pdh", "side": "short"}]),
                            "--min-dollar-volume", "0")
        [r] = out["ideas"]
        self.assertEqual((r["symbol"], r["market"], r["stops"], r["reasons"]), ("BTC-USDT", "crypto", [], [dc.auto.SPOT_LONG_ONLY]))
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))

    def test_a_batch_keeps_going_after_a_data_error(self):
        for workers in ("3", "1"):
            out = self.run_json("--ideas", self.ideas([{"symbol": "NVDA", "setup": "orb"}, {"symbol": "TSLA", "setup": "orb"}]),
                                "--min-dollar-volume", "0", "--workers", workers)
            nvda, tsla = out["ideas"]                      # the batch keeps its order whatever the pool does
            self.assertEqual((nvda["symbol"], tsla["symbol"], out["workers"]), ("NVDA", "TSLA", int(workers)))
            self.assertTrue(nvda["data_error"], nvda)      # no fixture: the entry skill reports its data source
            self.assertEqual(nvda["stops"], [])
            self.assertTrue(tsla["live"])
            self.assertEqual(len(tsla["stops"]), 3)
        # the three homes share one download cache
        caches = {os.path.realpath(self.out / f"TSLA-{s}" / "cache") for s in ("atr15", "struct", "atr4")}
        self.assertEqual(caches, {os.path.realpath(self.out / ".cache")})

    def test_every_idea_down_exits_2(self):
        self.run_json("--ideas", self.ideas([{"symbol": "NVDA", "setup": "orb"}]), "--min-dollar-volume", "0", expect=2)


class InputTests(DeskCheckTestCase):

    def err(self, *args, env=None):
        r = self.tool(*args, expect=1, env=env)
        self.assertTrue(r.stderr.startswith("error:"), r.stderr)
        return r.stderr

    def test_bad_input(self):
        self.assertIn("give a SYMBOL", self.err("--min-dollar-volume", "0"))
        self.assertIn("needs a setup", self.err("TSLA"))
        self.assertIn("needs a level", self.err("TSLA", "--setup", "level"))
        self.assertIn("not both", self.err("TSLA", "--setup", "orb", "--ideas", self.ideas([{"symbol": "TSLA", "setup": "orb"}])))
        self.assertIn("non-empty JSON list", self.err("--ideas", self.ideas([])))
        self.assertIn("side must be long or short", self.err("--ideas", self.ideas([{"symbol": "TSLA", "setup": "orb", "side": "flat"}])))
        self.assertIn("positive price", self.err("--ideas", self.ideas([{"symbol": "TSLA", "setup": "orb", "entry": -1}])))
        self.assertIn("--stops", self.err("TSLA", "--setup", "orb", "--stops", "atr15,wide"))
        self.assertIn("must be positive", self.err("TSLA", "--setup", "orb", "--rr", "0"))
        self.assertIn("--workers", self.err("TSLA", "--setup", "orb", "--workers", "0"))

    def test_home_must_be_a_desk(self):
        empty = self.home / "nothome"
        empty.mkdir()
        r = run_cli(TOOL, "--home", str(empty), "--out", str(self.out), "TSLA", "--setup", "orb", env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("holds no settings.json", r.stderr)


if __name__ == "__main__":
    unittest.main()
