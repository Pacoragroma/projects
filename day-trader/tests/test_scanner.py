"""Tests for skills/scanner/scripts/scan.py (offline, synthetic data only)."""
import json
import math
import os
import re
import shlex
import sys
import time
import unittest
from unittest import mock

# Works both with `unittest discover -s tests` and `python3 -m unittest tests.test_scanner`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (SKILLS, TempHomeTestCase, daily_bars, et_ts, fake_bars, run_cli,
                     skill_script, tc, trading_days, write_fixture, yahoo_chart_url,
                     yahoo_payload, load_skill_module)

scan = load_skill_module("scanner", "scan")
SCRIPT = skill_script("scanner", "scan.py")
END = "2026-10-30"          # a Friday


def wave(n=70, base=100.0, amp=1.0, k=0.9):
    """Range-bound closes: base +/- amp."""
    return [base + amp * math.sin(i * k) for i in range(n)]


def breakout_bars(vol_mult=2.5, end=END):
    return daily_bars(end, wave(70) + [104.0], [1_000_000] * 70 + [1_000_000 * vol_mult])


def breakdown_bars(end=END):
    return daily_bars(end, wave(70, 50) + [47.0], [800_000] * 70 + [4_000_000])


def gap_bars(gap_pct, end=END):
    closes = wave(70, 30)
    pc = closes[-1]
    o = pc * (1 + gap_pct / 100)
    return daily_bars(end, closes + [o], [2_000_000] * 71, gaps={70: o})


def pullback_bars(end=END):
    closes = [100 + i * 0.6 for i in range(50)]
    top = closes[-1]
    closes += [top + (1.0 if i % 2 else -1.0) for i in range(14)] + [top - 1.5, top + 0.3]
    return daily_bars(end, closes, [900_000] * len(closes))


def coil_bars(n=70, today=None, end=END):
    """Flat bars with a 2.00 range, then a 0.90-range inside day (optionally plus `today`)."""
    days = trading_days(end, n + 1 + (1 if today else 0))
    bars = [tc.Bar(et_ts(d, "09:30"), 100.0, 101.0, 99.0, 100.0, 1_000_000) for d in days[:n]]
    bars.append(tc.Bar(et_ts(days[n], "09:30"), 100.0, 100.5, 99.6, 100.2, 400_000))
    if today:
        o, h, l, c = today
        bars.append(tc.Bar(et_ts(days[n + 1], "09:30"), o, h, l, c, 500_000))
    return bars


def names(setups):
    return [s["setup"] for s in setups]


class DetectSetupTests(unittest.TestCase):
    def test_breakout_on_volume_and_hand_checked_score(self):
        bars = breakout_bars()
        found = scan.detect_setups(bars)
        self.assertEqual(found[0]["setup"], "breakout")
        bo = found[0]
        prior = bars[-21:-1]
        self.assertAlmostEqual(bo["levels"]["high_20d"], round(max(b.high for b in prior), 2))
        self.assertEqual(bo["bias"], "long")
        self.assertIn("above the 20-day high", bo["detail"])
        self.assertIn("2.5x volume", bo["detail"])
        # Score = 40 V + 40 M + 20 T, recomputed by hand from the bars.
        closes = [b.close for b in bars]
        atr = tc.atr(bars[:-1], 14)
        s20, s50 = tc.sma(closes, 20)[-1], tc.sma(closes, 50)[-1]
        v = min(1, math.log(2.5) / math.log(5))
        m = min(1, (bars[-1].close - bars[-2].close) / atr / 2)
        t = 0.5 * max(0, min(1, (bars[-1].close - s50) / atr)) + 0.5 * max(0, min(1, (s20 - s50) / atr))
        self.assertEqual(bo["score"], round(100 * (0.4 * v + 0.4 * m + 0.2 * t)))
        self.assertAlmostEqual(bo["components"]["volume"], v, places=3)

    def test_breakout_needs_volume(self):
        self.assertNotIn("breakout", names(scan.detect_setups(breakout_bars(vol_mult=1.4))))
        self.assertIn("breakout", names(scan.detect_setups(breakout_bars(vol_mult=1.5))))

    def test_breakdown(self):
        found = scan.detect_setups(breakdown_bars())
        self.assertEqual(names(found)[:2], ["breakdown", "unusual_volume"])   # 5x volume too
        self.assertEqual(found[0]["bias"], "short")
        self.assertEqual(found[0]["key_level"], "low_20d")
        self.assertEqual(found[1]["bias"], "short")      # unusual volume on a down day

    def test_gap_thresholds(self):
        self.assertIn("gap_up", names(scan.detect_setups(gap_bars(3.0))))
        self.assertNotIn("gap_up", names(scan.detect_setups(gap_bars(2.9))))
        down = scan.detect_setups(gap_bars(-4.0))
        self.assertIn("gap_down", names(down))
        g = next(s for s in down if s["setup"] == "gap_down")
        self.assertEqual(g["bias"], "short")
        self.assertIn("gapped -4.0%", g["detail"])
        self.assertNotIn("gap_down", names(scan.detect_setups(gap_bars(-2.9))))

    def test_gap_fill_and_fade_wording(self):
        closes = wave(70, 30)
        pc = closes[-1]
        bars = daily_bars(END, closes + [pc * 1.005], [2_000_000] * 71, gaps={70: pc * 1.05})
        g = next(s for s in scan.detect_setups(bars) if s["setup"] == "gap_up")
        self.assertIn("faded below the open", g["detail"])
        self.assertEqual(g["components"]["move"], round(min(1, (pc * 1.005 - pc) / tc.atr(bars[:-1]) / 2), 3))

    def test_unusual_volume_threshold(self):
        def bars(mult):
            return daily_bars(END, wave(71, 60, 0.5, 2.1), [1_000_000] * 70 + [1_000_000 * mult])
        self.assertIn("unusual_volume", names(scan.detect_setups(bars(3.0))))
        self.assertNotIn("unusual_volume", names(scan.detect_setups(bars(2.9))))

    def test_pullback(self):
        bars = pullback_bars()
        found = scan.detect_setups(bars)
        self.assertIn("pullback", names(found))
        pb = next(s for s in found if s["setup"] == "pullback")
        closes = [b.close for b in bars]
        self.assertGreater(tc.sma(closes, 20)[-1], tc.sma(closes, 50)[-1])
        self.assertTrue(40 <= tc.rsi(closes)[-1] <= 60)
        self.assertEqual(pb["key_level"], "sma20")
        self.assertAlmostEqual(pb["levels"]["sma20"], round(tc.sma(closes, 20)[-1], 2))

    def test_no_pullback_in_downtrend_or_flat_line(self):
        closes = [160 - i * 0.6 for i in range(66)]
        self.assertNotIn("pullback", names(scan.detect_setups(daily_bars(END, closes))))
        # 20-SMA a hair above the 50-SMA is a flat stock, not an uptrend.
        flat = daily_bars(END, wave(71, 80, 0.3, 2.9), [1_000_000] * 71)
        closes = [b.close for b in flat]
        self.assertGreater(tc.sma(closes, 20)[-1], tc.sma(closes, 50)[-1])
        self.assertEqual(scan.detect_setups(flat), [])

    def test_nr7_on_completed_day(self):
        bars = coil_bars()
        found = scan.detect_setups(bars)
        nr7 = next(s for s in found if s["setup"] == "nr7")
        self.assertEqual(nr7["levels"], {"range_high": 100.5, "range_low": 99.6})
        self.assertEqual(nr7["as_of"], bars[-1].date)
        self.assertEqual(nr7["bias"], "either")
        self.assertIn("today's range", nr7["detail"])
        # tightness = 1 - 0.9/2.0, dry-up = (1 - 0.4)/0.5 capped at 1, a flat line: ~no trend
        closes = [b.close for b in bars]
        s20, s50 = tc.sma(closes, 20)[-1], tc.sma(closes, 50)[-1]
        trend = 0.5 * (closes[-1] - s50) / 2.0 + 0.5 * (s20 - s50) / 2.0
        self.assertLess(trend, 0.06)
        self.assertEqual(nr7["score"], round(100 * 0.6 * (0.4 * 1.0 + 0.4 * 0.55 + 0.2 * trend)))

    def test_nr7_ranks_below_an_active_breakout(self):
        # A quiet inside day in a strong uptrend maxes every NR7 component; it must still rank
        # below a 20-day breakout on 3x volume (a coil has not done anything yet). Before the
        # fix it scored 95 vs 74, and NR7s filled 24 of the top 25 in a simulated full scan.
        days = trading_days(END, 71)
        bars, p = [], 50.0
        for d in days[:70]:
            bars.append(tc.Bar(et_ts(d, "09:30"), p, p * 1.01 * 1.01, p * 0.99, p * 1.01, 1_000_000))
            p *= 1.01
        mid = (bars[-1].high + bars[-1].low) / 2
        bars.append(tc.Bar(et_ts(days[70], "09:30"), mid, mid + 0.15, mid - 0.15, mid + 0.05, 400_000))
        nr7 = next(s for s in scan.detect_setups(bars) if s["setup"] == "nr7")
        self.assertEqual(nr7["components"]["trend"], 1.0)
        self.assertLessEqual(nr7["score"], round(100 * scan.NR7_WEIGHT))
        bo = scan.detect_setups(breakout_bars(vol_mult=3.0))[0]
        self.assertEqual(bo["setup"], "breakout")
        self.assertGreater(bo["score"], nr7["score"])

    def test_nr7_needs_strictly_narrowest_and_inside(self):
        bars = coil_bars()
        tie = bars[:-1] + [tc.Bar(bars[-1].ts, 100.0, 101.0, 99.0, 100.0, 400_000)]
        self.assertNotIn("nr7", names(scan.detect_setups(tie)))
        outside = bars[:-1] + [tc.Bar(bars[-1].ts, 100.0, 101.2, 100.5, 101.0, 400_000)]
        self.assertNotIn("nr7", names(scan.detect_setups(outside)))

    def test_nr7_during_session_refers_to_yesterday(self):
        bars = coil_bars(today=(100.3, 101.4, 100.1, 101.2))
        nr7 = next(s for s in scan.detect_setups(bars, progress=0.3) if s["setup"] == "nr7")
        self.assertEqual(nr7["as_of"], bars[-2].date)
        self.assertIn("yesterday's", nr7["detail"])
        self.assertIn("already trading above 100.50", nr7["detail"])
        # The same bars as a completed day: today's wide bar is not a coil.
        self.assertNotIn("nr7", names(scan.detect_setups(bars, progress=1.0)))

    def test_too_little_history(self):
        self.assertEqual(scan.detect_setups(breakout_bars()[-59:]), [])
        self.assertEqual(scan.detect_setups([]), [])

    def test_scores_bounded_and_sorted_on_random_data(self):
        import random
        for seed in range(25):
            rng = random.Random(seed)
            closes, vols, p = [], [], 50.0
            for _ in range(120):
                p *= math.exp(rng.gauss(0, 0.03))
                closes.append(p)
                vols.append(1e6 * math.exp(rng.gauss(0, 0.8)))
            gaps = {119: closes[118] * (1 + rng.uniform(-0.08, 0.08))}
            found = scan.detect_setups(daily_bars(END, closes, vols, gaps=gaps))
            scores = [s["score"] for s in found]
            self.assertEqual(scores, sorted(scores, reverse=True))
            for s in found:
                self.assertTrue(0 <= s["score"] <= 100, s)
                self.assertIn(s["setup"], scan.SETUPS)
                self.assertIn(s["key_level"], s["levels"])
                json.dumps(s)


class VolumePaceTests(unittest.TestCase):
    def test_curve(self):
        self.assertEqual(scan.volume_progress(0), 0.0)
        self.assertAlmostEqual(scan.volume_progress(30), 0.115)
        self.assertAlmostEqual(scan.volume_progress(45), (0.115 + 0.195) / 2)
        self.assertEqual(scan.volume_progress(390), 1.0)
        vals = [scan.volume_progress(m) for m in range(0, 390)]
        self.assertTrue(all(b > a for a, b in zip(vals, vals[1:])))
        self.assertLess(vals[-1], 0.93)               # the closing auction comes at the end
        # Half day (09:30-13:00): the curve is compressed onto 210 minutes.
        self.assertAlmostEqual(scan.volume_progress(105, 210), scan.volume_progress(195))

    def test_day_progress(self):
        last = tc.Bar(et_ts(END, "09:30"), 1, 1, 1, 1, 1)
        self.assertEqual(scan.day_progress(last, tc.parse_et("2026-10-31T12:00")), 1.0)
        self.assertAlmostEqual(scan.day_progress(last, tc.parse_et(f"{END}T10:00")), 0.115)
        self.assertEqual(scan.day_progress(last, tc.parse_et(f"{END}T16:00")), 1.0)
        half = {"currentTradingPeriod": {"regular": {"start": et_ts(END, "09:30"), "end": et_ts(END, "13:00")}}}
        self.assertEqual(scan.day_progress(last, tc.parse_et(f"{END}T13:30"), half), 1.0)
        self.assertAlmostEqual(scan.day_progress(last, tc.parse_et(f"{END}T11:15"), half),
                               scan.volume_progress(195))
        stale = {"currentTradingPeriod": {"regular": {"start": et_ts("2026-10-29", "09:30"),
                                                      "end": et_ts("2026-10-29", "13:00")}}}
        self.assertAlmostEqual(scan.day_progress(last, tc.parse_et(f"{END}T10:00"), stale), 0.115)
        self.assertAlmostEqual(scan.day_progress(last, tc.parse_et(f"{END}T10:00"), {"currentTradingPeriod": None}), 0.115)

    def test_day_progress_paces_replayed_data_at_its_own_stamp(self):
        # Replayed at 10:00 from data fetched at 15:00: the bar holds volume through 15:00, so
        # pacing it as 11.5% of a day would read ~6x relative volume.
        last = tc.Bar(et_ts(END, "09:30"), 1, 1, 1, 1, 1)
        later = {"regularMarketTime": et_ts(END, "15:00")}
        self.assertAlmostEqual(scan.day_progress(last, tc.parse_et(f"{END}T10:00"), later),
                               scan.volume_progress(330))
        earlier = {"regularMarketTime": et_ts(END, "09:55")}   # live data lags `now`: pace at `now`
        self.assertAlmostEqual(scan.day_progress(last, tc.parse_et(f"{END}T10:00"), earlier), 0.115)
        closed = {"regularMarketTime": et_ts(END, "16:00")}
        self.assertEqual(scan.day_progress(last, tc.parse_et(f"{END}T10:00"), closed), 1.0)
        self.assertAlmostEqual(scan.day_progress(last, tc.parse_et(f"{END}T10:00"), {"regularMarketTime": True}),
                               0.115)


class LookaheadTests(unittest.TestCase):
    """$DAYTRADER_NOW replays: a daily bar for the as-of day can hold trading after `now`."""
    NOW = tc.parse_et(f"{END}T10:16")
    BAR = tc.Bar(et_ts(END, "09:30"), 1, 1, 1, 1, 1)

    def test_data_after_now_is_flagged(self):
        full = scan.lookahead(self.BAR, self.NOW, {"regularMarketTime": et_ts(END, "16:00")})
        self.assertEqual(full, {"through": et_ts(END, "16:00"), "to_close": True})
        mid = scan.lookahead(self.BAR, self.NOW, {"regularMarketTime": et_ts(END, "15:59")})
        self.assertEqual(mid, {"through": et_ts(END, "15:59"), "to_close": False})
        # A live-style bar stamped with its last trade time says so too.
        live_row = tc.Bar(et_ts(END, "13:05"), 1, 1, 1, 1, 1)
        self.assertEqual(scan.lookahead(live_row, self.NOW)["through"], et_ts(END, "13:05"))
        # No timestamp says where the data ends: it may hold later trading.
        self.assertEqual(scan.lookahead(self.BAR, self.NOW), {"through": None, "to_close": False})

    def test_honest_as_of_data_is_not_flagged(self):
        self.assertIsNone(scan.lookahead(self.BAR, self.NOW, {"regularMarketTime": et_ts(END, "10:15")}))
        self.assertIsNone(scan.lookahead(self.BAR, tc.parse_et(f"{END}T16:00"),
                                         {"regularMarketTime": et_ts(END, "16:00")}))   # day is over
        self.assertIsNone(scan.lookahead(self.BAR, tc.parse_et("2026-11-02T10:16"),
                                         {"regularMarketTime": et_ts(END, "16:00")}))   # an earlier day

    def test_evaluate_symbol_flags_only_replays(self):
        bars = breakout_bars()
        meta = {"regularMarketTime": et_ts(END, "16:00")}
        r = scan.evaluate_symbol("BRKO", bars, self.NOW, meta, replay=True)
        self.assertTrue(r["row"]["after_as_of"])
        self.assertTrue(r["lookahead"]["to_close"])
        r = scan.evaluate_symbol("BRKO", bars, self.NOW, meta)
        self.assertFalse(r["row"]["after_as_of"])
        self.assertNotIn("lookahead", r)
        # Filtered names were judged on the later prices too.
        thin = daily_bars(END, wave(70, 20) + [21.5], [100_000] * 70 + [400_000])
        r = scan.evaluate_symbol("THIN", thin, self.NOW, meta, replay=True)
        self.assertEqual(r["status"], "filtered")
        self.assertIn("lookahead", r)

    def test_warning_text(self):
        now = self.NOW
        one = scan.replay_warning([{"symbol": "TSLA", "through": et_ts(END, "15:59"), "to_close": False}], now)
        self.assertIn("replay at 10:16 ET ($DAYTRADER_NOW): the 2026-10-30 daily bar of TSLA includes trading "
                      "after 10:16 ET (data through 15:59 ET)", one)
        self.assertIn("not the market at 10:16", one)
        many = scan.replay_warning([{"symbol": s, "through": et_ts(END, "16:00"), "to_close": True}
                                    for s in ("NVDA", "AMD")], now)
        self.assertIn("daily bars of 2 symbols (AMD, NVDA) include trading after 10:16 ET "
                      "(data runs to the close)", many)
        unknown = scan.replay_warning([{"symbol": "X", "through": None, "to_close": False}], now)
        self.assertIn("bar of X may include trading after 10:16 ET", unknown)
        self.assertIsNone(scan.replay_warning([], now))

    def test_partial_day_rvol_is_paced_with_floor(self):
        bars = breakout_bars(vol_mult=0.4)        # 400k so far vs a 1M average
        m = scan.metrics_at(bars, len(bars) - 1, progress=0.2)
        self.assertAlmostEqual(m["rvol"], 2.0)
        self.assertTrue(m["partial"])
        m = scan.metrics_at(bars, len(bars) - 1, progress=0.001)
        self.assertAlmostEqual(m["rvol"], 0.4 / scan.MIN_PROGRESS)
        self.assertAlmostEqual(scan.metrics_at(bars, len(bars) - 1)["rvol"], 0.4)
        bo = next(s for s in scan.detect_setups(bars, progress=0.2) if s["setup"] == "breakout")
        self.assertIn("trading", bo["detail"])
        self.assertIn("pace-adjusted", bo["detail"])


class NormalizeTests(unittest.TestCase):
    def test_merges_duplicate_rows_and_drops_future(self):
        d0, d1, d2 = trading_days("2026-11-02", 3)
        bars = [tc.Bar(et_ts(d0, "09:30"), 10, 11, 9, 10.5, 100),
                tc.Bar(et_ts(d1, "09:30"), 10.5, 11, 10, 10.8, 50),
                tc.Bar(et_ts(d1, "11:47"), 10.6, 11.5, 10.2, 11.2, 80),     # Yahoo's live row
                tc.Bar(et_ts(d2, "09:30"), 11, 12, 11, 11.5, 70)]
        out = scan.normalize_daily(bars, today=d1)
        self.assertEqual([b.date for b in out], [d0, d1])
        m = out[1]
        self.assertEqual((m.open, m.high, m.low, m.close, m.volume), (10.5, 11.5, 10, 11.2, 80))
        self.assertEqual(len(scan.normalize_daily(bars, today=d1, before_open=True)), 1)

    def test_bad_volume_becomes_zero(self):
        b = tc.Bar(et_ts(END, "09:30"), 10, 11, 9, 10, float("nan"))
        self.assertEqual(scan.normalize_daily([b], END)[0].volume, 0.0)


class EvaluateTests(unittest.TestCase):
    NOW = tc.parse_et(f"{END}T17:00")

    def test_row_shape(self):
        r = scan.evaluate_symbol("brko", breakout_bars(), self.NOW, {"longName": "Breakout Inc"})
        self.assertEqual(r["status"], "ok")
        row = r["row"]
        for k in ("symbol", "name", "score", "bias", "setups", "date", "partial_day", "last",
                  "change_pct", "gap_pct", "rvol", "atr14", "atr_pct", "sma20", "sma50", "rsi14",
                  "high_20d", "low_20d", "dollar_volume_20d", "key_level"):
            self.assertIn(k, row)
        self.assertEqual(row["symbol"], "BRKO")
        self.assertEqual(row["name"], "Breakout Inc")
        self.assertFalse(row["partial_day"])
        self.assertEqual(row["key_level"]["name"], "high_20d")
        self.assertEqual(row["rvol"], 2.5)
        json.dumps(r)

    def test_liquidity_filters(self):
        cheap = daily_bars(END, wave(70, 2.5, 0.05) + [2.9], [50_000_000] * 71)
        r = scan.evaluate_symbol("PENNY", cheap, self.NOW)
        self.assertEqual((r["status"], r["kind"]), ("filtered", "price"))
        self.assertIn(scan.evaluate_symbol("PENNY", cheap, self.NOW, min_price=1)["status"], ("ok", "none"))
        thin = daily_bars(END, wave(70, 20) + [21.5], [100_000] * 70 + [400_000])
        r = scan.evaluate_symbol("THIN", thin, self.NOW)
        self.assertEqual((r["status"], r["kind"]), ("filtered", "dollar volume"))
        self.assertIn("$2.00M", r["reason"])
        self.assertIn(scan.evaluate_symbol("THIN", thin, self.NOW, min_dollar_volume=1e6)["status"], ("ok", "none"))

    def test_dollar_volume_excludes_todays_bar(self):
        # A huge volume day must not lift a thin stock over the liquidity bar by itself.
        bars = daily_bars(END, wave(70, 20) + [21.5], [500_000] * 70 + [50_000_000])
        self.assertEqual(scan.evaluate_symbol("X", bars, self.NOW)["status"], "filtered")

    def test_history_and_staleness(self):
        r = scan.evaluate_symbol("NEW", breakout_bars()[-40:], self.NOW)
        self.assertEqual((r["status"], r["kind"]), ("skipped", "short history"))
        old = breakout_bars(end="2026-10-16")
        r = scan.evaluate_symbol("OLD", old, self.NOW)
        self.assertEqual((r["status"], r["kind"]), ("skipped", "stale"))
        # Friday's bar is current on Monday morning.
        self.assertEqual(scan.evaluate_symbol("F", breakout_bars(), tc.parse_et("2026-11-02T08:00"))["status"], "ok")

    def test_replay_ignores_bars_after_now(self):
        bars = breakout_bars() + daily_bars("2026-11-02", [90.0])
        r = scan.evaluate_symbol("BRKO", bars, self.NOW)
        self.assertEqual(r["row"]["date"], END)
        self.assertEqual(r["row"]["setups"][0]["setup"], "breakout")

    def test_pre_market_uses_last_completed_day(self):
        r = scan.evaluate_symbol("BRKO", breakout_bars(), tc.parse_et(f"{END}T08:00"))
        self.assertNotEqual(r["row"]["date"], END)
        self.assertFalse(r["row"]["partial_day"])

    def test_wanted_filter(self):
        r = scan.evaluate_symbol("DUMP", breakdown_bars(), self.NOW, wanted=["unusual_volume"])
        self.assertEqual(names(r["row"]["setups"]), ["unusual_volume"])
        r = scan.evaluate_symbol("DUMP", breakdown_bars(), self.NOW, wanted=["gap_up"])
        self.assertEqual(r["status"], "none")
        self.assertEqual(r["row"]["score"], 0)

    def test_replay_mid_session_on_a_completed_bar_is_not_projected(self):
        # $DAYTRADER_NOW=10:15 on a past date: Yahoo's bar for that date is the whole day (its
        # regularMarketTime is after that session's close). Projecting it as 17% of a day made
        # every name read ~6x "unusual volume".
        bars = daily_bars(END, wave(71, 80, 0.3, 2.9), [1_000_000] * 71)
        meta = {"regularMarketTime": et_ts("2026-11-04", "16:00")}
        r = scan.evaluate_symbol("CALM", bars, tc.parse_et(f"{END}T10:15"), meta)
        self.assertFalse(r["row"]["partial_day"])
        self.assertEqual(r["row"]["rvol"], 1.0)
        self.assertEqual(r["status"], "none")
        # Live mid-session data (stamped before the close) is still paced.
        live = {"regularMarketTime": et_ts(END, "10:14")}
        r = scan.evaluate_symbol("CALM", bars, tc.parse_et(f"{END}T10:15"), live)
        self.assertTrue(r["row"]["partial_day"])

    def test_intraday_marks_partial(self):
        r = scan.evaluate_symbol("BRKO", breakout_bars(vol_mult=0.5), tc.parse_et(f"{END}T10:00"))
        self.assertTrue(r["row"]["partial_day"])
        self.assertAlmostEqual(r["row"]["rvol"], round(0.5 / 0.115, 2))
        self.assertIn("unusual_volume", names(r["row"]["setups"]))


class RunScanTests(TempHomeTestCase):
    NOW = f"{END}T17:30"

    def universe(self, *syms):
        return [{"symbol": s, "name": f"{s} Inc", "exchange": "NASDAQ"} for s in syms]

    def test_rank_top_and_counts(self):
        data = {"BRKO": breakout_bars(), "DUMP": breakdown_bars(), "GAPU": gap_bars(6.0),
                "CALM": daily_bars(END, wave(71, 80, 0.3, 2.9), [1_000_000] * 71),
                "NEWB": breakout_bars()[-30:]}
        with fake_bars(data) as fb:
            res = scan.run_scan(self.universe("BRKO", "DUMP", "GAPU", "CALM", "NEWB", "GONE"),
                                tc.now_et(), workers=3, top=2)
        self.assertEqual(fb.call_args_list[0].args[1:3], ("1d", "6mo"))
        self.assertEqual(res["scanned"], 6)
        self.assertEqual(res["matched"], 3)
        self.assertEqual([r["symbol"] for r in res["results"]], ["DUMP", "BRKO"])
        self.assertGreaterEqual(res["results"][0]["score"], res["results"][1]["score"])
        self.assertEqual(res["skipped"], 2)
        self.assertEqual(res["skipped_by_reason"], {"download failed": 1, "short history": 1})
        self.assertEqual(res["no_setup"], ["CALM"])
        self.assertEqual(res["passed_filters"], 4)
        self.assertEqual(res["results"][0]["exchange"], "NASDAQ")
        self.assertEqual(res["results"][0]["name"], "DUMP Inc")
        self.assertFalse(res["incomplete"])
        text = scan.render(res)
        self.assertIn("Scanned 6 symbols", text)
        self.assertIn("2 skipped: 1 download failed, 1 short history", text)
        self.assertIn("top 2 shown", text)
        self.assertIn("48.51 (20d low)", text)
        self.assertIn("Skipped: GONE (no fixture for interval 1d); NEWB (only 30 daily bars, need 60)", text)
        self.assertIn(tc.DISCLAIMER, text)

    def test_all_downloads_fail_is_a_data_error(self):
        with fake_bars({}):
            with self.assertRaises(tc.FetchError):
                scan.run_scan(self.universe("A", "B"), tc.now_et())

    def test_unknown_symbols_are_bad_input(self):
        err = tc.FetchError("ZZZZ: HTTP 404 for https://query1.finance.yahoo.com/v8/finance/chart/ZZZZ - not found")
        with mock.patch.object(tc, "fetch_bars", side_effect=err):
            with self.assertRaises(ValueError) as cm:
                scan.run_scan(self.universe("ZZZZ"), tc.now_et())
        self.assertIn("ZZZZ", str(cm.exception))

    def test_everything_not_found_is_a_data_error_not_a_typo(self):
        def fetch(sym, *a, **k):
            time.sleep(0.002)
            raise tc.FetchError(f"{sym}: HTTP 404 for https://query1.finance.yahoo.com/x - not found")
        with mock.patch.object(tc, "fetch_bars", side_effect=fetch) as fb:
            with self.assertRaises(tc.FetchError) as cm:
                scan.run_scan(self.universe(*[f"S{i}" for i in range(300)]), tc.now_et(), workers=1)
        self.assertIn("API may have changed", str(cm.exception))
        self.assertLess(fb.call_count, 300)

    def test_source_down_aborts_after_first_wave(self):
        def fetch(sym, *a, **k):
            time.sleep(0.005)     # a little latency, like a real request
            raise tc.FetchError(f"{sym}: network error for https://query2.finance.yahoo.com/x: refused")
        with mock.patch.object(tc, "fetch_bars", side_effect=fetch) as fb:
            with self.assertRaises(tc.FetchError) as cm:
                scan.run_scan(self.universe(*[f"S{i}" for i in range(200)]), tc.now_et(), workers=2)
        self.assertIn("query2.finance.yahoo.com", str(cm.exception))
        self.assertLess(fb.call_count, 20)

    def test_source_dies_mid_scan_returns_partial_results(self):
        good = {f"G{i}": breakout_bars() for i in range(3)}

        def fetch(sym, *a, **k):
            time.sleep(0.005)
            if sym in good:
                return good[sym], {}
            raise tc.FetchError(f"{sym}: HTTP 429 for https://query1.finance.yahoo.com/x - rate limited")
        uni = self.universe(*good, *[f"B{i}" for i in range(100)])
        with mock.patch.object(tc, "fetch_bars", side_effect=fetch) as fb:
            res = scan.run_scan(uni, tc.now_et(), workers=1)
        self.assertTrue(res["incomplete"])
        self.assertIn("stopped early", res["warning"])
        self.assertEqual(res["matched"], 3)
        self.assertLess(fb.call_count, 60)       # stopped after ~30 failures in a row
        self.assertIn("WARNING: stopped early", scan.render(res))

    def test_single_worker_survives_one_early_failure(self):
        # With --workers 1 the "first wave" was a single download: one transient error on the
        # first symbol turned a whole-market scan into exit 2.
        good = {f"G{i}": breakout_bars() for i in range(4)}

        def fetch(sym, *a, **k):
            if sym == "BAD":
                raise tc.FetchError("BAD: HTTP 500 for https://query1.finance.yahoo.com/x")
            return good[sym], {}
        with mock.patch.object(tc, "fetch_bars", side_effect=fetch):
            res = scan.run_scan(self.universe("BAD", *good), tc.now_et(), workers=1)
        self.assertEqual(res["matched"], 4)
        self.assertEqual(res["skipped_by_reason"], {"download failed": 1})

    def test_setup_filter_does_not_claim_no_setup(self):
        # BRKO has a breakout; with --setups gap_up it must not be listed as "No setup today".
        with fake_bars({"BRKO": breakout_bars()}):
            res = scan.run_scan(self.universe("BRKO"), tc.now_et(), wanted=["gap_up"])
        text = scan.render(res)
        self.assertNotIn("No setup today", text)
        self.assertIn("No matching setup (gap_up): BRKO", text)
        with fake_bars({"CALM": daily_bars(END, wave(71, 80, 0.3, 2.9), [1_000_000] * 71)}):
            self.assertIn("No setup today: CALM", scan.render(scan.run_scan(self.universe("CALM"), tc.now_et())))

    def test_replay_warning_in_results_and_text(self):
        # $DAYTRADER_NOW=10:16 over data fetched after the close: say the scan saw the whole day.
        now = tc.parse_et(f"{END}T10:16")
        with fake_bars({"BRKO": breakout_bars(), "CALM": daily_bars(END, wave(71, 80, 0.3, 2.9), [1_000_000] * 71)},
                       meta={"regularMarketTime": et_ts(END, "16:00")}):
            res = scan.run_scan(self.universe("BRKO", "CALM"), now, replay=True)
        self.assertIn("daily bars of 2 symbols (BRKO, CALM) include trading after 10:16 ET", res["replay_warning"])
        self.assertTrue(res["results"][0]["after_as_of"])
        text = scan.render(res)
        self.assertIn("WARNING: replay at 10:16 ET ($DAYTRADER_NOW)", text)
        self.assertNotIn("results below are partial", text)
        json.dumps(res)
        # Data stamped by `now` (a faithful replay) or a live run: no warning.
        with fake_bars({"BRKO": breakout_bars()}, meta={"regularMarketTime": et_ts(END, "10:15")}):
            res = scan.run_scan(self.universe("BRKO"), now, replay=True)
        self.assertIsNone(res["replay_warning"])
        self.assertNotIn("WARNING", scan.render(res))
        with fake_bars({"BRKO": breakout_bars()}, meta={"regularMarketTime": et_ts(END, "16:00")}):
            self.assertIsNone(scan.run_scan(self.universe("BRKO"), now)["replay_warning"])

    def test_progress_lines(self):
        data = {f"S{i}": breakout_bars() for i in range(12)}
        lines = []
        with mock.patch.object(scan, "PROGRESS_EVERY", 5), fake_bars(data):
            scan.run_scan(self.universe(*data), tc.now_et(), workers=2, progress=lines.append)
        self.assertEqual(len(lines), 2)
        self.assertIn("/12 symbols", lines[0])

    def test_evaluation_error_is_counted_not_fatal(self):
        with fake_bars({"A": breakout_bars(), "B": breakout_bars()}), \
                mock.patch.object(scan, "evaluate_symbol", side_effect=[ZeroDivisionError("boom"), {"symbol": "B", "status": "none", "row": {"name": "", "symbol": "B", "partial_day": False}}]):
            res = scan.run_scan(self.universe("A", "B"), tc.now_et(), workers=1)
        self.assertEqual(res["skipped_by_reason"], {"error": 1})


class ParseTests(unittest.TestCase):
    def test_symbols(self):
        self.assertEqual(scan.parse_symbol_list("tsla, nvda AAPL,tsla"), ["TSLA", "NVDA", "AAPL"])
        self.assertEqual(scan.parse_symbol_list("brk.b,BRK-B,$amd"), ["BRK.B", "AMD"])
        for bad in ("", " , ", "TS!LA", "../etc"):
            with self.assertRaises(ValueError):
                scan.parse_symbol_list(bad)

    def test_setups(self):
        self.assertIsNone(scan.parse_setups(None))
        self.assertIsNone(scan.parse_setups("all"))
        self.assertEqual(scan.parse_setups("breakout, gap-up,breakout"), ["breakout", "gap_up"])
        with self.assertRaises(ValueError):
            scan.parse_setups("breakout,moonshot")

    def test_amounts(self):
        self.assertEqual(scan.parse_amount("20000000"), 20e6)
        self.assertEqual(scan.parse_amount("20M"), 20e6)
        self.assertEqual(scan.parse_amount("$1.5b"), 1.5e9)
        self.assertEqual(scan.parse_amount("500k"), 500e3)
        self.assertEqual(scan.parse_amount("2e7"), 2e7)
        self.assertEqual(scan.parse_amount(0), 0)
        # --help shows "default 20,000,000": thousands separators must work too
        self.assertEqual(scan.parse_amount("20,000,000"), 20e6)
        self.assertEqual(scan.parse_amount("$1,500,000.50"), 1_500_000.5)
        for bad in ("abc", "-5M", "20MM", "", "1,5M", "20,00,000"):
            with self.assertRaises(ValueError):
                scan.parse_amount(bad)

    def test_short_reason(self):
        r = ("TSLA: HTTP 403 for https://query2.finance.yahoo.com/v8/finance/chart/TSLA?interval=1d"
             " - access denied")
        self.assertEqual(scan.short_reason(r, "TSLA"), "HTTP 403 for query2.finance.yahoo.com - access denied")


class SkillDocTests(unittest.TestCase):
    TEXT = (SKILLS / "scanner" / "SKILL.md").read_text(encoding="utf-8")

    def test_frontmatter_and_required_lines(self):
        self.assertTrue(self.TEXT.startswith("---\nname: scanner\n"))
        front = self.TEXT.split("---")[1]
        desc = " ".join(line.strip() for line in front.split("description:", 1)[1].splitlines()[1:])
        self.assertTrue(desc.startswith("Day-trading desk:"))
        self.assertLessEqual(len(desc), 1024)
        self.assertIn("> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder "
                      "(`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.",
                      self.TEXT)
        self.assertIn("allow-listed", self.TEXT)
        self.assertLess(len(self.TEXT.splitlines()), 150)

    def test_every_documented_command_parses(self):
        cmds = re.findall(r"python3 scripts/scan\.py[^\n`#]*", self.TEXT)
        self.assertGreaterEqual(len(cmds), 5)
        for cmd in cmds:
            argv = shlex.split(cmd)[2:]
            args = scan.build_parser().parse_args(argv)
            scan.parse_setups(args.setups)
            scan.parse_amount(args.min_dollar_volume)
            if args.symbols:
                scan.parse_symbol_list(args.symbols)
        # flags named in the prose exist too
        opts = scan.build_parser()._option_string_actions
        for flag in set(re.findall(r"`(--[a-z-]+)", self.TEXT)):
            self.assertIn(flag, opts, flag)


class CliTests(TempHomeTestCase):
    NOW = f"{END}T17:30"

    NASDAQ = ("Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
              "BRKO|Breakout Inc. - Common Stock|Q|N|N|100|N|N\n"
              "QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N\n"
              "DUMP|Dump Corp - Common Stock|Q|N|N|100|N|N\n"
              "File Creation Time: 1030202606:01|||||||\n")
    OTHER = ("ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
             "GAPU|Gap Up Holdings|N|GAPU|N|100|N|GAPU\n"
             "CALM|Calm Industries|N|CALM|N|100|N|CALM\n"
             "File Creation Time: 1030202606:01|||||||\n")

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fixtures"
        for sym, bars in {"BRKO": breakout_bars(), "DUMP": breakdown_bars(), "GAPU": gap_bars(6.0),
                          "CALM": daily_bars(END, wave(71, 80, 0.3, 2.9), [1_000_000] * 71)}.items():
            write_fixture(self.fx, yahoo_chart_url(sym, "1d", "6mo"), yahoo_payload(bars, sym))
        write_fixture(self.fx, tc.NASDAQ_LISTED_URL, self.NASDAQ)
        write_fixture(self.fx, tc.OTHER_LISTED_URL, self.OTHER)
        self.env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(self.fx),
                    "DAYTRADER_NOW": self.NOW}

    def test_symbols_json(self):
        p = run_cli(SCRIPT, "--symbols", "BRKO,DUMP,NOPE", "--json", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        res = json.loads(p.stdout)
        self.assertEqual([r["symbol"] for r in res["results"]], ["DUMP", "BRKO"])
        self.assertEqual(res["skipped_symbols"][0]["symbol"], "NOPE")
        self.assertEqual(res["filters"]["universe"], "symbols")
        self.assertEqual(res["disclaimer"], tc.DISCLAIMER)
        self.assertEqual(res["results"][1]["setups"][0]["levels"]["high_20d"],
                         round(max(b.high for b in breakout_bars()[-21:-1]), 2))
        self.assertIsNone(res["replay_warning"])          # 17:30: the day's bar is complete

    def test_replay_mid_session_warns_text_and_json(self):
        # Fixtures fetched after the close, replayed at 10:16 (the critic's TSLA case).
        session = {"currentTradingPeriod": {"regular": {"start": et_ts(END, "09:30"), "end": et_ts(END, "16:00")}}}
        write_fixture(self.fx, yahoo_chart_url("BRKO", "1d", "6mo"),
                      yahoo_payload(breakout_bars(), "BRKO", regularMarketTime=et_ts(END, "15:59"), **session))
        env = dict(self.env, DAYTRADER_NOW=f"{END}T10:16")
        p = run_cli(SCRIPT, "--symbols", "BRKO", env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("WARNING: replay at 10:16 ET ($DAYTRADER_NOW): the 2026-10-30 daily bar of BRKO includes "
                      "trading after 10:16 ET (data through 15:59 ET)", p.stdout)
        j = run_cli(SCRIPT, "--symbols", "BRKO", "--json", env=env)
        self.assertEqual(j.returncode, 0, j.stderr)
        res = json.loads(j.stdout)
        self.assertIn("includes trading after 10:16 ET", res["replay_warning"])
        self.assertTrue(res["results"][0]["after_as_of"])
        # The same data replayed at its own time (10:16, stamped 10:15:55) is an honest as-of view.
        write_fixture(self.fx, yahoo_chart_url("BRKO", "1d", "6mo"),
                      yahoo_payload(breakout_bars(), "BRKO", regularMarketTime=et_ts(END, "10:15") + 55, **session))
        res = json.loads(run_cli(SCRIPT, "--symbols", "BRKO", "--json", env=env).stdout)
        self.assertIsNone(res["replay_warning"])
        self.assertFalse(res["results"][0]["after_as_of"])

    def test_universe_text(self):
        p = run_cli(SCRIPT, "--limit", "3", "--setups", "breakout,breakdown,gap_up", env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout
        self.assertIn("SCANNER · Fri 2026-10-30 17:30 ET", out)
        self.assertIn("Scanned 3 symbols", out)          # BRKO, DUMP, GAPU (QQQ is an ETF)
        for sym in ("BRKO", "DUMP", "GAPU"):
            self.assertIn(sym, out)
        self.assertNotIn("CALM", out)
        self.assertIn("setups: breakout, breakdown, gap_up", out)

    def test_no_data_is_exit_2_one_line(self):
        empty = self.home / "empty"
        empty.mkdir()
        for args in (("--symbols", "TSLA,NVDA"), ("--limit", "5")):
            p = run_cli(SCRIPT, *args, env=dict(self.env, DAYTRADER_FIXTURES=str(empty)))
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertTrue(p.stderr.startswith("data error: "), p.stderr)
            self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
            self.assertNotIn("Traceback", p.stderr)

    def test_ctrl_c_does_not_wait_for_in_flight_downloads(self):
        # Python joins ThreadPoolExecutor workers at exit, so Ctrl-C used to hang until every
        # in-flight download finished (up to a full timeout + retries each).
        import signal
        import subprocess
        driver = (
            "import sys, time, runpy\n"
            f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
            "import trader_common as tc\n"
            "def slow(*a, **k):\n"
            "    time.sleep(8)\n"
            "    raise tc.FetchError('x')\n"
            "tc.fetch_bars = slow\n"
            f"sys.argv = [{str(SCRIPT)!r}, '--symbols', 'A,B,C', '--quiet']\n"
            "print('started', flush=True)\n"
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n")
        p = subprocess.Popen([sys.executable, "-c", driver], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=dict(os.environ, **self.env))
        self.assertEqual(p.stdout.readline().strip(), "started")
        time.sleep(0.4)
        t0 = time.monotonic()
        p.send_signal(signal.SIGINT)
        try:
            _, err = p.communicate(timeout=15)
        finally:
            p.kill()
        self.assertEqual(p.returncode, 130, err)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertNotIn("Traceback", err)

    def test_bad_input_is_exit_1(self):
        for args in (("--setups", "moonshot"), ("--workers", "99"), ("--bogus",),
                     ("--min-dollar-volume", "lots"), ("--top", "-1")):
            p = run_cli(SCRIPT, *args, env=self.env)
            self.assertEqual(p.returncode, 1, (args, p.stderr))
            self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
