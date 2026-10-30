"""Tests for the time-machine skill (skills/time-machine/scripts/time_machine.py)."""
import contextlib
import io
import json
import math
import os
import random
import re
import shlex
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

# Works both with `unittest discover -s tests` and `python3 -m unittest tests.test_time_machine`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (SKILLS, TempHomeTestCase, bars_from_closes, et_ts, fake_bars, load_skill_module,  # noqa: E402
                     random_walk_history, random_walk_session, run_cli, skill_script, tc, trading_days,
                     write_fixture, yahoo_chart_url, yahoo_payload)

tm = load_skill_module("time-machine", "time_machine")
SCRIPT = skill_script("time-machine", "time_machine.py")

UP = (1.004, 1.005, 1.0)        # every bar closes +0.4%, never dips below the previous close
DOWN = (0.996, 1.0, 0.995)      # mirror image
FLAT = (1.0, 1.0, 1.0)


def const_library(shape, sessions=5, slots=tm.SLOTS):
    return [[shape] * slots for _ in range(sessions)]


def rw_sessions(end="2026-10-29", n=59, vol=0.002, seed=1, n_bars=78):
    return list(tc.group_sessions(random_walk_history(end, n, vol=vol, seed=seed, n_bars=n_bars)).values())


def bar(day, hhmm, o, h, l, c, v=1000):
    return tc.Bar(et_ts(day, hhmm), o, h, l, c, v)


class SlotAndLibraryTests(unittest.TestCase):
    def test_slot_of(self):
        self.assertEqual(tm.slot_of(et_ts("2026-10-29", "09:30")), 0)
        self.assertEqual(tm.slot_of(et_ts("2026-10-29", "10:15")), 9)
        self.assertEqual(tm.slot_of(et_ts("2026-10-29", "10:15") + 163), 9)   # live bar stamped 10:17:43
        self.assertEqual(tm.slot_of(et_ts("2026-10-29", "15:55")), 77)
        self.assertIsNone(tm.slot_of(et_ts("2026-10-29", "16:00")))
        self.assertIsNone(tm.slot_of(et_ts("2026-10-29", "09:25")))
        self.assertEqual(tm.slot_time(9), "10:15")
        self.assertEqual(tm.slot_time(78), "16:00")

    def test_shapes_relative_to_previous_close(self):
        bars = bars_from_closes("2026-10-29", [100, 102, 101], wick=0.5)
        lib = tm.build_library([bars])
        self.assertEqual(len(lib), 1)
        row = lib[0]
        self.assertEqual(len(row), 78)
        # first bar is measured from its own open (no overnight gap)
        for got, want in zip(row[0], (1.0, 1.005, 0.995)):
            self.assertAlmostEqual(got, want)
        for got, want in zip(row[1], (1.02, 1.025, 0.995)):
            self.assertAlmostEqual(got, want)
        for got, want in zip(row[2], (101 / 102, 102.5 / 102, 100.5 / 102)):
            self.assertAlmostEqual(got, want)
        self.assertTrue(all(s is None for s in row[3:]))

    def test_gap_inside_session_is_flat_and_late_open_is_missing(self):
        day = "2026-10-29"
        bars = [bar(day, "09:45", 50, 51, 49, 50), bar(day, "09:50", 50, 50.5, 49.5, 50),
                bar(day, "10:00", 52, 53, 51, 52)]                 # 09:55 missing: no trades
        row = tm.build_library([bars])[0]
        self.assertEqual(row[:3], [None, None, None])               # before the first bar
        self.assertEqual(row[5], (1.0, 1.0, 1.0))                   # 09:55 -> flat
        self.assertAlmostEqual(row[6][0], 52 / 50)                  # 10:00 relative to the 09:50 close
        self.assertAlmostEqual(row[6][1], 53 / 50)
        self.assertIsNone(row[7])

    def test_duplicate_live_bar_later_copy_wins(self):
        day = "2026-10-29"
        bars = [bar(day, "09:30", 10, 10, 10, 10), bar(day, "09:35", 10, 11, 10, 10.5),
                tc.Bar(et_ts(day, "09:35") + 130, 10, 12, 9, 11, 500)]
        row = tm.build_library([bars])[0]
        self.assertAlmostEqual(row[1][0], 1.1)
        self.assertAlmostEqual(row[1][1], 1.2)

    def test_live_row_in_the_same_slot_is_merged_not_replacing_the_bar(self):
        # Yahoo can append the live bar as a separate row stamped with the last trade time that only
        # carries the latest price. The slot must keep the real bar's open, high and low.
        day = "2026-10-29"
        bars = [bar(day, "09:30", 100, 100.5, 99.5, 100), bar(day, "09:35", 100, 103, 97, 101),
                tc.Bar(et_ts(day, "09:35") + 163, 101, 101, 101, 101, 0)]
        b = tm.slot_bars(bars)[1]
        self.assertEqual((b.open, b.high, b.low, b.close), (100, 103, 97, 101))
        row = tm.build_library([bars])[0]
        self.assertAlmostEqual(row[1][1], 1.03)
        self.assertAlmostEqual(row[1][2], 0.97)
        # the history check sees the 97 low: a -2% stop from the 09:35 entry (100) is hit
        self.assertEqual(tm.replay_history([bars], 1, -2.0, 5.0, "long")["stop"], 1)

    def test_half_day_rows_end_early(self):
        half = random_walk_session("2026-11-27", 100, n_bars=42)
        row = tm.build_library([half])[0]
        self.assertTrue(all(s is not None for s in row[:42]))
        self.assertTrue(all(s is None for s in row[42:]))

    def test_invert_and_detrend(self):
        inv = tm.invert_library([[(1.02, 1.05, 0.98)]])
        for got, want in zip(inv[0][0], (1 / 1.02, 1 / 0.98, 1 / 1.05)):
            self.assertAlmostEqual(got, want)
        flat = tm.detrend_library([[(1.02, 1.03, 1.0)], [(1.00, 1.01, 0.99)], [None]])
        self.assertAlmostEqual((flat[0][0][0] + flat[1][0][0]) / 2, 1.0)
        self.assertAlmostEqual(flat[0][0][1] / flat[0][0][0], 1.03 / 1.02)   # bar shape kept
        self.assertIsNone(flat[2][0])

    def test_library_trend(self):
        lib = [[(1.01, 1.01, 1.0)] * 2 + [None] * 76, [(0.99, 1.0, 0.99)] * 2 + [None] * 76]
        t = tm.library_trend(lib, 0, 1)
        self.assertEqual(t["sessions"], 2)
        self.assertAlmostEqual(t["avg_move_pct"], ((1.01 ** 2 - 1) + (0.99 ** 2 - 1)) / 2 * 100, places=3)
        self.assertEqual(tm.library_trend(const_library(UP), 0, 77)["t_stat"], 0.0)   # no spread

    def test_split_pct_sums_to_100(self):
        for counts in ([1, 1, 1], [3333, 3333, 3334], [0, 0, 7], [2941, 2553, 4506]):
            for digits in (1, 2):
                vals = tm.split_pct(counts, sum(counts), digits)
                self.assertAlmostEqual(sum(vals), 100.0, places=6)
        self.assertEqual(tm.split_pct([1, 1, 1], 3, 1), [33.4, 33.3, 33.3])
        self.assertEqual(tm.split_pct([0, 0], 0), [0.0, 0.0])


class SimulateTests(unittest.TestCase):
    def sim(self, lib, start=0, entry=100.0, stop=99.0, target=101.0, side="long", n=2000, seed=1, **kw):
        return tm.simulate(lib, start, entry, stop, target, side, n, random.Random(seed), **kw)

    def test_every_path_hits_target_when_every_bar_rises(self):
        r = self.sim(const_library(UP))
        self.assertEqual(r["target_first_pct"], 100.0)
        self.assertEqual(r["stop_first_pct"], 0.0)
        self.assertEqual(r["counts"]["target"], 2000)
        # +0.4% per bar with 0.5% wicks: high 1.005, 1.00902, 1.01306 -> hit in the 3rd bar
        self.assertEqual(r["median_minutes_to_target"], 15)
        self.assertAlmostEqual(r["expected_r"], 1.0)
        self.assertEqual(tm.verdict(r), "ODDS LEAN YOUR WAY")

    def test_falling_bars_stop_a_long_and_pay_a_short(self):
        self.assertEqual(self.sim(const_library(DOWN))["stop_first_pct"], 100.0)
        short = self.sim(const_library(DOWN), entry=100, stop=101, target=98, side="short")
        self.assertEqual(short["target_first_pct"], 100.0)
        self.assertAlmostEqual(short["expected_r"], 2.0)
        up_short = self.sim(const_library(UP), entry=100, stop=101, target=98, side="short")
        self.assertEqual(up_short["stop_first_pct"], 100.0)
        self.assertAlmostEqual(up_short["expected_r"], -1.0)
        self.assertEqual(tm.verdict(up_short), "ODDS LEAN AGAINST YOU")

    def test_short_equals_long_on_the_mirrored_library(self):
        lib = tm.build_library(rw_sessions(n=20))
        short = self.sim(lib, 30, 100.0, 101.0, 98.0, "short", 3000, seed=9)
        long_ = self.sim(tm.invert_library(lib), 30, 100.0, 100.0 * 100 / 101, 100.0 * 100 / 98, "long", 3000,
                         seed=9)
        self.assertEqual(short["counts"], long_["counts"])

    def test_both_levels_in_one_bar_counts_as_stop(self):
        r = self.sim(const_library((1.0, 1.02, 0.98)))
        self.assertEqual(r["stop_first_pct"], 100.0)
        self.assertEqual(r["ambiguous_pct"], 100.0)
        self.assertEqual(r["counts"]["ambiguous"], 2000)
        self.assertEqual(r["median_minutes_to_stop"], 5)
        short = self.sim(const_library((1.0, 1.02, 0.98)), entry=100, stop=101, target=99, side="short")
        self.assertEqual((short["stop_first_pct"], short["ambiguous_pct"]), (100.0, 100.0))

    def test_exact_touch_counts(self):
        self.assertEqual(self.sim(const_library((1.0, 1.01, 1.0)))["target_first_pct"], 100.0)
        self.assertEqual(self.sim(const_library((1.0, 1.0, 0.99)))["stop_first_pct"], 100.0)

    def test_open_at_close_r(self):
        flat = self.sim(const_library(FLAT), n=500)
        self.assertEqual(flat["open_at_close_pct"], 100.0)
        self.assertEqual(flat["avg_r_open"], 0.0)
        self.assertEqual(flat["expected_r"], 0.0)
        self.assertIsNone(flat["median_minutes_to_resolution"])
        self.assertIsNone(flat["resolved_target_share_pct"])
        self.assertEqual(tm.verdict(flat), "CLOSE TO A COIN FLIP")
        drift = self.sim(const_library((1.001, 1.001, 1.001)), start=68, target=110.0, n=200)
        self.assertEqual(drift["open_at_close_pct"], 100.0)
        self.assertEqual(drift["minutes_simulated"], 50)
        self.assertAlmostEqual(drift["avg_r_open"], (1.001 ** 10 - 1) / 0.01, places=3)
        short = self.sim(const_library((1.001, 1.001, 1.001)), start=68, entry=100, stop=102, target=90,
                         side="short", n=200)
        self.assertAlmostEqual(short["avg_r_open"], -(1.001 ** 10 - 1) / 0.02, places=3)

    def test_percentages_sum_to_100_and_counts_add_up(self):
        lib = tm.build_library(rw_sessions(n=30, seed=4))
        for start in (0, 20, 60):
            r = self.sim(lib, start, 100.0, 99.6, 100.5, n=3000, seed=start)
            self.assertAlmostEqual(r["target_first_pct"] + r["stop_first_pct"] + r["open_at_close_pct"], 100.0)
            c = r["counts"]
            self.assertEqual(c["target"] + c["stop"] + c["open"], 3000)
            self.assertLessEqual(c["ambiguous"], c["stop"])
            p = r["target_first_pct"] / 100
            self.assertAlmostEqual(r["target_first_se"], 100 * math.sqrt(p * (1 - p) / 3000), places=1)
            ev = (c["target"] * r["rr"] - c["stop"] + (c["open"] * r["avg_r_open"] if c["open"] else 0)) / 3000
            self.assertAlmostEqual(r["expected_r"], ev, places=2)

    def test_seed_reproducibility(self):
        lib = tm.build_library(rw_sessions(n=20, seed=2))
        a = self.sim(lib, 10, n=3000, seed=42)
        b = self.sim(lib, 10, n=3000, seed=42)
        c = self.sim(lib, 10, n=3000, seed=43)
        self.assertEqual(a, b)
        self.assertNotEqual(a["counts"], c["counts"])
        self.assertEqual(tm.default_seed("tsla", "2026-10-30"), tm.default_seed("TSLA", "2026-10-30"))
        self.assertNotEqual(tm.default_seed("TSLA", "2026-10-30"), tm.default_seed("TSLA", "2026-10-29"))

    def test_recorded_paths_do_not_change_the_odds(self):
        lib = tm.build_library(rw_sessions(n=20, seed=3))
        plain = self.sim(lib, 9, n=1000, seed=5)
        drawn = self.sim(lib, 9, n=1000, seed=5, keep_paths=300)
        self.assertEqual(plain["counts"], drawn["counts"])
        self.assertEqual(plain["sample_paths"], [])
        self.assertEqual(len(drawn["sample_paths"]), 300)
        steps = 77 - 9 + 1
        for s in drawn["sample_paths"]:
            self.assertEqual(len(s["prices"]), steps + 1)          # entry + every close to the end
            self.assertEqual(s["prices"][0], 100.0)
        outcomes = {s["outcome"] for s in drawn["sample_paths"]}
        self.assertTrue(outcomes <= {"target", "stop", "open"})

    def test_half_day_sessions_are_not_used_after_their_close(self):
        full = [[FLAT] * 78 for _ in range(3)]
        half = [[UP] * 42 + [None] * 36 for _ in range(3)]
        # from 14:00 only the full (flat) sessions exist -> nobody hits anything
        r = self.sim(full + half, start=54, target=100.5)
        self.assertEqual(r["sessions_at_start"], 3)
        self.assertEqual(r["open_at_close_pct"], 100.0)
        # starting late in the half days' morning: blocks that run past 12:55 switch to full sessions
        r = self.sim(full + half, start=40, target=150.0, stop=50.0, n=500, block=6)
        self.assertEqual(r["end_slot"], 77)
        self.assertEqual(r["counts"]["open"], 500)
        self.assertLessEqual(r["avg_r_open"], (1.004 ** 2 - 1) / 0.5 + 1e-9)
        # a library of only half days cannot replay an afternoon start ...
        with self.assertRaises(ValueError):
            self.sim(half, start=54)
        # ... and a morning start stops at their last bar
        r = self.sim(half, start=30, target=150.0, stop=50.0, n=50)
        self.assertTrue(r["end_capped"])
        self.assertEqual((r["end_slot"], r["end"]), (41, "13:00"))

    def test_early_close_end_slot(self):
        r = self.sim(const_library(FLAT), start=38, end_slot=41, n=100)
        self.assertEqual((r["minutes_simulated"], r["end"], r["end_capped"]), (20, "13:00", False))

    def test_start_slot_near_the_close(self):
        lib = tm.build_library(rw_sessions(n=20, seed=6))
        r = self.sim(lib, 77, n=2000)
        self.assertEqual(r["minutes_simulated"], 5)
        self.assertEqual((r["start"], r["end"]), ("15:55", "16:00"))
        self.assertGreater(r["open_at_close_pct"], 95.0)            # a 1% move in 5 minutes is rare
        if r["median_minutes_to_resolution"] is not None:
            self.assertEqual(r["median_minutes_to_resolution"], 5)
        with self.assertRaises(ValueError):
            self.sim(lib, 78)

    def test_validation(self):
        lib = const_library(FLAT)
        for bad in (dict(stop=101.0), dict(target=99.0), dict(stop=-1.0), dict(target=None), dict(n=0),
                    dict(block=0)):
            with self.assertRaises(ValueError, msg=bad):
                self.sim(lib, **bad)
        with self.assertRaises(ValueError):
            self.sim([], 0)

    def test_performance_budget(self):
        lib = tm.build_library(rw_sessions(n=59, seed=7))
        t0 = time.perf_counter()
        r = self.sim(lib, 0, 100.0, 50.0, 200.0, n=10_000, keep_paths=300)   # nothing resolves: worst case
        elapsed = time.perf_counter() - t0
        self.assertEqual(r["counts"]["open"], 10_000)
        self.assertLess(elapsed, 5.0)

    def test_zero_drift_symmetric_levels_give_symmetric_odds(self):
        # A library with zero realized drift (each random-walk session plus its mirror image) and
        # log-symmetric 1:1 levels: target-first and stop-first must agree within a few points.
        base = tm.build_library(rw_sessions(n=59, vol=0.002, seed=11))
        lib = base + tm.invert_library(base)
        r = self.sim(lib, 0, 100.0, 100 / 1.01, 101.0, n=10_000, seed=3)
        self.assertLess(abs(r["target_first_pct"] - r["stop_first_pct"]), 3.0)
        self.assertLess(abs(r["expected_r"]), 0.06)
        # Plain zero-drift random walks: the trend-removed replays are symmetric whatever the
        # sample's realized drift happened to be.
        for seed in (0, 5, 8):
            flat = tm.detrend_library(tm.build_library(rw_sessions(n=59, vol=0.002, seed=seed)))
            r = self.sim(flat, 0, 100.0, 99.0, 101.0, n=10_000, seed=seed)
            self.assertLess(abs(r["target_first_pct"] - r["stop_first_pct"]), 3.0, seed)


class HistoryTests(unittest.TestCase):
    DAYS = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]

    def sessions(self):
        d = self.DAYS
        return {
            # entry at 09:40 = close of the 09:35 bar = 100 in every session; stop -1%, target +2%
            d[0]: bars_from_closes(d[0], [100, 100, 101, 102.5]),                      # target 10:00 bar
            d[1]: bars_from_closes(d[1], [100, 100, 99.5, 98.9, 103]),                 # stop first
            d[2]: bars_from_closes(d[2], [100, 100, 100.5, 101]),                      # open, +1% = +1R
            d[3]: bars_from_closes(d[3], [100, 100, 100], wick=2.5)[:2]
            + [bar(d[3], "09:40", 100, 103, 98, 100)],                                 # both in one bar
            d[4]: bars_from_closes(d[4], [100]),                                       # no bars after 09:35
        }

    def test_outcomes_counts_and_r(self):
        h = tm.replay_history(self.sessions(), 2, -1.0, 2.0, "long")
        self.assertEqual((h["sessions"], h["target"], h["stop"], h["open"], h["ambiguous"], h["skipped"]),
                         (4, 1, 2, 1, 1, 1))
        by_day = {r["date"]: r for r in h["results"]}
        self.assertEqual(by_day[self.DAYS[0]]["outcome"], "target")
        self.assertEqual(by_day[self.DAYS[0]]["at"], "09:50")
        self.assertEqual(by_day[self.DAYS[0]]["r"], 2.0)
        self.assertEqual(by_day[self.DAYS[1]]["outcome"], "stop")
        self.assertEqual(by_day[self.DAYS[1]]["at"], "09:50")
        self.assertEqual(by_day[self.DAYS[2]]["outcome"], "open")
        self.assertAlmostEqual(by_day[self.DAYS[2]]["r"], 1.0)
        self.assertEqual(by_day[self.DAYS[3]]["outcome"], "stop")
        self.assertEqual(h["avg_r_open"], 1.0)
        self.assertAlmostEqual(h["expected_r"], (2 - 1 - 1 + 1) / 4)
        self.assertAlmostEqual(h["target_pct"] + h["stop_pct"] + h["open_pct"], 100.0)

    def test_entry_at_the_open_and_short_side(self):
        d = self.DAYS[0]
        s = {d: [bar(d, "09:30", 50, 50.2, 49.0, 49.1)]}
        h = tm.replay_history(s, 0, 1.0, -2.0, "short")          # entry 50 (the open), stop 50.5, target 49
        self.assertEqual(h["results"][0]["entry"], 50.0)
        self.assertEqual(h["results"][0]["outcome"], "target")
        lst = tm.replay_history([s[d]], 0, -3.0, 2.0, "long")      # a plain list of sessions works too
        self.assertEqual(lst["results"][0]["date"], d)
        self.assertEqual(lst["results"][0]["outcome"], "open")
        self.assertAlmostEqual(lst["results"][0]["r"], (49.1 / 50 - 1) / 0.03, places=3)
        self.assertEqual(tm.replay_history([s[d]], 0, -1.0, 2.0, "long")["stop"], 1)   # low 49.0 <= 49.5
        with self.assertRaises(ValueError):
            tm.replay_history(s, 0, -1.0, 2.0, "short")             # stop below entry for a short

    def test_late_open_day_is_skipped_not_entered_hours_later(self):
        # A day that only started trading at 11:00 (halted at the open, or the partial first day of
        # Yahoo's window) cannot say what a 10:15 entry did - like the replays, skip it.
        d = self.DAYS[0]
        late = bars_from_closes(d, [100, 103, 104], start="11:00")
        h = tm.replay_history({d: late}, 9, -1.0, 2.0, "long")
        self.assertEqual((h["sessions"], h["skipped"]), (0, 1))
        self.assertEqual(h["results"], [])
        # a day that opens exactly at the start slot is entered at that bar's open
        on_time = bars_from_closes(d, [100, 103], start="10:15")
        h = tm.replay_history({d: on_time}, 9, -1.0, 2.0, "long")
        self.assertEqual((h["sessions"], h["target"], h["results"][0]["entry"]), (1, 1, 100.0))


class VerdictTests(unittest.TestCase):
    def r(self, er, t, s, o=None, rr=1.0, share=None):
        o = 100 - t - s if o is None else o
        return {"expected_r": er, "target_first_pct": t, "stop_first_pct": s, "open_at_close_pct": o, "rr": rr,
                "breakeven_win_rate_pct": round(100 / (1 + rr), 2), "resolved_target_share_pct": share}

    def test_thresholds(self):
        self.assertEqual(tm.verdict(self.r(0.2, 40, 30)), "ODDS LEAN YOUR WAY")
        self.assertEqual(tm.verdict(self.r(0.2, 20, 30, rr=3)), "CLOSE TO A COIN FLIP")   # EV from R:R only
        self.assertEqual(tm.verdict(self.r(-0.2, 40, 30)), "ODDS LEAN AGAINST YOU")
        self.assertEqual(tm.verdict(self.r(0.1, 40, 30)), "CLOSE TO A COIN FLIP")
        self.assertEqual(tm.verdict(self.r(-0.1, 30, 40)), "CLOSE TO A COIN FLIP")

    def test_notes(self):
        notes = tm.verdict_notes(self.r(0.04, 29.4, 25.5, rr=1.0, share=53.55))
        self.assertIn("more than 50.0% of resolved trades", notes[0])
        self.assertIn("53.5%", notes[0])
        rr_edge = tm.verdict_notes(self.r(0.2, 20, 30, rr=3, share=40.0))
        self.assertTrue(any("comes from the reward:risk" in n for n in rr_edge))
        res = dict(self.r(0.3, 60, 38, share=61.2), side="long")
        res["trend"] = self.trend(0.42, 0.26, 1.9, alt=(49.1, 48.3, 2.6, 0.0, "CLOSE TO A COIN FLIP"))
        self.assertTrue(any(n.startswith("this verdict leans on a time-of-day drift in the past sessions, +0.42% "
                                         "on average between 10:15 and 11:00, the median time to the stop or "
                                         "target: 0.26R in favour of this long (t=1.9, within noise)")
                            for n in tm.verdict_notes(res)), tm.verdict_notes(res))
        # same verdict either way, but the expected value moves a lot -> still mentioned
        res = dict(self.r(0.11, 36.9, 63.1, rr=2.0, share=36.9), side="long")
        res["trend"] = self.trend(0.40, 0.25, 1.7, alt=(33.2, 66.8, 0.0, -0.01, "CLOSE TO A COIN FLIP"))
        self.assertTrue(any(n.startswith("part of this result is a time-of-day drift")
                            for n in tm.verdict_notes(res)))
        res["trend"]["alt"].update(expected_r=0.09, target_first_pct=36.0)    # immaterial -> no note
        self.assertFalse(any("drift" in n for n in tm.verdict_notes(res)))

    @staticmethod
    def trend(drift_pct, drift_r, t, alt, detrended=False, avg=None, avg_t=None):
        keys = ("target_first_pct", "stop_first_pct", "open_at_close_pct", "expected_r", "verdict")
        return {"avg_move_pct": drift_pct if avg is None else avg, "t_stat": t if avg_t is None else avg_t,
                "horizon": {"start": "10:15", "end": "11:00", "end_slot": 17, "minutes": 45,
                            "basis": "median_resolution"},
                "drift_pct": drift_pct, "drift_r": drift_r, "horizon_avg_move_pct": drift_pct,
                "horizon_t_stat": t, "detrended": detrended, "alt": dict(zip(keys, alt))}

    def test_trend_note_never_contradicts_itself(self):
        # The critic's case: the whole window moved +0.00% (t=0.0), yet removing the drift flips
        # +0.10R to -0.11R because of drift in the first bars: +0.11% by the median exit = 0.23R of a
        # 0.47% stop. The note must quote that, not the whole-window +0.00%.
        res = dict(self.r(0.104, 36.7, 63.2, o=0.1, rr=2.0, share=36.8), side="long")
        res["trend"] = self.trend(0.109, 0.232, 1.5, alt=(29.57, 70.42, 0.01, -0.113, "ODDS LEAN AGAINST YOU"),
                                  avg=0.0, avg_t=0.0)
        note = [n for n in tm.verdict_notes(res) if "drift" in n]
        self.assertEqual(len(note), 1, tm.verdict_notes(res))
        self.assertIn("leans on a time-of-day drift in the past sessions, +0.11% on average between 10:15 and "
                      "11:00, the median time to the stop or target: 0.23R in favour of this long", note[0])
        self.assertIn("-0.11R, ODDS LEAN AGAINST YOU", note[0])
        self.assertNotIn("+0.00%", note[0])
        self.assertNotIn("recent trend", note[0])
        # --detrend headline: the comparison names the same drift.
        flat = dict(self.r(-0.113, 29.57, 70.42, o=0.01, rr=2.0, share=29.6), side="long",
                    verdict="ODDS LEAN AGAINST YOU")
        flat["trend"] = self.trend(0.109, 0.232, 1.5, alt=(36.7, 63.2, 0.1, 0.104, "CLOSE TO A COIN FLIP"),
                                   detrended=True, avg=0.0, avg_t=0.0)
        note = [n for n in tm.verdict_notes(flat) if "drift" in n]
        self.assertTrue(note[0].startswith("with the time-of-day drift left in, the replays give 36.7% target / "
                                           "63.2% stop, +0.10R, CLOSE TO A COIN FLIP; that drift is +0.11%"), note)
        # A drift too small to matter, or pointing the other way, is never named as the cause.
        for drift_r in (0.01, -0.3):
            res["trend"] = self.trend(0.005 if drift_r > 0 else -0.14, drift_r, 0.1,
                                      alt=(29.57, 70.42, 0.01, -0.113, "ODDS LEAN AGAINST YOU"))
            note = [n for n in tm.verdict_notes(res) if "drift" in n]
            self.assertTrue(note[0].startswith("this verdict is sensitive to small time-of-day drifts in the past "
                                               "sessions, though they add up to only"), note)
            self.assertNotIn("leans on", note[0])
        # A short gains from a falling drift: the R is signed for the trade.
        res["side"] = "short"
        res["trend"] = self.trend(-0.2, 0.4, -2.5, alt=(29.57, 70.42, 0.01, -0.113, "ODDS LEAN AGAINST YOU"))
        note = [n for n in tm.verdict_notes(res) if "drift" in n]
        self.assertIn("-0.20% on average between 10:15 and 11:00, the median time to the stop or target: 0.40R in "
                      "favour of this short (t=-2.5)", note[0])


class TrendCheckTests(unittest.TestCase):
    MU = 0.0005          # +0.05% per bar for the first FRONT bars, then drifting back down

    def library(self, front=6, sessions=40):
        """Per-slot mean r_close = 1 + MU for the first `front` slots, 1 - MU*front/(78-front) later;
        sessions alternate a +/-0.1% first bar (given back in the last bar) so the slot means are
        exact, the whole day nets out and the sessions still differ."""
        late = -self.MU * front / (tm.SLOTS - front)
        lib = []
        for j in range(sessions):
            sgn = 1 if j % 2 == 0 else -1
            row = []
            for k in range(tm.SLOTS):
                noise = 0.001 * sgn * (1 if k == 0 else -1 if k == tm.LAST_SLOT else 0)
                c = 1 + (self.MU if k < front else late) + noise
                row.append((c, max(c, 1.0) * 1.0005, min(c, 1.0) * 0.9995))
            lib.append(row)
        return lib

    def test_slot_drift_is_what_detrending_removes(self):
        lib = self.library()
        self.assertAlmostEqual(tm.slot_drift(lib, 0, 5), (1 + self.MU) ** 6 - 1)
        self.assertAlmostEqual(tm.slot_drift(lib, 2, 3), (1 + self.MU) ** 2 - 1)
        self.assertAlmostEqual(tm.slot_drift(tm.detrend_library(lib), 0, 5), 0.0)
        self.assertAlmostEqual(tm.slot_drift([[None] * 78], 0, 77), 0.0)

    def test_horizon_runs_to_the_median_exit(self):
        lib = self.library()
        # 0.47% stop below 100, median resolution 30 minutes = 6 bars: 09:30-10:00.
        tr = tm.trend_check(lib, 0, tm.LAST_SLOT, "long", 100.0, 99.53, 30.0)
        self.assertEqual(tr["horizon"], {"start": "09:30", "end": "10:00", "end_slot": 5, "minutes": 30,
                                         "basis": "median_resolution"})
        drift = (1 + self.MU) ** 6 - 1
        self.assertAlmostEqual(tr["drift_pct"], round(drift * 100, 3))
        self.assertAlmostEqual(tr["drift_r"], round(drift / 0.0047, 3))       # ~0.64R of a 0.47% stop
        self.assertLess(abs(tr["avg_move_pct"]), 0.01)                         # whole window: ~nothing
        self.assertGreater(tr["horizon_t_stat"], 2)                           # sessions differ, drift real
        # a part-bar median rounds up to the bar it ends in; a short gets the sign flipped
        tr = tm.trend_check(lib, 2, tm.LAST_SLOT, "short", 100.0, 100.47, 12.5)
        self.assertEqual((tr["horizon"]["start"], tr["horizon"]["end"]), ("09:40", "09:55"))
        self.assertAlmostEqual(tr["drift_r"], round(-((1 + self.MU) ** 3 - 1) / 0.0047, 3))
        # no replay resolved: the whole window, capped at the end slot
        tr = tm.trend_check(lib, 0, 40, "long", 100.0, 99.53, None)
        self.assertEqual((tr["horizon"]["basis"], tr["horizon"]["end"], tr["horizon"]["end_slot"]),
                         ("window", "12:55", 40))
        tr = tm.trend_check(lib, 70, 77, "long", 100.0, 99.53, 600.0)
        self.assertEqual(tr["horizon"]["end"], "16:00")


class StartTests(unittest.TestCase):
    DAY = "2026-10-30"   # a Friday

    def today(self, upto="10:15"):
        n = (tc.parse_hhmm(upto) - tc.MARKET_OPEN) // 5 + 1
        return random_walk_session(self.DAY, 100, n_bars=n)

    def test_market_open_rounds_now_down(self):
        st = tm.choose_start(tc.parse_et(f"{self.DAY}T10:17"), None, self.today("10:15"))
        self.assertEqual((st["start_slot"], st["mode"], st["session"], st["end_slot"]), (9, "now", "today", 77))
        self.assertFalse(st["include_today"])
        self.assertEqual(st["warnings"], [])

    def test_stale_bars_warn(self):
        st = tm.choose_start(tc.parse_et(f"{self.DAY}T11:00"), None, self.today("10:00"))
        self.assertEqual(st["mode"], "now")
        self.assertTrue(any("10:00" in w and "halted" in w for w in st["warnings"]))

    def test_no_bars_today_simulates_full_session(self):
        st = tm.choose_start(tc.parse_et(f"{self.DAY}T10:17"), None, [])
        self.assertEqual((st["start_slot"], st["mode"], st["session"]), (0, "open", "today"))
        self.assertTrue(any("no 5-minute bars from today" in n for n in st["notes"]))

    def test_premarket_after_close_and_weekend(self):
        pre = tm.choose_start(tc.parse_et(f"{self.DAY}T08:00"), None, [])
        self.assertEqual((pre["mode"], pre["session"], pre["include_today"]), ("open", "today", False))
        self.assertIn("market not open yet", pre["notes"][0])
        post = tm.choose_start(tc.parse_et(f"{self.DAY}T17:00"), None, self.today("15:55"))
        self.assertEqual((post["mode"], post["session"], post["include_today"]), ("open", "next", True))
        self.assertIn("market closed - simulating the next session from the open", post["notes"])
        sat = tm.choose_start(tc.parse_et("2026-10-31T11:00"), None, [])
        self.assertEqual((sat["mode"], sat["session"], sat["include_today"]), ("open", "next", False))

    def test_at(self):
        st = tm.choose_start(tc.parse_et(f"{self.DAY}T11:00"), "10:17", self.today("10:55"))
        self.assertEqual((st["start_slot"], st["mode"]), (9, "at"))
        self.assertIn("rounded down to the 10:15", st["notes"][0])
        st = tm.choose_start(tc.parse_et("2026-10-31T11:00"), "14:00", [])
        self.assertEqual((st["start_slot"], st["session"]), (54, "next"))
        for bad in ("16:00", "09:00", "9", "25:00"):
            with self.assertRaises(ValueError, msg=bad):
                tm.choose_start(tc.parse_et(f"{self.DAY}T11:00"), bad, [])

    def test_early_close(self):
        st = tm.choose_start(tc.parse_et("2026-11-27T11:00"), None, self.today("10:55"), 780)
        self.assertEqual((st["end_slot"], st["mode"]), (41, "now"))
        self.assertTrue(any("early close today at 13:00" in n for n in st["notes"]))
        with self.assertRaises(ValueError):
            tm.choose_start(tc.parse_et("2026-11-27T11:00"), "14:00", [], 780)
        after = tm.choose_start(tc.parse_et("2026-11-27T13:30"), None, self.today("12:55"), 780)
        self.assertEqual((after["session"], after["include_today"], after["end_slot"]), ("next", True, 77))

    def test_session_end_minute_from_yahoo_meta(self):
        start, end = et_ts("2026-11-27", "09:30"), et_ts("2026-11-27", "13:00")
        cur = {"currentTradingPeriod": {"regular": {"start": start, "end": end, "timezone": "EST"}}}
        self.assertEqual(tm.session_end_minute(cur, "2026-11-27"), 780)
        self.assertIsNone(tm.session_end_minute(cur, "2026-11-28"))
        periods = {"tradingPeriods": [[{"start": et_ts("2026-11-26", "09:30"), "end": et_ts("2026-11-26", "16:00")}],
                                      [{"start": start, "end": end}]]}
        self.assertEqual(tm.session_end_minute(periods, "2026-11-27"), 780)
        self.assertEqual(tm.session_end_minute({"tradingPeriods": {"regular": periods["tradingPeriods"]}},
                                               "2026-11-26"), 960)
        self.assertIsNone(tm.session_end_minute({"tradingPeriods": [[{"start": "x"}]]}, "2026-11-27"))
        self.assertIsNone(tm.session_end_minute({}, "2026-11-27"))

    def test_today_range_ratio(self):
        past = [bars_from_closes(d, [100, 100.5, 100, 100.5], wick=0) for d in trading_days("2026-10-29", 6)]
        hot = bars_from_closes(self.DAY, [100, 102, 100, 102], wick=0)
        self.assertAlmostEqual(tm.today_range_ratio(hot, past, 4), 4.0)
        self.assertIsNone(tm.today_range_ratio(hot[:2], past, 4))


class SvgTests(unittest.TestCase):
    def test_svg_is_valid_and_complete(self):
        lib = tm.build_library(rw_sessions(n=20, seed=3))
        r = tm.simulate(lib, 9, 100.0, 99.5, 100.6, "long", 2000, random.Random(1), keep_paths=300)
        samples = r.pop("sample_paths")
        r.update(symbol="TSLA", mode="now", verdict=tm.verdict(r),
                 history={"sessions": 20, "target": 7, "stop": 8, "open": 5})
        svg = tm.render_svg(r, samples, random_walk_session("2026-10-30", 100, n_bars=10))
        root = ET.fromstring(svg)
        self.assertTrue(root.tag.endswith("svg"))
        self.assertEqual((root.get("width"), root.get("height")), ("800", "450"))
        ns = "{http://www.w3.org/2000/svg}"
        self.assertEqual(len(root.findall(f".//{ns}polyline")), 300)
        texts = " ".join(t.text or "" for t in root.iter(f"{ns}text"))
        for want in ("TIME MACHINE · TSLA · LONG from 10:15 ET", "HIT TARGET FIRST", "HIT STOP FIRST",
                     "OPEN AT CLOSE", "300 OF 2,000 PATHS DRAWN", "TARGET 100.60", "STOP 99.50", "ENTRY 100.00",
                     "Not financial advice"):
            self.assertIn(want, texts)
        pt, ps, po = tm.split_pct([r["counts"][k] for k in ("target", "stop", "open")], 2000, 1)
        self.assertIn(f"{pt:.1f}%", texts)
        self.assertIn(f"{po:.1f}%", texts)
        strokes = {g.get("stroke") for g in root.iter(f"{ns}g") if g.get("stroke")}
        self.assertIn(tm.COLORS["target"], strokes)


def front_loaded_history(end="2026-10-30", n=60, mu=0.00015, front=8, vol=0.002, seed=3, wick=0.0005):
    """The critic's shape: past sessions that drift up +mu per bar for the first `front` bars and give
    it back over the rest of the day, so the whole window nets out to ~0% while the first 40 minutes -
    where a tight trade is decided - drift up. Sessions come in mirrored-noise pairs (oldest first),
    so over whole pairs each slot's mean move is exactly the drift."""
    rng = random.Random(seed)
    bars = []
    noise = []
    for j, day in enumerate(trading_days(end, n)):
        if j % 2 == 0:
            noise = [rng.gauss(0, vol) for _ in range(tm.SLOTS)]
        sgn = 1 if j % 2 == 0 else -1
        t0, p = et_ts(day, "09:30"), 100.0
        for k in range(tm.SLOTS):
            m = mu if k < front else -mu * front / (tm.SLOTS - front)
            c = p * (1 + m + sgn * noise[k])
            bars.append(tc.Bar(t0 + k * 300, p, max(p, c) * (1 + wick), min(p, c) * (1 - wick), c, 1000))
            p = c
    return bars


def capture(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = tm.main(argv)
    return code, buf.getvalue()


class MainTests(TempHomeTestCase):
    NOW = "2026-10-30T10:17"

    def history(self, end="2026-10-30", n=60, seed=5):
        return random_walk_history(end, n, start_price=364.0, vol=0.002, seed=seed)

    def test_json_shape_and_no_peeking_at_today(self):
        bars = self.history()
        with fake_bars({"TSLA": bars}):
            code, out = capture(["TSLA", "--entry", "364.18", "--stop", "358.32", "--target", "370.04", "--json",
                                 "--paths", "2000"])
        self.assertEqual(code, 0)
        d = json.loads(out)
        for key in ("symbol", "side", "entry", "stop", "target", "rr", "start", "end", "paths", "sessions_used",
                    "target_first_pct", "stop_first_pct", "open_at_close_pct", "ambiguous_pct",
                    "target_first_se", "stop_first_se", "avg_r_open", "expected_r", "breakeven_win_rate_pct",
                    "median_minutes_to_resolution", "history", "trend", "verdict", "verdict_notes", "warnings",
                    "notes", "seed", "as_of", "disclaimer"):
            self.assertIn(key, d)
        self.assertNotIn("sample_paths", d)
        self.assertEqual((d["side"], d["start"], d["mode"], d["session"]), ("long", "10:15", "now", "today"))
        self.assertEqual(d["sessions_used"], 59)
        self.assertEqual(d["last_session"], "2026-10-29")             # today's bars never enter the library
        self.assertEqual(d["history"]["sessions"], 59)
        self.assertAlmostEqual(d["target_first_pct"] + d["stop_first_pct"] + d["open_at_close_pct"], 100.0)
        self.assertIn(d["verdict"], ("ODDS LEAN YOUR WAY", "ODDS LEAN AGAINST YOU", "CLOSE TO A COIN FLIP"))
        self.assertEqual(d["seed"], tm.default_seed("TSLA", "2026-10-30"))
        self.assertEqual(d["levels_source"], {"entry": "flag", "stop": "flag", "target": "flag"})
        self.assertIn("alt", d["trend"])

    def test_replay_date_excludes_later_sessions(self):
        with fake_bars({"TSLA": self.history()}), mock.patch.dict(os.environ, {"DAYTRADER_NOW": "2026-10-20T10:17"}):
            code, out = capture(["TSLA", "--entry", "100", "--stop", "99", "--target", "102", "--json",
                                 "--paths", "500", "--sessions", "10"])
        d = json.loads(out)
        self.assertEqual(d["last_session"], "2026-10-19")
        self.assertEqual(d["sessions_used"], 10)

    def test_levels_from_saved_plan_side_inference_and_save(self):
        tc.save_plan({"symbol": "TSLA", "side": "short", "entry": 364.18, "stop": 368.0, "target": 356.5,
                      "sizing": {"shares": 13}, "exit_plan": {"method": "atr"}})
        with fake_bars({"TSLA": self.history()}):
            code, out = capture(["TSLA", "--json", "--paths", "1000", "--save"])
        d = json.loads(out)
        self.assertEqual((d["side"], d["entry"], d["stop"], d["target"]), ("short", 364.18, 368.0, 356.5))
        self.assertEqual(set(d["levels_source"].values()), {"saved plan"})
        self.assertTrue(d["saved"])
        self.assertEqual(d["dropped_sections"], [])
        plan = tc.load_plan("TSLA")
        tmx = plan["time_machine"]
        for key in ("target_first_pct", "stop_first_pct", "neither_pct", "paths", "sessions", "verdict", "as_of"):
            self.assertIn(key, tmx)
        self.assertEqual(tmx["neither_pct"], d["open_at_close_pct"])
        self.assertEqual((tmx["entry"], tmx["stop"], tmx["target"], tmx["side"]), (364.18, 368.0, 356.5, "short"))
        self.assertEqual(plan["sizing"], {"shares": 13})               # same levels: kept
        # new levels via flags + --save: sections computed for the old levels are dropped
        with fake_bars({"TSLA": self.history()}):
            code, out = capture(["TSLA", "--stop", "369", "--json", "--paths", "500", "--save"])
        d = json.loads(out)
        self.assertEqual(d["levels_source"]["stop"], "flag")
        self.assertEqual(sorted(d["dropped_sections"]), ["exit_plan", "sizing"])
        plan = tc.load_plan("TSLA")
        self.assertNotIn("sizing", plan)
        self.assertEqual(plan["stop"], 369.0)
        self.assertEqual(plan["time_machine"]["stop"], 369.0)

    def test_null_sections_left_by_the_entry_skill_are_not_reported_as_cleared(self):
        # entry --save resets target/exit_plan/time_machine/sizing to null when its levels change
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 100.0, "stop": 99.0, "setup": "orb",
                      "target": None, "exit_plan": None, "time_machine": None, "sizing": None})
        with fake_bars({"TSLA": self.history()}):
            code, out = capture(["TSLA", "--target", "102", "--stop", "98.5", "--json", "--paths", "500",
                                 "--save"])
        d = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(d["dropped_sections"], [])
        self.assertEqual(tc.load_plan("TSLA")["time_machine"]["stop"], 98.5)

    def test_bad_input(self):
        with fake_bars({"TSLA": self.history()}):
            with self.assertRaises(ValueError):                      # no plan, no levels
                capture(["TSLA"])
            with self.assertRaises(ValueError):                      # long stop above entry
                capture(["TSLA", "--side", "long", "--entry", "100", "--stop", "101", "--target", "102"])
            with self.assertRaises(ValueError):                      # stop == entry
                capture(["TSLA", "--entry", "100", "--stop", "100", "--target", "102"])
            with self.assertRaises(ValueError):
                capture(["TSLA", "--entry", "100", "--stop", "99", "--target", "102", "--at", "17:00"])
            with self.assertRaises(ValueError):
                capture(["TSLA", "--entry", "100", "--stop", "99", "--target", "102", "--svg",
                         str(self.home / "no-such-dir" / "x.svg")])
        tc.save_plan({"symbol": "AAPL", "side": "long", "entry": 200, "stop": 198})
        with self.assertRaises(ValueError) as cm:                     # plan without a target
            capture(["AAPL"])
        self.assertIn("--target", str(cm.exception))

    def test_unknown_symbol_and_too_little_history(self):
        def missing(*a, **k):
            raise tc.FetchError("ZZZZ: HTTP 404 for https://query1.finance.yahoo.com/v8/finance/chart/ZZZZ - not found")
        with mock.patch.object(tc, "fetch_bars", side_effect=missing):
            with self.assertRaises(ValueError):
                capture(["ZZZZ", "--entry", "10", "--stop", "9", "--target", "12"])
        with fake_bars({"TSLA": self.history(n=4)}):
            with self.assertRaises(ValueError) as cm:
                capture(["TSLA", "--entry", "10", "--stop", "9", "--target", "12"])
        self.assertIn("need at least 5", str(cm.exception))
        with mock.patch.object(tc, "fetch_bars", side_effect=tc.FetchError("network error for https://x")):
            with self.assertRaises(tc.FetchError):
                capture(["TSLA", "--entry", "10", "--stop", "9", "--target", "12"])

    def test_text_output_and_warnings(self):
        bars = self.history()
        last = [b for b in bars if b.date == "2026-10-30" and b.ts <= et_ts("2026-10-30", "10:15")][-1].close
        with fake_bars({"TSLA": bars}):
            code, out = capture(["TSLA", "--entry", f"{last * 1.10:.2f}", "--stop", f"{last * 1.09:.2f}",
                                 "--target", f"{last * 1.12:.2f}", "--paths", "1000", "--detrend"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("TIME MACHINE · TSLA · LONG from 10:15 ET · 1,000 replays from "
                                            "TSLA's past 59 sessions"), lines[0])
        for want in ("HIT TARGET FIRST", "HIT STOP FIRST", "OPEN AT CLOSE", "Expected value:",
                     "Break-even needs > ", "History check (same clock time, last 59 sessions)",
                     "Trend check:", "with the trend left in", "VERDICT:", "already through your stop",
                     "Not saved", tc.DISCLAIMER):
            self.assertIn(want, out)

    def test_detrend_swaps_headline_and_comparison(self):
        args = ["TSLA", "--entry", "364.18", "--stop", "362", "--target", "368", "--json", "--paths", "2000"]
        with fake_bars({"TSLA": self.history()}):
            raw = json.loads(capture(args)[1])
            flat = json.loads(capture(args + ["--detrend"])[1])
        self.assertFalse(raw["trend"]["detrended"])
        self.assertTrue(flat["trend"]["detrended"])
        for k in ("target_first_pct", "stop_first_pct", "open_at_close_pct", "expected_r"):
            self.assertEqual(raw["trend"]["alt"][k], flat[k])
            self.assertEqual(flat["trend"]["alt"][k], raw[k])
        self.assertEqual(raw["history"], flat["history"])           # real history is never detrended

    def test_past_at_is_not_compared_with_the_current_price(self):
        bars = self.history()
        last = [b for b in bars if b.date == "2026-10-30" and b.ts <= et_ts("2026-10-30", "10:15")][-1].close
        lv = ["--entry", f"{last * 1.10:.2f}", "--stop", f"{last * 1.09:.2f}", "--target", f"{last * 1.12:.2f}"]
        with fake_bars({"TSLA": bars}):
            past = json.loads(capture(["TSLA", *lv, "--at", "09:45", "--json", "--paths", "500"])[1])
            ahead = json.loads(capture(["TSLA", *lv, "--at", "11:00", "--json", "--paths", "500"])[1])
        self.assertEqual((past["mode"], past["start"]), ("at", "09:45"))
        self.assertFalse(any("already through your stop" in w for w in past["warnings"]))
        self.assertTrue(any("already through your stop" in w for w in ahead["warnings"]))

    def test_trend_check_measures_the_drift_a_trade_lives_through(self):
        # Regression (critic): the whole window drifted +0.00% (t=0.0), the trend-removed replays
        # still came out far worse, and the note blamed "the stock's recent trend (+0.00%)". The drift
        # is in the first bars: measured to the median exit it is a real share of a 0.47% stop.
        lv = ["TSLA", "--entry", "100", "--stop", "99.53", "--target", "100.94", "--paths", "4000",
              "--sessions", "58"]                                       # whole mirrored pairs only
        with fake_bars({"TSLA": front_loaded_history()}), \
                mock.patch.dict(os.environ, {"DAYTRADER_NOW": "2026-10-30T19:30"}):
            d = json.loads(capture(lv + ["--json"])[1])
            text = capture(lv)[1]
        tr = d["trend"]
        self.assertLess(abs(tr["avg_move_pct"]), 0.005)                      # the misleading number
        h = tr["horizon"]
        self.assertEqual((h["start"], h["basis"]), ("09:30", "median_resolution"))
        self.assertEqual(h["minutes"], math.ceil(d["median_minutes_to_resolution"] / 5) * 5)
        bars_in = min(h["end_slot"] + 1, 8)
        want = (1.00015 ** bars_in) * (1 - 0.00015 * 8 / 70) ** (h["end_slot"] + 1 - bars_in) - 1
        self.assertAlmostEqual(tr["drift_pct"], want * 100, places=3)
        self.assertAlmostEqual(tr["drift_r"], want / 0.0047, places=2)
        self.assertGreater(tr["drift_r"], 0.1)
        gap = d["expected_r"] - tr["alt"]["expected_r"]
        self.assertGreaterEqual(gap, 0.1)                                    # material: a note is due
        notes = [n for n in d["verdict_notes"] if "drift" in n]
        self.assertEqual(len(notes), 1, d["verdict_notes"])
        self.assertIn(f"{tr['drift_pct']:+.2f}% on average between 09:30 and {h['end']}, the median time to "
                      f"the stop or target: {tr['drift_r']:.2f}R in favour of this long", notes[0])
        self.assertNotIn("+0.00%", notes[0])
        self.assertIn(f"Trend check: from 09:30 to {h['end']} (the median time to stop/target) the past "
                      f"sessions drifted {tr['drift_pct']:+.2f}%", " ".join(text.split()))
        self.assertIn("whole window to 16:00: +0.00% (t=0.0)", " ".join(text.split()))

    def test_display_pct_is_what_the_card_prints_and_is_saved(self):
        self.assertEqual(tm.display_pct({"target": 3166, "stop": 3157, "open": 3677}, 10000),
                         {"target_first": 31.6, "stop_first": 31.6, "open_at_close": 36.8})   # not 31.7 / 36.8
        with fake_bars({"TSLA": self.history()}):
            d = json.loads(capture(["TSLA", "--entry", "364.18", "--stop", "362.5", "--target", "366",
                                    "--paths", "3001", "--json", "--save"])[1])
            text = capture(["TSLA", "--entry", "364.18", "--stop", "362.5", "--target", "366", "--paths", "3001"])[1]
        shown = d["display_pct"]
        c = d["counts"]
        self.assertEqual([shown["target_first"], shown["stop_first"], shown["open_at_close"]],
                         tm.split_pct([c["target"], c["stop"], c["open"]], 3001, 1))
        self.assertAlmostEqual(sum(shown.values()), 100.0, places=9)
        for label, key in (("HIT TARGET FIRST", "target_first"), ("HIT STOP FIRST", "stop_first"),
                           ("OPEN AT CLOSE", "open_at_close")):
            self.assertIn(f"{label:<18} {shown[key]:5.1f}%", text)
        self.assertEqual(tc.load_plan("TSLA")["time_machine"]["display_pct"], shown)

    def test_entry_from_a_finished_session_trigger_is_noted(self):
        note = "trigger is from the finished 2026-10-29 session - re-plan from the next open"
        signal = {"source": "entry", "setup": "orb", "label": "ORB(15)", "date": "2026-10-29",
                  "triggered_at": "10:15", "entry": 364.18, "suggested_stop": 358.32, "session_over": True,
                  "note": note}
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04,
                      "entry_signal": signal})
        want = f"the saved TSLA entry 364.18 is an entry-skill trigger (ORB(15) at 10:15 ET): {note}"
        with fake_bars({"TSLA": self.history()}):
            d = json.loads(capture(["TSLA", "--json", "--paths", "500"])[1])
            self.assertIn(want, d["notes"])
            text = capture(["TSLA", "--paths", "500"])[1]
            self.assertIn("  - the saved TSLA entry 364.18 is an entry-skill trigger", text)
            d = json.loads(capture(["TSLA", "--entry", "364.5", "--json", "--paths", "500"])[1])  # a new entry
            self.assertFalse(any("entry-skill trigger" in n for n in d["notes"]), d["notes"])
        # A trigger saved live today has not played out: no note.
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04,
                      "entry_signal": dict(signal, date="2026-10-30", session_over=False, note=None)})
        self.assertIsNone(tm.finished_trigger_note(tc.load_plan("TSLA"), "TSLA", 364.18, "2026-10-30"))
        self.assertIsNotNone(tm.finished_trigger_note(tc.load_plan("TSLA"), "TSLA", 364.18, "2026-10-31"))

    def test_skill_md_routes_the_desk_run(self):
        text = (SKILLS / "time-machine" / "SKILL.md").read_text(encoding="utf-8")
        desc = " ".join(re.search(r"^description: >-\n(.*?)^---", text, re.M | re.S).group(1).split())
        self.assertIn('"plan a trade on TSLA and test it"', desc)
        self.assertIn('"plan and test a trade"', desc)
        self.assertLessEqual(len(desc), 1024)
        self.assertLess(text.index('## Full desk run ("plan a trade on SYM and test it")'), text.index("## Run"))
        section = re.search(r"^## Full desk run.*?(?=^## )", text, re.M | re.S).group(0)
        cmds = re.findall(r"`python3 scripts/time_machine\.py([^`]*)`", section)
        self.assertEqual(cmds, [" SYM --save"])
        for c in cmds:
            tm.build_parser().parse_args(shlex.split(c))

    def test_market_closed_runs_next_session_and_uses_todays_complete_session(self):
        with fake_bars({"TSLA": self.history()}), mock.patch.dict(os.environ, {"DAYTRADER_NOW": "2026-10-30T18:00"}):
            code, out = capture(["TSLA", "--entry", "100", "--stop", "99", "--target", "102", "--json",
                                 "--paths", "500"])
        d = json.loads(out)
        self.assertEqual((d["mode"], d["session"], d["start"], d["last_session"]), ("open", "next", "09:30",
                                                                                    "2026-10-30"))
        self.assertIn("market closed - simulating the next session from the open", d["notes"])


class CliTests(TempHomeTestCase):
    NOW = "2026-10-30T10:17"

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fixtures"
        bars = random_walk_history("2026-10-30", 60, start_price=364.0, vol=0.002, seed=21)
        write_fixture(self.fx, yahoo_chart_url("TSLA", "5m", "60d"), yahoo_payload(bars, "TSLA"))
        self.env = {"DAYTRADER_HOME": str(self.home / "state"), "DAYTRADER_FIXTURES": str(self.fx),
                    "DAYTRADER_NOW": self.NOW}

    def cli(self, *args, **env):
        return run_cli(SCRIPT, *args, env=dict(self.env, **env))

    def test_text_run(self):
        p = self.cli("TSLA", "--entry", "364.18", "--stop", "358.32", "--target", "370.04")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("TIME MACHINE · TSLA · LONG from 10:15 ET · 10,000 replays", p.stdout)
        self.assertIn("entry 364.18  target 370.04 (+1.61%)  stop 358.32 (-1.61%)  R:R 1.00", p.stdout)
        self.assertRegex(p.stdout, r"HIT TARGET FIRST +\d+\.\d%  ±\d\.\d")
        self.assertIn("VERDICT: ", p.stdout)
        self.assertIn("Not financial advice", p.stdout)

    def test_json_save_svg_and_determinism(self):
        svg = self.home / "out.svg"
        args = ("TSLA", "--entry", "364.18", "--stop", "358.32", "--target", "370.04", "--paths", "3000",
                "--json", "--save", "--svg", str(svg))
        p1 = self.cli(*args)
        self.assertEqual(p1.returncode, 0, p1.stderr)
        d1 = json.loads(p1.stdout)
        self.assertTrue(d1["saved"])
        self.assertEqual(d1["svg"], str(svg.resolve()))
        ET.fromstring(svg.read_text(encoding="utf-8"))
        plan = json.loads((self.home / "state" / "plans" / "TSLA.json").read_text())
        self.assertEqual(plan["time_machine"]["paths"], 3000)
        self.assertEqual(plan["time_machine"]["verdict"], d1["verdict"])
        d2 = json.loads(self.cli(*args).stdout)
        for k in ("counts", "expected_r", "history", "seed", "trend"):
            self.assertEqual(d1[k], d2[k])
        d3 = json.loads(self.cli(*args[:-3], "--seed", "7").stdout)
        self.assertEqual(d3["seed"], 7)
        self.assertNotEqual(d3["counts"], d1["counts"])

    def test_data_error_exits_2_without_traceback(self):
        empty = self.home / "empty"
        empty.mkdir()
        p = self.cli("TSLA", "--entry", "364.18", "--stop", "358.32", "--target", "370.04",
                     DAYTRADER_FIXTURES=str(empty))
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.startswith("data error: "), p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1)
        self.assertNotIn("Traceback", p.stderr)

    def test_bad_input_exits_1(self):
        for args in (("TSLA", "--entry", "364", "--stop", "370", "--target", "380", "--side", "long"),
                     ("TSLA", "--entry", "364", "--stop", "360", "--target", "370", "--paths", "5"),
                     ("TSLA", "--entry", "364", "--stop", "360", "--target", "370", "--at", "8:00"),
                     ("TSLA", "--entry", "abc"),
                     ("TSLA",)):
            p = self.cli(*args)
            self.assertEqual(p.returncode, 1, (args, p.stdout, p.stderr))
            self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
