"""Tests for the paper broker skill (skills/broker/scripts/broker.py). Offline only."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock

# Works both with `unittest discover -s tests` and `python3 -m unittest tests.test_broker`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (TempHomeTestCase, bars_from_closes, et_ts, fake_bars, load_skill_module, run_cli,
                     skill_script, tc, write_fixture, yahoo_chart_url, yahoo_payload)  # noqa: E402

broker = load_skill_module("broker", "broker")
SCRIPT = skill_script("broker", "broker.py")
DAY = "2026-10-30"          # a Friday (EDT)


def bar(hhmm, o, h, l, c, day=DAY):
    return tc.Bar(et_ts(day, hhmm), o, h, l, c, 1000.0)


def ts(hhmm, day=DAY):
    return float(et_ts(day, hhmm))


def quote_bars(price, day=DAY):
    """1-minute bars whose last close is `price` (tc.quote reads it through fake_bars)."""
    return bars_from_closes(day, [price], start="10:15", minutes=1)


def flat_session(price=100.0, n=78, day=DAY, wick=0.1):
    return bars_from_closes(day, [price] * n, wick=wick)


# ---------------------------------------------------------------------------
# Pure fill / exit rules
# ---------------------------------------------------------------------------

class BarExitTests(unittest.TestCase):
    def test_long_stop_on_touch_and_target_on_touch(self):
        self.assertEqual(broker.bar_exit("long", 100, 99, 102, bar("10:20", 100, 100.5, 99.0, 100), False),
                         ("stop", 99, False))
        self.assertEqual(broker.bar_exit("long", 100, 99, 102, bar("10:20", 100, 102.0, 99.5, 101), False),
                         ("target", 102.0, False))
        self.assertIsNone(broker.bar_exit("long", 100, 99, 102, bar("10:20", 100, 101.9, 99.1, 101), False))

    def test_same_bar_ambiguity_counts_as_stop(self):
        self.assertEqual(broker.bar_exit("long", 100, 99, 102, bar("10:20", 100, 103, 98, 101), False),
                         ("stop", 99, True))
        self.assertEqual(broker.bar_exit("short", 100, 101, 98, bar("10:20", 100, 102, 97, 99), False),
                         ("stop", 101, True))

    def test_open_beyond_a_level_decides_it(self):
        # gap through the stop: filled at the (worse) open
        self.assertEqual(broker.bar_exit("long", 100, 99, 102, bar("10:20", 98.5, 99.5, 98, 99), False),
                         ("stop", 98.5, False))
        # opening above the target came first -> target at the open, not ambiguous
        self.assertEqual(broker.bar_exit("long", 100, 99, 102, bar("10:20", 102.5, 103, 98, 99), False),
                         ("target", 102.5, False))
        self.assertEqual(broker.bar_exit("short", 100, 101, 98, bar("10:20", 101.5, 102, 100, 101), False),
                         ("stop", 101.5, False))

    def test_fill_bar_checks_only_the_stop(self):
        self.assertIsNone(broker.bar_exit("long", 100, 99, 102, bar("10:15", 100, 103, 99.5, 101), True))
        self.assertEqual(broker.bar_exit("long", 100, 99, 102, bar("10:15", 100, 103, 98, 101), True),
                         ("stop", 99, False))
        # filled already through the stop (gap fill) -> out at the fill price, not a fake profit
        self.assertEqual(broker.bar_exit("long", 98.5, 99, 102, bar("10:15", 98.5, 99, 98, 98.6), True),
                         ("stop", 98.5, False))
        self.assertEqual(broker.bar_exit("short", 101.5, 101, 98, bar("10:15", 101.5, 102, 101, 101.8), True),
                         ("stop", 101.5, False))

    def test_no_target(self):
        self.assertIsNone(broker.bar_exit("long", 100, 99, None, bar("10:20", 100, 150, 99.5, 120), False))


class WalkPositionTests(unittest.TestCase):
    def test_time_stop_closes_at_close_of_bar_ending_at_1555(self):
        bars = broker.normalize_bars(flat_session())
        r = broker.walk_position("long", 100, 99, 102, ts("10:16"), bars, ts("17:00"), 955)
        self.assertEqual((r["status"], r["reason"], r["price"]), ("closed", "eod", 100.0))
        self.assertEqual(tc.et_hhmm(r["ts"]), "15:55")   # out at the close of the 15:50-15:55 bar

    def test_custom_time_stop(self):
        bars = broker.normalize_bars(flat_session())
        r = broker.walk_position("short", 100, 101, 98, ts("10:16"), bars, ts("17:00"), 690)
        self.assertEqual((r["reason"], tc.et_hhmm(r["ts"])), ("eod", "11:30"))

    def test_only_completed_bars_after_the_fill_count(self):
        closes = [100] * 10 + [98.0]                    # 10:20 bar falls to 98 (through the 99 stop)
        bars = broker.normalize_bars(bars_from_closes(DAY, closes))
        before = broker.walk_position("long", 100, 99, 102, ts("10:16"), bars, ts("10:24"), 955)
        self.assertEqual(before["status"], "open")       # the 10:20 bar is still forming at 10:24
        self.assertEqual(tc.et_hhmm(before["checked_through"]), "10:20")
        after = broker.walk_position("long", 100, 99, 102, ts("10:16"), bars, ts("10:25"), 955)
        self.assertEqual((after["reason"], after["price"]), ("stop", 99.0))
        # a stop touched BEFORE the fill bar is ignored
        early = broker.normalize_bars([bar("09:30", 100, 100, 95, 100)] + bars_from_closes(DAY, [100] * 3, start="09:35"))
        r = broker.walk_position("long", 100, 99, 102, ts("09:40"), early, ts("09:50"), 955)
        self.assertEqual(r["status"], "open")

    def test_target_crossed_in_the_fill_bar_exits_at_the_target_not_a_better_open(self):
        # Regression: the fill bar skips the target; when it then CLOSED beyond the target, the next bar's
        # open was booked as a "gap" exit (102.60) although the resting target sold at 102.00 on the way up.
        bars = broker.normalize_bars([bar("10:15", 99.9, 102.7, 99.8, 102.6), bar("10:20", 102.6, 103, 102.4, 102.8)])
        r = broker.walk_position("long", 100, 99, 102, ts("10:16"), bars, ts("10:30"), 955)
        self.assertEqual((r["reason"], r["price"], tc.et_hhmm(r["ts"])), ("target", 102.0, "10:25"))
        bars = broker.normalize_bars([bar("10:15", 100.1, 100.2, 97.3, 97.4), bar("10:20", 97.4, 97.6, 97.0, 97.2)])
        r = broker.walk_position("short", 100, 101, 98, ts("10:16"), bars, ts("10:30"), 955)
        self.assertEqual((r["reason"], r["price"]), ("target", 98.0))
        # a genuine gap (the previous bar closed short of the target) still exits at the better open
        bars = broker.normalize_bars([bar("10:15", 99.9, 100.5, 99.8, 100.2), bar("10:20", 102.6, 103, 102.4, 102.8)])
        r = broker.walk_position("long", 100, 99, 102, ts("10:16"), bars, ts("10:30"), 955)
        self.assertEqual((r["reason"], r["price"]), ("target", 102.6))

    def test_early_close_day(self):
        bars = broker.normalize_bars(flat_session(n=42))   # 09:30..12:55 (half day)
        r = broker.walk_position("long", 100, 99, 102, ts("10:16"), bars, ts("12:54"), 955, session_end=780)
        self.assertEqual(r["status"], "open")             # the 12:50 bar is still forming
        r = broker.walk_position("long", 100, 99, 102, ts("10:16"), bars, ts("12:56"), 955, session_end=780)
        self.assertEqual((r["reason"], tc.et_hhmm(r["ts"])), ("eod", "12:55"))
        # without Yahoo's trading period: closed at the last bar once the session is over
        r = broker.walk_position("long", 100, 99, 102, ts("10:16"), bars, ts("10:00", "2026-11-02"), 955)
        self.assertEqual((r["reason"], tc.et_hhmm(r["ts"])), ("eod", "13:00"))
        self.assertIn("last bar", r["note"])

    def test_day_over_without_bars_is_an_issue(self):
        r = broker.walk_position("long", 100, 99, 102, ts("15:54"), [], ts("10:00", "2026-11-02"), 955)
        self.assertEqual(r["status"], "issue")

    def test_bars_from_later_days_are_not_used(self):
        nxt = bars_from_closes("2026-11-02", [90.0, 80.0])
        r = broker.walk_position("long", 100, 99, 102, ts("10:16"), broker.normalize_bars(flat_session(n=20) + nxt),
                                 ts("12:00"), 955)
        self.assertEqual(r["status"], "open")


class WalkWorkingTests(unittest.TestCase):
    def test_touch_is_not_a_fill_trade_through_is(self):
        bars = broker.normalize_bars([bar("10:20", 100, 100.2, 99.5, 99.8), bar("10:25", 99.8, 99.9, 99.4, 99.6)])
        r = broker.walk_working("long", 99.5, ts("10:16"), bars, ts("10:26"), 955)
        self.assertEqual(r["status"], "working")
        r = broker.walk_working("long", 99.5, ts("10:16"), bars, ts("10:31"), 955)
        self.assertEqual((r["status"], r["price"], tc.et_hhmm(r["ts"])), ("filled", 99.5, "10:25"))

    def test_gap_through_limit_fills_at_open(self):
        bars = broker.normalize_bars([bar("10:20", 99.0, 99.2, 98.8, 99.1)])
        self.assertEqual(broker.walk_working("long", 99.5, ts("10:16"), bars, ts("10:30"), 955)["price"], 99.0)
        bars = broker.normalize_bars([bar("10:20", 101.0, 101.2, 100.8, 101.1)])
        self.assertEqual(broker.walk_working("short", 100.5, ts("10:16"), bars, ts("10:30"), 955)["price"], 101.0)

    def test_bar_in_progress_at_confirmation_is_skipped(self):
        bars = broker.normalize_bars([bar("10:15", 100, 100.1, 99.0, 99.9), bar("10:20", 99.9, 100.0, 99.6, 99.8)])
        r = broker.walk_working("long", 99.5, ts("10:16"), bars, ts("10:40"), 955)
        self.assertEqual(r["status"], "working")
        # confirmed exactly on a bar boundary: that bar counts
        r = broker.walk_working("long", 99.5, ts("10:15"), bars, ts("10:40"), 955)
        self.assertEqual(r["status"], "filled")

    def test_limit_crossed_after_confirmation_inside_the_confirm_bar(self):
        # Regression: the bar in progress at confirmation was ignored entirely. Confirmed at 10:16 with the
        # market above the 99.50 buy limit, that bar then CLOSED at 99.00: the resting order bought at 99.50
        # on the way down - it was booked at the next bar's better open (99.00) instead.
        bars = broker.normalize_bars([bar("10:15", 100.0, 100.1, 98.9, 99.0), bar("10:20", 99.0, 99.2, 98.8, 99.1)])
        r = broker.walk_working("long", 99.5, ts("10:16"), bars, ts("10:30"), 955)
        self.assertEqual((r["status"], r["price"], tc.et_hhmm(r["ts"])), ("filled", 99.5, "10:16"))
        bars = broker.normalize_bars([bar("10:15", 100.0, 101.2, 99.9, 101.0), bar("10:20", 101.0, 101.2, 100.8, 101.1)])
        r = broker.walk_working("short", 100.5, ts("10:16"), bars, ts("10:30"), 955)
        self.assertEqual((r["status"], r["price"]), ("filled", 100.5))
        # not yet complete -> no fill yet; closed back above the limit -> its low may predate the order: no fill
        bars = broker.normalize_bars([bar("10:15", 100.0, 100.1, 98.9, 99.0)])
        self.assertEqual(broker.walk_working("long", 99.5, ts("10:16"), bars, ts("10:19"), 955)["status"], "working")
        bars = broker.normalize_bars([bar("10:15", 100.0, 100.1, 98.9, 99.9)])
        self.assertEqual(broker.walk_working("long", 99.5, ts("10:16"), bars, ts("10:30"), 955)["status"], "working")

    def test_day_order_expires_at_time_stop(self):
        bars = broker.normalize_bars(flat_session())
        r = broker.walk_working("long", 90, ts("10:16"), bars, ts("15:59"), 955)
        self.assertEqual(r["status"], "working")
        r = broker.walk_working("long", 90, ts("10:16"), bars, ts("16:00"), 955)
        self.assertEqual((r["status"], tc.et_hhmm(r["ts"])), ("expired", "15:55"))
        r = broker.walk_working("long", 90, ts("10:16"), bars, ts("09:00", "2026-11-02"), 955)
        self.assertEqual(r["status"], "expired")
        # bars starting at/after the time stop can't fill it
        late = broker.normalize_bars([bar("15:55", 100, 100, 80, 85)])
        self.assertEqual(broker.walk_working("long", 90, ts("15:52"), late, ts("16:30"), 955)["status"], "expired")


class NormalizeAndHelpersTests(unittest.TestCase):
    def test_merges_live_duplicate_and_drops_extended_hours(self):
        raw = [bar("09:25", 1, 1, 1, 1), bar("10:15", 100, 101, 99.5, 100.5),
               tc.Bar(et_ts(DAY, "10:15") + 163, 100.5, 101.5, 100.2, 101.2, 10)]
        got = broker.normalize_bars(raw)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0].ts, got[0].open, got[0].high, got[0].low, got[0].close),
                         (et_ts(DAY, "10:15"), 100, 101.5, 99.5, 101.2))

    def test_price_rounding_and_subpenny(self):
        self.assertEqual(broker.px(364.175), 364.18)
        self.assertEqual(broker.px(0.12345), 0.1235)
        self.assertTrue(broker.sub_penny(364.183))
        self.assertFalse(broker.sub_penny(364.18))
        self.assertFalse(broker.sub_penny(0.1234))

    def test_session_end_from_trading_periods(self):
        meta = {"tradingPeriods": [[{"start": et_ts(DAY, "09:30"), "end": et_ts(DAY, "13:00")}]]}
        self.assertEqual(broker.session_end_minute(meta, DAY), 780)
        self.assertIsNone(broker.session_end_minute({}, DAY))
        self.assertEqual(broker.effective_time_stop(955, 780), 775)
        self.assertEqual(broker.effective_time_stop(955, None), 955)


# ---------------------------------------------------------------------------
# Commands (in-process, isolated $DAYTRADER_HOME)
# ---------------------------------------------------------------------------

class BrokerCase(TempHomeTestCase):
    NOW = f"{DAY}T10:15"

    def run_main(self, *argv, now=None):
        out = io.StringIO()
        env = {"DAYTRADER_NOW": now} if now else {}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = broker.main(list(argv))
        return code, out.getvalue()

    def run_json(self, *argv, now=None):
        code, out = self.run_main(*argv, "--json", now=now)
        return code, json.loads(out)

    def ticket(self, *extra, qty="40", entry="100", stop="99", target="102", now=None, quote=100.0):
        args = ["ticket", "--symbol", "TSLA", "--qty", qty, "--entry", entry, "--stop", stop]
        if target is not None:
            args += ["--target", target]
        with fake_bars({("TSLA", "1m"): quote_bars(quote)}):
            return self.run_json(*args, *extra, now=now)

    def confirm(self, tid, price=100.0, now=f"{DAY}T10:16", five=None, yes=True, meta=None):
        mapping = {("TSLA", "1m"): quote_bars(price)}
        if five is not None:
            mapping[("TSLA", "5m")] = five
        with fake_bars(mapping, meta):
            return self.run_json("confirm", tid, *(["--yes"] if yes else []), now=now)

    def sync(self, five, now):
        with fake_bars({("TSLA", "5m"): five}):
            return self.run_json("sync", now=now)

    def ledger(self):
        return tc.load_ledger()


class TicketTests(BrokerCase):
    def test_ticket_card_and_json_shape(self):
        code, res = self.ticket(qty="8", entry="364.18", stop="358.32", target="370.04", quote=364.5)
        self.assertEqual(code, 0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "pending")
        self.assertIs(res["real_money"], False)
        self.assertEqual(res["mode"], "paper")
        k = res["ticket"]
        self.assertEqual(k["id"], "TKT-20261030-001")
        self.assertEqual((k["side"], k["qty"], k["type"], k["risk"], k["rr"]), ("long", 8, "limit", 46.88, 1.0))
        self.assertEqual(k["expires_at"], "2026-10-30T10:30:00-04:00")   # settings ticket_ttl_minutes = 15
        self.assertEqual(res["card"], "BUY 8 TSLA · LIMIT 364.18 · TARGET 370.04 · STOP 358.32 · PAPER · "
                                      "REAL MONEY: OFF · WAITING FOR YOUR YES — reply yes to confirm ticket "
                                      "TKT-20261030-001")
        led = self.ledger()
        self.assertEqual([t["status"] for t in led["tickets"]], ["pending"])
        self.assertEqual(led["trades"], [])               # a ticket never opens a position
        code, text = self.run_main("tickets")
        self.assertIn("TKT-20261030-001  pending", text)

    def test_text_card(self):
        with fake_bars({("TSLA", "1m"): quote_bars(364.5)}):
            code, text = self.run_main("ticket", "--symbol", "TSLA", "--qty", "8", "--entry", "364.18",
                                       "--stop", "358.32", "--target", "370.04")
        self.assertEqual(code, 0)
        self.assertIn("BUY 8 TSLA · LIMIT 364.18 · TARGET 370.04 · STOP 358.32 · PAPER · REAL MONEY: OFF", text)
        self.assertIn("WAITING FOR YOUR YES — reply yes to confirm ticket TKT-20261030-001", text)
        self.assertIn(tc.DISCLAIMER, text)

    def test_budget_block_refuses_with_exit_3_and_no_ticket(self):
        code, res = self.ticket(qty="200")          # $200 risk > $150 daily max loss
        self.assertEqual(code, broker.EXIT_BLOCKED)
        self.assertFalse(res["ok"])
        self.assertTrue(res["blocked"])
        self.assertTrue(any("daily max loss" in r for r in res["reasons"]))
        self.assertEqual(self.ledger()["tickets"], [])

    def test_validation_errors(self):
        for extra, msg in ((["--qty", "8", "--entry", "100", "--stop", "101", "--side", "long"], "stop must be below"),
                           (["--qty", "8", "--entry", "100.123", "--stop", "99"], "sub-penny"),
                           (["--qty", "0", "--entry", "100", "--stop", "99"], "--qty"),
                           (["--qty", "8", "--entry", "100"], "--stop is required"),
                           (["--qty", "8", "--stop", "99"], "--entry"),
                           (["--entry", "100", "--stop", "99"], "--qty"),
                           (["--qty", "8"], "no saved plan"),
                           (["--qty", "1500", "--entry", "100", "--stop", "99.95"], "4x"),
                           (["--qty", "8", "--entry", "100", "--stop", "99", "--time-stop", "17:00"], "time stop")):
            with self.subTest(extra=extra), fake_bars({("TSLA", "1m"): quote_bars(100)}):
                with self.assertRaises(ValueError) as cm:
                    self.run_main("ticket", "--symbol", "TSLA", *extra)
                self.assertIn(msg, str(cm.exception))
        self.assertEqual(self.ledger()["tickets"], [])

    def test_side_inferred_for_short(self):
        code, res = self.ticket(entry="100", stop="101", target="98")
        self.assertEqual(res["ticket"]["side"], "short")
        self.assertIn("SELL SHORT 40 TSLA", res["card"])

    def test_market_hours_window(self):
        for now, msg in ((f"2026-10-31T11:00", "closed"), (f"{DAY}T15:55", "time stop"),
                         (f"{DAY}T09:00", "pre-market")):
            with self.subTest(now=now), self.assertRaises(ValueError) as cm:
                self.ticket(now=now)
            self.assertIn(msg, str(cm.exception))
        code, res = self.ticket(now=f"{DAY}T09:20")        # expires 09:35, after the open: allowed
        self.assertEqual(code, 0)
        code, res = self.ticket(now=f"{DAY}T15:50")
        self.assertEqual(res["ticket"]["expires_at"], "2026-10-30T15:55:00-04:00")   # clipped to the time stop

    def test_ticket_from_plan_carries_size_time_stop_and_odds(self):
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04,
                      "setup": "orb", "exit_plan": {"time_stop": "11:30"},
                      "sizing": {"shares": 8, "side": "long", "entry": 364.18, "stop": 358.32},
                      "time_machine": {"side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04,
                                       "verdict": "CLOSE TO A COIN FLIP", "target_first_pct": 29.4,
                                       "stop_first_pct": 25.5}})
        with fake_bars({("TSLA", "1m"): quote_bars(364.5)}):
            code, res = self.run_json("ticket", "--symbol", "TSLA")
        k = res["ticket"]
        self.assertEqual((k["qty"], k["entry"], k["stop"], k["target"]), (8, 364.18, 358.32, 370.04))
        self.assertEqual((k["setup"], k["time_stop"], k["levels_source"], k["qty_source"]),
                         ("orb", "11:30", "plan", "plan sizing"))
        self.assertEqual(k["time_machine"]["verdict"], "CLOSE TO A COIN FLIP")
        # explicit levels that differ from the plan: the plan's odds and sizing are not borrowed
        with fake_bars({("TSLA", "1m"): quote_bars(364.5)}), self.assertRaises(ValueError) as cm:
            self.run_main("ticket", "--symbol", "TSLA", "--entry", "364.18", "--stop", "360")
        self.assertIn("other levels", str(cm.exception))
        with fake_bars({("TSLA", "1m"): quote_bars(364.5)}), self.assertRaises(ValueError):
            self.run_main("ticket", "--symbol", "TSLA", "--side", "short")
        with fake_bars({("TSLA", "1m"): quote_bars(364.5)}), self.assertRaises(ValueError) as cm:
            self.run_main("ticket", "--symbol", "TSLA", now=f"{DAY}T11:45")     # past the plan's 11:30 time stop
        self.assertIn("pass --time-stop", str(cm.exception))
        with fake_bars({("TSLA", "1m"): quote_bars(364.5)}):
            code, res = self.run_json("ticket", "--symbol", "TSLA", "--time-stop", "15:55", now=f"{DAY}T11:45")
        self.assertEqual((code, res["ticket"]["time_stop"]), (0, "15:55"))

    def test_stale_plan_warns(self):
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": "2026-10-29T10:00"}):
            tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 100, "stop": 99, "target": 102})
        with fake_bars({("TSLA", "1m"): quote_bars(100)}):
            code, res = self.run_json("ticket", "--symbol", "TSLA", "--qty", "10")
        self.assertTrue(any("2026-10-29" in w for w in res["warnings"]))

    def test_new_ticket_supersedes_pending_one_for_same_symbol(self):
        self.ticket()
        code, res = self.ticket(qty="30", now=f"{DAY}T10:16")
        self.assertEqual(res["superseded"], ["TKT-20261030-001"])
        old = broker.find(self.ledger(), "tickets", "TKT-20261030-001")
        self.assertEqual(old["status"], "cancelled")
        with self.assertRaises(ValueError) as cm:
            self.confirm("TKT-20261030-001", now=f"{DAY}T10:20")
        self.assertIn("superseded", str(cm.exception))

    def test_opposite_side_refused_while_position_open(self):
        self.ticket()
        self.confirm("TKT-20261030-001")
        with self.assertRaises(ValueError) as cm:
            self.ticket(entry="100", stop="101", target="98", now=f"{DAY}T10:17")
        self.assertIn("net against it", str(cm.exception))
        code, res = self.ticket(qty="10", now=f"{DAY}T10:18")     # same side: allowed, with a warning
        self.assertTrue(any("adds to your long TSLA" in w for w in res["warnings"]))

    def test_last_price_warnings(self):
        code, res = self.ticket(quote=98.5)
        self.assertTrue(any("through the stop" in w for w in res["warnings"]))
        code, res = self.ticket(entry="99.5", stop="98.5", target="101.5", quote=100.0, now=f"{DAY}T10:16")
        self.assertTrue(any("working day order" in n for n in res["notes"]))

    def test_quote_failure_does_not_block_a_limit_ticket(self):
        with fake_bars({}):
            code, res = self.run_json("ticket", "--symbol", "TSLA", "--qty", "10", "--entry", "100", "--stop", "99")
        self.assertEqual(code, 0)
        self.assertIsNone(res["last_price"])
        self.assertTrue(any("live price unavailable" in n for n in res["notes"]))
        self.assertTrue(any("no target" in w for w in res["warnings"]))


class ConfirmTests(BrokerCase):
    def test_without_yes_shows_ticket_and_exits_1(self):
        self.ticket()
        code, res = self.confirm("TKT-20261030-001", yes=False)
        self.assertEqual(code, 1)
        self.assertFalse(res["confirmed"])
        self.assertIn("--yes is required", res["reason"])
        led = self.ledger()
        self.assertEqual(led["tickets"][0]["status"], "pending")
        self.assertEqual(led["trades"], [])

    def test_yes_in_the_same_breath_is_refused(self):
        self.ticket()
        with self.assertRaises(ValueError) as cm:
            self.confirm("TKT-20261030-001", now=f"{DAY}T10:15:05")
        self.assertIn("later message", str(cm.exception))
        self.assertEqual(self.ledger()["trades"], [])

    def test_expired_ticket_refused_and_marked(self):
        self.ticket()
        with self.assertRaises(ValueError) as cm:
            self.confirm("TKT-20261030-001", now=f"{DAY}T10:30")
        self.assertIn("expired", str(cm.exception))
        self.assertEqual(self.ledger()["tickets"][0]["status"], "expired")

    def test_unknown_and_wrong_kind_of_id(self):
        with self.assertRaises(ValueError) as cm:
            self.confirm("TRADE-20261030-001")
        self.assertIn("use close", str(cm.exception))

    def test_marketable_limit_fills_at_better_price_and_once_only(self):
        self.ticket()
        code, res = self.confirm("TKT-20261030-001", price=99.80)
        self.assertEqual((code, res["status"], res["fill_price"]), (0, "filled", 99.8))
        tr = res["trade"]
        self.assertEqual((tr["id"], tr["ticket_id"], tr["status"], tr["entry_price"], tr["qty"]),
                         ("TRADE-20261030-001", "TKT-20261030-001", "open", 99.8, 40))
        self.assertEqual(tr["opened_at"], "2026-10-30T10:16:00-04:00")
        led = self.ledger()
        self.assertEqual((led["tickets"][0]["status"], led["tickets"][0]["trade_id"]), ("confirmed", tr["id"]))
        with self.assertRaises(ValueError) as cm:
            self.confirm("TKT-20261030-001", now=f"{DAY}T10:17")
        self.assertIn("already confirmed", str(cm.exception))
        self.assertEqual(len(self.ledger()["trades"]), 1)
        self.assertEqual(tc.day_summary()["open_risk"], 32.0)     # (99.80 - 99) x 40

    def test_limit_above_market_rests_as_working_order(self):
        self.ticket(entry="99.5", stop="98.5", target="101.5")
        code, res = self.confirm("TKT-20261030-001", price=100.0)
        self.assertEqual((code, res["status"], res["trade"]), (0, "working", None))
        self.assertEqual(self.ledger()["tickets"][0]["status"], "working")
        with self.assertRaises(ValueError) as cm:
            self.confirm("TKT-20261030-001", now=f"{DAY}T10:17")
        self.assertIn("working", str(cm.exception))

    def test_short_limit_mirror(self):
        self.ticket(entry="100", stop="101", target="98")
        code, res = self.confirm("TKT-20261030-001", price=100.3)      # above the sell limit -> fills at 100.30
        self.assertEqual((res["status"], res["fill_price"]), ("filled", 100.3))
        self.ticket(entry="100", stop="101", target="98", now=f"{DAY}T10:17")
        code, res = self.confirm("TKT-20261030-002", price=99.7, now=f"{DAY}T10:18")
        self.assertEqual(res["status"], "working")

    def test_market_fills_at_quote_and_rechecks_risk(self):
        self.ticket("--type", "market")
        code, res = self.confirm("TKT-20261030-001", price=100.4)
        self.assertEqual(res["fill_price"], 100.4)
        self.assertAlmostEqual(tc.trade_initial_risk(res["trade"]), 56.0)
        self.assertTrue(any("instead of" in n for n in res["notes"]))

    def test_price_through_the_stop_is_not_filled(self):
        self.ticket("--type", "market")
        with self.assertRaises(ValueError) as cm:
            self.confirm("TKT-20261030-001", price=98.9)
        self.assertIn("stop must be below", str(cm.exception))
        self.assertEqual(self.ledger()["trades"], [])
        self.assertEqual(self.ledger()["tickets"][0]["status"], "pending")

    def test_budget_is_rechecked_at_confirm(self):
        self.ticket()
        led = self.ledger()
        led["trades"].append({"id": "TRADE-20261030-009", "symbol": "NVDA", "side": "long", "qty": 100,
                              "entry_price": 50.0, "stop": 49.0, "status": "closed", "exit_price": 48.8,
                              "opened_at": f"{DAY}T09:40:00-04:00", "closed_at": f"{DAY}T10:00:00-04:00",
                              "pnl": -120.0, "exit_reason": "stop"})
        tc.save_ledger(led)
        code, res = self.confirm("TKT-20261030-001")
        self.assertEqual(code, broker.EXIT_BLOCKED)
        self.assertTrue(res["blocked"])
        self.assertEqual(len(self.ledger()["trades"]), 1)
        self.assertEqual(self.ledger()["tickets"][0]["status"], "pending")

    def test_quote_from_another_day_is_refused(self):
        self.ticket()
        with self.assertRaises(ValueError) as cm:
            self.confirm("TKT-20261030-001", meta={"regularMarketTime": et_ts("2026-10-29", "15:59")})
        self.assertIn("2026-10-29", str(cm.exception))

    def test_closed_market_and_data_error(self):
        self.ticket()
        with self.assertRaises(ValueError):
            self.confirm("TKT-20261030-001", now=f"{DAY}T15:56")     # past the time stop (ticket expired too)
        self.ticket(now=f"{DAY}T10:20")
        with fake_bars({}), self.assertRaises(tc.FetchError):
            self.run_main("confirm", "TKT-20261030-002", "--yes", now=f"{DAY}T10:21")
        self.assertEqual(self.ledger()["trades"], [])

    def test_no_ticket_or_fill_after_an_early_close(self):
        # Regression: on a half day (13:00 close) tickets outlived the close and confirm filled a paper
        # position at 13:30 that sync could never manage.
        hd = "2026-11-27"
        meta = {"currentTradingPeriod": {"regular": {"start": et_ts(hd, "09:30"), "end": et_ts(hd, "13:00")}}}
        quote = {("TSLA", "1m"): bars_from_closes(hd, [100.0], start="12:49", minutes=1)}
        args = ("ticket", "--symbol", "TSLA", "--qty", "40", "--entry", "100", "--stop", "99", "--target", "102")
        with fake_bars(quote, meta):
            code, res = self.run_json(*args, now=f"{hd}T12:50")
        self.assertEqual(res["ticket"]["expires_at"], "2026-11-27T12:55:00-05:00")   # 5 min before the close
        with fake_bars(quote, meta), self.assertRaises(ValueError) as cm:
            self.run_main(*args, now=f"{hd}T13:20")
        self.assertIn("13:00", str(cm.exception))
        # a ticket that (by older rules) outlived the close still cannot be filled after it
        led = self.ledger()
        led["tickets"][0]["expires_at"] = f"{hd}T13:40:00-05:00"
        tc.save_ledger(led)
        with fake_bars(quote, meta), self.assertRaises(ValueError) as cm:
            self.run_main("confirm", "TKT-20261127-001", "--yes", now=f"{hd}T13:30")
        self.assertIn("13:00", str(cm.exception))
        self.assertEqual(self.ledger()["trades"], [])
        with fake_bars(quote, meta):                                   # before the early time stop: fills
            code, res = self.run_json("confirm", "TKT-20261127-001", "--yes", now=f"{hd}T12:54")
        self.assertEqual((code, res["status"]), (0, "filled"))

    def test_working_orders_reserve_budget(self):
        self.ticket(qty="60", entry="99.5", stop="98.5", target="101.5")             # $60 risk
        self.confirm("TKT-20261030-001", price=100.0)                               # -> working
        code, res = self.ticket(qty="100", entry="100", stop="99", now=f"{DAY}T10:17")  # $100 more = $160
        self.assertEqual(code, broker.EXIT_BLOCKED)
        self.assertEqual(res["budget"]["working_orders"], 1)
        code, res = self.ticket(qty="80", entry="100", stop="99", now=f"{DAY}T10:18")  # $140: OK
        self.assertEqual(code, 0)
        self.assertEqual(res["budget"]["summary"]["trades_today"], 1)                   # the working order


class SyncTests(BrokerCase):
    def session(self, after):
        """Flat 100.0 bars 09:30-10:15, then `after` closes from 10:20 (bars open at the prior close)."""
        return bars_from_closes(DAY, [100.0] * 10 + list(after), wick=0.1)

    def open_long(self, **kw):
        self.ticket(**kw)
        code, res = self.confirm("TKT-20261030-001")
        self.assertEqual(res["status"], "filled")
        return res["trade"]

    def test_target_hit_records_pnl_and_r(self):
        self.open_long()
        code, res = self.sync(self.session([101.0, 102.5, 103.0]), now=f"{DAY}T11:00")
        self.assertEqual(code, 0)
        ev = res["events"][0]
        self.assertEqual((ev["kind"], ev["reason"], ev["price"], ev["pnl"], ev["r_multiple"]),
                         ("closed", "target", 102.0, 80.0, 2.0))
        self.assertEqual(ev["at"], "2026-10-30T10:30:00-04:00")       # end of the 10:25 bar
        t = self.ledger()["trades"][0]
        self.assertEqual((t["status"], t["exit_reason"], t["exit_price"], t["pnl"], t["r_multiple"]),
                         ("closed", "target", 102.0, 80.0, 2.0))
        s = tc.day_summary()
        self.assertEqual((s["realized_pnl"], s["wins"], s["open_risk"]), (80.0, 1, 0))
        # idempotent
        code, res = self.sync(self.session([101.0, 102.5, 103.0]), now=f"{DAY}T11:05")
        self.assertEqual(res["events"], [])

    def test_ambiguous_bar_is_a_stop(self):
        self.open_long()
        five = self.session([]) + [bar("10:20", 100.0, 102.5, 98.5, 100.0)]
        code, res = self.sync(five, now=f"{DAY}T10:30")
        ev = res["events"][0]
        self.assertEqual((ev["reason"], ev["price"], ev["pnl"], ev["r_multiple"], ev["ambiguous"]),
                         ("stop", 99.0, -40.0, -1.0, True))
        self.assertTrue(self.ledger()["trades"][0]["ambiguous_exit"])

    def test_time_stop_eod(self):
        self.open_long()
        five = bars_from_closes(DAY, [100.0] * 70 + [100.5] * 8, wick=0.1)
        code, res = self.sync(five, now=f"{DAY}T16:10")
        ev = res["events"][0]
        self.assertEqual((ev["reason"], ev["price"], ev["pnl"]), ("eod", 100.5, 20.0))
        self.assertEqual(ev["at"], "2026-10-30T15:55:00-04:00")

    def test_still_open_and_not_yet_complete_bars(self):
        self.open_long()
        code, res = self.sync(self.session([98.0]), now=f"{DAY}T10:23")   # 10:20 bar still forming
        self.assertEqual(res["events"], [])
        self.assertEqual(res["open"][0]["trade_id"], "TRADE-20261030-001")
        self.assertEqual(self.ledger()["trades"][0]["status"], "open")

    def test_working_order_fills_then_stops(self):
        self.ticket(entry="99.5", stop="98.5", target="101.5")
        self.confirm("TKT-20261030-001", price=100.0)
        five = self.session([99.3, 98.2])    # 10:20 trades through 99.50; 10:25 hits 98.50
        code, res = self.sync(five, now=f"{DAY}T11:00")
        kinds = [(e["kind"], e.get("reason")) for e in res["events"]]
        self.assertEqual(kinds, [("filled", None), ("closed", "stop")])
        self.assertEqual(res["events"][0]["price"], 99.5)
        t = self.ledger()["trades"][0]
        self.assertEqual((t["entry_price"], t["opened_at"], t["exit_price"], t["pnl"]),
                         (99.5, "2026-10-30T10:20:00-04:00", 98.5, -40.0))
        k = self.ledger()["tickets"][0]
        self.assertEqual((k["status"], k["trade_id"]), ("confirmed", t["id"]))

    def test_working_order_expires_unfilled(self):
        self.ticket(entry="95", stop="94", target="97", qty="10")
        self.confirm("TKT-20261030-001", price=100.0)
        code, res = self.sync(flat_session(), now=f"{DAY}T16:05")
        self.assertEqual(res["events"][0]["kind"], "expired")
        self.assertEqual(self.ledger()["tickets"][0]["status"], "expired")
        self.assertEqual(self.ledger()["trades"], [])

    def test_nothing_to_sync_needs_no_network(self):
        with fake_bars({}):
            code, text = self.run_main("sync")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to sync", text)

    def test_all_fetches_failing_is_a_data_error(self):
        self.open_long()
        with fake_bars({}), self.assertRaises(tc.FetchError):
            self.run_main("sync", now=f"{DAY}T11:00")

    def test_confirm_auto_syncs_so_the_tilt_guard_sees_stops(self):
        tc.save_settings({"max_consecutive_losses": 1})
        self.open_long(qty="20")
        self.ticket(qty="20", now=f"{DAY}T10:40")       # same side, add-on; created before the stop is known
        five = self.session([98.5])                     # the open trade hits its stop in the 10:20 bar
        code, res = self.confirm("TKT-20261030-002", now=f"{DAY}T10:41", five=five)
        self.assertEqual(code, broker.EXIT_BLOCKED)
        self.assertTrue(any("tilt" in r for r in res["reasons"]))
        self.assertEqual(self.ledger()["trades"][0]["exit_reason"], "stop")


class CloseCancelPositionsHistoryTests(BrokerCase):
    def open_long(self):
        self.ticket()
        return self.confirm("TKT-20261030-001")[1]["trade"]

    def test_manual_close_with_price(self):
        t = self.open_long()
        with fake_bars({("TSLA", "5m"): flat_session()}):
            code, res = self.run_json("close", t["id"], "--price", "100.75", now=f"{DAY}T10:40")
        self.assertEqual((res["closed_by"], res["trade"]["exit_reason"], res["trade"]["pnl"], res["trade"]["r_multiple"]),
                         ("manual", "manual", 30.0, 0.75))
        with self.assertRaises(ValueError):
            self.run_main("close", t["id"], "--price", "101", now=f"{DAY}T10:41")

    def test_close_at_quote_even_without_bars(self):
        t = self.open_long()
        with fake_bars({("TSLA", "1m"): quote_bars(99.5)}):
            code, res = self.run_json("close", t["id"], now=f"{DAY}T10:40")
        self.assertEqual((res["trade"]["exit_price"], res["trade"]["pnl"]), (99.5, -20.0))
        self.assertTrue(any("could not check" in n for n in res["notes"]))

    def test_close_after_stop_already_hit_reports_the_stop(self):
        t = self.open_long()
        five = bars_from_closes(DAY, [100.0] * 10 + [98.0], wick=0.1)
        with fake_bars({("TSLA", "5m"): five}):
            code, res = self.run_json("close", t["id"], "--price", "101", now=f"{DAY}T10:40")
        self.assertEqual((res["closed_by"], res["trade"]["exit_reason"], res["trade"]["exit_price"]),
                         ("sync", "stop", 99.0))

    def test_close_validation(self):
        with self.assertRaises(ValueError):
            self.run_main("close", "TRADE-20261030-001")
        with self.assertRaises(ValueError) as cm:
            self.run_main("close", "TKT-20261030-001")
        self.assertIn("use cancel", str(cm.exception))
        with self.assertRaises(ValueError):
            self.run_main("close", "X", "--price", "-1")

    def test_cancel(self):
        self.ticket(entry="99.5", stop="98.5", target="101.5")
        self.confirm("TKT-20261030-001", price=100.0)
        with fake_bars({}):                              # data down: still cancels, and says it could not check
            code, res = self.run_json("cancel", "TKT-20261030-001", now=f"{DAY}T10:20")
        self.assertEqual((res["was"], res["ticket"]["status"]), ("working", "cancelled"))
        self.assertTrue(any("could not check" in n for n in res["notes"]))
        with self.assertRaises(ValueError):
            self.run_main("cancel", "TKT-20261030-001", now=f"{DAY}T10:21")
        with self.assertRaises(ValueError) as cm:
            self.confirm("TKT-20261030-001", now=f"{DAY}T10:22")
        self.assertIn("cancelled", str(cm.exception))

    def test_cancel_syncs_first_so_a_fill_cannot_be_erased(self):
        # Regression: cancelling a working order used to skip the bars, so an order that had already
        # filled (and been stopped out) vanished from the ledger along with its loss.
        self.ticket(entry="99.5", stop="98.5", target="101.5")
        self.confirm("TKT-20261030-001", price=100.0)                     # working
        five = bars_from_closes(DAY, [100.0] * 10 + [99.3, 98.2], wick=0.1)  # fills 10:20, stops 10:25
        with fake_bars({("TSLA", "5m"): five}), self.assertRaises(ValueError) as cm:
            self.run_main("cancel", "TKT-20261030-001", now=f"{DAY}T10:40")
        self.assertIn("already filled", str(cm.exception))
        led = self.ledger()
        self.assertEqual(led["tickets"][0]["status"], "confirmed")
        t = led["trades"][0]
        self.assertEqual((t["entry_price"], t["exit_reason"], t["exit_price"], t["pnl"]), (99.5, "stop", 98.5, -40.0))
        self.assertEqual(tc.day_summary()["realized_pnl"], -40.0)
        # a working order that has not traded through its limit is still cancelled
        self.ticket(entry="97", stop="96", target="99", qty="10", now=f"{DAY}T10:41")
        self.confirm("TKT-20261030-002", price=100.0, now=f"{DAY}T10:42")
        with fake_bars({("TSLA", "5m"): five}):
            code, res = self.run_json("cancel", "TKT-20261030-002", now=f"{DAY}T10:50")
        self.assertEqual((code, res["was"], res["ticket"]["status"]), (0, "working", "cancelled"))

    def test_cannot_cancel_a_filled_ticket(self):
        self.open_long()
        with self.assertRaises(ValueError) as cm:
            self.run_main("cancel", "TKT-20261030-001", now=f"{DAY}T10:20")
        self.assertIn("close the trade", str(cm.exception))

    def test_positions_live_pnl_and_best_effort(self):
        self.open_long()
        with fake_bars({("TSLA", "1m"): quote_bars(101.0)}):
            code, res = self.run_json("positions", now=f"{DAY}T10:30")
        p = res["positions"][0]
        self.assertEqual((p["last"], p["unrealized"], p["unrealized_r"]), (101.0, 40.0, 1.0))
        self.assertEqual(res["unrealized_total"], 40.0)
        with fake_bars({}):
            code, res = self.run_json("positions", now=f"{DAY}T10:30")
        self.assertEqual(code, 0)
        self.assertIsNone(res["positions"][0]["unrealized"])
        self.assertEqual(res["errors"][0]["symbol"], "TSLA")
        with fake_bars({("TSLA", "1m"): quote_bars(98.0)}):
            code, text = self.run_main("positions", now=f"{DAY}T10:30")
        self.assertIn("through the stop - run sync", text)
        self.assertIn(tc.DISCLAIMER, text)          # simulated P&L is shown -> say it is not a guarantee
        with fake_bars({}):
            code, text = self.run_main("close", "TRADE-20261030-001", "--price", "100.5", now=f"{DAY}T10:31")
        self.assertIn("CLOSED (paper)", text)
        self.assertIn(tc.DISCLAIMER, text)

    def test_history_window_and_totals(self):
        led = {"tickets": [], "trades": [
            {"id": "TRADE-20261001-001", "symbol": "A", "side": "long", "qty": 1, "entry_price": 10, "stop": 9,
             "status": "closed", "pnl": 5.0, "r_multiple": 5.0, "exit_reason": "target", "exit_price": 15,
             "opened_at": "2026-10-01T10:00:00-04:00", "closed_at": "2026-10-01T11:00:00-04:00"},
            {"id": "TRADE-20261029-001", "symbol": "B", "side": "short", "qty": 10, "entry_price": 10, "stop": 11,
             "status": "closed", "pnl": -10.0, "r_multiple": -1.0, "exit_reason": "stop", "exit_price": 11,
             "opened_at": "2026-10-29T10:00:00-04:00", "closed_at": "2026-10-29T10:30:00-04:00"},
            {"id": "TRADE-20261030-001", "symbol": "C", "side": "long", "qty": 10, "entry_price": 10, "stop": 9,
             "status": "closed", "pnl": 20.0, "r_multiple": 2.0, "exit_reason": "eod", "exit_price": 12,
             "opened_at": "2026-10-30T10:00:00-04:00", "closed_at": "2026-10-30T15:55:00-04:00"}]}
        tc.save_ledger(led)
        code, res = self.run_json("history", "--days", "10")
        self.assertEqual([t["id"] for t in res["trades"]], ["TRADE-20261029-001", "TRADE-20261030-001"])
        self.assertEqual(res["totals"], {"trades": 2, "wins": 1, "losses": 1, "flat": 0, "win_rate_pct": 50.0,
                                         "net_pnl": 10.0, "avg_r": 0.5, "best": 20.0, "worst": -10.0})
        code, res = self.run_json("history", "--days", "30")
        self.assertEqual(res["totals"]["trades"], 3)
        code, text = self.run_main("history", "--days", "1")
        self.assertIn("TIME STOP", text)
        with self.assertRaises(ValueError):
            self.run_main("history", "--days", "0")
        with self.assertRaises(ValueError):                  # used to be an OverflowError traceback
            self.run_main("history", "--days", "1000000")

    def test_absurd_prices_are_input_errors_not_tracebacks(self):
        self.assertEqual(broker.px(1e30), 1e30)              # used to raise decimal.InvalidOperation
        self.assertFalse(broker.sub_penny(1e30))
        with fake_bars({}), self.assertRaises(ValueError):
            self.run_main("ticket", "--symbol", "TSLA", "--qty", "1", "--entry", "1e30", "--stop", "1")
        t = self.open_long()
        with fake_bars({}), self.assertRaises(ValueError):
            self.run_main("close", t["id"], "--price", "1e30", now=f"{DAY}T10:40")

    def test_empty_states(self):
        for cmd, text in (("positions", "No open paper positions"), ("tickets", "No tickets"),
                          ("history", "No closed paper trades")):
            with fake_bars({}):
                code, out = self.run_main(cmd)
            self.assertEqual(code, 0)
            self.assertIn(text, out)

    def test_ledger_lock_contention_and_stale_lock(self):
        lock = self.home / "ledger.lock"
        lock.write_text("12345")
        with mock.patch.object(broker, "LOCK_WAIT_SECONDS", 0.2), self.assertRaises(tc.StateError):
            self.run_main("cancel", "TKT-20261030-001")
        old = os.path.getmtime(lock) - 3600
        os.utime(lock, (old, old))                       # an abandoned lock is taken over
        code, res = self.ticket()
        self.assertEqual(code, 0)
        self.assertFalse(lock.exists())

    def test_corrupt_ledger_is_a_state_error(self):
        (self.home / "ledger.json").write_text("[1, 2]")
        with self.assertRaises(tc.StateError):
            self.run_main("tickets")


class PaperOnlyTests(unittest.TestCase):
    def test_no_live_flag_or_mode(self):
        p = broker.build_parser()
        for argv in (["ticket", "--symbol", "TSLA", "--qty", "8", "--live"],
                     ["--live", "positions"], ["--mode", "live", "positions"], ["positions", "--mode", "live"],
                     ["confirm", "TKT-1", "--yes", "--live"], ["ticket", "--symbol", "TSLA", "--l"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                p.parse_args(argv)
            self.assertEqual(cm.exception.code, 1)  # usage error (exit 2 is reserved for data errors)

    def test_no_network_or_broker_code_path(self):
        src = Path(broker.__file__).read_text(encoding="utf-8")
        for needle in ("urllib", "http.client", "import socket", "requests", "api_key", "add_argument(\"--live",
                       "add_argument(\"--mode"):
            self.assertNotIn(needle, src)
        p = broker.build_parser()
        subs = [a for a in p._actions if isinstance(a, broker.argparse._SubParsersAction)][0]
        options = {o for sp in [p, *subs.choices.values()] for a in sp._actions for o in a.option_strings}
        self.assertFalse([o for o in options if "live" in o or "mode" in o or "real" in o], options)


class SkillDocTests(unittest.TestCase):
    """SKILL.md is what keeps Claude from confirming on the user's behalf - guard its contract."""

    def setUp(self):
        self.skill = (SCRIPT.parent.parent / "SKILL.md").read_text(encoding="utf-8")

    def test_frontmatter_and_required_lines(self):
        head = self.skill.split("---")[1]
        self.assertIn("name: broker", head)
        desc = " ".join(head.split("description:", 1)[1].replace(">-", "").split())
        self.assertTrue(desc.startswith("Day-trading desk:"), desc[:40])
        self.assertLessEqual(len(desc), 1024)
        self.assertLessEqual(len(self.skill.splitlines()), 150)
        self.assertIn("> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder "
                      "(`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.",
                      self.skill)

    def test_yes_rules_are_spelled_out(self):
        flat = " ".join(self.skill.split())
        for phrase in ("in a later message", "Never run `confirm --yes` in the same turn", "only for that specific ticket",
                       '"maybe"', '"looks good?"', "does not count", "Live trading is intentionally not supported",
                       "allow-listed", "Never invent prices"):
            self.assertIn(phrase, flat)

    def test_every_documented_command_parses(self):
        import re
        import shlex
        p = broker.build_parser()
        docs = self.skill + (SCRIPT.parent.parent / "reference.md").read_text(encoding="utf-8") + (broker.__doc__ or "")
        lines = re.findall(r"python3 scripts/broker\.py ([^#`\n]*)", docs)
        self.assertGreater(len(lines), 10)
        for line in lines:
            with self.subTest(line=line), contextlib.redirect_stderr(io.StringIO()):
                p.parse_args(shlex.split(line))


class CliTests(TempHomeTestCase):
    """Real subprocess runs with offline fixtures (the exact Yahoo URLs tc.fetch_bars asks for)."""

    def test_ticket_confirm_sync_history_cli(self):
        fx = self.home / "fx"
        write_fixture(fx, yahoo_chart_url("TSLA", "1m", "1d"), yahoo_payload(quote_bars(364.10), "TSLA", price=364.10))
        closes = [362.0, 363.0, 363.5, 364.2, 364.0, 364.3, 365.0, 366.0, 363.0, 364.1, 366.0, 368.5, 371.0]
        five = bars_from_closes(DAY, closes, wick=0.2)
        b = five[9]                                   # the 10:15 fill bar spikes to 371 before the 10:16 fill
        five[9] = tc.Bar(b.ts, b.open, 371.0, b.low, b.close, b.volume)
        write_fixture(fx, yahoo_chart_url("TSLA", "5m", "5d"), yahoo_payload(five, "TSLA"))
        env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(fx)}

        def cli(now, *args):
            return run_cli(SCRIPT, *args, env=dict(env, DAYTRADER_NOW=now))

        r = cli(f"{DAY}T10:15", "ticket", "--symbol", "TSLA", "--qty", "8", "--entry", "364.18", "--stop", "358.32",
                "--target", "370.04")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WAITING FOR YOUR YES — reply yes to confirm ticket TKT-20261030-001", r.stdout)
        self.assertIn("REAL MONEY: OFF", r.stdout)
        r = cli(f"{DAY}T10:16", "confirm", "TKT-20261030-001")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT CONFIRMED", r.stdout)
        r = cli(f"{DAY}T10:16", "confirm", "TKT-20261030-001", "--yes", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual((res["status"], res["fill_price"], res["trade"]["id"]), ("filled", 364.1, "TRADE-20261030-001"))
        r = cli(f"{DAY}T11:00", "sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TARGET @ 370.04 in the 10:30 ET bar", r.stdout)     # not the 10:15 fill bar
        r = cli(f"{DAY}T11:00", "history", "--json")
        self.assertEqual(json.loads(r.stdout)["totals"]["net_pnl"], 47.52)   # (370.04 - 364.10) x 8
        r = cli(f"{DAY}T11:00", "--json", "positions")
        self.assertEqual(json.loads(r.stdout)["positions"], [])

    def test_data_error_is_one_line_exit_2(self):
        env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_NOW": f"{DAY}T10:15"}
        with tempfile.TemporaryDirectory() as empty:
            r = run_cli(SCRIPT, "ticket", "--symbol", "TSLA", "--qty", "8", "--type", "market", "--stop", "358.32",
                        env=dict(env, DAYTRADER_FIXTURES=empty))
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith("data error: "), r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)
        self.assertNotIn("Traceback", r.stderr)
        r = run_cli(SCRIPT, "ticket", "--symbol", "TSLA", "--qty", "8", "--live", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("unrecognized arguments: --live", r.stderr)


if __name__ == "__main__":
    unittest.main()
