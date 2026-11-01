#!/usr/bin/env python3
"""Paper auto-trader: place AND confirm paper trades under the owner's standing order. PAPER ONLY.

The owner's standing order (2026-10-31) replaces the per-ticket "yes" for paper trades, so this
tool confirms the tickets it makes. It fills nothing itself and skips no check: it runs the skills'
own CLIs as subprocesses in the --home desk (exit_plan.py, risk.py, broker.py ticket, broker.py
confirm --yes), so the broker, the risk skill and the daily budget check every trade exactly as
usual - the levels, the budget at the ticket and again at the fill, previews outside the session,
the time stop and the 10-second gap between a ticket and its yes. Nothing here can place a real
order.

Per idea, in order (after one broker.py sync when the ledger has open trades or working orders):
  1. a crypto short is skipped (spot crypto is long only);
  2. a symbol with an open trade, or a pending or working ticket, in the desk's ledger is skipped;
  3. --plan default builds the desk's default plan: exit_plan.py SYMBOL --entry E --side S --save
     (1.5 x ATR stop, a target of --rr R, default 2R, the default time stop) with the idea's setup
     label, then risk.py size --save; --plan saved trades the plan the desk saved in --home
     (risk.py size --save only when it carries no sizing for its levels);
  4. broker.py ticket: a preview (market closed, past the time stop) or a budget block is a skip.
Then, once every ticket of the batch exists: one wait until the newest is 10 seconds old (a replay
with $DAYTRADER_NOW does not sleep: its confirmations run 11 seconds later on the replay clock),
broker.py confirm --yes for each, and one desk-log line per confirmed ticket (desk_log.jsonl, as
desk_snapshot.py --event writes it). A ticket the confirmation refuses is cancelled. A replay batch
that mixes stocks and crypto needs a $DAYTRADER_NOW with Z or an offset (naive is ET for one, UTC
for the other).

  python3 tools/paper_autotrade.py --home ~/.day-trader-shadow --standing-order "owner 2026-10-31" \\
      --plan default BTC-USDT --side long --entry 101250 --setup orb
  python3 tools/paper_autotrade.py --home ~/.day-trader --standing-order "owner 2026-10-31" \\
      --plan saved --ideas passed.json --json

--ideas FILE: a JSON list of {"symbol", "side", "entry" (required for --plan default), "setup", "note"}.
Exit codes: 0 every idea processed (skips and per-idea errors are in the results), 1 bad input or
state (nothing traded), 2 a data source was down for every idea.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(ROOT / "shared"))
import trader_common as tc  # noqa: E402

sys.path.insert(0, str(TOOLS))
import desk_snapshot  # noqa: E402  (its --event line is the desk log's one format)

SKILLS = ROOT / "skills"
EXIT_PLAN = SKILLS / "exit" / "scripts" / "exit_plan.py"
RISK = SKILLS / "risk" / "scripts" / "risk.py"
BROKER = SKILLS / "broker" / "scripts" / "broker.py"
MIN_CONFIRM_SECONDS = 10              # broker.py refuses a yes to a ticket younger than this
REPLAY_STEP = MIN_CONFIRM_SECONDS + 1  # a replay confirms this many seconds after its tickets
WAIT_MARGIN = 1.0                     # ticket stamps are whole seconds
CALL_TIMEOUT = 600                    # seconds per skill command (data fetches retry with back-off)
EXIT_DATA, EXIT_BLOCKED = 2, 3        # the skills' exit codes: data source down, refused by the budget
DEFAULT_RR = 2.0                      # --plan default's target in R unless --rr says otherwise
SPOT_LONG_ONLY = "crypto spot is long only"
IN_A_TRADE = "already in a trade or order"
STOCK = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")   # the tickers broker.py accepts


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def price(value, what):
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a positive price, got {value!r}")
    try:
        v = float(str(value).replace(",", "").strip())
    except ValueError:
        raise ValueError(f"{what} must be a positive price, got {value!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{what} must be a positive price, got {value!r}")
    return v


def label(value, what):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text, got {value!r}")
    return " ".join(value.split()) or None


def parse_idea(n, row, plan_mode):
    """One idea, checked before anything trades: canonical symbol, side, entry, setup, note."""
    where = f"idea {n}"
    if not isinstance(row, dict):
        raise ValueError(f"{where} is not an object with symbol and side")
    sym = str(row.get("symbol") or "").strip().upper()
    if not (tc.is_crypto_symbol(sym) or STOCK.fullmatch(sym)):
        raise ValueError(f"{where}: not a ticker or a crypto pair: {row.get('symbol')!r}")
    sym = tc.canonical_symbol(sym)              # BTCUSDT and BTC-USDT are one symbol
    side = str(row.get("side") or "").strip().lower()
    if side not in ("long", "short"):
        raise ValueError(f"{where} ({sym}): side must be long or short, got {row.get('side')!r}")
    entry = None if row.get("entry") is None else price(row["entry"], f"{where} ({sym}) entry")
    if entry is None and plan_mode == "default":
        raise ValueError(f"{where} ({sym}): --plan default needs the idea's entry price")
    return {"symbol": sym, "side": side, "entry": entry, "setup": label(row.get("setup"), f"{where} setup"),
            "note": label(row.get("note"), f"{where} note")}


def load_ideas(args):
    if args.ideas and args.symbol:
        raise ValueError("give --ideas FILE or one SYMBOL with --side, not both")
    if args.ideas:
        for flag in ("side", "entry", "setup", "note"):
            if getattr(args, flag) is not None:
                raise ValueError(f"--{flag} belongs to a single SYMBOL idea; put it in the --ideas file")
        path = Path(args.ideas)
        if not path.is_file():
            raise ValueError(f"--ideas {path} does not exist")
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"--ideas {path} is not valid JSON ({e})") from None
        if not isinstance(rows, list):
            raise ValueError(f"--ideas {path} must hold a JSON list of ideas")
    elif args.symbol:
        if args.side is None:
            raise ValueError(f"{args.symbol} needs --side long or --side short")
        rows = [{"symbol": args.symbol, "side": args.side, "entry": args.entry, "setup": args.setup,
                 "note": args.note}]
    else:
        raise ValueError("no idea given: SYMBOL --side long|short [--entry E], or --ideas FILE")
    return [parse_idea(n, row, args.plan) for n, row in enumerate(rows, 1)]


def desk_home(value):
    """--home must already be a desk: a directory holding settings.json (never created here)."""
    home = Path(value).expanduser()
    if not home.is_dir():
        raise ValueError(f"--home {value} is not a directory - point it at a desk's DAYTRADER_HOME")
    if not (home / "settings.json").is_file():
        raise ValueError(f"--home {value} holds no settings.json - set the desk up first (risk.py config / "
                         f"budget.py set with DAYTRADER_HOME={value})")
    return home.resolve()


def replay_time(stamp):
    """$DAYTRADER_NOW as tc.parse_et reads it, before the zone is applied (naive stays naive)."""
    text = stamp.strip()
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise ValueError(f"$DAYTRADER_NOW {stamp!r} is not an ISO time") from None


def replay_after(stamp, seconds):
    """$DAYTRADER_NOW moved `seconds` later in its own form: a naive stamp stays naive (each skill reads
    it on its market's clock, ET or UTC); with Z or an offset it stays that instant's offset."""
    return (replay_time(stamp) + timedelta(seconds=seconds)).isoformat()


def check_replay(stamp, ideas):
    """A replay must be one instant for the whole batch. A naive $DAYTRADER_NOW is ET for a stock and
    UTC for a pair - hours apart - so a batch with both would expire one market's tickets on the
    other's clock."""
    if replay_time(stamp).tzinfo is None and len({tc.market_for(i["symbol"]).key for i in ideas}) > 1:
        raise ValueError(f"$DAYTRADER_NOW {stamp!r} has no zone, and this batch mixes stocks and crypto (a naive "
                         "time is ET for stocks but UTC for crypto) - give it a Z or an offset")


# ---------------------------------------------------------------------------
# The skills' CLIs
# ---------------------------------------------------------------------------

def call(script, *args):
    """Run a skill command in the desk ($DAYTRADER_HOME, and $DAYTRADER_NOW in a replay, come from
    os.environ). Returns (exit code, its --json output - {} without one -, its last stderr line)."""
    what = f"{script.name} {' '.join(args[:2])}"
    try:
        p = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                           env=dict(os.environ), timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return EXIT_DATA, {}, f"data error: {what} gave no answer in {CALL_TIMEOUT} s"
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        out = None
    lines = [ln.strip() for ln in p.stderr.splitlines() if ln.strip()]
    return p.returncode, out if isinstance(out, dict) else {}, lines[-1] if lines else f"{what} exited {p.returncode}"


def price_arg(x):
    return format(Decimal(repr(float(x))), "f")      # 0.00001234, not 1.234e-05


def new_result(idea):
    return {"symbol": idea["symbol"], "side": idea["side"], "setup": idea["setup"], "status": None,
            "reason": None, "ticket": None, "trade": None, "qty": None, "entry": idea["entry"], "stop": None,
            "target": None, "fill_price": None, "detail": None, "note": idea["note"]}


def take_levels(res, row):
    """Size, levels and setup of a plan, ticket or previewed order."""
    for key in ("qty", "entry", "stop", "target"):
        if row.get(key) is not None:
            res[key] = row[key]
    if row.get("setup"):
        res["setup"] = row["setup"]


def skip(res, reason, detail=None):
    res.update(status="skipped", reason=reason, detail=detail)
    return None


def fail(res, code, err):
    reason = err[len("error: "):] if err.startswith("error: ") else err
    if code == EXIT_DATA and not reason.startswith("data error"):
        reason = f"data error: {reason}"
    res.update(status="error", reason=reason)
    return None


def busy(sym):
    """An open trade, or a pending (not yet expired) or working ticket, in `sym` on this desk."""
    led = tc.load_ledger()

    def mine(row):
        return isinstance(row, dict) and tc.canonical_symbol(str(row.get("symbol") or "")) == sym

    with tc.market_scope(sym):
        now_ts = tc.now_et().timestamp()
        if any(mine(t) and t.get("status") == "open" for t in led["trades"]):
            return True
        for k in filter(mine, led["tickets"]):
            if k.get("status") == "working":
                return True
            if k.get("status") == "pending":
                try:
                    if tc.parse_et(str(k.get("expires_at"))).timestamp() > now_ts:
                        return True
                except ValueError:
                    pass                    # no usable expiry: the broker reads it as expired
    return False


def sync_first(ideas):
    """Bring the ledger up to date (stops, targets, time stops, working fills) before reading it -
    best effort, and only when it has something open. --market is the batch's: a naive replay clock
    is read on that market's clock, as by the batch's own commands."""
    led = tc.load_ledger()
    if not (any(isinstance(t, dict) and t.get("status") == "open" for t in led["trades"])
            or any(isinstance(k, dict) and k.get("status") == "working" for k in led["tickets"])):
        return []
    view = "crypto" if ideas and all(tc.is_crypto_symbol(i["symbol"]) for i in ideas) else "us"
    code, _, err = call(BROKER, "sync", "--market", view, "--json")
    return [] if code == 0 else [f"sync before the checks failed ({err}); the ledger was read as it was"]


def size(sym, res):
    code, out, err = call(RISK, "size", "--symbol", sym, "--save", "--json")
    if code == 1 and out.get("save_error"):          # sized at 0: the desk's risk rules say no
        res["qty"] = out.get("shares")
        return skip(res, ((out.get("warnings") or []) + [out["save_error"]])[0])
    if code != 0:
        return fail(res, code, err)
    return True


def default_plan(idea, res, rr=DEFAULT_RR):
    sym = idea["symbol"]
    code, out, err = call(EXIT_PLAN, sym, "--entry", price_arg(idea["entry"]), "--side", idea["side"],
                          "--rr", f"{rr:g}", "--save", "--json")
    if code != 0 or not out.get("saved"):
        return fail(res, code, err)
    take_levels(res, out)
    with tc.market_scope(sym):              # the plan is the idea's: its setup label, or none
        plan = tc.load_plan(sym) or {}
        tc.save_plan({"symbol": sym, "side": plan.get("side"), "entry": plan.get("entry"), "stop": plan.get("stop"),
                      "setup": idea["setup"]})
    return size(sym, res)


def sized(plan):
    """The saved plan carries risk.py's sizing for its own side, entry and stop."""
    s = plan.get("sizing")
    try:
        return (isinstance(s, dict) and s.get("side", plan.get("side")) == plan.get("side")
                and math.isclose(float(s["entry"]), float(plan["entry"]), rel_tol=1e-9)
                and math.isclose(float(s["stop"]), float(plan["stop"]), rel_tol=1e-9)
                and float(s.get("shares") or 0) > 0)
    except (KeyError, TypeError, ValueError):
        return False


def saved_plan(idea, res):
    sym = idea["symbol"]
    with tc.market_scope(sym):
        plan = tc.load_plan(sym)
    if not isinstance(plan, dict) or plan.get("entry") is None or plan.get("stop") is None:
        return fail(res, 1, f"no saved {sym} plan in {tc.home() / 'plans'} (--plan saved trades the desk's plan)")
    if plan.get("side") not in (None, idea["side"]):
        return fail(res, 1, f"the saved {sym} plan is a {plan['side']}, the idea a {idea['side']}")
    take_levels(res, plan)
    return True if sized(plan) else size(sym, res)


def prepare(idea, res, plan_mode, rr=DEFAULT_RR):
    """Steps 1-4: the pending ticket to confirm, or None (`res` says why not)."""
    sym, side = idea["symbol"], idea["side"]
    if side == "short" and tc.is_crypto_symbol(sym):
        return skip(res, SPOT_LONG_ONLY)
    if busy(sym):
        return skip(res, IN_A_TRADE)
    if not (default_plan(idea, res, rr) if plan_mode == "default" else saved_plan(idea, res)):
        return None
    args = ["ticket", "--symbol", sym, "--side", side, "--json"]
    if idea["setup"]:
        args.append(f"--setup={idea['setup']}")
    code, out, err = call(BROKER, *args)
    if code == 0 and out.get("status") == "pending" and isinstance(out.get("ticket"), dict):
        res["ticket"] = out["ticket"].get("id")
        take_levels(res, out["ticket"])
        return out["ticket"]
    take_levels(res, out.get("order") or {})
    if code == 0 and out.get("preview"):
        return skip(res, out.get("market_closed") or "the broker made a preview, not a ticket")
    if code == EXIT_BLOCKED and out.get("blocked"):
        return skip(res, "budget", "; ".join(out.get("reasons") or []) or None)
    return fail(res, code, err if code else "broker.py ticket made no ticket")


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

def created_ts(k):
    with tc.market_scope(str(k.get("symbol") or "")):
        try:
            return tc.parse_et(str(k.get("created_at"))).timestamp()
        except ValueError:
            return time.time()


def confirm_wait(created, now):
    """Seconds until the newest ticket is MIN_CONFIRM_SECONDS old (0 when every one already is; never
    more than a fresh ticket needs, whatever a stamp says)."""
    full = MIN_CONFIRM_SECONDS + WAIT_MARGIN
    return min(full, max(0.0, max(created) + full - now))


@contextmanager
def confirm_clock(tickets):
    """The clock the confirmations run on. Real clock: ONE sleep for the whole batch, until its newest
    ticket is MIN_CONFIRM_SECONDS old. Replay ($DAYTRADER_NOW): no sleep - the confirm phase runs
    REPLAY_STEP seconds after the tickets on the replay clock (put back afterwards)."""
    replay = os.environ.get("DAYTRADER_NOW")
    if replay:
        os.environ["DAYTRADER_NOW"] = replay_after(replay, REPLAY_STEP)
        try:
            yield
        finally:
            os.environ["DAYTRADER_NOW"] = replay
        return
    wait = confirm_wait([created_ts(k) for k in tickets], time.time())
    if wait > 0:
        time.sleep(wait)
    yield


def ledger_ticket(tid):
    try:
        return next((k for k in tc.load_ledger()["tickets"] if isinstance(k, dict) and k.get("id") == tid), {})
    except tc.StateError:
        return {}


def confirm(res, k, order):
    tid = k["id"]
    code, out, err = call(BROKER, "confirm", tid, "--yes", "--json")
    if code == 0 and out.get("confirmed"):
        trade = out.get("trade") if isinstance(out.get("trade"), dict) else {}
        res.update(status=out.get("status"), trade=trade.get("id"), fill_price=out.get("fill_price"))
        take_levels(res, out.get("ticket") or {})
        return log_trade(res, order)
    led = ledger_ticket(tid)              # a confirmation that died after writing the ledger still counts
    if led.get("status") in ("confirmed", "working"):
        res.update(status="filled" if led["status"] == "confirmed" else "working", trade=led.get("trade_id"),
                   fill_price=led.get("fill_price"))
        return log_trade(res, order)
    if code == EXIT_BLOCKED and out.get("blocked"):
        skip(res, "budget", "; ".join(out.get("reasons") or []) or None)
    else:
        fail(res, code, err)
    if led.get("status") == "pending":    # refused: cancel it, so no ticket lingers waiting for a yes
        done = call(BROKER, "cancel", tid, "--json")[0] == 0
        res["detail"] = "; ".join(x for x in (res.get("detail"), f"{tid} cancelled" if done else
                                              f"{tid} could not be cancelled - it expires unconfirmed") if x)


def log_trade(res, order):
    """One desk-log line per confirmed ticket, written exactly as desk_snapshot.py --event writes it."""
    fp = tc.fmt_price
    status = (f"FILLED @ {fp(res['fill_price'])} ({res['trade']})" if res["status"] == "filled"
              else f"WORKING (limit {fp(res['entry'])}, day order)")
    parts = [f"{res['side'].upper()} {tc.fmt_qty(res['qty'])}",
             f"entry {fp(res['entry'])} / stop {fp(res['stop'])} / target {fp(res['target'])}", status,
             f"ticket {res['ticket']}"]
    if res.get("setup"):
        parts.append(f"setup {res['setup']}")
    parts.append(f"paper, standing order: {order}")
    try:
        with tc.market_scope(res["symbol"]):    # a naive replay clock is read on the symbol's market clock
            desk_snapshot.append_log(f"Auto-traded {res['symbol']}", " · ".join(parts))
    except OSError as e:                    # the trade stands; only its log line is missing
        res["detail"] = f"desk log not written: {e}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def result_line(r):
    fp = tc.fmt_price
    line = f"{str(r['status']).upper()} {r['symbol']} {r['side'].upper()}"
    if r["qty"] is not None and r["stop"] is not None:
        line += (f" {tc.fmt_qty(r['qty'])} LIMIT {fp(r['entry'])} · stop {fp(r['stop'])} · "
                 f"target {fp(r['target'])}")
    if r["status"] == "filled":
        return f"{line} · filled @ {fp(r['fill_price'])} · {r['ticket']} -> {r['trade']}"
    if r["status"] == "working":
        return f"{line} · working (day order) · {r['ticket']}"
    return f"{line} · {r['reason']}" + (f": {r['detail']}" if r.get("detail") else "")


def render(out):
    lines = [f"PAPER AUTOTRADE · {out['home']} · plan {out['plan_mode']} · standing order: "
             f"{out['standing_order']} · REAL MONEY: OFF"]
    lines += [result_line(r) for r in out["results"]]
    lines += [f"  - {n}" for n in out["notes"]]
    return "\n".join(lines)


def build_parser():
    p = tc.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.allow_abbrev = False
    p.add_argument("symbol", nargs="?", metavar="SYMBOL",
                   help="one idea: a ticker or crypto pair (with --side; --entry for --plan default)")
    p.add_argument("--home", required=True, metavar="DIR",
                   help="the desk to trade in (its DAYTRADER_HOME; must hold settings.json)")
    p.add_argument("--standing-order", required=True, metavar="TEXT",
                   help="the owner's standing order this run trades under (echoed in the output and the desk log)")
    p.add_argument("--plan", required=True, choices=("default", "saved"),
                   help="default: build the desk's default plan per idea; saved: trade the plan saved in --home")
    p.add_argument("--ideas", metavar="FILE", help="JSON list of {symbol, side, entry, setup, note}")
    p.add_argument("--side", type=str.lower, choices=("long", "short"))
    p.add_argument("--entry", metavar="E", help="the idea's entry price (required for --plan default)")
    p.add_argument("--setup", metavar="LABEL", help="setup label for the plan and the journal, e.g. orb")
    p.add_argument("--rr", type=float, metavar="R",
                   help=f"--plan default only: target = entry +/- R x risk (default {DEFAULT_RR:g}); "
                        "a saved plan keeps its own target")
    p.add_argument("--note", metavar="TEXT", help="kept with the result")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return p


def target_rr(args):
    """--rr for --plan default, checked before anything trades."""
    if args.rr is None:
        return DEFAULT_RR
    if args.plan != "default":
        raise ValueError("--rr is for --plan default (a saved plan keeps its own target)")
    if not (math.isfinite(args.rr) and 0 < args.rr <= 50):
        raise ValueError(f"--rr must be above 0 and at most 50, not {args.rr:g}")
    return args.rr


def main():
    args = build_parser().parse_args()
    order = " ".join((args.standing_order or "").split())
    if not order:
        raise ValueError("--standing-order is empty - name the owner's standing order this run trades under")
    rr = target_rr(args)
    home = desk_home(args.home)
    ideas = load_ideas(args)
    if os.environ.get("DAYTRADER_NOW"):
        check_replay(os.environ["DAYTRADER_NOW"], ideas)         # a replay clock it cannot use: say so now
    os.environ["DAYTRADER_HOME"] = str(home)                     # this process and every skill: one desk
    tc.load_settings()
    notes = sync_first(ideas)                                    # a broken ledger stops here, before a trade
    results, tickets = [], []
    for idea in ideas:
        res = new_result(idea)
        results.append(res)
        try:
            k = prepare(idea, res, args.plan, rr)
        except (ValueError, tc.StateError, OSError) as e:
            k = fail(res, 1, str(e))
        if k:
            tickets.append((res, k))
    if tickets:
        with confirm_clock([k for _, k in tickets]):
            for res, k in tickets:
                try:
                    confirm(res, k, order)
                except (ValueError, tc.StateError, OSError) as e:
                    fail(res, 1, str(e))
    out = {"home": str(home), "standing_order": order, "plan_mode": args.plan, "mode": "paper",
           "real_money": False, "results": results, "notes": notes}
    tc.emit(out, args.json, render)
    down = [r for r in results if r["status"] == "error" and r["reason"].startswith("data error")]
    if results and len(down) == len(results):
        print(down[0]["reason"], file=sys.stderr)
        return EXIT_DATA
    return 0


if __name__ == "__main__":
    tc.run(main)
