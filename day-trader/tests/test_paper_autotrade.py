"""Tests for tools/paper_autotrade.py - paper trades confirmed under the owner's standing order. Offline.

Every skill runs as a real subprocess (helpers.run_cli runs the tool, the tool runs exit_plan.py,
risk.py and broker.py) on Binance and Yahoo fixtures in $DAYTRADER_FIXTURES, with $DAYTRADER_NOW
pinned: on that replay clock the confirmations run 11 seconds after the tickets, so the broker's
10-second rule is met by moving the clock, never by sleeping. The real-clock wait (one sleep for the
whole batch) is tested on its own with the clock mocked.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (ROOT, TempHomeTestCase, crypto_history, load_skill_module, random_walk_history,  # noqa: E402
                     run_cli, tc, utc_ts, write_binance_fixtures, write_binance_products, write_binance_quote,
                     write_fixture, yahoo_chart_url, yahoo_payload)

TOOL = ROOT / "tools" / "paper_autotrade.py"
_spec = importlib.util.spec_from_file_location("paper_autotrade", TOOL)
auto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(auto)

DAY = "2026-10-31"                    # a Saturday: crypto trades (on UTC days), US stocks do not
ORDER = "owner 2026-10-31: paper trades need no per-ticket yes"


class AutoTradeTestCase(TempHomeTestCase):
    """A desk in the temp $DAYTRADER_HOME (settings.json) trading Binance pairs at 04:40 UTC."""

    NOW = f"{DAY}T04:40"                 # naive: every crypto command reads it on the UTC clock

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fx"
        os.environ.update(DAYTRADER_FIXTURES=str(self.fx), DAYTRADER_CRYPTO_EXCHANGE="binance")
        write_binance_products(self.fx)
        tc.save_settings({"daily_max_loss": 1000, "max_open_risk": 1000})
        self.now_ts = utc_ts(DAY, "04:40")

    def pair(self, sym, start, seed):
        """Six UTC days of 5-minute klines to 04:40 for every request the skills make then (exit_plan's
        5d, the broker's 2d sync), and the 24 h ticker at the last close. Returns the bars."""
        bars = crypto_history(DAY, 6, start_price=start, until="04:40", seed=seed)
        for rng in ("5d", "2d"):
            write_binance_fixtures(self.fx, sym, bars, "5m", rng, self.now_ts)
        write_binance_quote(self.fx, sym, bars[-1].close, when_ts=self.now_ts - 5)
        return bars

    def tool(self, *args, expect=0, env=None):
        r = run_cli(TOOL, "--home", str(self.home), "--standing-order", ORDER, *args, env=env)
        self.assertEqual(r.returncode, expect, r.stderr + r.stdout)
        return r

    def run_json(self, *args, expect=0, env=None):
        out = json.loads(self.tool(*args, "--json", expect=expect, env=env).stdout)
        self.assertEqual((out["home"], out["standing_order"], out["mode"], out["real_money"]),
                         (str(self.home.resolve()), ORDER, "paper", False))
        return out

    def ideas(self, rows, name="ideas.json"):
        path = self.home / name
        path.write_text(json.dumps(rows), encoding="utf-8")
        return str(path)

    def log(self):
        path = self.home / "desk_log.jsonl"
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def plan(self, sym):
        with tc.market_scope(sym):
            return tc.load_plan(sym)


class CryptoAutoTradeTests(AutoTradeTestCase):
    def test_crypto_long_default_plan_is_ticketed_then_confirmed_11_seconds_later(self):
        bars = self.pair("BTC-USDT", 100_000.0, seed=7)
        entry = round(bars[-1].close + 10, 2)               # at/above the last price: fills on the yes
        out = self.run_json("--plan", "default", "BTC-USDT", "--side", "long", "--entry", str(entry),
                            "--setup", "orb", "--note", "scanner breakout")
        self.assertEqual(out["plan_mode"], "default")
        [r] = out["results"]
        self.assertEqual((r["symbol"], r["side"], r["setup"], r["status"], r["reason"], r["note"]),
                         ("BTC-USDT", "long", "orb", "filled", None, "scanner breakout"))
        self.assertEqual((r["ticket"], r["trade"], r["fill_price"]), ("TKT-20261031-001", "TRADE-20261031-001",
                                                                      bars[-1].close))
        # The desk's default plan: 1.5 x ATR(14) of today's 5-minute bars below the entry, a 2R target.
        with tc.market_scope("BTC-USDT"):
            atr = tc.atr([b for b in bars if b.date == DAY], 14)
        self.assertEqual(r["entry"], entry)
        self.assertAlmostEqual(r["stop"], entry - 1.5 * atr, delta=0.0051)
        self.assertAlmostEqual(r["target"], entry + 2 * (entry - r["stop"]), delta=0.0051)
        plan = self.plan("BTC-USDT")
        self.assertEqual((plan["entry"], plan["stop"], plan["target"], plan["setup"]),
                         (r["entry"], r["stop"], r["target"], "orb"))
        self.assertEqual((plan["exit_plan"]["method"], plan["exit_plan"]["params"], plan["exit_plan"]["time_stop"]),
                         ("atr", {"atr_mult": 1.5}, "23:55"))
        self.assertEqual(r["qty"], plan["sizing"]["shares"])           # risk.py sized it; the ticket used it
        led = tc.load_ledger()
        [k], [t] = led["tickets"], led["trades"]
        self.assertEqual((k["status"], k["setup"], k["qty_source"], k["levels_source"], k["exchange"]),
                         ("confirmed", "orb", "plan sizing", "plan", "binance"))
        self.assertEqual((k["created_at"], k["confirmed_at"]),           # the 10-second rule, on the replay clock
                         (f"{DAY}T04:40:00+00:00", f"{DAY}T04:40:11+00:00"))
        self.assertEqual((t["id"], t["status"], t["opened_at"], t["mode"]),
                         ("TRADE-20261031-001", "open", f"{DAY}T04:40:11+00:00", "paper"))
        [line] = self.log()                                               # desk_snapshot.py --event's format
        self.assertEqual(sorted(line), ["at", "detail", "event"])
        self.assertEqual((line["at"], line["event"]), (f"{DAY}T04:40:11Z", "Auto-traded BTC-USDT"))
        for words in (f"LONG {tc.fmt_qty(r['qty'])}", f"entry {tc.fmt_price(entry)} / stop {tc.fmt_price(r['stop'])} / "
                      f"target {tc.fmt_price(r['target'])}", f"FILLED @ {tc.fmt_price(bars[-1].close)}",
                      "ticket TKT-20261031-001", f"standing order: {ORDER}"):
            self.assertIn(words, line["detail"])
        self.assertEqual(auto.desk_snapshot.read_log()[-1]["event"], "Auto-traded BTC-USDT")

        # The trade is open: the same pair again (Binance spelling) is skipped, nothing new is placed.
        again = self.run_json("--plan", "default", "BTCUSDT", "--side", "long", "--entry", str(entry))
        self.assertEqual([(x["symbol"], x["status"], x["reason"]) for x in again["results"]],
                         [("BTC-USDT", "skipped", "already in a trade or order")])
        self.assertEqual(again["notes"], [])                            # its sync of the open trade worked
        led = tc.load_ledger()
        self.assertEqual((len(led["tickets"]), len(led["trades"]), len(self.log())), (1, 1, 1))

    def test_batch_skips_shorts_repeats_and_a_budget_refusal_at_the_yes(self):
        tc.save_settings({"max_trades_per_day": 2})
        btc = self.pair("BTC-USDT", 100_000.0, seed=7)
        eth = self.pair("ETH-USDT", 3_500.0, seed=8)
        sol = self.pair("SOL-USDT", 150.0, seed=9)
        owners = {"id": "TKT-20261031-900", "symbol": "XRP-USDT", "side": "long", "qty": 10.0, "type": "limit",
                  "entry": 2.5, "stop": 2.4, "target": 2.7, "status": "pending", "mode": "paper", "market": "crypto",
                  "created_at": f"{DAY}T04:35:00+00:00", "expires_at": f"{DAY}T04:50:00+00:00", "trade_id": None}
        tc.save_ledger({"tickets": [owners], "trades": []})          # someone's own ticket, waiting for a yes
        out = self.run_json("--plan", "default", "--ideas", self.ideas([
            {"symbol": "BTC-USDT", "side": "long", "entry": round(btc[-1].close + 10, 2), "setup": "orb"},
            {"symbol": "BTC-USDT", "side": "short", "entry": btc[-1].close, "setup": "breakdown"},
            {"symbol": "btcusdt", "side": "long", "entry": btc[-1].close, "setup": "orb"},
            {"symbol": "ETH-USDT", "side": "long", "entry": round(eth[-1].close - 20, 2), "setup": "pullback"},
            {"symbol": "SOL-USDT", "side": "long", "entry": round(sol[-1].close + 0.5, 2), "note": "third"}]))
        got = [(r["symbol"], r["side"], r["status"], r["reason"]) for r in out["results"]]
        self.assertEqual(got, [("BTC-USDT", "long", "filled", None),
                               ("BTC-USDT", "short", "skipped", "crypto spot is long only"),
                               ("BTC-USDT", "long", "skipped", "already in a trade or order"),   # BTC's ticket
                               ("ETH-USDT", "long", "working", None),          # a limit under the market rests
                               ("SOL-USDT", "long", "skipped", "budget")])      # 2 trades by its yes
        sol_res = out["results"][4]
        self.assertIn("already 2 trades today (max 2)", sol_res["detail"])
        self.assertIn(f"{sol_res['ticket']} cancelled", sol_res["detail"])
        tickets = {k["symbol"]: k for k in tc.load_ledger()["tickets"]}
        self.assertEqual({s: k["status"] for s, k in tickets.items()},
                         {"XRP-USDT": "pending", "BTC-USDT": "confirmed", "ETH-USDT": "working",
                          "SOL-USDT": "cancelled"})                    # only its own tickets get a yes
        self.assertEqual({k["confirmed_at"] for s, k in tickets.items() if s in ("BTC-USDT", "ETH-USDT")},
                         {f"{DAY}T04:40:11+00:00"})                    # one replay step for the whole batch
        self.assertEqual(out["results"][3]["ticket"], tickets["ETH-USDT"]["id"])
        self.assertIsNone(out["results"][3]["trade"])
        self.assertEqual([e["event"] for e in self.log()], ["Auto-traded BTC-USDT", "Auto-traded ETH-USDT"])
        self.assertIn("WORKING (limit", self.log()[1]["detail"])

        # A working order counts as being in the trade too.
        again = self.run_json("--plan", "default", "ETH-USDT", "--side", "long", "--entry", str(eth[-1].close))
        self.assertEqual([(r["status"], r["reason"]) for r in again["results"]],
                         [("skipped", "already in a trade or order")])
        self.assertEqual(again["notes"], [])

    def test_budget_blocked_ticket_is_skipped_as_budget(self):
        tc.save_settings({"max_trades_per_day": 0})
        bars = self.pair("BTC-USDT", 100_000.0, seed=7)
        out = self.run_json("--plan", "default", "BTC-USDT", "--side", "long", "--entry", str(bars[-1].close))
        [r] = out["results"]
        self.assertEqual((r["status"], r["reason"], r["ticket"]), ("skipped", "budget", None))
        self.assertIn("already 0 trades today (max 0)", r["detail"])
        self.assertGreater(r["qty"], 0)                                 # the order the budget refused
        self.assertEqual((tc.load_ledger()["tickets"], self.log()), ([], []))

    def test_saved_plan_levels_are_traded_as_saved(self):
        btc = self.pair("BTC-USDT", 100_000.0, seed=7)
        eth = self.pair("ETH-USDT", 3_500.0, seed=8)
        e1 = round(btc[-1].close + 20, 2)
        e2 = round(eth[-1].close + 5, 2)
        with tc.market_scope("BTC-USDT"):
            tc.save_plan({"symbol": "BTC-USDT", "side": "long", "entry": e1, "stop": round(e1 - 600, 2),
                          "target": round(e1 + 900, 2), "setup": "vwap_reclaim"})
            tc.save_plan({"symbol": "ETH-USDT", "side": "long", "entry": e2, "stop": round(e2 - 30, 2),
                          "target": round(e2 + 60, 2), "setup": "pullback",
                          "sizing": {"shares": 0.5, "side": "long", "entry": e2, "stop": round(e2 - 30, 2),
                                     "exchange": "binance", "as_of": "sized by the desk"}})
        out = self.run_json("--plan", "saved", "--ideas", self.ideas([
            {"symbol": "BTC-USDT", "side": "long", "entry": 1.0},     # the idea's entry: the plan decides
            {"symbol": "ETH-USDT", "side": "long"},
            {"symbol": "SOL-USDT", "side": "long"}]))                  # nothing saved for it
        btc_r, eth_r, sol_r = out["results"]
        self.assertEqual((btc_r["status"], btc_r["entry"], btc_r["stop"], btc_r["target"], btc_r["setup"]),
                         ("filled", e1, round(e1 - 600, 2), round(e1 + 900, 2), "vwap_reclaim"))
        self.assertEqual((eth_r["status"], eth_r["qty"], eth_r["entry"], eth_r["setup"]),
                         ("filled", 0.5, e2, "pullback"))
        self.assertEqual(sol_r["status"], "error")
        self.assertIn("no saved SOL-USDT plan", sol_r["reason"])
        p1, p2 = self.plan("BTC-USDT"), self.plan("ETH-USDT")
        self.assertEqual((p1["entry"], btc_r["qty"]), (e1, p1["sizing"]["shares"]))   # risk.py sized the unsized plan
        self.assertNotIn("exit_plan", p1)                                # ... and no new plan was built
        self.assertEqual(p2["sizing"]["as_of"], "sized by the desk")    # a sized plan is not re-sized
        self.assertEqual([t["entry_price"] for t in tc.load_ledger()["trades"]], [btc[-1].close, eth[-1].close])


class StockAutoTradeTests(AutoTradeTestCase):
    NOW = "2026-10-29T21:30"             # Thursday night, ET: after the close

    def setUp(self):
        super().setUp()
        bars = random_walk_history("2026-10-29", 5, start_price=360.0, seed=3)
        write_fixture(self.fx, yahoo_chart_url("TSLA", "5m", "5d"), yahoo_payload(bars, "TSLA"))

    def test_us_stock_at_night_is_a_preview_and_skipped(self):
        r = self.tool("--plan", "default", "TSLA", "--side", "long", "--entry", "365.10", "--setup", "breakout")
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2, r.stdout)                       # a header, then one line per idea
        self.assertIn(f"standing order: {ORDER}", lines[0])
        self.assertIn("REAL MONEY: OFF", lines[0])
        self.assertTrue(lines[1].startswith("SKIPPED TSLA LONG "), lines[1])
        self.assertIn("it is 21:30 ET, at or past the 15:55 ET time stop", lines[1])
        self.assertEqual(tc.load_ledger()["tickets"], [])               # a preview saves no ticket
        plan = self.plan("TSLA")                                        # the default plan is built all the same
        self.assertEqual((plan["setup"], plan["exit_plan"]["time_stop"]), ("breakout", "15:55"))
        self.assertEqual(self.log(), [])

    def test_a_size_of_zero_is_skipped_with_the_risk_skills_reason(self):
        tc.save_settings({"risk_per_trade_pct": 0.001})                # $0.25 of risk: not one share
        out = self.run_json("--plan", "default", "TSLA", "--side", "short", "--entry", "365.10")
        [r] = out["results"]                                            # a stock short is fine; its size is not
        self.assertEqual((r["side"], r["status"], r["qty"], r["ticket"]), ("short", "skipped", 0, None))
        self.assertTrue(r["reason"].startswith("0 shares: "), r["reason"])
        self.assertNotIn("sizing", self.plan("TSLA"))
        self.assertEqual(tc.load_ledger()["tickets"], [])


class InputAndDataTests(AutoTradeTestCase):
    def raw(self, *args, env=None):
        return run_cli(TOOL, *args, env=env)

    def test_bad_input_or_state_is_exit_1_before_anything_trades(self):
        home = str(self.home)
        with tempfile.TemporaryDirectory() as empty:
            cases = [
                (["--home", home, "--plan", "default", "BTC-USDT", "--side", "long", "--entry", "1"],
                 "--standing-order"),
                (["--home", home, "--standing-order", "  ", "--plan", "default", "BTC-USDT", "--side", "long",
                  "--entry", "1"], "standing-order is empty"),
                (["--home", str(Path(empty) / "nowhere"), "--standing-order", ORDER, "--plan", "default", "BTC-USDT",
                  "--side", "long", "--entry", "1"], "not a directory"),
                (["--home", empty, "--standing-order", ORDER, "--plan", "default", "BTC-USDT", "--side", "long",
                  "--entry", "1"], "settings.json"),
                (["--home", home, "--standing-order", ORDER, "--plan", "default", "BTC-USDT", "--side", "long"],
                 "needs the idea's entry"),
                (["--home", home, "--standing-order", ORDER, "--plan", "saved", "BTC-USDT"], "--side"),
                (["--home", home, "--standing-order", ORDER, "--plan", "saved", "--ideas",
                  self.ideas([{"symbol": "--yes", "side": "long"}], "flag.json")], "not a ticker"),
                (["--home", home, "--standing-order", ORDER, "--plan", "default", "--ideas",
                  self.ideas([{"symbol": "BTC-USDT", "side": "long", "entry": 1},
                              {"symbol": "TSLA", "side": "long", "entry": 1}], "mixed.json")],
                 "give it a Z or an offset"),                           # a naive replay clock across markets
            ]
            for args, words in cases:
                with self.subTest(args=args):
                    r = self.raw(*args)
                    self.assertEqual(r.returncode, 1, r.stderr + r.stdout)
                    self.assertIn(words, r.stderr)
                    self.assertNotIn("Traceback", r.stderr)
            self.assertEqual(os.listdir(empty), [])                     # --home is never created or written
        self.assertFalse((self.home / "ledger.json").exists())
        self.assertFalse((self.home / "plans").exists())

    def test_data_source_down_is_exit_2_only_when_nothing_could_be_processed(self):
        with tempfile.TemporaryDirectory() as empty:
            env = {"DAYTRADER_FIXTURES": empty}
            r = self.tool("--plan", "default", "BTC-USDT", "--side", "long", "--entry", "100000", "--json",
                          expect=2, env=env)
            [res] = json.loads(r.stdout)["results"]
            self.assertEqual(res["status"], "error")
            self.assertTrue(res["reason"].startswith("data error: "), res["reason"])
            self.assertTrue(r.stderr.startswith("data error: "), r.stderr)
            out = self.run_json("--plan", "default", "--ideas", self.ideas([
                {"symbol": "BTC-USDT", "side": "long", "entry": 100000},
                {"symbol": "ETH-USDT", "side": "short", "entry": 3500}]), env=env)
        self.assertEqual([r["status"] for r in out["results"]], ["error", "skipped"])
        self.assertEqual(tc.load_ledger()["tickets"], [])


class BrokerRefusalTests(TempHomeTestCase):
    """What a refused ticket or yes leaves behind: the broker's answers faked, the ledger real."""

    NOW = f"{DAY}T04:40"

    def setUp(self):
        super().setUp()
        self.calls = []

    def fake(self, answers):
        def call(script, *args):
            self.calls.append((script.name, args[0]))
            return answers[args[0]]
        return mock.patch.object(auto, "call", side_effect=call)

    def pending(self, **kw):
        k = {"id": "TKT-20261031-001", "symbol": "BTC-USDT", "side": "long", "qty": 0.01, "entry": 100_000.0,
             "stop": 99_000.0, "target": 102_000.0, "status": "pending", "created_at": f"{DAY}T04:40:00+00:00",
             "expires_at": f"{DAY}T04:55:00+00:00", "trade_id": None}
        k.update(kw)
        tc.save_ledger({"tickets": [k], "trades": []})
        res = auto.new_result({"symbol": "BTC-USDT", "side": "long", "entry": 100_000.0, "setup": None, "note": None})
        auto.take_levels(res, k)
        res["ticket"] = k["id"]
        return k, res

    def test_a_refused_yes_is_an_error_and_its_ticket_is_cancelled(self):
        k, res = self.pending()
        why = ("TKT-20261031-001 not filled at 98,500.00: for a long, the stop must be below the entry - the price "
               "moved through your levels; cancel it and re-plan")
        with self.fake({"confirm": (1, {}, f"error: {why}"), "cancel": (0, {"ok": True}, "")}):
            auto.confirm(res, k, ORDER)
        self.assertEqual((res["status"], res["reason"], res["detail"]), ("error", why, "TKT-20261031-001 cancelled"))
        self.assertEqual(self.calls, [("broker.py", "confirm"), ("broker.py", "cancel")])
        self.assertFalse((self.home / "desk_log.jsonl").exists())

    def test_a_yes_that_died_after_writing_the_ledger_still_counts(self):
        k, res = self.pending(status="confirmed", trade_id="TRADE-20261031-001", fill_price=99_990.0)
        with self.fake({"confirm": (2, {}, "data error: broker.py confirm TKT-20261031-001 gave no answer in 600 s")}):
            auto.confirm(res, k, ORDER)
        self.assertEqual((res["status"], res["trade"], res["fill_price"], res["reason"]),
                         ("filled", "TRADE-20261031-001", 99_990.0, None))
        self.assertEqual(self.calls, [("broker.py", "confirm")])          # nothing to cancel
        self.assertIn("FILLED @ 99,990.00 (TRADE-20261031-001)", auto.desk_snapshot.read_log()[-1]["detail"])

    def test_a_refused_ticket_is_an_error_with_the_brokers_words(self):
        with tc.market_scope("BTC-USDT"):
            tc.save_plan({"symbol": "BTC-USDT", "side": "long", "entry": 100_000.0, "stop": 99_000.0,
                          "sizing": {"shares": 0.01, "side": "long", "entry": 100_000.0, "stop": 99_000.0}})
        idea = {"symbol": "BTC-USDT", "side": "long", "entry": None, "setup": None, "note": None}
        for code, err, reason in ((1, "error: you have a short BTC-USDT trade (TRADE-1); close it first",
                                   "you have a short BTC-USDT trade (TRADE-1); close it first"),
                                  (2, "data error: BTC-USDT: HTTP 503", "data error: BTC-USDT: HTTP 503")):
            res = auto.new_result(idea)
            with self.subTest(code=code), self.fake({"ticket": (code, {}, err)}):
                self.assertIsNone(auto.prepare(idea, res, "saved"))     # sized plan: straight to the ticket
                self.assertEqual((res["status"], res["reason"], res["ticket"]), ("error", reason, None))
        self.assertEqual(self.calls, [("broker.py", "ticket")] * 2)

    def test_a_ticket_command_that_never_answers_is_a_data_error_not_a_crash(self):
        """broker.py ticket killed at CALL_TIMEOUT: that idea is a data error, the run still reports."""
        tc.save_settings({"daily_max_loss": 1000})
        with tc.market_scope("BTC-USDT"):
            tc.save_plan({"symbol": "BTC-USDT", "side": "long", "entry": 100_000.0, "stop": 99_000.0,
                          "sizing": {"shares": 0.01, "side": "long", "entry": 100_000.0, "stop": 99_000.0}})
        argv = ["paper_autotrade.py", "--home", str(self.home), "--standing-order", ORDER, "--plan", "saved",
                "BTC-USDT", "--side", "long", "--json"]
        hang = subprocess.TimeoutExpired(["broker.py", "ticket"], auto.CALL_TIMEOUT)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(auto.subprocess, "run", side_effect=hang) as run, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = auto.main()                                          # a sized plan: the ticket is the one call
        self.assertEqual((code, run.call_count), (auto.EXIT_DATA, 1))
        [r] = json.loads(stdout.getvalue())["results"]
        self.assertEqual((r["symbol"], r["status"], r["ticket"]), ("BTC-USDT", "error", None))
        self.assertEqual(r["reason"], f"data error: broker.py ticket --symbol gave no answer in {auto.CALL_TIMEOUT} s")
        self.assertTrue(stderr.getvalue().startswith("data error: "), stderr.getvalue())


class ClockTests(unittest.TestCase):
    """How long the batch waits before its yes: never on the replay clock, once on the real one."""

    @staticmethod
    def ticket(ts, sym="BTC-USDT"):
        return {"symbol": sym, "created_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")}

    def test_min_confirm_seconds_is_the_brokers(self):
        self.assertEqual(auto.MIN_CONFIRM_SECONDS, load_skill_module("broker", "broker").MIN_CONFIRM_SECONDS)
        self.assertEqual(auto.REPLAY_STEP, auto.MIN_CONFIRM_SECONDS + 1)

    def test_real_clock_sleeps_once_until_the_newest_ticket_is_old_enough(self):
        t0 = 1_793_421_600.0
        with mock.patch.dict(os.environ), mock.patch.object(auto.time, "time", return_value=t0), \
                mock.patch.object(auto.time, "sleep") as sleep:
            os.environ.pop("DAYTRADER_NOW", None)
            with auto.confirm_clock([self.ticket(t0 - 30), self.ticket(t0 - 4), self.ticket(t0 - 2, "TSLA")]):
                self.assertNotIn("DAYTRADER_NOW", os.environ)
        sleep.assert_called_once_with(t0 - 2 + 10 + auto.WAIT_MARGIN - t0)   # 9 s: the newest ticket decides
        self.assertEqual(auto.confirm_wait([t0 - 60, t0 - 20], t0), 0.0)     # old enough already: no wait

    def test_replay_clock_moves_11_seconds_and_never_sleeps(self):
        with mock.patch.dict(os.environ, {"DAYTRADER_NOW": f"{DAY}T04:40"}), \
                mock.patch.object(auto.time, "sleep") as sleep:
            with auto.confirm_clock([self.ticket(utc_ts(DAY, "04:40"))]):
                self.assertEqual(os.environ["DAYTRADER_NOW"], f"{DAY}T04:40:11")
                with tc.market_scope("BTC-USDT"):
                    self.assertEqual(tc.now_et().timestamp(), utc_ts(DAY, "04:40") + 11)
            self.assertEqual(os.environ["DAYTRADER_NOW"], f"{DAY}T04:40")
        sleep.assert_not_called()
        self.assertEqual(auto.replay_after(f"{DAY}T04:40:00Z", 11), f"{DAY}T04:40:11+00:00")
        self.assertEqual(auto.replay_after("2026-10-30T10:15:55-04:00", 11), "2026-10-30T10:16:06-04:00")


class PaperOnlyTests(unittest.TestCase):
    def test_no_live_option_and_no_network_code_of_its_own(self):
        options = {o for a in auto.build_parser()._actions for o in a.option_strings}
        self.assertFalse([o for o in options if "live" in o or "real" in o or "mode" in o], options)
        src = TOOL.read_text(encoding="utf-8")
        for needle in ("urllib", "http.client", "import socket", "requests", "api_key"):
            self.assertNotIn(needle, src)
        self.assertEqual(src.count('"--yes"'), 1)                       # one confirm call, on its own tickets


if __name__ == "__main__":
    unittest.main()
