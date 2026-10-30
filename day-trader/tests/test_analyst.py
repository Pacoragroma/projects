"""Tests for the analyst skill (skills/analyst/scripts/analyst.py). Offline, synthetic data only."""
import json
import os
import re
import shlex
import sys
import unittest

# Works both with `unittest discover -s tests` and `python3 -m unittest tests.test_analyst`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers  # noqa: E402
from helpers import (SKILLS, TempHomeTestCase, bars_from_closes, daily_bars, et_ts, fake_bars,  # noqa: E402
                     fake_http, random_walk_history, tc, trading_days, write_fixture, yahoo_chart_url,
                     yahoo_payload)

an = helpers.load_skill_module("analyst", "analyst")
SCRIPT = helpers.skill_script("analyst", "analyst.py")
SKILL_MD = SKILLS / "analyst" / "SKILL.md"
DAY = "2026-10-30"   # a Friday
PREV = "2026-10-29"
CIK = "0001318605"
SHARES_URL = an.SHARES_URL.format(cik=CIK)
SUBS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"


def at(stamp):
    return tc.parse_et(stamp)


def bar(day, hhmm, o, h, l, c, v=1000.0):
    return tc.Bar(et_ts(day, hhmm), o, h, l, c, v)


def flat_session(day, per_bar_volume, price=100.0, n_bars=78, start="09:30"):
    return bars_from_closes(day, [price] * n_bars, start=start, volume=per_bar_volume, wick=0.1)


def intraday_history(end_day, n_sessions, today_volume=2000.0, past_volume=1000.0, price=100.0):
    """n_sessions-1 full flat past sessions at past_volume per bar, then `end_day` at today_volume."""
    days = trading_days(end_day, n_sessions)
    bars = []
    for d in days[:-1]:
        bars += flat_session(d, past_volume, price)
    bars += flat_session(days[-1], today_volume, price)
    return bars


def rising_daily(end_day, n=60, start=100.0, step=1.0, volume=1_000_000):
    return daily_bars(end_day, [start + i * step for i in range(n)], volumes=[volume] * n, wick_pct=0.01)


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------

class BarCleaningTests(unittest.TestCase):
    def test_clean_daily_dedupes_restamps_and_drops_future(self):
        raw = [bar("2026-10-28", "09:30", 10, 11, 9, 10.5), bar(PREV, "09:30", 10, 11, 9, 10.0),
               tc.Bar(et_ts(PREV, "15:59"), 10, 12, 9, 11.0, 5),          # later copy of 10-29 wins
               bar(DAY, "09:30", 11, 12, 10, 11.5), bar("2026-11-02", "09:30", 12, 13, 11, 12.5)]
        out = an.clean_daily(raw, DAY, 10 * 60)
        self.assertEqual([b.date for b in out], ["2026-10-28", PREV, DAY])
        self.assertEqual(out[1].close, 11.0)
        self.assertTrue(all(b.time == "09:30" for b in out))
        pre_open = an.clean_daily(raw, DAY, 9 * 60)                      # today's bar not started yet
        self.assertEqual(pre_open[-1].date, PREV)

    def test_clean_daily_utc_midnight_stamp_belongs_to_next_date(self):
        from datetime import datetime, timezone
        ts = int(datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc).timestamp())   # 20:00 ET on 10-29
        out = an.clean_daily([tc.Bar(ts, 1, 2, 0.5, 1.5, 10)], DAY, 11 * 60)
        self.assertEqual(out[0].date, DAY)

    def test_clean_intraday_dedupes_live_bar_and_drops_unstarted(self):
        raw = [bar(DAY, "09:00", 1, 1, 1, 1),                                  # pre-market
               bar(DAY, "09:30", 10, 10.5, 9.9, 10.2), bar(DAY, "09:35", 10.2, 10.4, 10.1, 10.3),
               tc.Bar(et_ts(DAY, "09:37") + 43, 10.2, 10.6, 10.1, 10.55, 900),   # live copy of 09:35
               bar(DAY, "09:40", 10.5, 10.7, 10.4, 10.6)]                         # starts at "now"
        out = an.clean_intraday(raw, et_ts(DAY, "09:40"))
        self.assertEqual([b.time for b in out], ["09:30", "09:35"])
        self.assertEqual(out[1].close, 10.55)
        self.assertEqual(out[1].ts, et_ts(DAY, "09:35"))

    def test_aggregate_and_regression_slope(self):
        a = an.aggregate([bar(DAY, "09:30", 10, 11, 9.5, 10.5, 100), bar(DAY, "09:35", 10.5, 12, 10, 11, 50)])
        self.assertEqual((a.open, a.high, a.low, a.close, a.volume), (10, 12, 9.5, 11, 150))
        self.assertAlmostEqual(an.regression_slope([1, 2, 3, 4]), 1.0)
        self.assertAlmostEqual(an.regression_slope([4, 2, 0]), -2.0)
        self.assertIsNone(an.regression_slope([5]))


# ---------------------------------------------------------------------------
# Brief analysis
# ---------------------------------------------------------------------------

class TrendTests(unittest.TestCase):
    def test_uptrend_hand_checked(self):
        closes = [100.0 + i for i in range(60)]                 # 100..159
        t = an.trend_view(closes, 160.0)
        self.assertEqual(t["label"], "UPTREND")
        self.assertAlmostEqual(t["sma20"], 149.5)                # mean(140..159)
        self.assertAlmostEqual(t["sma50"], 134.5)                # mean(110..159)
        self.assertAlmostEqual(t["slope_20d_pct_per_day"], round(1 / 149.5 * 100, 3))
        self.assertEqual(t["slope_dir"], "rising")
        self.assertAlmostEqual(t["price_vs_sma20_pct"], round((160 / 149.5 - 1) * 100, 2))

    def test_downtrend_sideways_mixed(self):
        self.assertEqual(an.trend_view([200.0 - i for i in range(60)], 139.0)["label"], "DOWNTREND")
        flat = [100.0 + (i % 2) for i in range(60)]
        self.assertEqual(an.trend_view(flat, 100.5)["label"], "SIDEWAYS")
        # Rising trend, but today trades below the 20-day SMA: a pullback reads MIXED, not UPTREND.
        self.assertEqual(an.trend_view([100.0 + i for i in range(60)], 145.0)["label"], "MIXED")

    def test_short_history(self):
        t = an.trend_view([10.0] * 10, 10.0)
        self.assertEqual(t["label"], "UNKNOWN")
        self.assertIsNone(t["sma20"])
        t30 = an.trend_view([100.0 + i for i in range(30)], 131.0)
        self.assertIsNone(t30["sma50"])
        self.assertEqual(t30["label"], "UPTREND")
        self.assertIn("no 50-day SMA", t30["detail"])


class LevelsTests(unittest.TestCase):
    def test_ladder_sorted_merged_and_distances(self):
        days = trading_days(PREV, 25)
        hist = [bar(d, "09:30", 100, 101, 99, 100) for d in days[:-1]] + [bar(days[-1], "09:30", 100, 104, 98, 103)]
        lv = an.key_levels(hist, 102.0, 2.0, sma20=100.15, sma50=None, vwap=101.5)
        names = [x["name"] for x in lv]
        prices = [x["price"] for x in lv]
        self.assertEqual(prices, sorted(prices, reverse=True))
        # The prior day's 104 high is also the 20-day and the 25-day high: one merged row.
        self.assertEqual(lv[0]["name"], "prior day high = 20-day high = 25-day high")
        self.assertAlmostEqual(lv[0]["from_last_pct"], round((104 / 102 - 1) * 100, 2))
        self.assertAlmostEqual(lv[0]["from_last_atr"], 1.0)
        self.assertIn("today's VWAP", names)
        self.assertIn("prior day low = 20-day low = 25-day low", names)
        self.assertNotIn("50-day SMA", " ".join(names))


class RelativeVolumeTests(unittest.TestCase):
    def past(self, n, vol=1000.0, end=PREV):
        return tc.group_sessions(intraday_history(end, n, today_volume=vol, past_volume=vol))

    def test_time_of_day_hand_checked(self):
        today = flat_session(DAY, 2000.0)[:9]                    # 09:30..10:10 completed at 10:17
        r, why = an.time_of_day_rvol(today, self.past(5), 10 * 60 + 17)
        self.assertIsNone(why)
        self.assertEqual(r["through"], "10:15")
        self.assertEqual(r["sessions"], 5)
        self.assertEqual(r["volume"], 18000)                     # 9 bars x 2000
        self.assertEqual(r["baseline"], 9000)                    # 9 bars x 1000
        self.assertAlmostEqual(r["value"], 2.0)

    def test_half_days_excluded_from_baseline(self):
        past = self.past(4)
        past["2026-10-23"] = flat_session("2026-10-23", 5000.0, n_bars=42)  # 09:30-12:55 half day, heavy
        today = flat_session(DAY, 1000.0)[:54]                              # through 13:55
        r, _ = an.time_of_day_rvol(today, past, 14 * 60 + 2)
        self.assertEqual(r["sessions"], 4)
        self.assertAlmostEqual(r["value"], 1.0)

    def test_early_close_today_compares_through_last_bar(self):
        today = flat_session(DAY, 1000.0, n_bars=42)                        # stopped at 12:55
        r, _ = an.time_of_day_rvol(today, self.past(5), 15 * 60)
        self.assertEqual(r["through"], "13:00")
        self.assertAlmostEqual(r["value"], 1.0)

    def test_not_enough_sessions_or_too_early(self):
        today = flat_session(DAY, 1000.0)[:5]
        r, why = an.time_of_day_rvol(today, self.past(2), 10 * 60)
        self.assertIsNone(r)
        self.assertIn("only 2", why)
        r, why = an.time_of_day_rvol(today[:1], self.past(5), 9 * 60 + 33)
        self.assertIsNone(r)

    def test_daily_fallback(self):
        base = rising_daily(PREV, 25, volume=1_000_000)
        r = an.daily_rvol(1_500_000, base)
        self.assertEqual((r["value"], r["method"], r["sessions"]), (1.5, "daily", 20))
        self.assertIsNone(an.daily_rvol(1, base[:3]))


class GapTests(unittest.TestCase):
    def test_gap_up_filled_with_time(self):
        bars = [bar(DAY, "09:30", 105, 106, 103, 104), bar(DAY, "09:35", 104, 104.5, 99.8, 100.5)]
        g = an.gap_view(an.aggregate(bars), 100.0, 2.5, bars)
        self.assertEqual(g["direction"], "up")
        self.assertAlmostEqual(g["gap_pct"], 5.0)
        self.assertAlmostEqual(g["gap_atr"], 2.0)
        self.assertTrue(g["filled"])
        self.assertEqual(g["filled_at"], "09:35")

    def test_gap_down_not_filled_and_flat(self):
        s = bar(DAY, "09:30", 95, 97, 94, 96)
        g = an.gap_view(s, 100.0, None)
        self.assertEqual((g["direction"], g["filled"], g["gap_atr"]), ("down", False, None))
        flat = an.gap_view(bar(DAY, "09:30", 100.1, 101, 99, 100), 100.0, 1.0)
        self.assertEqual(flat["direction"], "flat")
        self.assertIsNone(flat["filled"])
        self.assertIsNone(an.gap_view(s, None, 1.0))


class BriefTests(TempHomeTestCase):
    NOW = f"{DAY}T10:17"

    def data(self, end=DAY, today_daily=True):
        daily = rising_daily(PREV, 60)
        if today_daily:
            daily.append(bar(DAY, "09:30", 160, 163, 158, 161, 2_000_000))
        intraday = intraday_history(end, 21, price=160.0)
        return daily, intraday

    def test_market_open_brief(self):
        daily, intraday = self.data()
        b = an.build_brief("tsla", daily, intraday, at(self.NOW), meta={"longName": "Tesla, Inc."})
        s = b["session"]
        self.assertEqual((b["symbol"], b["name"], b["market"]), ("TSLA", "Tesla, Inc.", "open"))
        self.assertTrue(s["is_today"])
        self.assertEqual((s["source"], s["date"], s["bars_5m"], s["last_bar"]), ("5m", DAY, 10, "10:15"))
        self.assertEqual(b["prior_day"]["date"], PREV)
        self.assertEqual(s["prev_close"], 159.0)                 # last daily close before today
        self.assertEqual(b["relative_volume"]["method"], "time_of_day")
        self.assertAlmostEqual(b["relative_volume"]["value"], 2.0)
        self.assertEqual(b["gap"]["direction"], "up")            # 5m open 160 vs 159
        self.assertIn("today's VWAP", [x["name"] for x in b["levels"]])
        self.assertEqual(b["trend"]["label"], "UPTREND")
        self.assertIsNotNone(b["volatility"]["atr14"])
        self.assertEqual(b["sec"], {"status": "skipped"})
        text = an.render_brief(b)
        for piece in ("BRIEF · TSLA · Tesla, Inc.", "TREND       UPTREND", "REL VOLUME  2.00x", "KEY LEVELS",
                      "> ", "SEC         skipped", tc.DISCLAIMER):
            self.assertIn(piece, text)

    def test_premarket_describes_last_session(self):
        daily, intraday = self.data()
        b = an.build_brief("TSLA", daily, intraday, at(f"{DAY}T08:45"))
        s = b["session"]
        self.assertEqual((b["market"], s["is_today"], s["date"]), ("pre-market", False, PREV))
        self.assertEqual(b["prior_day"]["date"], PREV)           # the levels for today's session
        self.assertEqual(s["prev_close"], 158.0)
        self.assertEqual(b["relative_volume"]["method"], "daily")
        self.assertTrue(any("pre-market" in n for n in b["notes"]))
        self.assertIn("LAST = prior day close", an.render_brief(b))

    def test_after_close_uses_official_daily_bar(self):
        daily, intraday = self.data()
        b = an.build_brief("TSLA", daily, intraday, at(f"{DAY}T17:00"))
        s = b["session"]
        self.assertEqual((b["market"], s["source"], s["last"], s["high"]), ("after-hours", "daily", 161.0, 163.0))
        self.assertEqual(b["relative_volume"]["through"], "16:00")
        self.assertEqual(b["prior_day"]["date"], PREV)           # baselines still exclude today

    def test_weekend(self):
        daily, intraday = self.data()
        b = an.build_brief("TSLA", daily, intraday, at("2026-10-31T12:00"))
        self.assertEqual((b["market"], b["session"]["date"], b["prior_day"]["date"]), ("closed", DAY, DAY))
        self.assertEqual(b["warnings"], [])

    def test_replay_ignores_future_bars(self):
        daily, intraday = self.data()
        later_daily = daily + [bar(d, "09:30", 170, 190, 150, 180, 9e6) for d in ("2026-11-02", "2026-11-03")]
        later_intra = intraday + flat_session("2026-11-02", 99999.0, price=180.0) + [
            bar(DAY, "10:20", 160, 250, 50, 240, 1e7)]                      # after "now" on the same day
        full = an.build_brief("TSLA", later_daily, later_intra, at(self.NOW))
        trimmed = an.build_brief("TSLA", daily, intraday, at(self.NOW))
        for key in ("session", "trend", "levels", "volatility", "relative_volume", "gap", "prior_day"):
            self.assertEqual(full[key], trimmed[key], key)

    def test_replay_drops_the_unfinished_5m_bar(self):
        daily, intraday = self.data()
        def drift(x):                                                     # today's bar k closes at 160 + 0.1k
            c = x.close + (x.ts - et_ts(DAY, "09:30")) / 3000
            return tc.Bar(x.ts, x.open, max(x.high, c), x.low, c, x.volume)
        intraday = [drift(x) if x.date == DAY else x for x in intraday]   # the 10:10 bar closes at 160.80
        # At 10:17 the 10:15 bar ends at 10:20: in a replay its close is the future.
        b = an.build_brief("TSLA", daily, intraday, at(self.NOW), replay=True)
        self.assertEqual((b["session"]["last_bar"], b["session"]["bars_5m"]), ("10:10", 9))
        self.assertAlmostEqual(b["price"], 160.8)
        self.assertEqual(b["relative_volume"]["through"], "10:15")
        live = an.build_brief("TSLA", daily, intraday, at(self.NOW))       # live: Yahoo's bar in progress is "now"
        self.assertEqual(live["session"]["last_bar"], "10:15")
        # Before the first bar completes, a replay must not fall back to today's full-day daily bar.
        early = an.build_brief("TSLA", daily, intraday, at(f"{DAY}T09:33"), replay=True)
        self.assertEqual((early["session"]["is_today"], early["session"]["date"]), (False, PREV))
        self.assertEqual(early["warnings"], [])
        self.assertTrue(any("first 5-minute bar" in n for n in early["notes"]), early["notes"])

    def test_intraday_missing_falls_back_to_daily(self):
        daily, _ = self.data()
        b = an.build_brief("TSLA", daily, [], at(self.NOW), intraday_error="HTTP 403", replay=True)
        self.assertEqual(b["session"]["source"], "daily")
        self.assertEqual(b["relative_volume"]["method"], "daily")
        self.assertTrue(b["relative_volume"]["partial_day"])
        self.assertTrue(any("5-minute bars unavailable" in n for n in b["notes"]))
        self.assertTrue(any(w.startswith("replay:") for w in b["warnings"]))
        self.assertTrue(b["sources"]["intraday"].startswith("error"))

    def test_too_early_for_relative_volume(self):
        daily, intraday = self.data()
        b = an.build_brief("TSLA", daily, intraday, at(f"{DAY}T09:33"))
        self.assertIsNone(b["relative_volume"])
        self.assertTrue(any("09:35" in n for n in b["notes"]))
        self.assertNotIn("REL VOLUME", an.render_brief(b))

    def test_warnings_for_missing_or_stale_bars(self):
        daily, _ = self.data(today_daily=False)
        b = an.build_brief("TSLA", daily, [], at(self.NOW))      # market hours, nothing for today
        self.assertTrue(any("no bars for today" in w for w in b["warnings"]))
        old = rising_daily("2026-10-16", 30)
        b = an.build_brief("TSLA", old, [], at(f"{DAY}T08:00"))
        self.assertTrue(any("stale" in w for w in b["warnings"]))
        b = an.build_brief("TSLA", old, [], at(self.NOW))         # delisted: both warnings in market hours
        self.assertEqual(len(b["warnings"]), 2)
        halted = intraday_history(DAY, 21)
        halted = [x for x in halted if x.date != DAY or x.time < "10:00"]
        b = an.build_brief("TSLA", daily, halted, at(f"{DAY}T11:00"))
        self.assertTrue(any("no 5-minute bars since 10:00" in w for w in b["warnings"]))

    def test_penny_stock_keeps_sub_cent_precision(self):
        daily = daily_bars(PREV, [0.5 + 0.001 * i for i in range(30)], wick_pct=0.02)
        daily.append(bar(DAY, "09:30", 0.5523, 0.5601, 0.5488, 0.5555, 2e6))
        b = an.build_brief("PENY", daily, [], at(self.NOW))
        self.assertAlmostEqual(b["session"]["change"], 0.5555 - 0.529, places=4)
        self.assertIn("+0.0265 (+5.01%)", an.render_brief(b))

    def test_short_history_and_bad_data(self):
        b = an.build_brief("NEW", rising_daily(PREV, 3), [], at(self.NOW))
        self.assertEqual(b["trend"]["label"], "UNKNOWN")
        self.assertIsNone(b["volatility"]["atr14"])
        self.assertIn("ATR(14) needs 14", an.render_brief(b))
        ipo = an.build_brief("NEW", [bar(DAY, "09:30", 20, 25, 19, 24, 5e6)], [], at(self.NOW))
        self.assertIsNone(ipo["gap"])
        self.assertTrue(any("new listing" in n for n in ipo["notes"]))
        an.render_brief(ipo)
        with self.assertRaises(tc.FetchError):
            an.build_brief("X", [], [], at(self.NOW))
        with self.assertRaises(ValueError):
            an.build_brief("X", rising_daily("2026-11-20", 5), [], at(self.NOW))


# ---------------------------------------------------------------------------
# SEC
# ---------------------------------------------------------------------------

def sec_tickers():
    return {"0": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."}}


def sec_submissions(dates=None, forms=None, items=None, files=None):
    dates = dates or ["2026-10-28", "2026-10-27", "2026-10-22", "2026-10-12", "2026-10-05", "2026-09-01"]
    forms = forms or ["4", "144", "8-K", "S-3ASR", "4", "8-K"]
    items = items or ["", "", "2.02,9.01", "", "", "5.02"]
    n = len(dates)
    return {"name": "Tesla, Inc.", "sicDescription": "Motor Vehicles & Passenger Car Bodies",
            "filings": {"recent": {"accessionNumber": [f"0001-26-{i:06d}" for i in range(n)],
                                   "filingDate": dates, "acceptanceDateTime": ["2026-10-28T16:05:00.000Z"] * n,
                                   "form": forms, "items": items, "primaryDocument": ["d.htm"] * n,
                                   "primaryDocDescription": [""] * n},
                        "files": files or []}}


def shares_concept(*rows):
    return {"cik": 1318605, "taxonomy": "dei", "tag": "EntityCommonStockSharesOutstanding", "units": {"shares": [
        {"end": e, "val": v, "accn": a, "form": f, "filed": fl} for e, v, a, f, fl in rows]}}


class SecTests(TempHomeTestCase):
    def test_classify_filings(self):
        c = an.classify_filing
        k = c({"form": "8-K", "items": ["2.02", "9.01"]})
        self.assertEqual((k["kind"], k["label"], k["dilution"]), ("event", "Results of operations (earnings)", False))
        self.assertTrue(c({"form": "8-K", "items": ["3.02"]})["dilution"])
        self.assertEqual(c({"form": "8-K", "items": ["9.01"]})["label"], "Financial statements and exhibits")
        for form in ("S-3ASR", "S-1/A", "F-3", "424B5", "424B2", "S-1MEF"):
            self.assertEqual((c({"form": form})["kind"], c({"form": form})["dilution"]), ("offering", True), form)
        self.assertEqual(c({"form": "SCHEDULE 13D"})["label"], "activist / 5%+ holder (13D)")
        self.assertEqual(c({"form": "SC 13G/A"})["kind"], "ownership")
        self.assertEqual(c({"form": "10-Q"})["label"], "quarterly report")
        self.assertEqual(c({"form": "4/A"})["label"], "form4")
        self.assertEqual(c({"form": "S-8"})["kind"], "other")          # employee plans are not flagged
        self.assertEqual(c({"form": "DEF 14A"})["kind"], "other")

    def test_summarize_window_and_counts(self):
        filings = [{"form": "4", "filed": "2026-11-02"},               # after "today" (replay): ignored
                   {"form": "4", "filed": DAY}, {"form": "144", "filed": "2026-10-20"},
                   {"form": "8-K", "filed": "2026-10-22", "items": ["2.02"]},
                   {"form": "424B5", "filed": "2026-10-10"}, {"form": "DEF 14A", "filed": "2026-10-09"},
                   {"form": "10-Q", "filed": "2026-09-01"}]               # before the window
        s = an.summarize_filings(filings, "2026-09-30", DAY)
        self.assertEqual([x["form"] for x in s["notable"]], ["8-K", "424B5"])
        self.assertEqual(s["insider"], {"form4": 1, "form144": 1, "form3": 0, "form5": 0})
        self.assertEqual(s["other"], {"DEF 14A": 1})
        self.assertEqual(s["dilution_flags"], 1)
        self.assertTrue(s["earnings_8k"])

    def test_latest_shares(self):
        c = shares_concept(("2026-04-15", 3_200_000_000, "a1", "10-Q", "2026-04-23"),
                           ("2026-10-15", 3_215_000_000, "a3", "10-Q", "2026-10-23"),
                           ("2026-12-15", 9, "a4", "10-K", "2027-01-30"))        # filed after today
        self.assertEqual(an.latest_shares(c, DAY), {"shares": 3_215_000_000, "as_of": "2026-10-15",
                                                    "filed": "2026-10-23", "form": "10-Q", "classes": 1})
        multi = shares_concept(("2026-10-20", 5_800_000_000, "g1", "10-Q", "2026-10-28"),
                               ("2026-10-20", 860_000_000, "g1", "10-Q", "2026-10-28"),
                               ("2026-10-20", 5_500_000_000, "g1", "10-Q", "2026-10-28"))
        m = an.latest_shares(multi, DAY)
        self.assertEqual((m["shares"], m["classes"]), (12_160_000_000, 3))
        self.assertIsNone(an.latest_shares({"units": {}}, DAY))
        self.assertIsNone(an.latest_shares([1, 2], DAY))
        self.assertIsNone(an.latest_shares({"units": {"shares": [{"end": "2026-10-01", "val": None}]}}, DAY))

    def test_fetch_sec_full_and_degraded(self):
        routes = {"company_tickers.json": sec_tickers(), SUBS_URL: sec_submissions(),
                  "companyconcept": shares_concept(("2026-10-15", 3_215_000_000, "a3", "10-Q", "2026-10-23"))}
        with fake_http(routes):
            s = an.fetch_sec("TSLA", DAY, 30)
        self.assertEqual((s["status"], s["cik"], s["name"]), ("ok", CIK, "Tesla, Inc."))
        self.assertEqual(s["shares"]["shares"], 3_215_000_000)
        self.assertEqual([x["form"] for x in s["filings"]["notable"]], ["8-K", "S-3ASR"])
        self.assertEqual(s["filings"]["insider"]["form4"], 2)
        routes["companyconcept"] = tc.FetchError(f"HTTP 404 for {SHARES_URL} - not found")
        with fake_http(routes):
            s = an.fetch_sec("TSLA", DAY, 30)
        self.assertEqual((s["status"], s["shares"]), ("ok", None))
        self.assertTrue(any("not reported" in n for n in s["notes"]))
        routes["companyconcept"] = tc.FetchError("HTTP 403 for data.sec.gov - access denied")
        with fake_http(routes):
            s = an.fetch_sec("TSLA", DAY, 30)
        self.assertEqual(s["status"], "partial")
        self.assertTrue(any("403" in e for e in s["errors"]))
        with fake_http({"company_tickers.json": sec_tickers()}):
            self.assertEqual(an.fetch_sec("SPY", DAY, 30)["status"], "not_found")
        with fake_http({"company_tickers.json": tc.FetchError("HTTP 403 for https://www.sec.gov/files/x")}):
            s = an.fetch_sec("TSLA", DAY, 30)
        self.assertEqual(s["status"], "error")
        self.assertIn("www.sec.gov", s["errors"][0])

    def test_odd_ticker_map_payload_degrades_instead_of_crashing(self):
        # Valid JSON of the wrong shape (an error object, not the ticker map) must not raise out of the brief.
        with fake_http({"company_tickers.json": {"error": "rate limited"}}):
            s = an.fetch_sec("TSLA", DAY, 30)
        self.assertEqual(s["status"], "error")
        self.assertTrue(s["errors"])

    def test_market_cap_caveats(self):
        daily = rising_daily(PREV, 30) + [bar(DAY, "09:30", 130, 131, 129, 130.0, 1e6)]
        base = {"status": "ok", "cik": CIK, "name": "X", "sic": None, "filings": None, "errors": [], "notes": []}
        # A 20-F counts ordinary shares; an ADR's price is per ADS (often several shares): no estimate.
        adr = dict(base, shares={"shares": 25_900_000_000, "as_of": "2026-03-31", "filed": "2026-04-15",
                                 "form": "20-F", "classes": 1})
        b = an.build_brief("TSM", daily, [], at(f"{DAY}T17:00"), sec=adr)
        self.assertIsNone(b["sec"]["shares"].get("market_cap_approx"))
        self.assertTrue(any("ADR" in n for n in b["notes"]), b["notes"])
        self.assertNotIn("market cap ≈", an.render_brief(b))
        # Several classes summed at one ticker's price: say so (BRK-A and BRK-B trade ~1,500x apart).
        multi = dict(base, shares={"shares": 1_440_000_000, "as_of": "2026-10-20", "filed": "2026-10-28",
                                   "form": "10-Q", "classes": 2})
        b = an.build_brief("BRK.B", daily, [], at(f"{DAY}T17:00"), sec=multi)
        self.assertEqual(b["sec"]["shares"]["market_cap_approx"], 1_440_000_000 * 130)
        self.assertIn("if all 2 classes traded at 130.00", an.render_brief(b))
        one = dict(base, shares={"shares": 1_000, "as_of": "2026-10-20", "filed": "2026-10-28", "form": "10-Q",
                                 "classes": 1})
        self.assertIn("market cap ≈ $130.00K", an.render_brief(an.build_brief("X", daily, [], at(f"{DAY}T17:00"),
                                                                               sec=one)))

    def test_truncated_recent_list_is_noted(self):
        subs = sec_submissions(dates=["2026-10-28", "2026-10-25"], forms=["424B2", "424B2"], items=["", ""],
                               files=[{"name": "CIK0001318605-submissions-001.json"}])
        with fake_http({"company_tickers.json": sec_tickers(), SUBS_URL: subs, "companyconcept": {"units": {}}}):
            s = an.fetch_sec("TSLA", DAY, 30)
        self.assertTrue(any("only reaches back to 2026-10-25" in n for n in s["notes"]))


class GatherTests(TempHomeTestCase):
    NOW = f"{DAY}T10:17"

    def test_gather_degrades_when_5m_and_sec_fail(self):
        daily = rising_daily(PREV, 60) + [bar(DAY, "09:30", 160, 161, 159, 160.5, 1e6)]
        with fake_bars({("TSLA", "1d"): daily}), fake_http({"sec.gov": tc.FetchError("HTTP 403 for https://www.sec.gov/x")}):
            b = an.gather_brief("TSLA", tc.now_et())
        self.assertEqual(b["sec"]["status"], "error")
        self.assertTrue(b["sources"]["intraday"].startswith("error"))
        self.assertIn("SEC         unavailable", an.render_brief(b))

    def test_gather_requires_daily_bars(self):
        with fake_bars({("TSLA", "5m"): intraday_history(DAY, 5)}):
            with self.assertRaises(tc.FetchError):
                an.gather_brief("TSLA", tc.now_et(), with_sec=False)


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def trade(tid, pnl, opened, closed, setup="orb", side="long", symbol="TSLA", entry=100.0, stop=99.0, qty=50,
          exit_reason="target", r="auto", status="closed", **kw):
    """A closed broker-shaped ledger trade. Default initial risk = |100 - 99| x 50 = $50."""
    t = {"id": tid, "ticket_id": f"TKT-{tid}", "symbol": symbol, "side": side, "qty": qty, "entry_price": entry,
         "stop": stop, "target": None, "setup": setup, "opened_at": opened, "status": status, "mode": "paper"}
    if status == "closed":
        t.update(pnl=pnl, closed_at=closed, exit_reason=exit_reason,
                 exit_price=entry + (pnl / qty if side == "long" else -pnl / qty),
                 r_multiple=round(pnl / (abs(entry - stop) * qty), 2) if r == "auto" else r)
    t.update(kw)
    return t


def ts(day, hhmm):
    return f"{day}T{hhmm}:00-04:00"


class ReviewTests(TempHomeTestCase):
    NOW = f"{DAY}T16:30"

    def four(self):
        return {"tickets": [], "trades": [
            trade("a", 100, ts("2026-10-26", "09:45"), ts("2026-10-26", "10:15")),
            trade("b", -50, ts("2026-10-27", "10:40"), ts("2026-10-27", "11:40"), setup="vwap", exit_reason="stop"),
            trade("c", -50, ts("2026-10-28", "09:50"), ts("2026-10-28", "10:00"), side="short", entry=100.0,
                  stop=101.0, exit_reason="stop"),
            trade("d", 25, ts("2026-10-29", "14:10"), ts("2026-10-29", "15:55"), exit_reason="eod")]}

    def test_hand_checked_stats(self):
        r = an.build_review(self.four(), 30, DAY, account=25_000)
        self.assertEqual((r["count"], r["wins"], r["losses"], r["scratches"]), (4, 2, 2, 0))
        self.assertEqual(r["win_rate_pct"], 50.0)
        self.assertEqual(r["net_pnl"], 25.0)
        self.assertEqual(r["net_pnl_pct_account"], 0.1)
        self.assertEqual(r["profit_factor"], 1.25)                       # 125 / 100
        self.assertAlmostEqual(r["expectancy_r"], 0.125)                 # (2 - 1 - 1 + 0.5) / 4
        self.assertEqual((r["avg_win"], r["avg_loss"]), (62.5, -50.0))
        self.assertAlmostEqual(r["avg_win_r"], 1.25)
        self.assertAlmostEqual(r["avg_loss_r"], -1.0)
        self.assertAlmostEqual(r["breakeven_win_rate_pct"], 44.4)        # 1 / (1 + 1.25)
        self.assertEqual(r["max_drawdown"], {"usd": 100.0, "peak_at": ts("2026-10-26", "10:15"),
                                             "trough_at": ts("2026-10-28", "10:00"), "pct_account": 0.4})
        self.assertEqual((r["best_trade"]["id"], r["worst_trade"]["id"]), ("a", "b"))
        self.assertEqual((r["max_win_streak"], r["max_loss_streak"]), (1, 2))
        h = r["avg_hold_minutes"]                                        # holds 30, 60, 10, 105 min
        self.assertAlmostEqual(h["all"], 51.25, delta=0.051)
        self.assertEqual((h["wins"], h["losses"]), (67.5, 35.0))

    def test_breakdowns(self):
        r = an.build_review(self.four(), 30, DAY)
        by = lambda k: {b["key"]: b for b in r[k]}  # noqa: E731
        self.assertEqual(by("by_setup")["orb"]["n"], 3)
        self.assertEqual(by("by_setup")["vwap"]["net_pnl"], -50.0)
        self.assertEqual([b["key"] for b in r["by_side"]], ["long", "short"])
        self.assertEqual([b["key"] for b in r["by_hour"]], ["09:00", "10:00", "14:00"])
        self.assertEqual({k: v["n"] for k, v in by("by_window").items()},
                         {"09:30-10:30": 2, "10:30-14:00": 1, "14:00-16:00": 1})
        self.assertEqual([b["key"] for b in r["by_exit"]], ["target", "stop", "time stop"])
        self.assertEqual(r["by_time_machine"][0]["key"], "not checked")
        self.assertIsNone(r["time_machine_check"])

    def test_time_machine_verdict_from_trade_or_ticket(self):
        led = self.four()
        led["trades"][0]["time_machine"] = {"verdict": "ODDS LEAN YOUR WAY", "target_first_pct": 40.0,
                                            "stop_first_pct": 30.0}
        led["tickets"].append({"id": "TKT-b", "time_machine": {"verdict": "ODDS LEAN AGAINST YOU",
                                                               "target_first_pct": 20.0, "stop_first_pct": 50.0}})
        r = an.build_review(led, 30, DAY)
        tm = {b["key"]: b["n"] for b in r["by_time_machine"]}
        self.assertEqual(tm, {"leaned your way": 1, "leaned against you": 1, "not checked": 2})
        c = r["time_machine_check"]
        self.assertEqual((c["n"], c["predicted_target_first_pct"], c["actual_target_pct"], c["actual_stop_pct"]),
                         (2, 30.0, 50.0, 50.0))
        self.assertTrue(any("leaned against you (net -$50.00)" in t for t in r["takeaways"]))

    def test_window_symbol_open_and_malformed(self):
        led = self.four()
        led["trades"] += [
            trade("old", 500, ts("2026-09-01", "10:00"), ts("2026-09-01", "11:00")),
            trade("open", None, ts(DAY, "10:00"), None, status="open"),
            {"id": "broken", "status": "closed", "symbol": "TSLA"},      # no P&L, no prices
            "garbage",
            trade("nv", 30, ts(DAY, "10:00"), ts(DAY, "10:30"), symbol="NVDA")]
        r = an.build_review(led, 30, DAY)
        self.assertEqual((r["count"], r["open_trades"], r["skipped_rows"], r["older_closed_trades"]), (5, 1, 2, 1))
        r = an.build_review(led, 1, DAY, symbol="NVDA")
        self.assertEqual((r["count"], r["since"]), (1, DAY))
        self.assertEqual(an.build_review(led, 90, DAY)["count"], 6)

    def test_missing_pnl_and_r_are_recomputed(self):
        t = trade("x", 0, ts(DAY, "09:40"), ts(DAY, "10:00"))
        t.update(pnl=None, r_multiple=None, exit_price=102.0)
        t2 = trade("y", -25, "2026-10-30T09:45:00", "2026-10-30T14:00:00Z", side="short", entry=50.0, stop=50.5)
        t2["r_multiple"] = None
        rows, _ = an.journal_rows({"tickets": [], "trades": [t, t2]}, "2026-10-01", DAY)
        by_id = {r["id"]: r for r in rows}
        self.assertEqual((by_id["x"]["pnl"], by_id["x"]["r"]), (100.0, 2.0))
        self.assertEqual(by_id["y"]["r"], -1.0)
        self.assertEqual(by_id["y"]["entry_minute"], 9 * 60 + 45)       # naive stamp = ET wall clock
        self.assertEqual(by_id["y"]["hold_minutes"], 15.0)              # 14:00Z = 10:00 ET

    def test_setup_labels_with_spaces_are_normalized(self):
        trades = [trade(f"g{i}", 50, ts(d, "09:45"), ts(d, "10:00"), setup="Gap and Go")
                  for i, d in enumerate(trading_days(PREV, 3))]
        trades += [trade(f"l{i}", -50, ts(d, "11:45"), ts(d, "12:00"), setup="vwap", exit_reason="stop")
                   for i, d in enumerate(trading_days(PREV, 3))]
        r = an.build_review({"tickets": [], "trades": trades}, 30, DAY)
        self.assertEqual([b["key"] for b in r["by_setup"]], ["gap-and-go", "vwap"])
        self.assertTrue(any("Your gap-and-go longs entered 09:30-10:30 are your only positive bucket" in t
                            for t in r["takeaways"]), r["takeaways"])

    def test_zero_trades(self):
        r = an.build_review({"tickets": [], "trades": []}, 30, DAY)
        self.assertEqual((r["count"], r["takeaways"], r["profit_factor"]), (0, [], None))
        text = an.render_review(r)
        self.assertIn("No closed paper trades", text)
        self.assertIn(tc.DISCLAIMER, text)
        with self.assertRaises(tc.StateError):
            an.build_review({"tickets": [], "trades": {"a": 1}}, 30, DAY)

    def test_no_losses_profit_factor(self):
        led = {"tickets": [], "trades": [trade("a", 50, ts(DAY, "09:40"), ts(DAY, "10:00"))]}
        r = an.build_review(led, 30, DAY)
        self.assertIsNone(r["profit_factor"])
        self.assertIn("∞ (no losses)", an.render_review(r))
        self.assertTrue(any("no losing trades" in t for t in r["takeaways"]))

    def test_takeaway_only_positive_bucket_and_time_of_day(self):
        days = trading_days(PREV, 12)
        trades = []
        for i, d in enumerate(days[:4]):                                 # ORB longs at the open: +1R, +2R, -1R, +2R
            trades.append(trade(f"o{i}", [50, 100, -50, 100][i], ts(d, "09:45"), ts(d, "10:20")))
        for i, d in enumerate(days[4:8]):                                # midday VWAP longs: all losers
            trades.append(trade(f"v{i}", -50, ts(d, "11:30"), ts(d, "12:30"), setup="vwap", exit_reason="stop"))
        for i, d in enumerate(days[8:11]):                               # midday VWAP shorts: losers
            trades.append(trade(f"s{i}", -25, ts(d, "12:00"), ts(d, "13:00"), setup="vwap", side="short",
                                entry=100.0, stop=101.0, exit_reason="manual"))
        r = an.build_review({"tickets": [], "trades": trades}, 30, DAY)
        t = "\n".join(r["takeaways"])
        self.assertIn("Your orb longs entered 09:30-10:30 are your only positive bucket", t)
        self.assertIn("Time of day matters here: entries 09:30-10:30 ET", t)
        self.assertIn("11 trades is still a small sample", t)
        self.assertIn("None of these trades carried a Time Machine verdict", t)
        self.assertIn("Manual exits average -0.50R", t)

    def test_takeaway_losers_held_longer_and_big_losses(self):
        trades = []
        for i, d in enumerate(trading_days(PREV, 6)):
            if i % 2:
                trades.append(trade(f"w{i}", 100, ts(d, "09:45"), ts(d, "10:00")))
            else:
                trades.append(trade(f"l{i}", -75, ts(d, "09:45"), ts(d, "11:15"), exit_reason="manual"))
        r = an.build_review({"tickets": [], "trades": trades}, 30, DAY)
        t = "\n".join(r["takeaways"])
        self.assertIn("You hold losers longer than winners (90 vs 15 min", t)
        self.assertIn("Your average loss is -1.50R", t)
        self.assertIn("Only 6 closed trades", t)

    def test_breakeven_comparison_leaves_scratches_out(self):
        # 4 x +1.2R, 4 x -1R, 2 scratches: +0.08R per trade. Break-even for a 1.2 payoff is 45.5% of
        # *decided* trades; these win 4 of 8 = 50%, so they are above it (40% counts the scratches).
        pnls = [60, 60, 60, 60, -50, -50, -50, -50, 0, 0]
        trades = [trade(f"t{i}", p, ts(d, "09:45"), ts(d, "10:00"), exit_reason="target" if p > 0 else "stop")
                  for i, (d, p) in enumerate(zip(trading_days(PREV, 10), pnls))]
        r = an.build_review({"tickets": [], "trades": trades}, 30, DAY)
        self.assertGreater(r["expectancy_r"], 0)
        self.assertEqual((r["win_rate_pct"], r["win_rate_ex_scratches_pct"]), (40.0, 50.0))
        t = "\n".join(r["takeaways"])
        self.assertNotIn("you are below it", t)
        self.assertIn("Leaving out 2 scratches, you win 50% of trades", t)
        self.assertIn("you are above it", t)

    def test_render_review_text(self):
        text = an.render_review(an.build_review(self.four(), 30, DAY, account=25_000))
        for piece in ("JOURNAL REVIEW · last 30 days (2026-10-01 to 2026-10-30) · 4 closed paper trades",
                      "Net P&L +$25.00 (+0.10% of the $25,000.00 paper account)", "profit factor 1.25",
                      "Max drawdown -$100.00", "TAKEAWAYS", "setup", "entry window ET", tc.DISCLAIMER):
            self.assertIn(piece, text)


# ---------------------------------------------------------------------------
# CLI (subprocess) and SKILL.md
# ---------------------------------------------------------------------------

def write_brief_fixtures(directory, symbol="TSLA"):
    intraday = random_walk_history(DAY, 21, start_price=350.0, vol=0.003, seed=3)
    sessions = tc.group_sessions(intraday)
    early = daily_bars(trading_days(min(sessions), 70)[-2], [300 + i * 0.5 for i in range(69)])
    agg = [tc.Bar(et_ts(d, "09:30"), s[0].open, max(b.high for b in s), min(b.low for b in s), s[-1].close,
                  sum(b.volume for b in s)) for d, s in sessions.items()]
    write_fixture(directory, yahoo_chart_url(symbol, "1d", "6mo"),
                  yahoo_payload(early + agg, symbol, longName="Tesla, Inc."))
    write_fixture(directory, yahoo_chart_url(symbol, "5m", "1mo"), yahoo_payload(intraday, symbol))
    write_fixture(directory, tc.SEC_TICKERS_URL, sec_tickers())
    write_fixture(directory, SUBS_URL, sec_submissions())
    write_fixture(directory, SHARES_URL, shares_concept(("2026-10-15", 3_215_000_000, "a3", "10-Q", "2026-10-23")))


class CliTests(TempHomeTestCase):
    def env(self, fixtures, now=f"{DAY}T10:17"):
        return {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(fixtures), "DAYTRADER_NOW": now}

    def test_brief_cli_with_fixtures(self):
        fx = self.home / "fx"
        write_brief_fixtures(fx)
        p = helpers.run_cli(SCRIPT, "brief", "TSLA", env=self.env(fx))
        self.assertEqual(p.returncode, 0, p.stderr)
        for piece in ("BRIEF · TSLA · Tesla, Inc. · Fri 2026-10-30 10:17 ET · market open", "TREND", "VOLATILITY",
                      "REL VOLUME", "through 10:15 ET", "KEY LEVELS", "Results of operations (earnings)",
                      "shelf registration · possible dilution", "2 Form 4", "shares outstanding 3.21B"):
            self.assertIn(piece, p.stdout)
        j = helpers.run_cli(SCRIPT, "brief", "tsla", "--json", env=self.env(fx))
        self.assertEqual(j.returncode, 0, j.stderr)
        d = json.loads(j.stdout)
        self.assertEqual((d["symbol"], d["market"], d["sec"]["status"]), ("TSLA", "open", "ok"))
        self.assertEqual(d["relative_volume"]["method"], "time_of_day")
        self.assertEqual(d["session"]["date"], DAY)
        self.assertGreater(d["sec"]["shares"]["market_cap_approx"], 0)
        nosec = helpers.run_cli(SCRIPT, "brief", "TSLA", "--no-sec", env=self.env(fx, "2026-10-31T09:00"))
        self.assertEqual(nosec.returncode, 0, nosec.stderr)
        self.assertIn("LAST SESSION 2026-10-30", nosec.stdout)

    def test_no_data_is_one_line_exit_2(self):
        empty = self.home / "nofx"
        empty.mkdir()
        p = helpers.run_cli(SCRIPT, "brief", "TSLA", env=self.env(empty))
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.startswith("data error: "), p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1)
        self.assertIn("finance.yahoo.com", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_review_cli(self):
        led = ReviewTests.four(None)
        (self.home / "ledger.json").write_text(json.dumps(led))
        p = helpers.run_cli(SCRIPT, "review", env=self.env(self.home, f"{DAY}T16:30"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("4 closed paper trades", p.stdout)
        j = helpers.run_cli(SCRIPT, "review", "--days", "2", "--json", env=self.env(self.home, f"{DAY}T16:30"))
        self.assertEqual(j.returncode, 0, j.stderr)
        self.assertEqual(json.loads(j.stdout)["count"], 1)               # only 10-29 closes in [10-29, 10-30]
        (self.home / "ledger.json").write_text("{broken")
        bad = helpers.run_cli(SCRIPT, "review", env=self.env(self.home))
        self.assertEqual(bad.returncode, 1)
        self.assertTrue(bad.stderr.startswith("error: "))

    def test_bad_flags_exit_1(self):
        for args in (["review", "--days", "0"], ["brief", "TS LA"], ["brief"], ["brief", "TSLA", "--live"], []):
            p = helpers.run_cli(SCRIPT, *args, env=self.env(self.home))
            self.assertEqual(p.returncode, 1, (args, p.stderr))
            self.assertNotIn("Traceback", p.stderr)


class SkillDocTests(unittest.TestCase):
    def test_skill_md_contract_and_commands_parse(self):
        md = SKILL_MD.read_text(encoding="utf-8")
        self.assertTrue(md.startswith("---\nname: analyst\n"))
        desc = re.search(r"description: >-\n(.*?)\n---", md, re.S).group(1)
        desc = " ".join(line.strip() for line in desc.splitlines())
        self.assertTrue(desc.startswith("Day-trading desk:"))
        self.assertLessEqual(len(desc), 1024)
        self.assertIn("> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder "
                      "(`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.", md)
        for section in ("## Run", "## How it works", "## Reading the output", "## Next step on the desk", "## Rules"):
            self.assertIn(section, md)
        self.assertIn("allow-listed", md)
        cmds = re.findall(r"python3 scripts/analyst\.py ([^\n`#]*)", md)
        self.assertGreaterEqual(len(cmds), 5)
        for c in cmds:
            an.build_parser().parse_args(shlex.split(c))   # SystemExit (test error) if a documented flag is wrong


if __name__ == "__main__":
    unittest.main()
