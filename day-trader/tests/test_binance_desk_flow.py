"""End-to-end crypto desk flow on Binance, the default: "Paper trade BTC" - BTC-USDT on a Saturday.

The desk of tests/test_crypto_desk_flow.py - the same tape on the same clock - priced from Binance's
public market data, the pack's default crypto source. Every skill runs as a real subprocess
(helpers.run_cli), offline, against Binance fixtures in $DAYTRADER_FIXTURES at the exact URLs
tc.binance_bars / binance_quote / binance_product_info request on data-api.binance.vision (5-minute
klines over 2d, 5d and 60d, daily klines over 6mo, the 24 h ticker, exchangeInfo), with one shared
$DAYTRADER_HOME and DAYTRADER_CRYPTO_EXCHANGE=binance set explicitly in every step's environment:

  scanner --symbols BTC-USDT -> news BTC-USDT -> entry --save -> exit_plan --method manual --rr 1 --save
  -> time_machine --save -> risk size --save -> budget check -> broker ticket -> confirm (no --yes:
  refused) -> confirm --yes (a later message) -> budget status -> alerts from-plan -> broker sync
  + alerts check -> budget status -> broker history -> analyst review

$DAYTRADER_NOW is a UTC time on Saturday 2026-10-31 (US stocks closed, crypto open). What Binance
changes against the Coinbase run: the pair is BTC-USDT (Binance's few USD pairs are thin), every card that
prices the pair says "Binance prices" and none says Coinbase, the paper fee is Binance's entry-tier
0.1% per fill instead of Coinbase's 0.6%, and the size is floored to BTCUSDT's LOT_SIZE step of
0.00001 BTC: $50 / (2,250 + 0.1% x (101,250 + 99,000)) = 0.0204061 BTC -> 0.0204. A stop-out then
costs $45.90 + $4.09 of fees = $49.99, inside the $50 budget, and the 1R target nets +$41.72
(+0.91R) after $4.18 of fees, where Coinbase's fees leave +$14.79 (+0.45R).

BinanceReadmeSampleTest runs the README's crypto desk-run commands on a fresh desk with no exchange
setting and no override at all - on the pack's default - and checks that every line of the README's
crypto sample block is really printed. To regenerate that block:
python3 tests/test_binance_desk_flow.py --print-run
"""
from __future__ import annotations

import os
import re
import shlex
import sys
import unittest
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (run_cli, skill_script, tc, write_binance_fixtures, write_binance_products,  # noqa: E402
                     write_binance_quote)
import test_crypto_desk_flow as cb  # noqa: E402  (the tape, the subprocess desk and the README parsing)
import test_desk_flow as us_desk  # noqa: E402  (the US desk flow and its README parsing)

SYM = "BTC-USDT"
PAIR = "BTCUSDT"                                   # Binance's own name for the pair
DAY = cb.DAY                                       # Saturday 2026-10-31
ENTRY, STOP, TARGET, RISK_PER_COIN = cb.ENTRY, cb.STOP, cb.TARGET, cb.RISK_PER_COIN
FEE = 0.001                                        # Binance's entry-tier taker fee: 0.1% of notional per fill
STEP = 0.00001                                     # BTCUSDT's LOT_SIZE stepSize
QTY = 0.0204                                       # $50 / (2,250 + 0.1% x (101,250 + 99,000)) = 0.0204061, floored
PRICE_RISK = round(QTY * RISK_PER_COIN, 2)         # 45.90
FEES_AT_STOP = round(QTY * FEE * (ENTRY + STOP), 2)  # 4.09 - counted as risk everywhere
MAX_LOSS = round(QTY * (RISK_PER_COIN + FEE * (ENTRY + STOP)), 2)   # 49.99: a stop-out, fees included
BUDGET = 50.00                                     # the default budget: 0.2% of $25,000
ENTRY_FEE = round(FEE * ENTRY * QTY, 2)            # 2.07
LABEL = "PAPER · REAL MONEY: OFF · Binance prices"
USD_NOTE = f"BTC-USD is priced from Binance's {PAIR} pair, quoted in USDT (Binance's few USD pairs are thin)"


class BinanceDeskTestCase(cb.CryptoDeskTestCase):
    """The crypto desk on Binance fixtures: BTC-USDT (LOT_SIZE step 0.00001, tick 0.01, NOTIONAL minimum
    5 USDT), with DAYTRADER_CRYPTO_EXCHANGE=binance in every step's environment."""

    EXCHANGE = "binance"
    SYM = SYM

    def write_products(self) -> None:
        write_binance_products(self.fixtures)

    def write_candles(self, bars: Sequence[tc.Bar], interval: str, range_: str, now_ts: int) -> None:
        write_binance_fixtures(self.fixtures, self.SYM, bars, interval, range_, now_ts)

    def write_quote(self, price: float, now_ts: int) -> None:
        write_binance_quote(self.fixtures, self.SYM, price, when_ts=now_ts - 5, open_24h=cb.HISTORY_5M[-288].open)

    def assert_binance_only(self) -> None:
        """Every step that priced the pair said Binance; nothing in the run mentions Coinbase."""
        self.assertEqual([skill for skill, out in self.outputs if "Coinbase" in out], [])
        led = self.ledger()
        self.assertEqual(([k["exchange"] for k in led["tickets"]], [t["exchange"] for t in led["trades"]]),
                         (["binance"], ["binance"]))

    # -- the shared night: scan, plan, test, size, ticket, yes ------------------------------

    def plan_and_fill(self, after: Sequence[Tuple[str, float, float, float, float, float]]) -> Dict[str, Any]:
        session = self.session(after)

        # Scanner and news at 00:41 UTC on a Saturday: Binance daily klines, no gap setups.
        self.publish("00:41", session, ENTRY)
        scan = self.run_skill("scanner", "scan.py", "--symbols", SYM, now="00:41")
        self.assertEqual((scan["market"], scan["scanned"], scan["skipped"], scan["passed_filters"]),
                         ("crypto", 1, 0, 1), scan)
        self.assertTrue(scan["market_open"])                    # Saturday: open, since crypto is 24/7
        hit = scan["results"][0]
        self.assertEqual((hit["symbol"], hit["date"], hit["bias"], hit["last"]), (SYM, DAY, "long", ENTRY))
        self.assertEqual([s["setup"] for s in hit["setups"]], ["breakout"], hit)
        self.assertEqual(hit["key_level"], {"name": "high_20d", "price": 100_600.00})
        self.assertGreaterEqual(hit["rvol"], 1.5)
        self.assertTrue(any("gap setups (gap_up, gap_down) are skipped" in n for n in scan["notes"]), scan["notes"])
        self.assertEqual(scan["filters"]["min_price"], 0.0)

        news = self.run_skill("news", "news.py", SYM, now="00:41")
        self.assertEqual((news["market"], news["tz"], news["warnings"]), ("crypto", "UTC", []))
        self.assertEqual(news["yahoo_symbols"], {SYM: cb.NEWS_SYM})    # Yahoo files the coin's news under BTC-USD
        sec = next(s for s in news["sources"] if s["source"] == "sec-edgar")
        self.assertEqual((sec["not_applicable"], sec["ok"], sec["failed"]), ([SYM], [], {}))   # never asked
        self.assertEqual([it["title"] for it in news["items"]][0], "Bitcoin climbs past $101,000 as ETF inflows jump")

        # Entry: the same ORB long on Binance's klines; --save starts the plan.
        ent = self.run_skill("entry", "entry.py", SYM, "--save", now="00:41:30")
        self.assertEqual(ent["status"], "TRIGGERED", ent)
        self.assertEqual((ent["setup"], ent["side"], ent["triggered_at"], ent["date"]), ("orb", "long", "00:40", DAY))
        self.assertEqual((ent["entry"], ent["suggested_stop"], ent["risk_per_share"]), (ENTRY, STOP, RISK_PER_COIN))
        self.assertEqual((ent["reference"]["or_high"], ent["reference"]["or_low"]), (101_200.00, 99_000.00))
        self.assertFalse([n for n in ent["notes"] if "priced from" in n])   # a USDT pair needs no currency note
        plan = self.plan()
        self.assertEqual((plan["symbol"], plan["side"], plan["entry"], plan["stop"]), (SYM, "long", ENTRY, STOP))

        # Exit: 1R target, the 23:55 UTC time stop, fees in R at Binance's 0.1% - small enough for no warning.
        ex = self.run_skill("exit", "exit_plan.py", SYM, "--method", "manual", "--rr", "1", "--save", now="00:42")
        self.assertEqual((ex["side"], ex["entry"], ex["stop"], ex["target"], ex["time_stop"]),
                         ("long", ENTRY, STOP, TARGET, "23:55"))
        fees = ex["fees"]
        self.assertEqual(fees["fee_pct"], 0.1)
        self.assertAlmostEqual(fees["round_trip_r"], 2 * FEE * ENTRY / RISK_PER_COIN, places=3)      # 0.090R
        self.assertAlmostEqual(fees["at_stop_r"], FEE * (ENTRY + STOP) / RISK_PER_COIN, places=3)     # 0.089R
        self.assertAlmostEqual(fees["at_target_r"], FEE * (ENTRY + TARGET) / RISK_PER_COIN, places=3)  # 0.091R
        self.assertFalse([w for w in ex["warnings"] if "paper fees" in w], ex["warnings"])
        self.assertTrue(ex["saved"])

        # Time Machine: 59 past UTC days of Binance klines to the 23:55 UTC time stop, judged after 0.1% fees.
        tm = self.run_skill("time-machine", "time_machine.py", SYM, "--save", now="00:43")
        self.assertEqual((tm["side"], tm["entry"], tm["stop"], tm["target"]), ("long", ENTRY, STOP, TARGET))
        self.assertEqual((tm["mode"], tm["start"], tm["end"], tm["paths"], tm["sessions_used"]),
                         ("now", "00:40", "23:55", 10_000, 59))
        self.assertEqual((tm["first_session"], tm["last_session"]), ("2026-09-02", cb.PREV))
        self.assertAlmostEqual(tm["target_first_pct"] + tm["stop_first_pct"] + tm["open_at_close_pct"], 100.0,
                               delta=0.02)
        self.assertIn(tm["verdict"], cb.VERDICTS)
        self.assertEqual(tm["fees"]["fee_pct"], 0.1)
        self.assertAlmostEqual(tm["fees"]["expected_r_after_fees"], tm["expected_r"] - tm["fees"]["expected_fees_r"],
                               delta=0.002)
        self.assertEqual(self.plan()["time_machine"]["fees"]["fee_pct"], 0.1)

        # Risk: floored to BTCUSDT's 0.00001 step; the loss at the stop, fees included, stays inside $50.
        rk = self.run_skill("risk", "risk.py", "size", "--symbol", SYM, "--save", now="00:44")
        self.assertEqual((rk["market"], rk["exchange"], rk["shares"], rk["order"], rk["unit"]),
                         ("crypto", "binance", QTY, f"BUY {tc.fmt_qty(QTY)} {SYM}", "BTC"))
        self.assertEqual((rk["step"], rk["tick"], rk["fee_pct"], rk["leverage"]), (STEP, 0.01, 0.1, 1))
        self.assertEqual((rk["risk_used"], rk["fees_at_stop"], rk["max_loss"]), (PRICE_RISK, FEES_AT_STOP, MAX_LOSS))
        self.assertAlmostEqual(rk["loss_per_unit"], RISK_PER_COIN + FEE * (ENTRY + STOP), places=2)   # 2,450.25
        self.assertLessEqual(rk["max_loss"], BUDGET)
        self.assertGreater(round((rk["shares"] + STEP) * rk["loss_per_unit"], 2), BUDGET)   # one step more: over
        self.assertTrue(rk["budget"]["ok"])
        sizing = self.plan()["sizing"]
        self.assertEqual((sizing["shares"], sizing["exchange"], sizing["step"], sizing["fee_pct"]),
                         (QTY, "binance", STEP, 0.1))

        # Budget: the UTC day, with the fees at the stop counted as risk.
        chk = self.run_skill("budget", "budget.py", "check", "--symbol", SYM, now="00:44:30")
        self.assertEqual((chk["ok"], chk["verdict"], chk["exit_code"], chk["market"], chk["date"]),
                         (True, "OK", 0, "crypto", DAY))
        self.assertEqual((chk["qty"], chk["fees_at_stop"]), (QTY, FEES_AT_STOP))
        self.assertAlmostEqual(chk["new_risk"], MAX_LOSS, places=2)          # 49.9851, kept to 4 decimals

        # Broker: a ticket from the plan, on Binance prices, waits for the user's yes.
        self.publish("00:45:10", session, ENTRY)
        tk = self.run_skill("broker", "broker.py", "ticket", "--symbol", SYM, now="00:45:10")
        t = tk["ticket"]
        self.assertEqual((tk["status"], tk["preview"], t["status"], t["mode"], tk["real_money"]),
                         ("pending", False, "pending", "paper", False))
        self.assertEqual((t["id"], t["market"], t["exchange"]), ("TKT-20261031-001", "crypto", "binance"))
        self.assertEqual((t["side"], t["qty"], t["entry"], t["stop"], t["target"]), ("long", QTY, ENTRY, STOP, TARGET))
        self.assertEqual((t["qty_source"], t["risk"], t["fees_at_stop"], t["max_loss"], t["fee_pct"]),
                         ("plan sizing", PRICE_RISK, FEES_AT_STOP, MAX_LOSS, 0.1))
        for words in (f"BUY {tc.fmt_qty(QTY)} {SYM}", LABEL, "WAITING FOR YOUR YES", t["id"]):
            self.assertIn(words, tk["card"])
        self.assertFalse([w for w in tk["warnings"] if "minimum order" in w or "sizing was made for" in w],
                         tk["warnings"])                      # $2,065 is far above the 5 USDT minimum
        self.assertEqual(self.ledger()["trades"], [])

        # Without --yes nothing happens (exit 1), and the ticket keeps waiting.
        self.publish("00:47", session, ENTRY)
        no = self.run_skill("broker", "broker.py", "confirm", t["id"], now="00:46", expect=1)
        self.assertFalse(no["confirmed"])
        self.assertEqual(self.ledger()["tickets"][0]["status"], "pending")

        # The user's explicit yes, in a later message: a marketable limit fills at Binance's last price.
        yes = self.run_skill("broker", "broker.py", "confirm", t["id"], "--yes", now="00:47")
        self.assertEqual((yes["confirmed"], yes["status"], yes["fill_price"], yes["real_money"]),
                         (True, "filled", ENTRY, False))
        trade = yes["trade"]
        self.assertEqual((trade["id"], trade["status"], trade["market"], trade["exchange"], trade["mode"]),
                         ("TRADE-20261031-001", "open", "crypto", "binance", "paper"))
        self.assertEqual((trade["qty"], trade["entry_price"], trade["fee_pct"], trade["entry_fee"], trade["fees"]),
                         (QTY, ENTRY, 0.1, ENTRY_FEE, ENTRY_FEE))
        self.assertEqual(trade["opened_at"], f"{DAY}T00:47:00+00:00")
        text = self.run_text("broker", "broker.py", "positions", "--market", "crypto", now="00:47:30")
        self.assertRegex(text, rf"TRADE-20261031-001  {SYM}  LONG  {tc.fmt_qty(QTY)}  101,250.00 ")
        self.assertIn("Crypto P&L is net of the paper fees of both fills (Binance prices).", text)

        # Budget sees the open position as committed risk, fees at the stop included.
        st = self.run_skill("budget", "budget.py", "status", "--market", "crypto", now="00:48")
        self.assertEqual((st["date"], st["weekday"], st["status"], st["market_open"]), (DAY, "Sat", "OPEN FOR TRADING", True))
        self.assertAlmostEqual(st["open_risk"]["total"], MAX_LOSS, places=2)
        self.assertEqual((st["trades_today"], len(st["positions"])), (1, 1))
        self.assertEqual((st["positions"][0]["fees_at_stop"], st["positions"][0]["exchange"]), (FEES_AT_STOP, "binance"))

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
        """Gross P&L from the prices, Binance's 0.1% on each fill, and pnl / R net of both."""
        sign = 1 if exit_price > ENTRY else -1
        exit_fee = round(FEE * exit_price * QTY, 2)
        self.assertEqual((tr["gross_pnl"], tr["entry_fee"], tr["exit_fee"]), (sign * PRICE_RISK, ENTRY_FEE, exit_fee))
        self.assertAlmostEqual(tr["fees"], ENTRY_FEE + exit_fee, places=2)
        self.assertAlmostEqual(tr["pnl"], sign * PRICE_RISK - ENTRY_FEE - exit_fee, places=2)
        self.assertAlmostEqual(tr["r_multiple"], tr["pnl"] / PRICE_RISK, delta=0.006)
        self.assertEqual((tr["market"], tr["exchange"], tr["mode"], tr["fee_pct"], tr["qty"]),
                         ("crypto", "binance", "paper", 0.1, QTY))


class BinanceDeskFlowTest(BinanceDeskTestCase):
    """Two complete Saturday nights on Binance prices: one hits the target, one hits the stop."""

    def test_paper_trade_btc_usdt_target_hit_net_of_binance_fees(self):
        m = self.plan_and_fill(cb.RALLY)

        # 01:06: up but not there yet - the trade stays open.
        self.publish("01:06", m["session"], 102_700.00)
        mid = self.run_skill("broker", "broker.py", "sync", "--market", "crypto", now="01:06")
        self.assertEqual([e for e in mid["events"] if e.get("kind") == "closed"], [])
        self.assertEqual(self.closed_trade()["status"], "open")

        # 01:31: the 01:20 kline traded through 103,500 -> closed at the target, net of both 0.1% fees.
        self.publish("01:31", m["session"], 103_400.00)
        text = self.run_text("broker", "broker.py", "sync", "--market", "crypto", now="01:31")
        tr = self.closed_trade()
        self.assertEqual((tr["status"], tr["exit_reason"], tr["exit_price"]), ("closed", "target", TARGET))
        self.assertEqual(tr["closed_at"], f"{DAY}T01:25:00+00:00")
        self.assert_net_of_fees(tr, TARGET)
        self.assertEqual((tr["pnl"], tr["fees"], tr["r_multiple"]), (41.72, 4.18, 0.91))   # 45.90 - 2.07 - 2.11
        self.assertIn(f"TRADE-20261031-001 {SYM} LONG {tc.fmt_qty(QTY)}: TARGET @ 103,500.00 in the 01:20 UTC bar · "
                      "+$41.72 +0.91R (net of $4.18 fees)", text)
        fired = self.run_skill("alerts", "alerts.py", "check", now="01:31")["fired"]
        self.assertEqual([(f["plan_role"], f["level"]) for f in fired], [("target", TARGET)])

        st = self.run_skill("budget", "budget.py", "status", "--market", "crypto", now="01:32")
        self.assertEqual((st["date"], st["status"], st["realized_pnl"]), (DAY, "OPEN FOR TRADING", 41.72))
        self.assertEqual((st["wins"], st["losses"], st["closed_today"], st["trades_today"], st["loss_streak"]),
                         (1, 0, 1, 1, 0))
        self.assertEqual((st["open_risk"]["total"], st["loss_budget"]["used"]), (0, 0))

        hist = self.run_skill("broker", "broker.py", "history", "--market", "crypto", now="01:32")
        self.assertEqual({k: hist["totals"][k] for k in ("trades", "net_pnl", "fees", "gross_pnl")},
                         {"trades": 1, "net_pnl": 41.72, "fees": 4.18, "gross_pnl": PRICE_RISK})

        rv = self.run_skill("analyst", "analyst.py", "review", "--market", "crypto", now="01:33")
        self.assertEqual((rv["count"], rv["wins"], rv["losses"], rv["net_pnl"], rv["crypto_fees"]), (1, 1, 0, 41.72, 4.18))
        self.assertAlmostEqual(rv["total_r"], 0.91, places=2)
        self.assertEqual(([b["key"] for b in rv["by_market"]], rv["trades"][0]["exit"]), (["crypto"], "target"))
        shown = m["tm"]["display_pct"]
        self.assertEqual((rv["time_machine_check"]["predicted_target_first_pct"],
                          rv["time_machine_check"]["actual_target_pct"]), (shown["target_first"], 100.0))
        text = self.run_text("analyst", "analyst.py", "review", "--market", "crypto", now="01:33")
        self.assertIn("Crypto paper fees $4.18 - crypto P&L and R are net of them", text)
        self.assert_binance_only()
        self.assert_paper_only()

    def test_paper_trade_btc_usdt_stop_hit_costs_the_budget_fees_included(self):
        m = self.plan_and_fill(cb.SLIDE)

        # 01:11: the 01:05 kline traded through 99,000 -> stopped out: $45.90 + $4.09 fees = $49.99 <= $50.
        self.publish("01:11", m["session"], 99_200.00)
        text = self.run_text("broker", "broker.py", "sync", "--market", "crypto", now="01:11")
        tr = self.closed_trade()
        self.assertEqual((tr["status"], tr["exit_reason"], tr["exit_price"]), ("closed", "stop", STOP))
        self.assertEqual(tr["closed_at"], f"{DAY}T01:10:00+00:00")
        self.assert_net_of_fees(tr, STOP)
        self.assertEqual((tr["pnl"], tr["fees"], tr["r_multiple"]), (-MAX_LOSS, 4.09, -1.09))
        self.assertIn(f"TRADE-20261031-001 {SYM} LONG {tc.fmt_qty(QTY)}: STOP @ 99,000.00 in the 01:05 UTC bar · "
                      "-$49.99 -1.09R (net of $4.09 fees)", text)
        fired = self.run_skill("alerts", "alerts.py", "check", now="01:11")["fired"]
        self.assertEqual([(f["plan_role"], f["level"]) for f in fired], [("stop", STOP)])

        st = self.run_skill("budget", "budget.py", "status", "--market", "crypto", now="01:12")
        self.assertEqual((st["realized_pnl"], st["wins"], st["losses"], st["loss_streak"], st["trades_today"]),
                         (-MAX_LOSS, 0, 1, 1, 1))
        self.assertEqual((st["loss_budget"]["used"], st["open_risk"]["total"]), (MAX_LOSS, 0))

        # The next crypto ticket's budget check counts today's (UTC) realized loss.
        chk = self.run_skill("budget", "budget.py", "check", "--symbol", SYM, "--risk", "110", now="01:13", expect=3)
        self.assertEqual(chk["verdict"], "BLOCKED")

        rv = self.run_skill("analyst", "analyst.py", "review", "--market", "crypto", now="01:14")
        self.assertEqual((rv["count"], rv["wins"], rv["losses"], rv["net_pnl"]), (1, 0, 1, -MAX_LOSS))
        self.assertEqual(rv["time_machine_check"]["actual_stop_pct"], 100.0)
        self.assert_binance_only()
        self.assert_paper_only()

    def test_btc_usd_is_priced_from_btcusdt_and_the_card_says_so(self):
        """Binance's few USD pairs are thin: BTC-USD reads the BTCUSDT klines and says so, as the README quotes."""
        self.publish("00:41", self.session(cb.RALLY), ENTRY)
        ent = self.run_skill("entry", "entry.py", "BTC-USD", now="00:41:30")
        self.assertEqual((ent["status"], ent["entry"], ent["triggered_at"]), ("TRIGGERED", ENTRY, "00:40"))
        self.assertIn(USD_NOTE, ent["notes"])
        self.assertIn(USD_NOTE, cb.readme_section())


class BinanceReadmeSampleTest(BinanceDeskTestCase):
    """The README's crypto desk-run commands work as written on the pack's default exchange, and its
    sample output is real output. No $DAYTRADER_CRYPTO_EXCHANGE and no saved setting: Binance must
    be what a new user gets."""

    EXCHANGE = None

    def night(self) -> List[Tuple[str, str]]:
        self.assertFalse((self.state / "settings.json").exists())
        self.assertNotIn(cb.EXCHANGE_ENV, os.environ)
        return cb.run_readme_night(self)

    def test_readme_crypto_sample_lines_are_printed_by_the_readme_commands(self):
        printed = {ln.strip() for _, text in self.night() for ln in text.splitlines()}
        sample = cb.readme_block(cb.SAMPLE_INTRO, "text")
        self.assertGreaterEqual(len(sample), 12)
        missing = [ln for ln in sample if ln.strip() not in printed]
        self.assertEqual(missing, [], "README crypto sample lines that no step printed (regenerate with "
                                      "python3 tests/test_binance_desk_flow.py --print-run)")
        for words in (SYM, "breakout", "ORB(15)", "UTC", "THE PLAN", "Fees", "0.1% per fill", "TIME MACHINE",
                      f"BUY {tc.fmt_qty(QTY)} {SYM}", "BUDGET CHECK", "WAITING FOR YOUR YES", "net of", LABEL):
            self.assertTrue(any(words in ln for ln in sample), words)
        self.assertFalse([ln for ln in sample if re.search(r"\bET\b", ln) or "Coinbase" in ln])
        self.assert_binance_only()
        self.assert_paper_only()


class BinanceReadmeTextTest(BinanceDeskTestCase):
    """The README's crypto section says what the code does, and every command in it is real."""

    def test_readme_crypto_section_is_honest_and_its_commands_are_real(self):
        section = cb.readme_section()
        for words in ("Binance", "data-api.binance.vision", "no exchange account", "no API key", "BTC-USDT",
                      "BTCUSDT", "BTC-USD", cb.SWITCH_TO_COINBASE, "api.exchange.coinbase.com",
                      "DAYTRADER_CRYPTO_EXCHANGE", "0.1%", "0.6%", "--crypto-fee-pct", "crypto_fee_pct",
                      "UTC day", "23:55 UTC", "--or-start", "--market crypto", "fractional", "0.00001",
                      "synthetic", "5-minute candles", "slippage", "estimate", "api.binance.com",
                      "geo", "paper trading only"):
            self.assertIn(words, section, words)
        self.assertLess(section.index("data-api.binance.vision"), section.index("api.exchange.coinbase.com"))
        # Every python3 command in the section (code blocks and inline code) parses with the scripts' own
        # argparse: no usage error, whatever the (empty) offline data then makes of it.
        commands = re.findall(r"python3 (skills/[\w-]+/scripts/\w+\.py)([^\n#`]*)", section)
        self.assertGreaterEqual(len(commands), 15)
        for path, rest in commands:
            _, skill, _, script = path.split("/")
            args = shlex.split(rest)
            p = run_cli(skill_script(skill, script), *args, *([] if "--json" in args else ["--json"]),
                        env={"DAYTRADER_HOME": str(self.state), "DAYTRADER_FIXTURES": str(self.fixtures),
                             "DAYTRADER_NOW": f"{DAY}T00:41Z", cb.EXCHANGE_ENV: "binance"}, timeout=90)
            self.assertNotIn("usage:", p.stderr, f"{path} {rest}")
            self.assertNotIn("Traceback", p.stderr, f"{path} {rest}")

    def test_network_table_lists_binance_first_and_coinbase_as_the_option(self):
        table = cb.readme_section("## Network access")
        rows = {ln.split("|")[1].strip(): ln for ln in table.splitlines() if ln.startswith("| `")}
        binance = rows["`data-api.binance.vision`"]
        coinbase = rows["`api.exchange.coinbase.com`"]
        for words in ("default", "klines", "ticker/24hr", "exchangeInfo", "no account or API key"):
            self.assertIn(words, binance, words)
        for words in ("crypto_exchange", "coinbase", "candles", "no account or API key"):
            self.assertIn(words, coinbase, words)
        self.assertLess(table.index(binance), table.index(coinbase))
        # Binance's main API host geo-blocks US IPs (HTTP 451) and the code never calls it: not a host to allow.
        self.assertNotIn("api.binance.com", " ".join(rows))
        self.assertEqual(tc.BINANCE_API, "https://data-api.binance.vision/api/v3")


class UsdPairWordingTest(unittest.TestCase):
    """Binance does list a few USD pairs (BTCUSD, ETHUSD, SOLUSD, BNBUSD - live on 2026-10-31, each with
    under $0.1M traded a day against BTCUSDT's ~$950M), so no card or doc may say it has none: BTC-USD is
    priced from BTCUSDT because that is where the market is."""

    FALSE_CLAIM = re.compile(r"(?:has|lists) no (?:USD|\{quote\}) pairs", re.IGNORECASE)

    def test_no_card_or_doc_says_binance_has_no_usd_pairs(self):
        root = cb.README.parent
        paths = [cb.README, root.parent / "README.md"] + sorted((root / "skills").glob("*/*.md"))
        paths += sorted(p for p in (root / "skills").glob("*/scripts/*.py") if p.name != "trader_common.py")
        paths += sorted((root / "tools").glob("*.py"))
        said = [f"{p.relative_to(root.parent)}: {m.group(0)}" for p in paths if p.exists()
                for m in self.FALSE_CLAIM.finditer(p.read_text(encoding="utf-8"))]
        self.assertEqual(said, [])
        self.assertIn("(Binance's few USD pairs are thin)", USD_NOTE)
        self.assertIn(USD_NOTE, cb.readme_section())


class UsDeskFlowUnchangedTest(unittest.TestCase):
    """The crypto section sits after the US desk flow, so the US README checks still read the US blocks."""

    def test_us_readme_blocks_are_still_the_tsla_run(self):
        self.assertEqual([(s, sc) for s, sc, _ in us_desk.readme_commands()],
                         [(s, sc) for s, sc, _ in us_desk.DESK_STEPS])
        self.assertTrue(all(us_desk.SYM in args for _, _, args in us_desk.readme_commands()))
        us_sample = us_desk.readme_block(us_desk.SAMPLE_INTRO, "text")
        self.assertTrue(any("TSLA" in ln for ln in us_sample))
        self.assertFalse([ln for ln in us_sample if "BTC-" in ln or "UTC" in ln or "Binance" in ln])
        text = cb.README.read_text(encoding="utf-8")
        self.assertLess(text.index("## The desk flow"), text.index(cb.CRYPTO_SECTION))
        self.assertLess(text.index(us_desk.SAMPLE_INTRO), text.index(cb.SAMPLE_INTRO))
        self.assertLess(text.index(cb.SAMPLE_INTRO), text.index(cb.COINBASE_INTRO))


def print_run() -> None:
    """Print every step's text output, to regenerate the README's crypto sample block."""
    case = BinanceReadmeSampleTest("test_readme_crypto_sample_lines_are_printed_by_the_readme_commands")
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
