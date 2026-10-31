import importlib.util
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


class DeskSnapshotTests(TempHomeTestCase):
    NOW = "2026-10-31T09:40"

    def run_snapshot(self, *args):
        out = self.home / "desk.json"
        with mock.patch.object(sys, "argv", ["desk_snapshot.py", "--out", str(out), *args]), \
                mock.patch.object(tc, "quote", return_value={"price": 77_500.0}), self.assertRaises(SystemExit) as cm:
            tc.run(snap.main)
        self.assertEqual(cm.exception.code, 0)
        return json.loads(out.read_text())

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

    def test_orb_sweep_keeps_trigger_order(self):
        watch = self.home / "orb.json"
        watch.write_text(json.dumps({"mode": "orb", "created_at": "2026-11-02T14:51:00Z", "universe": 5942, "scanned": 3000,
                                     "hits": [{"symbol": "ZZZ", "setups": ["orb"], "price": 10.5, "at": "09:50", "or_high": 10.4,
                                               "or_low": 10.0, "pct_over": 0.96},
                                              {"symbol": "AAA", "setups": ["orb"], "price": 50.2, "at": "09:55", "or_high": 50.0,
                                               "or_low": 49.0, "pct_over": 0.4}]}))
        with fake_bars({("BTC-USDT", "5m"): crypto_history("2026-10-31", 1, until="09:45")}):
            rows = self.run_snapshot("--symbol", "BTC-USDT", "--original-watchlist", str(watch))
        orb = [r for r in rows if r["kind"] == "scan" and r["market"] == "original"]
        self.assertEqual([r["symbol"] for r in orb], ["ZZZ", "AAA"])       # earliest trigger first, not by volume
        self.assertEqual(orb[0]["setup"], "orb at 09:50, +0.96% over the range")
        self.assertIsNone(orb[0]["dollar_volume"])


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
