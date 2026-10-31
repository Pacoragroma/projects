"""End-to-end crypto desk flow: "Paper trade BTC" - plan, test and paper-fill BTC-USD on a Saturday.

The same desk as tests/test_desk_flow.py, on a 24/7 market. Every skill runs as a real subprocess
(helpers.run_cli), offline, against Coinbase Exchange fixtures in $DAYTRADER_FIXTURES, with one
shared $DAYTRADER_HOME so the plan and the paper ledger flow from skill to skill:

  scanner --symbols BTC-USD -> news BTC-USD -> entry --save -> exit_plan --method manual --rr 1 --save
  -> time_machine --save -> risk size --save -> budget check -> broker ticket -> confirm (no --yes:
  refused) -> confirm --yes -> budget status -> alerts from-plan -> broker sync + alerts check
  -> budget status -> broker history -> analyst review

$DAYTRADER_NOW is a UTC time on Saturday 2026-10-31: the US market is shut, crypto is not, so every
step must treat the weekend as an ordinary trading day. Before each step the Coinbase candles are
re-published as the live API would answer at that moment (finished 5-minute bars only), at the
exact candle-window URLs tc.coinbase_bars requests: 5m over 2d (broker sync, alerts), 5d (entry,
exit) and 60d (time machine), 1d over 6mo (scanner), plus the ticker and 24h stats (tc.quote) and
the product list (tc.product_info: the size step and the price tick). News reads Yahoo's RSS and
search feeds for BTC-USD; SEC EDGAR has no fixtures and must not be asked at all (n/a for crypto).

Today's UTC day: a 4-week base under 100,600 on the daily chart, an opening range 00:00-00:15 UTC
(high 101,200, low 99,000), then the 00:35 bar closes at 101,250 above it -> an ORB long at 00:40,
stop 99,000 (the OR low), 1R target 103,500. The default $50 budget buys 0.01448645 BTC: the
price risk plus both fills' paper fees (0.6% each) at the stop. After the 00:47 paper fill the tape
either rallies through the target (01:20 bar) or slides through the stop (01:05 bar), and the P&L
is booked net of fees. AnchoredRangeTest runs the same tape twelve hours later, where the default
00:00 UTC range has long expired and `entry --or-start 12:00` anchors the ORB instead.

CryptoReadmeSampleTest runs the README's crypto desk-run commands in text mode on the same data and
checks that every line of the README's crypto sample block is really printed. To regenerate that
block: python3 tests/test_crypto_desk_flow.py --print-run
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import shlex
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (TempHomeTestCase, crypto_history, run_cli, skill_script, tc, utc_ts,  # noqa: E402
                     write_coinbase_fixtures, write_coinbase_products, write_coinbase_quote, write_fixture)
import test_desk_flow as us_desk  # noqa: E402  (the US desk flow and its README parsing)

README = Path(__file__).resolve().parent.parent / "README.md"
CRYPTO_SECTION = "## Crypto (24/7, Coinbase prices)"
RUN_HEADING = "### A crypto desk run"
SAMPLE_INTRO = "On the pack's synthetic crypto test data"   # the sentence right before the sample block

SYM = "BTC-USD"
DAY = "2026-10-31"          # a Saturday: US stocks are closed, crypto trades
PREV = "2026-10-30"
OPEN_PX = 100_000.00

# (start UTC, open, high, low, close, volume in BTC) - today's UTC day through the paper fill.
NIGHT = [
    ("00:00", 100_000.00, 100_600.00, 99_600.00, 100_400.00, 30.0),
    ("00:05", 100_400.00, 101_200.00, 100_200.00, 100_900.00, 28.0),   # OR high 101,200
    ("00:10", 100_900.00, 101_000.00, 99_000.00, 99_600.00, 29.0),     # OR low 99,000 (OR 00:00-00:15)
    ("00:15", 99_600.00, 100_300.00, 99_400.00, 100_100.00, 20.0),
    ("00:20", 100_100.00, 100_700.00, 99_900.00, 100_500.00, 18.0),
    ("00:25", 100_500.00, 101_100.00, 100_300.00, 100_800.00, 18.0),
    ("00:30", 100_800.00, 101_150.00, 100_500.00, 101_000.00, 15.0),
    ("00:35", 101_000.00, 101_400.00, 100_900.00, 101_250.00, 31.0),   # close > OR high -> LONG @ 101,250 (00:40)
    ("00:40", 101_250.00, 101_500.00, 101_000.00, 101_300.00, 20.0),
    ("00:45", 101_300.00, 101_400.00, 101_100.00, 101_250.00, 15.0),   # the 00:47 paper fill happens in this bar
]
RALLY = [  # opens below the target every bar; the 01:20 bar trades through 103,500
    ("00:50", 101_250.00, 101_700.00, 101_150.00, 101_600.00, 18.0),
    ("00:55", 101_600.00, 102_100.00, 101_500.00, 102_000.00, 18.0),
    ("01:00", 102_000.00, 102_500.00, 101_900.00, 102_400.00, 15.0),
    ("01:05", 102_400.00, 102_800.00, 102_200.00, 102_700.00, 15.0),
    ("01:10", 102_700.00, 103_100.00, 102_600.00, 103_000.00, 15.0),
    ("01:15", 103_000.00, 103_400.00, 102_900.00, 103_300.00, 15.0),
    ("01:20", 103_300.00, 103_650.00, 103_200.00, 103_550.00, 22.0),   # high >= 103,500 -> TARGET at 01:25
    ("01:25", 103_550.00, 103_700.00, 103_300.00, 103_400.00, 15.0),
]
SLIDE = [  # the 01:05 bar trades through 99,000
    ("00:50", 101_250.00, 101_300.00, 100_500.00, 100_600.00, 20.0),
    ("00:55", 100_600.00, 100_700.00, 99_900.00, 100_000.00, 20.0),
    ("01:00", 100_000.00, 100_200.00, 99_300.00, 99_500.00, 22.0),
    ("01:05", 99_500.00, 99_600.00, 98_800.00, 99_100.00, 30.0),       # low <= 99,000 -> STOP at 01:10
    ("01:10", 99_100.00, 99_400.00, 98_900.00, 99_200.00, 18.0),
]

ENTRY, STOP, TARGET = 101_250.00, 99_000.00, 103_500.00
RISK_PER_COIN = ENTRY - STOP                       # 2,250.00
FEE = 0.006                                        # the default crypto_fee_pct: 0.6% of notional per fill
QTY = 0.01448645                                   # $50 / (2,250 + 0.6% x (101,250 + 99,000)), floored to 1e-8
PRICE_RISK = round(QTY * RISK_PER_COIN, 2)         # 32.59
FEES_AT_STOP = round(QTY * FEE * (ENTRY + STOP), 2)  # 17.41 - counted as risk everywhere
MAX_LOSS = 50.00                                   # the default budget: 0.2% of $25,000
VERDICTS = ("ODDS LEAN YOUR WAY", "ODDS LEAN AGAINST YOU", "CLOSE TO A COIN FLIP")
PAPER_LINE = "Paper trading only - nothing here can place a real order."
SAYS_PAPER = {"scanner", "entry", "exit", "time-machine", "risk", "budget", "broker", "analyst"}   # the order chain
NOT_PAPER = re.compile(r"REAL MONEY: ON|live (?:order|trading|mode)|(?:sent|routed|submitted) to Coinbase"
                       r"|order (?:was )?placed", re.IGNORECASE)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Yahoo! Finance: BTC-USD News</title>
<item><title>Bitcoin climbs past $101,000 as ETF inflows jump</title>
<link>https://finance.yahoo.com/news/bitcoin-climbs-past-101000-etf-inflows-003000111.html</link>
<pubDate>Sat, 31 Oct 2026 00:30:00 +0000</pubDate>
<description>Spot bitcoin funds took in fresh money on Friday.</description></item>
<item><title>Bitcoin weekly outlook: traders eye the 100,000 level</title>
<link>https://finance.yahoo.com/news/bitcoin-weekly-outlook-traders-eye-120000222.html</link>
<pubDate>Tue, 27 Oct 2026 12:00:00 +0000</pubDate></item>
</channel></rss>
"""
SEARCH = {"news": [
    {"uuid": "c1", "title": "Bitcoin climbs past $101,000 as ETF inflows jump", "publisher": "Bloomberg",
     "link": "https://finance.yahoo.com/news/bitcoin-climbs-past-101000-etf-inflows-003000111.html",
     "providerPublishTime": utc_ts(DAY, "00:30"), "relatedTickers": [SYM]},
    {"uuid": "c2", "title": "Crypto exchange wins approval for a new derivatives desk", "publisher": "Reuters",
     "link": "https://www.reuters.com/markets/crypto-exchange-approval-2026-10-30/",
     "providerPublishTime": utc_ts(PREV, "22:10"), "relatedTickers": [SYM, "ETH-USD"]},
]}


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _shift(t: str, hours: int) -> str:
    """'00:41:30' + 12 h -> '12:41:30' (same UTC day)."""
    h, m, *s = t.split(":")
    return ":".join([_hhmm((int(h) + hours) * 60 + int(m))] + s)


def _bars(rows: Sequence[Tuple[str, float, float, float, float, float]], hours: int = 0) -> List[tc.Bar]:
    return [tc.Bar(utc_ts(DAY, _shift(t, hours)), o, h, l, c, v) for t, o, h, l, c, v in rows]


def _history_5m() -> List[tc.Bar]:
    """61 past UTC days of 24/7 5-minute bars, scaled so the last close is today's open (no gap)."""
    raw = crypto_history(PREV, 61, start_price=95_000.0, vol=0.0015, seed=31)
    k = OPEN_PX / raw[-1].close
    return [tc.Bar(b.ts, round(b.open * k, 2), round(b.high * k, 2), round(b.low * k, 2),
                   round(b.close * k, 2), b.volume) for b in raw]


def _history_daily() -> List[tc.Bar]:
    """~5 months of UTC-day candles ending yesterday at today's open: an uptrend into a 4-week base
    under 100,600, so the scanner reads today's push to 101,250 as a 20-day breakout."""
    rng = random.Random(17)
    closes = [60_000.0]
    for _ in range(119):
        closes.append(closes[-1] * math.exp(rng.gauss(0.004, 0.02)))
    k = 96_000.0 / closes[-1]
    closes = [round(c * k, 2) for c in closes]
    closes += [round(98_500.0 + 1_500.0 * math.sin(i * 0.9), 2) for i in range(28)] + [OPEN_PX]
    first = utc_ts(PREV, "00:00") - (len(closes) - 1) * 86400
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(tc.Bar(first + i * 86400, prev, round(max(prev, c) * 1.006, 2), round(min(prev, c) * 0.994, 2),
                          c, 2_800.0 + 150.0 * (i % 5)))
        prev = c
    return out


def _quiet_morning(hours: int) -> List[tc.Bar]:
    """00:00 to `hours`:00 UTC: a quiet range around today's open, ending at it. No bar closes outside
    the first 15 minutes' range, so the default 00:00 UTC ORB never triggers (EXPIRED at 02:00)."""
    t0, n = utc_ts(DAY, "00:00"), hours * 12
    closes = [round(OPEN_PX + 200.0 * math.sin(i * 0.3), 2) for i in range(1, n)] + [OPEN_PX]
    out, prev = [], OPEN_PX
    for i, c in enumerate(closes):
        wick = 300.0 if i < 3 else 40.0
        out.append(tc.Bar(t0 + i * 300, prev, round(max(prev, c) + wick, 2), round(min(prev, c) - wick, 2), c,
                          8.0 + (i % 4)))
        prev = c
    return out


HISTORY_5M = _history_5m()
HISTORY_1D = _history_daily()


class CryptoDeskTestCase(TempHomeTestCase):
    """One shared $DAYTRADER_HOME and fixture folder; every skill runs as a real subprocess.
    HOURS shifts the whole tape (and every clock time below) later into the UTC day."""

    HOURS = 0

    def setUp(self) -> None:
        super().setUp()
        self.fixtures = self.home / "fixtures"
        self.state = self.home / "desk"
        self.fixtures.mkdir()
        self.state.mkdir()
        self.outputs: List[Tuple[str, str]] = []   # (skill, stdout) of every step, for the paper checks
        write_coinbase_products(self.fixtures)
        write_fixture(self.fixtures, f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={SYM}"
                                     "&region=US&lang=en-US", RSS)
        write_fixture(self.fixtures, f"https://{tc.YAHOO_HOSTS[0]}/v1/finance/search?q={SYM}"
                                     "&quotesCount=0&newsCount=20", SEARCH)

    def at(self, t: str) -> str:
        return _shift(t, self.HOURS)

    def session(self, after: Sequence[Tuple[str, float, float, float, float, float]]) -> List[tc.Bar]:
        return (_quiet_morning(self.HOURS) if self.HOURS else []) + _bars(NIGHT, self.HOURS) + _bars(after, self.HOURS)

    # -- fixtures --------------------------------------------------------------------------

    def publish(self, now: str, session: Sequence[tc.Bar], price: float) -> None:
        """Write the Coinbase responses as the live API would answer at `now` (finished bars only)."""
        h, m, *s = self.at(now).split(":")
        now_ts = utc_ts(DAY, f"{h}:{m}") + (int(s[0]) if s else 0)
        today = [b for b in session if b.ts + 300 <= now_ts]
        for rng in ("2d", "5d", "60d"):
            write_coinbase_fixtures(self.fixtures, SYM, HISTORY_5M + today, "5m", rng, now_ts)
        day_bar = tc.Bar(utc_ts(DAY, "00:00"), today[0].open, max(b.high for b in today),
                         min(b.low for b in today), today[-1].close, sum(b.volume for b in today))
        write_coinbase_fixtures(self.fixtures, SYM, HISTORY_1D + [day_bar], "1d", "6mo", now_ts)
        stamp = datetime.fromtimestamp(now_ts - 5, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_coinbase_quote(self.fixtures, SYM, price, when=stamp, open_24h=HISTORY_5M[-288].open)

    # -- running the skills ----------------------------------------------------------------

    def run_text(self, skill: str, script: str, *args: str, now: str, expect: int = 0) -> str:
        """`now` is a UTC time today, before the HOURS shift (00:41 or 00:41:30)."""
        env = {"DAYTRADER_HOME": str(self.state), "DAYTRADER_FIXTURES": str(self.fixtures),
               "DAYTRADER_NOW": f"{DAY}T{self.at(now)}Z"}
        p = run_cli(skill_script(skill, script), *args, env=env, timeout=90)
        where = (f"{script} {' '.join(args)} @ {self.at(now)} UTC: exit {p.returncode}\nstderr: {p.stderr}\n"
                 f"stdout: {p.stdout[:3000]}")
        self.assertNotIn("Traceback", p.stderr, where)
        self.assertEqual(p.returncode, expect, where)
        self.outputs.append((skill, p.stdout))
        return p.stdout

    def run_skill(self, skill: str, script: str, *args: str, now: str, expect: int = 0) -> Dict[str, Any]:
        return json.loads(self.run_text(skill, script, *args, "--json", now=now, expect=expect))

    def plan(self) -> Dict[str, Any]:
        return json.loads((self.state / "plans" / f"{SYM}.json").read_text(encoding="utf-8"))

    def ledger(self) -> Dict[str, Any]:
        return json.loads((self.state / "ledger.json").read_text(encoding="utf-8"))

    def assert_paper_only(self) -> None:
        """Nothing printed anywhere in the run refers to a real order or real money, every trading
        skill says it is paper only, and nothing is on the US clock."""
        self.assertTrue(self.outputs)
        for skill, out in self.outputs:
            self.assertIsNone(NOT_PAPER.search(out), out[:2000])
            self.assertNotRegex(out, r"\bET\b")           # a crypto run is on the UTC clock throughout
            data = json.loads(out) if out.lstrip().startswith("{") else None
            if skill in SAYS_PAPER:                      # (news and alerts only read prices and headlines)
                said = (PAPER_LINE in str(data.get("disclaimer")) or "PAPER · REAL MONEY: OFF" in str(data.get("card"))
                        if data is not None else PAPER_LINE in out)
                self.assertTrue(said, f"{skill}: {out[:500]}")
            if data is not None:
                self.assertIs(data.get("real_money", False), False)
                if "action" in data:                     # the broker: the result, its ticket and trade
                    for part in (data, data.get("ticket") or {}, data.get("trade") or {}):
                        self.assertEqual(part.get("mode", "paper"), "paper")

    # -- the shared night: scan, plan, test, size, ticket, yes ------------------------------

    def plan_and_fill(self, after: Sequence[Tuple[str, float, float, float, float, float]],
                      entry_args: Sequence[str] = ()) -> Dict[str, Any]:
        session = self.session(after)

        # Scanner and news at 00:41 UTC on a Saturday: Coinbase daily candles, no gap setups.
        self.publish("00:41", session, ENTRY)
        scan = self.run_skill("scanner", "scan.py", "--symbols", SYM, now="00:41")
        self.assertEqual((scan["market"], scan["scanned"], scan["skipped"], scan["passed_filters"]),
                         ("crypto", 1, 0, 1), scan)
        self.assertTrue(scan["market_open"])                    # Saturday: open, since crypto is 24/7
        self.assertTrue(scan["as_of"].endswith("+00:00"), scan["as_of"])
        if not self.HOURS:          # at 00:41 the UTC day's volume is paced over 41 minutes: a breakout
            hit = scan["results"][0]
            self.assertEqual((hit["symbol"], hit["date"], hit["bias"], hit["last"]), (SYM, DAY, "long", ENTRY))
            self.assertEqual([s["setup"] for s in hit["setups"]], ["breakout"], hit)
            self.assertEqual(hit["key_level"], {"name": "high_20d", "price": 100_600.00})
            self.assertGreaterEqual(hit["rvol"], 1.5)
        self.assertTrue(any("gap setups (gap_up, gap_down) are skipped" in n for n in scan["notes"]), scan["notes"])
        self.assertEqual(scan["filters"]["min_price"], 0.0)

        news = self.run_skill("news", "news.py", SYM, now="00:41")
        self.assertEqual((news["market"], news["tz"], news["warnings"]), ("crypto", "UTC", []))
        sec = next(s for s in news["sources"] if s["source"] == "sec-edgar")
        self.assertEqual((sec["not_applicable"], sec["ok"], sec["failed"]), ([SYM], [], {}))   # never asked
        self.assertEqual([it["title"] for it in news["items"]][0], "Bitcoin climbs past $101,000 as ETF inflows jump")

        # Entry: the ORB long triggers on a bar's close; --save starts the plan.
        ent = self.run_skill("entry", "entry.py", SYM, *entry_args, "--save", now="00:41:30")
        self.assertEqual(ent["status"], "TRIGGERED", ent)
        self.assertEqual((ent["setup"], ent["side"], ent["triggered_at"], ent["date"]),
                         ("orb", "long", self.at("00:40"), DAY))
        self.assertEqual((ent["entry"], ent["suggested_stop"], ent["risk_per_share"]), (ENTRY, STOP, RISK_PER_COIN))
        self.assertEqual((ent["reference"]["or_start"], ent["reference"]["or_end"]), (self.at("00:00"), self.at("00:15")))
        self.assertEqual((ent["reference"]["or_high"], ent["reference"]["or_low"]), (101_200.00, 99_000.00))
        self.assertEqual(ent["cutoff"], self.at("02:00"))         # OR start + 2 h
        self.assertFalse(ent["session_over"])
        self.assertTrue(ent["as_of"].endswith("+00:00"))
        plan = self.plan()
        self.assertEqual((plan["side"], plan["entry"], plan["stop"], plan["setup"]), ("long", ENTRY, STOP, "orb"))
        self.assertIsNone(plan.get("target"))

        # Exit: keep the OR-low stop, 1R target, flat by the 23:55 UTC time stop; fees shown in R.
        ex = self.run_skill("exit", "exit_plan.py", SYM, "--method", "manual", "--rr", "1", "--save", now="00:42")
        self.assertEqual((ex["entry_source"], ex["stop_source"], ex["side_source"]),
                         ("saved plan", "saved plan", "saved plan"))
        self.assertEqual((ex["side"], ex["entry"], ex["stop"], ex["target"], ex["time_stop"]),
                         ("long", ENTRY, STOP, TARGET, "23:55"))
        self.assertAlmostEqual(ex["rr"], 1.0, places=6)
        fees = ex["fees"]
        self.assertEqual(fees["fee_pct"], 0.6)
        self.assertAlmostEqual(fees["at_stop_r"], FEE * (ENTRY + STOP) / RISK_PER_COIN, places=3)     # 0.534R
        self.assertAlmostEqual(fees["at_target_r"], FEE * (ENTRY + TARGET) / RISK_PER_COIN, places=3)  # 0.546R
        self.assertTrue(any("paper fees (0.6% per fill)" in w for w in ex["warnings"]), ex["warnings"])
        self.assertTrue(ex["market_open"])
        self.assertTrue(ex["saved"])
        self.assertEqual(self.plan()["exit_plan"]["time_stop"], "23:55")

        # Time Machine: past UTC days, replayed to the 23:55 UTC time stop; the verdict counts the fees.
        tm = self.run_skill("time-machine", "time_machine.py", SYM, "--save", now="00:43")
        self.assertEqual(tm["levels_source"], {"entry": "saved plan", "stop": "saved plan", "target": "saved plan"})
        self.assertEqual((tm["side"], tm["entry"], tm["stop"], tm["target"]), ("long", ENTRY, STOP, TARGET))
        self.assertEqual((tm["mode"], tm["start"], tm["end"], tm["paths"], tm["sessions_used"]),
                         ("now", self.at("00:40"), "23:55", 10_000, 59))
        self.assertEqual((tm["first_session"], tm["last_session"]), ("2026-09-02", PREV))   # whole UTC days only
        self.assertAlmostEqual(tm["target_first_pct"] + tm["stop_first_pct"] + tm["open_at_close_pct"], 100.0,
                               delta=0.02)
        self.assertIn(tm["verdict"], VERDICTS)
        self.assertAlmostEqual(tm["fees"]["expected_r_after_fees"], tm["expected_r"] - tm["fees"]["expected_fees_r"],
                               delta=0.002)
        plan = self.plan()
        self.assertEqual(plan["time_machine"]["display_pct"], tm["display_pct"])
        self.assertEqual(plan["time_machine"]["fees"]["fee_pct"], 0.6)
        self.assertIn("exit_plan", plan)

        # Risk: a fractional size whose loss at the stop, fees included, is the $50 budget.
        rk = self.run_skill("risk", "risk.py", "size", "--symbol", SYM, "--save", now="00:44")
        self.assertEqual((rk["market"], rk["shares"], rk["order"], rk["unit"]), ("crypto", QTY, f"BUY {QTY} {SYM}", "BTC"))
        self.assertNotEqual(rk["shares"], int(rk["shares"]))
        self.assertEqual((rk["risk_used"], rk["fees_at_stop"], rk["max_loss"]), (PRICE_RISK, FEES_AT_STOP, MAX_LOSS))
        self.assertAlmostEqual(rk["loss_per_unit"], RISK_PER_COIN + FEE * (ENTRY + STOP), places=2)    # 3,451.50
        self.assertEqual((rk["step"], rk["tick"], rk["leverage"]), (1e-08, 0.01, 1))
        self.assertEqual(rk["sources"], {"entry": "plan", "stop": "plan", "side": "plan", "target": "plan"})
        self.assertTrue(rk["budget"]["ok"])
        self.assertEqual(self.plan()["sizing"]["shares"], QTY)

        # Budget: the UTC day, with the fees at the stop counted as risk.
        chk = self.run_skill("budget", "budget.py", "check", "--symbol", SYM, now="00:44:30")
        self.assertEqual((chk["ok"], chk["verdict"], chk["exit_code"], chk["market"], chk["date"]),
                         (True, "OK", 0, "crypto", DAY))
        self.assertEqual((chk["new_risk"], chk["qty"], chk["fees_at_stop"]), (MAX_LOSS, QTY, FEES_AT_STOP))

        # Broker: a ticket from the plan waits for the user's yes - on a Saturday it is a real ticket.
        self.publish("00:45:10", session, ENTRY)
        tk = self.run_skill("broker", "broker.py", "ticket", "--symbol", SYM, now="00:45:10")
        t = tk["ticket"]
        self.assertEqual((tk["status"], tk["preview"], t["status"], t["mode"], tk["real_money"]),
                         ("pending", False, "pending", "paper", False))
        self.assertEqual(t["id"], "TKT-20261031-001")                 # the UTC day's id
        self.assertEqual((t["market"], t["side"], t["qty"], t["entry"], t["stop"], t["target"]),
                         ("crypto", "long", QTY, ENTRY, STOP, TARGET))
        self.assertEqual((t["levels_source"], t["qty_source"], t["setup"], t["time_stop"]),
                         ("plan", "plan sizing", "orb", "23:55"))
        self.assertEqual((t["risk"], t["fees_at_stop"], t["max_loss"], t["fee_pct"]),
                         (PRICE_RISK, FEES_AT_STOP, MAX_LOSS, 0.6))
        self.assertTrue(t["created_at"].endswith("+00:00"))
        self.assertEqual(t["time_machine"]["display_pct"], tm["display_pct"])
        for words in (f"BUY {QTY} {SYM}", "PAPER · REAL MONEY: OFF · Coinbase prices", "WAITING FOR YOUR YES", t["id"]):
            self.assertIn(words, tk["card"])
        self.assertEqual(self.ledger()["trades"], [])

        # Without --yes nothing happens (exit 1), and the ticket keeps waiting.
        self.publish("00:47", session, ENTRY)
        no = self.run_skill("broker", "broker.py", "confirm", t["id"], now="00:46", expect=1)
        self.assertFalse(no["confirmed"])
        self.assertEqual(self.ledger()["trades"], [])
        self.assertEqual(self.ledger()["tickets"][0]["status"], "pending")

        # The user's explicit yes (a later message): a marketable limit fills at the Coinbase last price.
        yes = self.run_skill("broker", "broker.py", "confirm", t["id"], "--yes", now="00:47")
        self.assertEqual((yes["confirmed"], yes["status"], yes["fill_price"], yes["real_money"]),
                         (True, "filled", ENTRY, False))
        trade = yes["trade"]
        self.assertEqual((trade["id"], trade["status"], trade["market"], trade["mode"], trade["qty"],
                          trade["entry_price"], trade["ticket_id"]),
                         ("TRADE-20261031-001", "open", "crypto", "paper", QTY, ENTRY, t["id"]))
        self.assertEqual(trade["opened_at"], f"{DAY}T{self.at('00:47')}:00+00:00")
        self.assertAlmostEqual(trade["entry_fee"], FEE * ENTRY * QTY, delta=0.005)                  # $8.80
        self.assertEqual(trade["fees"], trade["entry_fee"])

        # Budget sees the open position as committed risk, fees at the stop included.
        st = self.run_skill("budget", "budget.py", "status", "--market", "crypto", now="00:48")
        self.assertEqual((st["date"], st["weekday"], st["status"], st["market_open"]),
                         (DAY, "Sat", "OPEN FOR TRADING", True))
        self.assertAlmostEqual(st["open_risk"]["total"], MAX_LOSS, places=2)
        self.assertEqual((st["trades_today"], len(st["positions"])), (1, 1))
        self.assertEqual(st["positions"][0]["fees_at_stop"], FEES_AT_STOP)

        # Alerts read the same plan: stop below / target above for a long.
        al = self.run_skill("alerts", "alerts.py", "from-plan", SYM, "--levels", "stop,target", now="00:48")
        self.assertEqual(sorted((a["plan_role"], a["kind"], a["level"]) for a in al["added"]),
                         [("stop", "below", STOP), ("target", "above", TARGET)])
        return {"session": session, "ticket": t, "trade": trade, "tm": tm, "risk": rk}

    def closed_trade(self) -> Dict[str, Any]:
        trades = self.ledger()["trades"]
        self.assertEqual(len(trades), 1)
        return trades[0]

    def assert_net_of_fees(self, tr: Dict[str, Any], exit_price: float) -> None:
        """Gross P&L from the prices, a fee on each fill, and pnl / R net of both."""
        sign = 1 if exit_price > ENTRY else -1
        self.assertAlmostEqual(tr["gross_pnl"], sign * PRICE_RISK, places=2)
        self.assertAlmostEqual(tr["entry_fee"], FEE * ENTRY * QTY, delta=0.005)
        self.assertAlmostEqual(tr["exit_fee"], FEE * exit_price * QTY, delta=0.005)
        self.assertAlmostEqual(tr["fees"], tr["entry_fee"] + tr["exit_fee"], places=2)
        self.assertAlmostEqual(tr["pnl"], tr["gross_pnl"] - tr["fees"], places=2)
        self.assertAlmostEqual(tr["r_multiple"], tr["pnl"] / PRICE_RISK, delta=0.006)
        self.assertEqual((tr["market"], tr["mode"], tr["fee_pct"]), ("crypto", "paper", 0.6))


class CryptoDeskFlowTest(CryptoDeskTestCase):
    """Two complete Saturday nights on the paper desk: one hits the target, one hits the stop."""

    def test_paper_trade_btc_target_hit_net_of_fees(self):
        m = self.plan_and_fill(RALLY)

        # 01:06: up but not there yet - the trade stays open.
        self.publish("01:06", m["session"], 102_700.00)
        mid = self.run_skill("broker", "broker.py", "sync", "--market", "crypto", now="01:06")
        self.assertEqual([e for e in mid["events"] if e.get("kind") == "closed"], [])
        self.assertEqual(self.closed_trade()["status"], "open")

        # 01:31: the 01:20 bar traded through 103,500 -> closed at the target, net of both fees.
        self.publish("01:31", m["session"], 103_400.00)
        sync = self.run_skill("broker", "broker.py", "sync", "--market", "crypto", now="01:31")
        ev = [e for e in sync["events"] if e.get("kind") == "closed"]
        self.assertEqual([(e["reason"], e["price"], e["bar"]) for e in ev],
                         [("target", TARGET, f"{DAY}T{self.at('01:20')}:00+00:00")])
        tr = self.closed_trade()
        self.assertEqual((tr["status"], tr["exit_reason"], tr["exit_price"]), ("closed", "target", TARGET))
        self.assertEqual(tr["closed_at"], f"{DAY}T{self.at('01:25')}:00+00:00")
        self.assert_net_of_fees(tr, TARGET)
        self.assertLess(tr["pnl"], tr["gross_pnl"])
        self.assertLess(tr["r_multiple"], 1.0)                   # a 1R target nets less than 1R after fees
        self.assertEqual((ev[0]["pnl"], ev[0]["fees"]), (tr["pnl"], tr["fees"]))
        fired = self.run_skill("alerts", "alerts.py", "check", now="01:31")["fired"]
        self.assertEqual([(f["plan_role"], f["level"]) for f in fired], [("target", TARGET)])

        st = self.run_skill("budget", "budget.py", "status", "--market", "crypto", now="01:32")
        self.assertEqual((st["date"], st["status"]), (DAY, "OPEN FOR TRADING"))
        self.assertAlmostEqual(st["realized_pnl"], tr["pnl"], places=2)
        self.assertEqual((st["wins"], st["losses"], st["closed_today"], st["trades_today"], st["loss_streak"]),
                         (1, 0, 1, 1, 0))
        self.assertEqual((st["open_risk"]["total"], st["loss_budget"]["used"]), (0, 0))

        hist = self.run_skill("broker", "broker.py", "history", "--market", "crypto", now="01:32")
        tot = hist["totals"]
        self.assertEqual((tot["trades"], tot["net_pnl"], tot["fees"], tot["gross_pnl"]),
                         (1, tr["pnl"], tr["fees"], tr["gross_pnl"]))

        for args in ((), ("--market", "crypto")):
            rv = self.run_skill("analyst", "analyst.py", "review", *args, now="01:33")
            self.assertEqual((rv["count"], rv["wins"], rv["losses"]), (1, 1, 0), args)
            self.assertAlmostEqual(rv["net_pnl"], tr["pnl"], places=2)
            self.assertAlmostEqual(rv["total_r"], tr["r_multiple"], places=2)
            self.assertEqual(rv["crypto_fees"], tr["fees"])
            self.assertEqual([b["key"] for b in rv["by_market"]], ["crypto"])
            self.assertEqual([b["key"] for b in rv["by_hour"]], [f"{self.at('00:00')[:2]}:00 UTC"])
            self.assertEqual(rv["trades"][0]["exit"], "target")
        # The one Time Machine result reads the same on every card: TM, risk, broker and the review.
        shown = m["tm"]["display_pct"]
        self.assertEqual((rv["time_machine_check"]["predicted_target_first_pct"],
                          rv["time_machine_check"]["predicted_stop_first_pct"]),
                         (shown["target_first"], shown["stop_first"]))
        self.assertEqual(rv["time_machine_check"]["actual_target_pct"], 100.0)
        words = f"target first {shown['target_first']:.1f}% / stop first {shown['stop_first']:.1f}%"
        self.assertIn(f"time-machine: {words} - {m['tm']['verdict']}", m["risk"]["notes"])
        self.assertEqual(m["ticket"]["time_machine"]["display_pct"], shown)
        text = self.run_text("analyst", "analyst.py", "review", "--market", "crypto", now="01:33")
        self.assertIn(f"Crypto paper fees {tc.fmt_money(tr['fees'])} - crypto P&L and R are net of them", text)
        self.assertIn(f"JOURNAL REVIEW · crypto only · last 30 days (2026-10-02 to {DAY})", text)
        self.assert_paper_only()

    def test_paper_trade_btc_stop_hit_costs_the_budget_fees_included(self):
        m = self.plan_and_fill(SLIDE)

        # 01:11: the 01:05 bar traded through 99,000 -> stopped out. Price risk $32.59 + fees = the $50 budget.
        self.publish("01:11", m["session"], 99_200.00)
        sync = self.run_skill("broker", "broker.py", "sync", "--market", "crypto", now="01:11")
        self.assertEqual(sync["summary"]["losses"], 1, sync)
        tr = self.closed_trade()
        self.assertEqual((tr["status"], tr["exit_reason"], tr["exit_price"]), ("closed", "stop", STOP))
        self.assertEqual(tr["closed_at"], f"{DAY}T{self.at('01:10')}:00+00:00")
        self.assert_net_of_fees(tr, STOP)
        self.assertAlmostEqual(tr["pnl"], -MAX_LOSS, delta=0.02)        # what risk sized for, fees included
        self.assertLess(tr["r_multiple"], -1.0)                         # R is per price risk: fees make it worse
        fired = self.run_skill("alerts", "alerts.py", "check", now="01:11")["fired"]
        self.assertEqual([(f["plan_role"], f["level"]) for f in fired], [("stop", STOP)])

        st = self.run_skill("budget", "budget.py", "status", "--market", "crypto", now="01:12")
        self.assertAlmostEqual(st["realized_pnl"], tr["pnl"], places=2)
        self.assertEqual((st["wins"], st["losses"], st["loss_streak"], st["trades_today"]), (0, 1, 1, 1))
        self.assertAlmostEqual(st["loss_budget"]["used"], -tr["pnl"], places=2)
        self.assertEqual(st["open_risk"]["total"], 0)

        # The next crypto ticket's budget check counts today's (UTC) realized loss.
        chk = self.run_skill("budget", "budget.py", "check", "--symbol", SYM, "--risk", "110", now="01:13", expect=3)
        self.assertEqual(chk["verdict"], "BLOCKED")

        rv = self.run_skill("analyst", "analyst.py", "review", "--market", "crypto", now="01:14")
        self.assertEqual((rv["count"], rv["wins"], rv["losses"]), (1, 0, 1))
        self.assertAlmostEqual(rv["net_pnl"], tr["pnl"], places=2)
        self.assertEqual(rv["time_machine_check"]["actual_stop_pct"], 100.0)
        self.assert_paper_only()


class AnchoredRangeTest(CryptoDeskTestCase):
    """The same tape 12 hours later (Saturday 12:00-13:30 UTC = 08:00-09:30 ET, still the weekend):
    the default 00:00 UTC range has expired, so `entry --or-start 12:00` anchors the ORB."""

    HOURS = 12

    def test_or_start_anchors_the_range_later_in_the_day(self):
        self.publish("00:41", self.session(RALLY), ENTRY)
        ent = self.run_skill("entry", "entry.py", SYM, now="00:41:30")
        self.assertEqual((ent["status"], ent["cutoff"]), ("EXPIRED", "02:00"), ent)
        self.assertTrue(any("02:00 UTC is only the default cutoff" in n for n in ent["notes"]), ent["notes"])

        m = self.plan_and_fill(RALLY, entry_args=("--or-start", "12:00"))
        self.assertEqual(self.plan()["entry_signal"]["or_start"], "12:00")
        self.publish("01:31", m["session"], 103_400.00)
        self.run_skill("broker", "broker.py", "sync", "--market", "crypto", now="01:31")
        tr = self.closed_trade()
        self.assertEqual((tr["exit_reason"], tr["closed_at"]), ("target", f"{DAY}T13:25:00+00:00"))
        self.assert_net_of_fees(tr, TARGET)
        self.assert_paper_only()


# The README's crypto desk-run commands, in order, and the UTC time each one runs at.
DESK_STEPS = [("scanner", "scan.py", "00:41"), ("news", "news.py", "00:41"), ("entry", "entry.py", "00:41:30"),
              ("exit", "exit_plan.py", "00:42"), ("time-machine", "time_machine.py", "00:43"),
              ("risk", "risk.py", "00:44"), ("budget", "budget.py", "00:44:30"),
              ("broker", "broker.py", "00:45:10"), ("broker", "broker.py", "00:47"),
              ("broker", "broker.py", "01:31"), ("budget", "budget.py", "01:32"),
              ("analyst", "analyst.py", "01:33")]
QUOTES = {"00:41": ENTRY, "00:45:10": ENTRY, "00:47": ENTRY, "01:31": 103_400.00}   # Coinbase last price


def readme_block(after: str, lang: str) -> List[str]:
    """The non-blank lines of README.md's first ```<lang> block that follows the text `after`."""
    text = README.read_text(encoding="utf-8")
    start = text.index(f"```{lang}\n", text.index(after)) + len(lang) + 4
    return [ln for ln in text[start:text.index("```", start)].splitlines() if ln.strip()]


def readme_commands() -> List[Tuple[str, str, List[str]]]:
    """(skill, script, args) for each command of the README's crypto desk-run block, comments dropped."""
    out = []
    for line in readme_block(RUN_HEADING, "bash"):
        words = shlex.split(line, comments=True)
        if not words:
            continue                                        # a comment line
        assert words[0] == "python3" and words[1].startswith("skills/"), line
        _, skill, _, script = words[1].split("/")
        out.append((skill, script, words[2:]))
    return out


class CryptoReadmeSampleTest(CryptoDeskTestCase):
    """The README's crypto desk-run commands work as written, and its sample output is real output."""

    def night(self) -> List[Tuple[str, str]]:
        """Run the README's commands in text mode on the night that hits the target."""
        session = self.session(RALLY)
        commands = readme_commands()
        self.assertEqual([(skill, script) for skill, script, _ in commands],
                         [(skill, script) for skill, script, _ in DESK_STEPS])
        out = []
        for (skill, script, args), (_, _, now) in zip(commands, DESK_STEPS):
            if now in QUOTES:
                self.publish(now, session, QUOTES[now])
            out.append((f"$ {skill}/scripts/{script} {' '.join(args)}   # {DAY} (Saturday) {now} UTC",
                        self.run_text(skill, script, *args, now=now)))
        return out

    def test_readme_crypto_sample_lines_are_printed_by_the_readme_commands(self):
        printed = {ln.strip() for _, text in self.night() for ln in text.splitlines()}
        sample = readme_block(SAMPLE_INTRO, "text")
        self.assertGreaterEqual(len(sample), 12)
        missing = [ln for ln in sample if ln.strip() not in printed]
        self.assertEqual(missing, [], "README crypto sample lines that no step printed (regenerate with "
                                      "python3 tests/test_crypto_desk_flow.py --print-run)")
        for words in ("BTC-USD", "breakout", "ORB(15)", "UTC", "THE PLAN", "Fees", "TIME MACHINE",
                      "BUY 0.01448645 BTC-USD", "BUDGET CHECK", "WAITING FOR YOUR YES", "net of", "REAL MONEY: OFF"):
            self.assertTrue(any(words in ln for ln in sample), words)
        self.assertFalse([ln for ln in sample if re.search(r"\bET\b", ln)])
        self.assert_paper_only()

    def test_readme_crypto_section_is_honest_and_its_commands_are_real(self):
        text = README.read_text(encoding="utf-8")
        section = text[text.index(CRYPTO_SECTION):text.index("\n## ", text.index(CRYPTO_SECTION) + 1)]
        for words in ("UTC day", "23:55 UTC", "--or-start", "crypto_fee_pct", "0.6%", "--crypto-fee-pct",
                      "--market crypto", "fractional", "synthetic", "api.exchange.coinbase.com",
                      "no exchange account", "5-minute candles", "slippage", "estimate"):
            self.assertIn(words, section, words)
        # Every python3 command in the section (code blocks and inline code) parses with the scripts' own
        # argparse: no usage error, whatever the (empty) offline data then makes of it.
        commands = re.findall(r"python3 (skills/[\w-]+/scripts/\w+\.py)([^\n#`]*)", section)
        self.assertGreaterEqual(len(commands), 14)
        for path, rest in commands:
            _, skill, _, script = path.split("/")
            args = shlex.split(rest)
            p = run_cli(skill_script(skill, script), *args, *([] if "--json" in args else ["--json"]),
                        env={"DAYTRADER_HOME": str(self.state), "DAYTRADER_FIXTURES": str(self.fixtures),
                             "DAYTRADER_NOW": f"{DAY}T00:41Z"}, timeout=90)
            self.assertNotIn("usage:", p.stderr, f"{path} {rest}")
            self.assertNotIn("Traceback", p.stderr, f"{path} {rest}")


class UsDeskFlowUnchangedTest(unittest.TestCase):
    """The crypto section sits after the US desk flow, so the US README checks still read the US blocks."""

    def test_us_readme_blocks_are_still_the_tsla_run(self):
        self.assertEqual([(s, sc) for s, sc, _ in us_desk.readme_commands()],
                         [(s, sc) for s, sc, _ in us_desk.DESK_STEPS])
        self.assertTrue(all(us_desk.SYM in args for _, _, args in us_desk.readme_commands()))
        us_sample = us_desk.readme_block(us_desk.SAMPLE_INTRO, "text")
        self.assertTrue(any("TSLA" in ln for ln in us_sample))
        self.assertFalse([ln for ln in us_sample if SYM in ln or "UTC" in ln])
        text = README.read_text(encoding="utf-8")
        self.assertLess(text.index("## The desk flow"), text.index(CRYPTO_SECTION))
        self.assertLess(text.index(us_desk.SAMPLE_INTRO), text.index(SAMPLE_INTRO))


def print_run() -> None:
    """Print every step's text output, to regenerate the README's crypto sample block."""
    case = CryptoReadmeSampleTest("test_readme_crypto_sample_lines_are_printed_by_the_readme_commands")
    case.setUp()
    try:
        for header, text in case.night():
            print(header)
            print(text)
    finally:
        case.doCleanups()


if __name__ == "__main__":
    if sys.argv[1:] == ["--print-run"]:
        print_run()
    else:
        unittest.main()
