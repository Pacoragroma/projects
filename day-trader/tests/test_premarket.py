"""Tests for tools/premarket.py - the pre-market candidates and the merged study. Offline."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, TempHomeTestCase, run_cli  # noqa: E402

TOOL = ROOT / "tools" / "premarket.py"


def us_row(sym, score, dv, bias="long", setups=("breakout",)):
    return {"symbol": sym, "score": score, "bias": bias, "setups": [{"setup": s} for s in setups], "last": 10.0,
            "change_pct": 1.5, "rvol": 2.0, "atr_pct": 3.0, "dollar_volume_20d": dv, "high_20d": 11.0, "low_20d": 9.0, "rsi14": 55.0}


class PremarketTests(TempHomeTestCase):

    def setUp(self):
        super().setUp()
        self.us = self.home / "us.json"
        self.us.write_text(json.dumps({"results": [
            us_row("AAA", 90, 500e6), us_row("BBB", 70, 25e6), us_row("CCC", 65, 5e6),      # CCC too thin
            us_row("DDD", 40, 900e6), us_row("EEE", 61, 30e6),                              # DDD too weak
        ]}))
        self.orig = self.home / "watchlist.json"
        self.orig.write_text(json.dumps({"mode": "daily", "hits": [
            {"symbol": "AAA", "setups": ["20-day closing-high breakout"], "price": 10.0, "rsi": 71.0},
            {"symbol": "FFF", "setups": ["bull flag"], "price": 50.0, "rsi": 60.0},
            {"symbol": "GGG", "setups": ["oversold"], "price": 20.0, "rsi": 25.0},
        ]}))
        self.cands = self.home / "cands.json"

    def run_json(self, *args, expect=0):
        r = run_cli(TOOL, *args, "--json")
        self.assertEqual(r.returncode, expect, r.stderr + r.stdout)
        return json.loads(r.stdout) if expect == 0 else r.stderr

    def test_candidates(self):
        out = self.run_json("candidates", "--us", str(self.us), "--original", str(self.orig), "--out", str(self.cands), "--group-size", "2")
        syms = [c["symbol"] for c in out["candidates"]]
        self.assertEqual(syms, ["AAA", "EEE", "BBB", "FFF", "GGG"])          # ours by dollar volume, then the original's order
        by = {c["symbol"]: c for c in out["candidates"]}
        self.assertEqual((by["AAA"]["from"], by["AAA"]["original_tags"], by["AAA"]["setups"]), ("both", ["20-day closing-high breakout"], ["breakout"]))
        self.assertEqual((by["FFF"]["from"], by["FFF"]["rsi14"], by["FFF"]["last"]), ("original", 60.0, 50.0))
        self.assertEqual(out["groups"], [["AAA", "EEE"], ["BBB", "FFF"], ["GGG"]])
        self.assertEqual(out["counts"], {"ours": 3, "ours_kept": 3, "original_hits": 3, "kept": 5})
        self.assertEqual(json.loads(self.cands.read_text())["groups"], out["groups"])
        r = run_cli(TOOL, "candidates", "--us", str(self.us), "--original", str(self.orig), "--out", str(self.cands))
        self.assertIn("PRE-MARKET CANDIDATES · 5 names in 1 group(s)", r.stdout)
        self.assertIn("group 0: AAA EEE BBB FFF GGG", r.stdout)

    def test_candidates_cap_and_errors(self):
        out = self.run_json("candidates", "--us", str(self.us), "--original", str(self.orig), "--out", str(self.cands), "--max", "2")
        self.assertEqual([c["symbol"] for c in out["candidates"]], ["AAA", "EEE"])
        # --max-ours keeps room for the original's most traded hits
        out = self.run_json("candidates", "--us", str(self.us), "--original", str(self.orig), "--out", str(self.cands), "--max", "3", "--max-ours", "1")
        self.assertEqual([c["symbol"] for c in out["candidates"]], ["AAA", "FFF", "GGG"])
        self.assertEqual(out["counts"]["ours_kept"], 1)
        err = self.run_json("candidates", "--us", str(self.home / "nope.json"), "--original", str(self.orig), "--out", str(self.cands), expect=1)
        self.assertIn("our scan", err)
        empty = self.home / "empty.json"
        empty.write_text(json.dumps({"results": []}))
        self.orig.write_text(json.dumps({"hits": []}))
        err = self.run_json("candidates", "--us", str(empty), "--original", str(self.orig), "--out", str(self.cands), expect=1)
        self.assertIn("no candidates", err)

    def test_merge(self):
        self.run_json("candidates", "--us", str(self.us), "--original", str(self.orig), "--out", str(self.cands), "--group-size", "3")
        parts = self.home / "parts"
        parts.mkdir()
        (parts / "charts-0.json").write_text(json.dumps({"group": 0, "symbols": [
            {"symbol": "AAA", "orb_follow_through": "good", "watch_note": "over 10.9"},
            {"symbol": "eee", "orb_follow_through": "poor", "watch_note": "fades"}]}))
        (parts / "charts-1.json").write_text(json.dumps({"group": 1, "symbols": [{"symbol": "FFF", "orb_follow_through": "mixed"}]}))
        (parts / "news-0.json").write_text(json.dumps({"symbols": [
            {"symbol": "AAA", "red_flag": False}, {"symbol": "BBB", "red_flag": True, "red_flag_reason": "guidance cut"}]}))
        (parts / "macro.json").write_text(json.dumps({"futures": "ES +0.4%", "calendar": ["10:00 ISM"]}))
        (parts / "broken.json").write_text("{not json")
        study = self.home / "study.json"
        out = self.run_json("merge", "--date", "2026-11-03", "--candidates", str(self.cands), "--charts", str(parts / "charts-*.json"),
                            "--news", str(parts / "news-*.json"), str(parts / "broken.json"), "--macro", str(parts / "macro.json"), "--out", str(study))
        self.assertEqual(out["missing"], {"chart": ["BBB", "GGG"], "news": ["EEE", "FFF", "GGG"]})
        self.assertEqual((out["good"], out["poor"], out["red_flags"]), (["AAA"], ["EEE"], ["BBB: guidance cut"]))
        self.assertEqual(len(out["problems"]), 1)
        self.assertIn("broken.json", out["problems"][0])
        self.assertEqual(out["macro"]["futures"], "ES +0.4%")
        doc = json.loads(study.read_text())
        self.assertEqual(doc["date"], "2026-11-03")
        self.assertEqual([s["symbol"] for s in doc["symbols"]], ["AAA", "EEE", "BBB", "FFF", "GGG"])
        self.assertEqual(doc["symbols"][0]["chart"]["watch_note"], "over 10.9")
        self.assertEqual(doc["symbols"][0]["scan"]["score"], 90)
        self.assertNotIn("problems", doc)
        r = run_cli(TOOL, "merge", "--date", "2026-11-03", "--candidates", str(self.cands), "--out", str(study))
        self.assertIn("charts missing 5 · news missing 5 · macro MISSING", r.stdout)

    def test_merge_bad_candidates(self):
        bad = self.home / "bad.json"
        bad.write_text(json.dumps([1, 2]))
        err = self.run_json("merge", "--date", "2026-11-03", "--candidates", str(bad), "--out", str(self.home / "s.json"), expect=1)
        self.assertIn("not this tool's candidates JSON", err)


if __name__ == "__main__":
    unittest.main()
