#!/usr/bin/env python3
"""Snapshot the paper desk into one JSON file, e.g. for a live dashboard of the desk.

Reads the desk's state in $DAYTRADER_HOME (settings, ledger, plans, the desk log) plus recent
5-minute candles of a focus symbol, and writes one JSON array of rows tagged by `kind`: account
(today's numbers, then the all-time results: all_trades, all_wins, all_losses, all_pnl, all_fees,
all_win_rate, all_avg_r, all_profit_factor, open_positions, first_trade_at), plan, ticket (working
orders, and pending tickets until they expire), position, trade, equity (account "desk": a start
row, then the cumulative net P&L after each closed trade), scanmeta, scan, candidate, candle, log.

Optional inputs: scanner --json output (--scan, repeatable), the original day-trader-skill
scanner's trader/watchlist.json (--original-watchlist: a daily scan's hits most traded first, an
intraday ORB sweep's furthest over the range first) and a list of analysed candidates
(--candidates). --shadow-home DIR (must exist) also reads a second desk, e.g. a shadow desk that
takes every idea: shadow_account, shadow_position and shadow_trade rows (ids "shadow-...") and its
equity rows (account "shadow"); everything else still comes from $DAYTRADER_HOME. `--event "..."
--detail "..."` first appends a line to the desk log (desk_log.jsonl). Read-only otherwise; paper
only.

  python3 tools/desk_snapshot.py --symbol BTC-USDT --out desk.json
  python3 tools/desk_snapshot.py --symbol TSLA --out desk.json --scan us.json --event "Scanned the market"
  python3 tools/desk_snapshot.py --symbol BTC-USDT --out desk.json --shadow-home ~/.day-trader-shadow
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
import trader_common as tc  # noqa: E402

LOG = "desk_log.jsonl"


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_of(stamp):
    """Unix seconds of an ISO stamp (naive = the active market's wall clock), or None."""
    try:
        return tc.parse_et(str(stamp)).timestamp() if stamp else None
    except (ValueError, OverflowError):
        return None


def to_utc(stamp):
    if not stamp:
        return None
    ts = ts_of(stamp)
    return str(stamp) if ts is None else iso(ts)


def num(x, nd=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if nd is not None else v


def append_log(event, detail):
    p = tc.state_path(LOG)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": iso(tc.now_et().timestamp()), "event": event, "detail": detail or ""}) + "\n")


def read_log(limit=60):
    p = tc.state_path(LOG)
    if not p.exists():
        return []
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[-limit:]


def desk_status(settings, check, summary):
    s, used = settings, check["loss_budget_used"]
    if (not check["ok"] or used >= s["daily_max_loss"] - 1e-9 or summary["trades_today"] >= s["max_trades_per_day"]
            or summary["loss_streak"] >= s["max_consecutive_losses"]):
        return "STOP TRADING"
    if used >= 0.75 * s["daily_max_loss"]:
        return "CAUTION"
    return "OPEN FOR TRADING"


def load_desk(home):
    """Settings, ledger and today's summary of the desk in `home`, read with tc's own loaders by
    pointing $DAYTRADER_HOME there meanwhile (put back afterwards, also on error)."""
    saved = os.environ.get("DAYTRADER_HOME")
    os.environ["DAYTRADER_HOME"] = str(home)
    try:
        settings, ledger = tc.load_settings(), tc.load_ledger()
        return settings, ledger, tc.day_summary(ledger)
    finally:
        if saved is None:
            os.environ.pop("DAYTRADER_HOME", None)
        else:
            os.environ["DAYTRADER_HOME"] = saved


def all_time(ledger):
    """All-time results: the closed trades of any day, plus the open positions and the first trade."""
    trades = ledger.get("trades", [])
    closed = [t for t in trades if t.get("status") == "closed"]
    pnls = [num(t.get("pnl")) or 0.0 for t in closed]
    rs = [r for r in (num(t.get("r_multiple")) for t in closed) if r is not None]
    wins, won, lost = sum(1 for p in pnls if p > 0), sum(p for p in pnls if p > 0), sum(p for p in pnls if p < 0)
    opened = [ts for ts in (ts_of(t.get("opened_at")) for t in trades) if ts is not None]
    return {
        "all_trades": len(closed), "all_wins": wins, "all_losses": sum(1 for p in pnls if p < 0),
        "all_pnl": num(sum(pnls), 2), "all_fees": num(sum(num(t.get("fees")) or 0.0 for t in closed), 2),
        "all_win_rate": num(wins / len(closed) * 100, 1) if closed else None,
        "all_avg_r": num(sum(rs) / len(rs), 2) if rs else None,
        "all_profit_factor": num(won / -lost, 2) if lost < 0 else None,   # 0 when there are losses but no wins
        "open_positions": sum(1 for t in trades if t.get("status") == "open"),
        "first_trade_at": iso(min(opened)) if opened else None,
    }


def equity_rows(account, ledger, start_at):
    """One desk's equity curve: a start row (n 0 at `start_at`), then the cumulative net P&L after
    each closed trade in closed_at order (by the instant, whatever the stamp's offset)."""
    def closed_key(t):   # a trade without a readable closed_at goes last
        ts = ts_of(t.get("closed_at"))
        return (ts is None, ts or 0.0)

    rows = [{"kind": "equity", "id": f"equity-{account}-0", "account": account, "n": 0, "at": start_at,
             "symbol": None, "pnl": 0.0, "cum_pnl": 0.0}]
    cum = 0.0
    closed = sorted((t for t in ledger.get("trades", []) if t.get("status") == "closed"), key=closed_key)
    for n, t in enumerate(closed, 1):
        pnl = num(t.get("pnl")) or 0.0
        cum += pnl
        rows.append({"kind": "equity", "id": f"equity-{account}-{n}", "account": account, "n": n,
                     "at": to_utc(t.get("closed_at")), "symbol": str(t.get("symbol", "")).upper(),
                     "pnl": num(pnl, 2), "cum_pnl": num(cum, 2)})
    return rows


def trade_rows(ledger, last_of, position="position", trade="trade", prefix=""):
    """A `position` row per open trade (marked at last_of(symbol)) and a `trade` row per closed one.
    `prefix` goes in front of every id, so a second desk's rows never collide with the desk's."""
    rows = []
    for t in ledger.get("trades", []):
        tsym = str(t.get("symbol", "")).upper()
        base = {"id": f"{prefix}{t.get('id')}" if prefix else t.get("id"), "symbol": tsym, "side": t.get("side"),
                "qty": num(t.get("qty")), "entry_price": num(t.get("entry_price"), 2), "stop": num(t.get("stop"), 2),
                "target": num(t.get("target"), 2), "opened_at": to_utc(t.get("opened_at"))}
        if t.get("status") == "open":
            px = last_of(tsym)
            unreal = tc.trade_pnl(t, px) if px else None
            rows.append(dict(base, kind=position, last=num(px, 2), unrealized_pnl=num(unreal, 2)))
        elif t.get("status") == "closed":
            rows.append(dict(base, kind=trade, exit_price=num(t.get("exit_price"), 2),
                             exit_reason=t.get("exit_reason"), gross_pnl=num(t.get("gross_pnl", t.get("pnl")), 2),
                             fees=num(t.get("fees") or 0, 2), pnl=num(t.get("pnl"), 2),
                             r_multiple=num(t.get("r_multiple"), 2), closed_at=to_utc(t.get("closed_at"))))
    return rows


def orb_order(h):
    """An ORB sweep's hits: furthest over the opening range first, then by symbol (no pct_over last)."""
    pct = num(h.get("pct_over"))
    return (pct is None, -(pct or 0.0), str(h.get("symbol", "")))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="BTC-USDT")
    ap.add_argument("--out", required=True)
    ap.add_argument("--hours", type=float, default=12.0, help="candle window")
    ap.add_argument("--scan", action="append", default=[], help="scanner --json output to show (repeatable)")
    ap.add_argument("--candidates", help="JSON list of analysed candidates to show")
    ap.add_argument("--original-watchlist", help="trader/watchlist.json from the original day-trader-skill scanner")
    ap.add_argument("--shadow-home", metavar="DIR",
                    help="a second desk's $DAYTRADER_HOME (e.g. ~/.day-trader-shadow) to show beside this one")
    ap.add_argument("--top-scan", type=int, default=15)
    ap.add_argument("--event")
    ap.add_argument("--detail", default="")
    args = ap.parse_args()

    for flag, paths in (("--scan", args.scan), ("--original-watchlist", [args.original_watchlist]),
                        ("--candidates", [args.candidates])):
        for path in paths:
            if path and not Path(path).exists():   # a missing input would silently empty its table
                raise ValueError(f"{flag} {path} does not exist")
    shadow = Path(args.shadow_home).expanduser() if args.shadow_home else None
    if shadow is not None:   # tc.home() would quietly create an empty desk at a mistyped path
        if not shadow.is_dir():
            raise ValueError(f"--shadow-home {args.shadow_home} "
                             + ("is not a directory" if shadow.exists() else "does not exist"))
        if shadow.resolve() == tc.home().resolve():   # the comparison would show the desk against itself
            raise ValueError(f"--shadow-home {args.shadow_home} is the desk's own $DAYTRADER_HOME")
    sym = args.symbol.upper()
    tc.use_market_for(sym)
    if args.event:
        append_log(args.event, args.detail)

    settings = tc.load_settings()
    ledger = tc.load_ledger()
    summary = tc.day_summary(ledger)
    check = tc.budget_check(0, settings=settings, ledger=ledger)
    now_ts = tc.now_et().timestamp()
    as_of = iso(now_ts)
    shadow_book = load_desk(shadow) if shadow is not None else None

    bars, _meta = tc.fetch_bars(sym, "5m", "1d")
    keep = int(args.hours * 12)
    bars = bars[-keep:]
    try:
        last = tc.quote(sym)["price"]
    except tc.FetchError:
        last = bars[-1].close if bars else None

    rows = []
    desk = all_time(ledger)
    rows.append({
        "kind": "account", "id": "account", "as_of": as_of, "day": summary["date"],
        "exchange": tc.exchange_name() if tc.is_crypto_symbol(sym) else "Yahoo Finance", "symbol": sym,
        "status": desk_status(settings, check, summary), "last_price": num(last, 2),
        "account_size": num(settings["account_size"], 2),
        "risk_per_trade": num(settings["account_size"] * settings["risk_per_trade_pct"] / 100, 2),
        "fee_pct": num(tc.fee_pct(settings=settings), 4),
        "realized_pnl": num(summary["realized_pnl"], 2), "open_risk": num(summary["open_risk"], 2),
        "loss_budget_used": num(check["loss_budget_used"], 2), "loss_budget_left": num(check["loss_budget_left"], 2),
        "daily_max_loss": num(settings["daily_max_loss"], 2), "trades_today": summary["trades_today"],
        "max_trades": settings["max_trades_per_day"], "wins": summary["wins"], "losses": summary["losses"],
        **desk,
    })

    plan = tc.load_plan(sym)
    if plan and plan.get("entry") is not None:
        tm = plan.get("time_machine") or {}
        disp = tm.get("display_pct") or {}
        fees = tm.get("fees") or {}
        rows.append({
            "kind": "plan", "id": f"plan-{sym}", "symbol": sym, "side": plan.get("side"),
            "entry": num(plan.get("entry"), 2), "stop": num(plan.get("stop"), 2), "target": num(plan.get("target"), 2),
            "rr": num(tc.reward_risk(plan["side"], plan["entry"], plan["stop"], plan["target"]), 2) if plan.get("target") else None,
            "setup": plan.get("setup"), "updated_at": to_utc(plan.get("updated_at")),
            "tm_target_first": num(disp.get("target_first", tm.get("target_first_pct")), 1),
            "tm_stop_first": num(disp.get("stop_first", tm.get("stop_first_pct")), 1),
            "tm_open": num(disp.get("open_at_close", tm.get("open_at_close_pct")), 1),
            "tm_verdict": tm.get("verdict"),
            "tm_expected_r": num(tm.get("expected_r"), 2),
            "tm_expected_r_after_fees": num(fees.get("expected_r_after_fees", tm.get("expected_r")), 2),
            "tm_paths": tm.get("paths"), "tm_days": tm.get("sessions_used") or tm.get("sessions"),
        })

    quotes = {sym: last}

    def last_of(symbol):
        if symbol not in quotes:
            try:
                with tc.market_scope(symbol):
                    quotes[symbol] = tc.quote(symbol)["price"]
            except tc.FetchError:
                quotes[symbol] = None
        return quotes[symbol]

    for k in ledger.get("tickets", []):   # every symbol: crypto and stocks share the desk
        if k.get("status") == "pending":
            exp = ts_of(k.get("expires_at"))
            if exp is None or now_ts >= exp:   # the broker reads it as expired: it can't be confirmed any more
                continue
        if k.get("status") in ("pending", "working"):
            rows.append({
                "kind": "ticket", "id": k.get("id"), "symbol": str(k.get("symbol", "")).upper(),
                "side": k.get("side"), "status": k.get("status"),
                "qty": num(k.get("qty")), "entry": num(k.get("entry"), 2), "stop": num(k.get("stop"), 2),
                "target": num(k.get("target"), 2), "created_at": to_utc(k.get("created_at")),
                "expires_at": to_utc(k.get("expires_at")),
            })
    rows += trade_rows(ledger, last_of)
    books = [("desk", ledger, desk)]
    if shadow_book is not None:
        sh_settings, sh_ledger, sh_summary = shadow_book
        shadow_desk = all_time(sh_ledger)
        rows.append({
            "kind": "shadow_account", "id": "shadow_account", "account_size": num(sh_settings["account_size"], 2),
            "risk_per_trade": num(sh_settings["account_size"] * sh_settings["risk_per_trade_pct"] / 100, 2),
            "fee_pct": num(tc.fee_pct(settings=sh_settings), 4),
            "realized_pnl": num(sh_summary["realized_pnl"], 2), "open_risk": num(sh_summary["open_risk"], 2),
            "trades_today": sh_summary["trades_today"], "wins": sh_summary["wins"], "losses": sh_summary["losses"],
            **shadow_desk,
        })
        rows += trade_rows(sh_ledger, last_of, "shadow_position", "shadow_trade", "shadow-")
        books.append(("shadow", sh_ledger, shadow_desk))
    for account, book, results in books:
        rows += equity_rows(account, book, results["first_trade_at"] or as_of)

    for path in args.scan:
        res = json.loads(Path(path).read_text(encoding="utf-8"))
        mkt = res.get("market", "us")
        rows.append({"kind": "scanmeta", "id": f"scanmeta-{mkt}", "market": mkt, "as_of": to_utc(res.get("as_of")),
                     "scanned": res.get("scanned"), "universe": res.get("universe_size"), "matched": res.get("matched"),
                     "skipped": res.get("skipped"), "filtered": res.get("filtered"), "elapsed_s": res.get("elapsed_s"),
                     "source": "Binance" if mkt == "crypto" else "Yahoo Finance", "incomplete": bool(res.get("incomplete"))})
        for rank, r in enumerate((res.get("results") or [])[:args.top_scan], 1):
            setups = r.get("setups") or []
            kl = r.get("key_level") or {}
            rows.append({"kind": "scan", "id": f"scan-{mkt}-{r['symbol']}", "market": mkt, "rank": rank,
                         "symbol": r["symbol"], "setup": ", ".join(x.get("setup", "") for x in setups),
                         "score": r.get("score"), "bias": r.get("bias"), "last": num(r.get("last")),
                         "change_pct": num(r.get("change_pct"), 2), "rvol": num(r.get("rvol"), 2),
                         "key_level": num(kl.get("price")), "detail": (setups[0].get("detail") if setups else "") or "",
                         "date": r.get("date")})
    if args.original_watchlist:
        w = json.loads(Path(args.original_watchlist).read_text(encoding="utf-8"))
        hits = list(w.get("hits") or [])
        mode = w.get("mode")   # the pack writes "daily" or "orb"; a file without it: ORB hits have no volume
        if mode == "daily" or (mode != "orb" and all(h.get("volume") is not None for h in hits)):
            # daily scan: most traded first (a hit without volume, possible with --min-volume 0, last)
            hits.sort(key=lambda h: -(float(h.get("price") or 0) * float(h.get("volume") or 0)))
        else:   # an intraday ORB sweep has no volume: furthest over its opening range first
            hits.sort(key=orb_order)
        rows.append({"kind": "scanmeta", "id": "scanmeta-original", "market": "original", "as_of": w.get("created_at"),
                     "scanned": w.get("scanned"), "universe": w.get("universe"), "matched": len(hits),
                     "source": f"original pack, {w.get('mode')} scan, {w.get('as_of')}", "incomplete": False})
        for rank, h in enumerate(hits[:args.top_scan], 1):
            rows.append({"kind": "scan", "id": f"scan-original-{h['symbol']}", "market": "original", "rank": rank,
                         "symbol": h["symbol"], "last": num(h.get("price"), 2),
                         "setup": ", ".join(h.get("setups") or []) + (
                             f" at {h['at']}, {h['pct_over']:+.2f}% over the range" if h.get("pct_over") is not None else ""),
                         "rsi": num(h.get("rsi"), 1),
                         "dollar_volume": None if h.get("volume") is None else num(float(h.get("price") or 0) * float(h["volume"]), 0),
                         "date": h.get("close_date")})
    if args.candidates:
        for c in json.loads(Path(args.candidates).read_text(encoding="utf-8")):
            rows.append(dict({k: c.get(k) for k in (
                "symbol", "setup", "side", "entry", "stop", "target", "tm_target_first", "tm_stop_first", "tm_open",
                "ev_after_fees", "verdict", "decision", "reason")}, kind="candidate", id=f"cand-{c.get('symbol')}"))

    for b in bars:
        rows.append({"kind": "candle", "id": f"c-{b.ts}", "t": iso(b.ts), "o": b.open, "h": b.high,
                     "l": b.low, "c": b.close, "v": round(b.volume, 4)})
    for i, e in enumerate(read_log()):
        rows.append({"kind": "log", "id": f"log-{i:04d}", "at": e["at"], "event": e["event"], "detail": e["detail"]})

    Path(args.out).write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(json.dumps({"out": args.out, "bytes": Path(args.out).stat().st_size, "rows": kinds,
                      "status": rows[0]["status"], "last": rows[0]["last_price"], "as_of": rows[0]["as_of"]}))


if __name__ == "__main__":
    tc.run(main)
