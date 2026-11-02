#!/usr/bin/env python3
"""Desk check: the RUNBOOK's three-stop check of a batch of ideas, scripted. PAPER ONLY, no orders.

An idea stays live for 15 minutes after its trigger bar closes, so the desk's checks must run in
seconds, not one hand-typed command at a time. This tool runs the same skill CLIs the runbook lists,
in throwaway homes (one per stop, each a copy of --home's settings.json; the desk itself is never
touched), and applies the numeric pass rules. Per idea and per stop (atr15: a 1.5 x ATR stop,
struct: the structure stop over 36 bars, atr4: a 4 x ATR stop):
  entry.py SYM --setup S [--level X] --side L --cutoff C --save    the trigger (--cutoff per market)
  exit_plan.py SYM --entry E --side L <stop flags> --rr 3 --save   the stop and the 3R target
  time_machine.py SYM --save                                        the replay odds and expected R
  risk.py size --symbol SYM --save                                  the desk's size for that plan
so each home ends with a complete plans/<SYM>.json. Ideas run --workers (3) at a time, and the
homes share one download cache (--out/.cache), so a batch of ten takes about a minute. The rows say:
  live     entry.py says TRIGGERED on a bar that closed within --live-minutes (15) of now
           ($DAYTRADER_NOW in a replay); an older trigger is a watch, not an idea
  best     the stop with the highest expected R (crypto: after fees)
  passes   live, best expected R >= --min-r (0.2), its verdict is not ODDS LEAN AGAINST YOU, sized
           above zero, and liquid: analyst.py brief's 20-day dollar volume >= --min-dollar-volume
           ($20M stocks, $5M crypto; 0 skips that call; an unreadable brief leaves it unknown)
The news rule ("no red-flag news") is NOT applied here: the caller checks the news (news.py, the
news agents), then copies the best home's plan into the desk and trades it with paper_autotrade.py
--plan saved. Every number comes from the skills' own JSON; nothing here places an order.

  python3 tools/desk_check.py --home ~/.day-trader --out $OUT/homes --ideas $OUT/ideas.json --json
  python3 tools/desk_check.py --home ~/.day-trader --out $OUT/homes BTC-USDT --setup level --level 77654.44

--ideas FILE: a JSON list of {"symbol", "setup", "side" (default long), "level" (setup level),
"entry" (used only when there is no trigger, e.g. an ORB hit's trigger close), "note"}.
--candidates FILE merges one dashboard row per idea into that candidates file (decision "skip" or
"pass (numbers; news pending)" with the reason).
Exit codes: 0 every idea processed (fails and skips are in the rows), 1 bad input, 2 a data source
was down for every idea.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(ROOT / "shared"))
import trader_common as tc  # noqa: E402

sys.path.insert(0, str(TOOLS))
import paper_autotrade as auto  # noqa: E402  (price_arg(), desk_home(), the shared constants)

SKILLS = ROOT / "skills"
ENTRY = SKILLS / "entry" / "scripts" / "entry.py"
TIME_MACHINE = SKILLS / "time-machine" / "scripts" / "time_machine.py"
ANALYST = SKILLS / "analyst" / "scripts" / "analyst.py"
EXIT_PLAN, RISK = auto.EXIT_PLAN, auto.RISK
STOPS = {                                   # the runbook's three standard stops, in its order
    "atr15": ["--method", "atr", "--atr-mult", "1.5"],
    "struct": ["--method", "structure", "--lookback", "36"],
    "atr4": ["--method", "atr", "--atr-mult", "4"],
}
AGAINST = "ODDS LEAN AGAINST YOU"
DEFAULT_RR = 3.0                            # the owner's 3:1 rule (2026-11-01)
DEFAULT_MIN_R = 0.2                         # the desk enters at +0.2R or better
DEFAULT_LIVE_MINUTES = 15
CUTOFF = {"us": "15:55", "crypto": "23:55"}  # without --cutoff entry.py stops looking 2 h after the open
MIN_DOLLAR_VOLUME = {"us": 20e6, "crypto": 5e6}
EXIT_DATA, CALL_TIMEOUT = auto.EXIT_DATA, auto.CALL_TIMEOUT
DEFAULT_WORKERS = 3                         # ideas checked at once (each one's stops run in turn)
PASS, SKIP = "pass (numbers; news pending)", "skip"


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def load_ideas(args):
    if args.ideas:
        if args.symbol:
            raise ValueError("give a SYMBOL or --ideas FILE, not both")
        try:
            rows = json.loads(Path(args.ideas).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"--ideas {args.ideas}: {e}") from None
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"--ideas {args.ideas}: expected a non-empty JSON list of ideas")
    elif args.symbol:
        rows = [{"symbol": args.symbol, "setup": args.setup, "side": args.side, "level": args.level,
                 "entry": args.entry, "note": args.note}]
    else:
        raise ValueError("give a SYMBOL (with --setup) or --ideas FILE")
    ideas = []
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not str(row.get("symbol") or "").strip():
            raise ValueError(f"idea {n}: needs a symbol")
        sym = str(row["symbol"]).strip().upper()
        setup = str(row.get("setup") or "").strip().lower()
        if not setup:
            raise ValueError(f"idea {n} ({sym}): needs a setup (orb, vwap, level, pdh, pdl)")
        side = str(row.get("side") or "long").strip().lower()
        if side not in ("long", "short"):
            raise ValueError(f"idea {n} ({sym}): side must be long or short, not {row.get('side')!r}")
        level = row.get("level")
        if setup == "level" and level is None:
            raise ValueError(f"idea {n} ({sym}): --setup level needs a level")
        for k in ("level", "entry"):
            if row.get(k) is not None and float(row[k]) <= 0:
                raise ValueError(f"idea {n} ({sym}): {k} must be a positive price")
        ideas.append({"symbol": sym, "setup": setup, "side": side, "level": level, "entry": row.get("entry"),
                      "note": row.get("note"), "cutoff": row.get("cutoff")})
    return ideas


# ---------------------------------------------------------------------------
# Throwaway homes
# ---------------------------------------------------------------------------

def make_home(out, sym, stop, settings):
    """$OUT/homes/<SYM>-<stop>, fresh: plans/, a copy of the desk's settings.json, and cache/ as a link
    to the batch's shared download cache (--out/.cache), so three stops fetch each chart once."""
    home = out / f"{sym}-{stop}"
    if home.exists():
        shutil.rmtree(home)
    (home / "plans").mkdir(parents=True)
    shutil.copyfile(settings, home / "settings.json")
    shared = out / ".cache"
    shared.mkdir(exist_ok=True)
    try:
        os.symlink(shared, home / "cache", target_is_directory=True)
    except OSError:                                    # no symlinks here: a private cache, still correct
        (home / "cache").mkdir()
    return home


def call(script, home, *args):
    """Run a skill command in `home` (an explicit environment per call: ideas run in threads).
    $DAYTRADER_NOW (a replay) and $DAYTRADER_FIXTURES pass through from this process's environment.
    Returns (exit code, its --json output - {} without one -, its last stderr line)."""
    what = f"{script.name} {' '.join(args[:2])}"
    env = dict(os.environ, DAYTRADER_HOME=str(home))
    try:
        p = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, env=env,
                           timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return EXIT_DATA, {}, f"data error: {what} gave no answer in {CALL_TIMEOUT} s"
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        out = None
    lines = [ln.strip() for ln in p.stderr.splitlines() if ln.strip()]
    return p.returncode, out if isinstance(out, dict) else {}, lines[-1] if lines else f"{what} exited {p.returncode}"


def plan_file(home):
    files = sorted((home / "plans").glob("*.json"))
    return str(files[0]) if files else None


# ---------------------------------------------------------------------------
# The skills
# ---------------------------------------------------------------------------

def trigger(idea, home, cutoff):
    args = [idea["symbol"], "--setup", idea["setup"], "--side", idea["side"], "--cutoff", cutoff, "--save", "--json"]
    if idea["level"] is not None:
        args += ["--level", auto.price_arg(idea["level"])]
    code, out, err = call(ENTRY, home, *args)
    if code != 0:
        return code, None, err
    since = out.get("since_trigger") or {}
    return 0, {
        "status": out.get("status"), "label": out.get("label"), "date": out.get("date"),
        "triggered_at": out.get("triggered_at"), "entry": out.get("entry"),
        "trigger_bar": {k: (out.get("trigger_bar") or {}).get(k) for k in ("start", "end", "close", "volume", "rvol")},
        "minutes_since": since.get("minutes"), "stop_hit_since": since.get("stop_hit"),
        "as_of": out.get("as_of"), "cutoff": out.get("cutoff"), "session_over": out.get("session_over"),
        "expired_reason": out.get("expired_reason"), "notes": out.get("notes") or [],
    }, None


def stop_plan(idea, home, entry, name, rr):
    code, out, err = call(EXIT_PLAN, home, idea["symbol"], "--entry", auto.price_arg(entry), "--side", idea["side"],
                          *STOPS[name], "--rr", f"{rr:g}", "--save", "--json")
    if code != 0 or not out.get("saved"):
        return code or 1, None, err if code else "exit_plan.py saved no plan"
    return 0, {"stop": out.get("stop"), "target": out.get("target"), "risk_per_share": out.get("risk_per_share"),
               "stop_pct": out.get("stop_pct"), "method": out.get("method") or name}, None


def odds(sym, home):
    code, out, err = call(TIME_MACHINE, home, sym, "--save", "--json")
    if code != 0:
        return code, None, err
    return 0, {k: out.get(k) for k in ("expected_r", "target_first_pct", "stop_first_pct", "open_at_close_pct",
                                       "verdict", "verdict_notes", "paths", "sessions_used", "trend",
                                       "median_minutes_to_resolution")}, None


def sizing(sym, home):
    code, out, err = call(RISK, home, "size", "--symbol", sym, "--save", "--json")
    if code == 1 and out.get("save_error"):          # sized at zero: the desk's risk rules say no
        return 0, {"shares": 0, "risk_used": 0.0, "notional": 0.0, "order": None, "capped_by": None,
                   "note": ((out.get("warnings") or []) + [out["save_error"]])[0]}, None
    if code != 0:
        return code, None, err
    return 0, {k: out.get(k) for k in ("shares", "risk_used", "notional", "order", "capped_by", "position_pct",
                                       "sizing_note")}, None


def liquidity(sym, home, floor):
    """analyst.py brief's 20-day dollar volume against the floor: {"dollar_volume_20d", "liquid",
    "reason", "trend"}. A brief that cannot be read leaves liquid None (unknown), which does not
    fail the idea; the runbook's sweep has already ranked its ideas by liquidity."""
    if floor <= 0:
        return {"dollar_volume_20d": None, "liquid": None, "reason": "not checked (--min-dollar-volume 0)", "trend": None}
    code, out, err = call(ANALYST, home, "brief", sym, "--no-sec", "--json")
    dv = ((out.get("liquidity") or {}).get("avg_dollar_volume_20d")) if code == 0 else None
    trend = ((out.get("trend") or {}).get("label")) if code == 0 else None
    if dv is None:
        return {"dollar_volume_20d": None, "liquid": None, "trend": trend,
                "reason": "liquidity unknown (" + (err if code else "the brief gives no dollar volume") + ")"}
    return {"dollar_volume_20d": dv, "liquid": dv >= floor, "trend": trend,
            "reason": None if dv >= floor else f"20-day dollar volume ${dv:,.0f} under ${floor:,.0f}"}


# ---------------------------------------------------------------------------
# One idea
# ---------------------------------------------------------------------------

def check_idea(idea, args, settings, floor_for):
    sym, side = idea["symbol"], idea["side"]
    market = tc.market_for(sym).key
    res = {"symbol": sym, "side": side, "setup": idea["setup"], "level": idea["level"], "note": idea["note"],
           "market": market, "live": False, "trigger": None, "entry_used": None, "entry_source": None,
           "stops": [], "best": None, "liquidity": None, "passes_numbers": False, "reasons": [],
           "data_error": None, "news": "not checked here"}
    if side == "short" and tc.is_crypto_symbol(sym):
        res["reasons"].append(auto.SPOT_LONG_ONLY)
        return res
    cutoff = idea["cutoff"] or args.cutoff or CUTOFF[market]
    for name in args.stops:
        home = make_home(args.out, sym, name, settings)
        code, trig, err = trigger(idea, home, cutoff)
        if code:
            res["data_error" if code == EXIT_DATA else "error"] = err
            res["reasons"].append(f"entry.py: {err}")
            break
        if res["trigger"] is None:                         # the same trigger in every home
            res["trigger"] = trig
            res["liquidity"] = liquidity(sym, home, floor_for(market))
            minutes = trig.get("minutes_since")
            res["live"] = (trig.get("status") == "TRIGGERED" and minutes is not None
                           and 0 <= minutes <= args.live_minutes)
            if trig.get("status") == "TRIGGERED":
                res["entry_used"], res["entry_source"] = trig["entry"], f"the trigger bar's close ({trig['triggered_at']})"
            elif idea["entry"] is not None:
                res["entry_used"], res["entry_source"] = float(idea["entry"]), "the idea's entry (no trigger yet)"
            if res["entry_used"] is None:
                res["reasons"].append(f"no entry price: entry.py says {trig.get('status')} and the idea gives none")
                break
        row = {"name": name, "home": str(home), "plan_file": None}
        code, plan, err = stop_plan(idea, home, res["entry_used"], name, args.rr)
        if code:
            row["error"] = err
            res["stops"].append(row)
            if code == EXIT_DATA:
                res["data_error"] = err
            continue
        row.update(plan)
        code, tm, err = odds(sym, home)
        if code:
            row["error"] = err
            res["stops"].append(row)
            if code == EXIT_DATA:
                res["data_error"] = err
            continue
        row.update(tm)
        code, sz, err = sizing(sym, home)
        if code:
            row["error"] = err
        else:
            row.update(sz)
        row["plan_file"] = plan_file(home)
        res["stops"].append(row)
    judge(res, args)
    return res


def judge(res, args):
    done = [s for s in res["stops"] if s.get("expected_r") is not None]
    if not done:
        if not res["reasons"]:
            res["reasons"].append("no stop could be replayed")
        return
    best = max(done, key=lambda s: s["expected_r"])
    res["best"] = best["name"]
    checks = [
        ("not live" + (f" (trigger {res['trigger']['minutes_since']:.0f} min old)" if res["trigger"] and res["trigger"].get("minutes_since") is not None else ""), res["live"]),
        (f"expected R {best['expected_r']:+.2f} under {args.min_r:+.2f}", best["expected_r"] >= args.min_r),
        (f"verdict {best.get('verdict')}", best.get("verdict") != AGAINST),
        ("sized at zero" + (f" ({best.get('note')})" if best.get("note") else ""), (best.get("shares") or 0) > 0),
    ]
    liq = res.get("liquidity") or {}
    if liq.get("liquid") is False:
        checks.append((liq.get("reason") or "not liquid", False))
    failed = [label for label, ok in checks if not ok]
    res["reasons"].extend(failed)
    res["passes_numbers"] = not failed


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def candidate_row(res):
    best = next((s for s in res["stops"] if s["name"] == res["best"]), None) or {}
    trig = res.get("trigger") or {}
    setup = res["setup"] + (f" {tc.fmt_price(res['level'])}" if res.get("level") else "")
    if trig.get("triggered_at"):
        setup += f", {trig['triggered_at']} {tc.market_for(res['symbol']).tz_label}"
    if res["passes_numbers"]:
        reason = (f"Live trigger; best stop {res['best']}: {best.get('expected_r'):+.2f}R expected, target first "
                  f"{best.get('target_first_pct')}%, stop first {best.get('stop_first_pct')}%. News check pending.")
    else:
        reason = "; ".join(res["reasons"]) or "skipped"
    return {"symbol": res["symbol"], "side": res["side"], "setup": setup, "entry": res.get("entry_used"),
            "stop": best.get("stop"), "target": best.get("target"), "tm_target_first": best.get("target_first_pct"),
            "tm_stop_first": best.get("stop_first_pct"), "tm_open": best.get("open_at_close_pct"),
            "ev_after_fees": best.get("expected_r"), "verdict": best.get("verdict"),
            "decision": PASS if res["passes_numbers"] else SKIP, "reason": reason}


def merge_candidates(path, rows):
    p = Path(path)
    try:
        old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except ValueError:
        old = []
    if not isinstance(old, list):
        old = []
    new = {r["symbol"]: r for r in rows}
    kept = [r for r in old if not (isinstance(r, dict) and r.get("symbol") in new)]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(kept + list(new.values()), indent=1), encoding="utf-8")


def fmt(x, nd=2):
    if x is None:
        return "-"
    return f"{x:.0f}" if nd == 0 else f"{x:+.{nd}f}"


def qty(x):
    return "-" if x is None else tc.fmt_qty(x)


def render(out):
    lines = [f"DESK CHECK · {len(out['ideas'])} idea(s) · {out['as_of']} · rr {out['rr']:g} · min R {out['min_r']:+.2f} · "
             f"live within {out['live_minutes']} min · paper only, no orders",
             f"{'symbol':<10} {'side':<5} {'setup':<7} {'trigger':<18} {'live':<4} {'20d $vol':>8} {'best':<6} {'stop':>10} "
             f"{'target':>10} {'exp R':>6} {'tgt%':>5} {'stop%':>5} {'size':>9}  verdict / reasons"]
    for r in out["ideas"]:
        t = r.get("trigger") or {}
        trig = f"{t.get('status') or '-'}" + (f" {t['triggered_at']}" if t.get("triggered_at") else "")
        if t.get("minutes_since") is not None:
            trig += f" ({t['minutes_since']:.0f}m)"
        b = next((s for s in r["stops"] if s["name"] == r["best"]), {}) if r["best"] else {}
        tail = ("PASS (numbers; news pending)" if r["passes_numbers"]
                else "skip: " + "; ".join(r["reasons"])) if r["stops"] or r["reasons"] else "-"
        dv = (r.get("liquidity") or {}).get("dollar_volume_20d")
        lines.append(f"{r['symbol']:<10} {r['side']:<5} {r['setup']:<7} {trig:<18} {'yes' if r['live'] else 'no':<4} "
                     f"{('$' + tc.fmt_big(dv)) if dv is not None else '-':>8} {r['best'] or '-':<6} "
                     f"{tc.fmt_price(b['stop']) if b.get('stop') else '-':>10} "
                     f"{tc.fmt_price(b['target']) if b.get('target') else '-':>10} {fmt(b.get('expected_r')):>6} "
                     f"{fmt(b.get('target_first_pct'), 0):>5} {fmt(b.get('stop_first_pct'), 0):>5} "
                     f"{qty(b.get('shares')):>9}  {tail}")
        for s in r["stops"]:
            lines.append(f"{'':<10} {'':<5} {'':<7} {'':<18} {'':<4} {'':>8} {s['name']:<6} "
                         f"{tc.fmt_price(s['stop']) if s.get('stop') else '-':>10} "
                         f"{tc.fmt_price(s['target']) if s.get('target') else '-':>10} {fmt(s.get('expected_r')):>6} "
                         f"{fmt(s.get('target_first_pct'), 0):>5} {fmt(s.get('stop_first_pct'), 0):>5} "
                         f"{qty(s.get('shares')):>9}  {s.get('verdict') or s.get('error') or ''}")
    passed = [r["symbol"] for r in out["ideas"] if r["passes_numbers"]]
    lines.append(f"Numbers pass: {', '.join(passed) if passed else 'none'}. The news rule is still to check; "
                 "a pass trades through paper_autotrade.py --plan saved after its plan is copied into the desk.")
    lines.append(tc.DISCLAIMER if hasattr(tc, "DISCLAIMER") else
                 "Not financial advice. Paper trading only - nothing here can place a real order.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = tc.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbol", nargs="?", metavar="SYMBOL", help="one idea (with --setup); or use --ideas FILE")
    p.add_argument("--home", required=True, metavar="DIR", help="the desk whose settings.json the throwaway homes copy")
    p.add_argument("--out", required=True, metavar="DIR", help="where the throwaway homes go: DIR/<SYM>-<stop>")
    p.add_argument("--ideas", metavar="FILE", help="JSON list of {symbol, setup, side, level, entry, note}")
    p.add_argument("--setup", type=str.lower, help="entry setup for SYMBOL: orb, vwap, level, pdh, pdl")
    p.add_argument("--side", type=str.lower, choices=("long", "short"), default="long")
    p.add_argument("--level", type=float, metavar="X", help="price for --setup level")
    p.add_argument("--entry", type=float, metavar="E", help="entry to replay when there is no trigger yet")
    p.add_argument("--note", metavar="TEXT", help="kept with the result")
    p.add_argument("--cutoff", metavar="HH:MM", help="entry.py cutoff (default 15:55 ET stocks, 23:55 UTC crypto)")
    p.add_argument("--rr", type=float, default=DEFAULT_RR, metavar="R", help=f"target in R (default {DEFAULT_RR:g})")
    p.add_argument("--min-r", type=float, default=DEFAULT_MIN_R, metavar="R",
                   help=f"the best stop's expected R must reach this (default {DEFAULT_MIN_R:+.2f})")
    p.add_argument("--live-minutes", type=float, default=DEFAULT_LIVE_MINUTES, metavar="M",
                   help=f"a trigger older than this is a watch, not an idea (default {DEFAULT_LIVE_MINUTES})")
    p.add_argument("--min-dollar-volume", type=float, metavar="USD",
                   help="liquidity floor (default $20M stocks, $5M crypto; 0 skips the scanner call)")
    p.add_argument("--stops", default=",".join(STOPS), metavar="LIST", help=f"stops to run (default {','.join(STOPS)})")
    p.add_argument("--candidates", metavar="FILE", help="merge one dashboard candidate row per idea into this file")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
                   help=f"ideas checked at once (default {DEFAULT_WORKERS}; 1 = one after another)")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return p


def main():
    args = build_parser().parse_args()
    try:
        ideas = load_ideas(args)
        home = auto.desk_home(args.home)
        args.stops = [s.strip() for s in args.stops.split(",") if s.strip()]
        bad = [s for s in args.stops if s not in STOPS]
        if bad or not args.stops:
            raise ValueError(f"--stops: unknown {', '.join(bad) or 'empty'} (choose from {', '.join(STOPS)})")
        if args.rr <= 0 or args.live_minutes <= 0:
            raise ValueError("--rr and --live-minutes must be positive")
        if args.min_dollar_volume is not None and args.min_dollar_volume < 0:
            raise ValueError("--min-dollar-volume must be 0 or more")
        if args.workers < 1:
            raise ValueError("--workers must be at least 1")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    args.out = Path(args.out).expanduser().resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    settings = home / "settings.json"

    def floor_for(market):
        return MIN_DOLLAR_VOLUME[market] if args.min_dollar_volume is None else args.min_dollar_volume

    with ThreadPoolExecutor(max_workers=min(args.workers, len(ideas))) as pool:
        rows = list(pool.map(lambda i: check_idea(i, args, settings, floor_for), ideas))
    out = {"home": str(home), "out": str(args.out), "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "rr": args.rr, "min_r": args.min_r, "live_minutes": args.live_minutes, "stops": args.stops, "workers": args.workers,
           "ideas": rows, "passed_numbers": [r["symbol"] for r in rows if r["passes_numbers"]],
           "news_checked": False, "mode": "paper", "real_money": False}
    if args.candidates:
        merge_candidates(args.candidates, [candidate_row(r) for r in rows])
        out["candidates"] = args.candidates
    tc.emit(out, args.json, render)
    if rows and all(r.get("data_error") for r in rows):
        return EXIT_DATA
    return 0


if __name__ == "__main__":
    tc.run(main)
