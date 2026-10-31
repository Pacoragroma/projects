import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, TempHomeTestCase, crypto_history, fake_bars, tc, utc_ts  # noqa: E402

_spec = importlib.util.spec_from_file_location("desk_snapshot", ROOT / "tools" / "desk_snapshot.py")
snap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snap)


def closed_trade(tid, symbol, pnl, opened_at, closed_at, r_multiple, qty, entry, stop, fees=None):
    """A closed long paper trade as the broker records it (crypto: gross_pnl and fees; pnl is net)."""
    t = {"id": tid, "symbol": symbol, "side": "long", "qty": qty, "entry_price": entry, "stop": stop, "target": None,
         "status": "closed", "exit_reason": "target" if pnl > 0 else ("stop" if pnl < 0 else "eod"), "pnl": pnl,
         "r_multiple": r_multiple, "opened_at": opened_at, "closed_at": closed_at}
    if fees is not None:
        t.update(gross_pnl=round(pnl + fees, 2), fees=fees)
    return t


def open_trade(tid, symbol, qty, entry, stop, opened_at):
    return {"id": tid, "symbol": symbol, "side": "long", "qty": qty, "entry_price": entry, "stop": stop,
            "target": None, "status": "open", "opened_at": opened_at}


# Five closed trades over three days, listed out of order: a TSLA win, a BTC loss, an ETH breakeven
# (its gross gain all went to fees), an AAPL loss and today's BTC win; plus one open BTC position.
# B opened first, though A's ET stamp sorts first as text; C closed before D (14:00Z vs 14:30Z),
# though D's stamp sorts first as text.
TRD_A = closed_trade("TRD-A", "TSLA", 46.88, "2026-10-29T10:15:00-04:00", "2026-10-29T11:00:00-04:00", 1.0, 8, 364.18, 358.32)
TRD_B = closed_trade("TRD-B", "BTC-USDT", -5.54, "2026-10-29T13:00:00+00:00", "2026-10-30T02:00:00+00:00", -1.39,
                     0.01, 77_000, 76_600, fees=1.54)
TRD_C = closed_trade("TRD-C", "ETH-USDT", 0.0, "2026-10-30T13:00:00+00:00", "2026-10-30T14:00:00+00:00", 0.0,
                     0.4, 3_000, 2_975, fees=1.2)
TRD_D = closed_trade("TRD-D", "AAPL", -12.5, "2026-10-30T09:45:00-04:00", "2026-10-30T10:30:00-04:00", -1.0, 25, 270.0, 269.5)
TRD_E = closed_trade("TRD-E", "BTC-USDT", 20.0, "2026-10-31T08:00:00+00:00", "2026-10-31T09:10:00+00:00", 2.5,
                     0.05, 77_100, 76_940, fees=1.56)
TRD_F = dict(open_trade("TRD-F", "BTC-USDT", 0.01, 77_200, 77_000, "2026-10-31T09:20:00+00:00"),
             entry_fee=0.77, fees=0.77)    # an open crypto trade has paid its entry fee: not in all_fees yet
DESK_TRADES = [TRD_E, TRD_D, TRD_A, TRD_F, TRD_C, TRD_B]


def by_kind(rows):
    kinds = {}
    for r in rows:
        kinds.setdefault(r["kind"], []).append(r)
    return kinds


class DeskSnapshotTests(TempHomeTestCase):
    NOW = "2026-10-31T09:40"

    def run_snapshot(self, *args):
        out, printed = self.home / "desk.json", io.StringIO()
        with mock.patch.object(sys, "argv", ["desk_snapshot.py", "--out", str(out), *args]), \
                mock.patch.object(tc, "quote", return_value={"price": 77_500.0}), mock.patch("sys.stdout", printed), \
                self.assertRaises(SystemExit) as cm:
            tc.run(snap.main)
        self.assertEqual(cm.exception.code, 0)
        self.printed = json.loads(printed.getvalue())       # the one-line summary the CLI prints
        return json.loads(out.read_text())

    def btc_snapshot(self, *args):
        with fake_bars({("BTC-USDT", "5m"): crypto_history("2026-10-31", 1, start_price=77_000, until="09:45")}):
            return by_kind(self.run_snapshot("--symbol", "BTC-USDT", *args))

    def failed_snapshot(self, *args):
        """(exit code, stderr) of a snapshot run that should fail."""
        err = io.StringIO()
        with fake_bars({("BTC-USDT", "5m"): crypto_history("2026-10-31", 1, until="09:45")}), \
                mock.patch.object(sys, "argv", ["desk_snapshot.py", "--out", str(self.home / "desk.json"),
                                                "--symbol", "BTC-USDT", *args]), \
                mock.patch.object(tc, "quote", return_value={"price": 77_500.0}), mock.patch("sys.stderr", err), \
                self.assertRaises(SystemExit) as cm:
            tc.run(snap.main)
        return cm.exception.code, err.getvalue()

    def shadow_dir(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name)

    def test_rows_from_the_desk_scans_and_candidates(self):
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "binance"
        tc.save_settings({"max_trades_per_day": 20, "daily_max_loss": 1000})
        with tc.market_scope("BTC-USDT"):
            tc.save_plan({"symbol": "BTCUSDT", "side": "long", "entry": 77_400, "stop": 77_000, "target": 78_200,
                          "time_machine": {"display_pct": {"target_first": 31.6, "stop_first": 31.6, "open_at_close": 36.8},
                                           "verdict": "CLOSE TO A COIN FLIP", "expected_r": 0.04, "paths": 10000}})
        tc.save_ledger({"tickets": [{"id": "TKT-1", "symbol": "BTC-USDT", "side": "long", "qty": 0.01, "entry": 77_400,
                                     "stop": 77_000, "target": 78_200, "status": "pending",
                                     "created_at": "2026-10-31T09:30:00+00:00", "expires_at": "2026-10-31T09:45:00+00:00"}],
                        "trades": [{"id": "TRD-1", "symbol": "TSLA", "side": "long", "qty": 8, "entry_price": 364.18,
                                    "stop": 358.32, "target": 370.04, "status": "closed", "exit_price": 370.04,
                                    "exit_reason": "target", "pnl": 46.88, "r_multiple": 1.0,
                                    "opened_at": "2026-10-30T10:15:00-04:00", "closed_at": "2026-10-30T11:00:00-04:00"}]})
        scan = self.home / "crypto.json"
        scan.write_text(json.dumps({"market": "crypto", "as_of": "2026-10-31T08:50:24+00:00", "scanned": 513,
                                    "universe_size": 513, "matched": 1, "results": [{
                                        "symbol": "ZRO-USDT", "score": 38, "bias": "long", "last": 1.72, "change_pct": 0.35,
                                        "rvol": 3.02, "date": "2026-10-31", "key_level": {"name": "high", "price": 1.73},
                                        "setups": [{"setup": "unusual_volume", "detail": "volume 3.0x"}]}]}))
        watch = self.home / "watchlist.json"
        watch.write_text(json.dumps({"mode": "daily", "as_of": "2026-10-30 close", "created_at": "2026-10-31T09:00:32Z",
                                     "universe": 5942, "scanned": 1590,
                                     "hits": [{"symbol": "INTC", "setups": ["oversold"], "price": 65.76, "rsi": 25.2, "volume": 450884426},
                                              {"symbol": "LLY", "setups": ["20-day closing-high breakout"], "price": 1417.54, "rsi": 78.0, "volume": 3000000}]}))
        cands = self.home / "cands.json"
        cands.write_text(json.dumps([{"symbol": "ZRO-USDT", "decision": "skip", "reason": "stop first in 68% of replays"}]))
        bars = crypto_history("2026-10-31", 1, start_price=77_000, until="09:45")
        with fake_bars({("BTC-USDT", "5m"): bars}):
            rows = self.run_snapshot("--symbol", "BTC-USDT", "--scan", str(scan), "--original-watchlist", str(watch),
                                     "--candidates", str(cands), "--hours", "1", "--event", "Scanned", "--detail", "513 pairs")
        kinds = {}
        for r in rows:
            kinds.setdefault(r["kind"], []).append(r)
        acct = kinds["account"][0]
        self.assertEqual((acct["status"], acct["max_trades"], acct["daily_max_loss"], acct["exchange"], acct["fee_pct"]),
                         ("OPEN FOR TRADING", 20, 1000.0, "Binance", 0.1))
        plan = kinds["plan"][0]                      # BTCUSDT and BTC-USDT are one plan
        self.assertEqual((plan["tm_target_first"], plan["tm_verdict"], plan["rr"]), (31.6, "CLOSE TO A COIN FLIP", 2.0))
        self.assertEqual([t["id"] for t in kinds["ticket"]], ["TKT-1"])
        self.assertEqual(kinds["trade"][0]["symbol"], "TSLA")          # every symbol shares the desk
        self.assertEqual(len(kinds["candle"]), 12)
        self.assertEqual({m["market"] for m in kinds["scanmeta"]}, {"crypto", "original"})
        self.assertEqual([s["symbol"] for s in kinds["scan"] if s["market"] == "original"], ["INTC", "LLY"])  # most traded first
        self.assertEqual(kinds["candidate"][0]["decision"], "skip")
        self.assertEqual(kinds["log"][-1]["event"], "Scanned")
        self.assertTrue((self.home / "desk_log.jsonl").exists())


    def test_missing_input_is_an_error_not_an_empty_table(self):
        with fake_bars({("BTC-USDT", "5m"): crypto_history("2026-10-31", 1, until="09:45")}):
            with mock.patch.object(sys, "argv", ["desk_snapshot.py", "--out", str(self.home / "d.json"), "--symbol",
                                                 "BTC-USDT", "--scan", str(self.home / "nope.json")]), \
                    self.assertRaises(SystemExit) as cm, mock.patch("sys.stderr"):
                tc.run(snap.main)
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse((self.home / "d.json").exists())

    def test_orb_sweep_ranks_hits_by_how_far_over_the_range(self):
        hit = lambda sym, at, pct: {"symbol": sym, "setups": ["orb"], "price": 10.5, "at": at, "or_high": 10.4,
                                    "or_low": 10.0, "pct_over": pct}
        watch = self.home / "orb.json"
        watch.write_text(json.dumps({"mode": "orb", "created_at": "2026-11-02T14:51:00Z", "universe": 5942, "scanned": 3000,
                                     "hits": [hit("AAA", "09:50", 0.4), hit("ZZZ", "09:50", 0.96), hit("NOP", "09:55", None),
                                              hit("MMM", "09:55", 0.96), hit("QQQ", "09:55", 2.1)]}))
        daily = self.home / "daily.json"
        daily.write_text(json.dumps({"mode": "daily", "hits": [{"symbol": "AAA", "price": 10.0, "volume": 1_000},
                                                               {"symbol": "ZZZ", "price": 50.0, "volume": 1_000_000},
                                                               {"symbol": "MMM", "price": 20.0, "volume": 100_000}]}))
        orb = self.btc_snapshot("--original-watchlist", str(watch))["scan"]
        self.assertEqual([r["symbol"] for r in orb], ["QQQ", "MMM", "ZZZ", "AAA", "NOP"])  # furthest over first, then A-Z
        self.assertEqual([r["rank"] for r in orb], [1, 2, 3, 4, 5])
        self.assertEqual((orb[2]["setup"], orb[4]["setup"]), ("orb at 09:50, +0.96% over the range", "orb"))
        self.assertIsNone(orb[0]["dollar_volume"])
        scan = self.btc_snapshot("--original-watchlist", str(daily))["scan"]   # a daily scan: still most traded first
        self.assertEqual([(r["symbol"], r["dollar_volume"]) for r in scan],
                         [("ZZZ", 50_000_000.0), ("MMM", 2_000_000.0), ("AAA", 10_000.0)])

    def test_a_daily_hit_without_volume_keeps_the_daily_ranking(self):
        # The pack's daily scan writes "volume": null when Yahoo gave none (possible with --min-volume 0). It is
        # still a daily scan: most traded first, no volume last; not an ORB sweep with every pct_over missing (A-Z).
        daily = self.home / "daily.json"
        daily.write_text(json.dumps({"mode": "daily", "hits": [{"symbol": "AAA", "price": 10.0, "volume": None},
                                                               {"symbol": "ZZZ", "price": 50.0, "volume": 1_000_000},
                                                               {"symbol": "MMM", "price": 20.0, "volume": 100_000}]}))
        scan = self.btc_snapshot("--original-watchlist", str(daily))["scan"]
        self.assertEqual([(r["symbol"], r["rank"], r["dollar_volume"]) for r in scan],
                         [("ZZZ", 1, 50_000_000.0), ("MMM", 2, 2_000_000.0), ("AAA", 3, None)])
        orb = self.home / "orb.json"                                              # no mode (an older file): no volume = ORB
        orb.write_text(json.dumps({"hits": [{"symbol": "AAA", "setups": ["orb"], "price": 10.5, "at": "09:50", "pct_over": 0.4},
                                            {"symbol": "ZZZ", "setups": ["orb"], "price": 10.5, "at": "09:55", "pct_over": 0.96}]}))
        self.assertEqual([r["symbol"] for r in self.btc_snapshot("--original-watchlist", str(orb))["scan"]], ["ZZZ", "AAA"])

    def test_account_row_adds_all_time_results(self):
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "binance"
        tc.save_ledger({"tickets": [], "trades": DESK_TRADES})
        acct = self.btc_snapshot()["account"][0]
        self.assertEqual({k: acct[k] for k in acct if k.startswith("all_") or k in ("open_positions", "first_trade_at")},
                         {"all_trades": 5, "all_wins": 2, "all_losses": 2,     # the breakeven counts, but as neither
                          "all_pnl": 48.84, "all_fees": 4.3, "all_win_rate": 40.0, "all_avg_r": 0.22,
                          "all_profit_factor": 3.71,                           # 66.88 won / 18.04 lost
                          "open_positions": 1, "first_trade_at": "2026-10-29T13:00:00Z"})
        self.assertEqual((acct["day"], acct["realized_pnl"], acct["trades_today"], acct["wins"], acct["losses"],
                          acct["open_risk"]), ("2026-10-31", 20.0, 2, 1, 0, 2.0))   # today's numbers as before
        self.assertLessEqual({"as_of", "day", "exchange", "symbol", "status", "last_price", "account_size",
                              "risk_per_trade", "fee_pct", "loss_budget_used", "loss_budget_left", "daily_max_loss",
                              "max_trades"}, set(acct))

    def test_equity_rows_follow_closed_at_order(self):
        tc.save_ledger({"tickets": [], "trades": DESK_TRADES})
        kinds = self.btc_snapshot()
        eq = kinds["equity"]
        self.assertEqual([(r["id"], r["account"], r["n"], r["at"], r["symbol"], r["pnl"], r["cum_pnl"]) for r in eq], [
            ("equity-desk-0", "desk", 0, "2026-10-29T13:00:00Z", None, 0.0, 0.0),      # starts at the first trade's open
            ("equity-desk-1", "desk", 1, "2026-10-29T15:00:00Z", "TSLA", 46.88, 46.88),
            ("equity-desk-2", "desk", 2, "2026-10-30T02:00:00Z", "BTC-USDT", -5.54, 41.34),
            ("equity-desk-3", "desk", 3, "2026-10-30T14:00:00Z", "ETH-USDT", 0.0, 41.34),
            ("equity-desk-4", "desk", 4, "2026-10-30T14:30:00Z", "AAPL", -12.5, 28.84),
            ("equity-desk-5", "desk", 5, "2026-10-31T09:10:00Z", "BTC-USDT", 20.0, 48.84)])
        self.assertEqual(eq[-1]["cum_pnl"], kinds["account"][0]["all_pnl"])
        self.assertEqual(eq[0]["at"], kinds["account"][0]["first_trade_at"])

    def test_no_trades_at_all(self):
        kinds = self.btc_snapshot()
        acct = kinds["account"][0]
        self.assertEqual({k: acct[k] for k in acct if k.startswith("all_") or k in ("open_positions", "first_trade_at")},
                         {"all_trades": 0, "all_wins": 0, "all_losses": 0, "all_pnl": 0.0, "all_fees": 0.0,
                          "all_win_rate": None, "all_avg_r": None, "all_profit_factor": None,
                          "open_positions": 0, "first_trade_at": None})
        self.assertEqual(kinds["equity"], [{"kind": "equity", "id": "equity-desk-0", "account": "desk", "n": 0,
                                            "at": acct["as_of"], "symbol": None, "pnl": 0.0, "cum_pnl": 0.0}])
        self.assertEqual(acct["as_of"], "2026-10-31T09:40:00Z")
        self.assertFalse({"position", "trade", "shadow_account", "shadow_position", "shadow_trade"} & set(kinds))

    def test_all_time_profit_factor_and_win_rate_edges(self):
        day = "2026-10-30T1%d:00:00+00:00"
        win = closed_trade("W", "BTC-USDT", 10.0, day % 0, day % 1, 1.0, 0.01, 77_000, 76_000)
        loss = closed_trade("L", "BTC-USDT", -4.0, day % 2, day % 3, None, 0.01, 77_000, 77_000)   # no R: no risk at entry
        held = open_trade("O", "TSLA", 8, 364.18, 358.32, "2026-10-29T15:00:00-04:00")    # opened before any closed one
        only_wins = snap.all_time({"trades": [win]})
        self.assertEqual((only_wins["all_profit_factor"], only_wins["all_win_rate"]), (None, 100.0))   # no losing trade
        only_losses = snap.all_time({"trades": [loss, dict(loss, id="L2", pnl=None, r_multiple=-1.0)]})
        self.assertEqual((only_losses["all_profit_factor"], only_losses["all_win_rate"], only_losses["all_losses"],
                          only_losses["all_avg_r"], only_losses["all_pnl"]), (0.0, 0.0, 1, -1.0, -4.0))  # pnl None = 0
        mixed = snap.all_time({"trades": [win, loss, held]})
        self.assertEqual((mixed["all_trades"], mixed["all_avg_r"], mixed["all_profit_factor"], mixed["open_positions"],
                          mixed["first_trade_at"]), (2, 1.0, 2.5, 1, "2026-10-29T19:00:00Z"))
        self.assertEqual(snap.all_time({"trades": [held]})["all_trades"], 0)

    def test_shadow_desk_rows_beside_the_desk(self):
        os.environ["DAYTRADER_CRYPTO_EXCHANGE"] = "binance"
        with tc.market_scope("BTC-USDT"):
            tc.save_plan({"symbol": "BTC-USDT", "side": "long", "entry": 77_400, "stop": 77_000, "target": 78_200})
        tc.save_ledger({"tickets": [], "trades": [
            closed_trade("TRD-1", "TSLA", 46.88, "2026-10-30T10:15:00-04:00", "2026-10-30T11:00:00-04:00", 1.0, 8, 364.18, 358.32),
            open_trade("TRD-4", "BTC-USDT", 0.02, 77_400, 77_000, "2026-10-31T09:30:00+00:00")]})
        desk_ledger = (self.home / "ledger.json").read_text()
        shadow = self.shadow_dir()
        with mock.patch.dict(os.environ, {"DAYTRADER_HOME": str(shadow)}):   # the shadow desk took every idea
            tc.save_settings({"account_size": 10_000, "risk_per_trade_pct": 0.4, "crypto_fee_pct": 0.075})   # desk: $50, 0.1%
            with tc.market_scope("BTC-USDT"):
                tc.save_plan({"symbol": "BTC-USDT", "side": "long", "entry": 70_000, "stop": 69_000})
            tc.save_ledger({"tickets": [{"id": "TKT-9", "symbol": "BTC-USDT", "side": "long", "qty": 0.01, "entry": 77_400,
                                         "stop": 77_000, "status": "pending", "expires_at": "2026-10-31T09:50:00+00:00"}],
                            "trades": [
                closed_trade("TRD-1", "BTC-USDT", -8.0, "2026-10-31T07:00:00+00:00", "2026-10-31T07:30:00+00:00", -1.0,
                             0.02, 77_400, 77_000, fees=1.5),           # the same id as the desk's TSLA trade
                closed_trade("TRD-2", "SOL-USDT", 12.0, "2026-10-30T20:00:00+00:00", "2026-10-30T21:00:00+00:00", 1.5,
                             4, 200, 198, fees=0.5),
                open_trade("TRD-3", "BTC-USDT", 0.01, 77_000, 76_800, "2026-10-31T09:00:00+00:00")]})
        shadow_files = {p: p.read_bytes() for p in shadow.rglob("*") if p.is_file()}
        kinds = self.btc_snapshot("--shadow-home", str(shadow), "--event", "Refreshed")

        self.assertEqual(kinds["shadow_account"], [{
            "kind": "shadow_account", "id": "shadow_account", "account_size": 10_000.0, "risk_per_trade": 40.0,
            "fee_pct": 0.075, "realized_pnl": -8.0, "open_risk": 2.0, "trades_today": 2, "wins": 0, "losses": 1,
            "all_trades": 2, "all_wins": 1, "all_losses": 1, "all_pnl": 4.0, "all_fees": 2.0, "all_win_rate": 50.0,
            "all_avg_r": 0.25, "all_profit_factor": 1.5, "open_positions": 1, "first_trade_at": "2026-10-30T20:00:00Z"}])
        trades, positions = kinds["shadow_trade"], kinds["shadow_position"]
        self.assertEqual([t["id"] for t in trades], ["shadow-TRD-1", "shadow-TRD-2"])
        self.assertEqual(set(trades[0]), set(kinds["trade"][0]))                 # exactly the desk rows' fields
        self.assertEqual((trades[0]["symbol"], trades[0]["pnl"], trades[0]["gross_pnl"], trades[0]["fees"],
                          trades[0]["closed_at"]), ("BTC-USDT", -8.0, -6.5, 1.5, "2026-10-31T07:30:00Z"))
        self.assertEqual([(p["id"], p["last"], p["unrealized_pnl"]) for p in positions], [("shadow-TRD-3", 77_500.0, 5.0)])
        self.assertEqual(set(positions[0]), set(kinds["position"][0]))
        self.assertEqual([r["id"] for r in kinds["equity"] if r["account"] == "shadow"],
                         ["equity-shadow-0", "equity-shadow-1", "equity-shadow-2"])
        self.assertEqual([(r["at"], r["symbol"], r["cum_pnl"]) for r in kinds["equity"] if r["account"] == "shadow"],
                         [("2026-10-30T20:00:00Z", None, 0.0), ("2026-10-30T21:00:00Z", "SOL-USDT", 12.0),
                          ("2026-10-31T07:30:00Z", "BTC-USDT", 4.0)])
        ids = [r["id"] for rows in kinds.values() for r in rows]
        self.assertEqual(len(ids), len(set(ids)))                                # no id collides with the desk's
        self.assertEqual({k: self.printed["rows"][k] for k in ("shadow_account", "shadow_trade", "shadow_position", "equity")},
                         {"shadow_account": 1, "shadow_trade": 2, "shadow_position": 1, "equity": 5})

        # The rest of the snapshot still reads the desk, and neither home was changed.
        acct = kinds["account"][0]
        self.assertEqual((acct["account_size"], acct["all_trades"], acct["all_pnl"], acct["open_positions"]),
                         (25_000.0, 1, 46.88, 1))
        self.assertEqual(([t["id"] for t in kinds["trade"]], [p["id"] for p in kinds["position"]]), (["TRD-1"], ["TRD-4"]))
        self.assertEqual(kinds["plan"][0]["entry"], 77_400.0)
        self.assertNotIn("ticket", kinds)                                         # the shadow's ticket is not the desk's
        self.assertEqual([r["id"] for r in kinds["equity"] if r["account"] == "desk"], ["equity-desk-0", "equity-desk-1"])
        self.assertEqual(os.environ["DAYTRADER_HOME"], str(self.home))
        self.assertEqual([t["id"] for t in tc.load_ledger()["trades"]], ["TRD-1", "TRD-4"])
        self.assertEqual((self.home / "ledger.json").read_text(), desk_ledger)
        self.assertEqual({p: p.read_bytes() for p in shadow.rglob("*") if p.is_file()}, shadow_files)
        self.assertEqual(kinds["log"][-1]["event"], "Refreshed")                  # logged on the desk only
        self.assertFalse((shadow / "desk_log.jsonl").exists())

    def test_an_empty_shadow_home_is_a_desk_with_no_trades(self):
        shadow = self.shadow_dir()
        kinds = self.btc_snapshot("--shadow-home", str(shadow))
        sh = kinds["shadow_account"][0]
        self.assertEqual((sh["account_size"], sh["risk_per_trade"], sh["all_trades"], sh["all_win_rate"], sh["first_trade_at"]),
                         (25_000.0, 50.0, 0, None, None))
        self.assertEqual([(r["id"], r["at"]) for r in kinds["equity"]],
                         [("equity-desk-0", "2026-10-31T09:40:00Z"), ("equity-shadow-0", "2026-10-31T09:40:00Z")])
        self.assertEqual(list(shadow.iterdir()), [])

    def test_missing_shadow_home_is_an_error(self):
        missing = self.home / "no-such-shadow"
        code, err = self.failed_snapshot("--shadow-home", str(missing), "--event", "Refreshed")
        self.assertEqual(code, 1)
        self.assertIn(f"--shadow-home {missing} does not exist", err)
        self.assertFalse(missing.exists())                                        # not created as an empty desk
        self.assertFalse((self.home / "desk.json").exists())
        self.assertFalse((self.home / "desk_log.jsonl").exists())                 # nothing logged either
        not_a_dir = self.home / "shadow.txt"
        not_a_dir.write_text("x")
        self.assertEqual(self.failed_snapshot("--shadow-home", str(not_a_dir))[0], 1)
        code, err = self.failed_snapshot("--shadow-home", str(self.home))          # the desk against itself
        self.assertEqual(code, 1)
        self.assertIn("the desk's own $DAYTRADER_HOME", err)

    def test_shadow_home_is_put_back_after_an_error(self):
        shadow = self.shadow_dir()
        (shadow / "ledger.json").write_text("{not json")
        code, err = self.failed_snapshot("--shadow-home", str(shadow))
        self.assertEqual(code, 1)
        self.assertIn("ledger.json is not valid JSON", err)
        self.assertEqual(os.environ["DAYTRADER_HOME"], str(self.home))
        self.assertFalse((self.home / "desk.json").exists())
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):               # never the real ~/.day-trader
            del os.environ["DAYTRADER_HOME"]
            with self.assertRaises(tc.StateError):
                snap.load_desk(shadow)
            self.assertNotIn("DAYTRADER_HOME", os.environ)                        # unset again, as it was
        (shadow / "ledger.json").write_text(json.dumps({"trades": [TRD_F]}))
        settings, ledger, summary = snap.load_desk(shadow)
        self.assertEqual((settings["account_size"], [t["id"] for t in ledger["trades"]], summary["open_positions"]),
                         (25_000.0, ["TRD-F"], 1))
        self.assertEqual(os.environ["DAYTRADER_HOME"], str(self.home))

    def test_expired_pending_tickets_are_not_listed(self):
        k = {"symbol": "BTC-USDT", "side": "long", "qty": 0.01, "entry": 77_400, "stop": 77_000, "target": 78_200,
             "created_at": "2026-10-31T09:25:00+00:00"}
        tc.save_ledger({"trades": [], "tickets": [                                # now is 09:40 UTC
            dict(k, id="TKT-OLD", status="pending", expires_at="2026-10-31T09:39:00+00:00"),
            dict(k, id="TKT-LIVE", status="pending", expires_at="2026-10-31T09:50:00Z"),
            dict(k, id="TKT-EDGE", status="pending", expires_at="2026-10-31T05:40:00-04:00"),   # expires right now
            dict(k, id="TKT-ET", status="pending", expires_at="2026-10-31T06:00:00-04:00"),     # 10:00 UTC: still live
            dict(k, id="TKT-NOEXP", status="pending"),                            # the broker reads it as expired too
            dict(k, id="TKT-WORK", status="working", expires_at="2026-10-31T09:30:00+00:00"),   # confirmed, resting
            dict(k, id="TKT-DONE", status="confirmed", expires_at="2026-10-31T09:50:00+00:00"),
            dict(k, id="TKT-GONE", status="cancelled", expires_at="2026-10-31T09:50:00+00:00")]})
        tickets = self.btc_snapshot()["ticket"]
        self.assertEqual([(t["id"], t["status"]) for t in tickets],
                         [("TKT-LIVE", "pending"), ("TKT-ET", "pending"), ("TKT-WORK", "working")])
        self.assertEqual([t["expires_at"] for t in tickets],
                         ["2026-10-31T09:50:00Z", "2026-10-31T10:00:00Z", "2026-10-31T09:30:00Z"])


class DeskBackupRestoreTests(TempHomeTestCase):
    NOW = "2026-10-31T09:40"

    def test_round_trip_and_no_rollback(self):
        import subprocess
        tc.save_settings({"max_trades_per_day": 20, "daily_max_loss": 1000})
        with tc.market_scope("BTC-USDT"):
            tc.save_plan({"symbol": "BTC-USDT", "side": "long", "entry": 100, "stop": 99, "target": 102})
        (self.home / "alerts.json").write_text(json.dumps({"alerts": [{"id": "ALR-1"}]}))
        (self.home / "desk_log.jsonl").write_text(json.dumps({"at": "x", "event": "Desk opened", "detail": ""}) + "\n")
        cands = self.home / "c.json"
        cands.write_text(json.dumps([{"symbol": "ZRO-USDT", "decision": "skip"}]))
        out = self.home / "backup.json"
        tool = lambda name, *a, env=None: subprocess.run([sys.executable, str(ROOT / "tools" / name), *a],
                                                         capture_output=True, text=True, env=env or dict(os.environ))
        r = tool("desk_backup.py", "--out", str(out), "--candidates", str(cands))
        self.assertEqual(r.returncode, 0, r.stderr)
        state = json.loads(out.read_text())
        self.assertEqual(state["ledger"], {"tickets": [], "trades": []})        # no ledger.json yet: default
        self.assertEqual((state["settings"]["max_trades_per_day"], list(state["plans"]), state["candidates"][0]["symbol"]),
                         (20, ["BTC-USDT.json"], "ZRO-USDT"))
        wrapped = self.home / "wrapped.json"                                     # as ArtifactData get saves it
        wrapped.write_text(json.dumps({"id": "desk_state", "data": state, "version": 3}))
        with tempfile.TemporaryDirectory() as fresh:
            env = dict(os.environ, DAYTRADER_HOME=fresh)
            r = tool("desk_restore.py", str(wrapped), "--candidates-out", str(Path(fresh) / "cands.json"), env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads((Path(fresh) / "settings.json").read_text())["daily_max_loss"], 1000.0)
            self.assertEqual(json.loads((Path(fresh) / "plans" / "BTC-USDT.json").read_text())["target"], 102)
            self.assertEqual(json.loads((Path(fresh) / "cands.json").read_text())[0]["symbol"], "ZRO-USDT")
            self.assertIn("Desk opened", (Path(fresh) / "desk_log.jsonl").read_text())
            r = tool("desk_restore.py", str(wrapped), env=env)                    # a live desk is never rolled back
            self.assertEqual(r.returncode, 1)
            self.assertIn("already holds a desk", r.stderr)
            r = tool("desk_backup.py", "--out", str(out), "--previous", str(wrapped), env=env)   # candidates carried over
            self.assertEqual(json.loads(out.read_text())["candidates"][0]["symbol"], "ZRO-USDT")
        r = tool("desk_backup.py", "--out", str(out), "--candidates", str(self.home / "missing.json"))
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
