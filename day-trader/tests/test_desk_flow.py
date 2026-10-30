"""End-to-end desk flow for the video's prompt: "Plan a trade on TSLA and test it".

Every skill runs as a real subprocess (helpers.run_cli), offline, against $DAYTRADER_FIXTURES,
with one shared $DAYTRADER_HOME so the plan and the paper ledger flow from skill to skill:

  scanner -> news -> entry --save -> exit_plan --save -> time_machine --save -> risk size --save
  -> budget check -> broker ticket -> confirm (no --yes: refused) -> confirm --yes -> budget status
  -> alerts from-plan -> broker sync + alerts check -> budget status -> analyst review

$DAYTRADER_NOW moves forward like a real morning, and before each step the Yahoo fixtures are
re-published with only the 5-minute bars that had finished by then - what the live API would
return at that moment - so no step can see the future. The fixture URLs are the exact ones the
scripts request: 5m/5d (entry, exit, broker sync), 5m/60d (time machine), 1m/1d (tc.quote, used
by the broker), 1d/6mo (scanner), plus the Yahoo RSS feed (news; its other sources are left
without fixtures, so they fail and the run must carry on with a warning).

Today's session: a 5-week base under 360 on the daily chart (20-day high 362.16), an opening
range 09:30-09:45 (high 362.40, low 358.32), then the 10:10 bar closes at 364.18 above the
range -> at 10:16 the scanner reports a 20-day BREAKOUT on ~2x paced volume, and entry an ORB
long at 10:15, stop 358.32 (the OR low), 1R target 370.04, 8 shares for $46.88 of risk with the
default settings. After the 10:22 paper fill the tape either rallies through the target (11:00)
or slides through the stop (10:45).

AfterHoursDeskFlowTest plans and tests the same trade at 19:30 and on Saturday: the plan is flagged
as a finished session's trigger, the Time Machine simulates the next session, and the broker only
previews (exit 0, nothing saved, nothing to confirm).

ReadmeSampleTest runs the README's own desk-flow commands in text mode on the same data and
checks that every line of the README's sample output block is really printed. To regenerate
that block: python3 tests/test_desk_flow.py --print-morning
"""
from __future__ import annotations

import json
import math
import os
import random
import shlex
import shutil
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (TempHomeTestCase, daily_bars, et_ts, random_walk_history,  # noqa: E402
                     random_walk_session, run_cli, skill_script, tc, write_fixture, yahoo_chart_url,
                     yahoo_payload)

README = Path(__file__).resolve().parent.parent / "README.md"
SAMPLE_INTRO = "On the pack's synthetic test data"   # the sentence right before the sample block

SYM = "TSLA"
DAY = "2026-10-30"          # a Friday (EDT)
PREV = "2026-10-29"
OPEN_PX = 360.00

# (start ET, open, high, low, close, volume) - today's regular session through the paper fill.
MORNING = [
    ("09:30", 360.00, 361.80, 359.20, 361.20, 1_400_000),
    ("09:35", 361.20, 362.40, 360.10, 360.60, 1_100_000),
    ("09:40", 360.60, 361.50, 358.32, 359.40, 1_000_000),  # OR 09:30-09:45: high 362.40 / low 358.32
    ("09:45", 359.40, 360.90, 359.00, 360.70, 800_000),
    ("09:50", 360.70, 361.90, 360.20, 361.50, 700_000),
    ("09:55", 361.50, 362.30, 361.00, 362.00, 700_000),
    ("10:00", 362.00, 362.38, 361.40, 361.90, 600_000),
    ("10:05", 361.90, 362.35, 361.60, 362.10, 600_000),
    ("10:10", 362.10, 364.50, 362.00, 364.18, 1_500_000),  # close > OR high -> LONG @ 364.18 (10:15)
    ("10:15", 364.18, 364.60, 363.70, 364.00, 900_000),
    ("10:20", 364.00, 364.40, 363.80, 364.18, 700_000),    # the 10:22 paper fill happens in this bar
]
RALLY = [  # opens below the target every bar; the 10:55 bar trades through 370.04
    ("10:25", 364.18, 365.50, 364.00, 365.30, 800_000),
    ("10:30", 365.30, 366.40, 365.00, 366.20, 800_000),
    ("10:35", 366.20, 367.10, 365.90, 366.90, 700_000),
    ("10:40", 366.90, 368.00, 366.50, 367.80, 700_000),
    ("10:45", 367.80, 368.90, 367.40, 368.70, 700_000),
    ("10:50", 368.70, 369.60, 368.30, 369.40, 700_000),
    ("10:55", 369.40, 370.30, 369.10, 370.10, 900_000),    # high >= 370.04 -> TARGET at 11:00
    ("11:00", 370.10, 370.50, 369.60, 369.90, 600_000),
]
SLIDE = [  # the 10:40 bar trades through 358.32
    ("10:25", 364.18, 364.30, 362.50, 362.80, 800_000),
    ("10:30", 362.80, 363.00, 360.90, 361.20, 800_000),
    ("10:35", 361.20, 361.60, 359.80, 360.10, 900_000),
    ("10:40", 360.10, 360.40, 358.00, 358.60, 1_200_000),  # low <= 358.32 -> STOP at 10:45
    ("10:45", 358.60, 359.20, 357.90, 358.90, 700_000),
]

ENTRY, STOP, TARGET, SHARES = 364.18, 358.32, 370.04, 8
RISK_PER_SHARE = round(ENTRY - STOP, 2)            # 5.86
RISK = round(RISK_PER_SHARE * SHARES, 2)           # 46.88
VERDICTS = ("ODDS LEAN YOUR WAY", "ODDS LEAN AGAINST YOU", "CLOSE TO A COIN FLIP")

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Yahoo! Finance: TSLA News</title>
<item><title>Tesla beats third-quarter earnings estimates as deliveries rise</title>
<link>https://finance.yahoo.com/news/tesla-beats-third-quarter-earnings-estimates-130500123.html</link>
<pubDate>Fri, 30 Oct 2026 13:05:00 +0000</pubDate>
<description>Quarterly results came in above analyst estimates.</description></item>
<item><title>Tesla shares edge higher in early trading</title>
<link>https://finance.yahoo.com/news/tesla-shares-edge-higher-early-134500456.html</link>
<pubDate>Fri, 30 Oct 2026 13:45:00 +0000</pubDate></item>
</channel></rss>
"""


def _bars(rows: Sequence[Tuple[str, float, float, float, float, float]], day: str = DAY) -> List[tc.Bar]:
    return [tc.Bar(et_ts(day, t), o, h, l, c, v) for t, o, h, l, c, v in rows]


def _history_5m() -> List[tc.Bar]:
    """59 prior sessions of 5-minute bars, scaled so the last close is today's open (no gap)."""
    raw = random_walk_history(PREV, 59, start_price=330.0, vol=0.002, seed=11)
    k = OPEN_PX / raw[-1].close
    return [tc.Bar(b.ts, round(b.open * k, 2), round(b.high * k, 2), round(b.low * k, 2),
                   round(b.close * k, 2), round(b.volume * 8)) for b in raw]


def _history_daily() -> List[tc.Bar]:
    """~6 months of daily bars ending yesterday at today's open: an uptrend into a 5-week base
    just under 360 (20-day high 362.16), so the scanner sees today's 10:16 push to 364.18 on
    ~2x paced volume as a 20-day breakout - the video's first card, "TSLA · BREAKOUT"."""
    rng = random.Random(7)
    closes = [100.0]
    for _ in range(99):
        closes.append(closes[-1] * math.exp(rng.gauss(0.003, 0.02)))
    k = 350.0 / closes[-1]
    closes = [round(c * k, 2) for c in closes]
    closes += [round(354.0 + 4.5 * math.sin(i * 0.9), 2) for i in range(24)] + [OPEN_PX]
    return daily_bars(PREV, closes, wick_pct=0.006,
                      volumes=[20_000_000 + 1_000_000 * (i % 7) for i in range(len(closes))])


HISTORY_5M = _history_5m()
HISTORY_1D = _history_daily()


class DeskTestCase(TempHomeTestCase):
    """One shared $DAYTRADER_HOME and fixture folder; every skill runs as a real subprocess."""

    def setUp(self) -> None:
        super().setUp()
        self.fixtures = self.home / "fixtures"
        self.state = self.home / "desk"
        self.fixtures.mkdir()
        self.state.mkdir()
        write_fixture(self.fixtures, f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={SYM}"
                                     "&region=US&lang=en-US", RSS)

    # -- fixtures --------------------------------------------------------------------------

    def publish(self, now: str, session: Sequence[tc.Bar], price: float) -> None:
        """Write the Yahoo payloads as the live API would answer at `now` (finished bars only)."""
        now_ts = et_ts(DAY, now)
        today = [b for b in session if b.ts + 300 <= now_ts]
        meta = {"regularMarketTime": now_ts - 5, "longName": "Tesla, Inc.",
                "currentTradingPeriod": {"regular": {"start": et_ts(DAY, "09:30"), "end": et_ts(DAY, "16:00"),
                                                     "timezone": "EDT", "gmtoffset": -14400}}}
        last5 = sorted({b.date for b in HISTORY_5M})[-4:]
        recent = [b for b in HISTORY_5M if b.date in last5]
        day_bar = tc.Bar(et_ts(DAY, "09:30"), today[0].open, max(b.high for b in today),
                         min(b.low for b in today), today[-1].close, sum(b.volume for b in today))
        tick = tc.Bar(now_ts - now_ts % 60 - 60, price, price, price, price, 12_000)
        for (interval, rng), bars in {("5m", "60d"): HISTORY_5M + today, ("5m", "5d"): recent + today,
                                      ("1m", "1d"): [tick], ("1d", "6mo"): HISTORY_1D + [day_bar]}.items():
            write_fixture(self.fixtures, yahoo_chart_url(SYM, interval, rng),
                          yahoo_payload(bars, SYM, price=price, **meta))

    # -- running the skills ----------------------------------------------------------------

    def run_text(self, skill: str, script: str, *args: str, now: str, expect: int = 0) -> str:
        """`now` is an ET time today (10:16) or a full ET stamp (2026-10-31T11:00)."""
        env = {"DAYTRADER_HOME": str(self.state), "DAYTRADER_FIXTURES": str(self.fixtures),
               "DAYTRADER_NOW": now if "T" in now else f"{DAY}T{now}"}
        p = run_cli(skill_script(skill, script), *args, env=env, timeout=60)
        where = f"{script} {' '.join(args)} @ {now}: exit {p.returncode}\nstderr: {p.stderr}\nstdout: {p.stdout[:2000]}"
        self.assertNotIn("Traceback", p.stderr, where)
        self.assertEqual(p.returncode, expect, where)
        return p.stdout

    def run_skill(self, skill: str, script: str, *args: str, now: str,
                  expect: int = 0) -> Dict[str, Any]:
        return json.loads(self.run_text(skill, script, *args, "--json", now=now, expect=expect))

    def plan(self) -> Dict[str, Any]:
        return json.loads((self.state / "plans" / f"{SYM}.json").read_text(encoding="utf-8"))

    def ledger(self) -> Dict[str, Any]:
        return json.loads((self.state / "ledger.json").read_text(encoding="utf-8"))


class DeskFlowTest(DeskTestCase):
    """Two complete mornings on the paper desk: one hits the target, one hits the stop."""

    # -- the shared morning: scan, plan, test, size, ticket, yes ----------------------------

    def plan_and_fill(self, after: Sequence[tc.Bar]) -> Dict[str, Any]:
        session = _bars(MORNING) + list(after)

        # Scanner and news at 10:16 (daily bars + RSS): the video's first card, TSLA · BREAKOUT.
        self.publish("10:16", session, 364.18)
        scan = self.run_skill("scanner", "scan.py", "--symbols", SYM, now="10:16")
        self.assertEqual((scan["scanned"], scan["skipped"], scan["passed_filters"]), (1, 0, 1), scan)
        self.assertTrue(scan["partial_day"])
        self.assertEqual([r["symbol"] for r in scan["results"]], [SYM], scan)
        hit = scan["results"][0]
        self.assertEqual([s["setup"] for s in hit["setups"]], ["breakout"], hit)
        self.assertEqual((hit["bias"], hit["last"], hit["key_level"]), ("long", ENTRY, {"name": "high_20d",
                                                                                         "price": 362.16}))
        self.assertGreaterEqual(hit["rvol"], 1.5)                  # the breakout's volume confirmation
        self.assertIn("above the 20-day high 362.16", hit["setups"][0]["detail"])
        # The data is stamped 10:15:55, so this replay is honest: no look-ahead warning.
        self.assertFalse(hit["after_as_of"])
        self.assertIsNone(scan["replay_warning"])

        news = self.run_skill("news", "news.py", SYM, now="10:16")
        self.assertTrue(news["warnings"], "search/SEC have no fixtures: they must fail softly")
        earnings = [it for it in news["items"] if "EARNINGS" in it["tags"]]
        self.assertEqual(len(earnings), 1, news["items"])
        self.assertEqual(earnings[0]["impact"], "HIGH")

        # Entry: the ORB long triggers on the 10:10 bar's close; --save starts the plan.
        ent = self.run_skill("entry", "entry.py", SYM, "--save", now="10:16:30")
        self.assertEqual(ent["status"], "TRIGGERED", ent)
        self.assertEqual((ent["setup"], ent["side"], ent["triggered_at"]), ("orb", "long", "10:15"))
        self.assertEqual((ent["entry"], ent["suggested_stop"]), (ENTRY, STOP))
        self.assertEqual((ent["reference"]["or_high"], ent["reference"]["or_low"]), (362.40, 358.32))
        self.assertIsNotNone(ent["saved"])
        plan = self.plan()
        self.assertEqual((plan["side"], plan["entry"], plan["stop"], plan["setup"]), ("long", ENTRY, STOP, "orb"))
        self.assertEqual(plan["entry_signal"]["triggered_at"], "10:15")
        self.assertIsNone(plan.get("target"))

        # Exit: keep the entry's OR-low stop (manual = the saved plan's stop), 1R target.
        ex = self.run_skill("exit", "exit_plan.py", SYM, "--method", "manual", "--rr", "1", "--save",
                            now="10:17")
        self.assertEqual((ex["entry_source"], ex["stop_source"], ex["side_source"]),
                         ("saved plan", "saved plan", "saved plan"))
        self.assertEqual((ex["side"], ex["entry"], ex["stop"], ex["target"]), ("long", ENTRY, STOP, TARGET))
        self.assertAlmostEqual(ex["rr"], 1.0, places=6)
        self.assertAlmostEqual(ex["risk_per_share"], RISK_PER_SHARE, places=6)
        self.assertTrue(ex["saved"])
        plan = self.plan()
        self.assertEqual(plan["target"], TARGET)
        self.assertEqual((plan["exit_plan"]["method"], plan["exit_plan"]["time_stop"]), ("manual", "15:55"))
        self.assertIn("entry_signal", plan)                      # earlier sections survive the merge

        # Time Machine: reads every level from the saved plan, replays from 10:15.
        tm = self.run_skill("time-machine", "time_machine.py", SYM, "--save", now="10:18")
        self.assertEqual(tm["levels_source"], {"entry": "saved plan", "stop": "saved plan", "target": "saved plan"})
        self.assertEqual((tm["side"], tm["entry"], tm["stop"], tm["target"]), ("long", ENTRY, STOP, TARGET))
        self.assertEqual((tm["mode"], tm["start"], tm["paths"], tm["sessions_used"]), ("now", "10:15", 10_000, 59))
        self.assertAlmostEqual(tm["target_first_pct"] + tm["stop_first_pct"] + tm["open_at_close_pct"], 100.0,
                               delta=0.02)
        self.assertIn(tm["verdict"], VERDICTS)
        self.assertEqual(tm["dropped_sections"], [])
        plan = self.plan()
        self.assertEqual(plan["time_machine"]["verdict"], tm["verdict"])
        self.assertEqual(plan["time_machine"]["target_first_pct"], tm["target_first_pct"])
        self.assertEqual(plan["time_machine"]["neither_pct"], tm["open_at_close_pct"])
        self.assertIn("exit_plan", plan)

        # Risk: size the saved plan with the default settings ($25k, 0.2% = $50 budget).
        rk = self.run_skill("risk", "risk.py", "size", "--symbol", SYM, "--save", now="10:19")
        self.assertEqual((rk["shares"], rk["side"], rk["order"]), (SHARES, "long", f"BUY {SHARES} {SYM}"))
        self.assertAlmostEqual(rk["risk_used"], RISK, places=2)
        self.assertEqual(rk["sources"], {"entry": "plan", "stop": "plan", "side": "plan", "target": "plan"})
        self.assertTrue(rk["budget"]["ok"])
        self.assertEqual(rk["dropped_sections"], [])
        plan = self.plan()
        self.assertEqual(plan["sizing"]["shares"], SHARES)
        self.assertIn("time_machine", plan)
        self.assertIn("exit_plan", plan)

        # Budget: the guard-rail check Claude runs before any ticket.
        chk = self.run_skill("budget", "budget.py", "check", "--symbol", SYM, now="10:19:30")
        self.assertEqual((chk["ok"], chk["verdict"], chk["exit_code"]), (True, "OK", 0))
        self.assertAlmostEqual(chk["new_risk"], RISK, places=2)

        # Broker: a ticket from the plan waits for the user's yes; nothing is filled yet.
        self.publish("10:20:10", session, 364.00)
        tk = self.run_skill("broker", "broker.py", "ticket", "--symbol", SYM, now="10:20:10")
        t = tk["ticket"]
        self.assertEqual((tk["status"], t["status"], t["mode"]), ("pending", "pending", "paper"))
        self.assertEqual((t["side"], t["qty"], t["entry"], t["stop"], t["target"]),
                         ("long", SHARES, ENTRY, STOP, TARGET))
        self.assertEqual((t["levels_source"], t["qty_source"], t["setup"], t["time_stop"]),
                         ("plan", "plan sizing", "orb", "15:55"))
        self.assertEqual(t["time_machine"]["verdict"], tm["verdict"])
        for words in (f"BUY {SHARES} {SYM}", "PAPER", "REAL MONEY: OFF", "WAITING FOR YOUR YES", t["id"]):
            self.assertIn(words, tk["card"])
        self.assertFalse(tk["real_money"])
        self.assertEqual(self.ledger()["trades"], [])

        # Without --yes nothing happens (exit 1), and the ticket keeps waiting.
        self.publish("10:22", session, 364.18)
        no = self.run_skill("broker", "broker.py", "confirm", t["id"], now="10:21", expect=1)
        self.assertFalse(no["confirmed"])
        self.assertEqual(self.ledger()["trades"], [])
        self.assertEqual(self.ledger()["tickets"][0]["status"], "pending")

        # The user's explicit yes (a later message): a marketable limit fills at the quote.
        yes = self.run_skill("broker", "broker.py", "confirm", t["id"], "--yes", now="10:22")
        self.assertEqual((yes["confirmed"], yes["status"], yes["fill_price"]), (True, "filled", ENTRY))
        trade = yes["trade"]
        self.assertEqual((trade["status"], trade["qty"], trade["entry_price"], trade["ticket_id"]),
                         ("open", SHARES, ENTRY, t["id"]))
        self.assertEqual(trade["time_machine"]["verdict"], tm["verdict"])

        # Budget sees the open position as committed risk.
        st = self.run_skill("budget", "budget.py", "status", now="10:23")
        self.assertEqual(st["status"], "OPEN FOR TRADING")
        self.assertAlmostEqual(st["open_risk"]["total"], RISK, places=2)
        self.assertEqual((st["trades_today"], len(st["positions"])), (1, 1))
        self.assertAlmostEqual(st["room_for_new_risk"], 150.0 - RISK, places=2)

        # Alerts read the same plan: stop below / target above for a long.
        al = self.run_skill("alerts", "alerts.py", "from-plan", SYM, "--levels", "stop,target", now="10:23")
        self.assertEqual((al["plan"]["side"], al["plan"]["stop"], al["plan"]["target"]), ("long", STOP, TARGET))
        self.assertEqual(sorted((a["plan_role"], a["kind"], a["level"]) for a in al["added"]),
                         [("stop", "below", STOP), ("target", "above", TARGET)])
        return {"session": session, "ticket": t, "trade": trade, "tm": tm, "risk": rk}

    def assert_alert_fired(self, now: str, role: str, price: float) -> None:
        fired = self.run_skill("alerts", "alerts.py", "check", now=now)["fired"]
        self.assertEqual([(f["plan_role"], f["price"]) for f in fired], [(role, price)])

    def closed_trade(self) -> Dict[str, Any]:
        trades = self.ledger()["trades"]
        self.assertEqual(len(trades), 1)
        return trades[0]

    # -- the two endings -------------------------------------------------------------------

    def test_plan_a_trade_on_tsla_and_test_it_target_hit(self):
        m = self.plan_and_fill(_bars(RALLY))

        # 10:31: up but not there yet - the trade stays open.
        self.publish("10:31", m["session"], 366.20)
        mid = self.run_skill("broker", "broker.py", "sync", now="10:31")
        self.assertEqual([e for e in mid["events"] if e.get("type") == "closed"], [])
        self.assertEqual(self.closed_trade()["status"], "open")

        # 11:01: the 10:55 bar traded through 370.04 -> closed at the target, +1R.
        self.publish("11:01", m["session"], 369.90)
        self.run_skill("broker", "broker.py", "sync", now="11:01")
        tr = self.closed_trade()
        self.assertEqual((tr["status"], tr["exit_reason"], tr["exit_price"]), ("closed", "target", TARGET))
        self.assertAlmostEqual(tr["pnl"], RISK, places=2)
        self.assertAlmostEqual(tr["r_multiple"], 1.0, places=2)
        self.assertTrue(str(tr["closed_at"]).startswith(f"{DAY}T11:00"), tr["closed_at"])
        self.assert_alert_fired("11:01", "target", TARGET)

        st = self.run_skill("budget", "budget.py", "status", now="11:02")
        self.assertEqual(st["status"], "OPEN FOR TRADING")
        self.assertAlmostEqual(st["realized_pnl"], RISK, places=2)
        self.assertEqual((st["wins"], st["losses"], st["closed_today"], st["trades_today"], st["loss_streak"]),
                         (1, 0, 1, 1, 0))
        self.assertEqual(st["open_risk"]["total"], 0)
        self.assertEqual(st["loss_budget"]["used"], 0)

        rv = self.run_skill("analyst", "analyst.py", "review", now="11:03")
        self.assertEqual((rv["count"], rv["wins"], rv["losses"]), (1, 1, 0))
        self.assertAlmostEqual(rv["net_pnl"], RISK, places=2)
        self.assertAlmostEqual(rv["total_r"], 1.0, places=2)
        self.assertEqual([b["key"] for b in rv["by_setup"]], ["orb"])
        self.assertEqual(rv["trades"][0]["exit"], "target")
        self.assertEqual(rv["time_machine_check"]["n"], 1)
        self.assertEqual(rv["time_machine_check"]["actual_target_pct"], 100.0)
        self.assertEqual(rv["by_time_machine"][0]["key"], _lean(m["tm"]["verdict"]))
        # The one Time Machine result reads the same on every card: TM, risk, broker and the review.
        shown = m["tm"]["display_pct"]
        self.assertEqual((rv["time_machine_check"]["predicted_target_first_pct"],
                          rv["time_machine_check"]["predicted_stop_first_pct"]),
                         (shown["target_first"], shown["stop_first"]))
        words = f"target first {shown['target_first']:.1f}% / stop first {shown['stop_first']:.1f}%"
        self.assertIn(f"time-machine: {words} - {m['tm']['verdict']}", m["risk"]["notes"])
        self.assertEqual(m["ticket"]["time_machine"]["display_pct"], shown)
        text = self.run_text("analyst", "analyst.py", "review", now="11:03")
        self.assertIn(f"predicted target-first {shown['target_first']:.1f}% / stop-first "
                      f"{shown['stop_first']:.1f}%", text)

    def test_plan_a_trade_on_tsla_and_test_it_stop_hit(self):
        m = self.plan_and_fill(_bars(SLIDE))

        # 10:46: the 10:40 bar traded through 358.32 -> stopped out, -1R.
        self.publish("10:46", m["session"], 358.90)
        sync = self.run_skill("broker", "broker.py", "sync", now="10:46")
        self.assertEqual(sync["summary"]["losses"], 1, sync)
        tr = self.closed_trade()
        self.assertEqual((tr["status"], tr["exit_reason"], tr["exit_price"]), ("closed", "stop", STOP))
        self.assertAlmostEqual(tr["pnl"], -RISK, places=2)
        self.assertAlmostEqual(tr["r_multiple"], -1.0, places=2)
        self.assertTrue(str(tr["closed_at"]).startswith(f"{DAY}T10:45"), tr["closed_at"])
        self.assert_alert_fired("10:46", "stop", STOP)

        st = self.run_skill("budget", "budget.py", "status", now="10:47")
        self.assertAlmostEqual(st["realized_pnl"], -RISK, places=2)
        self.assertEqual((st["wins"], st["losses"], st["loss_streak"], st["trades_today"]), (0, 1, 1, 1))
        self.assertAlmostEqual(st["loss_budget"]["used"], RISK, places=2)
        self.assertAlmostEqual(st["loss_budget"]["left"], 150.0 - RISK, places=2)
        self.assertEqual(st["open_risk"]["total"], 0)
        self.assertEqual(st["status"], "OPEN FOR TRADING")

        # The next ticket's budget check now counts the realized loss.
        chk = self.run_skill("budget", "budget.py", "check", "--risk", "110", now="10:48", expect=3)
        self.assertEqual(chk["verdict"], "BLOCKED")

        rv = self.run_skill("analyst", "analyst.py", "review", now="10:49")
        self.assertEqual((rv["count"], rv["wins"], rv["losses"]), (1, 0, 1))
        self.assertAlmostEqual(rv["net_pnl"], -RISK, places=2)
        self.assertAlmostEqual(rv["total_r"], -1.0, places=2)
        self.assertEqual(rv["trades"][0]["exit"], "stop")
        self.assertEqual(rv["time_machine_check"]["actual_stop_pct"], 100.0)


def _full_session() -> List[tc.Bar]:
    """Today's whole session: the rally morning, then a quiet seeded walk to the 15:55 bar."""
    morning = _bars(MORNING) + _bars(RALLY)
    pad = random_walk_session(DAY, morning[-1].close, n_bars=78, vol=0.002, seed=99)[len(morning):]
    k = morning[-1].close / pad[0].open
    return morning + [tc.Bar(b.ts, round(b.open * k, 2), round(b.high * k, 2), round(b.low * k, 2),
                             round(b.close * k, 2), b.volume * 6) for b in pad]


class AfterHoursDeskFlowTest(DeskTestCase):
    """The same desk run after the close and on the weekend: every step still runs, the plan says
    its trigger is from a finished session, and the broker shows a preview with nothing to confirm."""

    def test_plan_and_test_after_the_close_and_on_saturday(self):
        session = _full_session()
        for now in (f"{DAY}T19:30", "2026-10-31T11:00"):
            with self.subTest(now=now):
                shutil.rmtree(self.state)                 # a fresh desk for each run
                self.state.mkdir()
                self.publish("16:00", session, session[-1].close)
                ent = self.run_skill("entry", "entry.py", SYM, "--save", now=now)
                self.assertEqual((ent["status"], ent["entry"], ent["suggested_stop"]), ("TRIGGERED", ENTRY, STOP))
                self.assertTrue(any(f"finished {DAY} session" in n for n in ent["notes"]), ent["notes"])
                self.assertTrue(self.plan()["entry_signal"]["session_over"])
                ex = self.run_skill("exit", "exit_plan.py", SYM, "--method", "manual", "--rr", "1", "--save", now=now)
                self.assertEqual((ex["entry"], ex["stop"], ex["target"]), (ENTRY, STOP, TARGET))
                self.assertTrue(any("re-plan from the next open" in w for w in ex["warnings"]), ex["warnings"])
                tm = self.run_skill("time-machine", "time_machine.py", SYM, "--save", now=now)
                self.assertEqual((tm["mode"], tm["start"]), ("open", "09:30"))
                self.assertIn("market closed - simulating the next session from the open", tm["notes"])
                shown = tm["display_pct"]
                words = f"target first {shown['target_first']:.1f}% / stop first {shown['stop_first']:.1f}%"
                tm_text = self.run_text("time-machine", "time_machine.py", SYM, now=now)
                for label, key in (("HIT TARGET FIRST", "target_first"), ("HIT STOP FIRST", "stop_first")):
                    self.assertRegex(tm_text, rf"{label}\s+{shown[key]:.1f}%")
                rk = self.run_skill("risk", "risk.py", "size", "--symbol", SYM, "--save", now=now)
                self.assertEqual(rk["shares"], SHARES)
                self.assertIn(f"time-machine: {words} - {tm['verdict']}", rk["notes"])
                chk = self.run_skill("budget", "budget.py", "check", "--symbol", SYM, now=now)
                self.assertEqual(chk["verdict"], "OK")
                tk = self.run_skill("broker", "broker.py", "ticket", "--symbol", SYM, now=now)
                self.assertEqual((tk["status"], tk["preview"], tk.get("ticket")), ("preview", True, None))
                self.assertIn("MARKET CLOSED — preview only", tk["card"])
                card = self.run_text("broker", "broker.py", "ticket", "--symbol", SYM, now=now)
                self.assertIn("ORDER TICKET · PREVIEW (not a ticket - nothing to confirm)", card)
                self.assertIn(f"time-machine: {tm['verdict']} ({words})", card)
                self.assertNotIn("WAITING FOR YOUR YES", card)
                self.assertFalse((self.state / "ledger.json").exists() and self.ledger()["tickets"])
                # nothing was saved, so there is nothing a "yes" could confirm
                self.run_text("broker", "broker.py", "confirm", f"TKT-{DAY.replace('-', '')}-001", "--yes",
                              now=now, expect=1)


# The README's desk-flow commands, in order, and the (ET) time each one runs at.
DESK_STEPS = [("scanner", "scan.py", "10:16"), ("news", "news.py", "10:16"), ("entry", "entry.py", "10:16:30"),
              ("exit", "exit_plan.py", "10:17"), ("time-machine", "time_machine.py", "10:18"),
              ("risk", "risk.py", "10:19"), ("budget", "budget.py", "10:19:30"),
              ("broker", "broker.py", "10:20:10")]


def readme_block(after: str, lang: str) -> List[str]:
    """The non-blank lines of README.md's first ```<lang> block that follows the text `after`."""
    text = README.read_text(encoding="utf-8")
    start = text.index(f"```{lang}\n", text.index(after)) + len(lang) + 4
    return [ln for ln in text[start:text.index("```", start)].splitlines() if ln.strip()]


def readme_commands() -> List[Tuple[str, str, List[str]]]:
    """(skill, script, args) for each command of the README's desk-flow block, comments dropped."""
    out = []
    for line in readme_block("## The desk flow", "bash"):
        words = shlex.split(line, comments=True)
        assert words[0] == "python3" and words[1].startswith("skills/"), line
        _, skill, _, script = words[1].split("/")
        out.append((skill, script, words[2:]))
    return out


class ReadmeSampleTest(DeskTestCase):
    """The README's desk-flow commands work as written, and its sample output is real output."""

    def morning(self) -> List[Tuple[str, str]]:
        """Run the README's commands in text mode on the morning that hits the target."""
        session = _bars(MORNING) + _bars(RALLY)
        commands = readme_commands()
        self.assertEqual([(skill, script) for skill, script, _ in commands],
                         [(skill, script) for skill, script, _ in DESK_STEPS])
        out = []
        for (skill, script, args), (_, _, now) in zip(commands, DESK_STEPS):
            if skill in ("scanner", "broker"):  # fresh quotes for the first look and for the ticket
                self.publish(now, session, 364.18 if skill == "scanner" else 364.00)
            out.append((f"$ {skill}/scripts/{script} {' '.join(args)}   # {DAY} {now} ET",
                        self.run_text(skill, script, *args, now=now)))
        return out

    def test_readme_sample_lines_are_printed_by_the_readme_commands(self):
        printed = {ln.strip() for _, text in self.morning() for ln in text.splitlines()}
        sample = readme_block(SAMPLE_INTRO, "text")
        self.assertGreaterEqual(len(sample), 10)
        missing = [ln for ln in sample if ln.strip() not in printed]
        self.assertEqual(missing, [], "README sample lines that no step printed (regenerate with "
                                      "python3 tests/test_desk_flow.py --print-morning)")
        for words in ("TSLA", "breakout", "ORB(15)", "THE PLAN", "TIME MACHINE", "BUDGET CHECK",
                      "WAITING FOR YOUR YES"):
            self.assertTrue(any(words in ln for ln in sample), words)


def print_morning() -> None:
    """Print every step's text output, to regenerate the README sample block."""
    case = ReadmeSampleTest("test_readme_sample_lines_are_printed_by_the_readme_commands")
    case.setUp()
    try:
        for header, text in case.morning():
            print(header)
            print(text)
    finally:
        case.doCleanups()


def _lean(verdict: str) -> str:
    return {"ODDS LEAN YOUR WAY": "leaned your way", "ODDS LEAN AGAINST YOU": "leaned against you",
            "CLOSE TO A COIN FLIP": "coin flip"}[verdict]


if __name__ == "__main__":
    if sys.argv[1:] == ["--print-morning"]:
        print_morning()
    else:
        unittest.main()
