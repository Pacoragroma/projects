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
                     skill_script, tc, utc_ts, write_binance_fixtures, write_binance_products,  # noqa: E402
                     write_binance_quote, write_coinbase_fixtures, write_coinbase_products, write_coinbase_quote,
                     write_fixture, yahoo_chart_url, yahoo_payload)

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
        # Outside the session the ticket is a PREVIEW (exit 0, nothing saved) - see TicketPreviewTests.
        for now, msg in ((f"2026-10-31T11:00", "closed"), (f"{DAY}T15:55", "time stop"),
                         (f"{DAY}T09:00", "pre-market")):
            with self.subTest(now=now):
                code, res = self.ticket(now=now)
                self.assertEqual((code, res["status"], res["preview"]), (0, "preview", True))
                self.assertIn(msg, res["market_closed"])
                self.assertEqual(self.ledger()["tickets"], [])
        code, res = self.ticket(now=f"{DAY}T09:20")        # expires 09:35, after the open: allowed
        self.assertEqual((code, res["status"], res["preview"]), (0, "pending", False))
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
        with fake_bars({("TSLA", "1m"): quote_bars(364.5)}):
            code, res = self.run_json("ticket", "--symbol", "TSLA", now=f"{DAY}T11:45")  # past the plan's 11:30 stop
        self.assertEqual((code, res["status"]), (0, "preview"))
        self.assertIn("pass --time-stop", res["market_closed"])
        self.assertTrue(res["banner"].startswith("PAST THE 11:30 ET TIME STOP — preview only."))
        self.assertEqual([k["id"] for k in self.ledger()["tickets"]], ["TKT-20261030-001"])   # no new ticket
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


class TicketPreviewTests(BrokerCase):
    """Outside the session `ticket` still checks everything but prints a PREVIEW: exit 0, nothing saved."""

    BANNER = "MARKET CLOSED — preview only. Re-run ticket after 09:30 ET to get a ticket you can confirm."
    NEXT_DAY = ("MARKET CLOSED — preview only. Re-run ticket after 09:30 ET on the next trading day to get a "
                "ticket you can confirm.")

    def save_tsla_plan(self, **tm_extra):
        tm = {"side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04, "verdict": "CLOSE TO A COIN FLIP",
              "target_first_pct": 31.66, "stop_first_pct": 31.57, "open_at_close_pct": 36.77}
        tm.update(tm_extra)
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04,
                      "setup": "orb", "sizing": {"shares": 8, "side": "long", "entry": 364.18, "stop": 358.32},
                      "time_machine": tm})

    def test_preview_outside_the_session_is_exit_0_and_saves_nothing(self):
        self.save_tsla_plan()
        for now, banner in (("2026-10-31T11:00", self.NEXT_DAY), (f"{DAY}T19:30", self.NEXT_DAY),
                            (f"{DAY}T08:00", self.BANNER)):
            with self.subTest(now=now):
                with fake_bars({("TSLA", "1m"): quote_bars(368.14)}):
                    code, res = self.run_json("ticket", "--symbol", "TSLA", now=now)
                self.assertEqual(code, 0)
                self.assertEqual((res["ok"], res["status"], res["preview"], res["real_money"]),
                                 (True, "preview", True, False))
                self.assertNotIn("ticket", res)
                self.assertNotIn("awaiting", res)
                o = res["order"]
                self.assertEqual((o["side"], o["qty"], o["entry"], o["stop"], o["target"], o["risk"], o["status"]),
                                 ("long", 8, 364.18, 358.32, 370.04, 46.88, "preview"))
                self.assertNotIn("id", o)
                self.assertEqual(res["banner"], banner)
                self.assertEqual(res["card"], "BUY 8 TSLA · LIMIT 364.18 · TARGET 370.04 · STOP 358.32 · PAPER · "
                                              "REAL MONEY: OFF · " + banner)
                self.assertTrue(res["budget"]["ok"])
                self.assertEqual(self.ledger(), {"tickets": [], "trades": []})
                with fake_bars({("TSLA", "1m"): quote_bars(368.14)}):
                    code, text = self.run_main("ticket", "--symbol", "TSLA", now=now)
                self.assertEqual(code, 0)
                self.assertIn("ORDER TICKET · PREVIEW", text)
                self.assertIn("BUY 8 TSLA · LIMIT 364.18 · TARGET 370.04 · STOP 358.32 · PAPER · REAL MONEY: OFF", text)
                self.assertIn("$46.88", text)
                self.assertIn(banner, text)
                self.assertNotIn("WAITING FOR YOUR YES", text)
                self.assertNotIn("reply yes", text)
                self.assertIn(tc.DISCLAIMER, text)
        code, res = self.run_json("tickets", "--all")
        self.assertEqual(res["tickets"], [])

    def test_past_the_time_stop_before_the_close(self):
        code, res = self.ticket(now=f"{DAY}T15:57")
        self.assertEqual((code, res["status"]), (0, "preview"))
        self.assertEqual(res["banner"], "PAST THE 15:55 ET TIME STOP — preview only. Re-run ticket after 09:30 ET on "
                                        "the next trading day to get a ticket you can confirm.")

    def test_preview_still_validates_levels_exposure_and_budget(self):
        sat = "2026-10-31T11:00"
        with fake_bars({("TSLA", "1m"): quote_bars(100)}), self.assertRaises(ValueError) as cm:
            self.run_main("ticket", "--symbol", "TSLA", "--qty", "8", "--entry", "100", "--stop", "101",
                          "--side", "long", now=sat)
        self.assertIn("stop must be below", str(cm.exception))
        with fake_bars({("TSLA", "1m"): quote_bars(100)}), self.assertRaises(ValueError) as cm:
            self.run_main("ticket", "--symbol", "TSLA", "--qty", "1500", "--entry", "100", "--stop", "99.95", now=sat)
        self.assertIn("4x", str(cm.exception))
        code, res = self.ticket(qty="200", now=sat)            # $200 risk > $150 daily max loss
        self.assertEqual(code, broker.EXIT_BLOCKED)
        self.assertTrue(res["blocked"] and res["preview"])
        self.assertTrue(any("start fresh on the next session" in n for n in res["notes"]))
        with fake_bars({("TSLA", "1m"): quote_bars(100)}):
            code, text = self.run_main("ticket", "--symbol", "TSLA", "--qty", "200", "--entry", "100", "--stop", "99",
                                       now=sat)
        self.assertIn("TICKET REFUSED", text)
        self.assertIn(self.NEXT_DAY, text)
        self.assertEqual(self.ledger()["tickets"], [])

    def test_preview_never_supersedes_a_live_ticket_and_confirm_is_unchanged(self):
        self.ticket()                                                       # TKT-001, pending until 10:30
        code, res = self.ticket("--time-stop", "10:00", qty="30", now=f"{DAY}T10:16")   # past its own time stop
        self.assertEqual((code, res["status"], res["preview"]), (0, "preview", True))
        led = self.ledger()
        self.assertEqual([(k["id"], k["status"]) for k in led["tickets"]], [("TKT-20261030-001", "pending")])
        code, res = self.confirm("TKT-20261030-001", now=f"{DAY}T10:17")    # the real ticket still confirms
        self.assertEqual((code, res["status"]), (0, "filled"))
        # A real ticket made outside the session by older versions still cannot be filled then.
        led = self.ledger()
        led["tickets"].append(dict(led["tickets"][0], id="TKT-20261031-001", status="pending", trade_id=None,
                                   created_at="2026-10-31T10:00:00-04:00", expires_at="2026-10-31T23:00:00-04:00"))
        tc.save_ledger(led)
        with self.assertRaises(ValueError) as cm:
            self.confirm("TKT-20261031-001", now="2026-10-31T11:00")
        self.assertIn("market is closed", str(cm.exception))

    def test_preview_wording_of_the_last_price_notes(self):
        code, res = self.ticket(entry="99.5", stop="98.5", target="101.5", now="2026-10-31T11:00")
        self.assertTrue(any("a confirmed ticket would rest as a working day order" in n for n in res["notes"]))
        self.assertFalse(any("on yes" in n for n in res["notes"]))
        code, res = self.ticket(entry="100.5", stop="99.5", target="102.5", now="2026-10-31T11:00")
        self.assertTrue(any("a confirmed ticket would fill right away" in n for n in res["notes"]))
        w, n = broker.order_checks(dict(symbol="TSLA", side="long", entry=100.0, stop=99.0, target=102.0, qty=10,
                                        type="limit"), tc.DEFAULT_SETTINGS, {"price": 100.5, "date": DAY},
                                   "2026-10-31", preview=True)
        self.assertTrue(any("the last session's price" in x for x in n))


class TimeMachineNoteTests(BrokerCase):
    """The ticket says when its levels were never tested by the time machine; odds read as printed there."""

    UNTESTED = "not tested by the time machine for these levels"

    def plan(self, tm=None, target=370.04):
        p = {"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "target": target,
             "sizing": {"shares": 8, "side": "long", "entry": 364.18, "stop": 358.32}}
        if tm is not None:
            p["time_machine"] = tm
        tc.save_plan(p)

    def plan_ticket(self, *extra):
        with fake_bars({("TSLA", "1m"): quote_bars(364.5)}):
            return self.run_json("ticket", "--symbol", "TSLA", *extra)

    def tm(self, **kw):
        d = {"side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04, "verdict": "CLOSE TO A COIN FLIP",
             "target_first_pct": 31.66, "stop_first_pct": 31.57, "open_at_close_pct": 36.77}
        d.update(kw)
        return d

    def test_plan_without_time_machine(self):
        self.plan()
        code, res = self.plan_ticket()
        self.assertIn(f"{self.UNTESTED} (run time_machine.py TSLA --save)", res["notes"])
        self.assertIsNone(res["ticket"]["time_machine"])
        with fake_bars({("TSLA", "1m"): quote_bars(364.5)}):
            code, text = self.run_main("ticket", "--symbol", "TSLA", now=f"{DAY}T10:16")
        self.assertIn(f"  - {self.UNTESTED} (run time_machine.py TSLA --save)", text)

    def test_time_machine_for_other_levels(self):
        self.plan(self.tm(target=375.0))                      # exit moved the target after the time machine ran
        code, res = self.plan_ticket()
        self.assertIn(f"{self.UNTESTED} (run time_machine.py TSLA --save) - the plan's odds were for other levels",
                      res["notes"])
        self.assertIsNone(res["ticket"]["time_machine"])

    def test_flag_levels_without_a_plan_name_the_levels(self):
        code, res = self.ticket()
        self.assertIn(f"{self.UNTESTED} (run time_machine.py TSLA --entry 100.00 --stop 99.00 --target 102.00 --save)",
                      res["notes"])
        code, res = self.ticket(target=None, now=f"{DAY}T10:16")
        self.assertIn(f"{self.UNTESTED} (it needs a target to measure the odds)", res["notes"])

    def test_matching_time_machine_has_no_untested_note_and_prints_display_pct(self):
        self.plan(self.tm(display_pct={"target_first": 31.6, "stop_first": 31.6, "open_at_close": 36.8}))
        code, res = self.plan_ticket()
        self.assertFalse(any(self.UNTESTED in n for n in res["notes"]))
        self.assertEqual(res["ticket"]["time_machine"]["display_pct"],
                         {"target_first": 31.6, "stop_first": 31.6, "open_at_close": 36.8})
        text = broker.render_ticket(res)
        self.assertIn("time-machine: CLOSE TO A COIN FLIP (target first 31.6% / stop first 31.6%)", text)
        self.assertNotIn("31.66", text)

    def test_raw_odds_fall_back_to_one_decimal(self):
        self.plan(self.tm())
        code, res = self.plan_ticket()
        text = broker.render_ticket(res)
        self.assertIn("(target first 31.7% / stop first 31.6%)", text)    # raw 31.66 / 31.57 with :.1f
        self.assertNotIn("31.66", text)
        self.assertEqual(broker.tm_display({"target_first_pct": 30, "display_pct": {"target_first": "x"}}),
                         {"target_first": 30.0, "stop_first": None, "open_at_close": None})


class DirectionalRiskTests(BrokerCase):
    def test_working_order_risk_matches_tc_open_risk(self):
        # Regression: working-order risk used |entry - stop|, so a (hand-edited) stop past the limit showed
        # reserved risk that tc.day_summary - and so the budget check - counts as 0.
        led = {"tickets": [{"id": "TKT-20261030-009", "symbol": "TSLA", "side": "long", "qty": 10, "entry": 100.0,
                            "stop": 101.0, "status": "working", "confirmed_at": f"{DAY}T10:05:00-04:00"},
                           {"id": "TKT-20261030-010", "symbol": "NVDA", "side": "short", "qty": 10, "entry": 50.0,
                            "stop": 51.0, "status": "working", "confirmed_at": f"{DAY}T10:06:00-04:00"}],
               "trades": []}
        res = broker.check_budget(led, 0.0, broker.load_settings(), DAY)
        self.assertEqual(res["working_risk"], 10.0)                      # the short only
        self.assertEqual(res["working_risk"], res["summary"]["open_risk"])
        tc.save_ledger(led)
        with fake_bars({}):
            code, res = self.run_json("positions")
        self.assertEqual(res["working_risk"], 10.0)


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
        with fake_bars(quote, meta):
            code, res = self.run_json(*args, now=f"{hd}T13:20")          # after the early close: a preview only
        self.assertEqual((code, res["status"]), (0, "preview"))
        self.assertIn("13:00", res["market_closed"])
        self.assertTrue(res["banner"].startswith("MARKET CLOSED — preview only."))
        self.assertEqual(len(self.ledger()["tickets"]), 1)
        # a ticket that (by older rules) outlived the close still cannot be filled after it
        led = self.ledger()
        led["tickets"][0].update(status="pending", expires_at=f"{hd}T13:40:00-05:00")   # (the preview expired it)
        led["tickets"][0].pop("expired_at", None)
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
        five = bars_from_closes(DAY, [100.0] * 11 + [100.4, 100.75], wick=0.1)   # 10:25 bar trades to 100.85
        with fake_bars({("TSLA", "5m"): five}):
            code, res = self.run_json("close", t["id"], "--price", "100.75", now=f"{DAY}T10:40")
        self.assertEqual((res["closed_by"], res["trade"]["exit_reason"], res["trade"]["pnl"], res["trade"]["r_multiple"]),
                         ("manual", "manual", 30.0, 0.75))
        self.assertFalse([n for n in res["notes"] if "not checked" in n], res["notes"])
        with self.assertRaises(ValueError):
            self.run_main("close", t["id"], "--price", "101", now=f"{DAY}T10:41")

    def test_manual_close_price_must_have_traded_since_the_fill(self):
        # Regression: `close --price 1000` on a ~100 stock recorded +$36,000 of made-up profit, and
        # realized profit widens today's loss budget - a way around the daily guard-rail.
        t = self.open_long()
        five = flat_session(wick=0.1)            # 99.90-100.10 all day; bars after 10:40 are the future
        five[20] = tc.Bar(five[20].ts, 100.0, 105.0, 100.0, 100.0, five[20].volume)   # 11:10, after "now"
        for price in ("1000", "100.5", "99.5", "105"):
            with self.subTest(price=price), fake_bars({("TSLA", "5m"): five}), \
                    self.assertRaises(ValueError) as cm:
                self.run_main("close", t["id"], "--price", price, now=f"{DAY}T10:40")
            self.assertIn("outside what TSLA traded since the fill (99.90-100.10", str(cm.exception))
        led = self.ledger()
        self.assertEqual((led["trades"][0]["status"], led["trades"][0].get("pnl")), ("open", None))
        self.assertEqual(broker.traded_range(five, None, et_ts(DAY, "10:40")), None)
        self.assertEqual(broker.traded_range([], et_ts(DAY, "10:16"), et_ts(DAY, "10:40")), None)
        self.assertEqual(broker.traded_range(five, et_ts(DAY, "10:16"), et_ts(DAY, "10:16")), (99.9, 100.1))  # fill bar
        with fake_bars({("TSLA", "5m"): five}):          # at the edge of the range (half a cent of float noise)
            code, res = self.run_json("close", t["id"], "--price", "100.10", now=f"{DAY}T10:40")
        self.assertEqual((code, res["trade"]["exit_price"]), (0, 100.1))

    def test_manual_close_price_without_bars_is_accepted_with_a_note(self):
        t = self.open_long()
        with fake_bars({}):
            code, res = self.run_json("close", t["id"], "--price", "100.5", now=f"{DAY}T10:40")
        self.assertEqual((code, res["trade"]["exit_price"]), (0, 100.5))
        self.assertTrue(any("not checked against traded prices" in n for n in res["notes"]), res["notes"])

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


# ---------------------------------------------------------------------------
# Crypto: 24/7 on UTC days, Coinbase prices, fractional sizes, fees on every fill
# ---------------------------------------------------------------------------

CDAY = "2026-10-31"          # a Saturday: the stock market is closed, crypto is not


def cbar(hhmm, o, h, l, c, day=CDAY):
    return tc.Bar(utc_ts(day, hhmm), o, h, l, c, 5.0)


def crypto_flat(start, end, price=101200.0, day=CDAY, start_day=None, wick=100.0):
    """Flat 5-minute UTC bars from `start` (on start_day) up to `end` (on day), exclusive."""
    t, stop = utc_ts(start_day or day, start), utc_ts(day, end)
    out = []
    while t < stop:
        out.append(tc.Bar(t, price, price + wick, price - wick, price, 5.0))
        t += 300
    return out


def btc_trade(**kw):
    t = {"id": "TRADE-20261031-001", "ticket_id": "TKT-20261031-001", "symbol": "BTC-USD", "market": "crypto",
         "side": "long", "qty": 0.01, "entry_price": 101234.56, "stop": 99000.0, "target": 104000.0,
         "time_stop": "23:55", "opened_at": f"{CDAY}T04:41:00+00:00", "status": "open", "tick": 0.01,
         "fee_pct": 0.6, "entry_fee": 6.07, "fees": 6.07}
    t.update(kw)
    return t


class CryptoPureTests(unittest.TestCase):
    def test_tick_rounding_and_rules(self):
        self.assertEqual(broker.px(101234.567, 0.01), 101234.57)
        self.assertEqual(broker.px(0.000012345, 1e-8), 0.00001235)
        self.assertEqual(broker.px(364.175), 364.18)                    # stocks unchanged
        self.assertTrue(broker.off_tick(101234.567, 0.01))
        self.assertFalse(broker.off_tick(101234.57, 0.01))
        self.assertTrue(broker.off_step(0.0123456789, 1e-8))
        self.assertFalse(broker.off_step(0.02946375, 1e-8))
        self.assertEqual(broker.floor_step(0.0123456789, 1e-8), 0.01234567)
        self.assertEqual((broker.price_arg(101250, 0.01), broker.price_arg(364.18)), ("101250.00", "364.18"))

    def test_markets_and_symbols(self):
        self.assertIs(broker.row_market({"symbol": "BTC-USD"}), tc.CRYPTO)
        self.assertIs(broker.row_market({"symbol": "BRK-B", "market": "crypto"}), tc.US)    # the symbol decides
        self.assertEqual(broker.parse_symbol("1inch-usd"), "1INCH-USD")
        self.assertEqual(broker.qty_text({"symbol": "BTC-USD", "qty": 0.02946375}), "0.02946375")
        self.assertEqual(broker.qty_text({"symbol": "TSLA", "qty": 1000}), "1000")
        self.assertEqual(broker.unit_word("ETH-USD"), "ETH")

    def test_fees_on_both_fills_net_pnl_and_r(self):
        t = btc_trade()
        broker.close_trade(t, 104000.0, f"{CDAY}T05:10:00+00:00", "target")
        # gross (104000 - 101234.56) x 0.01 = 27.65; fees 6.07 + 6.24; R = net / 22.3456 price risk
        self.assertEqual((t["gross_pnl"], t["exit_fee"], t["fees"], t["pnl"], t["r_multiple"]),
                         (27.65, 6.24, 12.31, 15.34, 0.69))
        t = btc_trade()
        broker.close_trade(t, 99000.0, f"{CDAY}T05:05:00+00:00", "stop")
        self.assertEqual((t["gross_pnl"], t["fees"], t["pnl"], t["r_multiple"]), (-22.35, 12.01, -34.36, -1.54))
        us = {"id": "T", "symbol": "TSLA", "side": "long", "qty": 8, "entry_price": 364.10, "stop": 358.32}
        broker.close_trade(us, 370.04, f"{DAY}T10:35:00-04:00", "target")
        self.assertEqual(us["pnl"], 47.52)
        self.assertNotIn("fees", us)                                       # stocks: no fee fields

    def test_budget_counts_crypto_fees_at_the_stop(self):
        rows = broker.budget_rows([btc_trade(), {"id": "U", "symbol": "TSLA", "side": "long", "qty": 8,
                                                 "entry_price": 364.18, "stop": 358.32, "status": "open"}])
        # 22.3456 price risk + 0.6% x (101234.56 + 99000) x 0.01 = 12.0141 of fees
        self.assertAlmostEqual(tc.trade_initial_risk(rows[0]), 34.3597, places=3)
        self.assertAlmostEqual(tc.trade_initial_risk(rows[1]), 46.88, places=6)
        self.assertEqual(rows[1]["stop"], 358.32)

    def test_session_rules_follow_the_utc_clock(self):
        with tc.market_scope("BTC-USD"):
            sat = tc.parse_et(f"{CDAY}T04:40")
            self.assertIsNone(broker.order_window_problem(sat, 1435, 15))             # the weekend is open
            late = tc.parse_et(f"{CDAY}T23:57")
            self.assertIn("23:55 UTC time stop", broker.order_window_problem(late, 1435, 15))
            self.assertEqual(broker.preview_banner(late, 1435),
                             "PAST THE 23:55 UTC TIME STOP — preview only. Re-run ticket after 00:00 UTC to get a "
                             "ticket you can confirm.")
            self.assertEqual((broker.parse_time_stop("23:55"), broker.default_time_stop()), (1435, "23:55"))
            with self.assertRaises(ValueError) as cm:
                broker.parse_time_stop("00:00")
            self.assertIn("between 00:05 and 23:59 UTC", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            broker.parse_time_stop("17:00")
        self.assertIn("between 09:35 and 16:00 ET", str(cm.exception))            # stocks unchanged
        self.assertEqual(broker.default_time_stop(), "15:55")

    def test_walk_runs_on_the_utc_day_through_the_night(self):
        bars = crypto_flat("22:00", "23:59") + crypto_flat("00:00", "01:00", day="2026-11-01")
        with tc.market_scope("BTC-USD"):
            bars = broker.normalize_bars(bars)
            self.assertEqual(len(bars), 36)                   # no session filter: every 5-minute slot counts
            r = broker.walk_position("long", 101200, 99000, 104000, utc_ts(CDAY, "22:01"), bars,
                                     utc_ts("2026-11-01", "00:30"), 1435, tick=0.01)
            self.assertEqual((r["reason"], r["price"], tc.et_hhmm(r["ts"])), ("eod", 101200.0, "23:55"))
            r = broker.walk_working("long", 101000, utc_ts(CDAY, "22:01"), bars, utc_ts("2026-11-01", "00:01"),
                                    1435, tick=0.01)
            self.assertEqual(r["status"], "expired")


class CryptoBrokerCase(BrokerCase):
    NOW = f"{CDAY}T04:40"
    SYM = "BTC-USD"

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fx"
        write_coinbase_products(self.fx)
        self.quote(101234.56)

    def quote(self, price, when=f"{CDAY}T04:40:30Z"):
        write_coinbase_quote(self.fx, "BTC-USD", price, when=when)

    def run_main(self, *argv, now=None):
        with mock.patch.dict(os.environ, {"DAYTRADER_FIXTURES": str(self.fx)}):
            return super().run_main(*argv, now=now)

    def cticket(self, *extra, qty="0.01", entry="101250", stop="99000", target="104000", now=None):
        args = ["ticket", "--symbol", self.SYM, "--qty", qty, "--entry", entry, "--stop", stop]
        if target is not None:
            args += ["--target", target]
        return self.run_json(*args, *extra, now=now)

    def cconfirm(self, tid="TKT-20261031-001", now=f"{CDAY}T04:41", yes=True):
        return self.run_json("confirm", tid, *(["--yes"] if yes else []), now=now)

    def open_btc(self):
        code, res = self.cticket()
        self.assertEqual(code, 0, res)
        code, res = self.cconfirm()
        self.assertEqual((code, res["status"]), (0, "filled"), res)
        return res["trade"]


class CryptoTicketTests(CryptoBrokerCase):
    def test_saturday_ticket_is_a_real_ticket_with_fees_and_coinbase_label(self):
        code, res = self.cticket()
        self.assertEqual((code, res["status"], res["preview"]), (0, "pending", False))
        k = res["ticket"]
        self.assertEqual((k["id"], k["market"], k["qty"], k["tick"], k["qty_step"], k["fee_pct"]),
                         ("TKT-20261031-001", "crypto", 0.01, 0.01, 1e-8, 0.6))
        self.assertEqual((k["risk"], k["fees_at_stop"], k["max_loss"]), (22.5, 12.02, 34.52))
        self.assertEqual((k["created_at"], k["expires_at"]), (f"{CDAY}T04:40:00+00:00", f"{CDAY}T04:55:00+00:00"))
        self.assertEqual(res["card"], "BUY 0.01 BTC-USD · LIMIT 101,250.00 · TARGET 104,000.00 · STOP 99,000.00 · "
                                      "PAPER · REAL MONEY: OFF · Coinbase prices · WAITING FOR YOUR YES — reply yes "
                                      "to confirm ticket TKT-20261031-001")
        self.assertTrue(any("round-trip fees ≈ 0.53R at these levels" in n for n in res["notes"]), res["notes"])
        self.assertTrue(any("after fees the R:R is 0.44" in w for w in res["warnings"]), res["warnings"])
        code, text = self.run_main("ticket", "--symbol", "BTC-USD", "--qty", "0.01", "--entry", "101250",
                                   "--stop", "99000", "--target", "104000")
        self.assertIn("  Fees      0.6% per fill (paper) · $12.02 if the stop fills -> loss at the stop $34.52", text)
        self.assertIn("  Expires   04:55 UTC (15 min) · day order · flat by 23:55 UTC", text)
        self.assertIn("replaced the earlier pending BTC-USD ticket(s) TKT-20261031-001", text)

    def test_stock_tickets_carry_their_market_and_stay_unchanged(self):
        code, res = self.ticket(now=f"{DAY}T10:15")
        self.assertEqual((code, res["ticket"]["market"]), (0, "us"))
        self.assertNotIn("fee_pct", res["ticket"])
        self.assertTrue(res["card"].endswith("· PAPER · REAL MONEY: OFF · WAITING FOR YOUR YES — reply yes to "
                                             "confirm ticket TKT-20261030-001"))

    def test_fractional_sizes_ticks_and_whole_shares(self):
        for extra, msg in ((["--qty", "0.0123456789"], "size step 0.00000001 (e.g. 0.01234567)"),
                           (["--qty", "0.000000001"], "size step 0.00000001 (the smallest size is 0.00000001)"),
                           (["--qty", "0"], "--qty must be a positive amount of BTC"),
                           (["--entry", "101250.555"], "not on BTC-USD's price tick 0.01 (e.g. 101250.56)")):
            with self.subTest(extra=extra), self.assertRaises(ValueError) as cm:
                self.cticket(*extra)
            self.assertIn(msg, str(cm.exception))
        with fake_bars({("TSLA", "1m"): quote_bars(100)}), self.assertRaises(ValueError) as cm:
            self.run_main("ticket", "--symbol", "TSLA", "--qty", "8.5", "--entry", "100", "--stop", "99",
                          now=f"{DAY}T10:15")
        self.assertIn("whole number of shares", str(cm.exception))
        self.assertEqual(self.ledger()["tickets"], [])

    def test_past_the_utc_time_stop_is_a_preview_until_midnight(self):
        code, res = self.cticket(now=f"{CDAY}T23:57")
        self.assertEqual((code, res["status"], res["preview"]), (0, "preview", True))
        self.assertEqual(res["banner"], "PAST THE 23:55 UTC TIME STOP — preview only. Re-run ticket after 00:00 UTC "
                                        "to get a ticket you can confirm.")
        self.assertIn("· Coinbase prices · PAST THE 23:55 UTC TIME STOP", res["card"])
        self.assertEqual(self.ledger()["tickets"], [])
        code, res = self.cticket(now="2026-11-01T00:01")                     # Sunday, a new UTC day
        self.assertEqual((code, res["status"], res["ticket"]["id"]), (0, "pending", "TKT-20261101-001"))

    def test_budget_counts_fees_and_blocks_on_the_loss_at_the_stop(self):
        # price risk 0.06 x 2,250 = $135 fits the $150 daily max loss; with $72.09 of fees at the stop it does not
        code, res = self.cticket(qty="0.06")
        self.assertEqual((code, res["blocked"]), (broker.EXIT_BLOCKED, True))
        self.assertTrue(any("$207.09" in r for r in res["reasons"]), res["reasons"])
        code, text = self.run_main("ticket", "--symbol", "BTC-USD", "--qty", "0.06", "--entry", "101250",
                                   "--stop", "99000", "--target", "104000")
        self.assertIn("Risk $207.09 (incl. $72.09 of fees at the stop) - BLOCKED by today's budget:", text)
        self.assertEqual(self.ledger()["tickets"], [])


class CryptoConfirmTests(CryptoBrokerCase):
    def test_the_yes_rule_holds_on_a_saturday(self):
        code, res = self.cticket()
        tid = res["ticket"]["id"]
        code, res = self.cconfirm(tid, yes=False)
        self.assertEqual((code, res["confirmed"]), (1, False))
        self.assertIn("in a later message", res["reason"])
        with self.assertRaises(ValueError) as cm:                       # same breath: refused
            self.cconfirm(tid, now=f"{CDAY}T04:40:05")
        self.assertIn("s ago", str(cm.exception))
        code, res = self.cconfirm(tid)
        self.assertEqual((code, res["status"], res["fill_price"]), (0, "filled", 101234.56))
        tr = res["trade"]
        self.assertEqual((tr["id"], tr["market"], tr["qty"], tr["entry_fee"], tr["fees"], tr["opened_at"]),
                         ("TRADE-20261031-001", "crypto", 0.01, 6.07, 6.07, f"{CDAY}T04:41:00+00:00"))
        with self.assertRaises(ValueError) as cm:
            self.cconfirm(tid, now=f"{CDAY}T04:42")
        self.assertIn("already confirmed", str(cm.exception))
        code, text = self.run_main("tickets", "--market", "crypto", now=f"{CDAY}T04:42")
        self.assertIn("TKT-20261031-001  confirmed  BUY 0.01 BTC-USD · LIMIT 101,250.00", text)

    def test_expiry_and_the_time_stop_still_apply(self):
        self.cticket()
        with self.assertRaises(ValueError) as cm:
            self.cconfirm(now=f"{CDAY}T04:56")
        self.assertIn("expired at 04:55 UTC without a yes", str(cm.exception))
        code, res = self.cticket(now=f"{CDAY}T23:50")
        self.assertEqual(res["ticket"]["expires_at"], f"{CDAY}T23:55:00+00:00")     # clipped to the time stop
        with self.assertRaises(ValueError) as cm:
            self.cconfirm(res["ticket"]["id"], now=f"{CDAY}T23:56")
        self.assertIn("expired", str(cm.exception))
        self.assertEqual(self.ledger()["trades"], [])

    def test_budget_is_rechecked_at_confirm_with_fees(self):
        code, res = self.cticket(qty="0.03")                       # $67.50 + $36.05 fees = $103.55 now: OK
        self.assertEqual(code, 0, res)
        led = self.ledger()
        led["trades"].append(btc_trade(id="TRADE-20261031-000", status="closed", pnl=-60.0, r_multiple=-1.0,
                                       exit_price=98000.0, closed_at=f"{CDAY}T03:00:00+00:00",
                                       opened_at=f"{CDAY}T02:00:00+00:00"))
        tc.save_ledger(led)
        # $60 lost today + $67.04 price risk fits $150; + $36.04 of fees does not -> blocked, still pending
        code, res = self.cconfirm()
        self.assertEqual((code, res["blocked"]), (broker.EXIT_BLOCKED, True))
        self.assertEqual([k["status"] for k in self.ledger()["tickets"]], ["pending"])

    def test_working_order_fills_later_and_pays_its_fee(self):
        code, res = self.cticket(entry="101000")
        code, res = self.cconfirm()
        self.assertEqual((code, res["status"]), (0, "working"))
        self.assertIn("before 23:55 UTC; otherwise it expires (day order)", broker.render_confirm(res))
        code, text = self.run_main("tickets", now=f"{CDAY}T04:42")        # the US view labels the UTC row
        self.assertIn("04:40 UTC  until 23:55", text)
        five = crypto_flat("04:40", "05:00") + [cbar("05:00", 101100, 101150, 100900, 101050)]
        with fake_bars({("BTC-USD", "5m"): five}):
            code, res = self.run_json("sync", "--market", "crypto", now=f"{CDAY}T05:06")
        ev = res["events"][0]
        self.assertEqual((ev["kind"], ev["price"], ev["market"]), ("filled", 101000.0, "crypto"))
        tr = self.ledger()["trades"][0]
        self.assertEqual((tr["entry_price"], tr["entry_fee"], tr["fee_pct"]), (101000.0, 6.06, 0.6))


class CryptoSyncTests(CryptoBrokerCase):
    def test_stop_hit_is_net_of_fees_in_dollars_and_r(self):
        self.open_btc()
        five = crypto_flat("04:40", "05:00") + [cbar("05:00", 101200, 101300, 98900, 99100)]
        with fake_bars({("BTC-USD", "5m"): five}):
            code, text = self.run_main("sync", "--market", "crypto", now=f"{CDAY}T05:10")
        self.assertIn("SYNC · 05:10 UTC · paper ledger", text)
        self.assertIn("TRADE-20261031-001 BTC-USD LONG 0.01: STOP @ 99,000.00 in the 05:00 UTC bar · -$34.36 -1.54R "
                      "(net of $12.01 fees)", text)
        self.assertIn("Today: realized -$34.36", text)
        t = self.ledger()["trades"][0]
        self.assertEqual((t["exit_reason"], t["closed_at"], t["gross_pnl"], t["fees"], t["pnl"], t["r_multiple"]),
                         ("stop", f"{CDAY}T05:05:00+00:00", -22.35, 12.01, -34.36, -1.54))

    def test_us_view_sync_fetches_crypto_bars_at_the_same_instant(self):
        # A naive $DAYTRADER_NOW is read in the active market's zone. A symbol-less sync (US view: ET)
        # that replays a crypto trade must not let Coinbase re-read it as UTC - 4 h earlier here.
        self.open_btc()
        five = crypto_flat("04:40", "05:00") + [cbar("05:00", 101200, 101300, 98900, 99100)]
        seen = []

        def fake(symbol, interval="5m", range_="60d", prepost=False, ttl=None):
            seen.append((tc.market().key, tc.now_et().timestamp()))
            return list(five), {"symbol": symbol}
        with mock.patch.object(tc, "fetch_bars", side_effect=fake):
            code, res = self.run_json("sync", now=f"{CDAY}T01:10")       # 01:10 ET = 05:10 UTC
        self.assertEqual(seen, [("crypto", utc_ts(CDAY, "05:10"))])
        self.assertEqual([(e["reason"], e["price"]) for e in res["events"]], [("stop", 99000.0)])
        self.assertEqual(os.environ.get("DAYTRADER_NOW"), self.NOW)          # restored afterwards

    def test_time_stop_at_2355_utc(self):
        self.open_btc()
        five = crypto_flat("04:40", "23:59")
        with fake_bars({("BTC-USD", "5m"): five}):
            code, res = self.run_json("sync", "--market", "crypto", now="2026-11-01T00:10")
        ev = res["events"][0]
        self.assertEqual((ev["reason"], ev["price"], ev["at"]), ("eod", 101200.0, f"{CDAY}T23:55:00+00:00"))
        self.assertEqual((ev["gross_pnl"], ev["fees"], ev["pnl"]), (-0.35, 12.14, -12.49))

    def test_positions_history_and_close_are_net_of_fees(self):
        tr = self.open_btc()
        self.quote(102000.0, when=f"{CDAY}T04:45:00Z")
        code, res = self.run_json("positions", "--market", "crypto", now=f"{CDAY}T04:45")
        p = res["positions"][0]
        # gross (102000 - 101234.56) x 0.01 = 7.65; fees if closed now 6.07 + 6.12
        self.assertEqual((p["unrealized_gross"], p["fees_if_closed"], p["unrealized"]), (7.65, 12.19, -4.54))
        code, text = self.run_main("positions", "--market", "crypto", now=f"{CDAY}T04:45")
        self.assertIn("PAPER POSITIONS · 04:45 UTC · REAL MONEY: OFF", text)
        self.assertIn("Crypto P&L is net of the paper fees of both fills", text)
        five = crypto_flat("04:40", "04:45")
        with fake_bars({("BTC-USD", "5m"): five}):
            code, res = self.run_json("close", tr["id"], "--price", "101250", now=f"{CDAY}T04:46")
        t = res["trade"]
        self.assertEqual((t["exit_reason"], t["exit_price"], t["fees"], t["pnl"]), ("manual", 101250.0, 12.15, -12.0))
        with fake_bars({("BTC-USD", "5m"): five}), self.assertRaises(ValueError) as cm:
            self.run_main("close", tr["id"], "--price", "105000", now=f"{CDAY}T04:47")
        self.assertIn("already closed", str(cm.exception))
        code, res = self.run_json("history", "--market", "crypto", now=f"{CDAY}T05:00")
        self.assertEqual((res["totals"]["net_pnl"], res["totals"]["fees"], res["totals"]["gross_pnl"]),
                         (-12.0, 12.15, 0.15))
        code, text = self.run_main("history", "--market", "crypto", now=f"{CDAY}T05:00")
        self.assertIn("· crypto fees $12.15 (P&L and R are net of them)", text)

    def test_a_mixed_ledger_replays_every_trade_in_its_own_market(self):
        us = {"id": "TRADE-20261030-001", "ticket_id": "TKT-20261030-001", "symbol": "TSLA", "market": "us",
              "side": "long", "qty": 40, "entry_price": 100.0, "stop": 99.0, "target": 102.0, "time_stop": "15:55",
              "opened_at": f"{DAY}T10:16:00-04:00", "status": "open"}
        tc.save_ledger({"tickets": [], "trades": [us, btc_trade()]})
        five = {("TSLA", "5m"): flat_session(), ("BTC-USD", "5m"): crypto_flat("04:40", "05:00")}
        with fake_bars(five):
            code, text = self.run_main("sync", "--market", "crypto", now=f"{CDAY}T05:00")
        self.assertIn("TRADE-20261030-001 TSLA LONG 40: TIME STOP @ 100.00 in the 15:50 ET bar", text)
        self.assertIn("open: TRADE-20261031-001 BTC-USD LONG 0.01 @ 101,234.56", text)
        self.assertIn("(checked through 05:00 UTC)", text)
        led = self.ledger()
        self.assertEqual(led["trades"][0]["closed_at"], f"{DAY}T15:55:00-04:00")     # dated in its own zone
        self.assertEqual(led["trades"][1]["status"], "open")


# ---------------------------------------------------------------------------
# Crypto on Binance (the default exchange), and every row priced on its own exchange
# ---------------------------------------------------------------------------

class ExchangeRowTests(TempHomeTestCase):
    def test_rows_name_their_exchange_and_the_label_follows_it(self):
        self.assertEqual((broker.unit_word("BTCUSDT"), broker.unit_word("BTC-USDT")), ("BTC", "BTC"))
        self.assertEqual(broker.row_exchange({"symbol": "BTC-USDT", "exchange": "Binance"}), "binance")
        self.assertEqual(broker.row_exchange({"symbol": "BTC-USD"}), "coinbase")      # rows from v1.1.0 were Coinbase's
        self.assertIsNone(broker.row_exchange({"symbol": "TSLA", "exchange": "binance"}))
        self.assertEqual(broker.paper_label({"symbol": "BTC-USDT", "exchange": "binance"}),
                         "PAPER · REAL MONEY: OFF · Binance prices")
        self.assertEqual(broker.paper_label({"symbol": "BTC-USD"}), "PAPER · REAL MONEY: OFF · Coinbase prices")
        self.assertEqual(broker.paper_label({"symbol": "TSLA", "exchange": "binance"}), "PAPER · REAL MONEY: OFF")
        self.assertEqual((broker.min_order_text(5.0, "BTC-USD", "binance"), broker.min_order_text(1.0, "BTC-USD", "coinbase"),
                          broker.min_order_text(0.0001, "ETH-BTC", "binance")), ("5 USDT", "$1.00", "0.0001 BTC"))
        self.assertEqual((broker.feed_key({"symbol": "btc-usd"}), broker.feed_key({"symbol": "TSLA"})),
                         (("BTC-USD", "coinbase"), ("TSLA", None)))

    def test_exchange_scope_routes_tc_and_puts_the_setting_back(self):
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "binance"
        with broker.exchange_scope("coinbase"):
            self.assertEqual((tc.crypto_exchange(), tc.CRYPTO.source), ("coinbase", "Coinbase Exchange"))
        self.assertEqual(os.environ["DAYTRADER_CRYPTO_EXCHANGE"], "binance")
        del os.environ["DAYTRADER_CRYPTO_EXCHANGE"]
        with broker.exchange_scope("binance"):
            self.assertEqual(tc.crypto_exchange(), "binance")
        self.assertNotIn("DAYTRADER_CRYPTO_EXCHANGE", os.environ)
        with self.assertRaises(tc.StateError) as cm, broker.exchange_scope("kraken"):
            pass
        self.assertIn("ledger.json names exchange 'kraken'", str(cm.exception))


class BinanceBrokerCase(CryptoBrokerCase):
    """The crypto cases on Binance: BTC-USDT (step 0.00001, tick 0.01, minimum 5 USDT), 0.1% per fill."""
    SYM = "BTC-USDT"

    def setUp(self):
        super().setUp()                    # Coinbase fixtures too, for the tests that switch exchanges
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "binance"
        write_binance_products(self.fx)

    def quote(self, price, when=f"{CDAY}T04:40:30Z"):
        write_binance_quote(self.fx, self.SYM, price, when_ts=tc.parse_iso_utc(when))


class BinanceTicketTests(BinanceBrokerCase):
    def test_saturday_ticket_on_binance_prices_step_and_fee(self):
        code, res = self.cticket()
        self.assertEqual((code, res["status"], res["preview"]), (0, "pending", False))
        k = res["ticket"]
        self.assertEqual((k["market"], k["exchange"], k["qty"], k["tick"], k["qty_step"], k["fee_pct"]),
                         ("crypto", "binance", 0.01, 0.01, 1e-05, 0.1))
        # $22.50 of price risk + 0.1% x (101250 + 99000) x 0.01 = $2.00 of fees at the stop
        self.assertEqual((k["risk"], k["fees_at_stop"], k["max_loss"]), (22.5, 2.0, 24.5))
        self.assertEqual(res["card"], "BUY 0.01 BTC-USDT · LIMIT 101,250.00 · TARGET 104,000.00 · STOP 99,000.00 · PAPER "
                                      "· REAL MONEY: OFF · Binance prices · WAITING FOR YOUR YES — reply yes to confirm "
                                      "ticket TKT-20261031-001")
        code, text = self.run_main("ticket", "--symbol", "BTC-USDT", "--qty", "0.01", "--entry", "101250", "--stop",
                                   "99000", "--target", "104000")
        self.assertIn("  Fees      0.1% per fill (paper) · $2.00 if the stop fills -> loss at the stop $24.50", text)
        self.assertIn("  Budget    OK · $125.50 of $150.00 daily loss budget left after this", text)
        self.assertNotIn("Coinbase", text)

    def test_binance_pair_rules_minimum_order_usd_symbols_and_shorts(self):
        with self.assertRaises(ValueError) as cm:
            self.cticket(qty="0.000015")
        self.assertIn("--qty 0.000015 is not a multiple of BTC-USDT's size step 0.00001 (e.g. 0.00001)", str(cm.exception))
        code, res = self.cticket(qty="0.00004")                              # $4.05 of BTC
        self.assertIn("notional $4.05 is below Binance's 5 USDT minimum order for BTC-USDT - a real order this small "
                      "would be rejected", res["warnings"])
        self.assertFalse(any("minimum order" in w for w in self.cticket()[1]["warnings"]))
        code, res = self.run_json("ticket", "--symbol", "BTC-USD", "--qty", "0.01", "--entry", "101250", "--stop", "99000")
        self.assertEqual((res["ticket"]["exchange"], res["ticket"]["qty_step"]), ("binance", 1e-05))
        self.assertIn("BTC-USD is priced from Binance's BTCUSDT pair (Binance's few USD pairs are thin; USDT trades close to, "
                      "not exactly at, $1)", res["notes"])
        code, res = self.cticket(stop="102000", target="100000")
        self.assertTrue(any(n.startswith("spot crypto cannot be sold short on Binance (that needs margin or derivatives)")
                            for n in res["notes"]), res["notes"])

    def test_offline_and_sizing_from_another_exchange(self):
        fx, self.fx = self.fx, self.home / "nofx"          # no fixtures at all: offline
        self.fx.mkdir()
        try:
            code, res = self.run_json("ticket", "--symbol", "BTC-USDT", "--qty", "0.01", "--entry", "101250",
                                      "--stop", "99000")
        finally:
            self.fx = fx
        self.assertEqual((code, res["ticket"]["qty_step"], res["ticket"]["exchange"]), (0, 1e-8, "binance"))
        self.assertTrue(any(n.startswith("Binance pair rules unavailable") for n in res["notes"]), res["notes"])
        tc.save_plan({"symbol": "BTC-USDT", "side": "long", "entry": 101250.0, "stop": 99000.0, "target": 104000.0,
                      "sizing": {"shares": 0.01234567, "side": "long", "entry": 101250.0, "stop": 99000.0,
                                 "market": "crypto", "exchange": "coinbase", "step": 1e-8, "fee_pct": 0.6}})
        code, res = self.run_json("ticket", "--symbol", "BTC-USDT")
        self.assertEqual((code, res["ticket"]["qty"], res["ticket"]["qty_source"]), (0, 0.01234, "plan sizing"))
        self.assertIn("the saved BTC-USDT sizing was made for Coinbase (its size step and fee); this ticket uses Binance "
                      "prices - re-size it: risk.py size --symbol BTC-USDT --save", res["warnings"])


class BinanceFillSyncTests(BinanceBrokerCase):
    def test_fill_and_stop_are_net_of_binance_fees(self):
        tr = self.open_btc()
        self.assertEqual((tr["symbol"], tr["exchange"], tr["entry_price"], tr["fee_pct"], tr["entry_fee"]),
                         ("BTC-USDT", "binance", 101234.56, 0.1, 1.01))
        code, text = self.run_main("positions", "--market", "crypto", now=f"{CDAY}T04:45")
        self.assertIn("Crypto P&L is net of the paper fees of both fills (Binance prices).", text)
        five = crypto_flat("04:40", "05:00") + [cbar("05:00", 101200, 101300, 98900, 99100)]
        with fake_bars({("BTC-USDT", "5m"): five}):
            code, res = self.run_json("sync", "--market", "crypto", now=f"{CDAY}T05:10")
        ev = res["events"][0]
        # gross -22.35; fees 1.01 + 0.99 (0.1% of each fill); net -24.35 = -1.09R of the 22.35 price risk
        self.assertEqual((ev["reason"], ev["price"], ev["exchange"], ev["gross_pnl"], ev["fees"], ev["pnl"],
                          ev["r_multiple"]), ("stop", 99000.0, "binance", -22.35, 2.0, -24.35, -1.09))
        self.assertEqual(res["summary"]["realized_pnl"], -24.35)
        self.assertIn("BTC-USDT LONG 0.01: STOP @ 99,000.00 in the 05:00 UTC bar · -$24.35 -1.09R (net of $2.00 fees)",
                      broker.render_sync(res))

    def test_a_ticket_confirmed_after_the_switch_fills_on_its_own_exchange(self):
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "coinbase"
        write_coinbase_quote(self.fx, "BTC-USD", 101234.56, when=f"{CDAY}T04:40:30Z")
        code, res = self.run_json("ticket", "--symbol", "BTC-USD", "--qty", "0.01", "--entry", "101250", "--stop", "99000",
                                  "--target", "104000")
        self.assertEqual((res["ticket"]["exchange"], res["ticket"]["fee_pct"]), ("coinbase", 0.6))
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "binance"          # the user switches before saying yes
        write_binance_quote(self.fx, "BTC-USD", 100500.0, when_ts=utc_ts(CDAY, "04:40") + 30)    # Binance is lower
        code, res = self.cconfirm()
        tr = res["trade"]
        self.assertEqual((code, res["fill_price"], tr["exchange"], tr["fee_pct"], tr["entry_fee"]),
                         (0, 101234.56, "coinbase", 0.6, 6.07))                  # Coinbase's price and fee, not Binance's
        self.assertIn("TKT-20261031-001 was made on Coinbase prices, so it fills and is managed on Coinbase (at its 0.6% "
                      "fee); the crypto_exchange setting now says Binance, which new tickets use", res["notes"])
        self.assertIn("· PAPER · REAL MONEY: OFF · Coinbase prices", broker.render_confirm(res))
        self.assertEqual(os.environ["DAYTRADER_CRYPTO_EXCHANGE"], "binance")

    def test_sync_positions_and_close_price_every_row_on_its_own_exchange(self):
        b = btc_trade(exchange="binance", fee_pct=0.1, entry_fee=1.01, fees=1.01)
        c = btc_trade(id="TRADE-20261031-002", ticket_id="TKT-20261031-002")   # no exchange field: a Coinbase row
        tc.save_ledger({"tickets": [], "trades": [b, c]})
        write_binance_quote(self.fx, "BTC-USD", 101500.0, when_ts=utc_ts(CDAY, "04:45"))
        write_coinbase_quote(self.fx, "BTC-USD", 102000.0, when=f"{CDAY}T04:45:00Z")
        code, res = self.run_json("positions", "--market", "crypto", now=f"{CDAY}T04:45")
        self.assertEqual({p["id"]: (p["exchange"], p["last"]) for p in res["positions"]},
                         {b["id"]: ("binance", 101500.0), c["id"]: ("coinbase", 102000.0)})
        code, text = self.run_main("positions", "--market", "crypto", now=f"{CDAY}T04:45")
        self.assertIn("Crypto P&L is net of the paper fees of both fills (Binance and Coinbase prices).", text)
        flat = crypto_flat("04:40", "05:05")
        drop = flat[:-1] + [cbar("05:00", 101200, 101300, 98900, 99100)]          # only Coinbase trades through 99,000
        seen = []

        def fake(symbol, interval="5m", range_="60d", prepost=False, ttl=None):
            seen.append((symbol, tc.crypto_exchange()))
            return list(drop if tc.crypto_exchange() == "coinbase" else flat), {"symbol": symbol}
        with mock.patch.object(tc, "fetch_bars", side_effect=fake):
            code, res = self.run_json("sync", "--market", "crypto", now=f"{CDAY}T05:06")
        self.assertEqual(seen, [("BTC-USD", "binance"), ("BTC-USD", "coinbase")])
        self.assertEqual([(e["trade_id"], e["exchange"], e["reason"]) for e in res["events"]], [(c["id"], "coinbase", "stop")])
        self.assertEqual([(o["trade_id"], o["exchange"]) for o in res["open"]], [(b["id"], "binance")])
        self.assertEqual(os.environ["DAYTRADER_CRYPTO_EXCHANGE"], "binance")
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "coinbase"                # the Binance trade still closes on Binance
        with fake_bars({("BTC-USD", "5m"): flat}):
            code, res = self.run_json("close", b["id"], now=f"{CDAY}T05:06")
        self.assertEqual((res["closed_by"], res["trade"]["exit_price"], res["quote"]["price"], res["trade"]["fee_pct"]),
                         ("manual", 101500.0, 101500.0, 0.1))                  # Binance's quote, not Coinbase's 102,000
        self.assertIn("· Binance prices", broker.render_close(res))

    def test_an_unknown_exchange_is_reported_not_priced_elsewhere(self):
        tc.save_ledger({"tickets": [], "trades": [btc_trade(symbol="BTC-USDT", exchange="binance", fee_pct=0.1),
                                                  btc_trade(id="TRADE-20261031-002", exchange="kraken")]})
        with fake_bars({("BTC-USDT", "5m"): crypto_flat("04:40", "05:00")}):
            code, res = self.run_json("sync", "--market", "crypto", now=f"{CDAY}T05:01")
        self.assertEqual([(e["symbol"], e["exchange"]) for e in res["errors"]], [("BTC-USD", "kraken")])
        self.assertIn("ledger.json names exchange 'kraken' - expected binance or coinbase", res["errors"][0]["error"])
        self.assertEqual([t["status"] for t in self.ledger()["trades"]], ["open", "open"])


class BinanceSafetyTests(BinanceBrokerCase):
    """The safety rules on the default exchange, as CryptoTicketTests / CryptoConfirmTests check them on
    Coinbase: a preview past the time stop has nothing to confirm, the yes rule holds on a Saturday, and
    the daily budget (Binance's 0.1% fees at the stop included) is enforced at the ticket and again at yes."""

    def test_past_the_utc_time_stop_is_a_preview_with_nothing_to_confirm(self):
        code, res = self.cticket(now=f"{CDAY}T23:57")
        self.assertEqual((code, res["status"], res["preview"]), (0, "preview", True))
        self.assertIn("· Binance prices · PAST THE 23:55 UTC TIME STOP", res["card"])
        self.assertNotIn("id", res["order"])                                  # a preview has no ticket id
        self.assertEqual(self.ledger()["tickets"], [])
        with self.assertRaises(ValueError) as cm:                               # nothing was saved to say yes to
            self.cconfirm(now=f"{CDAY}T23:58")
        self.assertIn("no ticket TKT-20261031-001", str(cm.exception))
        code, res = self.cticket(now="2026-11-01T00:01")                     # Sunday, a new UTC day
        self.assertEqual((code, res["status"], res["ticket"]["id"]), (0, "pending", "TKT-20261101-001"))

    def test_the_yes_rule_holds_on_a_saturday(self):
        code, res = self.cticket()
        tid = res["ticket"]["id"]
        code, res = self.cconfirm(tid, yes=False)
        self.assertEqual((code, res["confirmed"]), (1, False))
        self.assertIn("in a later message", res["reason"])
        with self.assertRaises(ValueError) as cm:                             # same breath: refused
            self.cconfirm(tid, now=f"{CDAY}T04:40:05")
        self.assertIn("s ago", str(cm.exception))
        self.assertEqual(([k["status"] for k in self.ledger()["tickets"]], self.ledger()["trades"]), (["pending"], []))
        code, res = self.cconfirm(tid)
        self.assertEqual((code, res["status"], res["trade"]["exchange"], res["trade"]["fee_pct"]),
                         (0, "filled", "binance", 0.1))

    def test_the_budget_counts_binance_fees_at_the_ticket_and_again_at_yes(self):
        # 0.065 BTC: $146.25 of price risk fits the $150 daily max loss; + $13.02 of fees at the stop does not
        code, res = self.cticket(qty="0.065")
        self.assertEqual((code, res["blocked"]), (broker.EXIT_BLOCKED, True))
        self.assertTrue(any("$159.27" in r for r in res["reasons"]), res["reasons"])
        self.assertEqual(self.ledger()["tickets"], [])
        code, res = self.cticket(qty="0.06")                                  # $135 + $12.02 = $147.02: OK now
        self.assertEqual((code, res["status"]), (0, "pending"), res)
        led = self.ledger()
        led["trades"].append(btc_trade(id="TRADE-20261031-000", symbol="BTC-USDT", exchange="binance", fee_pct=0.1,
                                       status="closed", pnl=-10.0, r_multiple=-0.5, exit_price=98000.0,
                                       closed_at=f"{CDAY}T03:00:00+00:00", opened_at=f"{CDAY}T02:00:00+00:00"))
        tc.save_ledger(led)
        # at yes: $10 lost today + $134.07 price risk at the 101,234.56 fill fits $150; + $12.01 of fees does not
        code, res = self.cconfirm()
        self.assertEqual((code, res["confirmed"], res["blocked"]), (broker.EXIT_BLOCKED, False, True))
        self.assertEqual([k["status"] for k in self.ledger()["tickets"]], ["pending"])
        self.assertEqual([t["id"] for t in self.ledger()["trades"]], ["TRADE-20261031-000"])


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

    def test_sandbox_state_and_manual_price_rules_are_spelled_out(self):
        # The README tells the user; Claude only sees SKILL.md once installed, so it says it too.
        flat = " ".join(self.skill.split())
        for phrase in ("(`ledger.json`) and `settings.json` live in `$DAYTRADER_HOME`", "may be gone next conversation",
                       "offer both as files to download", "`--price` must be a price that traded since the fill"):
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

    def test_weekend_ticket_is_a_preview_cli(self):
        fx = self.home / "fx"
        write_fixture(fx, yahoo_chart_url("TSLA", "1m", "1d"), yahoo_payload(quote_bars(368.14), "TSLA", price=368.14))
        env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(fx), "DAYTRADER_NOW": "2026-10-31T11:00"}
        r = run_cli(SCRIPT, "ticket", "--symbol", "TSLA", "--qty", "8", "--entry", "364.18", "--stop", "358.32",
                    "--target", "370.04", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ORDER TICKET · PREVIEW", r.stdout)
        self.assertIn("MARKET CLOSED — preview only. Re-run ticket after 09:30 ET", r.stdout)
        self.assertNotIn("WAITING FOR YOUR YES", r.stdout)
        r = run_cli(SCRIPT, "tickets", "--all", "--json", env=env)
        self.assertEqual(json.loads(r.stdout)["tickets"], [])

    def test_crypto_saturday_ticket_confirm_sync_budget_cli(self):
        """BTC-USD on Saturday 2026-10-31 (UTC): ticket -> yes in a later step -> the target fills on a later
        bar -> P&L net of fees -> the budget's UTC day shows it. Coinbase fixtures only, no network."""
        fx = self.home / "fx"
        write_coinbase_products(fx)
        write_coinbase_quote(fx, "BTC-USD", 101234.56, when=f"{CDAY}T04:40:30Z")
        five = crypto_flat("00:00", "05:30", start_day="2026-10-30")
        i = next(n for n, b in enumerate(five) if b.ts == utc_ts(CDAY, "05:05"))
        five[i] = cbar("05:05", 101200, 104100, 101100, 103900)               # the target trades at 05:05
        write_coinbase_fixtures(fx, "BTC-USD", five, "5m", "2d", now_ts=utc_ts(CDAY, "05:30"))
        env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(fx)}
        budget_script = skill_script("budget", "budget.py")

        def cli(now, *args, script=SCRIPT):
            return run_cli(script, *args, env=dict(env, DAYTRADER_NOW=now))

        r = cli(f"{CDAY}T04:40", "ticket", "--symbol", "BTC-USD", "--qty", "0.01", "--entry", "101250",
                "--stop", "99000", "--target", "104000")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("BUY 0.01 BTC-USD · LIMIT 101,250.00 · TARGET 104,000.00 · STOP 99,000.00 · PAPER · "
                      "REAL MONEY: OFF · Coinbase prices", r.stdout)
        self.assertIn("WAITING FOR YOUR YES — reply yes to confirm ticket TKT-20261031-001", r.stdout)
        self.assertNotIn("PREVIEW", r.stdout)                                   # the weekend is open for crypto
        r = cli(f"{CDAY}T04:41", "confirm", "TKT-20261031-001")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT CONFIRMED", r.stdout)
        r = cli(f"{CDAY}T04:41", "confirm", "TKT-20261031-001", "--yes", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual((res["status"], res["fill_price"], res["trade"]["market"], res["trade"]["entry_fee"]),
                         ("filled", 101234.56, "crypto", 6.07))
        r = cli(f"{CDAY}T05:30", "sync", "--market", "crypto")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TARGET @ 104,000.00 in the 05:05 UTC bar · +$15.34 +0.69R (net of $12.31 fees)", r.stdout)
        r = cli(f"{CDAY}T05:30", "history", "--market", "crypto", "--json")
        tot = json.loads(r.stdout)["totals"]
        self.assertEqual((tot["net_pnl"], tot["fees"], tot["gross_pnl"], tot["avg_r"]), (15.34, 12.31, 27.65, 0.69))
        r = cli(f"{CDAY}T05:31", "status", "--market", "crypto", script=budget_script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RISKED SO FAR · Sat 2026-10-31 · 05:31 UTC (market open)", r.stdout)
        self.assertIn("realized P&L  +$15.34  (1 closed: 1 W / 0 L)", r.stdout)
        st = json.loads(cli(f"{CDAY}T05:31", "status", "--market", "crypto", "--json", script=budget_script).stdout)
        self.assertEqual((st["date"], st["realized_pnl"], st["trades_today"], st["status"]),
                         (CDAY, 15.34, 1, "OPEN FOR TRADING"))

    def binance_cli_flow(self, exit_bar):
        """BTC-USDT on Binance (the default exchange), Saturday 2026-10-31 04:40 UTC: ticket -> yes in a later
        step -> `exit_bar` (05:05) takes the trade out -> P&L net of 0.1% fees on both fills -> the budget's
        UTC day. Binance fixtures only, no network. Returns (sync stdout, history totals, budget status JSON)."""
        fx = self.home / "fx"
        write_binance_products(fx)
        write_binance_quote(fx, "BTC-USDT", 101234.56, when_ts=utc_ts(CDAY, "04:40") + 30)
        five = crypto_flat("00:00", "05:30", start_day="2026-10-30")
        i = next(n for n, b in enumerate(five) if b.ts == utc_ts(CDAY, "05:05"))
        five[i] = exit_bar
        write_binance_fixtures(fx, "BTC-USDT", five, "5m", "2d", now_ts=utc_ts(CDAY, "05:30"))
        env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(fx), "DAYTRADER_CRYPTO_EXCHANGE": "binance"}
        budget_script = skill_script("budget", "budget.py")

        def cli(now, *args, script=SCRIPT):
            return run_cli(script, *args, env=dict(env, DAYTRADER_NOW=now))

        r = cli(f"{CDAY}T04:40", "ticket", "--symbol", "BTC-USDT", "--qty", "0.01", "--entry", "101250",
                "--stop", "99000", "--target", "104000")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("BUY 0.01 BTC-USDT · LIMIT 101,250.00 · TARGET 104,000.00 · STOP 99,000.00 · PAPER · "
                      "REAL MONEY: OFF · Binance prices", r.stdout)
        self.assertIn("  Fees      0.1% per fill (paper) · $2.00 if the stop fills -> loss at the stop $24.50", r.stdout)
        self.assertIn("WAITING FOR YOUR YES — reply yes to confirm ticket TKT-20261031-001", r.stdout)
        self.assertNotIn("PREVIEW", r.stdout)                                   # the weekend is open for crypto
        self.assertNotIn("Coinbase", r.stdout)
        r = cli(f"{CDAY}T04:41", "confirm", "TKT-20261031-001")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT CONFIRMED", r.stdout)
        r = cli(f"{CDAY}T04:41", "confirm", "TKT-20261031-001", "--yes", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        tr = res["trade"]
        self.assertEqual((res["status"], res["fill_price"], tr["exchange"], tr["fee_pct"], tr["entry_fee"]),
                         ("filled", 101234.56, "binance", 0.1, 1.01))
        sync = cli(f"{CDAY}T05:30", "sync", "--market", "crypto")
        self.assertEqual(sync.returncode, 0, sync.stderr)
        r = cli(f"{CDAY}T05:30", "history", "--market", "crypto", "--json")
        totals = json.loads(r.stdout)["totals"]
        r = cli(f"{CDAY}T05:31", "status", "--market", "crypto", script=budget_script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RISKED SO FAR · Sat 2026-10-31 · 05:31 UTC (market open)", r.stdout)
        st = json.loads(cli(f"{CDAY}T05:31", "status", "--market", "crypto", "--json", script=budget_script).stdout)
        led = tc.load_ledger()
        self.assertEqual(([k["exchange"] for k in led["tickets"]], [t["exchange"] for t in led["trades"]]),
                         (["binance"], ["binance"]))
        return sync.stdout, totals, st, r.stdout

    def test_binance_saturday_ticket_confirm_sync_target_budget_cli(self):
        text, tot, st, card = self.binance_cli_flow(cbar("05:05", 101200, 104100, 101100, 103900))
        # gross (104000 - 101234.56) x 0.01 = 27.65; fees 1.01 + 1.04; R = 25.60 / the 22.35 price risk
        self.assertIn("TRADE-20261031-001 BTC-USDT LONG 0.01: TARGET @ 104,000.00 in the 05:05 UTC bar · +$25.60 +1.15R "
                      "(net of $2.05 fees)", text)
        self.assertEqual((tot["net_pnl"], tot["fees"], tot["gross_pnl"], tot["avg_r"]), (25.6, 2.05, 27.65, 1.15))
        self.assertIn("realized P&L  +$25.60  (1 closed: 1 W / 0 L)", card)
        self.assertEqual((st["date"], st["realized_pnl"], st["trades_today"], st["status"], st["loss_budget"]["used"]),
                         (CDAY, 25.6, 1, "OPEN FOR TRADING", 0.0))

    def test_binance_saturday_ticket_confirm_sync_stop_budget_cli(self):
        text, tot, st, card = self.binance_cli_flow(cbar("05:05", 101200, 101300, 98900, 99100))
        # gross (99000 - 101234.56) x 0.01 = -22.35; fees 1.01 + 0.99; net -24.35 = -1.09R of the price risk
        self.assertIn("TRADE-20261031-001 BTC-USDT LONG 0.01: STOP @ 99,000.00 in the 05:05 UTC bar · -$24.35 -1.09R "
                      "(net of $2.00 fees)", text)
        self.assertEqual((tot["net_pnl"], tot["fees"], tot["gross_pnl"], tot["avg_r"]), (-24.35, 2.0, -22.35, -1.09))
        self.assertIn("realized P&L  -$24.35  (1 closed: 0 W / 1 L)", card)
        self.assertEqual((st["date"], st["realized_pnl"], st["loss_streak"], st["loss_budget"]["used"]),
                         (CDAY, -24.35, 1, 24.35))

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
