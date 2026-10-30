"""Tests for the entry skill (skills/entry/scripts/entry.py)."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

# tests/ is not a package: make `import helpers` work for `python3 -m unittest tests.test_entry` too.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (TempHomeTestCase, et_ts, fake_bars, load_skill_module, run_cli, skill_script,  # noqa: E402
                     tc, write_fixture, yahoo_chart_url, yahoo_payload)

E = load_skill_module("entry", "entry")
SCRIPT = skill_script("entry", "entry.py")
DAY, PREV = "2026-10-30", "2026-10-29"   # Friday, Thursday


def mk(day, rows, start="09:30", volume=100_000):
    """Bars from (open, high, low, close[, volume]) rows on the 5-minute grid."""
    t0 = et_ts(day, start)
    out = []
    for i, r in enumerate(rows):
        o, h, l, c = r[:4]
        out.append(tc.Bar(t0 + i * 300, o, h, l, c, r[4] if len(r) > 4 else volume))
    return out


def flat(price, n, volume=100_000):
    return [(price, price, price, price, volume)] * n


# OR (09:30-09:45): high 101.5, low 99.0, mid 100.25. Then a long break on the 09:50 bar.
ORB_ROWS = [
    (100.0, 101.0, 99.0, 100.5),
    (100.5, 101.5, 99.5, 101.0),
    (101.0, 101.2, 100.0, 100.8),
    (100.8, 101.4, 100.6, 101.3),   # 09:45 - inside the range
    (101.3, 102.0, 101.2, 101.9),   # 09:50 - close 101.9 > 101.5 -> trigger, closes 09:55
    (101.9, 102.5, 101.5, 102.2),
]

PREV_ROWS = [(98.0, 99.0, 97.0, 98.5), (98.5, 100.0, 98.0, 99.5)] + flat(99.0, 76)  # PDH 100, PDL 97


def at(day, hhmm):
    return et_ts(day, hhmm)


def clock(times):
    """A tc.now_et replacement that walks `times` (ET ISO) and then stays on the last one."""
    seq = [tc.parse_et(t) for t in times]
    state = {"i": 0}

    def now():
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]
    return now


class OrbTests(unittest.TestCase):
    def test_long_trigger_hand_checked(self):
        r = E.find_trigger(mk(DAY, ORB_ROWS), "orb", "both", {"now_ts": at(DAY, "10:05")})
        self.assertEqual(r["status"], "TRIGGERED")
        self.assertEqual(r["side"], "long")
        self.assertEqual(r["triggered_at"], "09:55")
        self.assertEqual(r["trigger_bar"]["start"], "09:50")
        self.assertEqual(r["entry"], 101.9)
        self.assertEqual(r["suggested_stop"], 99.0)
        self.assertEqual(r["stop_basis"], "OR low")
        self.assertAlmostEqual(r["risk_per_share"], 2.9)
        ref = r["reference"]
        self.assertEqual((ref["or_high"], ref["or_low"], ref["or_mid"]), (101.5, 99.0, 100.25))
        self.assertEqual(ref["or_bars"], 3)
        self.assertEqual(r["bars_checked"], 5)
        self.assertEqual(r["last_price"], 102.2)       # the 10:00 bar has started (still forming)
        self.assertFalse(r["since_trigger"]["stop_hit"])

    def test_short_trigger_and_stop_is_or_high(self):
        rows = ORB_ROWS[:4] + [(100.8, 100.9, 98.5, 98.7), (98.7, 99.0, 98.0, 98.2)]
        r = E.find_trigger(mk(DAY, rows), "orb", None, {"now_ts": at(DAY, "10:05")})
        self.assertEqual((r["status"], r["side"], r["entry"]), ("TRIGGERED", "short", 98.7))
        self.assertEqual((r["suggested_stop"], r["stop_basis"]), (101.5, "OR high"))

    def test_side_filter_ignores_the_other_break(self):
        rows = ORB_ROWS[:3] + [(100.8, 100.9, 98.5, 98.7), (98.7, 101.0, 98.6, 100.9),
                               (100.9, 102.0, 100.8, 101.8)]
        r = E.find_trigger(mk(DAY, rows), "orb", "long", {"now_ts": at(DAY, "10:05")})
        self.assertEqual((r["side"], r["triggered_at"], r["entry"]), ("long", "10:00", 101.8))
        r = E.find_trigger(mk(DAY, rows), "orb", "both", {"now_ts": at(DAY, "10:05")})
        self.assertEqual((r["side"], r["triggered_at"]), ("short", "09:50"))

    def test_wick_or_close_equal_to_the_high_does_not_trigger(self):
        rows = ORB_ROWS[:3] + [(100.8, 102.5, 100.5, 101.0), (101.0, 101.6, 100.9, 101.5)]
        r = E.find_trigger(mk(DAY, rows), "orb", "both", {"now_ts": at(DAY, "10:00")})
        self.assertEqual(r["status"], "WAITING")
        self.assertEqual([(w["side"], w["level"], w["armed"]) for w in r["watching"]],
                         [("long", 101.5, True), ("short", 99.0, True)])

    def test_only_completed_bars_trigger(self):
        bars = mk(DAY, ORB_ROWS)
        r = E.find_trigger(bars, "orb", "both", {"now_ts": at(DAY, "09:54") + 59})
        self.assertEqual(r["status"], "WAITING")             # 09:50 bar still forming
        self.assertEqual(r["bars_checked"], 4)
        self.assertEqual(r["last_price"], 101.9)
        r = E.find_trigger(bars, "orb", "both", {"now_ts": at(DAY, "09:55")})
        self.assertEqual((r["status"], r["triggered_at"]), ("TRIGGERED", "09:55"))

    def test_opening_range_still_forming(self):
        r = E.find_trigger(mk(DAY, ORB_ROWS), "orb", "both", {"now_ts": at(DAY, "09:42")})
        self.assertEqual(r["status"], "WAITING")
        self.assertFalse(r["reference"]["or_complete"])
        self.assertEqual(r["watching"], [])
        self.assertTrue(any("still forming" in n for n in r["notes"]))

    def test_before_the_open(self):
        r = E.find_trigger([], "orb", "both", {"now_ts": at(DAY, "08:00"), "date": DAY},
                           mk(PREV, PREV_ROWS))
        self.assertEqual((r["status"], r["bars_checked"], r["last_price"]), ("WAITING", 0, 99.0))
        self.assertEqual(r["last_source"], "prev_close")
        self.assertTrue(any("not open yet" in n for n in r["notes"]))

    def test_cutoff(self):
        bars = mk(DAY, ORB_ROWS)
        r = E.find_trigger(bars, "orb", "both", {"cutoff": "09:55"})          # replay; closes at 09:55
        self.assertEqual(r["status"], "TRIGGERED")
        r = E.find_trigger(bars, "orb", "both", {"cutoff": "09:50"})
        self.assertEqual((r["status"], r["expired_reason"]), ("EXPIRED", "cutoff"))
        self.assertEqual(r["bars_checked"], 4)
        r = E.find_trigger(bars, "orb", "both", {"cutoff": "09:50", "now_ts": at(DAY, "09:49")})
        self.assertEqual(r["status"], "WAITING")

    def test_late_open_uses_first_minutes_of_trading(self):
        bars = mk(DAY, ORB_ROWS, start="10:00")
        r = E.find_trigger(bars, "orb", "both", {"now_ts": at(DAY, "10:35")})
        self.assertEqual((r["reference"]["or_start"], r["reference"]["or_end"]), ("10:00", "10:15"))
        self.assertEqual((r["status"], r["triggered_at"]), ("TRIGGERED", "10:25"))
        self.assertTrue(any("late open" in n for n in r["notes"]))

    def test_longer_opening_range(self):
        r = E.find_trigger(mk(DAY, ORB_ROWS), "orb", "both", {"or_minutes": 30})
        self.assertEqual(r["reference"]["or_high"], 102.5)
        self.assertEqual(r["status"], "EXPIRED")   # no bars after 10:00 in this session
        self.assertEqual(r["label"], "ORB(30)")

    def test_replay_without_trigger_expires(self):
        r = E.find_trigger(mk(DAY, ORB_ROWS[:3] + flat(100.0, 30)), "orb", "both", {})
        self.assertEqual((r["status"], r["session_over"]), ("EXPIRED", True))

    def test_empty_replay_session(self):
        r = E.find_trigger([], "orb", "both", {"date": DAY})
        self.assertEqual((r["status"], r["bars_checked"], r["entry"]), ("EXPIRED", 0, None))


class VwapTests(unittest.TestCase):
    ROWS = [(100.0, 100.0, 100.0, 100.0), (100.0, 100.0, 98.0, 98.0), (98.0, 99.5, 97.5, 99.4)]

    def test_reclaim_long_hand_checked(self):
        # VWAP: 100, (100 + 98.667)/2 = 99.333, then (100 + 98.667 + 98.8)/3 = 99.156.
        r = E.find_trigger(mk(DAY, self.ROWS), "vwap", "both", {})
        self.assertEqual((r["status"], r["side"], r["triggered_at"]), ("TRIGGERED", "long", "09:45"))
        self.assertEqual(r["entry"], 99.4)
        self.assertAlmostEqual(r["reference"]["vwap"], 99.16, places=2)
        self.assertEqual((r["suggested_stop"], r["stop_basis"]), (97.5, "trigger-bar low"))

    def test_needs_three_bars(self):
        rows = [(100.0, 100.0, 99.0, 99.0), (99.0, 101.0, 99.0, 101.0), (101.0, 101.5, 100.8, 101.2)]
        r = E.find_trigger(mk(DAY, rows), "vwap", "long", {})
        self.assertEqual(r["status"], "EXPIRED")   # the only cross was on bar 2

    def test_short_mirror(self):
        rows = [(100.0, 100.0, 100.0, 100.0), (100.0, 102.0, 100.0, 102.0), (102.0, 102.5, 100.4, 100.5)]
        r = E.find_trigger(mk(DAY, rows), "vwap", "both", {})
        self.assertEqual((r["side"], r["entry"], r["suggested_stop"]), ("short", 100.5, 102.5))

    def test_waiting_shows_vwap_and_arming(self):
        rows = [(100.0, 100.0, 100.0, 100.0), (100.0, 100.0, 98.0, 98.0), (98.0, 98.5, 97.5, 98.0)]
        r = E.find_trigger(mk(DAY, rows), "vwap", "both", {"now_ts": at(DAY, "09:46")})
        self.assertEqual(r["status"], "WAITING")
        armed = {w["side"]: w["armed"] for w in r["watching"]}
        self.assertEqual(armed, {"long": True, "short": False})


class LevelTests(unittest.TestCase):
    def test_cross_above(self):
        rows = [(99.0, 99.5, 98.8, 99.2), (99.2, 100.3, 99.1, 100.2), (100.2, 100.5, 99.9, 100.4)]
        r = E.find_trigger(mk(DAY, rows), "level", "long", {"level": 100.0})
        self.assertEqual((r["triggered_at"], r["entry"], r["suggested_stop"]), ("09:40", 100.2, 99.1))
        self.assertEqual(r["reference"], {"level": 100.0})

    def test_gap_through_level_triggers_on_first_close_with_level_stop(self):
        prev = mk(PREV, [(98.0, 99.0, 97.5, 98.5)])
        rows = [(101.0, 101.5, 100.6, 101.2), (101.2, 101.4, 100.9, 101.0)]
        r = E.find_trigger(mk(DAY, rows), "level", "both", {"level": 100.0}, prev)
        self.assertEqual((r["side"], r["triggered_at"], r["entry"]), ("long", "09:35", 101.2))
        self.assertEqual((r["suggested_stop"], r["stop_basis"]), (100.0, "back below level"))
        self.assertTrue(any("gapped through" in n for n in r["notes"]))

    def test_not_armed_when_price_was_already_above(self):
        prev = mk(PREV, [(101.0, 101.5, 100.5, 101.0)])
        above = [(101.0, 101.3, 100.8, 101.1), (101.1, 101.6, 101.0, 101.5)]
        r = E.find_trigger(mk(DAY, above), "level", "long", {"level": 100.0, "now_ts": at(DAY, "09:41")}, prev)
        self.assertEqual(r["status"], "WAITING")
        self.assertFalse(r["watching"][0]["armed"])
        dip = above + [(101.5, 101.5, 99.5, 99.8), (99.8, 100.6, 99.7, 100.5)]
        r = E.find_trigger(mk(DAY, dip), "level", "long", {"level": 100.0}, prev)
        self.assertEqual((r["status"], r["triggered_at"], r["entry"]), ("TRIGGERED", "09:50", 100.5))

    def test_both_sides_breaks_whichever_way_first(self):
        prev = mk(PREV, [(101.0, 101.5, 100.5, 101.0)])
        rows = [(101.0, 101.2, 99.6, 99.7), (99.7, 99.9, 99.0, 99.2)]
        r = E.find_trigger(mk(DAY, rows), "level", None, {"level": 100.0}, prev)
        self.assertEqual((r["side"], r["triggered_at"], r["suggested_stop"]), ("short", "09:35", 101.2))

    def test_pdh_and_pdl(self):
        prev = mk(PREV, PREV_ROWS)
        up = [(99.0, 99.8, 98.9, 99.5), (99.5, 100.4, 99.4, 100.3)]
        r = E.find_trigger(mk(DAY, up), "pdh", None, {}, prev)
        self.assertEqual((r["side"], r["reference"]["pdh"], r["entry"]), ("long", 100.0, 100.3))
        self.assertEqual(r["context"]["pdl"], 97.0)
        down = [(98.0, 98.2, 96.5, 96.8)]
        r = E.find_trigger(mk(DAY, down), "pdl", "both", {}, prev)
        self.assertEqual((r["side"], r["reference"]["pdl"], r["entry"]), ("short", 97.0, 96.8))
        self.assertEqual(r["suggested_stop"], 98.2)

    def test_pdh_input_errors(self):
        with self.assertRaises(ValueError):
            E.find_trigger(mk(DAY, ORB_ROWS), "pdh", "short", {}, mk(PREV, PREV_ROWS))
        with self.assertRaises(ValueError):
            E.find_trigger(mk(DAY, ORB_ROWS), "pdh", None, {}, None)


class RvolAndParamTests(unittest.TestCase):
    def test_min_rvol_skips_light_break_and_takes_the_heavy_one(self):
        rows = [r + (100_000,) for r in ORB_ROWS[:4]] + [
            (101.3, 102.0, 101.2, 101.9, 120_000),   # break on 1.2x -> skipped
            (101.9, 102.5, 101.5, 102.2, 300_000),   # 300k / avg(100k x4, 120k) = 2.88x
        ]
        r = E.find_trigger(mk(DAY, rows), "orb", "long", {"min_rvol": 2.0})
        self.assertEqual((r["triggered_at"], r["entry"]), ("10:00", 102.2))
        self.assertAlmostEqual(r["rvol"], 300_000 / 104_000, places=2)
        self.assertEqual(len(r["rejected"]), 1)
        self.assertEqual(r["rejected"][0]["time"], "09:55")
        self.assertEqual(r["rejected"][0]["rvol"], 1.2)

    def test_min_rvol_needs_three_prior_bars(self):
        rows = [(100.0, 101.0, 99.0, 100.0, 100_000), (100.0, 102.0, 100.0, 101.8, 900_000)]
        r = E.find_trigger(mk(DAY, rows), "orb", "long", {"or_minutes": 5, "min_rvol": 1.5})
        self.assertEqual(r["status"], "EXPIRED")
        self.assertIn("fewer than 3", r["rejected"][0]["reason"])
        r = E.find_trigger(mk(DAY, rows), "orb", "long", {"or_minutes": 5})
        self.assertEqual(r["status"], "TRIGGERED")
        self.assertIsNone(r["rvol"])

    def test_bad_params(self):
        bad = [("orb", {"or_minutes": 7}), ("orb", {"or_minutes": 0}), ("orb", {"cutoff": "09:40"}),
               ("vwap", {"cutoff": "09:30"}), ("vwap", {"cutoff": "16:05"}), ("level", {}),
               ("level", {"level": -1}), ("orb", {"min_rvol": -1}), ("nope", {}),
               ("orb", {"cutoff": "11h30"})]
        for setup, params in bad:
            with self.assertRaises(ValueError, msg=(setup, params)):
                E.check_params(setup, params)
        with self.assertRaises(ValueError):
            E.resolve_side("orb", "sideways")
        self.assertEqual(E.resolve_side("pdl", "both"), "short")
        self.assertEqual(E.resolve_side("vwap", None), "both")


class SessionTests(unittest.TestCase):
    def test_half_day_session_over(self):
        bars = mk(DAY, ORB_ROWS[:3] + flat(100.0, 39))     # 09:30-12:55
        r = E.find_trigger(bars, "orb", "both", {"cutoff": "14:00", "session_end": 780,
                                                 "now_ts": at(DAY, "13:30")})
        self.assertEqual((r["status"], r["expired_reason"]), ("EXPIRED", "session over"))
        r = E.find_trigger(bars, "orb", "both", {"cutoff": "14:00", "now_ts": at(DAY, "13:30")})
        self.assertEqual(r["status"], "WAITING")

    def test_session_end_from_yahoo_trading_periods(self):
        start, end = at("2026-11-27", "09:30"), at("2026-11-27", "13:00")
        meta = {"tradingPeriods": [[{"timezone": "EST", "start": start, "end": end, "gmtoffset": -18000}]]}
        self.assertEqual(E.session_end_minute(meta, "2026-11-27"), 780)
        self.assertEqual(E.session_end_minute(meta, "2026-11-25"), 960)
        meta2 = {"currentTradingPeriod": {"regular": {"start": start, "end": end}},
                 "tradingPeriods": {"regular": [[{"start": "x"}]]}}
        self.assertEqual(E.session_end_minute(meta2, "2026-11-27"), 780)
        self.assertEqual(E.session_end_minute({}, DAY), 960)
        self.assertEqual(E.session_end_minute({"tradingPeriods": "junk"}, DAY), 960)

    def test_pick_session(self):
        sessions = {PREV: mk(PREV, PREV_ROWS), DAY: mk(DAY, ORB_ROWS)}
        now = tc.parse_et("2026-10-31T12:00")                # Saturday
        day, replay, notes = E.pick_session(sessions, now)
        self.assertEqual((day, replay), (DAY, True))
        self.assertIn("no session today", notes[0])
        self.assertEqual(E.pick_session({PREV: []}, tc.parse_et("2026-10-30T08:00"))[:2], (DAY, False))
        self.assertEqual(E.pick_session(sessions, tc.parse_et("2026-10-30T10:00"), PREV)[:2], (PREV, True))
        for bad in ("2026-10-31", "2026-11-02", "2026-10-20"):
            with self.assertRaises(ValueError):
                E.pick_session(sessions, tc.parse_et("2026-10-31T12:00"), bad)

    def test_weekday_with_no_bars_yet(self):
        r = E.evaluate("HOL", mk(PREV, PREV_ROWS), {}, tc.parse_et("2026-10-30T10:00"), "orb", None, {})
        self.assertEqual((r["date"], r["status"], r["replay"]), (DAY, "WAITING", False))
        self.assertTrue(any("no 5-minute bars yet" in n for n in r["notes"]))
        self.assertFalse(any("opening range forms" in n for n in r["notes"]))

    def test_normalize_snaps_live_bar_and_merges_duplicates(self):
        t = at(DAY, "10:20")
        bars = [tc.Bar(t, 100.0, 100.5, 99.8, 100.2, 5000),
                tc.Bar(t + 157, 100.2, 100.9, 100.1, 100.7, 7000),     # live point stamped 10:22:37
                tc.Bar(t - 300, 99.9, 100.1, 99.7, 100.00000610351562, 4000)]
        out = E.normalize_bars(bars)
        self.assertEqual([b.ts for b in out], [t - 300, t])
        self.assertEqual((out[1].open, out[1].high, out[1].low, out[1].close, out[1].volume),
                         (100.0, 100.9, 99.8, 100.7, 7000))
        self.assertEqual(out[0].close, 100.0)
        r = E.find_trigger(out, "vwap", "both", {"now_ts": t + 200})
        self.assertEqual(r["bars_checked"], 1)                 # 10:20 bar still forming at 10:23:20

    def test_since_trigger_reports_a_hit_stop(self):
        rows = ORB_ROWS[:5] + [(101.9, 102.0, 98.9, 99.2)]
        r = E.find_trigger(mk(DAY, rows), "orb", "long", {"now_ts": at(DAY, "10:05")})
        st = r["since_trigger"]
        self.assertEqual((st["stop_hit"], st["stop_hit_at"], st["minutes"]), (True, "10:00", 10))
        self.assertAlmostEqual(st["move_r"], (99.2 - 101.9) / 2.9, places=2)
        self.assertTrue(any("already hit" in n for n in r["notes"]))

    def test_evaluate_picks_today_and_previous_session(self):
        bars = mk(PREV, PREV_ROWS) + mk(DAY, ORB_ROWS)
        r = E.evaluate("tsla", bars, {}, tc.parse_et("2026-10-30T10:05"), "orb", None, {})
        self.assertEqual((r["symbol"], r["date"], r["replay"], r["status"]), ("TSLA", DAY, False, "TRIGGERED"))
        self.assertEqual(r["context"]["pdh"], 100.0)
        self.assertEqual(r["disclaimer"], tc.DISCLAIMER)
        r = E.evaluate("TSLA", bars, {}, tc.parse_et("2026-10-30T10:05"), "orb", None, {}, PREV)
        self.assertTrue(r["replay"])
        self.assertIsNone(r["context"]["pdh"])       # no session before PREV in this data
        with self.assertRaises(ValueError):          # ...so a PDH break cannot be evaluated there
            E.evaluate("TSLA", bars, {}, tc.parse_et("2026-10-30T10:05"), "pdh", None, {}, PREV)


class PlanTests(TempHomeTestCase):
    NOW = "2026-10-30T10:05"

    def triggered(self):
        return E.evaluate("TSLA", mk(PREV, PREV_ROWS) + mk(DAY, ORB_ROWS), {}, tc.now_et(), "orb", None, {})

    def test_plan_fields(self):
        plan, cleared = E.plan_from_result(self.triggered(), None)
        self.assertEqual({k: plan[k] for k in ("symbol", "side", "entry", "stop", "setup")},
                         {"symbol": "TSLA", "side": "long", "entry": 101.9, "stop": 99.0, "setup": "orb"})
        self.assertEqual(plan["entry_signal"]["triggered_at"], "09:55")
        self.assertEqual(plan["entry_signal"]["or_minutes"], 15)
        self.assertEqual(cleared, [])

    def test_stale_downstream_sections_are_cleared(self):
        tc.save_plan({"symbol": "TSLA", "side": "short", "entry": 105.0, "stop": 107.0, "target": 101.0,
                      "time_machine": {"verdict": "ODDS LEAN YOUR WAY"}, "sizing": {"shares": 10}})
        res = self.triggered()
        E.save_result(res)
        self.assertEqual(sorted(res["saved"]["cleared"]), ["sizing", "target", "time_machine"])
        plan = tc.load_plan("TSLA")
        self.assertEqual((plan["side"], plan["entry"], plan["stop"]), ("long", 101.9, 99.0))
        self.assertIsNone(plan["target"])
        self.assertIsNone(plan["time_machine"])
        self.assertEqual(plan["entry_signal"]["setup"], "orb")
        self.assertIn("created_at", plan)

    def test_same_trigger_keeps_downstream_sections(self):
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 101.9, "stop": 99.0, "target": 107.7,
                      "exit_plan": {"rr": 2.0}})
        res = self.triggered()
        E.save_result(res)
        self.assertEqual(res["saved"]["cleared"], [])
        self.assertEqual(tc.load_plan("TSLA")["target"], 107.7)

    def test_waiting_is_not_saved(self):
        res = E.evaluate("TSLA", mk(DAY, ORB_ROWS[:4]), {}, tc.now_et(), "orb", None, {})
        E.save_result(res)
        self.assertIsNone(tc.load_plan("TSLA"))
        self.assertIn("WAITING", res["save_skipped"])
        with self.assertRaises(ValueError):
            E.plan_from_result(res, None)


class WatchTests(TempHomeTestCase):
    def opts(self, *argv):
        args = E.build_parser().parse_args(["TSLA", "--watch", *argv])
        return E.options_from_args(args, tc.parse_et("2026-10-30T09:50"))

    def run_watch(self, opts, times, as_json=False, save=False, fetch_side_effect=None):
        bars = mk(PREV, PREV_ROWS) + mk(DAY, ORB_ROWS)
        out, err = io.StringIO(), io.StringIO()
        with fake_bars({"TSLA": bars}) as fb, \
                mock.patch.object(tc, "now_et", side_effect=clock(times)), \
                mock.patch.object(E.time, "sleep") as sleep, redirect_stdout(out), redirect_stderr(err):
            if fetch_side_effect:
                fb.side_effect = fetch_side_effect
            code = E.watch(opts, as_json, save)
        return code, out.getvalue(), err.getvalue(), sleep

    def test_watch_until_trigger_then_save(self):
        code, out, _, sleep = self.run_watch(self.opts(), ["2026-10-30T09:50", "2026-10-30T09:53",
                                                           "2026-10-30T09:56"], save=True)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("09:50 ET  WAITING"))
        self.assertTrue(lines[2].startswith("09:56 ET  TRIGGERED"))
        self.assertIn("TSLA 5m · ORB(15) · TRIGGERED 09:55 ET · LONG @ 101.90 (OR 99.00–101.50)", out)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(60.0)
        self.assertEqual(tc.load_plan("TSLA")["entry"], 101.9)

    def test_watch_stops_after_max_minutes(self):
        code, out, _, sleep = self.run_watch(self.opts("--interval", "60", "--max-minutes", "3"),
                                             ["2026-10-30T09:50"] * 4, as_json=True)
        self.assertEqual(code, 0)
        rows = [json.loads(x) for x in out.splitlines()]
        self.assertEqual([r.get("poll") for r in rows], [1, 2, 3, 4, None])
        self.assertTrue(rows[-1]["final"])
        self.assertEqual(rows[-1]["watch"]["stopped"], "max-minutes")
        self.assertEqual(sleep.call_count, 3)

    def test_watch_survives_one_data_error(self):
        bars = mk(PREV, PREV_ROWS) + mk(DAY, ORB_ROWS)
        effects = [(bars, {}), tc.FetchError("TSLA: HTTP 503 for https://query1.finance.yahoo.com/x"),
                   (bars, {})]
        code, out, err, sleep = self.run_watch(self.opts(), ["2026-10-30T09:51", "2026-10-30T09:53",
                                                             "2026-10-30T09:56"], fetch_side_effect=effects)
        self.assertEqual(code, 0)
        self.assertIn("data error (retrying)", err)
        self.assertIn("TRIGGERED", out)
        self.assertEqual(sleep.call_count, 2)

    def test_unreachable_from_the_first_check_fails_fast_naming_the_host(self):
        # Review fix: a blocked host used to sleep through two retries (2+ min, past a chat command
        # timeout) and, with --max-minutes 1, ended on a generic message without the host.
        err = tc.FetchError("TSLA: HTTP 403 for https://query2.finance.yahoo.com/v8/finance/chart/TSLA")
        for argv in ((), ("--max-minutes", "1")):
            with self.assertRaises(tc.FetchError) as cm:
                self.run_watch(self.opts(*argv), ["2026-10-30T09:50"], fetch_side_effect=[err] * 5)
            self.assertIn("query2.finance.yahoo.com", str(cm.exception))
        with fake_bars({}), mock.patch.object(E.time, "sleep") as sleep, redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()), mock.patch.object(tc, "now_et", side_effect=clock(["2026-10-30T09:50"])):
            with self.assertRaises(tc.FetchError):
                E.watch(self.opts(), False, False)
        sleep.assert_not_called()

    def test_three_failures_in_a_row_mid_watch_raise_the_data_error(self):
        bars = mk(PREV, PREV_ROWS) + mk(DAY, ORB_ROWS)
        err = tc.FetchError("TSLA: network error for https://query2.finance.yahoo.com/x: timed out")
        with self.assertRaises(tc.FetchError) as cm:
            self.run_watch(self.opts(), ["2026-10-30T09:50"], fetch_side_effect=[(bars, {}), err, err, err])
        self.assertIn("query2.finance.yahoo.com", str(cm.exception))

    def test_ctrl_c_exits_130(self):
        code, out, _, _ = self.run_watch(self.opts(), ["2026-10-30T09:50"],
                                         fetch_side_effect=[KeyboardInterrupt()])
        self.assertEqual(code, 130)


class ReviewRegressionTests(TempHomeTestCase):
    """Defects found in review; each test failed before its fix."""

    def test_stop_hit_inside_the_forming_bar_is_reported(self):
        # Long trigger 09:55 (stop = OR low 99.00); at 09:57 the forming 09:55 bar has already traded
        # 98.50 and sits at 98.70 (-1.1R). The card must not say "stop not hit".
        rows = ORB_ROWS[:5] + [(101.9, 102.0, 98.5, 98.7)]
        r = E.find_trigger(mk(DAY, rows), "orb", "long", {"now_ts": at(DAY, "09:57")})
        self.assertEqual((r["status"], r["triggered_at"]), ("TRIGGERED", "09:55"))   # trigger unchanged
        st = r["since_trigger"]
        self.assertTrue(st["stop_hit"])
        self.assertEqual(st["stop_hit_at"], "09:57")
        self.assertEqual((st["low"], st["high"], st["bars"]), (98.5, 102.0, 0))
        self.assertTrue(any("already hit" in n for n in r["notes"]))
        self.assertEqual(r["context"]["low"], 98.5)
        res = dict(r, symbol="TSLA", as_of="2026-10-30T09:57:00-04:00")
        self.assertNotIn("stop not hit", E.render(res))
        # A forming bar that stays above the stop does not count as a hit, and replays have none.
        ok = ORB_ROWS[:5] + [(101.9, 102.4, 101.0, 102.1)]
        r = E.find_trigger(mk(DAY, ok), "orb", "long", {"now_ts": at(DAY, "09:57")})
        self.assertFalse(r["since_trigger"]["stop_hit"])
        self.assertEqual(r["since_trigger"]["high"], 102.4)

    def test_sub_penny_prints_never_round_entry_and_stop_together(self):
        # Close 100.004 is above the 100.00 level, the bar never traded back below it, so the stop is
        # the level; both round to 100.00 -> zero risk, and --save failed validation.
        rows = [(99.9, 100.0, 99.8, 99.99), (100.002, 100.006, 100.001, 100.004)]
        r = E.find_trigger(mk(DAY, rows), "level", "long", {"level": 100.0})
        self.assertEqual(r["status"], "TRIGGERED")
        self.assertLess(r["suggested_stop"], r["entry"])
        self.assertGreater(r["risk_per_share"], 0)
        short = [(100.1, 100.2, 100.0, 100.01), (99.998, 99.999, 99.994, 99.996)]
        r = E.find_trigger(mk(DAY, short), "level", "short", {"level": 100.0})
        self.assertGreater(r["suggested_stop"], r["entry"])
        res = dict(r, symbol="TSLA", date=DAY, replay=True, as_of="2026-10-30T17:00:00-04:00", notes=[])
        E.save_result(res)                                   # raised ValueError before the fix
        self.assertEqual(tc.load_plan("TSLA")["side"], "short")

    def test_save_over_a_plan_file_that_is_not_an_object_is_a_state_error(self):
        (self.home / "plans").mkdir()
        (self.home / "plans" / "TSLA.json").write_text("[1]")
        res = E.evaluate("TSLA", mk(PREV, PREV_ROWS) + mk(DAY, ORB_ROWS), {},
                         tc.parse_et("2026-10-30T10:05"), "orb", None, {})
        with self.assertRaises(tc.StateError):              # exit 1 via tc.run, not a traceback
            E.save_result(res)

    def test_default_cutoff_expiry_says_how_to_look_later(self):
        bars = mk(PREV, PREV_ROWS) + mk(DAY, ORB_ROWS[:3] + flat(100.0, 40))
        r = E.evaluate("AMD", bars, {}, tc.parse_et("2026-10-30T13:00"), "level", "long", {"level": 103.0})
        self.assertEqual((r["status"], r["expired_reason"]), ("EXPIRED", "cutoff"))
        self.assertTrue(any("--cutoff" in n for n in r["notes"]), r["notes"])
        # An explicit cutoff is the user's choice: no hint.
        r = E.evaluate("AMD", bars, {}, tc.parse_et("2026-10-30T13:00"), "level", "long",
                       {"level": 103.0, "cutoff": "11:30"})
        self.assertFalse(any("--cutoff" in n for n in r["notes"]), r["notes"])
        args = E.build_parser().parse_args(["AMD", "--level", "103"])
        self.assertIsNone(E.options_from_args(args, tc.parse_et("2026-10-30T13:00"))["params"]["cutoff"])


class CliTests(TempHomeTestCase):
    NOW = "2026-10-30T10:05"

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fixtures"
        bars = mk(PREV, PREV_ROWS) + mk(DAY, ORB_ROWS)
        write_fixture(self.fx, yahoo_chart_url("TSLA", "5m", "5d"), yahoo_payload(bars, "TSLA"))
        write_fixture(self.fx, yahoo_chart_url("TSLA", "5m", "60d"), yahoo_payload(bars, "TSLA"))
        self.env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(self.fx),
                    "DAYTRADER_NOW": self.NOW}

    def test_text_card(self):
        p = run_cli(SCRIPT, "TSLA", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], "TSLA 5m · ORB(15) · TRIGGERED 09:55 ET · LONG @ 101.90 (OR 99.00–101.50)")
        self.assertIn("suggested stop 99.00 (OR low)", p.stdout)
        self.assertEqual(lines[-1], tc.DISCLAIMER)

    def test_json_and_save(self):
        p = run_cli(SCRIPT, "TSLA", "--json", "--save", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        r = json.loads(p.stdout)
        for k in ("status", "triggered_at", "entry", "side", "reference", "suggested_stop",
                  "bars_checked", "last_price", "since_trigger", "watching", "notes", "saved"):
            self.assertIn(k, r)
        self.assertEqual((r["status"], r["entry"], r["suggested_stop"]), ("TRIGGERED", 101.9, 99.0))
        plan = json.loads((self.home / "plans" / "TSLA.json").read_text())
        self.assertEqual((plan["entry"], plan["stop"], plan["setup"]), (101.9, 99.0, "orb"))

    def test_waiting_with_level_and_replay_date(self):
        p = run_cli(SCRIPT, "TSLA", "--level", "103", "--side", "long", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("LEVEL · WAITING as of 10:05 ET · level 103.00", p.stdout)
        self.assertIn("long  on a 5m close above 103.00", p.stdout)
        p = run_cli(SCRIPT, "TSLA", "--date", PREV, "--setup", "vwap", "--json", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        r = json.loads(p.stdout)
        self.assertEqual((r["date"], r["replay"]), (PREV, True))

    def test_missing_data_is_a_one_line_data_error(self):
        empty = self.home / "empty"
        empty.mkdir()
        p = run_cli(SCRIPT, "TSLA", env=dict(self.env, DAYTRADER_FIXTURES=str(empty)))
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.startswith("data error: "), p.stderr)
        self.assertIn("finance.yahoo.com", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1)

    def test_bad_input_exits_1(self):
        for args in (["--setup", "level"], ["--setup", "pdl", "--side", "long"], ["--live"],
                     ["--date", "2026-11-03"], ["--interval", "30"]):
            p = run_cli(SCRIPT, "TSLA", *args, env=self.env)
            self.assertEqual(p.returncode, 1, (args, p.stderr))
            self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
