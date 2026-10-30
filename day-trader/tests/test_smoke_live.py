"""Tests for tools/smoke_live.py - fully offline.

The smoke test is meant for the live hosts, so here every source is faked: tc.fetch_bars via
helpers.fake_bars and tc.http_get via helpers.fake_http (the House index zip and PTR PDF come
from test_pelosi's builders), or urlopen itself for the blocked-proxy case. One synthetic world
makes every check PASS; broken, stale, odd-shaped and blocked sources must turn into FAIL rows
(exit 1) without stopping the other checks.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (ROOT, TempHomeTestCase, daily_bars, et_ts, fake_bars, fake_http,  # noqa: E402
                     random_walk_history, tc)
import test_pelosi as tp  # noqa: E402  (synthetic House index zips and PTR PDFs)

TOOL = ROOT / "tools" / "smoke_live.py"
_spec = importlib.util.spec_from_file_location("day_trader_smoke_live", TOOL)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)  # type: ignore[union-attr]

DAY = "2026-10-30"                     # a Friday
NOW = tc.parse_et(f"{DAY}T16:30")
SYM = "TSLA"
CIK = 1318605
SUBMISSIONS = f"data.sec.gov/submissions/CIK{CIK:010d}.json"
PTR_DOC = "20026590"
HOSTS = {"yahoo-5m": "query1/query2.finance.yahoo.com", "yahoo-1d": "query1/query2.finance.yahoo.com",
         "yahoo-quote": "query1/query2.finance.yahoo.com", "nasdaq-symbols": "www.nasdaqtrader.com",
         "news-rss": "feeds.finance.yahoo.com", "news-search": "query1/query2.finance.yahoo.com",
         "sec-filings": "www.sec.gov, data.sec.gov", "house-index": "disclosures-clerk.house.gov",
         "house-ptr": "disclosures-clerk.house.gov"}


# ---------------------------------------------------------------------------
# A synthetic world in which every live source answers sensibly
# ---------------------------------------------------------------------------

def bars_world() -> Dict[Any, List[tc.Bar]]:
    closes = [300.0 + i * 0.5 for i in range(125)]
    return {
        (SYM, "5m"): random_walk_history(DAY, 40, start_price=350.0, seed=3),
        (SYM, "1d"): daily_bars(DAY, closes, volumes=[90_000_000.0] * len(closes)),
        (SYM, "1m"): [tc.Bar(et_ts(DAY, "15:59"), 362.0, 362.2, 361.9, 362.1, 50_000.0)],
    }


BARS_META = {"regularMarketTime": et_ts(DAY, "16:00"), "currency": "USD", "longName": "Tesla, Inc."}


def symbol_files(n_nasdaq: int = 2000, n_other: int = 1500) -> Dict[str, str]:
    nasdaq = ["Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares",
              "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N", "MSFT|Microsoft Corporation - Common Stock|Q|N|N|100|N|N",
              "TSLA|Tesla, Inc. - Common Stock|Q|N|N|100|N|N", "QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N",
              "ABCDW|ABCD Corp. - Warrants|S|N|N|100|N|N"]
    nasdaq += [f"Q{i:04d}|Test Company {i} - Common Stock|Q|N|N|100|N|N" for i in range(n_nasdaq)]
    nasdaq.append("File Creation Time: 1030202617:00|||||||")
    other = ["ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol",
             "JPM|JPMorgan Chase & Co. Common Stock|N|JPM|N|100|N|JPM",
             "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY"]
    other += [f"N{i:04d}|Test Industries {i} Common Stock|N|N{i:04d}|N|100|N|N{i:04d}" for i in range(n_other)]
    other.append("File Creation Time: 1030202617:00|||||||")
    return {"nasdaqlisted.txt": "\n".join(nasdaq) + "\n", "otherlisted.txt": "\n".join(other) + "\n"}


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Yahoo! Finance: TSLA News</title>
<item><title>Tesla beats third-quarter earnings estimates as deliveries rise</title>
<link>https://finance.yahoo.com/news/tesla-beats-third-quarter-earnings-estimates-130500123.html</link>
<pubDate>Fri, 30 Oct 2026 13:05:00 +0000</pubDate></item>
<item><title>Tesla shares edge higher in early trading</title>
<link>https://finance.yahoo.com/news/tesla-shares-edge-higher-early-134500456.html</link>
<pubDate>Fri, 30 Oct 2026 13:45:00 +0000</pubDate></item>
</channel></rss>
"""

SEARCH = {"news": [
    {"title": "Tesla to expand Berlin factory output", "publisher": "Reuters",
     "link": "https://finance.yahoo.com/news/tesla-expand-berlin-120000111.html",
     "providerPublishTime": et_ts(DAY, "08:00"), "relatedTickers": ["TSLA"]},
    {"title": "EV makers rally", "publisher": "Bloomberg", "link": "https://finance.yahoo.com/news/ev-rally.html",
     "providerPublishTime": et_ts(DAY, "11:30"), "relatedTickers": ["TSLA", "RIVN"]},
]}


def sec_submissions(dates: Optional[List[str]] = None) -> Dict[str, Any]:
    dates = dates or ["2026-10-22", "2026-10-21", "2026-09-15"]
    n = len(dates)
    return {"cik": str(CIK), "name": "Tesla, Inc.", "filings": {"recent": {
        "accessionNumber": [f"0001628280-26-0{i:05d}" for i in range(n)],
        "form": ["8-K", "10-Q", "4"][:n] + ["4"] * max(0, n - 3),
        "filingDate": dates,
        "acceptanceDateTime": [f"{d}T16:05:12.000Z" for d in dates],
        "primaryDocument": [f"doc{i}.htm" for i in range(n)],
        "items": ["2.02,9.01", "", ""][:n] + [""] * max(0, n - 3),
        "primaryDocDescription": ["8-K", "10-Q", "FORM 4"][:n] + ["FORM 4"] * max(0, n - 3),
    }}}


def house_index(recs: Optional[List[Dict[str, str]]] = None) -> bytes:
    return tp.index_zip(2026, recs if recs is not None else [
        tp.rec("Pelosi", "Nancy", "CA11", "10/28/2026", PTR_DOC),
        tp.rec("Smith", "John", "OH04", "10/29/2026", "8221999"),           # paper: never picked
        tp.rec("Pelosi", "Nancy", "CA11", "5/15/2026", "10066000", ftype="O"),
    ])


def http_world() -> Dict[str, Any]:
    pelosi = smoke.load_skill("pelosi", "pelosi")
    routes: Dict[str, Any] = dict(symbol_files())
    routes.update({
        "feeds.finance.yahoo.com/rss": RSS,
        "/v1/finance/search": SEARCH,
        "www.sec.gov/files/company_tickers.json": {"0": {"cik_str": CIK, "ticker": SYM, "title": "Tesla, Inc."}},
        SUBMISSIONS: sec_submissions(),
        pelosi.INDEX_URL.format(year=2026): house_index(),
        pelosi.ptr_url(2026, PTR_DOC): tp.pelosi_pdf_bytes(),
    })
    return routes


def run_main(*argv: str) -> Tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = smoke.main(list(argv))
    return code, buf.getvalue()


class SmokeTestCase(TempHomeTestCase):
    def world(self, bars: Optional[Dict[Any, List[tc.Bar]]] = None, routes: Optional[Dict[str, Any]] = None,
              meta: Optional[Dict[str, Any]] = None):
        """Patch every source (and pin the clock) for this test; returns the fetch_bars and http_get mocks."""
        fb = fake_bars(bars_world() if bars is None else bars, meta=BARS_META if meta is None else meta)
        fh = fake_http(http_world() if routes is None else routes)
        clock = mock.patch.object(tc, "now_et", return_value=NOW)
        mocks = [fb.__enter__(), fh.__enter__(), clock.__enter__()]
        for cm in (clock, fh, fb):
            self.addCleanup(cm.__exit__, None, None, None)
        return mocks[0], mocks[1]

    def rows(self, res: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {r["name"]: r for r in res["checks"]}


# ---------------------------------------------------------------------------
# Everything answers: all PASS, exit 0
# ---------------------------------------------------------------------------

class AllPassTests(SmokeTestCase):
    def test_every_check_passes_on_sane_data(self):
        bars, http = self.world()
        res = smoke.run_checks(SYM, now=NOW)
        rows = self.rows(res)
        self.assertEqual(list(rows), smoke.CHECK_NAMES)
        self.assertEqual({n: r["status"] for n, r in rows.items()}, {n: "PASS" for n in smoke.CHECK_NAMES},
                         json.dumps(res, indent=1))
        self.assertEqual((res["ok"], res["passed"], res["failed"], res["skipped"]), (True, 9, 0, 0))
        self.assertEqual({n: r["host"] for n, r in rows.items()}, HOSTS)

        self.assertIn("3,120 bars, 40 sessions", rows["yahoo-5m"]["detail"])
        self.assertIn("125 daily bars", rows["yahoo-1d"]["detail"])
        self.assertIn("TSLA 362.10 USD at 2026-10-30 16:00 ET (Tesla, Inc.)", rows["yahoo-quote"]["detail"])
        self.assertIn("3,504 common stocks (NASDAQ 2,003, NYSE 1,501)", rows["nasdaq-symbols"]["detail"])
        self.assertIn("2 headlines (2 dated)", rows["news-rss"]["detail"])
        self.assertIn("Tesla shares edge higher", rows["news-rss"]["detail"])
        self.assertIn("EV makers rally", rows["news-search"]["detail"])
        self.assertIn("3 recent filings (1 8-K/6-K), newest 8-K filed 2026-10-22", rows["sec-filings"]["detail"])
        self.assertIn("3 filings in the 2026 index, 2 PTRs, newest filed 2026-10-29", rows["house-index"]["detail"])
        self.assertIn(f"DocID {PTR_DOC} (", rows["house-ptr"]["detail"])
        self.assertIn("6 trade(s)", rows["house-ptr"]["detail"])

        # The skills' own code paths, one request per source (the Nasdaq list is two files).
        self.assertEqual(sorted((c.args[0], c.args[1], c.args[2]) for c in bars.call_args_list),
                         [(SYM, "1d", "6mo"), (SYM, "1m", "1d"), (SYM, "5m", "60d")])
        urls = [c.args[0] for c in http.call_args_list]
        self.assertEqual(len(urls), len(set(urls)), urls)
        self.assertEqual(len(urls), 8, urls)
        self.assertTrue(any(u.startswith("https://query1.finance.yahoo.com/v1/finance/search?q=TSLA") for u in urls))
        self.assertIn(f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/{PTR_DOC}.pdf", urls)
        self.assertFalse(any("8221999" in u for u in urls), "a paper filing is never downloaded")

    def test_main_text_and_json_exit_0(self):
        self.world()
        code, out = run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("LIVE SMOKE TEST · TSLA · 2026-10-30 16:30 ET", out)
        for name in smoke.CHECK_NAMES:
            self.assertRegex(out, rf"(?m)^{name}\s+{HOSTS[name].replace('.', '[.]')}\s+PASS\s")
        self.assertIn("OK: 9 of 9 checks passed", out)
        self.assertNotIn("FAIL", out)

        code, out = run_main("--json")
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertTrue(res["ok"])
        self.assertEqual([r["status"] for r in res["checks"]], ["PASS"] * 9)
        self.assertEqual(set(res["checks"][0]), {"name", "host", "used_by", "status", "seconds", "detail"})

    def test_only_runs_the_named_checks_and_ptr_brings_its_index(self):
        self.world()
        code, out = run_main("--json", "--only", "yahoo-quote,house-ptr")
        self.assertEqual(code, 0)
        self.assertEqual([r["name"] for r in json.loads(out)["checks"]], ["yahoo-quote", "house-index", "house-ptr"])

    def test_no_recent_electronic_ptr_is_a_skip_not_a_failure(self):
        routes = http_world()
        pelosi = smoke.load_skill("pelosi", "pelosi")
        routes[pelosi.INDEX_URL.format(year=2026)] = house_index([
            tp.rec("Pelosi", "Nancy", "CA11", "6/02/2026", PTR_DOC),           # older than 60 days
            tp.rec("Smith", "John", "OH04", "10/29/2026", "8221999")])         # paper
        self.world(routes=routes)
        code, out = run_main("--json")
        res = json.loads(out)
        self.assertEqual(code, 0, out)
        ptr = self.rows(res)["house-ptr"]
        self.assertEqual(ptr["status"], "SKIP")
        self.assertIn("no electronic PTR filed since 2026-08-31", ptr["detail"])
        self.assertEqual((res["passed"], res["skipped"], res["failed"]), (8, 1, 0))

    def test_live_env_ignores_fixtures_and_replay_clock_and_restores_them(self):
        self.world()
        env = {"DAYTRADER_FIXTURES": str(self.home / "fx"), "DAYTRADER_NOW": "2026-10-29T10:00"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("DAYTRADER_NO_CACHE", None)
            seen: Dict[str, Optional[str]] = {}

            def spy(*a: Any, **k: Any) -> Dict[str, Any]:
                seen.update({v: os.environ.get(v) for v in ("DAYTRADER_FIXTURES", "DAYTRADER_NOW",
                                                            "DAYTRADER_NO_CACHE")})
                return {"ok": True, "symbol": SYM, "checked_at": NOW.isoformat(), "passed": 0, "failed": 0,
                        "skipped": 0, "checks": [], "notes": []}

            with mock.patch.object(smoke, "run_checks", side_effect=spy):
                code, out = run_main()
            self.assertEqual(seen, {"DAYTRADER_FIXTURES": None, "DAYTRADER_NOW": None, "DAYTRADER_NO_CACHE": "1"})
            self.assertIn("note: $DAYTRADER_FIXTURES is set and ignored here", out)
            self.assertIn("note: $DAYTRADER_NOW is set and ignored here", out)
            self.assertEqual(os.environ["DAYTRADER_NOW"], "2026-10-29T10:00")
            self.assertNotIn("DAYTRADER_NO_CACHE", os.environ)


# ---------------------------------------------------------------------------
# Broken, stale or blocked sources: FAIL rows, exit 1, no crash
# ---------------------------------------------------------------------------

class FailureTests(SmokeTestCase):
    def test_bad_sources_fail_one_by_one_without_stopping_the_run(self):
        world = bars_world()
        stale_day = "2026-09-18"
        world[(SYM, "1d")] = daily_bars(stale_day, [300.0 + i for i in range(125)])  # 6 weeks old
        del world[(SYM, "1m")]                                                       # quote: FetchError
        routes = http_world()
        routes["nasdaqlisted.txt"], routes["otherlisted.txt"] = (v for v in symbol_files(5, 5).values())
        routes["feeds.finance.yahoo.com/rss"] = "<html><body>Will be right back</body></html>"
        routes["/v1/finance/search"] = {"finance": {"error": {"code": "Bad Request", "description": "Invalid"}}}
        routes[SUBMISSIONS] = {"filings": {"recent": "nope"}}
        pelosi = smoke.load_skill("pelosi", "pelosi")
        routes[pelosi.ptr_url(2026, PTR_DOC)] = tp.paper_pdf()                       # scanned: no text
        self.world(bars=world, routes=routes)

        code, out = run_main("--json")
        self.assertEqual(code, 1)
        res = json.loads(out)
        rows = self.rows(res)
        self.assertEqual({n: r["status"] for n, r in rows.items()},
                         {"yahoo-5m": "PASS", "yahoo-1d": "FAIL", "yahoo-quote": "FAIL", "nasdaq-symbols": "FAIL",
                          "news-rss": "FAIL", "news-search": "FAIL", "sec-filings": "FAIL",
                          "house-index": "PASS", "house-ptr": "FAIL"})
        self.assertFalse(res["ok"])
        self.assertIn("older than 7 days - stale or frozen data?", rows["yahoo-1d"]["detail"])
        self.assertIn("no fixture for interval 1m", rows["yahoo-quote"]["detail"])
        self.assertIn("only 14 common stocks parsed (expected at least 3,000)", rows["nasdaq-symbols"]["detail"])
        self.assertIn("returned <html>, not an RSS feed", rows["news-rss"]["detail"])
        self.assertIn("Yahoo search for TSLA: Bad Request: Invalid", rows["news-search"]["detail"])
        self.assertIn("unexpected SEC submissions format", rows["sec-filings"]["detail"])
        self.assertIn(f"{PTR_DOC}.pdf came back paper", rows["house-ptr"]["detail"])

        code, text = run_main()
        self.assertEqual(code, 1)
        self.assertRegex(text, r"(?m)^yahoo-1d\s+query1/query2[.]finance[.]yahoo[.]com\s+FAIL\s")
        self.assertRegex(text, r"(?m)^news-rss\s+feeds[.]finance[.]yahoo[.]com\s+FAIL\s")
        self.assertIn("FAILED: 7 of 9 checks failed (2 passed).", text)
        self.assertNotIn("Blocked:", text)      # these hosts answered; their data was wrong

    def test_a_parser_crash_is_a_fail_row(self):
        world = bars_world()

        def fetch(symbol: str, interval: str = "5m", range_: str = "60d", **kw: Any):
            if interval == "5m":
                raise KeyError("timestamp")
            return list(world[(symbol, interval)]), dict(BARS_META, symbol=symbol)

        self.world()
        with mock.patch.object(tc, "fetch_bars", side_effect=fetch):
            res = smoke.run_checks(SYM, only=["yahoo-5m", "yahoo-1d"], now=NOW)
        rows = self.rows(res)
        self.assertEqual((rows["yahoo-5m"]["status"], rows["yahoo-5m"]["detail"]), ("FAIL", "KeyError: 'timestamp'"))
        self.assertEqual(rows["yahoo-1d"]["status"], "PASS")

    def test_timestamps_in_the_wrong_unit_or_frozen_are_caught(self):
        self.world(meta=dict(BARS_META, regularMarketTime=et_ts(DAY, "16:00") * 1000))
        res = smoke.run_checks(SYM, only=["yahoo-quote"], now=NOW)
        self.assertIn("in the future - timestamps parsed in the wrong unit?", res["checks"][0]["detail"])

        world = bars_world()
        five = world[(SYM, "5m")]
        world[(SYM, "5m")] = [tc.Bar(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in five[::3]]  # 15m
        self.world(bars=world)
        res = smoke.run_checks(SYM, only=["yahoo-5m"], now=NOW)
        self.assertEqual(res["checks"][0]["status"], "FAIL")
        self.assertIn("bars are not 5 minutes apart (median step 900 s)", res["checks"][0]["detail"])

    def test_blocked_proxy_fails_fast_with_the_allow_list_hint(self):
        """The sandbox case: the proxy refuses every CONNECT. No retries, every host named."""
        refused = urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
        with mock.patch("urllib.request.urlopen", side_effect=refused) as urlopen, \
                mock.patch.object(tc, "now_et", return_value=NOW):
            started = time.monotonic()
            code, out = run_main()
            elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertLess(elapsed, 5.0, "a proxy refusal must not be retried with backoff")
        urls = [c.args[0].full_url for c in urlopen.call_args_list]
        self.assertEqual(len(urls), len(set(urls)), "each URL is asked once")
        for name, host in HOSTS.items():
            status = "SKIP" if name == "house-ptr" else "FAIL"
            self.assertRegex(out, rf"(?m)^{name}\s+{host.replace('.', '[.]')}\s+{status}\s")
        self.assertIn("SKIP    0.0s  needs the house-index check to pass first", out)
        self.assertIn("blocked by proxy for https://query1.finance.yahoo.com/v8/finance/chart/TSLA?interval=5m"
                      "&range=60d&includePrePost=false (Tunnel connection failed: 403 Forbidden) - access denied; "
                      "if you are in a sandbox, query1.finance.yahoo.com may need to be allow-listed", out)
        self.assertIn("Blocked: query1/query2.finance.yahoo.com; www.nasdaqtrader.com; feeds.finance.yahoo.com; "
                      "www.sec.gov, data.sec.gov; disclosures-clerk.house.gov - allow outbound HTTPS", out)
        self.assertIn("FAILED: 8 of 9 checks failed, 1 skipped (0 passed).", out)


class CliTests(unittest.TestCase):
    def run_tool(self, *args: str) -> subprocess.CompletedProcess:
        # Only argument errors here: each exits before any request is made.
        return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, timeout=60)

    def test_usage_errors_exit_1(self):
        for args, words in ((["--only", "yahoo-5m,bogus"], "unknown check bogus"),
                            (["--symbol", "$$$"], "is not a ticker"),
                            (["--frobnicate"], "unrecognized arguments")):
            p = self.run_tool(*args)
            self.assertEqual(p.returncode, 1, (args, p.stderr))
            self.assertIn(words, p.stderr)
            self.assertNotIn("Traceback", p.stderr)

    def test_help_lists_every_check(self):
        p = self.run_tool("--help")
        self.assertEqual(p.returncode, 0)
        flat = "".join(p.stdout.split())        # argparse may wrap "house-index" after the hyphen
        for name in smoke.CHECK_NAMES:
            self.assertIn(name, flat)

    def test_readme_mentions_the_smoke_test(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("python3 tools/smoke_live.py", readme)
        self.assertIn("run `tools/smoke_live.py` first", readme)


if __name__ == "__main__":
    unittest.main()
