"""Tests for skills/exit/scripts/exit_plan.py (offline, synthetic data only)."""
import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import sys
import unittest
from decimal import ROUND_CEILING, ROUND_FLOOR
from unittest import mock

# Works both with `unittest discover -s tests` and `python3 -m unittest tests.test_exit`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (ROOT, SKILLS, TempHomeTestCase, bars_from_closes, et_ts, fake_bars,  # noqa: E402
                     load_skill_module, random_walk_history, run_cli, skill_script, tc,
                     trading_days, utc_ts, write_coinbase_fixtures, write_coinbase_products,
                     write_coinbase_quote, write_fixture, yahoo_chart_url, yahoo_payload)

ep = load_skill_module("exit", "exit_plan")
SCRIPT = skill_script("exit", "exit_plan.py")
DAY = "2026-10-30"      # a Friday
PREV = "2026-10-29"


def flat(day, n, price=100.0, wick=0.5, start="09:30"):
    """n bars at a constant price: every true range is exactly 2 * wick."""
    return bars_from_closes(day, [price] * n, start=start, wick=wick)


def mids_bars(day, mids, half=0.5, start="09:30"):
    """Bars with open = close = mid and high/low = mid -/+ half. With mid steps <= half every
    true range is exactly 2 * half, so ATR is exact while the lows/highs move."""
    t0 = et_ts(day, start)
    return [tc.Bar(t0 + i * 300, m, m + half, m - half, m, 100_000) for i, m in enumerate(mids)]


# Lows 97.0 early on (outside a 12-bar window), 98.0 inside it; highs top out at 101.5. ATR = 1.0.
STRUCT_MIDS = [98, 97.5, 97.5, 98, 98.5, 99, 99, 98.5, 99, 99.5, 100, 100.5, 101, 101, 101, 101, 101, 101]


def plan(side, entry, method, bars, **params):
    params.setdefault("today", DAY)
    return ep.make_plan(side, entry, method, bars, params)


class RoundingTests(unittest.TestCase):
    def test_tick(self):
        self.assertEqual(ep.tick(361.835), 361.84)          # half-up like a person, not banker's
        self.assertEqual(ep.tick(0.45125), 0.4513)          # sub-dollar: 4 decimals
        self.assertEqual(ep.tick(0.99996), 1.0)
        self.assertEqual(ep.tick(98.49999999999999, ROUND_FLOOR), 98.5)   # float noise ignored
        self.assertEqual(ep.tick(9.996, ROUND_FLOOR), 9.99)
        self.assertEqual(ep.tick(349.5436, ROUND_CEILING), 349.55)

    def test_fmt_rr(self):
        self.assertEqual([ep.fmt_rr(x) for x in (1.0, 2.0, 1.5, 1.99, 0.333)],
                         ["1.0", "2.0", "1.5", "1.99", "0.33"])


class AtrTests(unittest.TestCase):
    def test_todays_atr_ignores_other_sessions(self):
        bars = flat(PREV, 78, wick=5.0) + flat(DAY, 15, wick=0.5)
        a, info = ep.intraday_atr(bars, DAY)
        self.assertAlmostEqual(a, 1.0)
        self.assertEqual((info["source"], info["bars"], info["today_bars"]), ("today", 15, 15))

    def test_fallback_to_last_14_bars_without_the_overnight_gap(self):
        bars = flat(PREV, 20, price=90.0, wick=0.5) + flat(DAY, 5, price=100.0, wick=1.0)
        a, info = ep.intraday_atr(bars, DAY)
        self.assertAlmostEqual(a, (9 * 1.0 + 5 * 2.0) / 14)   # the 90 -> 100 gap is not a true range
        self.assertEqual((info["source"], info["today_bars"]), ("recent", 5))
        self.assertEqual(info["first"], f"{PREV} 10:25")        # 9 bars before the 11:05 end
        self.assertGreater(sum(tc.true_ranges(bars)[-14:]) / 14, a + 0.5)  # naive TR would count the gap

    def test_only_completed_bars_count(self):
        bars = flat(DAY, 15) + bars_from_closes(DAY, [100.0], start="10:45", wick=10.0)
        now_ts = et_ts(DAY, "10:46")
        a, info = ep.intraday_atr(bars, DAY, now_ts=now_ts)
        self.assertAlmostEqual(a, 1.0)
        self.assertEqual(info["bars"], 15)

    def test_not_enough_bars(self):
        a, info = ep.intraday_atr(flat(DAY, 5), DAY)
        self.assertIsNone(a)
        self.assertEqual(info["bars"], 5)

    def test_bar_hygiene(self):
        pre = bars_from_closes(DAY, [100.0, 100.0], start="09:20")          # pre-market: dropped
        regular = flat(DAY, 3)
        live = tc.Bar(et_ts(DAY, "09:40") + 163, 100, 101, 99, 100.5, 5)   # Yahoo's live copy of 09:40
        got = ep.usable_bars(pre + regular + [live], et_ts(DAY, "09:43"))
        self.assertEqual([b.time for b in got], ["09:30", "09:35", "09:40"])   # re-stamped to the slot
        self.assertEqual(got[-1].close, 100.5)                              # fresher copy kept
        _, info = ep.intraday_atr(got, DAY, now_ts=et_ts(DAY, "09:45"))
        self.assertEqual(info["bars"], 3)                                   # 09:40 bar done at 09:45
        self.assertEqual(len(ep.usable_bars(regular, et_ts(DAY, "09:35"))), 1)  # no future bars


class MakePlanTests(unittest.TestCase):
    def test_atr_long(self):
        r = plan("long", 100, "atr", flat(DAY, 18))
        self.assertEqual((r["stop"], r["target"], r["rr"]), (98.5, 103.0, 2.0))
        self.assertEqual((r["risk_per_share"], r["reward_per_share"]), (1.5, 3.0))
        self.assertEqual((r["stop_pct"], r["target_pct"], r["breakeven_win_rate_pct"]), (1.5, 3.0, 33.3))
        self.assertEqual((r["atr"], r["stop_atr"], r["target_atr"]), (1.0, 1.5, 3.0))
        self.assertEqual(r["management"]["breakeven_at"], 101.5)
        self.assertEqual(r["time_stop"], "15:55")
        self.assertEqual(r["warnings"], [])
        self.assertEqual(r["params"]["atr_mult"], 1.5)

    def test_atr_short_and_multiplier(self):
        r = plan("short", 100, "atr", flat(DAY, 18), atr_mult=2, rr=1.5)
        self.assertEqual((r["stop"], r["target"], r["rr"]), (102.0, 97.0, 1.5))
        self.assertEqual(r["management"]["breakeven_at"], 98.0)
        self.assertEqual(r["breakeven_win_rate_pct"], 40.0)

    def test_structure_long_uses_window_low_minus_buffer(self):
        bars = mids_bars(DAY, STRUCT_MIDS)
        r = plan("long", 101, "structure", bars)
        self.assertEqual(r["method"], "structure")
        self.assertEqual(r["structure"]["level"], 98.0)            # not the older 97.0 low
        self.assertEqual(r["structure"]["at"], f"{DAY} 10:05")
        self.assertEqual((r["stop"], r["target"]), (97.9, 107.2))  # 98 - 0.1 ATR; 101 + 2 x 3.10
        self.assertEqual(r["stop_atr"], 3.1)
        wide = plan("long", 101, "structure", bars, lookback=18)
        self.assertEqual((wide["structure"]["level"], wide["stop"]), (97.0, 96.9))

    def test_structure_short(self):
        r = plan("short", 100, "structure", mids_bars(DAY, STRUCT_MIDS))
        self.assertEqual((r["structure"]["level"], r["stop"], r["target"]), (101.5, 101.6, 96.8))

    def test_structure_on_wrong_side_falls_back_to_atr(self):
        r = plan("long", 97.5, "structure", mids_bars(DAY, STRUCT_MIDS))   # entry below the swing low
        self.assertEqual((r["method"], r["method_requested"]), ("atr", "structure"))
        self.assertEqual(r["stop"], 96.0)
        self.assertTrue(any("ATR stop is used instead" in n for n in r["notes"]), r["notes"])
        s = plan("short", 102, "structure", mids_bars(DAY, STRUCT_MIDS))
        self.assertEqual((s["method"], s["stop"]), ("atr", 103.5))

    def test_structure_includes_forming_bar_and_flags_previous_session(self):
        bars = flat(PREV, 78) + flat(DAY, 3)
        forming = tc.Bar(et_ts(DAY, "09:45"), 100, 100.2, 96.0, 99.0, 1)
        r = plan("long", 99.5, "structure", bars + [forming], now_ts=et_ts(DAY, "09:47"))
        self.assertEqual(r["structure"]["level"], 96.0)
        self.assertEqual(r["structure"]["sessions"], 2)
        self.assertTrue(any("previous session" in n for n in r["notes"]))
        self.assertTrue(any("today has only 3 completed" in n for n in r["notes"]), r["notes"])

    def test_structure_window_entirely_from_the_previous_session_is_flagged(self):
        r = plan("long", 101, "structure", flat(PREV, 78), now_ts=et_ts(DAY, "08:30"))   # pre-market
        self.assertEqual((r["structure"]["level"], r["structure"]["sessions"]), (99.5, 1))
        self.assertTrue(any(f"window is from the previous session ({PREV} 15:00" in n for n in r["notes"]),
                        r["notes"])

    def test_percent(self):
        self.assertEqual(tuple(plan("long", 200, "percent", [], pct=1)[k] for k in ("stop", "target")),
                         (198.0, 204.0))
        self.assertEqual(tuple(plan("short", 200, "percent", [], pct=1)[k] for k in ("stop", "target")),
                         (202.0, 196.0))
        r = plan("long", 100, "percent", [], pct=0.333)
        self.assertEqual((r["stop"], r["target"]), (99.67, 100.66))     # cents
        p = plan("long", 0.4512, "percent", [], pct=2)
        self.assertEqual((p["stop"], p["target"]), (0.4422, 0.4692))     # sub-dollar ticks
        self.assertIsNone(p["atr"])
        self.assertEqual(p["params"], {"atr_mult": None, "pct": 2.0, "lookback": None})

    def test_manual_video_example(self):
        r = plan("long", 364.18, "manual", [], stop=358.32, rr=1)
        self.assertEqual((r["stop"], r["target"], r["rr"]), (358.32, 370.04, 1.0))
        self.assertEqual((r["risk_per_share"], r["breakeven_win_rate_pct"]), (5.86, 50.0))
        self.assertIsNone(r["management"]["breakeven_at"])               # +1R is the target itself
        self.assertIn("no breakeven step", r["management"]["rule"])
        fixed = plan("long", 364.18, "manual", [], stop=358.32, target=370.04)
        self.assertEqual((fixed["rr"], fixed["rr_requested"]), (1.0, None))

    def test_invalid_levels(self):
        cases = [
            (("long", 100, "manual", []), {"stop": 101}, "--side short"),
            (("short", 100, "manual", []), {"stop": 99}, "--side long"),
            (("long", 100, "manual", []), {}, "--stop"),
            (("long", 100, "manual", []), {"stop": 99, "target": 99.5}, "target must be above"),
            (("short", 1.0, "percent", []), {"pct": 60}, "at or below zero"),
            (("long", 100, "percent", []), {"pct": 100}, "below 100"),
            (("long", 100, "percent", []), {"pct": 0.001}, "rounds to the entry"),
            (("long", 100, "atr", flat(DAY, 5)), {}, "needs ATR(14)"),
            (("long", 100, "structure", flat(DAY, 5)), {}, "needs ATR(14)"),
            (("long", 100, "atr", flat(DAY, 20, wick=0)), {}, "ATR(14) is zero"),
            (("long", 100, "atr", flat(DAY, 20)), {"atr_mult": 200}, "at most 20"),
            (("long", 100, "atr", flat(DAY, 20)), {"rr": -1}, "positive"),
            (("long", float("nan"), "percent", []), {}, "positive"),
            (("sideways", 100, "percent", []), {}, "long or short"),
            (("long", 100, "fib", []), {}, "unknown method"),
            (("long", 100, "atr", flat(DAY, 20)), {"time_stop": "16:30"}, "between 09:35 and 16:00"),
            (("long", 100, "structure", flat(DAY, 20)), {"lookback": 0}, "--lookback"),
        ]
        for args, params, msg in cases:
            with self.subTest(args=args[:3], params=params):
                with self.assertRaises(ValueError) as cm:
                    plan(*args, **params)
                self.assertIn(msg, str(cm.exception))

    def test_stop_distance_warnings(self):
        bars = flat(DAY, 18)
        tight = plan("long", 100, "manual", bars, stop=99.7)
        self.assertTrue(any("wicked out" in w for w in tight["warnings"]), tight["warnings"])
        wide = plan("long", 100, "manual", bars, stop=95)
        self.assertTrue(any("5.0 ATR" in w and "very wide" in w for w in wide["warnings"]), wide["warnings"])
        ok = plan("long", 100, "manual", bars, stop=98)
        self.assertEqual(ok["warnings"], [])
        no_atr = plan("long", 100, "manual", flat(DAY, 5), stop=98)
        self.assertIsNone(no_atr["stop_atr"])
        self.assertTrue(any("sanity check was skipped" in n for n in no_atr["notes"]))

    def test_min_reward_risk_warning(self):
        low = plan("long", 100, "percent", [], pct=1, rr=1.5, min_rr=2)
        self.assertTrue(any("below your minimum of 2.0" in w for w in low["warnings"]))
        self.assertEqual(plan("long", 100, "percent", [], pct=1, rr=2, min_rr=2)["warnings"], [])

    def test_target_bigger_than_a_whole_day(self):
        hist = []
        for d in trading_days(DAY, 5)[:-1]:
            hist += flat(d, 78)                   # every past session ranges exactly 1.00
        r = plan("long", 100, "atr", hist + flat(DAY, 18))
        self.assertEqual(r["avg_session_range"], 1.0)
        self.assertTrue(any("unusually big day" in w for w in r["warnings"]), r["warnings"])
        close = plan("long", 100, "manual", hist + flat(DAY, 18), stop=99.7, rr=2)
        self.assertFalse(any("unusually big day" in w for w in close["warnings"]))

    def test_last_price_checks(self):
        bars = flat(DAY, 18)
        through = plan("long", 100, "atr", bars, last_price=98.0)
        self.assertTrue(any("only works as a pending buy at 100.00" in w for w in through["warnings"]))
        short = plan("short", 100, "atr", bars, last_price=102.0)
        self.assertTrue(any("pending short" in w for w in short["warnings"]))
        done = plan("long", 100, "atr", bars, last_price=103.5)
        self.assertTrue(any("move has happened" in w for w in done["warnings"]))
        far = plan("long", 100, "atr", bars, last_price=102.6)
        self.assertTrue(any("2.6 ATR below the last price" in n for n in far["notes"]), far["notes"])
        near = plan("long", 100, "atr", bars, last_price=100.4)
        self.assertEqual((near["warnings"], near["notes"]), ([], []))

    def test_last_price_checks_when_the_market_is_closed(self):
        # Pre-market / after hours the last price is the previous regular close, not where the
        # stock trades now (a gap planner's entry can sit well beyond it).
        bars = flat(DAY, 18)
        gap = plan("long", 100, "atr", bars, last_price=98.0, market_open=False)
        w = " ".join(gap["warnings"])
        self.assertIn("the last close 98.00 is already below the stop 98.50", w)
        self.assertIn("if it is still there at the open", w)
        self.assertNotIn("do not buy at the market", w)
        done = plan("short", 100, "atr", bars, last_price=96.5, market_open=False)
        self.assertTrue(any("last close 96.50" in x and "opening price" in x for x in done["warnings"]),
                        done["warnings"])
        far = plan("long", 100, "atr", bars, last_price=102.6, market_open=False)
        self.assertTrue(any("2.6 ATR below the last close 102.60" in n for n in far["notes"]), far["notes"])


class TimeContextTests(unittest.TestCase):
    def ctx(self, when, ts="15:55", end=None, last=None):
        return ep.time_context(tc.parse_et(when), tc.parse_hhmm(ts), end, last)

    def test_early_close_moves_the_time_stop(self):
        c = self.ctx("2026-11-27T12:40", end=13 * 60)
        self.assertEqual(c["time_stop_min"], 12 * 60 + 55)
        self.assertEqual(c["minutes_left"], 15)
        self.assertTrue(any("early close at 13:00" in n for n in c["notes"]))
        self.assertTrue(any("only 15 minutes" in w for w in c["warnings"]))
        after = self.ctx("2026-11-27T13:30", end=13 * 60)    # plan is for the next (full) session
        self.assertEqual(after["time_stop_min"], 15 * 60 + 55)
        self.assertFalse(after["market_open"])

    def test_past_time_stop_and_market_states(self):
        late = self.ctx(f"{DAY}T15:58")
        self.assertTrue(any("past the 15:55 time stop" in w for w in late["warnings"]))
        self.assertEqual(self.ctx(f"{DAY}T11:00")["minutes_left"], 295)
        self.assertTrue(any("pre-market" in n for n in self.ctx(f"{DAY}T08:00")["notes"]))
        sat = self.ctx("2026-10-31T11:00")
        self.assertFalse(sat["market_open"])
        self.assertTrue(any("market closed" in n for n in sat["notes"]))

    def test_stale_data_warnings(self):
        old = tc.Bar(et_ts(DAY, "10:30"), 1, 1, 1, 1, 1)
        self.assertTrue(any("halted or delayed" in w
                            for w in self.ctx(f"{DAY}T11:00", last=old)["warnings"]))
        fresh = tc.Bar(et_ts(DAY, "10:50"), 1, 1, 1, 1, 1)
        self.assertEqual(self.ctx(f"{DAY}T11:00", last=fresh)["warnings"], [])
        yday = tc.Bar(et_ts(PREV, "15:55"), 1, 1, 1, 1, 1)
        self.assertTrue(any("no 5-minute bars from today" in w
                            for w in self.ctx(f"{DAY}T11:00", last=yday)["warnings"]))

    def test_session_end_from_yahoo_meta(self):
        day = "2026-11-27"
        per = {"timezone": "EST", "start": et_ts(day, "09:30"), "end": et_ts(day, "13:00"), "gmtoffset": -18000}
        self.assertEqual(ep.session_end_minute({"currentTradingPeriod": {"regular": per}}, day), 780)
        self.assertEqual(ep.session_end_minute({"tradingPeriods": [[per]]}, day), 780)
        self.assertEqual(ep.session_end_minute({"tradingPeriods": {"regular": [[per]]}}, day), 780)
        self.assertIsNone(ep.session_end_minute({"tradingPeriods": [[per]]}, "2026-11-30"))
        self.assertIsNone(ep.session_end_minute({"currentTradingPeriod": {"regular": {"start": "x"}}}, day))
        self.assertIsNone(ep.session_end_minute({}, day))


class MethodResolutionTests(unittest.TestCase):
    def test_flags_imply_methods(self):
        self.assertEqual(ep.resolve_method(None, []), "atr")
        self.assertEqual(ep.resolve_method(None, ["stop"]), "manual")
        self.assertEqual(ep.resolve_method(None, ["pct"]), "percent")
        self.assertEqual(ep.resolve_method(None, ["lookback", "atr_mult"]), "structure")
        self.assertEqual(ep.resolve_method(None, ["atr_mult"]), "atr")
        self.assertEqual(ep.resolve_method("structure", ["atr_mult"]), "structure")

    def test_conflicts_are_errors_not_ignored(self):
        for method, given in ((None, ["stop", "pct"]), ("atr", ["pct"]), ("percent", ["stop"]),
                              ("manual", ["atr_mult"]), ("atr", ["lookback"])):
            with self.subTest(method=method, given=given), self.assertRaises(ValueError):
                ep.resolve_method(method, given)

    def test_every_documented_command_parses(self):
        text = (SKILLS / "exit" / "SKILL.md").read_text(encoding="utf-8")
        ref = SKILLS / "exit" / "reference.md"
        if ref.exists():
            text += ref.read_text(encoding="utf-8")
        cmds = re.findall(r"python3 scripts/exit_plan\.py([^`\n#]*)", text)
        self.assertGreaterEqual(len(cmds), 5)
        parser = ep.build_parser()
        for c in cmds:
            with self.subTest(cmd=c):
                args = parser.parse_args(shlex.split(c))
                given = [k for k in ("stop", "pct", "lookback", "atr_mult") if getattr(args, k) is not None]
                ep.resolve_method(args.method, given)
                ep.parse_time_stop(args.time_stop or "15:55")


DESK_SCRIPTS = {"scan.py": ("scanner", "scan"), "news.py": ("news", "news"), "entry.py": ("entry", "entry"),
                "exit_plan.py": ("exit", "exit_plan"), "time_machine.py": ("time-machine", "time_machine"),
                "risk.py": ("risk", "risk"), "budget.py": ("budget", "budget"), "broker.py": ("broker", "broker")}


def desk_run_section(skill):
    """The '## Full desk run' section of skills/<skill>/SKILL.md."""
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^## Full desk run.*?(?=^## )", text, re.M | re.S)
    return m.group(0) if m else ""


class DeskRunDocTests(unittest.TestCase):
    """'Plan a trade on TSLA and test it' must route to exit/time-machine and name the whole chain."""

    def test_description_routes_the_headline_prompt(self):
        spec = importlib.util.spec_from_file_location("day_trader_package_skills_exit",
                                                      ROOT / "tools" / "package_skills.py")
        pkg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pkg)
        fm = pkg.parse_frontmatter((SKILLS / "exit" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(pkg.check_frontmatter("exit", fm), [])       # <= 1024 characters, no tags
        self.assertIn('"plan a trade on TSLA and test it"', fm["description"])
        self.assertIn('"plan and test a trade"', fm["description"])

    def test_section_is_near_the_top_and_mirrored_in_time_machine(self):
        sec = desk_run_section("exit")
        self.assertTrue(sec.startswith('## Full desk run ("plan a trade on SYM and test it")'), sec[:80])
        self.assertEqual(sec, desk_run_section("time-machine"))
        text = (SKILLS / "exit" / "SKILL.md").read_text(encoding="utf-8")
        self.assertLess(text.index("## Full desk run"), text.index("## Run"))
        steps = re.findall(r"^\d\. \*\*([a-z-]+)\*\*", sec, re.M)
        self.assertEqual(steps, ["scanner", "news", "entry", "exit", "time-machine", "risk", "budget", "broker"])

    def test_every_desk_run_command_parses_with_the_real_parser(self):
        cmds = re.findall(r"`python3 scripts/(\w+\.py)([^`]*)`", desk_run_section("exit"))
        self.assertEqual(len(cmds), 9)
        self.assertEqual({c[0] for c in cmds}, set(DESK_SCRIPTS))
        for script, argv in cmds:
            skill, module = DESK_SCRIPTS[script]
            with self.subTest(cmd=f"{script}{argv}"):
                mod = load_skill_module(skill, module)
                with contextlib.redirect_stderr(io.StringIO()):
                    args = mod.build_parser().parse_args(shlex.split(argv))
                self.assertFalse(getattr(args, "json", False))
        exit_args = [shlex.split(a) for s, a in cmds if s == "exit_plan.py"]
        for argv in exit_args:                                       # and exit's own flag rules hold
            args = ep.build_parser().parse_args(argv)
            given = [k for k in ("stop", "pct", "lookback", "atr_mult") if getattr(args, k) is not None]
            ep.resolve_method(args.method, given)


class MainTests(TempHomeTestCase):
    NOW = f"{DAY}T11:00"

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ep.main(list(argv) + ["--json"]), 0)
        return json.loads(out.getvalue())

    def bars(self):
        return flat(PREV, 78) + flat(DAY, 78)        # today's bars after 11:00 must be ignored

    def test_entry_and_side_from_saved_plan(self):
        tc.save_plan({"symbol": "TSLA", "side": "short", "entry": 100.0, "stop": 101.0, "setup": "orb"})
        with fake_bars({"TSLA": self.bars()}):
            r = self.run_main("TSLA")
        self.assertEqual((r["side"], r["entry"], r["stop"], r["target"]), ("short", 100.0, 101.5, 97.0))
        self.assertEqual((r["entry_source"], r["side_source"]), ("saved plan", "saved plan"))
        self.assertEqual(r["atr_info"]["bars"], 18)                     # 09:30..10:55 only
        self.assertFalse(r["saved"])
        self.assertEqual(tc.load_plan("TSLA")["stop"], 101.0)           # not saved without --save

    def test_manual_reuses_saved_stop_and_warns_when_stale(self):
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": f"{PREV}T10:00"}):
            tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 100.0, "stop": 98.0})
        with fake_bars({"TSLA": self.bars()}):
            r = self.run_main("TSLA", "--method", "manual", "--rr", "1.5")
        self.assertEqual((r["stop"], r["target"], r["stop_source"]), (98.0, 103.0, "saved plan"))
        self.assertTrue(any(f"plan is from {PREV}" in w for w in r["warnings"]), r["warnings"])

    def test_entry_from_live_quote_or_replay_close(self):
        bars = self.bars()
        with fake_bars({"TSLA": bars}, meta={"regularMarketPrice": 100.25}) as m:
            r = self.run_main("TSLA")
        self.assertEqual((r["entry"], r["entry_source"]), (100.25, "current price"))
        self.assertIn("1m", [c.args[1] for c in m.call_args_list])      # went through tc.quote
        future = {"regularMarketPrice": 120.0, "regularMarketTime": et_ts(DAY, "16:00")}
        with fake_bars({"TSLA": bars}, meta=future):
            r = self.run_main("TSLA")
        self.assertEqual((r["entry"], r["entry_source"]), (100.0, "last 5-minute close"))

    def test_side_inferred_from_manual_stop(self):
        with fake_bars({"TSLA": self.bars()}):
            r = self.run_main("TSLA", "--entry", "100", "--stop", "101")
        self.assertEqual((r["side"], r["side_source"], r["target"]), ("short", "inferred from the stop", 98.0))

    def test_save_merges_and_clears_stale_sections(self):
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 100.0, "stop": 98.5, "target": 103.0,
                      "setup": "orb", "entry_signal": {"status": "TRIGGERED"},
                      "time_machine": {"verdict": "CLOSE TO A COIN FLIP"}, "sizing": {"shares": 33}})
        with fake_bars({"TSLA": self.bars()}):
            same = self.run_main("TSLA", "--save")                     # same levels: keep everything
            self.assertEqual(same["dropped_sections"], [])
            self.assertIn("time_machine", tc.load_plan("TSLA"))
            new_target = self.run_main("TSLA", "--rr", "3", "--save")  # sizing still valid
            self.assertEqual(new_target["dropped_sections"], ["time_machine"])
            self.assertIn("sizing", tc.load_plan("TSLA"))
            new_stop = self.run_main("TSLA", "--pct", "1", "--save")
            self.assertEqual(new_stop["dropped_sections"], ["sizing"])
        saved = tc.load_plan("TSLA")
        self.assertEqual((saved["stop"], saved["target"], saved["setup"]), (99.0, 102.0, "orb"))
        self.assertEqual(saved["entry_signal"], {"status": "TRIGGERED"})
        self.assertNotIn("time_machine", saved)
        self.assertNotIn("sizing", saved)
        xp = saved["exit_plan"]
        for k in ("method", "atr", "rr", "time_stop", "notes"):
            self.assertIn(k, xp)
        self.assertEqual((xp["method"], xp["rr"], xp["time_stop"]), ("percent", 2.0, "15:55"))

    def test_null_sections_left_by_the_entry_skill_are_not_reported_as_cleared(self):
        # entry.py --save resets derived sections to null (keys kept) when its levels change.
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 100.0, "stop": 98.5, "target": None,
                      "setup": "orb", "exit_plan": None, "time_machine": None, "sizing": None})
        with fake_bars({"TSLA": self.bars()}):
            r = self.run_main("TSLA", "--method", "manual", "--save")
        self.assertEqual(r["dropped_sections"], [])
        self.assertEqual((tc.load_plan("TSLA")["target"], tc.load_plan("TSLA")["setup"]), (103.0, "orb"))

    def test_replay_does_not_see_the_rest_of_the_bar_in_progress(self):
        # Replaying 11:02 from a full day of bars: the data's 11:00 bar already holds 11:00-11:05.
        later = [tc.Bar(et_ts(DAY, "11:00"), 100, 100.5, 90.0, 95.0, 1)] + flat(DAY, 10, price=95.0, start="11:05")
        replay = flat(PREV, 78) + flat(DAY, 18) + later
        self.assertEqual(ep.usable_bars(replay, et_ts(DAY, "11:02"))[-1].time, "10:55")
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": f"{DAY}T11:02"}), fake_bars({("TSLA", "5m"): replay}):
            r = self.run_main("TSLA", "--method", "structure")
        self.assertEqual((r["entry"], r["entry_source"], r["last_price"]), (100.0, "last 5-minute close", 100.0))
        self.assertEqual(r["structure"]["level"], 99.5)
        # Live, Yahoo's bar in progress holds only what has traded so far, so it still counts.
        live = flat(PREV, 78) + flat(DAY, 18) + [tc.Bar(et_ts(DAY, "11:00"), 100, 100.2, 99.0, 99.2, 1)]
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": f"{DAY}T11:02"}), fake_bars({("TSLA", "5m"): live}):
            r = self.run_main("TSLA", "--method", "structure", "--entry", "99.2")
        self.assertEqual((r["structure"]["level"], r["last_price"]), (99.0, 99.2))

    def test_side_from_an_old_saved_plan_is_a_warning(self):
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": "2026-10-27T10:00"}):
            tc.save_plan({"symbol": "TSLA", "side": "short", "entry": 250.0, "stop": 252.0})
        with fake_bars({"TSLA": self.bars()}):
            r = self.run_main("TSLA", "--entry", "100")
        self.assertEqual((r["side"], r["side_source"]), ("short", "saved plan"))
        self.assertTrue(any("2026-10-27" in w and "--side" in w for w in r["warnings"]), r["warnings"])
        self.assertFalse(any("taken from the saved" in n for n in r["notes"]), r["notes"])   # not twice
        tc.save_plan({"symbol": "TSLA", "side": "short", "entry": 250.0, "stop": 252.0})   # today's plan
        with fake_bars({"TSLA": self.bars()}):
            r = self.run_main("TSLA", "--entry", "100")
        self.assertFalse(any("--side" in w for w in r["warnings"]), r["warnings"])
        self.assertTrue(any("side short taken from the saved TSLA plan" in n for n in r["notes"]), r["notes"])

    def test_entry_from_a_finished_session_trigger_is_a_warning(self):
        # entry.py --save after the close stores entry_signal.session_over + note: exit repeats it.
        note = f"trigger is from the finished {DAY} session - re-plan from the next open"
        signal = {"source": "entry", "setup": "orb", "label": "ORB(15)", "date": DAY, "triggered_at": "10:15",
                  "entry": 100.0, "suggested_stop": 98.5, "session_over": True, "note": note}
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 100.0, "stop": 98.5, "setup": "orb",
                      "entry_signal": signal})
        want = f"the saved TSLA entry 100.00 is an entry-skill trigger (ORB(15) at 10:15 ET): {note}"
        with fake_bars({"TSLA": self.bars()}):
            r = self.run_main("TSLA", "--method", "manual", "--rr", "1", "--save")
            self.assertIn(want, r["warnings"])
            self.assertEqual(tc.load_plan("TSLA")["entry_signal"]["note"], note)   # kept for time-machine
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ep.main(["TSLA", "--method", "manual"])
            self.assertIn("! the saved TSLA entry 100.00 is an entry-skill trigger", out.getvalue())
            # A new entry from the user is not that trigger: no warning.
            r = self.run_main("TSLA", "--entry", "100.4", "--method", "manual")
            self.assertFalse(any("entry-skill trigger" in w for w in r["warnings"]), r["warnings"])
            # A trigger saved live today is fine...
            tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 100.0, "stop": 98.5,
                          "entry_signal": dict(signal, session_over=False, note=None)})
            r = self.run_main("TSLA", "--method", "manual")
            self.assertFalse(any("entry-skill trigger" in w for w in r["warnings"]), r["warnings"])
            # ...but once its session date is in the past it has played out just the same.
            tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 100.0, "stop": 98.5,
                          "entry_signal": dict(signal, date=PREV, session_over=False, note=None)})
            r = self.run_main("TSLA", "--method", "manual")
        self.assertIn(f"the saved TSLA entry 100.00 is an entry-skill trigger (ORB(15) at 10:15 ET): trigger is "
                      f"from the finished {PREV} session - re-plan from the next open", r["warnings"])

    def test_pre_market_gap_plan_compares_with_the_last_close(self):
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": f"{DAY}T08:30"}), fake_bars({"TSLA": self.bars()}):
            r = self.run_main("TSLA", "--entry", "103")
        self.assertFalse(r["market_open"])
        self.assertTrue(any("the last close 100.00 is already below the stop 101.50" in w
                            for w in r["warnings"]), r["warnings"])
        self.assertFalse(any("do not buy at the market" in w for w in r["warnings"]), r["warnings"])

    def test_skill_md_card_quote_is_what_the_script_prints(self):
        text = (SKILLS / "exit" / "SKILL.md").read_text(encoding="utf-8")
        quoted = re.findall(r"`(THE PLAN [^`]*|ENTRY [\d.]+ / [^`]*)`", text)
        self.assertTrue(quoted)
        out = io.StringIO()
        with fake_bars({"TSLA": self.bars()}), contextlib.redirect_stdout(out):
            ep.main(["TSLA", "--entry", "364.18", "--stop", "358.32", "--rr", "1"])
        lines = out.getvalue().splitlines()
        for q in quoted:
            with self.subTest(quote=q):
                self.assertIn(q, lines)

    def test_data_failures(self):
        down = tc.FetchError("TSLA: HTTP 403 for https://query2.finance.yahoo.com/x - access denied")
        with mock.patch.object(tc, "fetch_bars", side_effect=down):
            with self.assertRaises(tc.FetchError):                      # atr needs bars -> exit 2
                ep.main(["TSLA", "--entry", "100"])
            r = self.run_main("TSLA", "--entry", "100", "--stop", "98")  # manual still plans
        self.assertEqual(r["target"], 104.0)
        self.assertIsNone(r["atr"])
        self.assertTrue(any("query2.finance.yahoo.com" in w for w in r["warnings"]))
        gone = tc.FetchError("ZZZZ: HTTP 404 for https://query1.finance.yahoo.com/x - not found")
        with mock.patch.object(tc, "fetch_bars", side_effect=gone), self.assertRaises(ValueError) as cm:
            ep.main(["ZZZZ", "--entry", "100", "--stop", "98"])
        self.assertIn("check the symbol", str(cm.exception))
        with fake_bars({"TSLA": []}), self.assertRaises(ValueError):
            ep.main(["TSLA", "--entry", "100"])

    def test_corrupt_plan_is_state_error(self):
        (self.home / "plans").mkdir()
        (self.home / "plans" / "TSLA.json").write_text("[1, 2]")
        with self.assertRaises(tc.StateError):
            ep.main(["TSLA", "--entry", "100", "--pct", "1"])

    def test_text_card(self):
        out = io.StringIO()
        with fake_bars({"TSLA": self.bars()}), contextlib.redirect_stdout(out):
            ep.main(["TSLA", "--entry", "100", "--rr", "1"])
        text = out.getvalue()
        self.assertIn("THE PLAN — TSLA LONG · ATR stop (1.5 x ATR)", text)
        self.assertIn("ENTRY 100.00 / STOP 98.50 / TARGET 101.50 / R:R 1.0", text)
        self.assertIn("break-even win rate 50.0%", text)
        self.assertIn("(4h 55m left)", text)
        self.assertIn(tc.DISCLAIMER, text)


class CliTests(TempHomeTestCase):
    NOW = f"{DAY}T11:02"

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fixtures"
        bars = random_walk_history(DAY, 5, start_price=364.0, vol=0.002, seed=11)
        meta = {"currentTradingPeriod": {"regular": {"start": et_ts(DAY, "09:30"), "end": et_ts(DAY, "16:00")}}}
        write_fixture(self.fx, yahoo_chart_url("TSLA", "5m", "5d"), yahoo_payload(bars, "TSLA", **meta))
        self.env = {"DAYTRADER_HOME": str(self.home / "state"), "DAYTRADER_FIXTURES": str(self.fx),
                    "DAYTRADER_NOW": self.NOW}

    def cli(self, *args, **env):
        return run_cli(SCRIPT, *args, env=dict(self.env, **env))

    def test_json_text_and_save(self):
        p = self.cli("TSLA", "--entry", "364.18", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        r = json.loads(p.stdout)
        self.assertEqual((r["symbol"], r["side"], r["entry"], r["method"]), ("TSLA", "long", 364.18, "atr"))
        self.assertLess(r["stop"], 364.18)
        self.assertAlmostEqual(r["target"] - 364.18, 2 * (364.18 - r["stop"]), delta=0.011)
        self.assertEqual((r["atr_info"]["source"], r["atr_info"]["bars"]), ("today", 18))
        self.assertEqual(r["disclaimer"], tc.DISCLAIMER)

        p = self.cli("TSLA", "--entry", "364.18", "--stop", "358.32", "--rr", "1", "--save")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ENTRY 364.18 / STOP 358.32 / TARGET 370.04 / R:R 1.0", p.stdout)
        self.assertIn("Saved to", p.stdout)
        saved = json.loads((self.home / "state" / "plans" / "TSLA.json").read_text())
        self.assertEqual((saved["entry"], saved["stop"], saved["target"]), (364.18, 358.32, 370.04))
        self.assertEqual(saved["exit_plan"]["method"], "manual")

    def test_no_data_is_exit_2_one_line(self):
        empty = self.home / "empty"
        empty.mkdir()
        p = self.cli("TSLA", "--entry", "364.18", DAYTRADER_FIXTURES=str(empty))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertTrue(p.stderr.startswith("data error: "), p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_bad_input_is_exit_1(self):
        for args in (("TSLA", "--entry", "abc"), ("TSLA", "--rr", "2", "--target", "370"),
                     ("TSLA", "--method", "fib"), ("TSLA", "--pct", "1", "--stop", "350"),
                     ("TSLA", "--entry", "364", "--stop", "370", "--side", "long"),
                     ("TSLA", "--time-stop", "17:00"), ("TS LA",), ("TSLA", "--live")):
            with self.subTest(args=args):
                p = self.cli(*args)
                self.assertEqual(p.returncode, 1, (args, p.stdout, p.stderr))
                self.assertNotIn("Traceback", p.stderr)


# ---------------------------------------------------------------------------
# Crypto: 24/7, UTC days, the market's time stop, fees in R
# ---------------------------------------------------------------------------

SAT, FRI = "2026-10-31", "2026-10-30"   # a Saturday: crypto trades the weekend


def utc_flat(day, n, price=100.0, wick=0.5, start="00:00"):
    t0 = utc_ts(day, start)
    return [tc.Bar(t0 + i * 300, price, price + wick, price - wick, price, 12.5) for i in range(n)]


class CryptoPlanTests(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        tc.use_market_for("BTC-USD")          # temp_home switches back to US after each test

    def test_time_stop_is_the_markets(self):
        r = plan("long", 100, "atr", utc_flat(SAT, 18), today=SAT)
        self.assertEqual((r["time_stop"], r["stop"], r["target"]), ("23:55", 98.5, 103.0))
        self.assertEqual(ep.parse_time_stop("23:59"), 1439)
        with self.assertRaises(ValueError) as cm:
            ep.parse_time_stop("00:00")
        self.assertIn("between 00:05 and 23:59 UTC (the UTC day)", str(cm.exception))
        self.assertEqual(plan("long", 100, "percent", [], pct=1, lookback=288, today=SAT)["time_stop"], "23:55")
        tc.set_market(tc.US)
        self.assertEqual(plan("long", 100, "percent", [], pct=1)["time_stop"], "15:55")

    def test_ticks_follow_the_product(self):
        self.assertEqual(ep.tick(94500.123, increment=0.01), 94500.12)
        self.assertEqual(ep.tick(0.1234567, increment=0.00001), 0.12346)
        self.assertEqual(ep.tick(0.000012345678), 0.00001235)               # 4 significant digits
        r = plan("long", 0.00001236, "percent", [], pct=2, today=SAT)
        self.assertEqual((r["stop"], r["target"]), (0.00001211, 0.00001286))
        self.assertGreater(r["risk_per_share"], 0)
        r = plan("long", 0.15234, "percent", [], pct=1, tick=0.00001, today=SAT)
        self.assertEqual((r["entry"], r["stop"], r["target"]), (0.15234, 0.15082, 0.15538))   # 2R of 0.00152
        tc.set_market(tc.US)
        self.assertEqual(ep.tick(0.45125), 0.4513)                          # stocks unchanged

    def test_atr_runs_straight_across_midnight_utc(self):
        # 24/7: the 00:00 bar's true range includes the jump from yesterday's last close.
        bars = utc_flat(FRI, 20, price=90.0, start="22:20") + utc_flat(SAT, 5, price=100.0, wick=1.0)
        a, info = ep.intraday_atr(bars, SAT)
        self.assertEqual((info["source"], info["today_bars"]), ("recent", 5))
        self.assertAlmostEqual(a, (9 * 1.0 + (100 + 1 - 90) + 4 * 2.0) / 14)
        r = plan("long", 100, "manual", bars, stop=98, today=SAT)
        self.assertTrue(any("no completed" in n or "today has only 5" in n for n in r["notes"]), r["notes"])
        self.assertTrue(any(f"({FRI} 23:15 to {SAT} 00:20 UTC)" in n for n in r["notes"]), r["notes"])

    def test_fees_in_r(self):
        tight = plan("long", 100000, "manual", [], stop=99000, rr=2, fee_pct=0.6, today=SAT)
        f = tight["fees"]
        self.assertEqual((f["fee_pct"], f["round_trip_r"]), (0.6, 1.2))     # 0.6% x 2 x 100k / 1000
        self.assertEqual((f["at_stop_r"], f["at_target_r"]), (1.194, 1.212))
        self.assertAlmostEqual(f["breakeven_win_rate_after_fees_pct"], 100 * 2.194 / (0.788 + 2.194), places=1)
        self.assertTrue(any(w.startswith("paper fees (0.6% per fill) cost about 1.20R per round trip") for w in
                            tight["warnings"]), tight["warnings"])
        wide = plan("long", 100000, "manual", [], stop=90000, rr=2, fee_pct=0.6, today=SAT)
        self.assertEqual(wide["fees"]["round_trip_r"], 0.12)
        self.assertFalse(any("paper fees" in w for w in wide["warnings"]))  # the card's Fees row is enough
        losing = plan("long", 100000, "manual", [], stop=99500, rr=1, fee_pct=0.6, today=SAT)
        self.assertIsNone(losing["fees"]["breakeven_win_rate_after_fees_pct"])
        self.assertTrue(any("even the target loses" in w for w in losing["warnings"]), losing["warnings"])
        tc.set_market(tc.US)
        self.assertNotIn("fees", plan("long", 100, "manual", [], stop=98))   # stock plans: no fee section

    def test_time_context_is_24_7(self):
        c = ep.time_context(tc.parse_et(f"{SAT}T04:40"), tc.parse_hhmm("23:55"))
        self.assertEqual((c["market_open"], c["minutes_left"], c["notes"]), (True, 1155, []))
        late = ep.time_context(tc.parse_et(f"{SAT}T23:57"), tc.parse_hhmm("23:55"))
        self.assertIn("it is 23:57 UTC, past the 23:55 time stop - no new day trades until the next UTC day "
                      "(00:00 UTC)", late["warnings"])
        stale = ep.time_context(tc.parse_et(f"{SAT}T04:40"), 1435, None, tc.Bar(utc_ts(SAT, "04:00"), 1, 1, 1, 1, 1))
        self.assertTrue(any("no trades or delayed data?" in w for w in stale["warnings"]), stale["warnings"])

    def test_finished_trigger_fallback_note(self):
        sig = {"setup": "orb", "label": "ORB(15)", "date": FRI, "triggered_at": "00:25", "entry": 101.9}
        self.assertEqual(ep.finished_trigger_warning({"entry_signal": sig}, "BTC-USD", 101.9, SAT),
                         "the saved BTC-USD entry 101.90 is an entry-skill trigger (ORB(15) at 00:25 UTC): trigger is "
                         f"from the finished {FRI} UTC session - re-plan from the current price")


class CryptoCliTests(TempHomeTestCase):
    """The real script against Coinbase fixtures on a Saturday, 04:40 UTC."""
    NOW = f"{SAT}T04:40"

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fixtures"
        bars = []
        for day in ("2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", FRI):
            bars += utc_flat(day, 288, price=100.0, wick=0.5)
        bars += utc_flat(SAT, 57, price=100.0, wick=0.5)                    # 00:00 .. 04:40 (forming)
        write_coinbase_fixtures(self.fx, "BTC-USD", bars, "5m", "5d", now_ts=utc_ts(SAT, "04:40"))
        write_coinbase_quote(self.fx, "BTC-USD", 100.0, when=f"{SAT}T04:40:00Z")
        write_coinbase_products(self.fx)
        self.env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(self.fx), "DAYTRADER_NOW": self.NOW}

    def cli(self, *args):
        return run_cli(SCRIPT, *args, env=self.env)

    def test_json_text_and_save(self):
        p = self.cli("BTC-USD", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        r = json.loads(p.stdout)
        self.assertEqual((r["entry"], r["entry_source"], r["stop"], r["target"]), (100.0, "current price", 98.5, 103.0))
        self.assertEqual((r["time_stop"], r["minutes_to_time_stop"], r["market_open"]), ("23:55", 1155, True))
        self.assertEqual((r["atr"], r["atr_info"]["source"], r["atr_info"]["today_bars"]), (1.0, "today", 56))
        self.assertEqual(r["avg_session_range"], 1.0)                       # whole UTC days only
        self.assertEqual(r["fees"]["round_trip_r"], 0.8)                    # 0.6% x 2 x 100 / 1.50
        self.assertTrue(any("paper fees" in w for w in r["warnings"]), r["warnings"])
        p = self.cli("BTC-USD", "--entry", "100", "--pct", "10", "--rr", "1.5", "--save")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("THE PLAN — BTC-USD LONG · percent stop (10%)", p.stdout)
        self.assertIn("ENTRY 100.00 / STOP 90.00 / TARGET 115.00 / R:R 1.5", p.stdout)
        self.assertIn("23:55  UTC - flat by then, win or lose (19h 15m left)", p.stdout)
        self.assertIn("Fees 0.12R round trip at 0.6% per fill (paper broker) · break-even win rate after fees "
                      "44.8%", " ".join(p.stdout.split()))                  # (1 + 0.114) / (1.5 - 0.129 + 1.114)
        saved = json.loads((self.home / "plans" / "BTC-USD.json").read_text())
        self.assertEqual((saved["stop"], saved["target"], saved["exit_plan"]["time_stop"]), (90.0, 115.0, "23:55"))

    def test_bad_time_stop_and_unknown_product(self):
        p = self.cli("BTC-USD", "--time-stop", "00:00")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("between 00:05 and 23:59 UTC", p.stderr)
        empty = self.home / "empty"
        empty.mkdir()
        p = run_cli(SCRIPT, "BTC-USD", env=dict(self.env, DAYTRADER_FIXTURES=str(empty)))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("api.exchange.coinbase.com", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


class CryptoDeskRunDocTests(unittest.TestCase):
    def test_desk_run_says_crypto_works_the_same(self):
        sec = desk_run_section("exit")
        line = [ln for ln in sec.splitlines() if ln.startswith("Crypto works the same way")]
        self.assertEqual(len(line), 1, sec)
        for word in ("Coinbase", "`BTC-USD`", "UTC", "UTC day", "fractional", "fees"):
            self.assertIn(word, line[0])
        self.assertEqual(sec, desk_run_section("time-machine"))
        for skill in ("entry", "exit", "time-machine"):
            text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
            desc = " ".join(re.search(r"^description: >-\n(.*?)^---", text, re.M | re.S).group(1).split())
            self.assertIn('"paper trade BTC"', desc, skill)
            self.assertIn('"plan a trade on BTC-USD"', desc, skill)
            self.assertLessEqual(len(desc), 1024, skill)


if __name__ == "__main__":
    unittest.main()
