import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

# Lets `python3 -m unittest tests.test_common` run on its own (discover adds tests/ itself).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (ROOT, SKILLS, TempHomeTestCase, bars_from_closes, daily_bars, et_ts,  # noqa: E402
                     crypto_history, utc_ts, write_coinbase_fixtures, write_coinbase_products,
                     write_coinbase_quote, write_binance_fixtures, write_binance_products,
                     write_binance_quote,
                     fake_http, random_walk_history, tc, write_fixture, yahoo_chart_url,
                     yahoo_payload)


class TimeTests(unittest.TestCase):
    def test_eastern_conversion_across_dst(self):
        # 2026-10-30 is EDT (UTC-4); 2026-11-02 is EST (UTC-5).
        self.assertEqual(tc.to_et(datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc).timestamp()).strftime("%H:%M"), "09:30")
        self.assertEqual(tc.to_et(datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc).timestamp()).strftime("%H:%M"), "09:30")

    def test_fallback_dst_rule_matches_zoneinfo(self):
        if tc.ET is None:
            self.skipTest("zoneinfo unavailable")
        for y in (2024, 2025, 2026, 2027):
            for month in range(1, 13):
                for day in (1, 7, 8, 14, 15, 28):
                    for hour in (0, 6, 7, 12):
                        u = datetime(y, month, day, hour, tzinfo=timezone.utc)
                        self.assertEqual(tc._us_eastern_offset(u), u.astimezone(tc.ET).utcoffset(), u)

    def test_parse_hhmm(self):
        self.assertEqual(tc.parse_hhmm("10:15"), 615)
        self.assertEqual(tc.hhmm(615), "10:15")
        for bad in ("25:00", "10:60", "1015", ""):
            with self.assertRaises(ValueError):
                tc.parse_hhmm(bad)

    def test_now_override(self):
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": "2026-10-30T10:15"}):
            n = tc.now_et()
            self.assertEqual((n.hour, n.minute), (10, 15))
            self.assertEqual(tc.today_et(), "2026-10-30")
            self.assertTrue(tc.is_market_open())
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": "2026-10-31T10:15"}):  # Saturday
            self.assertFalse(tc.is_market_open())

    def test_regular_session_and_grouping(self):
        bars = bars_from_closes("2026-10-29", [10, 11, 12], start="09:25") + \
            bars_from_closes("2026-10-30", [12, 13], start="15:55")
        groups = tc.group_sessions(bars)
        self.assertEqual(list(groups), ["2026-10-29", "2026-10-30"])
        self.assertEqual([b.time for b in groups["2026-10-29"]], ["09:30", "09:35"])
        self.assertEqual([b.time for b in groups["2026-10-30"]], ["15:55"])


class YahooParseTests(unittest.TestCase):
    def test_parse_drops_null_rows_and_repairs_wicks(self):
        payload = yahoo_payload(bars_from_closes("2026-10-30", [100, 101, 102]))
        q = payload["chart"]["result"][0]["indicators"]["quote"][0]
        q["close"][1] = None
        q["high"][2] = 50.0  # below the close: repaired to max(o, c)
        bars, meta = tc.parse_yahoo_chart(payload)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[1].high, 102)
        self.assertEqual(meta["symbol"], "TEST")

    def test_parse_error_payload(self):
        with self.assertRaises(tc.FetchError):
            tc.parse_yahoo_chart({"chart": {"result": None, "error": {"code": "Not Found", "description": "No data"}}})

    def test_yahoo_symbol(self):
        self.assertEqual(tc.yahoo_symbol("brk.b"), "BRK-B")


class IndicatorTests(unittest.TestCase):
    def test_sma_ema(self):
        self.assertEqual(tc.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        e = tc.ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[2], 2.0)
        self.assertAlmostEqual(e[3], 3.0)

    def test_rsi_bounds(self):
        up = tc.rsi([float(i) for i in range(30)])
        self.assertEqual(up[-1], 100.0)
        mixed = tc.rsi([10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10])
        self.assertTrue(40 < mixed[-1] < 60)

    def test_atr_and_vwap(self):
        bars = bars_from_closes("2026-10-30", [10] * 20, wick=0.5)
        self.assertAlmostEqual(tc.atr(bars, 14), 1.0)
        self.assertIsNone(tc.atr(bars[:5], 14))
        v = tc.vwap(bars_from_closes("2026-10-30", [10, 20]))
        self.assertAlmostEqual(v[0], 10.0)
        self.assertAlmostEqual(v[1], (10 + (20 + 10 + 20) / 3) / 2)


class SymbolTests(unittest.TestCase):
    NASDAQ = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
        "QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N\n"
        "ABCDW|ABCD Corp - Warrants|S|N|N|100|N|N\n"
        "ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N\n"
        "File Creation Time: 1030202606:01|||||||\n")
    OTHER = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        "BRK.B|Berkshire Hathaway Inc. Class B|N|BRK.B|N|100|N|BRK.B\n"
        "ABR$D|Arbor Realty Trust 6.375% Series D Preferred Stock|N|ABRpD|N|100|N|ABR-D\n"
        "BABA|Alibaba Group Holding Limited American Depositary Shares|N|BABA|N|100|N|BABA\n"
        "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY\n"
        "File Creation Time: 1030202606:01|||||||\n")

    def test_universe_filters(self):
        with fake_http({"nasdaqlisted": self.NASDAQ, "otherlisted": self.OTHER}):
            syms = [r["symbol"] for r in tc.us_symbols(ttl=0)]
            self.assertEqual(syms, ["AAPL", "BRK.B", "BABA"])
            with_etfs = [r["symbol"] for r in tc.us_symbols(include_etfs=True, ttl=0)]
            self.assertEqual(with_etfs, ["AAPL", "QQQ", "BRK.B", "BABA", "SPY"])


class SecTests(TempHomeTestCase):
    def test_recent_filings(self):
        tickers = {"0": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
                   "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"}}
        subs = {"filings": {"recent": {
            "accessionNumber": ["0001-26-000002", "0001-26-000001"],
            "filingDate": ["2026-10-22", "2026-09-01"],
            "acceptanceDateTime": ["2026-10-22T16:05:00.000Z", "2026-09-01T08:00:00.000Z"],
            "form": ["8-K", "4"],
            "items": ["2.02,9.01", ""],
            "primaryDocument": ["tsla-8k.htm", "form4.xml"],
            "primaryDocDescription": ["8-K", "4"]}}}
        with fake_http({"company_tickers.json": tickers, "CIK0001318605.json": subs}):
            self.assertEqual(tc.sec_cik("brk.b"), "0001067983")
            f = tc.sec_recent_filings("TSLA", forms={"8-K"})
            self.assertEqual(len(f), 1)
            self.assertEqual(f[0]["items"], ["2.02", "9.01"])
            self.assertEqual(f[0]["url"], "https://www.sec.gov/Archives/edgar/data/1318605/000126000002/tsla-8k.htm")
            self.assertEqual(len(tc.sec_recent_filings("TSLA", since="2026-10-01")), 1)
            with self.assertRaises(tc.FetchError):
                tc.sec_recent_filings("NOPE")

    def test_sec_user_agent_uses_contact(self):
        self.assertIn("personal research", tc._user_agent("https://data.sec.gov/x"))
        tc.save_settings({"contact_email": "me@example.com"})
        self.assertTrue(tc._user_agent("https://www.sec.gov/x").endswith("me@example.com"))
        self.assertEqual(tc._user_agent("https://query1.finance.yahoo.com/x"), tc.BROWSER_UA)


class StateTests(TempHomeTestCase):
    NOW = "2026-10-30T10:15"

    def test_settings_roundtrip_and_validation(self):
        s = tc.save_settings({"account_size": "30000", "max_trades_per_day": "4"})
        self.assertEqual(s["account_size"], 30000.0)
        self.assertEqual(s["max_trades_per_day"], 4)
        with self.assertRaises(ValueError):
            tc.save_settings({"nope": 1})
        with self.assertRaises(ValueError):
            tc.save_settings({"account_size": -5})

    def test_corrupt_state_raises_state_error(self):
        (self.home / "ledger.json").write_text("{not json")
        with self.assertRaises(tc.StateError):
            tc.load_ledger()

    def test_plans(self):
        p = tc.save_plan({"symbol": "tsla", "side": "long", "entry": 364.18, "stop": 358.32, "target": 370.04})
        self.assertEqual(p["symbol"], "TSLA")
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 364.18, "stop": 358.32, "setup": "orb"})
        loaded = tc.load_plan("TSLA")
        self.assertEqual(loaded["target"], 370.04)  # merged, not replaced
        self.assertEqual(loaded["setup"], "orb")
        self.assertEqual(len(tc.list_plans()), 1)
        with self.assertRaises(ValueError):
            tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 10, "stop": 11})

    def test_validate_levels_and_rr(self):
        self.assertEqual(tc.validate_levels("long", 364.18, 358.32, 370.04), [])
        self.assertEqual(tc.validate_levels("short", 100, 102, 96), [])
        self.assertTrue(tc.validate_levels("short", 100, 98, 96))
        self.assertTrue(tc.validate_levels("sideways", 100, 98, 96))
        self.assertAlmostEqual(tc.reward_risk("long", 364.18, 358.32, 370.04), 1.0, places=6)
        self.assertAlmostEqual(tc.reward_risk("short", 100, 102, 96), 2.0)

    def test_new_id(self):
        self.assertEqual(tc.new_id("TKT", []), "TKT-20261030-001")
        self.assertEqual(tc.new_id("TKT", ["TKT-20261030-001", "TKT-20261030-007", "TKT-20261029-099"]), "TKT-20261030-008")


class SizingAndBudgetTests(TempHomeTestCase):
    NOW = "2026-10-30T11:00"

    def test_video_example_buys_8_tsla(self):
        r = tc.position_size(364.18, 358.32)   # defaults: $25k account, 0.2% risk = $50
        self.assertEqual(r["shares"], 8)
        self.assertEqual(r["capped_by"], "risk")
        self.assertAlmostEqual(r["risk_used"], 46.88)

    def test_position_cap(self):
        r = tc.position_size(100, 99.9, risk_dollars=1000)  # 10,000 shares by risk, capped at 25% of 25k
        self.assertEqual(r["shares"], 62)
        self.assertEqual(r["capped_by"], "max_position_pct")
        with self.assertRaises(ValueError):
            tc.position_size(100, 100)

    def _trade(self, tid, pnl=None, status="closed", opened="2026-10-30T09:45:00-04:00",
               closed="2026-10-30T10:00:00-04:00", entry=100.0, stop=99.0, qty=50, side="long"):
        t = {"id": tid, "symbol": "X", "side": side, "qty": qty, "entry_price": entry, "stop": stop,
             "target": 102.0, "opened_at": opened, "status": status}
        if status == "closed":
            t.update(pnl=pnl, closed_at=closed, exit_price=entry + pnl / qty)
        return t

    def test_day_summary_and_budget(self):
        led = {"tickets": [], "trades": [
            self._trade("a", pnl=-50, closed="2026-10-30T09:50:00-04:00"),
            self._trade("b", pnl=-50, closed="2026-10-30T09:55:00-04:00"),
            self._trade("c", status="open"),
            self._trade("old", pnl=-500, opened="2026-10-29T10:00:00-04:00", closed="2026-10-29T11:00:00-04:00"),
        ]}
        s = tc.day_summary(led)
        self.assertEqual(s["realized_pnl"], -100)
        self.assertEqual(s["open_risk"], 50)
        self.assertEqual(s["trades_today"], 3)
        self.assertEqual(s["loss_streak"], 2)
        ok = tc.budget_check(0, ledger=led)
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["loss_budget_left"], 0)
        blocked = tc.budget_check(10, ledger=led)
        self.assertFalse(blocked["ok"])
        self.assertTrue(any("daily max loss" in r for r in blocked["reasons"]))

    def test_tilt_guard(self):
        led = {"tickets": [], "trades": [self._trade(str(i), pnl=-1, closed=f"2026-10-30T10:0{i}:00-04:00") for i in range(3)]}
        r = tc.budget_check(5, ledger=led)
        self.assertFalse(r["ok"])
        self.assertTrue(any("tilt" in x for x in r["reasons"]))


class HttpTests(TempHomeTestCase):
    def test_fixture_mode_and_fetch_bars(self):
        bars = random_walk_history("2026-10-30", 2)
        fx = self.home / "fx"
        write_fixture(fx, yahoo_chart_url("TSLA", "5m", "60d"), yahoo_payload(bars, "TSLA"))
        with mock.patch.dict(os.environ, {"DAYTRADER_FIXTURES": str(fx)}):
            got, meta = tc.fetch_bars("TSLA")
            self.assertEqual(len(got), len(bars))
            self.assertEqual(meta["symbol"], "TSLA")
            with self.assertRaises(tc.FetchError):
                tc.fetch_bars("NVDA")

    def test_cache(self):
        calls = []

        def fake(url, **kw):
            calls.append(url)
            return b'{"a": 1}'
        os.environ.pop("DAYTRADER_NO_CACHE")
        with mock.patch.object(tc, "http_get", side_effect=fake):
            self.assertEqual(tc.get_json("https://example.com/x", ttl=60), {"a": 1})
            self.assertEqual(tc.get_json("https://example.com/x", ttl=60), {"a": 1})
            tc.get_json("https://example.com/x", ttl=0)
        self.assertEqual(len(calls), 2)

    def test_non_json_is_fetch_error(self):
        with fake_http({"example.com": "<html>blocked</html>"}):
            with self.assertRaises(tc.FetchError):
                tc.get_json("https://example.com/y")


class SharedLibHardeningTests(TempHomeTestCase):
    NOW = "2026-10-30T11:00"

    def test_proxy_refusal_fails_fast_with_hint(self):
        import urllib.error
        calls = []

        def refuse(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.URLError(OSError("Tunnel connection failed: 403 Forbidden"))
        with mock.patch("urllib.request.urlopen", side_effect=refuse), mock.patch("time.sleep") as slept:
            with self.assertRaises(tc.FetchError) as cm:
                tc.http_get("https://query1.finance.yahoo.com/v8/finance/chart/TSLA")
        self.assertEqual(len(calls), 1)
        slept.assert_not_called()
        self.assertIn("query1.finance.yahoo.com may need to be allow-listed", str(cm.exception))

    def test_truncated_body_is_fetch_error(self):
        import http.client
        with mock.patch("urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"x")), \
                mock.patch("time.sleep"):
            with self.assertRaises(tc.FetchError):
                tc.http_get("https://example.com/z", retries=1)

    def test_fetch_bars_reports_both_hosts(self):
        with fake_http({"finance.yahoo.com": tc.FetchError("network error for host")}):
            with self.assertRaises(tc.FetchError) as cm:
                tc.fetch_bars("TSLA")
        self.assertEqual(str(cm.exception).count("network error"), 2)

    def test_non_object_state_files_are_state_errors(self):
        for name, loader in (("ledger.json", tc.load_ledger), ("settings.json", tc.load_settings)):
            (self.home / name).write_text("[1, 2]")
            with self.assertRaises(tc.StateError):
                loader()
            (self.home / name).unlink()
        (self.home / "plans").mkdir()
        (self.home / "plans" / "TSLA.json").write_text('"x"')
        with self.assertRaises(tc.StateError):
            tc.load_plan("TSLA")

    def test_save_plan_rejects_inconsistent_merge(self):
        tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 100, "stop": 99, "target": 102})
        with self.assertRaises(ValueError):  # old target 102 would sit below a new 103 entry
            tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 103, "stop": 101})
        p = tc.save_plan({"symbol": "TSLA", "side": "long", "entry": 103, "stop": 101, "target": None})
        self.assertIsNone(p["target"])

    def test_open_risk_is_directional(self):
        locked = {"id": "t", "side": "long", "entry_price": 100, "stop": 103, "qty": 10}
        self.assertEqual(tc.trade_initial_risk(locked), 0.0)
        short = {"id": "s", "side": "short", "entry_price": 100, "stop": 101, "qty": 10}
        self.assertEqual(tc.trade_initial_risk(short), 10.0)
        with self.assertRaises(tc.StateError):
            tc.trade_initial_risk({"id": "bad", "side": "long"})
        for side in (None, "", "buy"):
            with self.assertRaises(tc.StateError):
                tc.trade_initial_risk({"id": "odd", "side": side, "entry_price": 100, "stop": 99, "qty": 1})

    def test_utc_stamps_count_on_their_et_day(self):
        # 2026-10-31T01:00Z is still Oct 30 in New York.
        led = {"tickets": [], "trades": [{"id": "a", "side": "long", "qty": 1, "entry_price": 10, "stop": 9,
                                          "status": "closed", "pnl": -1, "opened_at": "2026-10-30T15:00:00Z",
                                          "closed_at": "2026-10-31T01:00:00Z"}]}
        s = tc.day_summary(led, "2026-10-30")
        self.assertEqual((s["trades_today"], s["closed_today"], s["realized_pnl"]), (1, 1, -1))

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            tc.position_size(100, 99, risk_dollars=-50)
        with self.assertRaises(ValueError):
            tc.position_size(100, 99, account=0)
        with self.assertRaises(ValueError):
            tc.save_settings({"max_trades_per_day": -1})

    def test_sec_unexpected_shapes_are_fetch_errors(self):
        with fake_http({"company_tickers.json": {"error": "rate limited"}}):
            with self.assertRaises(tc.FetchError):
                tc.sec_cik("TSLA")
        tickers = {"0": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."}}
        with fake_http({"company_tickers.json": tickers, "CIK0001318605.json": {"filings": []}}):
            with self.assertRaises(tc.FetchError):
                tc.sec_recent_filings("TSLA")

    def test_sec_403_hint_mentions_contact(self):
        self.assertIn("DAYTRADER_CONTACT", tc._host_hint("https://data.sec.gov/x", 403))
        self.assertNotIn("DAYTRADER_CONTACT", tc._host_hint("https://data.sec.gov/x", 403, proxy=True))
        self.assertIn("allow-listed", tc._host_hint("https://data.sec.gov/x", 403, proxy=True))
        tc.save_settings({"contact_email": "me@example.com"})
        self.assertNotIn("DAYTRADER_CONTACT", tc._host_hint("https://data.sec.gov/x", 403))

    def test_yahoo_odd_shapes(self):
        for bad in ([], {"chart": "x"}, {"chart": {"error": "boom"}}, {"chart": {"result": [None]}}):
            with self.assertRaises(tc.FetchError):
                tc.parse_yahoo_chart(bad)

    def test_usage_errors_exit_1(self):
        p = tc.make_parser("x")
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stderr"):
            p.parse_args(["--nope"])
        self.assertEqual(cm.exception.code, 1)


class OutputTests(unittest.TestCase):
    def test_formatters(self):
        self.assertEqual(tc.fmt_money(-1234.5), "-$1,234.50")
        self.assertEqual(tc.fmt_money(5, signed=True), "+$5.00")
        self.assertEqual(tc.fmt_pct(29.44), "29.4%")
        self.assertEqual(tc.fmt_big(5_900_000), "5.90M")
        t = tc.table([["TSLA", "364.18"], ["F", "9.50"]], ["sym", "price"])
        self.assertIn("TSLA  364.18", t)
        self.assertIn("F       9.50", t)


class VendoredCopiesTests(unittest.TestCase):
    def test_every_skill_ships_identical_common(self):
        canonical = (ROOT / "shared" / "trader_common.py").read_bytes()
        skills = [p for p in SKILLS.iterdir() if p.is_dir()]
        self.assertEqual(len(skills), 11, sorted(p.name for p in skills))
        for s in skills:
            copy = s / "scripts" / "trader_common.py"
            self.assertTrue(copy.exists(), f"{copy} missing - run tools/sync_common.py")
            self.assertEqual(copy.read_bytes(), canonical, f"{copy} is stale - run tools/sync_common.py")


if __name__ == "__main__":
    unittest.main()


class MarketTests(TempHomeTestCase):
    def test_symbol_detection(self):
        for sym in ("BTC-USD", "eth-usd", "SOL-USDC", "DOGE-EUR", "1INCH-USD"):
            self.assertTrue(tc.is_crypto_symbol(sym), sym)
        for sym in ("TSLA", "BRK-B", "BF-B", "BRK.B", "", None):
            self.assertFalse(tc.is_crypto_symbol(sym), sym)
        self.assertIs(tc.market_for("BTC-USD"), tc.CRYPTO)
        self.assertIs(tc.market_for("TSLA"), tc.US)
        with mock.patch.dict(os.environ, {"DAYTRADER_MARKET": "crypto"}):
            self.assertIs(tc.market_for(None), tc.CRYPTO)
            self.assertIs(tc.market_for("TSLA"), tc.US)        # a symbol always wins
        with mock.patch.dict(os.environ, {"DAYTRADER_MARKET": "forex"}):
            with self.assertRaises(ValueError):
                tc.market_for(None)

    def test_crypto_time_is_utc_and_always_open(self):
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": "2026-10-31T04:40"}):   # a Saturday
            self.assertFalse(tc.is_market_open())                                   # US: weekend
            with tc.market_scope("BTC-USD"):
                n = tc.now_et()
                self.assertEqual((n.hour, n.minute, n.utcoffset().total_seconds()), (4, 40, 0))
                self.assertTrue(tc.is_market_open())
                self.assertEqual((tc.MARKET_OPEN, tc.MARKET_CLOSE), (0, 1440))
                self.assertEqual(tc.tz_label(), "UTC")
                ts = utc_ts("2026-10-31", "23:55")
                self.assertTrue(tc.in_regular_session(ts))
                self.assertEqual((tc.et_date(ts), tc.et_hhmm(ts)), ("2026-10-31", "23:55"))
            self.assertIs(tc.market(), tc.US)
            self.assertEqual(tc.MARKET_OPEN, 570)

    def test_crypto_sessions_are_utc_days(self):
        bars = crypto_history("2026-10-31", 2, until="01:00")
        with tc.market_scope("crypto"):
            groups = tc.group_sessions(bars)
        self.assertEqual(list(groups), ["2026-10-30", "2026-10-31"])
        self.assertEqual(len(groups["2026-10-30"]), 288)
        self.assertEqual(len(groups["2026-10-31"]), 12)

    def test_parse_iso_utc(self):
        self.assertEqual(tc.parse_iso_utc("2026-10-31T04:40:00Z"), utc_ts("2026-10-31", "04:40"))
        self.assertAlmostEqual(tc.parse_iso_utc("2026-10-31T04:40:00.123456789Z"), utc_ts("2026-10-31", "04:40") + 0.123456789, places=5)
        self.assertEqual(tc.parse_iso_utc("2026-10-31T00:40:00-04:00"), utc_ts("2026-10-31", "04:40"))
        with self.assertRaises(ValueError):
            tc.parse_iso_utc("yesterday")


class CoinbaseTests(TempHomeTestCase):
    NOW = "2026-10-31T04:40"

    def test_windows_are_aligned_and_cover_the_range(self):
        now = utc_ts("2026-10-31", "04:42")
        w = tc.coinbase_candle_windows("BTC-USD", "5m", "60d", now)
        self.assertTrue(all((first // 300) % 300 == 0 for _, first, _ in w))
        self.assertTrue(all(last - first <= 299 * 300 for _, first, last in w))
        self.assertLessEqual(w[0][1], now - 60 * 86400 + 300)
        self.assertEqual(w[-1][2], utc_ts("2026-10-31", "04:40"))
        self.assertIn("granularity=300&start=", w[0][0])
        with self.assertRaises(ValueError):
            tc.coinbase_candle_windows("BTC-USD", "2m", "1d", now)

    def test_parse_candles(self):
        rows = [[1761885600, 99.0, 101.0, 100.0, 100.5, 3.2], [1761885300, 98.0, 100.0, 99.0, 100.0, 1.0],
                [1761885300, 98.0, 100.0, 99.0, 100.0, 1.0], ["bad"], [1761885000, None, 1, 1, 1, 1]]
        bars = tc.parse_coinbase_candles(rows)
        self.assertEqual([b.ts for b in bars], [1761885300, 1761885600])
        with self.assertRaises(tc.FetchError):
            tc.parse_coinbase_candles({"message": "NotFound"})

    def test_fetch_bars_routes_crypto_to_coinbase_with_fixtures(self):
        tc.use_market_for("BTC-USD")
        bars = crypto_history("2026-10-31", 61, until="04:45")
        fx = self.home / "fx"
        write_coinbase_fixtures(fx, "BTC-USD", bars, now_ts=utc_ts("2026-10-31", "04:40"))
        write_coinbase_quote(fx, "BTC-USD", 101234.56)
        with mock.patch.dict(os.environ, {"DAYTRADER_FIXTURES": str(fx)}):
            got, meta = tc.fetch_bars("BTC-USD", "5m", "60d")
            q = tc.quote("btc-usd")
        self.assertEqual(got[-1].ts, utc_ts("2026-10-31", "04:40"))
        self.assertEqual(len(got), 60 * 288)
        self.assertEqual((meta["exchangeName"], meta["currency"], meta["market"]), ("Coinbase", "USD", "crypto"))
        self.assertEqual(q["price"], 101234.56)
        self.assertEqual(q["exchange"], "Coinbase Exchange")
        self.assertEqual(len(tc.group_sessions(got)), 61)

    def test_products_and_info(self):
        fx = self.home / "fx"
        write_coinbase_products(fx)
        with mock.patch.dict(os.environ, {"DAYTRADER_FIXTURES": str(fx)}):
            syms = [r["symbol"] for r in tc.crypto_symbols()]
            info = tc.product_info("btc-usd")
            with self.assertRaises(tc.FetchError):
                tc.product_info("NOPE-USD")
        self.assertEqual(syms, ["BTC-USD", "ETH-USD", "SOL-USD"])      # USDT dropped as a stablecoin
        self.assertEqual((info["base_increment"], info["quote_increment"]), (1e-8, 0.01))


class CryptoSizingTests(TempHomeTestCase):
    def test_fractional_size_includes_fees(self):
        with tc.market_scope("BTC-USD"):
            r = tc.position_size(100_000.0, 99_500.0)          # $50 budget, $500 stop, 0.6% fee per fill
        # loss per BTC = 500 + 0.006 * (100000 + 99500) = 1697 -> 50 / 1697 = 0.02946375...
        self.assertAlmostEqual(r["shares"], 0.02946375, places=8)
        self.assertLessEqual(r["risk_used"] + r["fees_at_stop"], 50.0 + 0.01)
        self.assertEqual(r["fee_pct"], 0.6)
        no_fee = tc.position_size(100_000.0, 99_500.0, account=100_000, risk_dollars=50, fee=0, step=0.001)
        self.assertEqual(no_fee["shares"], 0.1)
        capped = tc.position_size(100_000.0, 99_500.0, risk_dollars=50, fee=0, step=0.001)
        self.assertEqual((capped["shares"], capped["capped_by"]), (0.062, "max_position_pct"))  # 25% of $25k

    def test_us_sizing_unchanged(self):
        r = tc.position_size(364.18, 358.32)
        self.assertEqual((r["shares"], r["fee_pct"]), (8, 0.0))
        self.assertIsInstance(r["shares"], int)

    def test_formatters(self):
        self.assertEqual(tc.fmt_qty(8), "8")
        self.assertEqual(tc.fmt_qty(0.02946375), "0.02946375")
        self.assertEqual(tc.fmt_price(0.00001234), "0.00001234")
        self.assertEqual(tc.fmt_price(0.5), "0.5000")
        self.assertEqual(tc.fmt_price(101234.5), "101,234.50")


class BinanceTests(TempHomeTestCase):
    NOW = "2026-10-31T04:40"

    def setUp(self):
        super().setUp()
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "binance"

    def test_symbols(self):
        for sym in ("BTCUSDT", "ETHUSDC", "BTCFDUSD", "BTC-USDT"):
            self.assertTrue(tc.is_crypto_symbol(sym), sym)
        self.assertFalse(tc.is_crypto_symbol("USDT"))
        self.assertEqual(tc.crypto_pair("btcusdt"), ("BTC", "USDT"))
        self.assertEqual(tc.crypto_pair("USDCUSDT"), ("USDC", "USDT"))
        self.assertEqual(tc.binance_symbol("BTC-USD"), "BTCUSDT")      # Binance's few USD pairs are thin
        self.assertEqual(tc.binance_symbol("eth-usdc"), "ETHUSDC")
        self.assertEqual(tc.coinbase_product("BTCUSDT"), "BTC-USDT")

    def test_exchange_setting_and_fees(self):
        self.assertEqual((tc.crypto_exchange(), tc.exchange_name()), ("binance", "Binance"))
        self.assertEqual((tc.CRYPTO.source, tc.US.source), ("Binance", "Yahoo Finance"))
        with tc.market_scope("crypto"):
            self.assertEqual(tc.fee_pct(), 0.1)
        os.environ.pop("DAYTRADER_CRYPTO_EXCHANGE")
        self.assertEqual(tc.load_settings()["crypto_exchange"], "binance")   # the default
        tc.save_settings({"crypto_exchange": "Coinbase"})
        s = tc.load_settings()
        self.assertEqual((s["crypto_exchange"], s["crypto_fee_pct"]), ("coinbase", 0.6))
        tc.save_settings({"crypto_fee_pct": 0.25})                           # an explicit fee wins
        self.assertEqual(tc.load_settings()["crypto_fee_pct"], 0.25)
        with self.assertRaises(ValueError):
            tc.save_settings({"crypto_exchange": "kraken"})
        with mock.patch.dict(os.environ, {"DAYTRADER_CRYPTO_EXCHANGE": "ftx"}):
            with self.assertRaises(ValueError):
                tc.crypto_exchange()

    def test_windows(self):
        now = utc_ts("2026-10-31", "04:42")
        w = tc.binance_candle_windows("BTC-USDT", "5m", "60d", now)
        self.assertIn(len(w), (18, 19))      # 17,280 candles; alignment can split them over one more window
        self.assertTrue(all((first // 300) % 1000 == 0 for _, first, _ in w))
        self.assertTrue(all(last - first <= 999 * 300 for _, first, last in w))
        self.assertEqual(w[-1][2], utc_ts("2026-10-31", "04:40"))
        self.assertIn("klines?symbol=BTCUSDT&interval=5m&startTime=", w[0][0])

    def test_parse_klines(self):
        rows = [[1793404800000, "77368.76", "77420.00", "77320.00", "77326.00", "30.46", 1793405099999, "0", 1, "0", "0", "0"],
                [1793405100000, "77326.00", "77330.00", "77300.00", "77310.00", "1.0"], ["bad"], [1793405400000, None, 1, 1, 1, 1]]
        bars = tc.parse_binance_klines(rows)
        self.assertEqual([b.ts for b in bars], [1793404800, 1793405100])
        self.assertEqual((bars[0].open, bars[0].high, bars[0].low, bars[0].close), (77368.76, 77420.0, 77320.0, 77326.0))
        with self.assertRaises(tc.FetchError):
            tc.parse_binance_klines({"code": -1121, "msg": "Invalid symbol."})

    def test_fetch_quote_and_products_with_fixtures(self):
        tc.use_market_for("BTC-USDT")
        bars = crypto_history("2026-10-31", 61, start_price=77_000, until="04:45")
        fx = self.home / "fx"
        now = utc_ts("2026-10-31", "04:40")
        write_binance_fixtures(fx, "BTC-USDT", bars, now_ts=now)
        write_binance_quote(fx, "BTC-USDT", 77211.64, when_ts=now, open_24h=78183.98)
        write_binance_products(fx)
        with mock.patch.dict(os.environ, {"DAYTRADER_FIXTURES": str(fx)}):
            got, meta = tc.fetch_bars("BTCUSDT", "5m", "60d")
            q = tc.quote("BTC-USDT")
            info = tc.product_info("BTC-USDT")
            syms = [r["symbol"] for r in tc.crypto_symbols()]
            with self.assertRaises(tc.FetchError):
                tc.fetch_bars("DOGE-USDT", "5m", "1d")
        self.assertEqual((len(got), got[-1].ts), (60 * 288, now))
        self.assertEqual((meta["exchangeName"], meta["symbol"], meta["currency"]), ("Binance", "BTCUSDT", "USDT"))
        self.assertEqual((q["price"], q["previous_close"], q["exchange"], q["bid"]), (77211.64, 78183.98, "Binance", 77211.63))
        self.assertEqual((info["base_increment"], info["quote_increment"], info["status"]), (1e-5, 0.01, "online"))
        self.assertEqual(syms, ["BTC-USDT", "ETH-USDT", "SOL-USDT"])   # stablecoin and leveraged token dropped

    def test_unknown_pair_message(self):
        with fake_http({"klines": tc.FetchError("HTTP 400 for https://data-api.binance.vision/... ")}):
            with self.assertRaises(tc.FetchError) as cm:
                tc.binance_bars("NOPE-USDT", "5m", "1d")
        self.assertIn("Binance has no spot pair NOPEUSDT", str(cm.exception))

    def test_sizing_uses_binance_fee(self):
        with tc.market_scope("BTC-USDT"):
            r = tc.position_size(77_000.0, 76_500.0, step=1e-5)
        # loss per BTC = 500 + 0.001 * (77000 + 76500) = 653.5 -> 50 / 653.5 = 0.0765... -> 0.07651 (step 1e-5)
        self.assertEqual((r["shares"], r["fee_pct"]), (0.07651, 0.1))


class SharedLibFixesFromBinanceReviewTests(TempHomeTestCase):
    NOW = "2026-10-31T09:40"

    def test_one_letter_and_non_latin_coins_are_crypto(self):
        for sym in ("A-USDT", "T-USDT", "币安人生-USDT", "BROCCOLI714USDT", "币安人生USDT"):
            self.assertTrue(tc.is_crypto_symbol(sym), sym)
        self.assertEqual(tc.crypto_pair("BROCCOLI714USDT"), ("BROCCOLI714", "USDT"))
        self.assertEqual(tc.binance_symbol("A-USDT"), "AUSDT")
        for sym in ("BRK-B", "TSLA", "A"):
            self.assertFalse(tc.is_crypto_symbol(sym), sym)

    def test_one_plan_per_pair_whatever_the_spelling(self):
        tc.save_plan({"symbol": "BTCUSDT", "side": "long", "entry": 100, "stop": 99, "target": 102})
        self.assertEqual(tc.load_plan("BTC-USDT")["target"], 102)
        self.assertEqual(tc.canonical_symbol("btcusdt"), "BTC-USDT")
        self.assertEqual(tc.canonical_symbol("tsla"), "TSLA")
        tc.save_plan({"symbol": "币安人生-USDT", "side": "long", "entry": 1, "stop": 0.9, "target": 1.2})
        tc.save_plan({"symbol": "牛来-USDT", "side": "long", "entry": 2, "stop": 1.9, "target": 2.2})
        self.assertEqual(tc.load_plan("币安人生-USDT")["entry"], 1)    # no collision on non-Latin names
        self.assertEqual(tc.load_plan("牛来-USDT")["entry"], 2)

    def test_leveraged_filter_keeps_real_coins_and_new_stablecoins_drop(self):
        rows = [{"symbol": b + "USDT", "status": "TRADING", "baseAsset": b, "quoteAsset": "USDT", "isSpotTradingAllowed": True}
                for b in ("BTC", "BTCUP", "JUP", "SYRUP", "USD1", "RLUSD", "A", "U")]
        with fake_http({"exchangeInfo?permissions=SPOT": {"symbols": rows}}):
            syms = [r["symbol"] for r in tc.binance_crypto_symbols(ttl=0)]
        self.assertEqual(syms, ["A-USDT", "BTC-USDT", "JUP-USDT", "SYRUP-USDT"])   # BTCUP dropped: its root BTC is listed
        # U (a dollar-pegged token, 1.00 with a 0.05% ATR) is dropped with the other stablecoins

    def test_bad_exchange_setting_does_not_break_stock_runs(self):
        with mock.patch.dict(os.environ, {"DAYTRADER_CRYPTO_EXCHANGE": "kraken"}):
            s = tc.load_settings()                       # stocks keep working
            self.assertEqual(s["account_size"], 25000.0)
            with self.assertRaises(ValueError):          # a crypto command still says what is wrong
                tc.crypto_exchange()

    def test_fee_can_follow_the_exchange_again(self):
        os.environ.pop("DAYTRADER_CRYPTO_EXCHANGE", None)
        tc.save_settings({"crypto_fee_pct": 0.25})
        self.assertEqual(tc.load_settings()["crypto_fee_pct"], 0.25)
        tc.save_settings({"crypto_fee_pct": None})
        self.assertEqual(tc.load_settings()["crypto_fee_pct"], 0.1)          # Binance's default again
        tc.save_settings({"crypto_exchange": "coinbase"})
        self.assertEqual(tc.load_settings()["crypto_fee_pct"], 0.6)

    def test_binance_windows_fetch_in_parallel_and_keep_order(self):
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "binance"
        tc.use_market_for("BTC-USDT")
        bars = crypto_history("2026-10-31", 61, start_price=77_000, until="09:45")
        fx = self.home / "fx"
        now = utc_ts("2026-10-31", "09:40")
        write_binance_fixtures(fx, "BTC-USDT", bars, now_ts=now)
        with mock.patch.dict(os.environ, {"DAYTRADER_FIXTURES": str(fx)}):
            got, _ = tc.fetch_bars("BTC-USDT", "5m", "60d")
        self.assertEqual(len(got), 60 * 288)
        self.assertEqual([b.ts for b in got], sorted(b.ts for b in got))
        self.assertEqual(got[-1].ts, now)
