"""Tests for the budget skill (skills/budget/scripts/budget.py). Offline, synthetic data only."""
import io
import json
import os
import random
import re
import shlex
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

# Works both with `unittest discover -s tests` and `python3 -m unittest tests.test_budget`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from helpers import (SKILLS, TempHomeTestCase, bars_from_closes, fake_bars, tc, write_coinbase_quote,  # noqa: E402
                     write_fixture, yahoo_chart_url, yahoo_payload)

bud = helpers.load_skill_module("budget", "budget")
SCRIPT = helpers.skill_script("budget", "budget.py")
SKILL_MD = SKILLS / "budget" / "SKILL.md"
DAY = "2026-10-30"


def trade(tid, pnl=None, status="closed", opened=f"{DAY}T09:45:00-04:00", closed=f"{DAY}T10:00:00-04:00",
          entry=100.0, stop=99.0, qty=50, side="long", symbol="X", **kw):
    """A ledger trade. Default risk = |100 - 99| x 50 = $50."""
    t = {"id": tid, "symbol": symbol, "side": side, "qty": qty, "entry_price": entry, "stop": stop,
         "target": entry + 2 * (entry - stop) if all(isinstance(x, float) for x in (entry, stop)) else None,
         "opened_at": opened,
         "status": status}
    if status == "closed":
        t.update(pnl=pnl, closed_at=closed)
        if pnl is not None:
            t["exit_price"] = entry + (pnl / qty if side == "long" else -pnl / qty)
    t.update(kw)
    return t


def ledger(*trades, tickets=()):
    return {"tickets": list(tickets), "trades": list(trades)}


SETTINGS = bud.clean_settings(dict(tc.DEFAULT_SETTINGS))   # $150 max loss / $150 open / 5 trades / 3 losses


def status_of(led, settings=None, day=DAY, now=f"{DAY}T11:00:00-04:00"):
    view = bud.budget_view(bud.normalize_ledger(led), day, DAY, now)
    return bud.assess(view, settings or SETTINGS, day)


def check_of(led, risk, settings=None, per_share=None):
    view = bud.budget_view(bud.normalize_ledger(led), DAY, DAY, f"{DAY}T11:00:00-04:00")
    return bud.check_new_risk(view, settings or SETTINGS, DAY, risk, per_share)


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = bud.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class LedgerHygieneTests(TempHomeTestCase):
    NOW = f"{DAY}T11:00"

    def test_utc_stamps_are_counted_on_their_eastern_date(self):
        # 02:30 UTC on the 30th is 22:30 ET on the 29th: that loss belongs to yesterday.
        led = ledger(trade("a", pnl=-50, opened="2026-10-29T15:00:00-04:00", closed="2026-10-30T02:30:00Z"))
        norm = bud.normalize_ledger(led)
        self.assertEqual(norm["trades"][0]["closed_at"], "2026-10-29T22:30:00-04:00")
        self.assertEqual(status_of(led)["realized_pnl"], 0)
        self.assertEqual(status_of(led, day="2026-10-29")["realized_pnl"], -50)

    def test_missing_pnl_and_r_are_computed(self):
        t = trade("s", side="short", entry=160.0, stop=162.0, qty=10)
        t.update(pnl=None, exit_price=162.0)
        norm = bud.normalize_ledger(ledger(t))
        self.assertEqual(norm["trades"][0]["pnl"], -20.0)
        self.assertEqual(norm["trades"][0]["r_multiple"], -1.0)

    def test_cancelled_trades_never_count(self):
        led = ledger(trade("c", status="cancelled", entry=None, stop=None))
        s = status_of(led)
        self.assertEqual(s["trades_today"], 0)
        self.assertEqual(s["status"], bud.OPEN)

    def test_unmeasurable_risk_fails_closed(self):
        for bad in (trade("o", status="open", stop=None), trade("o", status="open", qty="lots"),
                    trade("o", status="open", side="sideways"), trade("x", pnl=None),
                    trade("y", pnl=-5, closed=None)):
            with self.subTest(bad=bad):
                with self.assertRaises(tc.StateError):
                    bud.normalize_ledger(ledger(bad))
        with self.assertRaises(tc.StateError):
            bud.normalize_ledger({"tickets": [], "trades": ["oops"]})
        with self.assertRaises(tc.StateError):
            bud.normalize_ledger({"tickets": {"TKT-1": {}}, "trades": []})

    def test_numeric_strings_are_accepted(self):
        t = trade("o", status="open", qty="8", entry="364.18", stop="358.32")
        self.assertEqual(status_of(ledger(t))["open_risk"]["total"], 46.88)

    def test_working_orders_dedupe_ticket_and_trade(self):
        tk = {"id": "TKT-1", "symbol": "AMZN", "side": "long", "qty": 10, "entry": 220.0, "stop": 218.0,
              "status": "working", "trade_id": "TR-1"}
        tr = {"id": "TR-1", "ticket_id": "TKT-1", "symbol": "AMZN", "side": "long", "qty": 10,
              "limit_price": 220.0, "stop": 218.0, "status": "working"}
        self.assertEqual(len(bud.normalize_ledger(ledger(tr, tickets=[tk]))["working"]), 1)
        self.assertEqual(len(bud.normalize_ledger(ledger(tickets=[tk]))["working"]), 1)
        s = status_of(ledger(tr, tickets=[tk]))
        self.assertEqual(s["open_risk"], {"total": 20.0, "positions": 0.0, "working_orders": 20.0, "limit": 150.0})
        self.assertEqual(s["trades_today"], 1)      # it can fill without another check
        self.assertEqual(s["positions"], [])
        self.assertEqual(len(s["working_orders"]), 1)

    def test_pending_tickets_expire(self):
        now = tc.parse_et(f"{DAY}T11:00")
        live = {"id": "A", "status": "pending", "qty": 8, "entry": 364.18, "stop": 358.32,
                "expires_at": f"{DAY}T11:10:00-04:00"}
        old = dict(live, id="B", expires_at=f"{DAY}T10:50:00-04:00")
        norm = bud.normalize_ledger(ledger(tickets=[live, old]))
        got = bud.live_pending(norm["pending"], now)
        self.assertEqual([k["id"] for k in got], ["A"])
        self.assertEqual(got[0]["risk"], 46.88)

    def test_bad_settings_are_a_state_error(self):
        with self.assertRaises(tc.StateError):
            bud.clean_settings(dict(tc.DEFAULT_SETTINGS, daily_max_loss="lots"))
        with self.assertRaises(tc.StateError):
            bud.clean_settings(dict(tc.DEFAULT_SETTINGS, max_trades_per_day=-1))


class StatusTests(unittest.TestCase):
    def test_fresh_day_is_open_for_trading(self):
        s = status_of(ledger())
        self.assertEqual(s["status"], bud.OPEN)
        self.assertEqual(s["room_for_new_risk"], 150.0)
        self.assertEqual(s["loss_budget"]["used_pct"], 0)
        self.assertEqual(s["limits_hit"], [])
        self.assertEqual(s["cautions"], [])

    def test_open_risk_uses_the_budget_but_profits_do_not_extend_it(self):
        led = ledger(trade("w", pnl=200), trade("o", status="open", qty=8, entry=364.18, stop=358.32))
        s = status_of(led)
        self.assertEqual(s["loss_budget"]["used"], 46.88)        # +$200 realized does not add room
        self.assertEqual(s["loss_budget"]["left"], 103.12)
        self.assertEqual(s["room_for_new_risk"], 103.12)
        self.assertEqual(s["risked_today"], 96.88)

    def test_caution_at_75_percent(self):
        s = status_of(ledger(trade("a", pnl=-112.5)))
        self.assertEqual(s["status"], bud.CAUTION)
        self.assertTrue(any("75%" in c for c in s["cautions"]))
        self.assertEqual(status_of(ledger(trade("a", pnl=-100)))["status"], bud.OPEN)   # 67%, room = 1 trade

    def test_realized_max_loss_is_done_for_the_day(self):
        s = status_of(ledger(trade("a", pnl=-100), trade("b", pnl=60, closed=f"{DAY}T10:30:00-04:00"),
                             trade("c", pnl=-115, closed=f"{DAY}T10:45:00-04:00")))
        self.assertEqual(s["status"], bud.STOP)
        self.assertEqual([(x["code"], x["scope"]) for x in s["limits_hit"]], [("daily_max_loss", "today")])
        self.assertEqual(s["room_for_new_risk"], 0)

    def test_full_budget_from_open_risk_waits_for_a_close(self):
        s = status_of(ledger(trade("a", pnl=-100), trade("o", status="open")))
        self.assertEqual(s["status"], bud.STOP)
        self.assertEqual([(x["code"], x["scope"]) for x in s["limits_hit"]], [("loss_budget_full", "until_close")])

    def test_max_open_risk(self):
        settings = dict(SETTINGS, max_open_risk=100.0)
        led = ledger(trade("o1", status="open"), trade("o2", status="open", symbol="Y"))
        s = status_of(led, settings)
        self.assertEqual(s["status"], bud.STOP)
        self.assertIn("max_open_risk", [x["code"] for x in s["limits_hit"]])

    def test_max_trades_and_last_trade_caution(self):
        wins = [trade(str(i), pnl=10, closed=f"{DAY}T10:0{i}:00-04:00") for i in range(5)]
        s = status_of(ledger(*wins))
        self.assertEqual(s["status"], bud.STOP)
        self.assertEqual([x["code"] for x in s["limits_hit"]], ["max_trades"])
        s4 = status_of(ledger(*wins[:4]))
        self.assertEqual(s4["status"], bud.CAUTION)
        self.assertIn("1 trade left today (4 of 5 used)", s4["cautions"])

    def test_tilt_guard_and_one_more_loss_caution(self):
        losses = [trade(str(i), pnl=-1, closed=f"{DAY}T10:0{i}:00-04:00") for i in range(3)]
        s = status_of(ledger(*losses))
        self.assertEqual(s["status"], bud.STOP)
        self.assertEqual([x["code"] for x in s["limits_hit"]], ["tilt_guard"])
        s2 = status_of(ledger(*losses[:2]))
        self.assertEqual(s2["status"], bud.CAUTION)
        self.assertTrue(any("one more trips the tilt guard" in c for c in s2["cautions"]))
        # A win resets the streak.
        s3 = status_of(ledger(*losses[:2], trade("w", pnl=5, closed=f"{DAY}T10:30:00-04:00")))
        self.assertEqual(s3["loss_streak"], 0)

    def test_room_smaller_than_a_standard_trade_is_a_caution(self):
        s = status_of(ledger(trade("o", status="open", qty=120)), dict(SETTINGS, daily_max_loss=1000.0))
        self.assertEqual(s["room_for_new_risk"], 30.0)    # 150 max open risk - 120 open
        self.assertEqual(s["status"], bud.CAUTION)

    def test_zero_limits_block_everything(self):
        for key in ("daily_max_loss", "max_open_risk", "max_trades_per_day", "max_consecutive_losses"):
            with self.subTest(key=key):
                s = status_of(ledger(), dict(SETTINGS, **{key: 0}))
                self.assertEqual(s["status"], bud.STOP)
                self.assertFalse(check_of(ledger(), 1.0, dict(SETTINGS, **{key: 0}))["ok"])

    def test_past_day_is_reconstructed_at_its_close(self):
        # Opened on the 29th, closed on the 30th: open at the 29th's close, realized on the 30th.
        t = trade("a", pnl=-30, opened="2026-10-29T15:00:00-04:00", closed=f"{DAY}T09:35:00-04:00")
        later = trade("b", pnl=-10, opened=f"{DAY}T10:00:00-04:00", closed=f"{DAY}T10:10:00-04:00")
        y = status_of(ledger(t, later), day="2026-10-29")
        self.assertEqual(y["open_risk"]["total"], 50.0)
        self.assertEqual(y["realized_pnl"], 0)
        self.assertEqual(y["trades_today"], 1)
        self.assertEqual(len(y["positions"]), 1)
        today = status_of(ledger(t, later))
        self.assertEqual(today["realized_pnl"], -40)
        self.assertEqual(today["open_risk"]["total"], 0)
        self.assertEqual(today["trades_today"], 1)


class CheckTests(unittest.TestCase):
    def test_ok_and_after_numbers(self):
        r = check_of(ledger(trade("a", pnl=-40)), 46.88)
        self.assertTrue(r["ok"])
        self.assertEqual(r["verdict"], "OK")
        self.assertEqual(r["after"], {"loss_budget_used": 86.88, "loss_budget_left": 63.12,
                                      "open_risk": 46.88, "trade_number": 2})
        self.assertEqual(r["max_new_risk_now"], 110.0)

    def test_exact_boundary(self):
        led = ledger(trade("a", pnl=-40), trade("o", status="open", qty=8, entry=364.18, stop=358.32))
        room = check_of(led, 1.0)["max_new_risk_now"]
        self.assertEqual(room, 63.12)
        self.assertTrue(check_of(led, room)["ok"])
        blocked = check_of(led, room + 0.01, per_share=5.86)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["verdict"], "BLOCKED")
        self.assertTrue(any("daily max loss" in x for x in blocked["reasons"]))
        self.assertEqual(blocked["max_shares_now"], 10)

    def test_invalid_risk(self):
        for bad in (0, -5, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                check_of(ledger(), bad)

    def test_working_orders_are_committed_but_pending_tickets_are_not(self):
        work = {"id": "TKT-W", "symbol": "AMZN", "side": "long", "qty": 60, "entry": 220.0, "stop": 218.0,
                "status": "working"}   # $120 of risk
        pend = dict(work, id="TKT-P", status="pending", expires_at=f"{DAY}T23:00:00-04:00")
        self.assertTrue(check_of(ledger(tickets=[pend]), 100)["ok"])
        r = check_of(ledger(tickets=[work]), 40)
        self.assertFalse(r["ok"])
        self.assertEqual(r["working_order_risk"], 120.0)
        # The shared tc.budget_check alone (what the broker runs) does not see the working order.
        self.assertTrue(tc.budget_check(40, settings=SETTINGS, ledger=ledger(tickets=[work]), day=DAY)["ok"])

    def test_last_trade_and_tilt_warnings(self):
        led = ledger(*[trade(str(i), pnl=-1 if i >= 2 else 3, closed=f"{DAY}T10:0{i}:00-04:00") for i in range(4)])
        r = check_of(led, 20)
        self.assertTrue(r["ok"])
        self.assertTrue(any("last trade today" in w for w in r["warnings"]))
        self.assertTrue(any("tilt guard" in w for w in r["warnings"]))
        self.assertFalse(check_of(ledger(*led["trades"], trade("e", pnl=5, closed=f"{DAY}T10:30:00-04:00")), 20)["ok"])

    def test_status_and_check_agree(self):
        """Randomised: room is always checkable, a cent more never is, and STOP blocks any risk."""
        rng = random.Random(7)
        for _ in range(300):
            trades = []
            for i in range(rng.randint(0, 7)):
                minute = 10 + i
                if rng.random() < 0.3:
                    trades.append(trade(f"o{i}", status="open", qty=rng.randint(1, 80),
                                        opened=f"{DAY}T10:{minute}:00-04:00"))
                else:
                    trades.append(trade(f"c{i}", pnl=round(rng.uniform(-70, 60), 2),
                                        opened=f"{DAY}T10:{minute}:00-04:00", closed=f"{DAY}T11:{minute}:00-04:00"))
            settings = dict(SETTINGS, daily_max_loss=float(rng.choice([100, 150, 151.37, 400])),
                            max_open_risk=float(rng.choice([75, 150, 333.33])),
                            max_trades_per_day=rng.randint(1, 6), max_consecutive_losses=rng.randint(1, 4))
            led = ledger(*trades)
            s = status_of(led, settings)
            room = s["room_for_new_risk"]
            if s["status"] == bud.STOP:
                self.assertEqual(room, 0)
                self.assertFalse(check_of(led, 0.01, settings)["ok"])
            else:
                self.assertGreater(room, 0)
                self.assertTrue(check_of(led, room, settings)["ok"], (s, room))
                self.assertFalse(check_of(led, room + 0.01, settings)["ok"], (s, room))


class HistoryTests(unittest.TestCase):
    def test_rows_flags_and_totals(self):
        led = ledger(
            trade("old", pnl=-500, opened="2026-10-01T10:00:00-04:00", closed="2026-10-01T11:00:00-04:00"),
            trade("a", pnl=45.2, opened="2026-10-26T10:00:00-04:00", closed="2026-10-26T10:30:00-04:00"),
            # The 29th: L L L W (tilt even though the day ends on a win), -$162 (max loss), 6 trades.
            *[trade(f"t{i}", pnl=p, opened=f"2026-10-29T10:0{i}:00-04:00", closed=f"2026-10-29T11:0{i}:00-04:00")
              for i, p in enumerate([-60, -60, -60, 18, 0, 0])],
            trade("open", status="open", opened=f"{DAY}T10:00:00-04:00"),
        )
        h = bud.history(bud.normalize_ledger(led), SETTINGS, DAY, 10)
        self.assertEqual((h["start"], h["end"]), ("2026-10-21", DAY))
        self.assertEqual([r["date"] for r in h["rows"]], ["2026-10-26", "2026-10-29", DAY])
        d29 = h["rows"][1]
        self.assertEqual((d29["trades"], d29["wins"], d29["losses"], d29["scratches"]), (6, 1, 3, 2))
        self.assertEqual(d29["realized_pnl"], -162.0)
        self.assertEqual(d29["max_loss_streak"], 3)
        self.assertEqual(d29["flags"], ["MAX LOSS", "OVER MAX TRADES", "TILT"])
        self.assertEqual(d29["r_total"], -3.24)      # 3 x -1.2R + 0.36R (R computed from pnl / $50 risk)
        self.assertEqual(h["rows"][2]["still_open"], 1)
        t = h["totals"]
        self.assertEqual((t["trades"], t["closed"], t["wins"], t["losses"]), (8, 7, 2, 3))
        self.assertEqual(t["realized_pnl"], -116.8)
        self.assertEqual((t["green_days"], t["red_days"], t["limit_days"]), (1, 1, 1))
        self.assertEqual((t["best_day"], t["worst_day"]), ("2026-10-26", "2026-10-29"))
        self.assertAlmostEqual(t["win_rate_pct"], 28.6)
        text = bud.render_history(h)
        self.assertIn("MAX LOSS, OVER MAX TRADES, TILT", text)

    def test_empty_window(self):
        h = bud.history(bud.normalize_ledger(ledger()), SETTINGS, DAY, 5)
        self.assertEqual(h["rows"], [])
        self.assertIsNone(h["totals"]["win_rate_pct"])
        self.assertIn("No paper trades", bud.render_history(h))


class SettingsTests(TempHomeTestCase):
    NOW = f"{DAY}T11:00"

    def test_loosened(self):
        cur = {"daily_max_loss": 150.0, "max_open_risk": 150.0, "max_trades_per_day": 5, "max_consecutive_losses": 3}
        self.assertEqual(bud.loosened(cur, {"daily_max_loss": 200.0, "max_trades_per_day": 4}), ["daily_max_loss"])
        self.assertEqual(bud.loosened(cur, {"max_consecutive_losses": 3}), [])

    def test_limit_notes(self):
        warn, notes = bud.limit_notes(SETTINGS)
        self.assertEqual(warn, [])
        self.assertIn("the daily max loss covers 3 full-size losing trades at $50.00 each", notes)
        warn, _ = bud.limit_notes(dict(SETTINGS, daily_max_loss=40.0))
        self.assertTrue(any("every full-size ticket will be blocked" in w for w in warn))
        warn, _ = bud.limit_notes(dict(SETTINGS, daily_max_loss=2000.0))
        self.assertTrue(any("8.0% of the $25,000.00 account" in w for w in warn))

    def test_set_saves_and_show(self):
        code, out, _ = run_main("set", "--daily-max-loss", "$200", "--max-trades", "4")
        self.assertEqual(code, 0)
        self.assertIn("was $150.00", out)
        s = tc.load_settings()
        self.assertEqual((s["daily_max_loss"], s["max_trades_per_day"]), (200.0, 4))
        code, out, _ = run_main("set", "--json")
        self.assertFalse(json.loads(out)["changed"])

    def test_cannot_loosen_while_stopped_without_force(self):
        tc.save_ledger(ledger(*[trade(str(i), pnl=-10, closed=f"{DAY}T10:0{i}:00-04:00") for i in range(3)]))
        with self.assertRaises(ValueError) as cm:
            run_main("set", "--max-losses", "5")
        self.assertIn("--force", str(cm.exception))
        self.assertEqual(tc.load_settings()["max_consecutive_losses"], 3)
        self.assertEqual(run_main("set", "--max-trades", "3")[0], 0)        # tightening is fine
        code, out, _ = run_main("set", "--max-losses", "5", "--force")
        self.assertEqual(code, 0)
        self.assertIn("! --force: loosened tilt guard (losses in a row) while STOP TRADING was active", out)
        self.assertEqual(tc.load_settings()["max_consecutive_losses"], 5)

    def test_set_repairs_a_broken_value(self):
        tc.save_json("settings.json", {"daily_max_loss": "lots"})
        with self.assertRaises(tc.StateError):
            run_main("status")
        self.assertEqual(run_main("set", "--daily-max-loss", "150")[0], 0)
        self.assertEqual(run_main("status")[0], 0)

    def test_parser_rejects_bad_values_with_exit_1(self):
        for argv in (["set", "--max-trades", "0"], ["set", "--max-losses", "2.5"],
                     ["set", "--daily-max-loss", "-5"], ["check", "--risk", "abc"],
                     ["status", "--date", "10/29/2026"], ["history", "--days", "0"], ["--live"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                    bud.main(argv)
                self.assertEqual(cm.exception.code, 1)


class CheckCommandTests(TempHomeTestCase):
    NOW = f"{DAY}T11:00"

    def test_risk_sources(self):
        code, out, _ = run_main("check", "--entry", "364.18", "--stop", "358.32", "--qty", "8", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["new_risk"], 46.88)
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04,
                      "sizing": {"shares": 30, "entry": 364.18, "stop": 358.32}})
        code, out, _ = run_main("check", "--symbol", "tsla", "--json")
        r = json.loads(out)
        self.assertEqual((code, r["new_risk"], r["ok"], r["max_shares_now"]), (3, 175.8, False, 25))
        code, out, _ = run_main("check", "--symbol", "TSLA", "--qty", "8")
        self.assertEqual(code, 0)
        self.assertIn("BUDGET CHECK · TSLA · new risk $46.88 -> OK", out)

    def test_same_symbol_already_open_is_noted(self):
        tc.save_ledger(ledger(trade("TR-1", status="open", symbol="TSLA", qty=8, entry=364.18, stop=358.32)))
        code, out, _ = run_main("check", "--symbol", "TSLA", "--risk", "46.88", "--json")
        r = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(any("already has an open paper position" in n and "TR-1" in n for n in r["notes"]))
        self.assertEqual(r["after"]["open_risk"], 93.76)

    def test_risk_source_errors(self):
        for argv, msg in ((["check"], "missing --entry, --stop, --qty"),
                          (["check", "--risk", "10", "--qty", "3"], "not both"),
                          (["check", "--symbol", "NVDA"], "no saved plan for NVDA"),
                          (["check", "--entry", "10", "--stop", "10", "--qty", "1"], "must differ")):
            with self.subTest(argv=argv):
                with self.assertRaises(ValueError) as cm:
                    run_main(*argv)
                self.assertIn(msg, str(cm.exception))
        tc.save_plan({"symbol": "AMD", "side": "long", "entry": 160, "stop": 158})
        with self.assertRaises(ValueError) as cm:
            run_main("check", "--symbol", "AMD")
        self.assertIn("risk.py size", str(cm.exception))

    def test_future_date(self):
        with self.assertRaises(ValueError):
            run_main("status", "--date", "2026-11-02")


class MarkTests(TempHomeTestCase):
    NOW = f"{DAY}T11:00"

    def test_mark_math(self):
        pos = [bud.position_row(dict(trade("L", status="open", qty=8, entry=364.18, stop=358.32), side="long")),
               bud.position_row(dict(trade("S", status="open", side="short", entry=50.0, stop=51.0, qty=100,
                                           symbol="Y"), target=48.0))]
        rows = bud.mark_positions(pos, {"X": 357.10, "Y": 47.5})
        self.assertEqual((rows[0]["unrealized"], rows[0]["r_now"]), (-56.64, -1.21))
        self.assertTrue(rows[0]["note"].startswith("THROUGH STOP"))
        self.assertEqual((rows[1]["unrealized"], rows[1]["r_now"]), (250.0, 2.5))
        self.assertTrue(rows[1]["note"].startswith("past target"))
        self.assertEqual(bud.mark_positions(pos, {})[0]["note"], "no price")

    def test_fetch_marks_partial_and_total_failure(self):
        tc.save_ledger(ledger(trade("a", status="open", symbol="TSLA"), trade("b", status="open", symbol="NVDA")))
        bars = bars_from_closes(DAY, [100.5, 101.0], minutes=1, start="10:58")
        with fake_bars({("TSLA", "1m"): bars}):
            code, out, _ = run_main("status", "--mark", "--json")
        r = json.loads(out)
        self.assertEqual(r["marks"]["prices"], {"TSLA": 101.0})
        self.assertEqual(len(r["marks"]["errors"]), 1)
        self.assertEqual(r["marks"]["unrealized_total"], 50.0)
        with fake_bars({}):
            with self.assertRaises(tc.FetchError):
                run_main("status", "--mark")
        with self.assertRaises(ValueError):
            run_main("status", "--mark", "--date", "2026-10-29")

    def test_through_a_stop_past_the_entry_claims_no_planned_risk(self):
        tc.save_ledger(ledger(trade("L", status="open", symbol="TSLA", entry=100.0, stop=103.0, qty=10)))
        with fake_bars({("TSLA", "1m"): bars_from_closes(DAY, [102.5], minutes=1, start="10:58")}):
            r = json.loads(run_main("status", "--mark", "--json")[1])
        msg = [c for c in r["cautions"] if "through its stop" in c]
        self.assertEqual(len(msg), 1, r["cautions"])
        self.assertIn("risks no loss", msg[0])
        self.assertNotIn("$0.00 planned risk", msg[0])


class CliTests(TempHomeTestCase):
    NOW = f"{DAY}T11:05"

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fixtures"
        self.fx.mkdir()
        self.env = {"DAYTRADER_FIXTURES": str(self.fx)}   # never the real network
        tc.save_ledger(ledger(
            trade("TRADE-20261030-001", status="open", symbol="TSLA", qty=8, entry=364.18, stop=358.32,
                  opened=f"{DAY}T10:16:00-04:00"),
            trade("TRADE-20261030-002", pnl=-40, symbol="META", opened=f"{DAY}T09:45:00-04:00",
                  closed=f"{DAY}T10:05:00-04:00")))

    def cli(self, *args):
        return helpers.run_cli(SCRIPT, *args, env=self.env)

    def test_status_card(self):
        p = self.cli("status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("RISKED SO FAR · Fri 2026-10-30 · 11:05 ET (market open)", p.stdout)
        self.assertIn("STATUS: OPEN FOR TRADING", p.stdout)
        self.assertIn("$86.88 of $150.00 used (57.9%) · $63.12 left", p.stdout)
        self.assertIn("Trades today   2 of 5", p.stdout)
        self.assertIn("TRADE-20261030-001  TSLA", p.stdout)
        self.assertIn(tc.DISCLAIMER, p.stdout)
        j = json.loads(self.cli("--json").stdout)       # no command = status
        self.assertEqual(j["status"], "OPEN FOR TRADING")
        self.assertEqual(j["room_for_new_risk"], 63.12)
        for key in ("loss_budget", "open_risk", "limits_hit", "cautions", "positions", "working_orders",
                    "pending_tickets", "trades_today", "loss_streak", "realized_pnl", "as_of"):
            self.assertIn(key, j)

    def test_check_exit_codes(self):
        ok = self.cli("check", "--risk", "46.88")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("-> OK", ok.stdout)
        blocked = self.cli("check", "--risk", "63.13", "--json")
        self.assertEqual(blocked.returncode, 3, blocked.stderr)
        j = json.loads(blocked.stdout)
        self.assertEqual((j["ok"], j["verdict"], j["exit_code"]), (False, "BLOCKED", 3))
        self.assertTrue(j["reasons"])
        bad = self.cli("check", "--risk", "-1")
        self.assertEqual(bad.returncode, 1)
        self.assertNotIn("Traceback", bad.stderr)

    def test_mark_with_fixture_and_offline_failure(self):
        bars = bars_from_closes(DAY, [360.0, 358.0, 357.10], minutes=1, start="11:02")
        write_fixture(self.fx, yahoo_chart_url("TSLA", "1m", "1d"), yahoo_payload(bars, "TSLA"))
        p = self.cli("status", "--mark")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("STATUS: CAUTION", p.stdout)
        self.assertIn("THROUGH STOP", p.stdout)
        self.assertIn("-$56.64", p.stdout)
        for f in self.fx.iterdir():
            f.unlink()
        p = self.cli("status", "--mark")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, "")
        lines = p.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, p.stderr)
        self.assertTrue(lines[0].startswith("data error: TSLA:"), lines[0])
        self.assertIn("finance.yahoo.com", lines[0])

    def test_set_and_history(self):
        p = self.cli("set", "--daily-max-loss", "100")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("was $150.00", p.stdout)
        self.assertIn("STATUS: CAUTION", self.cli("status").stdout)     # $86.88 of $100 = 87%
        self.assertEqual(self.cli("set", "--daily-max-loss", "80").returncode, 0)
        p = self.cli("status")
        self.assertIn("STOP TRADING", p.stdout)
        self.assertIn("loss budget full", p.stdout)
        p = self.cli("set", "--daily-max-loss", "150")
        self.assertEqual(p.returncode, 1)
        self.assertIn("--force", p.stderr)
        h = self.cli("history", "--days", "3")
        self.assertEqual(h.returncode, 0, h.stderr)
        self.assertIn("2026-10-30  Fri", h.stdout)
        self.assertIn("1 still open", h.stdout)

    def test_corrupt_ledger_is_one_line_error(self):
        (self.home / "ledger.json").write_text("{nope")
        p = self.cli("status")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.stderr.startswith("error: "))
        self.assertNotIn("Traceback", p.stderr)


class ReviewRegressionTests(TempHomeTestCase):
    """Defects found in review: each test failed before its fix."""
    NOW = f"{DAY}T11:05"

    def test_unreadable_stamps_fail_closed(self):
        # A $160 realized loss with a close time the budget cannot read must not vanish from today.
        for bad in (trade("a", pnl=-160, closed="10/30/2026 10:00"),
                    trade("b", pnl=-160, closed=1793368800),
                    trade("c", status="open", opened="this morning")):
            with self.subTest(bad=bad):
                with self.assertRaises(tc.StateError) as cm:
                    bud.normalize_ledger(ledger(bad))
                self.assertIn(bad["id"], str(cm.exception))
        # A stamp Python cannot parse but that starts with an ISO date keeps its date.
        ok = trade("d", pnl=-20, closed=f"{DAY}T10:00:00.1234567-04:00")
        self.assertEqual(status_of(ledger(ok))["realized_pnl"], -20)
        tc.save_ledger(ledger(trade("a", pnl=-160, closed="10/30/2026 10:00")))
        code = None
        with self.assertRaises(tc.StateError):
            code = run_main("check", "--risk", "40")
        self.assertIsNone(code)

    def test_non_object_ledger_or_settings_is_a_state_error(self):
        for body in ('[{"id": 1}]', '"abc"', "5"):
            with self.subTest(ledger=body):
                (self.home / "ledger.json").write_text(body)
                with self.assertRaises(tc.StateError):
                    run_main("status")
        (self.home / "ledger.json").unlink()
        (self.home / "settings.json").write_text("[1, 2]")
        for argv in (["status"], ["check", "--risk", "10"], ["set", "--max-trades", "3"], ["history"]):
            with self.subTest(settings=argv):
                with self.assertRaises(tc.StateError):
                    run_main(*argv)
        p = helpers.run_cli(SCRIPT, "check", "--risk", "10")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        (self.home / "settings.json").unlink()
        tc.save_json("plans/TSLA.json", [1])        # a plan that is not an object
        with self.assertRaises(tc.StateError):
            run_main("check", "--symbol", "TSLA")

    def test_caution_threshold_is_not_rounded_up(self):
        settings = dict(SETTINGS, daily_max_loss=1000.0)     # room stays 150 (max open risk): no other caution
        self.assertEqual(status_of(ledger(trade("a", pnl=-749.6)), settings)["status"], bud.OPEN)   # 74.96%
        self.assertEqual(status_of(ledger(trade("a", pnl=-750)), settings)["status"], bud.CAUTION)  # 75.00%

    def test_room_is_checkable_with_sub_cent_limits(self):
        rng = random.Random(11)
        for _ in range(400):
            trades = []
            for i in range(rng.randint(0, 6)):
                if rng.random() < 0.3:
                    trades.append(trade(f"o{i}", status="open", qty=rng.randint(1, 80),
                                        opened=f"{DAY}T10:{10 + i}:00-04:00"))
                else:
                    trades.append(trade(f"c{i}", pnl=round(rng.uniform(-70, 60), 2),
                                        opened=f"{DAY}T10:{10 + i}:00-04:00", closed=f"{DAY}T11:{10 + i}:00-04:00"))
            settings = dict(SETTINGS, daily_max_loss=rng.choice([150.005, 99.999, 120.0049]),
                            max_open_risk=rng.choice([75.005, 150.0, 140.333]),
                            max_trades_per_day=9, max_consecutive_losses=9)
            led = ledger(*trades)
            s = status_of(led, settings)
            room = s["room_for_new_risk"]
            if s["status"] == bud.STOP:
                self.assertEqual(room, 0)
                self.assertFalse(check_of(led, 0.01, settings)["ok"])
            else:
                self.assertGreater(room, 0, s)
                self.assertTrue(check_of(led, room, settings)["ok"], (s, room))
                self.assertFalse(check_of(led, room + 0.01, settings)["ok"], (s, room))

    def test_sub_penny_risk_is_not_rounded_down(self):
        tc.save_ledger(ledger(trade("a", pnl=-40), trade("o", status="open", qty=8, entry=364.18, stop=358.32)))
        # 0.0007/share x 90,175 = $63.1225 of risk; only $63.12 of room is left. The broker's own
        # check (tc.budget_check on the true risk) blocks it, so the budget must not say OK.
        self.assertFalse(tc.budget_check(abs(0.5007 - 0.5) * 90175)["ok"])
        code, out, _ = run_main("check", "--entry", "0.5007", "--stop", "0.5", "--qty", "90175", "--json")
        r = json.loads(out)
        self.assertEqual((code, r["ok"]), (3, False))
        # The share count it offers instead really fits.
        n = r["max_shares_now"]
        self.assertLessEqual(abs(0.5007 - 0.5) * n, 63.12 + 1e-9)
        self.assertEqual(run_main("check", "--entry", "0.5007", "--stop", "0.5", "--qty", str(n))[0], 0)
        text = run_main("check", "--entry", "0.5007", "--stop", "0.5", "--qty", "90175")[1]
        self.assertIn(f"(= {n} shares at $0.0007/share risk)", text)
        self.assertIn("new risk $63.1225 -> BLOCKED", text)       # not "$63.12", the room it exceeds
        self.assertEqual(r["new_risk"], 63.1225)

    def test_zero_limit_can_be_repaired_without_force(self):
        # A 0 limit (hand edit / another tool; `set` itself refuses 0) is STOP TRADING, and its reason
        # says to fix it with `budget.py set`. That fix must not then be refused as "loosening".
        for key, argv in (("max_trades_per_day", ["--max-trades", "5"]),
                          ("daily_max_loss", ["--daily-max-loss", "150"])):
            with self.subTest(key=key):
                tc.save_json("settings.json", {key: 0})
                self.assertEqual(run_main("status", "--json")[1].count('"STOP TRADING"'), 1)
                self.assertEqual(run_main("set", *argv)[0], 0)
                self.assertEqual(json.loads(run_main("status", "--json")[1])["status"], bud.OPEN)
        # Loosening a real limit while stopped is still refused.
        tc.save_json("settings.json", {"max_trades_per_day": 0})
        tc.save_ledger(ledger(*[trade(str(i), pnl=-10, closed=f"{DAY}T10:0{i}:00-04:00") for i in range(3)]))
        with self.assertRaises(ValueError):
            run_main("set", "--max-trades", "5", "--max-losses", "6")

    def test_history_flags_an_intraday_max_loss(self):
        d = "2026-10-29"
        led = ledger(trade("x", pnl=-160, qty=200, opened=f"{d}T10:00:00-04:00", closed=f"{d}T10:30:00-04:00"),
                     trade("y", pnl=50, opened=f"{d}T11:00:00-04:00", closed=f"{d}T11:30:00-04:00"))
        h = bud.history(bud.normalize_ledger(led), SETTINGS, DAY, 5)
        row = h["rows"][0]
        self.assertEqual((row["realized_pnl"], row["realized_low"]), (-110.0, -160.0))
        self.assertIn("MAX LOSS", row["flags"])
        self.assertEqual(h["totals"]["limit_days"], 1)

    def test_past_day_card_labels_are_spaced(self):
        tc.save_ledger(ledger(trade("a", pnl=-30, opened="2026-10-29T10:00:00-04:00",
                                    closed="2026-10-29T10:30:00-04:00")))
        out = run_main("status", "--date", "2026-10-29")[1]
        self.assertRegex(out, r"Risked that day +\$50\.00 across 1 trade")
        self.assertRegex(out, r"Trades that day +1 of 5")

    def test_stale_working_order_counts_risk_but_not_a_trade_today(self):
        old = {"id": "TKT-OLD", "symbol": "AMZN", "side": "long", "qty": 10, "entry": 220.0, "stop": 218.0,
               "status": "working", "confirmed_at": "2026-10-29T15:30:00-04:00"}
        new = dict(old, id="TKT-NEW", confirmed_at=f"{DAY}T10:30:00-04:00")
        s = status_of(ledger(tickets=[old]))
        self.assertEqual((s["trades_today"], s["open_risk"]["working_orders"]), (0, 20.0))
        s = status_of(ledger(tickets=[old, new]))
        self.assertEqual((s["trades_today"], s["open_risk"]["working_orders"]), (1, 40.0))

    def test_blocked_text_claims_no_certainty_about_the_broker(self):
        work = {"id": "TKT-OLD", "symbol": "AMZN", "side": "long", "qty": 60, "entry": 220.0, "stop": 218.0,
                "status": "working", "confirmed_at": "2026-10-29T15:30:00-04:00"}
        tc.save_ledger(ledger(tickets=[work]))
        code, out, _ = run_main("check", "--risk", "40")
        self.assertEqual(code, 3)
        self.assertNotIn("will refuse it too", out)
        self.assertIn("Do not open a ticket", out)


class WorkingOrderHistoryTests(TempHomeTestCase):
    """Regression: a confirmed-but-unfilled limit order was a trade in `status` but `history` said
    "No paper trades in this window"."""
    NOW = f"{DAY}T11:00"

    @staticmethod
    def working(tid="TKT-20261030-001", confirmed=f"{DAY}T10:22:00-04:00"):
        return {"id": tid, "symbol": "TSLA", "side": "long", "qty": 8, "type": "limit", "entry": 364.18,
                "stop": 358.32, "target": 370.04, "status": "working", "confirmed_at": confirmed}

    def test_todays_working_order_counts_as_a_trade_in_both(self):
        tc.save_ledger(ledger(tickets=[self.working()]))
        st = json.loads(run_main("status", "--json")[1])
        self.assertEqual(st["trades_today"], 1)
        h = json.loads(run_main("history", "--json")[1])
        self.assertEqual(len(h["rows"]), 1)
        row = h["rows"][0]
        self.assertEqual((row["date"], row["trades"], row["working"], row["still_open"], row["closed"]),
                         (DAY, st["trades_today"], 1, 0, 0))
        self.assertEqual(row["risked"], st["risked_today"])
        self.assertEqual((h["totals"]["trades"], h["totals"]["working"]), (1, 1))
        text = run_main("history")[1]
        self.assertNotIn("No paper trades", text)
        self.assertIn("1 working (not filled)", text)
        self.assertIn("Total: 1 trades (1 working, not filled)", text)

    def test_working_order_beside_a_filled_trade(self):
        tc.save_ledger(ledger(trade("TRADE-20261030-001", pnl=-40, opened=f"{DAY}T09:45:00-04:00"),
                              tickets=[self.working("TKT-20261030-002")]))
        st = json.loads(run_main("status", "--json")[1])
        row = json.loads(run_main("history", "--json")[1])["rows"][0]
        self.assertEqual((row["trades"], row["working"], row["losses"]), (2, 1, 1))
        self.assertEqual(row["trades"], st["trades_today"])

    def test_a_past_days_unsynced_working_order_matches_status_date(self):
        old = self.working("TKT-20261029-001", confirmed="2026-10-29T15:30:00-04:00")
        tc.save_ledger(ledger(tickets=[old]))
        past = json.loads(run_main("status", "--date", "2026-10-29", "--json")[1])
        row = json.loads(run_main("history", "--json")[1])["rows"][0]
        self.assertEqual((row["date"], row["trades"], row["working"], row["unsynced_working"]),
                         ("2026-10-29", past["trades_today"], 0, 1))
        self.assertIn("1 working never synced (run broker sync)", run_main("history")[1])


class DirectionalRiskTests(unittest.TestCase):
    def test_stop_past_the_entry_risks_nothing_and_rows_add_up(self):
        # tc.trade_initial_risk is directional: 0 once a stop has moved past the entry. The position rows
        # used |entry - stop| and so did not add up to the open-risk total.
        locked = trade("L", status="open", entry=100.0, stop=103.0, qty=10)
        short = trade("S", status="open", side="short", entry=50.0, stop=49.0, qty=10, symbol="Y")
        live = trade("R", status="open", entry=100.0, stop=99.0, qty=20, symbol="Z")
        s = status_of(ledger(locked, short, live))
        self.assertEqual([p["risk"] for p in s["positions"]], [0.0, 0.0, 20.0])
        self.assertEqual(s["open_risk"]["total"], 20.0)
        self.assertEqual(s["open_risk"]["positions"], sum(p["risk"] for p in s["positions"]))
        self.assertEqual(s["loss_budget"]["used"], 20.0)
        rows = bud.mark_positions(s["positions"][:1], {"X": 102.0})
        self.assertEqual((rows[0]["unrealized"], rows[0]["r_now"]), (20.0, None))   # no risk -> no R

    def test_working_ticket_needs_a_side(self):
        bad = {"id": "TKT-X", "symbol": "AMZN", "qty": 10, "entry": 220.0, "stop": 218.0, "status": "working"}
        with self.assertRaises(tc.StateError):
            bud.normalize_ledger(ledger(tickets=[bad]))


def btc(tid="TRADE-20261031-001", status="open", qty=0.01, entry=101234.56, stop=99000.0, **kw):
    """A crypto ledger trade as the broker writes it (UTC stamps, its own fee_pct)."""
    t = {"id": tid, "symbol": "BTC-USD", "market": "crypto", "side": "long", "qty": qty, "entry_price": entry,
         "stop": stop, "target": 104000.0, "status": status, "fee_pct": 0.6,
         "opened_at": "2026-10-31T01:00:00+00:00"}
    t.update(kw)
    return t


class CryptoBudgetTests(TempHomeTestCase):
    """The budget day follows the market checked (the UTC day for crypto); crypto fees count as risk."""
    NOW = "2026-10-31T02:00:00Z"     # Saturday 02:00 UTC = Friday 22:00 in New York

    def setUp(self):
        super().setUp()
        tc.save_ledger(ledger(
            trade("TRADE-20261030-001", pnl=-60, symbol="TSLA", opened=f"{DAY}T10:00:00-04:00",
                  closed=f"{DAY}T15:00:00-04:00"),
            btc(status="closed", pnl=-40.0, r_multiple=-1.79, exit_price=99000.0,
                closed_at="2026-10-31T01:30:00+00:00")))

    def json_of(self, *argv):
        code, out, err = run_main(*argv, "--json")
        return code, json.loads(out)

    def test_today_is_the_utc_day_for_crypto_and_the_et_day_for_stocks(self):
        code, j = self.json_of("status", "--market", "crypto")
        self.assertEqual((j["date"], j["realized_pnl"], j["closed_today"]), ("2026-10-31", -40.0, 1))
        code, j = self.json_of("status")                    # the stock day: both losses are on Friday 10-30 ET
        self.assertEqual((j["date"], j["realized_pnl"], j["closed_today"]), (DAY, -100.0, 2))
        code, out, _ = run_main("status", "--market", "crypto")
        self.assertIn("RISKED SO FAR · Sat 2026-10-31 · 02:00 UTC (market open)", out)
        code, out, _ = run_main("status")
        self.assertIn("RISKED SO FAR · Fri 2026-10-30 · 22:00 ET (market closed)", out)
        code, j = self.json_of("history", "--market", "crypto")
        self.assertEqual([(r["date"], r["realized_pnl"]) for r in j["rows"]], [(DAY, -60.0), ("2026-10-31", -40.0)])
        code, j = self.json_of("history")
        self.assertEqual([(r["date"], r["realized_pnl"]) for r in j["rows"]], [(DAY, -100.0)])

    def test_check_symbol_uses_its_market(self):
        code, j = self.json_of("check", "--symbol", "BTC-USD", "--risk", "100")      # $40 + $100 <= $150
        self.assertEqual((code, j["verdict"], j["market"], j["date"]), (0, "OK", "crypto", "2026-10-31"))
        code, j = self.json_of("check", "--symbol", "TSLA", "--risk", "100")         # $100 + $100 > $150
        self.assertEqual((code, j["verdict"], j["date"]), (bud.EXIT_BLOCKED, "BLOCKED", DAY))
        code, j = self.json_of("check", "--market", "crypto", "--risk", "100")
        self.assertEqual(j["verdict"], "OK")
        with self.assertRaises(ValueError):
            run_main("check", "--symbol", "BTC-USD", "--market", "us", "--risk", "10")

    def test_crypto_risk_includes_both_fills_fees(self):
        tc.save_ledger(ledger(btc(qty=0.02946375, entry=100000.0, stop=99500.0), tickets=[
            {"id": "TKT-20261031-002", "symbol": "BTC-USD", "side": "long", "qty": 0.01, "entry": 101250.0,
             "stop": 99000.0, "status": "pending", "expires_at": "2026-10-31T02:10:00+00:00"}]))
        code, j = self.json_of("status", "--market", "crypto")
        pos = j["positions"][0]
        # $14.73 of price risk + $35.27 of fees at the stop; the row still shows the real stop
        self.assertEqual((j["open_risk"]["total"], pos["risk"], pos["stop"], pos["fees_at_stop"]), (50.0, 50.0, 99500.0, 35.27))
        self.assertEqual(j["pending_tickets"][0]["risk"], 34.52)                    # $22.50 + $12.02 of fees
        code, j = self.json_of("check", "--symbol", "BTC-USD", "--entry", "100000", "--stop", "99500",
                               "--qty", "0.02946375")
        self.assertAlmostEqual(j["new_risk"], 50.0, places=3)
        self.assertEqual((j["fees_at_stop"], j["unit"], j["ok"]), (35.27, "BTC", True))
        self.assertTrue(any("includes $35.27 of paper fees at the stop" in n for n in j["notes"]))
        code, j = self.json_of("check", "--symbol", "BTC-USD", "--entry", "100000", "--stop", "99500", "--qty", "0.1")
        self.assertEqual((j["verdict"], j["max_shares_now"]), ("BLOCKED", 0.05892751))    # $100 left / $1,697 per BTC
        code, out, _ = run_main("check", "--symbol", "BTC-USD", "--entry", "100000", "--stop", "99500", "--qty", "0.1")
        self.assertIn("(= 0.05892751 BTC at $1,697.00/BTC risk incl. fees)", out)

    def test_closed_crypto_pnl_is_net_of_fees(self):
        norm = bud.normalize_ledger(ledger(btc(status="closed", exit_price=104000.0,
                                               closed_at="2026-10-31T01:30:00+00:00"),
                                           btc("B", status="closed", exit_price=104000.0, fees=12.31,
                                               closed_at="2026-10-31T01:40:00+00:00")))
        self.assertEqual([t["pnl"] for t in norm["trades"]], [15.34, 15.34])         # 27.65 gross - 12.31 fees
        self.assertEqual(norm["trades"][0]["r_multiple"], 0.69)                       # R = the stop distance

    def test_mark_is_net_of_fees(self):
        rows = bud.mark_positions([{"id": "T", "symbol": "BTC-USD", "side": "long", "qty": 0.01, "entry": 101234.56,
                                    "stop": 99000.0, "target": 104000.0, "risk": 34.36, "fee_pct": 0.6}],
                                  {"BTC-USD": 102000.0})
        # gross 7.65 - fees 0.6% x (101234.56 + 102000) x 0.01 = 12.19; R over the 22.35 price risk
        self.assertEqual((rows[0]["unrealized"], rows[0]["r_now"], rows[0]["note"]), (-4.54, -0.2, ""))

    def test_cli_mark_reads_the_coinbase_ticker(self):
        tc.save_ledger(ledger(btc()))
        fx = self.home / "fx"
        write_coinbase_quote(fx, "BTC-USD", 102000.0, when="2026-10-31T01:59:00Z")
        p = helpers.run_cli(SCRIPT, "status", "--market", "crypto", "--mark", "--json",
                            env={"DAYTRADER_FIXTURES": str(fx)})
        self.assertEqual(p.returncode, 0, p.stderr)
        j = json.loads(p.stdout)
        self.assertEqual((j["date"], j["marks"]["prices"], j["marks"]["unrealized_total"]),
                         ("2026-10-31", {"BTC-USD": 102000.0}, -4.54))
        self.assertEqual(j["open_risk"]["total"], 34.36)


class SkillDocTests(unittest.TestCase):
    def test_every_documented_command_parses(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        cmds = re.findall(r"python3 scripts/budget\.py([^\n`#]*)", text)
        self.assertGreaterEqual(len(cmds), 8)
        parser = bud.build_parser()
        for c in cmds:
            argv = shlex.split(c.strip())
            with self.subTest(cmd=c):
                try:
                    parser.parse_args(argv)
                except SystemExit as e:   # pragma: no cover - fails the test with the command shown
                    self.fail(f"SKILL.md command does not parse: budget.py {c.strip()} ({e})")

    def test_required_lines(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder "
                      "(`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.",
                      text)
        self.assertTrue(text.startswith("---\nname: budget\ndescription:"))
        desc = text.split("---")[1]
        self.assertIn("Day-trading desk:", desc)
        self.assertLessEqual(len(" ".join(desc.split("description:", 1)[1].split())), 1024)
        self.assertIn('"how am I doing today?"', " ".join(desc.split()))         # routing: today = budget
        reading = text.split("## Reading the output", 1)[1].split("\n## ", 1)[0]
        self.assertIn("analyst.py review", reading)                            # the longer-term picture
        rules = " ".join(text.split("## Rules", 1)[1].split())
        for phrase in ("may be gone next conversation", "an empty ledger reads OPEN FOR TRADING",
                       "`ledger.json` and `settings.json` as downloads"):    # sandbox: state can vanish
            self.assertIn(phrase, rules)
        self.assertLessEqual(len(text.splitlines()), 150)


if __name__ == "__main__":
    unittest.main()
