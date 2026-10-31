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


if __name__ == "__main__":
    unittest.main()
