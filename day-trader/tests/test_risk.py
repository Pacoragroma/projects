"""Tests for skills/risk/scripts/risk.py (offline, synthetic data only)."""
import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

# Works both with `unittest discover -s tests` and `python3 -m unittest tests.test_risk`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (TempHomeTestCase, bars_from_closes, et_ts, fake_bars, load_skill_module,  # noqa: E402
                     run_cli, skill_script, tc, write_fixture, yahoo_chart_url, yahoo_payload)

risk = load_skill_module("risk", "risk")
SCRIPT = skill_script("risk", "risk.py")
SETTINGS = dict(tc.DEFAULT_SETTINGS)


def run_main(*argv):
    """Call risk.main in-process; return (exit code, stdout)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = risk.main(list(argv))
    return code, buf.getvalue()


def trade(tid, symbol="NVDA", status="closed", pnl=None, qty=50, entry=100.0, stop=99.0, side="long",
          opened="2026-10-30T09:45:00-04:00", closed="2026-10-30T09:50:00-04:00"):
    t = {"id": tid, "symbol": symbol, "side": side, "qty": qty, "entry_price": entry, "stop": stop,
         "target": 102.0, "opened_at": opened, "status": status}
    if status == "closed":
        t.update(pnl=pnl, closed_at=closed)
    return t


class SizePositionTests(unittest.TestCase):
    def test_video_example_buys_8(self):
        sz = risk.size_position("long", 364.18, 358.32, 370.04, SETTINGS)
        self.assertEqual(sz["shares"], 8)
        self.assertEqual(sz["action"], "BUY")
        self.assertEqual(sz["capped_by"], "risk")
        self.assertAlmostEqual(sz["risk_per_share"], 5.86)
        self.assertAlmostEqual(sz["risk_used"], 46.88)
        self.assertAlmostEqual(sz["notional"], 2913.44)
        self.assertAlmostEqual(sz["risk_budget"], 50.0)
        self.assertAlmostEqual(sz["position_pct"], 11.65)
        self.assertAlmostEqual(sz["rr"], 1.0, places=4)
        self.assertAlmostEqual(sz["breakeven_win_rate_pct"], 50.0, places=2)
        self.assertAlmostEqual(sz["reward_at_target"], 46.88)
        self.assertEqual(sz["limits"], {"risk": 8, "max_position_pct": 17, "buying_power": 68})

    def test_short_and_risk_dollars(self):
        sz = risk.size_position("short", 364.18, 370.04, 352.46, SETTINGS, risk_dollars=100)
        self.assertEqual(sz["action"], "SELL SHORT")
        self.assertEqual(sz["shares"], 17)
        self.assertAlmostEqual(sz["rr"], 2.0, places=4)
        self.assertIsNone(sz["risk_pct"])
        self.assertEqual(sz["risk_budget_source"], "risk_dollars")

    def test_account_and_risk_pct_override(self):
        sz = risk.size_position("long", 50.0, 49.0, None, SETTINGS, account=10000, risk_pct=0.5)
        self.assertEqual(sz["shares"], 50)       # $50 / $1
        self.assertIsNone(sz["rr"])
        self.assertEqual(sz["account"], 10000)

    def test_exact_multiple_does_not_lose_a_share_to_float_noise(self):
        # 58.6 / (364.18 - 358.32) is 9.99999999999998 in floats
        sz = risk.size_position("long", 364.18, 358.32, None, SETTINGS, risk_dollars=58.6)
        self.assertEqual(sz["shares"], 10)
        self.assertLessEqual(sz["risk_used"], 58.6)

    def test_max_position_cap(self):
        sz = risk.size_position("long", 100.0, 99.9, None, SETTINGS, risk_dollars=1000)
        self.assertEqual(sz["shares"], 62)       # 25% of 25k = 6,250 -> 62 shares
        self.assertEqual(sz["capped_by"], "max_position_pct")
        self.assertEqual(sz["limits"]["risk"], 10000)

    def test_buying_power_cap_and_leverage(self):
        one = risk.size_position("long", 100.0, 99.0, None, SETTINGS, risk_dollars=1000, max_position_pct=200)
        self.assertEqual((one["shares"], one["capped_by"]), (250, "buying_power"))
        self.assertFalse(one["leverage_given"])
        two = risk.size_position("long", 100.0, 99.0, None, SETTINGS, risk_dollars=1000, max_position_pct=200,
                                 leverage=2)
        self.assertEqual((two["shares"], two["capped_by"]), (500, "max_position_pct"))
        four = risk.size_position("long", 100.0, 99.0, None, SETTINGS, risk_dollars=1000, max_position_pct=400,
                                  leverage=4)
        self.assertEqual((four["shares"], four["capped_by"]), (1000, "risk"))
        self.assertAlmostEqual(four["notional"], 100000.0)

    def test_zero_shares(self):
        sz = risk.size_position("long", 500.0, 440.0, None, SETTINGS, account=10000)
        self.assertEqual(sz["shares"], 0)
        self.assertEqual(sz["risk_used"], 0)
        msg = risk.zero_shares_reason(sz)
        self.assertIn("$60.00", msg)
        self.assertIn("$20.00", msg)
        pricey = risk.size_position("long", 5000.0, 4990.0, None, SETTINGS, account=10000)
        self.assertEqual(pricey["shares"], 0)
        self.assertEqual(pricey["capped_by"], "max_position_pct")
        self.assertIn("--max-position-pct 50", risk.zero_shares_reason(pricey))

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            risk.size_position("long", 100.0, 100.0, None, SETTINGS)
        with self.assertRaises(ValueError):
            risk.size_position("long", 100.0, 101.0, None, SETTINGS)      # long stop above entry
        with self.assertRaises(ValueError):
            risk.size_position("long", 100.0, 99.0, None, dict(SETTINGS, account_size=0))
        with self.assertRaises(ValueError):
            risk.size_position("long", 100.0, 99.0, None, SETTINGS, leverage=3)
        with self.assertRaises(ValueError):
            risk.size_position("long", 100.0, 99.0, None, dict(SETTINGS, risk_per_trade_pct=0))
        with self.assertRaises(ValueError):   # tc.position_size alone would return -9 shares
            risk.size_position("long", 364.18, 358.32, None, SETTINGS, risk_dollars=-50)

    def test_sub_dollar_stock(self):
        sz = risk.size_position("long", 0.85, 0.80, None, SETTINGS)
        self.assertEqual(sz["shares"], 1000)    # $50 / $0.05
        self.assertAlmostEqual(sz["notional"], 850.0)


class ResolveLevelsTests(unittest.TestCase):
    PLAN = {"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04}

    def test_flags_only_infers_side(self):
        lv = risk.resolve_levels(None, 364.18, 370.04)
        self.assertEqual(lv["side"], "short")
        self.assertEqual(lv["sources"]["side"], "inferred")
        self.assertIsNone(lv["target"])
        with self.assertRaises(ValueError):
            risk.resolve_levels(None, 10.0, 10.0)
        with self.assertRaises(ValueError):
            risk.resolve_levels(None, None, 9.0)

    def test_all_from_plan(self):
        lv = risk.resolve_levels(self.PLAN)
        self.assertEqual((lv["side"], lv["entry"], lv["stop"], lv["target"]), ("long", 364.18, 358.32, 370.04))
        self.assertEqual(set(lv["sources"].values()), {"plan"})
        self.assertFalse(risk.levels_changed(self.PLAN, lv))

    def test_new_stop_keeps_target_new_entry_drops_it(self):
        lv = risk.resolve_levels(self.PLAN, stop=357.0)
        self.assertEqual(lv["target"], 370.04)
        self.assertTrue(risk.levels_changed(self.PLAN, lv))
        lv = risk.resolve_levels(self.PLAN, entry=365.0)
        self.assertIsNone(lv["target"])
        self.assertTrue(any("saved target" in n for n in lv["notes"]))

    def test_plan_side_prevents_silent_flip(self):
        # entry typed below the plan's long stop must not become a short
        with self.assertRaises(ValueError) as cm:
            risk.resolve_levels(self.PLAN, entry=357.0)
        self.assertIn("long", str(cm.exception))
        self.assertIn("saved plan", str(cm.exception))

    def test_side_flag_conflict_and_flag_levels_override_plan_side(self):
        with self.assertRaises(ValueError):
            risk.resolve_levels(self.PLAN, side="short")
        lv = risk.resolve_levels(self.PLAN, entry=364.0, stop=368.0)
        self.assertEqual(lv["side"], "short")
        self.assertTrue(any("saved plan was long" in n for n in lv["notes"]))

    def test_quote_entry(self):
        lv = risk.resolve_levels(None, stop=358.32, last_price=364.18)
        self.assertEqual(lv["sources"]["entry"], "quote")
        self.assertEqual(lv["side"], "long")


class ChecksTests(unittest.TestCase):
    def _budget(self, ledger=None, risk_used=46.88, per_share=5.86, settings=SETTINGS):
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": "2026-10-30T11:00"}):
            bc = tc.budget_check(risk_used, settings=settings, ledger=ledger or {"tickets": [], "trades": []})
        return risk.budget_view(bc, per_share, risk_used)

    def test_clean_trade_has_no_warnings(self):
        sz = risk.size_position("long", 364.18, 358.32, 370.04, SETTINGS)
        self.assertEqual(risk.risk_checks(sz, SETTINGS, self._budget()), [])

    def test_low_rr_noise_stop_and_pdt(self):
        sz = risk.size_position("long", 100.0, 99.95, 100.02, SETTINGS, account=10000)
        w = risk.risk_checks(sz, SETTINGS)
        self.assertTrue(any("R:R 0.40 is below" in x for x in w), w)
        self.assertTrue(any("noise" in x for x in w), w)
        self.assertTrue(any(risk.PDT_NOTE in x for x in w), w)
        at_25k = risk.size_position("long", 100.0, 99.0, 102.0, SETTINGS, account=25000)
        self.assertFalse(any(risk.PDT_NOTE in x for x in risk.risk_checks(at_25k, SETTINGS)))

    def test_rr_exactly_at_minimum_is_not_flagged(self):
        sz = risk.size_position("long", 364.18, 358.32, 370.04, SETTINGS)   # R:R 1.0 in floats
        self.assertFalse(any("R:R" in x for x in risk.risk_checks(sz, SETTINGS)))

    def test_margin_leverage_short_and_size_warnings(self):
        sz = risk.size_position("long", 100.0, 98.5, None, SETTINGS, account=20000, risk_dollars=1000,
                                max_position_pct=200, leverage=4)
        self.assertEqual((sz["shares"], sz["capped_by"]), (400, "max_position_pct"))
        w = risk.risk_checks(sz, SETTINGS)
        self.assertTrue(any("larger than the account" in x for x in w), w)
        self.assertTrue(any("4x intraday" in x for x in w), w)
        self.assertTrue(any("3.00% of the account on one trade" in x for x in w), w)   # $600 of $20k
        short = risk.size_position("short", 100.0, 101.0, None, SETTINGS, leverage=1)
        self.assertTrue(any("cannot sell short" in x for x in risk.risk_checks(short, SETTINGS)))
        default_lev = risk.size_position("short", 100.0, 101.0, None, SETTINGS)
        self.assertFalse(any("cannot sell short" in x for x in risk.risk_checks(default_lev, SETTINGS)))
        capped = risk.size_position("long", 100.0, 99.0, None, SETTINGS, risk_dollars=1000, max_position_pct=200)
        self.assertTrue(any("buying power" in x for x in risk.risk_checks(capped, SETTINGS)))
        tiny = risk.size_position("long", 100.0, 99.0, None, SETTINGS, risk_pct=0.002)
        self.assertTrue(any("is a percent" in x for x in risk.risk_checks(tiny, SETTINGS)))

    def test_budget_blocked_suggests_fitting_size(self):
        led = {"tickets": [], "trades": [trade("a", pnl=-60), trade("b", status="open", qty=40)]}
        sz = risk.size_position("long", 364.18, 358.32, 370.04, SETTINGS)
        b = self._budget(led, sz["risk_used"], sz["risk_per_share"])
        # used = 60 realized loss + 40 open risk = 100 -> 50 left; 46.88 fits
        self.assertTrue(b["ok"])
        self.assertAlmostEqual(b["loss_budget_left"], 50.0)
        self.assertAlmostEqual(b["loss_budget_left_after"], 3.12)
        self.assertEqual(b["max_shares_now"], 8)
        led["trades"].append(trade("c", pnl=-20, closed="2026-10-30T10:30:00-04:00"))
        b = self._budget(led, sz["risk_used"], sz["risk_per_share"])
        self.assertFalse(b["ok"])
        self.assertAlmostEqual(b["max_risk_now"], 30.0)
        self.assertEqual(b["max_shares_now"], 5)               # floor(30 / 5.86)
        w = risk.risk_checks(sz, SETTINGS, b)
        self.assertTrue(any(x.startswith("daily budget: worst-case loss") for x in w), w)
        self.assertTrue(any("at most 5 shares" in x for x in w), w)

    def test_tilt_guard_means_no_shares_fit(self):
        led = {"tickets": [], "trades": [trade(str(i), pnl=-1, closed=f"2026-10-30T10:0{i}:00-04:00")
                                         for i in range(3)]}
        b = self._budget(led)
        self.assertFalse(b["ok"])
        self.assertEqual(b["max_shares_now"], 0)

    def test_existing_items_and_old_plan(self):
        sz = risk.size_position("long", 364.18, 358.32, None, SETTINGS)
        w = risk.risk_checks(sz, SETTINGS, None, ["an open long paper position in TSLA"], "2026-10-29",
                             "2026-10-30")
        self.assertTrue(any("you already have an open long" in x for x in w))
        self.assertTrue(any("2026-10-29" in x for x in w))

    def test_ledger_items(self):
        now = tc.parse_et("2026-10-30T11:00")
        led = {"tickets": [
            {"id": "T1", "symbol": "TSLA", "status": "pending", "qty": 5, "expires_at": "2026-10-30T11:10:00-04:00"},
            {"id": "T0", "symbol": "TSLA", "status": "pending", "qty": 5, "expires_at": "2026-10-30T10:00:00-04:00"},
            {"id": "T2", "symbol": "AMD", "status": "working", "qty": 9},
            {"id": "T3", "symbol": "TSLA", "status": "confirmed", "qty": 5}],
            "trades": [trade("X", symbol="TSLA", status="open"), trade("Y", symbol="tsla", pnl=5)]}
        items = risk.ledger_items(led, now, "TSLA")
        self.assertEqual([t["id"] for t in items["open"]], ["X"])
        self.assertEqual([t["id"] for t in items["pending"]], ["T1"])     # T0 expired
        self.assertEqual(items["working"], [])
        self.assertEqual([t["id"] for t in risk.ledger_items(led, now)["working"]], ["T2"])
        self.assertEqual(len(risk.describe_items(items)), 2)

    def test_settings_warnings(self):
        s = dict(SETTINGS, risk_per_trade_pct=1.0)       # $250 a trade vs $150 daily max loss
        w = risk.settings_warnings(s)
        self.assertTrue(any("daily max loss" in x for x in w), w)
        self.assertEqual(risk.settings_warnings(SETTINGS), [])
        self.assertTrue(any(risk.PDT_NOTE in x for x in risk.settings_warnings(dict(SETTINGS, account_size=5000))))


class CommandTests(TempHomeTestCase):
    NOW = "2026-10-30T10:15"

    def test_size_text_and_json(self):
        code, out = run_main("size", "--entry", "364.18", "--stop", "358.32", "--target", "370.04")
        self.assertEqual(code, 0)
        self.assertIn("BUY 8 shares @ 364.18 · STOP 358.32 · TARGET 370.04", out)
        self.assertIn("$46.88", out)
        self.assertIn("size set by", out)
        self.assertIn(tc.DISCLAIMER, out)
        code, out = run_main("--json", "size", "--symbol", "tsla", "--entry", "364.18", "--stop", "358.32")
        d = json.loads(out)
        for k in ("symbol", "side", "action", "shares", "order", "entry", "stop", "target", "risk_per_share",
                  "risk_used", "notional", "position_pct", "capped_by", "limits", "rr", "budget", "warnings",
                  "notes", "sources", "saved", "disclaimer"):
            self.assertIn(k, d)
        self.assertEqual((d["symbol"], d["order"], d["shares"]), ("TSLA", "BUY 8 TSLA", 8))
        self.assertTrue(d["budget"]["ok"])
        self.assertAlmostEqual(d["budget"]["loss_budget_left_after"], 103.12)

    def test_save_merges_sizing_and_reads_plan_back(self):
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04,
                      "entry_signal": {"setup": "orb"},
                      "time_machine": {"side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04,
                                       "target_first_pct": 29.4, "stop_first_pct": 25.5, "verdict": "CLOSE TO A COIN FLIP"}})
        code, out = run_main("size", "--symbol", "TSLA", "--save", "--json")
        d = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(d["saved"])
        self.assertEqual(d["dropped_sections"], [])
        self.assertTrue(any("time-machine: target first 29.4%" in n for n in d["notes"]))
        plan = tc.load_plan("TSLA")
        self.assertEqual(plan["sizing"]["shares"], 8)
        self.assertAlmostEqual(plan["sizing"]["risk_used"], 46.88)
        self.assertIn("time_machine", plan)          # same levels: kept
        # new entry -> target/time_machine computed for the old levels are cleared, entry_signal kept
        code, out = run_main("size", "TSLA", "--entry", "365", "--save", "--json")
        d = json.loads(out)
        self.assertEqual(sorted(d["dropped_sections"]), ["target", "time_machine"])
        plan = tc.load_plan("TSLA")
        self.assertNotIn("time_machine", plan)
        self.assertNotIn("target", plan)
        self.assertIn("entry_signal", plan)
        self.assertEqual((plan["entry"], plan["sizing"]["shares"]), (365.0, 7))

    def test_null_sections_left_by_the_entry_skill_are_not_reported_as_cleared(self):
        # entry --save resets target/exit_plan/time_machine/sizing to null when its levels change
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "setup": "orb",
                      "target": None, "exit_plan": None, "time_machine": None, "sizing": None})
        code, out = run_main("size", "TSLA", "--entry", "365", "--stop", "359", "--save", "--json")
        d = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(d["dropped_sections"], [])
        code, out = run_main("size", "TSLA", "--entry", "366", "--stop", "360", "--save")
        self.assertNotIn("Cleared", out)

    def test_zero_shares_not_saved(self):
        code, out = run_main("size", "--symbol", "TSLA", "--entry", "500", "--stop", "400", "--save")
        self.assertEqual(code, 1)
        self.assertIn("0 SHARES", out)
        self.assertIn("Not saved", out)
        self.assertIsNone(tc.load_plan("TSLA"))

    def test_input_errors(self):
        with self.assertRaises(ValueError):
            run_main("size")
        with self.assertRaises(ValueError):
            run_main("size", "--entry", "10", "--stop", "9", "--save")       # --save needs a symbol
        with self.assertRaises(ValueError):
            run_main("size", "--symbol", "TSLA")                             # no plan, no stop
        with self.assertRaises(ValueError):
            run_main("size", "--entry", "10", "--stop", "9", "--risk-pct", "150")
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                risk.main(["size", "--entry", "10", "--stop", "9", "--leverage", "3"])
        self.assertEqual(cm.exception.code, 1)                               # usage errors are exit 1

    def test_entry_from_quote(self):
        bars = bars_from_closes("2026-10-30", [363.0, 364.18], start="10:10", minutes=1)
        with fake_bars({("TSLA", "1m"): bars}):
            code, out = run_main("size", "TSLA", "--stop", "358.32", "--json")
        d = json.loads(out)
        self.assertEqual((d["entry"], d["shares"], d["sources"]["entry"]), (364.18, 8, "quote"))
        with fake_bars({}):
            with self.assertRaises(tc.FetchError):
                run_main("size", "TSLA", "--stop", "358.32")

    def test_config_and_show(self):
        code, out = run_main("config", "--account", "30000", "--risk-pct", "0.25", "--min-rr", "1.5")
        self.assertEqual(code, 0)
        self.assertIn("$75.00", out)
        s = tc.load_settings()
        self.assertEqual((s["account_size"], s["risk_per_trade_pct"], s["min_reward_risk"]), (30000.0, 0.25, 1.5))
        _, out = run_main("config", "--json")
        self.assertEqual(json.loads(out)["changed"], [])
        tc.save_ledger({"tickets": [], "trades": [trade("L", pnl=-50), trade("O", symbol="TSLA", status="open",
                                                                          qty=8, entry=364.18, stop=358.32)]})
        code, out = run_main("show", "--json")
        d = json.loads(out)
        self.assertAlmostEqual(d["today"]["open_risk"], 46.88)
        self.assertAlmostEqual(d["today"]["loss_budget_left"], 53.12)
        self.assertEqual([p["symbol"] for p in d["open_positions"]], ["TSLA"])
        self.assertFalse(d["next_trade"]["ok"])                # a full $75 trade does not fit in $53.12
        self.assertAlmostEqual(d["today"]["max_risk_now"], 53.12)
        code, text = run_main("show")
        self.assertIn("OPEN POSITIONS", text)
        self.assertIn("TSLA", text)

    def test_corrupt_settings_or_ledger_is_state_error(self):
        tc.save_ledger({"tickets": [], "trades": [{"id": "bad", "status": "open"}]})
        with self.assertRaises(tc.StateError):
            run_main("size", "--entry", "10", "--stop", "9")
        (self.home / "settings.json").write_text(json.dumps({"account_size": "lots"}))
        with self.assertRaises(tc.StateError):
            run_main("show")


class ReviewRegressionTests(TempHomeTestCase):
    """Defects found in review: each test failed before its fix."""
    NOW = "2026-10-30T10:15"

    @staticmethod
    def _working(tid="TKT-20261030-001", confirmed="2026-10-30T10:02:00-04:00", qty=60, entry=100.0, stop=98.0):
        return {"id": tid, "symbol": "AMD", "side": "long", "qty": qty, "type": "limit", "entry": entry,
                "stop": stop, "target": 104.0, "status": "working", "created_at": confirmed,
                "confirmed_at": confirmed, "trade_id": None}

    def test_todays_working_order_is_committed_risk_like_the_broker_counts_it(self):
        # $120 of risk in a working limit order + $46.88 new = $166.88 > $150: broker/budget block it.
        tc.save_ledger({"tickets": [self._working()], "trades": []})
        _, out = run_main("size", "--entry", "364.18", "--stop", "358.32", "--json")
        b = json.loads(out)["budget"]
        self.assertFalse(b["ok"], b)
        self.assertAlmostEqual(b["open_risk"], 120.0)
        self.assertAlmostEqual(b["max_risk_now"], 30.0)
        self.assertEqual(b["max_shares_now"], 5)
        self.assertEqual(b["trades_today"], 1)                       # the order took a trade slot
        _, out = run_main("show", "--json")
        d = json.loads(out)
        self.assertAlmostEqual(d["today"]["open_risk"], 120.0)
        self.assertAlmostEqual(d["today"]["working_order_risk"], 120.0)
        self.assertEqual(d["open_positions"], [])                     # not a position yet
        _, text = run_main("show")
        self.assertNotIn("not in open risk", text)
        self.assertIn("working order", text)

    def test_yesterdays_working_order_is_not_counted(self):
        # The broker only reserves today's working (day) orders.
        tc.save_ledger({"tickets": [self._working(confirmed="2026-10-29T15:00:00-04:00")], "trades": []})
        _, out = run_main("size", "--entry", "364.18", "--stop", "358.32", "--json")
        b = json.loads(out)["budget"]
        self.assertTrue(b["ok"], b)
        self.assertAlmostEqual(b["open_risk"], 0.0)

    def test_count_blocked_budget_leaves_no_room_and_no_ticket_hint(self):
        led = {"tickets": [], "trades": [trade(str(i), pnl=-1, closed=f"2026-10-30T10:0{i}:00-04:00")
                                         for i in range(3)]}
        tc.save_ledger(led)
        _, out = run_main("size", "--entry", "364.18", "--stop", "358.32", "--json")
        b = json.loads(out)["budget"]
        self.assertFalse(b["ok"])
        self.assertEqual(b["max_risk_now"], 0.0)                    # tilt guard: no new risk fits
        _, text = run_main("size", "--entry", "364.18", "--stop", "358.32")
        self.assertNotIn("then a paper broker ticket", text)
        self.assertNotIn("room for new risk $147.00", text)
        _, out = run_main("show", "--json")
        self.assertEqual(json.loads(out)["today"]["max_risk_now"], 0.0)

    def test_zero_share_reason_is_the_first_warning(self):
        # R:R 0.5 is also below the minimum; the card says "see the first ! line".
        _, text = run_main("size", "--entry", "5000", "--stop", "4900", "--target", "5050", "--account", "10000")
        bangs = [ln for ln in text.splitlines() if ln.startswith("! ")]
        self.assertTrue(bangs[0].startswith("! 0 shares"), bangs)

    def test_zero_share_reason_names_every_cap_that_allows_zero(self):
        # $10k account: $20 budget < $100 stop distance AND 25% max position ($2,500) < one $5,000 share.
        sz = risk.size_position("long", 5000.0, 4900.0, None, SETTINGS, account=10000)
        msg = risk.zero_shares_reason(sz)
        self.assertIn("$100.00", msg)
        self.assertIn("--max-position-pct 50", msg)

    def test_zero_share_suggested_risk_pct_is_enough_for_one_share(self):
        sz = risk.size_position("long", 364.18, 358.32, None, SETTINGS, risk_dollars=5)
        self.assertEqual(sz["shares"], 0)
        m = re.search(r"--risk-pct ([\d.]+)", risk.zero_shares_reason(sz))
        self.assertIsNotNone(m, risk.zero_shares_reason(sz))
        again = risk.size_position("long", 364.18, 358.32, None, SETTINGS, risk_pct=float(m.group(1)))
        self.assertGreaterEqual(again["shares"], 1)

    def test_zero_share_reason_never_suggests_an_impossible_max_position(self):
        # one $120,000 share is more than 4x a $25k account: no --max-position-pct (max 400) can fix it
        sz = risk.size_position("long", 120000.0, 119000.0, None, SETTINGS, risk_dollars=2000,
                                max_position_pct=400, leverage=4)
        self.assertEqual(sz["shares"], 0)
        msg = risk.zero_shares_reason(sz)
        self.assertNotIn("--max-position-pct 480", msg)
        self.assertIn("4x", msg)
        # 1x buying power is the only cap: say which leverage would allow a share
        sz = risk.size_position("long", 30000.0, 29000.0, None, SETTINGS, risk_dollars=2000, max_position_pct=400)
        self.assertEqual((sz["shares"], sz["capped_by"]), (0, "buying_power"))
        self.assertIn("--leverage 2", risk.zero_shares_reason(sz))

    def test_inferred_short_note_does_not_suggest_a_flag_that_errors(self):
        _, out = run_main("size", "--entry", "364.18", "--stop", "370.04", "--json")
        notes = json.loads(out)["notes"]
        self.assertTrue(any("SHORT inferred" in n for n in notes), notes)
        self.assertFalse(any("pass --side long" in n for n in notes), notes)


class TimeMachineNoteTests(TempHomeTestCase):
    """`size` shows the time machine's odds exactly as it printed them, and says when these levels were
    never tested."""
    NOW = "2026-10-30T10:15"
    UNTESTED = "not tested by the time machine for these levels"

    @staticmethod
    def tm(**kw):
        d = {"side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04, "verdict": "CLOSE TO A COIN FLIP",
             "target_first_pct": 31.66, "stop_first_pct": 31.57, "open_at_close_pct": 36.77}
        d.update(kw)
        return d

    def save_plan(self, tm=None):
        p = {"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04}
        if tm is not None:
            p["time_machine"] = tm
        tc.save_plan(p)

    def notes(self, *argv):
        code, out = run_main("size", *argv, "--json")
        self.assertEqual(code, 0)
        return json.loads(out)["notes"]

    def test_display_pct_is_printed_as_the_time_machine_printed_it(self):
        self.save_plan(self.tm(display_pct={"target_first": 31.6, "stop_first": 31.6, "open_at_close": 36.8}))
        notes = self.notes("TSLA")
        self.assertIn("time-machine: target first 31.6% / stop first 31.6% - CLOSE TO A COIN FLIP", notes)
        self.assertFalse(any(self.UNTESTED in n for n in notes))
        _, text = run_main("size", "TSLA")
        self.assertIn("- time-machine: target first 31.6% / stop first 31.6%", text)

    def test_raw_values_fall_back_to_one_decimal(self):
        self.save_plan(self.tm())
        self.assertIn("time-machine: target first 31.7% / stop first 31.6% - CLOSE TO A COIN FLIP", self.notes("TSLA"))
        self.save_plan(self.tm(display_pct={"target_first": None, "stop_first": "?"}))   # malformed: raw values
        self.assertIn("time-machine: target first 31.7% / stop first 31.6% - CLOSE TO A COIN FLIP", self.notes("TSLA"))

    def test_plan_without_time_machine(self):
        self.save_plan()
        self.assertIn(f"{self.UNTESTED} (run time_machine.py TSLA --save)", self.notes("TSLA"))
        _, text = run_main("size", "TSLA")
        self.assertIn(f"- {self.UNTESTED} (run time_machine.py TSLA --save)", text)

    def test_odds_for_other_levels_are_not_shown(self):
        # Regression: the odds were printed whenever the plan's levels matched, even if the time machine
        # had been run for another target.
        self.save_plan(self.tm(target=375.0))
        notes = self.notes("TSLA")
        self.assertFalse(any(n.startswith("time-machine:") for n in notes), notes)
        self.assertIn(f"{self.UNTESTED} (run time_machine.py TSLA --save) - the plan's odds were for other levels",
                      notes)

    def test_levels_off_the_plan_name_them_unless_saved(self):
        self.save_plan(self.tm())
        notes = self.notes("TSLA", "--entry", "365", "--stop", "359", "--target", "371")
        self.assertIn(f"{self.UNTESTED} (run time_machine.py TSLA --entry 365.00 --stop 359.00 --target 371.00 "
                      "--save) - the plan's odds were for other levels", notes)
        notes = self.notes("TSLA", "--entry", "365", "--stop", "359", "--target", "371", "--save")
        self.assertIn(f"{self.UNTESTED} (run time_machine.py TSLA --save) - the plan's odds were for other levels",
                      notes)
        self.assertNotIn("time_machine", tc.load_plan("TSLA"))          # cleared by the save
        self.assertIn(f"{self.UNTESTED} (run time_machine.py TSLA --save)", self.notes("TSLA"))

    def test_no_target_or_no_symbol(self):
        notes = self.notes("TSLA", "--entry", "100", "--stop", "99")
        self.assertIn(f"{self.UNTESTED} (it needs a target to measure the odds)", notes)
        notes = self.notes("--entry", "100", "--stop", "99", "--target", "102")
        self.assertFalse(any(self.UNTESTED in n for n in notes), notes)  # no ticker: nothing to run


class DirectionalOpenRiskTests(TempHomeTestCase):
    NOW = "2026-10-30T10:15"

    def test_stop_past_the_entry_is_zero_open_risk_everywhere(self):
        # trade_initial_risk is directional: a long at 100 with its stop moved to 103 risks nothing more.
        t = trade("TRADE-20261030-001", status="open", qty=10, entry=100.0, stop=103.0)
        s = trade("TRADE-20261030-002", symbol="AMD", status="open", qty=10, entry=50.0, stop=51.0, side="short")
        tc.save_ledger({"tickets": [], "trades": [t, s]})
        _, out = run_main("show", "--json")
        d = json.loads(out)
        self.assertEqual([p["risk"] for p in d["open_positions"]], [0.0, 10.0])
        self.assertEqual(d["today"]["open_risk"], 10.0)
        self.assertEqual(d["today"]["open_risk"], sum(p["risk"] for p in d["open_positions"]))
        self.assertEqual(d["today"]["open_risk"], tc.day_summary()["open_risk"])
        _, out = run_main("size", "--entry", "364.18", "--stop", "358.32", "--json")
        self.assertEqual(json.loads(out)["budget"]["open_risk"], 10.0)


class CliTests(TempHomeTestCase):
    NOW = "2026-10-30T10:15"

    def _env(self, fixtures):
        return {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(fixtures),
                "DAYTRADER_NOW": "2026-10-30T10:15"}

    def test_cli_quote_fixture_and_data_error(self):
        fx = self.home / "fx"
        bars = bars_from_closes("2026-10-30", [363.5, 364.18], start="10:12", minutes=1)
        write_fixture(fx, yahoo_chart_url("TSLA", "1m", "1d"),
                      yahoo_payload(bars, "TSLA", price=364.18, regularMarketTime=et_ts("2026-10-30", "10:14")))
        r = run_cli(SCRIPT, "size", "TSLA", "--stop", "358.32", "--target", "370.04", "--save", "--json",
                    env=self._env(fx))
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual((d["order"], d["entry"], d["saved"]), ("BUY 8 TSLA", 364.18, True))
        self.assertEqual(tc.load_plan("TSLA")["sizing"]["shares"], 8)
        r = run_cli(SCRIPT, "size", "TSLA", env=self._env(fx))                      # now from the plan
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("BUY 8 TSLA @ 364.18 · STOP 358.32 · TARGET 370.04", r.stdout)
        r = run_cli(SCRIPT, "size", "NVDA", "--stop", "180", env=self._env(fx))     # no fixture -> exit 2
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith("data error: "), r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)
        self.assertNotIn("Traceback", r.stderr)

    def test_cli_offline_sizing_config_and_bad_flags(self):
        env = self._env(self.home / "nofx")
        r = run_cli(SCRIPT, "size", "--entry", "364.18", "--stop", "370.04", "--risk-dollars", "100", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SELL SHORT 17 shares", r.stdout)
        r = run_cli(SCRIPT, "config", "--account", "10000", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pattern-day-trader", r.stdout)
        r = run_cli(SCRIPT, "show", "--json", env=env)
        self.assertEqual(json.loads(r.stdout)["settings"]["account_size"], 10000.0)
        for bad in (["size", "--entry", "x", "--stop", "9"], ["size", "--live"], ["size", "--entry", "10"]):
            r = run_cli(SCRIPT, *bad, env=env)
            self.assertEqual(r.returncode, 1, (bad, r.stderr))
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
