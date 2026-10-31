"""Tests for skills/alerts/scripts/alerts.py (price alerts)."""
import contextlib
import io
import json
import os
import re
import shlex
import sys
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (SKILLS, TempHomeTestCase, et_ts, fake_bars, fake_http, load_skill_module,  # noqa: E402
                     run_cli, skill_script, tc, utc_ts, write_binance_fixtures, write_coinbase_fixtures,
                     write_fixture, yahoo_chart_url, yahoo_payload)

A = load_skill_module("alerts", "alerts")
SCRIPT = skill_script("alerts", "alerts.py")
DAY, PREV = "2026-10-30", "2026-10-29"   # Friday, Thursday


def mk(day, rows, start="09:30", volume=100_000):
    """Bars from (open, high, low, close[, volume]) rows on the 5-minute grid."""
    t0 = et_ts(day, start)
    return [tc.Bar(t0 + i * 300, r[0], r[1], r[2], r[3], r[4] if len(r) > 4 else volume)
            for i, r in enumerate(rows)]


# Hand-checkable session. Bar times: 09:30 09:35 09:40 09:45 09:50 09:55 10:00
ROWS = [
    (100.0, 100.4, 99.8, 100.2),
    (100.2, 100.5, 100.0, 100.3),
    (100.3, 100.6, 100.1, 100.4),
    (100.4, 101.2, 100.3, 100.6),   # 09:45 spike to 101.2, closes 100.6
    (100.6, 100.8, 100.2, 100.3),
    (100.3, 100.4, 99.5, 99.7),     # 09:55 dips to 99.5
    (99.7, 101.5, 99.6, 101.3),     # 10:00 runs to 101.5
]
BARS = mk(DAY, ROWS)


def at(hhmm, day=DAY):
    return tc.parse_et(f"{day}T{hhmm}")


def new_alert(kind, bars, when, existing=(), **kw):
    now = at(when) if isinstance(when, str) else when
    ctx = A.market_context(bars, None, now)
    return A.make_alert("TEST", kind, ctx, now, list(existing), **kw)


def run(alert, bars, when, quote=None):
    return A.evaluate(alert, bars, quote, at(when) if isinstance(when, str) else when)


def times(res):
    return [e["bar_time"] for e in res["events"]]


class LevelAlertTests(unittest.TestCase):
    def test_touch_between_checks_fires_from_the_bar_high(self):
        a = new_alert("above", BARS, "09:42", level=101.0)
        self.assertTrue(a["legs"][0]["armed"])
        self.assertEqual(a["created_price"], 100.4)
        r = run(a, BARS, "09:52")                      # 09:45 bar poked 101.2, price back at 100.3
        self.assertTrue(r["fired"])
        e = r["events"][0]
        self.assertEqual((e["bar_time"], e["how"], e["price"], e["level"], e["leg"]), ("09:45", "touch", 101.0, 101.0, "up"))
        self.assertTrue(e["bar_complete"])
        self.assertEqual(r["last_price"], 100.3)
        self.assertEqual(r["alert"]["fire_count"], 1)

    def test_rearms_only_after_a_close_back_across_by_the_buffer(self):
        a = new_alert("above", BARS, "09:42", level=101.0)  # default buffer 0.25% -> close < 100.7475
        self.assertEqual(times(run(a, BARS, "10:06")), ["09:45", "10:00"])  # 09:45 closed 100.6: re-armed
        wide = new_alert("above", BARS, "09:42", level=101.0, rearm_pct=1.0)  # close < 99.99: the 09:55 bar
        self.assertEqual(times(run(wide, BARS, "10:06")), ["09:45", "10:00"])
        wider = new_alert("above", BARS, "09:42", level=101.0, rearm_pct=2.0)  # close < 98.98: never
        r = run(wider, BARS, "10:06")
        self.assertEqual(times(r), ["09:45"])
        self.assertEqual(A.status_text(r["alert"]), "waiting")
        self.assertIn("re-arms on a 5m close below 98.98", A.waiting_reason(r["alert"]))

    def test_below_mirror(self):
        a = new_alert("below", BARS, "09:42", level=99.6)
        r = run(a, BARS, "10:06")
        self.assertEqual(times(r), ["09:55"])
        self.assertEqual((r["events"][0]["leg"], r["events"][0]["price"]), ("down", 99.6))

    def test_exact_level_touch_survives_yahoo_float_noise(self):
        bars = mk(DAY, [(369.5, 369.9, 369.4, 369.8), (369.8, 370.0399780273438, 369.7, 369.9)])
        a = new_alert("above", bars[:1], "09:35", level=370.04)
        self.assertTrue(run(a, bars, "09:45")["fired"])

    def test_high_equal_below_level_does_not_fire(self):
        a = new_alert("above", BARS, "09:42", level=101.6)
        self.assertFalse(run(a, BARS, "10:06")["fired"])

    def test_once_switches_off_and_stays_off(self):
        a = new_alert("below", BARS, "09:42", level=99.6, once=True)
        r = run(a, BARS, "10:06")
        self.assertEqual(r["alert"]["status"], "fired")
        self.assertTrue(r["events"][0]["deactivated"])
        later = run(r["alert"], BARS + mk(DAY, [(99.0, 99.1, 98.0, 98.5)], start="10:05"), "10:20")
        self.assertFalse(later["fired"])
        self.assertEqual(A.status_text(later["alert"]), "fired")

    def test_forming_bar_fires_once_then_completes_without_double_fire(self):
        a = new_alert("above", BARS, "09:42", level=101.0)
        r1 = run(a, BARS, "09:47")                     # 09:45 bar still forming
        self.assertEqual(times(r1), ["09:45"])
        self.assertFalse(r1["events"][0]["bar_complete"])
        self.assertFalse(r1["alert"]["legs"][0]["armed"])
        r2 = run(r1["alert"], BARS, "09:52")           # same bar, now complete: re-arms on its close
        self.assertEqual(r2["events"], [])
        self.assertTrue(r2["alert"]["legs"][0]["armed"])
        r3 = run(r2["alert"], BARS, "10:06")
        self.assertEqual(times(r3), ["10:00"])
        self.assertEqual(r3["alert"]["fire_count"], 2)

    def test_prices_from_before_the_alert_do_not_count(self):
        # Created at 09:47 inside the 09:45 bar, after its 101.2 spike: the snapshot remembers it.
        a = new_alert("above", BARS, "09:47", level=101.0)
        self.assertEqual(a["snapshot"], {"ts": et_ts(DAY, "09:45"), "high": 101.2, "low": 100.3})
        self.assertEqual(times(run(a, BARS, "09:58")), [])
        # The same bar later makes a NEW high above the snapshot: that part is post-alert.
        early = [*BARS[:3], tc.Bar(et_ts(DAY, "09:45"), 100.4, 100.7, 100.3, 100.6, 1)]
        b = new_alert("above", early, "09:47", level=101.0)
        self.assertEqual(b["snapshot"]["high"], 100.7)
        self.assertEqual(times(run(b, BARS, "09:58")), ["09:45"])
        # Without a snapshot only the creation bar's close (after creation) counts.
        c = dict(b, snapshot=None)
        self.assertEqual(times(run(c, BARS, "09:58")), [])

    def test_bar_starting_at_creation_counts_in_full(self):
        a = new_alert("above", BARS, "09:45", level=101.0)
        self.assertEqual(a["cursor"], et_ts(DAY, "09:45"))
        self.assertEqual(times(run(a, BARS, "09:52")), ["09:45"])

    def test_already_beyond_waits_for_a_move_back(self):
        a = new_alert("above", BARS, "09:42", level=100.0)   # price 100.4 is already above
        leg = a["legs"][0]
        self.assertFalse(leg["armed"])
        self.assertTrue(leg["beyond_at_creation"])
        self.assertEqual(A.status_text(a), "waiting")
        self.assertIn("was already at/above 100.00", A.waiting_reason(a))
        r = run(a, BARS, "10:06")  # 09:55 closes 99.7 < 99.75 -> re-armed; 10:00 trades 101.5
        self.assertEqual(times(r), ["10:00"])

    def test_gap_through_the_level_overnight(self):
        prev = mk(PREV, [(100.0, 100.2, 99.8, 100.0)] * 3, start="15:45")
        a = new_alert("above", prev, at("16:30", PREV), level=101.0)
        self.assertIsNone(a["snapshot"])                       # market closed: nothing to snapshot
        today = mk(DAY, [(101.5, 101.8, 101.4, 101.6)])
        r = run(a, prev + today, "09:40")
        e = r["events"][0]
        self.assertEqual((e["how"], e["price"], e["date"]), ("gap", 101.5, DAY))
        self.assertIn("gapped above 101.00 - opened 101.50 at 09:30 ET", A.event_line(e, at("09:40")))

    def test_quote_extends_the_forming_bar(self):
        a = new_alert("above", BARS, "09:42", level=101.0, rearm_pct=2.0)
        r = run(a, BARS[:5], "09:54")
        self.assertEqual(times(r), ["09:45"])
        a2 = r["alert"]
        a2["legs"][0]["armed"] = True                          # pretend it re-armed
        a2["legs"][0]["last_fire_bar"] = None
        quote = {"price": 101.05, "time": et_ts(DAY, "09:53")}
        r2 = run(a2, BARS[:5], "09:54", quote)
        self.assertEqual(times(r2), ["09:50"])
        self.assertEqual(r2["last_price"], 101.05)
        self.assertEqual(run(a2, BARS[:5], "09:54", {"price": 101.05, "time": None})["events"], [])
        self.assertEqual(times(run(a2, BARS[:5], "09:54", 101.05)), ["09:50"])  # float = observed now
        # A quote stamped in the future (clock skew) or outside the session is ignored.
        self.assertEqual(run(a2, BARS[:5], "09:54", {"price": 101.05, "time": et_ts(DAY, "09:58")})["events"], [])

    def test_bars_after_now_are_ignored(self):
        a = new_alert("above", BARS[:3], "09:42", level=101.4)
        self.assertFalse(run(a, BARS, "09:58")["fired"])        # the 10:00 bar is in the future
        self.assertTrue(run(a, BARS, "10:01")["fired"])

    def test_evaluate_is_pure_and_skips_processed_bars(self):
        a = new_alert("above", BARS, "09:42", level=101.0)
        before = json.dumps(a, sort_keys=True)
        r = run(a, BARS, "10:06")
        self.assertEqual(json.dumps(a, sort_keys=True), before)
        self.assertEqual(r["alert"]["cursor"], et_ts(DAY, "10:05"))
        self.assertEqual(run(r["alert"], BARS, "10:06")["events"], [])  # nothing new: no repeat
        self.assertEqual(run(a, [], "10:06")["events"], [])           # no data: no fire, no crash

    def test_create_needs_a_price(self):
        with self.assertRaises(ValueError):
            A.market_context([], None, at("10:00"))
        ctx = A.market_context([], {"price": 50.0, "time": et_ts(DAY, "09:59")}, at("10:00"))
        self.assertEqual(ctx["last"], 50.0)


class PctMoveTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(A.parse_pct_move("3"), (3.0, "both"))
        self.assertEqual(A.parse_pct_move("+2.5"), (2.5, "up"))
        self.assertEqual(A.parse_pct_move("-1%"), (1.0, "down"))
        for bad in ("", "abc", "0", "-0", "60", "1-2"):
            with self.assertRaises(ValueError):
                A.parse_pct_move(bad)

    def test_band_from_the_price_when_added(self):
        a = new_alert("pct_move", BARS, "09:42", pct=1.0)
        self.assertEqual([(leg["dir"], leg["level"]) for leg in a["legs"]], [("up", 101.4), ("down", 99.4)])
        self.assertEqual(A.condition_text(a), "±1% from 100.40")
        r = run(a, BARS, "10:06")
        self.assertEqual([(e["bar_time"], e["leg"]) for e in r["events"]], [("10:00", "up")])
        self.assertEqual(A.status_text(r["alert"]), "armed down only")
        down = new_alert("pct_move", BARS, "09:42", pct=1.0, direction="down")
        self.assertEqual([leg["dir"] for leg in down["legs"]], ["down"])
        self.assertFalse(run(down, BARS, "10:06")["fired"])

    def test_manual_reference(self):
        a = new_alert("pct_move", BARS, "09:42", pct=1.0, direction="up", ref=100.0, ref_basis="prev-close")
        self.assertEqual(a["legs"][0]["level"], 101.0)
        self.assertEqual(A.condition_text(a), "+1% from 100.00 (prev close)")

    def test_both_sides_in_one_bar_with_once(self):
        bars = mk(DAY, [(100, 100.1, 99.9, 100), (100, 103, 97, 100)])
        a = new_alert("pct_move", bars[:1], "09:35", pct=2.0, once=True)
        r = run(a, bars, "09:45")
        self.assertEqual(sorted(e["leg"] for e in r["events"]), ["down", "up"])
        self.assertEqual(r["alert"]["status"], "fired")


# Flat bars (open = high = low = close) with equal volume: VWAP is the running mean of closes.
VWAP_PRICES = [100, 101, 99, 98, 103, 104]   # VWAP 100, 100.5, 100, 99.5, 100.2, 100.83
VWAP_BARS = mk(DAY, [(p, p, p, p) for p in VWAP_PRICES])
PRE = mk(PREV, [(100, 100, 100, 100)], start="15:55")   # creation data for alerts set before the open


class VwapTests(unittest.TestCase):
    def test_close_based_crosses_hand_checked(self):
        a = new_alert("vwap_cross", PRE, "09:00", vwap_direction="any")
        self.assertIsNone(a["vwap_side"])              # before the session: side set by bars
        r = run(a, VWAP_BARS, "10:00")
        self.assertEqual([(e["bar_time"], e["leg"], e["vwap"]) for e in r["events"]],
                         [("09:40", "below", 100.0), ("09:50", "above", 100.2)])
        self.assertIn("closed below VWAP 100.00 at 09:45 ET (5m close 99.00)", A.event_line(r["events"][0], at("10:00")))

    def test_direction_filter(self):
        up = new_alert("vwap_cross", PRE, "09:00", vwap_direction="above")
        self.assertEqual(times(run(up, VWAP_BARS, "10:00")), ["09:50"])
        down = new_alert("vwap_cross", PRE, "09:00", vwap_direction="below")
        self.assertEqual(times(run(down, VWAP_BARS, "10:00")), ["09:40"])

    def test_forming_bar_is_not_used(self):
        a = new_alert("vwap_cross", PRE, "09:00")
        self.assertEqual(times(run(a, VWAP_BARS, "09:52")), ["09:40"])   # the 09:50 bar is still forming

    def test_side_at_creation_and_no_cross_before_it(self):
        a = new_alert("vwap_cross", VWAP_BARS, "09:47")   # bars to 09:45 (98 vs VWAP 99.5): below
        self.assertEqual((a["vwap_side"], a["vwap_day"], a["last_vwap"]), (-1, DAY, 99.5))
        self.assertEqual(times(run(a, VWAP_BARS, "10:00")), ["09:50"])
        self.assertEqual(A.status_text(a), "armed")
        down = new_alert("vwap_cross", VWAP_BARS, "09:47", vwap_direction="below")
        self.assertEqual(A.status_text(down), "waiting")   # already below: must cross back up first

    def test_new_session_resets_the_side(self):
        prev = mk(PREV, [(p, p, p, p) for p in (100, 99, 98)], start="15:45")  # ends below its VWAP
        today = mk(DAY, [(p, p, p, p) for p in (105, 106, 107)])           # gaps up, stays above
        a = new_alert("vwap_cross", prev, at("15:52", PREV))
        self.assertEqual(a["vwap_side"], -1)
        r = run(a, prev + today, "09:50")
        self.assertEqual(r["events"], [])               # a gap is not a VWAP cross
        self.assertEqual((r["alert"]["vwap_side"], r["alert"]["vwap_day"]), (1, DAY))

    def test_once(self):
        a = new_alert("vwap_cross", PRE, "09:00", once=True)
        r = run(a, VWAP_BARS, "10:00")
        self.assertEqual(times(r), ["09:40"])
        self.assertEqual(r["alert"]["status"], "fired")


class PlanSpecTests(unittest.TestCase):
    LONG = {"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04}
    SHORT = {"symbol": "TSLA", "side": "short", "entry": 100.0, "stop": 102.0, "target": 96.0}

    def test_long_is_direction_aware(self):
        specs, warns = A.plan_alert_specs(self.LONG, 362.0)
        self.assertEqual(specs, [("entry", "above", 364.18), ("stop", "below", 358.32), ("target", "above", 370.04)])
        self.assertEqual(warns, [])
        specs, warns = A.plan_alert_specs(self.LONG, 365.0)       # already past the entry
        self.assertEqual(specs[0], ("entry", "below", 364.18))
        self.assertTrue(any("pullback" in w for w in warns))
        _, warns = A.plan_alert_specs(self.LONG, 357.0)
        self.assertTrue(any("through the plan's stop" in w for w in warns))

    def test_short_mirror_and_missing_target(self):
        specs, _ = A.plan_alert_specs(self.SHORT, 101.0)
        self.assertEqual(specs, [("entry", "below", 100.0), ("stop", "above", 102.0), ("target", "below", 96.0)])
        specs, warns = A.plan_alert_specs(dict(self.SHORT, target=None), 101.0, ["stop", "target"])
        self.assertEqual(specs, [("stop", "above", 102.0)])
        self.assertTrue(any("no target" in w for w in warns))

    def test_invalid_plan(self):
        for bad in ({"side": "long", "entry": 100, "stop": 101}, {"side": "long", "entry": "x", "stop": 1},
                    {"side": "sideways", "entry": 100, "stop": 99}, {"side": "long"}):
            with self.assertRaises(ValueError):
                A.plan_alert_specs(bad, 100.0)

    def test_plan_drift(self):
        a = {"id": "ALR-1", "symbol": "TSLA", "source": "plan", "plan_role": "stop", "status": "active", "level": 358.32}
        self.assertIsNone(A.plan_drift(a, self.LONG))
        self.assertIn("plan stop is now 359.00", A.plan_drift(a, dict(self.LONG, stop=359.0)))
        self.assertIn("plan is gone", A.plan_drift(a, None))


class StateAndCommandTests(TempHomeTestCase):
    NOW = f"{DAY}T09:42"

    def cli(self, *argv, now=None):
        if now:
            os.environ["DAYTRADER_NOW"] = f"{DAY}T{now}"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = A.main(list(argv))
        return code, out.getvalue()

    def jcli(self, *argv, now=None):
        code, out = self.cli(*argv, "--json", now=now)
        self.assertEqual(code, 0)
        return json.loads(out)

    def test_add_check_list_remove_roundtrip(self):
        with fake_bars({("TSLA", "5m"): BARS, ("NVDA", "5m"): VWAP_BARS}):
            r = self.jcli("add", "TSLA", "--above", "101", "--note", "breakout")
            self.assertEqual(r["added"][0]["id"], "ALR-20261030-001")
            self.assertEqual(self.jcli("add", "TSLA", "--above", "101")["duplicates"][0]["id"], "ALR-20261030-001")
            self.jcli("add", "TSLA", "--below", "99.6", "--once")
            self.jcli("add", "NVDA", "--vwap-cross")
            rep = self.jcli("check", now="10:06")
        self.assertEqual(rep["checked"], 3)
        self.assertEqual([(e["id"], e["bar_time"]) for e in rep["fired"]],
                         [("ALR-20261030-001", "09:45"), ("ALR-20261030-003", "09:50"),
                          ("ALR-20261030-002", "09:55"), ("ALR-20261030-001", "10:00")])
        self.assertEqual(rep["active"], 2)
        for k in ("as_of", "market_open", "symbols", "errors", "notes", "alerts", "disclaimer"):
            self.assertIn(k, rep)
        self.assertEqual(rep["symbols"]["TSLA"]["range"], "5d")
        state = json.loads((self.home / "alerts.json").read_text())
        self.assertEqual([a["status"] for a in state["alerts"]], ["active", "fired", "active"])
        lst = self.jcli("list")
        self.assertEqual((lst["count"], lst["active"]), (3, 2))
        self.assertEqual(self.jcli("list", "--active")["count"], 2)
        code, text = self.cli("list")
        self.assertIn("ALERTS · 2 active, 1 fired (one-shot)", text)
        self.assertEqual(self.jcli("remove", "--fired")["removed"], ["ALR-20261030-002"])
        self.assertEqual(self.jcli("remove", "alr-20261030-003")["remaining"], 1)
        with self.assertRaises(ValueError):
            self.cli("remove", "ALR-404")
        with self.assertRaises(ValueError):
            self.cli("remove", "--all", "--symbol", "TSLA")
        self.assertEqual(self.jcli("remove", "--all")["remaining"], 0)
        code, text = self.cli("check")
        self.assertIn("No active alerts", text)

    def test_ids_are_never_reused(self):
        with fake_bars({"TSLA": BARS}):
            self.jcli("add", "TSLA", "--above", "101")
            self.jcli("add", "TSLA", "--above", "102")
            self.jcli("remove", "ALR-20261030-002")
            self.assertEqual(self.jcli("add", "TSLA", "--above", "103")["added"][0]["id"], "ALR-20261030-003")

    def test_store_keeps_concurrent_changes(self):
        with fake_bars({"TSLA": BARS}):
            self.jcli("add", "TSLA", "--above", "101")
            self.jcli("add", "TSLA", "--below", "99.6")
            alerts = A.load_alerts()
            ev = {a["id"]: A.evaluate(a, BARS, None, at("10:06"))["alert"] for a in alerts}
            self.jcli("remove", "ALR-20261030-002")        # removed by another process meanwhile
            self.jcli("add", "TSLA", "--above", "105")     # added meanwhile
            A.store_updates(ev)
        state = A.load_alerts()
        self.assertEqual([a["id"] for a in state], ["ALR-20261030-001", "ALR-20261030-003"])
        self.assertEqual(state[0]["fire_count"], 2)
        self.assertEqual(state[1]["fire_count"], 0)
        # An update for a different alert that happens to carry the same id is not applied.
        A.store_updates({state[1]["id"]: dict(state[1], created_ts=1, fire_count=99)})
        self.assertEqual(A.load_alerts()[1]["fire_count"], 0)

    def test_add_warnings_and_validation(self):
        with fake_bars({"TSLA": BARS}):
            r = self.jcli("add", "TSLA", "--above", "100")
            self.assertTrue(any("already at/above" in w for w in r["warnings"]))
            r = self.jcli("add", "TSLA", "--below", "10")
            self.assertTrue(any("typo" in w for w in r["warnings"]))
            with self.assertRaises(ValueError):                    # BARS has no previous session
                self.cli("add", "TSLA", "--pct-move", "-1", "--ref", "prev-close")
        for argv in (["add", "TSLA"], ["add", "TSLA", "--above", "1", "--below", "2"],
                     ["add", "TSLA", "--above", "0"], ["add", "TSLA", "--above", "101", "--ref", "100"],
                     ["add", "TSLA", "--pct-move", "x"], ["add", "TSLA", "--vwap-cross", "up"],
                     ["add", "TSLA", "--above", "101", "--rearm-pct", "-1"], ["add", "$$", "--above", "1"],
                     ["watch", "--interval", "1"], ["from-plan", "TSLA", "--levels", "nope"]):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = A.main(argv)
                except ValueError:
                    code = 1
            self.assertEqual(code, 1, argv)

    def test_prev_close_reference(self):
        prev = mk(PREV, [(100.0, 100.2, 99.8, 100.0)] * 3, start="15:45")
        with fake_bars({"TSLA": prev + BARS}):
            r = self.jcli("add", "TSLA", "--pct-move", "-1", "--ref", "prev-close")
            a = r["added"][0]
            self.assertEqual((a["ref_basis"], a["ref_price"], a["legs"][0]["level"]), ("prev-close", 100.0, 99.0))
            r = self.jcli("add", "TSLA", "--pct-move", "2", "--ref", "50")
            self.assertEqual(r["added"][0]["ref_basis"], "manual")
            self.assertEqual([leg["armed"] for leg in r["added"][0]["legs"]], [False, True])  # 100.4 > 51
            self.assertTrue(any("outside the band" in w for w in r["warnings"]))

    def test_from_plan_adds_and_replaces(self):
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 101.0, "stop": 99.6, "target": 102.0})
        with fake_bars({"TSLA": BARS}):
            r = self.jcli("from-plan", "TSLA")
            self.assertEqual([(a["plan_role"], a["kind"], a["level"], a["once"]) for a in r["added"]],
                             [("entry", "above", 101.0, True), ("stop", "below", 99.6, True),
                              ("target", "above", 102.0, True)])
            r = self.jcli("from-plan", "TSLA", "--levels", "stop", "--repeat")
            self.assertEqual(r["replaced"], ["ALR-20261030-002"])
            self.assertFalse(r["added"][0]["once"])
            self.assertEqual(len(A.load_alerts()), 3)
            tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 101.0, "stop": 99.0, "target": None})
            rep = self.jcli("check", now="10:06")
        self.assertEqual([(e["plan_role"], e["bar_time"]) for e in rep["fired"]], [("entry", "09:45"), ("stop", "09:55")])
        self.assertTrue(any("plan stop is now 99.00" in n for n in rep["notes"]))
        self.assertTrue(any("plan target is now -" in n for n in rep["notes"]))
        with self.assertRaises(ValueError):
            self.cli("from-plan", "NVDA")              # no plan
        (self.home / "plans" / "TSLA.json").write_text("{broken")
        with fake_bars({"TSLA": BARS}):
            notes = self.jcli("check", now="10:20")["notes"]
        self.assertTrue(any("could not read the saved plan" in n for n in notes), notes)
        self.assertFalse(any("plan is gone" in n for n in notes), notes)

    def test_check_partial_and_total_data_failure(self):
        with fake_bars({"TSLA": BARS, "NVDA": VWAP_BARS}):
            self.jcli("add", "TSLA", "--above", "101")
            self.jcli("add", "NVDA", "--vwap-cross")
        with fake_bars({"TSLA": BARS}):                  # NVDA now fails
            rep = self.jcli("check", now="10:06")
        self.assertIn("NVDA", rep["errors"])
        self.assertEqual(len(rep["fired"]), 2)
        with fake_bars({}):
            with self.assertRaises(tc.FetchError):
                self.cli("check", now="10:10")
        with fake_bars({"TSLA": BARS}):
            rep = self.jcli("check", "--symbol", "TSLA", now="10:10")
        self.assertEqual(rep["checked"], 1)

    def test_stale_data_note_only_once_the_session_is_underway(self):
        prev = mk(PREV, [(100.0, 100.2, 99.8, 100.0)] * 3, start="15:45")
        with fake_bars({"TSLA": prev + BARS}):
            self.jcli("add", "TSLA", "--above", "150")
            self.assertEqual(self.jcli("check", now="10:12")["notes"], [])
            notes = self.jcli("check", now="10:40")["notes"]
        self.assertTrue(any("newest bar is 2026-10-30 10:00 ET" in n for n in notes), notes)
        with fake_bars({"TSLA": prev}):
            self.assertEqual(self.jcli("check", now="09:33")["notes"], [])   # first bar not out yet

    def test_unknown_symbol_is_bad_input(self):
        with mock.patch.object(tc, "fetch_bars", side_effect=tc.FetchError("ZZZZ: HTTP 404 for https://x - not found")):
            with self.assertRaises(ValueError):
                self.cli("add", "ZZZZ", "--above", "5")

    def test_range_grows_with_alert_age(self):
        now = int(at("10:00").timestamp())
        self.assertEqual(A.range_for(now - 3 * 86400, now), "5d")
        self.assertEqual(A.range_for(now - 20 * 86400, now), "1mo")
        self.assertEqual(A.range_for(now - 40 * 86400, now), "60d")

    def test_corrupt_state(self):
        (self.home / "alerts.json").write_text('{"alerts": 5}')
        with self.assertRaises(tc.StateError):
            A.load_alerts()


class Clock:
    """Fake sleep/monotonic that also moves $DAYTRADER_NOW forward."""

    def __init__(self, start):
        self.t = 0.0
        self.now = tc.parse_et(start)
        self.sleeps = 0

    def sleep(self, s):
        self.sleeps += 1
        self.t += s
        self.now += timedelta(seconds=s)
        os.environ["DAYTRADER_NOW"] = self.now.strftime("%Y-%m-%dT%H:%M:%S")

    def mono(self):
        return self.t


class WatchTests(TempHomeTestCase):
    NOW = f"{DAY}T09:42"

    def watch(self, clock, **kw):
        os.environ["DAYTRADER_NOW"] = clock.now.strftime("%Y-%m-%dT%H:%M:%S")
        out = io.StringIO()
        args = dict(symbols=None, interval=60, max_minutes=60, as_json=False)
        args.update(kw)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = A.watch(sleep=clock.sleep, clock=clock.mono, **args)
        return code, out.getvalue()

    def add(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            A.main(["add", "TSLA", *argv])

    def test_stops_when_the_last_one_shot_fires(self):
        with fake_bars({"TSLA": BARS}):
            self.add("--below", "99.6", "--once")
            code, out = self.watch(Clock(f"{DAY}T09:48"), bell=True)
        self.assertEqual(code, 0)
        self.assertIn("\aFIRED  ALR-20261030-001  TSLA  below 99.60", out)
        self.assertIn("no active alerts left: ", out)
        self.assertIn("1 alert fired", out)

    def test_stops_after_max_minutes_and_at_the_close(self):
        with fake_bars({"TSLA": BARS}):
            self.add("--above", "150")
            c = Clock(f"{DAY}T10:00")
            code, out = self.watch(c, max_minutes=5, quiet=True)
            self.assertEqual((code, c.sleeps), (0, 5))
            self.assertIn("after 5 min: 6 checks, 0 alerts fired", out)
            c = Clock(f"{DAY}T15:58")
            code, out = self.watch(c, as_json=True)
        lines = [json.loads(x) for x in out.splitlines()]
        self.assertEqual(lines[-1]["stopped"], "market closed")
        self.assertEqual(c.sleeps, 2)               # 15:58, 15:59, 16:00 -> stop

    def test_closed_market_runs_one_check(self):
        with fake_bars({"TSLA": BARS}):
            self.add("--above", "150")
            c = Clock("2026-10-31T11:00")           # Saturday
            code, out = self.watch(c)
        self.assertEqual(c.sleeps, 0)
        self.assertIn("market closed", out)

    def test_data_down_from_the_start_and_ctrl_c(self):
        with fake_bars({"TSLA": BARS}):
            self.add("--above", "150")
        with fake_bars({}):
            with self.assertRaises(tc.FetchError):
                self.watch(Clock(f"{DAY}T10:00"))
        c = Clock(f"{DAY}T10:00")
        c.sleep = mock.Mock(side_effect=KeyboardInterrupt)
        with fake_bars({"TSLA": BARS}):
            code, out = self.watch(c)
        self.assertEqual(code, 130)
        self.assertIn("(Ctrl-C)", out)

    def test_nothing_to_watch(self):
        with self.assertRaises(ValueError):
            self.watch(Clock(f"{DAY}T10:00"))


class CliTests(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        self.fx = self.home / "fx"
        prev = mk(PREV, [(100.0, 100.2, 99.8, 100.0)] * 78)
        payload = yahoo_payload(prev + BARS, "TSLA")
        for rng in ("5d", "1mo", "60d"):
            write_fixture(self.fx, yahoo_chart_url("TSLA", "5m", rng), payload)
        self.env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(self.fx),
                    "DAYTRADER_NOW": f"{DAY}T09:42"}

    def test_add_check_list_text_and_json(self):
        p = run_cli(SCRIPT, "add", "TSLA", "--above", "101", "--note", "breakout", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Added ALR-20261030-001 · TSLA above 101.00 · armed", p.stdout)
        self.assertIn("fires at >= 101.00 (+0.60% away)", p.stdout)
        p = run_cli(SCRIPT, "check", env=dict(self.env, DAYTRADER_NOW=f"{DAY}T10:06"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("ALERTS CHECK · 10:06 ET · 1 active alert on 1 symbol"))
        self.assertIn("traded above 101.00 09:45-09:50 ET (high 101.20)", p.stdout)
        self.assertIn("traded above 101.00 10:00-10:05 ET (high 101.50)", p.stdout)
        self.assertIn('"breakout"', p.stdout)
        p = run_cli(SCRIPT, "list", "--json", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = json.loads(p.stdout)["alerts"]
        self.assertEqual((rows[0]["fire_count"], rows[0]["status"]), (2, "waiting"))

    def test_from_plan_text(self):
        with mock.patch.dict(os.environ, {"DAYTRADER_HOME": str(self.home)}):
            tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 101.0, "stop": 99.6, "target": 102.0})
        p = run_cli(SCRIPT, "from-plan", "TSLA", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("TSLA LONG plan · entry 101.00 / stop 99.60 / target 102.00 · 3 one-shot alerts", p.stdout)
        self.assertIn("Added ALR-20261030-002 (stop) · TSLA below 99.60 · once · armed", p.stdout)

    def test_missing_data_is_a_one_line_data_error(self):
        empty = self.home / "empty"
        empty.mkdir()
        for argv in (["add", "TSLA", "--above", "101"], ["check"]):
            if argv == ["check"]:
                run_cli(SCRIPT, "add", "TSLA", "--above", "101", env=self.env)
            p = run_cli(SCRIPT, *argv, env=dict(self.env, DAYTRADER_FIXTURES=str(empty)))
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
            self.assertTrue(p.stderr.startswith("data error: "), p.stderr)
            self.assertIn("query", p.stderr)          # names the Yahoo host
            self.assertNotIn("Traceback", p.stderr)

    def test_watch_on_a_closed_market_runs_one_check(self):
        p = run_cli(SCRIPT, "add", "TSLA", "--above", "150", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = run_cli(SCRIPT, "watch", "--json", env=dict(self.env, DAYTRADER_NOW="2026-10-31T11:00"))
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = [json.loads(x) for x in p.stdout.splitlines()]
        self.assertEqual((len(lines), lines[-1]["final"], lines[-1]["stopped"]), (2, True, "market closed"))

    def test_bad_input_exits_1(self):
        for argv in (["add", "TSLA"], ["add", "TSLA", "--above", "-3"], ["remove"], ["bogus"],
                     ["add", "TSLA", "--above", "101", "--live"], ["from-plan", "TSLA"]):
            p = run_cli(SCRIPT, *argv, env=self.env)
            self.assertEqual(p.returncode, 1, (argv, p.stderr))
            self.assertNotIn("Traceback", p.stderr)


class ReviewRegressionTests(unittest.TestCase):
    """Defects found in review; each test failed before its fix."""

    def test_prev_close_after_the_bell_is_todays_close(self):
        # Set in the evening for tomorrow, "prev close" is today's close, not yesterday's.
        prev = mk(PREV, [(100.0, 100.2, 99.8, 100.0)] * 3, start="15:45")
        today = mk(DAY, [(105.0, 105.2, 104.8, 105.0)] * 3, start="15:45")
        self.assertEqual(A.market_context(prev + today, None, at("17:00"))["prev_close"], 105.0)
        self.assertEqual(A.market_context(prev + today, None, at("16:00"))["prev_close"], 105.0)
        self.assertEqual(A.market_context(prev + today, None, at("15:52"))["prev_close"], 100.0)
        self.assertEqual(A.market_context(prev, None, at("09:00"))["prev_close"], 100.0)

    def test_quote_ahead_of_lagging_bars_does_not_skip_them(self):
        # Yahoo's bars stop at 10:10 while its last price is from 10:24. The quote must not
        # move the cursor past 10:15/10:20, or the real bars arriving next check are skipped.
        bars = mk(DAY, [(100.0, 100.2, 99.9, 100.1)] * 9)          # 09:30 .. 10:10
        a = new_alert("above", bars[:3], "09:42", level=101.0)
        r = run(a, bars, "10:26", {"price": 100.3, "time": et_ts(DAY, "10:24")})
        self.assertFalse(r["fired"])
        self.assertEqual(r["last_price"], 100.3)                     # still the freshest price
        late = bars + mk(DAY, [(100.1, 101.5, 100.0, 100.2), (100.2, 100.4, 100.0, 100.3)], start="10:15")
        self.assertEqual(times(run(r["alert"], late, "10:31")), ["10:15"])
        # A quote in the bar still forming still counts right away.
        r = run(a, bars, "10:17", {"price": 101.2, "time": et_ts(DAY, "10:16")})
        self.assertEqual(times(r), ["10:15"])

    def test_last_price_uses_a_quote_newer_than_the_bars(self):
        bars = mk(DAY, [(100.0, 100.2, 99.9, 100.1)] * 9)          # 09:30 .. 10:10
        ctx = A.market_context(bars, {"price": 100.9, "time": et_ts(DAY, "10:24")}, at("10:26"))
        self.assertEqual(ctx["last"], 100.9)

    def test_vwap_alert_added_after_the_close_does_not_claim_a_side(self):
        # Tomorrow's first bars set the side (a gap is not a cross), so do not say "fires on a
        # close below VWAP" from today's finished session.
        a = new_alert("vwap_cross", VWAP_BARS, "17:00")
        self.assertIsNone(a["vwap_side"])
        self.assertEqual(A.status_text(a), "waiting")
        self.assertIn("not set yet", A.waiting_reason(a))

    def test_vwap_does_not_fire_on_data_without_volume(self):
        # Indexes / FX on Yahoo report volume 0: "VWAP" would just be each bar's own typical
        # price and close-vs-typical-price flips would fire as fake crosses.
        bars = mk(DAY, [(100, 101, 99, 100.5, 0), (100.5, 101, 99.5, 99.6, 0),
                        (99.6, 100.8, 99.4, 100.7, 0), (100.7, 101, 99, 99.2, 0)])
        a = new_alert("vwap_cross", PRE, "09:00")
        r = run(a, bars, "10:00")
        self.assertEqual(r["events"], [])
        self.assertIsNone(r["alert"]["vwap_side"])
        ctx = A.market_context(bars, None, at("09:52"))
        self.assertIsNone(ctx["vwap"])
        self.assertFalse(ctx["has_volume"])
        # Volume that starts after a zero-volume first bar is fine.
        vb = [tc.Bar(b.ts, b.open, b.high, b.low, b.close, 0 if i == 0 else 1000)
              for i, b in enumerate(VWAP_BARS)]
        self.assertTrue(run(new_alert("vwap_cross", PRE, "09:00"), vb, "10:00")["fired"])


class CommandReviewTests(TempHomeTestCase):
    NOW = f"{DAY}T09:42"

    def test_vwap_alert_refused_without_volume_data(self):
        flat = [tc.Bar(b.ts, b.open, b.high, b.low, b.close, 0) for b in BARS]
        with fake_bars({"^VIX": flat}):
            with self.assertRaises(ValueError) as cm:
                A.main(["add", "^VIX", "--vwap-cross"])
        self.assertIn("volume", str(cm.exception))
        self.assertEqual(A.load_alerts(), [])

    def test_malformed_alert_in_state_is_a_state_error_not_a_traceback(self):
        (self.home / "alerts.json").write_text(json.dumps({"alerts": [{"id": "X", "symbol": "TSLA"}]}))
        with self.assertRaises(tc.StateError):
            A.main(["list"])
        with self.assertRaises(tc.StateError):
            A.main(["check"])

    def test_watch_text_shows_stale_data_and_symbol_errors(self):
        prev = mk(PREV, [(100.0, 100.2, 99.8, 100.0)] * 3, start="15:45")
        with fake_bars({"TSLA": prev + BARS, "NVDA": VWAP_BARS}):
            with contextlib.redirect_stdout(io.StringIO()):
                A.main(["add", "TSLA", "--above", "150"])
                A.main(["add", "NVDA", "--above", "150"])
        c = Clock(f"{DAY}T10:40")
        os.environ["DAYTRADER_NOW"] = c.now.strftime("%Y-%m-%dT%H:%M:%S")
        out = io.StringIO()
        with fake_bars({"TSLA": prev + BARS}):                       # NVDA now fails
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                A.watch(None, 60, 3, False, sleep=c.sleep, clock=c.mono)
        text = out.getvalue()
        self.assertIn("newest bar is 2026-10-30 10:00 ET", text)
        self.assertIn("NVDA: no fixture", text)
        self.assertEqual(text.count("newest bar is"), 1, text)     # said once, not every poll

    def test_watch_json_ends_with_a_final_line_when_data_fails_mid_run(self):
        with fake_bars({"TSLA": BARS}):
            with contextlib.redirect_stdout(io.StringIO()):
                A.main(["add", "TSLA", "--above", "150"])
        calls = []

        def flaky(symbol, interval="5m", range_="60d", prepost=False, ttl=None):
            calls.append(symbol)
            if len(calls) > 1:
                raise tc.FetchError("TSLA: HTTP 403 for https://query2.finance.yahoo.com/x")
            return list(BARS), {"regularMarketPrice": BARS[-1].close}

        c = Clock(f"{DAY}T10:00")
        os.environ["DAYTRADER_NOW"] = c.now.strftime("%Y-%m-%dT%H:%M:%S")
        out = io.StringIO()
        with mock.patch.object(tc, "fetch_bars", side_effect=flaky):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(tc.FetchError):               # still exit 2 via tc.run
                    A.watch(None, 60, 30, True, sleep=c.sleep, clock=c.mono)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)                              # one check, then the summary
        self.assertTrue(lines[-1]["final"])
        self.assertEqual((lines[-1]["stopped"], lines[-1]["polls"]), ("data error", 4))
        self.assertIn("query2.finance.yahoo.com", lines[-1]["error"])

    def test_store_does_not_roll_back_a_newer_concurrent_check(self):
        # `watch` in the background + `check` in the foreground: the save that lands last must not
        # rewind an alert's cursor, or the next check reports an old touch again.
        with fake_bars({"TSLA": BARS}):
            with contextlib.redirect_stdout(io.StringIO()):
                A.main(["add", "TSLA", "--above", "101"])
        orig = A.load_alerts()[0]
        newer = A.evaluate(orig, BARS, None, at("10:06"))["alert"]   # fired 09:45 and 10:00
        older = A.evaluate(orig, BARS, None, at("09:52"))["alert"]   # fired 09:45 only
        A.store_updates({orig["id"]: newer})
        A.store_updates({orig["id"]: older})
        st = A.load_alerts()[0]
        self.assertEqual((st["fire_count"], st["cursor"]), (2, et_ts(DAY, "10:05")))
        self.assertEqual(A.evaluate(st, BARS, None, at("10:07"))["events"], [])
        # A one-shot switched off by the other process stays off.
        once = dict(orig, once=True)
        A.save_alerts([once])
        off = A.evaluate(once, BARS, None, at("10:06"))["alert"]
        still_on = A.evaluate(once, BARS[:3], None, at("09:44"))["alert"]
        A.store_updates({orig["id"]: off})
        A.store_updates({orig["id"]: still_on})
        self.assertEqual(A.load_alerts()[0]["status"], "fired")

    def test_check_text_carries_the_disclaimer_when_something_fired(self):
        with fake_bars({"TSLA": BARS}):
            with contextlib.redirect_stdout(io.StringIO()):
                A.main(["add", "TSLA", "--above", "101"])
            os.environ["DAYTRADER_NOW"] = f"{DAY}T10:06"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                A.main(["check"])
        self.assertIn("FIRED", out.getvalue())
        self.assertIn(tc.DISCLAIMER, out.getvalue())


class SkillDocTests(unittest.TestCase):
    def test_every_documented_command_parses(self):
        text = (SKILLS / "alerts" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Scripts live in this skill's `scripts/` folder. Run them from the skill's folder "
                      "(`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.", text)
        cmds = re.findall(r"python3 scripts/alerts\.py ([^`\n]*)", text)
        self.assertGreater(len(cmds), 8)
        self.assertTrue(any(c.split()[:2] == ["add", "BTC-USDT"] for c in cmds))   # the primary crypto example
        self.assertIn("Binance by default (or", text)
        parser = A.build_parser()
        for c in cmds:
            argv = shlex.split(c.split("  #")[0].split(" # ")[0])
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    parser.parse_args(argv)
                except SystemExit as e:
                    self.fail(f"SKILL.md command does not parse: {c!r} ({e})")
        self.assertLess(len(text.splitlines()), 160)



# ---------------------------------------------------------------------------
# Crypto: Coinbase 5m candles, 24/7, UTC days; per-symbol market in a mixed check
# ---------------------------------------------------------------------------

CDAY = "2026-10-31"     # a Saturday


def crypto_bars(until="05:10", dip_at="04:50", dip=97_900.0, start=f"2026-10-30"):
    """Flat 100,000 BTC candles from 00:00 UTC on `start` up to `until` on CDAY; one dip to `dip`."""
    out, t, stop = [], utc_ts(start, "00:00"), utc_ts(CDAY, until)
    while t < stop:
        low = dip if dip_at and t == utc_ts(CDAY, dip_at) else 99_950.0
        out.append(tc.Bar(t, 100_000.0, 100_050.0, low, 100_000.0, 2.0))
        t += 300
    return out


class CryptoCoreTests(TempHomeTestCase):
    NOW = f"{CDAY}T04:40"

    def setUp(self):
        super().setUp()
        tc.set_market(tc.CRYPTO)

    def test_crypto_ranges_and_never_done(self):
        now = utc_ts(CDAY, "05:10")
        self.assertEqual(A.range_for(now - 600, now, tc.CRYPTO), "2d")
        self.assertEqual(A.range_for(now - int(3.5 * 86400), now, tc.CRYPTO), "5d")
        self.assertEqual(A.range_for(now - 90 * 86400, now, tc.CRYPTO), "60d")
        self.assertEqual(A.range_for(now - 3 * 86400, now, tc.US), "5d")              # stocks unchanged
        sat_night = tc.to_local(utc_ts(CDAY, "23:59"), tc.CRYPTO)
        self.assertFalse(A.market_done_for_day(sat_night, tc.CRYPTO))
        self.assertTrue(A.market_done_for_day(tc.to_local(utc_ts(CDAY, "16:00"), tc.US), tc.US))
        self.assertFalse(A.markets_done([tc.US, tc.CRYPTO], sat_night))
        self.assertTrue(A.markets_done([tc.US], sat_night))

    def test_alert_fires_on_a_saturday_and_times_print_in_utc(self):
        bars = crypto_bars()
        now = tc.now_et()
        ctx = A.market_context([b for b in bars if b.ts < now.timestamp()], None, now)
        self.assertTrue(ctx["market_open"])
        self.assertEqual(ctx["prev_close"], 100_000.0)                                 # Friday's UTC day
        a = A.make_alert("BTC-USD", "below", ctx, now, [], level=98_000.0, once=True)
        self.assertEqual((a["market"], a["created_at"]), ("crypto", f"{CDAY}T04:40:00+00:00"))
        later = tc.to_local(utc_ts(CDAY, "05:10"), tc.CRYPTO)
        r = A.evaluate(a, bars, None, later)
        self.assertEqual([(e["bar_time"], e["market"]) for e in r["events"]], [("04:50", "crypto")])
        tc.set_market(tc.US)                                    # rendering does not depend on the active market
        line = A.event_line(r["events"][0], later)
        self.assertIn("traded below 98,000.00 04:50-04:55 UTC (low 97,900.00)", line)
        self.assertIn("fired 2026-10-31 04:50 UTC", A.waiting_reason(r["alert"]))

    def test_unknown_pair_is_bad_input(self):
        with mock.patch.object(tc, "fetch_bars", side_effect=tc.FetchError("XYZ-USD: Coinbase returned no candles")):
            with self.assertRaisesRegex(ValueError, "Coinbase has no candles for this pair"):
                A.fetch("XYZ-USD", "2d")

    def test_alert_without_market_field_follows_its_symbol(self):
        self.assertIs(A.alert_market({"symbol": "ETH-USD"}), tc.CRYPTO)
        self.assertIs(A.alert_market({"symbol": "TSLA", "market": "crypto"}), tc.US)
        self.assertIs(A.alert_market({"market": "crypto"}), tc.CRYPTO)


class CryptoCommandTests(TempHomeTestCase):
    NOW = f"{DAY}T09:42"

    def cli(self, *argv, now):
        os.environ["DAYTRADER_NOW"] = now
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = A.main(list(argv))
        return code, out.getvalue()

    def test_mixed_check_runs_each_symbol_in_its_own_market(self):
        crypto = crypto_bars()
        with fake_bars({("TSLA", "5m"): BARS, ("BTC-USD", "5m"): [b for b in crypto if b.ts < utc_ts(CDAY, "04:40")]}):
            self.cli("add", "TSLA", "--above", "150", now=f"{DAY}T09:42")
            self.cli("add", "BTC-USD", "--below", "98000", now=f"{CDAY}T04:40")
        seen = []

        def fake(symbol, interval="5m", range_="5d", prepost=False, ttl=None):
            seen.append((symbol, tc.market().key, int(tc.now_et().timestamp()), range_))
            return (list(crypto) if symbol == "BTC-USD" else list(BARS)), {}
        with mock.patch.object(tc, "fetch_bars", side_effect=fake):
            code, out = self.cli("check", now=f"{CDAY}T01:10")              # naive: ET (mixed -> US clock)
        self.assertEqual(code, 0)
        instant = int(tc.parse_et(f"{CDAY}T01:10").timestamp())                # 05:10 UTC
        self.assertEqual(sorted(seen), [("BTC-USD", "crypto", instant, "2d"), ("TSLA", "us", instant, "5d")])
        self.assertTrue(out.startswith("ALERTS CHECK · 01:10 ET · 2 active alerts on 2 symbols · "
                                       "US market closed (crypto trades 24/7)"), out)
        self.assertIn("FIRED  ALR-20261031-001  BTC-USD  below 98,000.00", out)
        self.assertIn("04:50-04:55 UTC", out)
        self.assertEqual(os.environ["DAYTRADER_NOW"], f"{CDAY}T01:10")         # the pin is undone

    def test_crypto_watch_does_not_stop_for_a_closed_market(self):
        crypto = crypto_bars(dip_at=None)
        with fake_bars({("BTC-USD", "5m"): crypto}):
            self.cli("add", "BTC-USD", "--above", "150000", now=f"{CDAY}T04:40")
            tc.set_market(tc.CRYPTO)
            c = Clock(f"{CDAY}T05:00")
            c.now = tc.to_local(utc_ts(CDAY, "05:00"), tc.CRYPTO)             # a Saturday, in UTC
            os.environ["DAYTRADER_NOW"] = c.now.strftime("%Y-%m-%dT%H:%M:%S")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = A.watch(None, 60, 3, False, sleep=c.sleep, clock=c.mono)
        text = out.getvalue()
        self.assertEqual((code, c.sleeps), (0, 3))
        self.assertIn("05:00 UTC · 1 active · nothing new", text)
        self.assertIn("after 3 min: 4 checks", text)
        self.assertNotIn("market closed", text)


class CryptoCliTests(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        self.fx = self.home / "fx"
        bars = crypto_bars()
        for hhmm in ("04:40", "05:10"):                       # the live window's URL moves with "now"
            write_coinbase_fixtures(self.fx, "BTC-USD", bars, "5m", "2d", utc_ts(CDAY, hhmm))
        self.env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(self.fx),
                    "DAYTRADER_NOW": f"{CDAY}T04:40"}

    def test_add_check_list_on_a_saturday(self):
        p = run_cli(SCRIPT, "add", "BTC-USD", "--below", "98000", "--once", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Added ALR-20261031-001 · BTC-USD below 98,000.00 · once · armed", p.stdout)
        self.assertIn("fires at <= 98,000.00 (-2.00% away)", p.stdout)
        p = run_cli(SCRIPT, "check", env=dict(self.env, DAYTRADER_NOW=f"{CDAY}T05:10"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("ALERTS CHECK · 05:10 UTC · 1 active alert on 1 symbol\n"), p.stdout)
        self.assertIn("traded below 98,000.00 04:50-04:55 UTC (low 97,900.00) · last 100,000.00 · one-shot, now off",
                      p.stdout)
        self.assertNotIn("market closed", p.stdout)
        j = run_cli(SCRIPT, "list", "--json", env=dict(self.env, DAYTRADER_NOW=f"{CDAY}T05:10"))
        row = json.loads(j.stdout)["alerts"][0]
        self.assertEqual((row["market"], row["status"], row["fire_count"]), ("crypto", "fired", 1))
        t = run_cli(SCRIPT, "list", env=dict(self.env, DAYTRADER_NOW=f"{CDAY}T05:10"))
        self.assertIn("prices as of 10-31 05:10 UTC", t.stdout)

    def test_missing_coinbase_data_is_exit_2(self):
        empty = self.home / "empty"
        empty.mkdir()
        p = run_cli(SCRIPT, "add", "BTC-USD", "--above", "101000", env=dict(self.env, DAYTRADER_FIXTURES=str(empty)))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("api.exchange.coinbase.com", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)


# ---------------------------------------------------------------------------
# Crypto on Binance (the default exchange): BTC-USDT, BTC-USD priced from BTCUSDT, sub-cent coins
# ---------------------------------------------------------------------------

def scaled(bars, k):
    return [tc.Bar(b.ts, b.open * k, b.high * k, b.low * k, b.close * k, b.volume / k) for b in bars]


class BinanceCoreTests(TempHomeTestCase):
    NOW = f"{CDAY}T04:40"

    def setUp(self):
        super().setUp()
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "binance"            # temp_home restores it
        tc.set_market(tc.CRYPTO)

    def test_sub_cent_coin_levels_stay_exact(self):
        bars = scaled(crypto_bars(), 1e-10)                       # ~0.00001 USDT, one dip to 0.00000979
        now = tc.now_et()
        ctx = A.market_context([b for b in bars if b.ts < now.timestamp()], None, now)
        self.assertEqual(ctx["last"], 1e-05)
        a = A.make_alert("PEPE-USDT", "below", ctx, now, [], level=0.0000098, once=True)
        self.assertEqual((a["level"], a["exchange"], a["legs"][0]["armed"]), (9.8e-06, "binance", True))
        self.assertEqual(A.condition_text(a), "below 0.000009800")
        r = A.evaluate(a, bars, None, tc.to_local(utc_ts(CDAY, "05:10"), tc.CRYPTO))
        self.assertEqual([(e["bar_time"], e["level"]) for e in r["events"]], [("04:50", 9.8e-06)])
        band = A.make_alert("PEPE-USDT", "pct_move", ctx, now, [], pct=2.0)
        self.assertEqual([leg["level"] for leg in band["legs"]], [1.02e-05, 9.8e-06])   # not 0.0
        tc.set_market(tc.US)                                       # stocks: 4 decimals, as before
        self.assertEqual((A._exact(370.0399780), A._px(0.00001234)), (370.04, 0.0))

    def test_unknown_pair_is_bad_input(self):
        with fake_http({"klines": tc.FetchError("HTTP 400 for https://data-api.binance.vision/api/v3/klines?symbol=NOPEUSDT")}):
            with self.assertRaises(ValueError) as cm:
                A.fetch("NOPE-USDT", "2d")
        self.assertEqual(str(cm.exception), "NOPE-USDT: Binance has no candles for this pair (not a listed pair? "
                                             "crypto pairs look like BTC-USDT or BTC-USD)")

    def test_only_crypto_alerts_record_an_exchange(self):
        now = tc.now_et()
        ctx = A.market_context([b for b in crypto_bars() if b.ts < now.timestamp()], None, now)
        self.assertEqual(A.make_alert("BTC-USDT", "above", ctx, now, [], level=101_000.0)["exchange"], "binance")
        tc.set_market(tc.US)
        us = A.make_alert("TSLA", "above", A.market_context(BARS, None, at("09:42")), at("09:42"), [], level=101.0)
        self.assertNotIn("exchange", us)
        self.assertNotIn("exchange", A.summary_row(us))


class BinanceCommandTests(TempHomeTestCase):
    NOW = f"{CDAY}T04:40"

    def cli(self, *argv, now):
        os.environ["DAYTRADER_NOW"] = now
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = A.main(list(argv))
        return code, out.getvalue()

    def test_an_alert_set_on_coinbase_says_when_binance_checks_it(self):
        crypto = crypto_bars()
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "coinbase"
        with fake_bars({("BTC-USD", "5m"): [b for b in crypto if b.ts < utc_ts(CDAY, "04:40")]}):
            self.cli("add", "BTC-USD", "--below", "98000", now=f"{CDAY}T04:40")
            self.cli("add", "BTC-USD", "--above", "150000", now=f"{CDAY}T04:40")
        state = json.loads((self.home / "alerts.json").read_text())
        self.assertEqual([a["exchange"] for a in state["alerts"]], ["coinbase", "coinbase"])
        state["alerts"][1].pop("exchange")                       # saved by v1.1.0: no field, Coinbase prices
        (self.home / "alerts.json").write_text(json.dumps(state))
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "binance"
        with fake_bars({("BTC-USD", "5m"): crypto}):
            code, out = self.cli("check", now=f"{CDAY}T05:10")
            self.assertEqual(code, 0)
            self.assertIn("FIRED  ALR-20261031-001  BTC-USD  below 98,000.00", out)
            self.assertIn("note: BTC-USD: ALR-20261031-001, ALR-20261031-002 were set on Coinbase prices, "
                          "now checked on Binance prices", out)
            _, again = self.cli("check", "--json", now=f"{CDAY}T05:15")
        rep = json.loads(again)
        self.assertEqual(rep["notes"], [])                            # said once: the alerts now read Binance
        self.assertEqual((rep["symbols"]["BTC-USD"]["exchange"], rep["alerts"][0]["exchange"]), ("binance", "binance"))
        self.assertEqual({a["exchange"] for a in json.loads((self.home / "alerts.json").read_text())["alerts"]},
                         {"binance"})


class BinanceCliTests(TempHomeTestCase):
    """The real script against Binance fixtures on a Saturday (the same tape as CryptoCliTests)."""

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fx"
        bars = crypto_bars()
        for hhmm in ("04:40", "05:10"):                       # the live window's URL moves with "now"
            write_binance_fixtures(self.fx, "BTC-USDT", bars, "5m", "2d", now_ts=utc_ts(CDAY, hhmm))
        self.env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(self.fx),
                    "DAYTRADER_NOW": f"{CDAY}T04:40", "DAYTRADER_CRYPTO_EXCHANGE": "binance"}

    def test_add_check_list_btc_usdt_on_a_saturday(self):
        p = run_cli(SCRIPT, "add", "BTC-USDT", "--below", "98000", "--once", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Added ALR-20261031-001 · BTC-USDT below 98,000.00 · once · armed", p.stdout)
        self.assertIn("  last 100,000.00 on Binance · fires at <= 98,000.00 (-2.00% away)", p.stdout)
        p = run_cli(SCRIPT, "add", "BTC-USD", "--above", "150000", env=self.env)     # read from BTCUSDT
        self.assertEqual(p.returncode, 0, p.stderr)
        p = run_cli(SCRIPT, "check", env=dict(self.env, DAYTRADER_NOW=f"{CDAY}T05:10"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("ALERTS CHECK · 05:10 UTC · 2 active alerts on 2 symbols\n"), p.stdout)
        self.assertIn("traded below 98,000.00 04:50-04:55 UTC (low 97,900.00) · last 100,000.00 · one-shot, now off",
                      p.stdout)
        self.assertNotIn("Coinbase", p.stdout + p.stderr)
        self.assertNotIn("note:", p.stdout)
        j = run_cli(SCRIPT, "list", "--json", env=dict(self.env, DAYTRADER_NOW=f"{CDAY}T05:10"))
        rows = {r["symbol"]: r for r in json.loads(j.stdout)["alerts"]}
        self.assertEqual((rows["BTC-USDT"]["exchange"], rows["BTC-USDT"]["status"]), ("binance", "fired"))
        self.assertEqual((rows["BTC-USD"]["exchange"], rows["BTC-USD"]["market"]), ("binance", "crypto"))

    def test_binance_is_the_default_and_its_host_is_named_when_down(self):
        os.environ.pop("DAYTRADER_CRYPTO_EXCHANGE")                          # no variable (temp_home restores it)
        env = {k: v for k, v in self.env.items() if k != "DAYTRADER_CRYPTO_EXCHANGE"}   # and no setting
        p = run_cli(SCRIPT, "add", "BTCUSDT", "--pct-move", "2", "--json", env=env)
        self.assertEqual(p.returncode, 0, p.stderr)                         # only Binance fixtures exist
        a = json.loads(p.stdout)["added"][0]
        self.assertEqual((a["exchange"], a["ref_price"], [g["level"] for g in a["legs"]]),
                         ("binance", 100_000.0, [102_000.0, 98_000.0]))
        empty = self.home / "empty"
        empty.mkdir()
        p = run_cli(SCRIPT, "add", "BTC-USDT", "--above", "101000", env=dict(env, DAYTRADER_FIXTURES=str(empty)))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("data-api.binance.vision", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)


if __name__ == "__main__":
    unittest.main()
