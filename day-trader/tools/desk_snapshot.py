#!/usr/bin/env python3
"""Snapshot the paper desk into one JSON file, e.g. for a live dashboard of the desk.

Reads the desk's state in $DAYTRADER_HOME (settings, ledger, plans, the desk log) plus recent
5-minute candles of a focus symbol, and writes one JSON array of rows tagged by `kind`: account,
plan, ticket, position, trade, scanmeta, scan, candidate, candle, log. Optional inputs: scanner
--json output (--scan, repeatable), the original day-trader-skill scanner's trader/watchlist.json
(--original-watchlist) and a list of analysed candidates (--candidates). `--event "..." --detail
"..."` first appends a line to the desk log (desk_log.jsonl). Read-only otherwise; paper only.

  python3 tools/desk_snapshot.py --symbol BTC-USDT --out desk.json
  python3 tools/desk_snapshot.py --symbol TSLA --out desk.json --scan us.json --event "Scanned the market"
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


def to_utc(stamp):
    if not stamp:
        return None
    try:
        return iso(tc.parse_et(str(stamp)).timestamp())
    except ValueError:
        return str(stamp)


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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="BTC-USDT")
    ap.add_argument("--out", required=True)
    ap.add_argument("--hours", type=float, default=12.0, help="candle window")
    ap.add_argument("--scan", action="append", default=[], help="scanner --json output to show (repeatable)")
    ap.add_argument("--candidates", help="JSON list of analysed candidates to show")
    ap.add_argument("--original-watchlist", help="trader/watchlist.json from the original day-trader-skill scanner")
    ap.add_argument("--top-scan", type=int, default=15)
    ap.add_argument("--event")
    ap.add_argument("--detail", default="")
    args = ap.parse_args()

    sym = args.symbol.upper()
    tc.use_market_for(sym)
    if args.event:
        append_log(args.event, args.detail)

    settings = tc.load_settings()
    ledger = tc.load_ledger()
    summary = tc.day_summary(ledger)
    check = tc.budget_check(0, settings=settings, ledger=ledger)
    now_ts = tc.now_et().timestamp()

    bars, _meta = tc.fetch_bars(sym, "5m", "1d")
    keep = int(args.hours * 12)
    bars = bars[-keep:]
    try:
        last = tc.quote(sym)["price"]
    except tc.FetchError:
        last = bars[-1].close if bars else None

    rows = []
    rows.append({
        "kind": "account", "id": "account", "as_of": iso(now_ts), "day": summary["date"],
        "exchange": tc.exchange_name() if tc.is_crypto_symbol(sym) else "Yahoo Finance", "symbol": sym,
        "status": desk_status(settings, check, summary), "last_price": num(last, 2),
        "account_size": num(settings["account_size"], 2),
        "risk_per_trade": num(settings["account_size"] * settings["risk_per_trade_pct"] / 100, 2),
        "fee_pct": num(tc.fee_pct(settings=settings), 4),
        "realized_pnl": num(summary["realized_pnl"], 2), "open_risk": num(summary["open_risk"], 2),
        "loss_budget_used": num(check["loss_budget_used"], 2), "loss_budget_left": num(check["loss_budget_left"], 2),
        "daily_max_loss": num(settings["daily_max_loss"], 2), "trades_today": summary["trades_today"],
        "max_trades": settings["max_trades_per_day"], "wins": summary["wins"], "losses": summary["losses"],
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
        if k.get("status") in ("pending", "working"):
            rows.append({
                "kind": "ticket", "id": k.get("id"), "symbol": str(k.get("symbol", "")).upper(),
                "side": k.get("side"), "status": k.get("status"),
                "qty": num(k.get("qty")), "entry": num(k.get("entry"), 2), "stop": num(k.get("stop"), 2),
                "target": num(k.get("target"), 2), "created_at": to_utc(k.get("created_at")),
                "expires_at": to_utc(k.get("expires_at")),
            })
    for t in ledger.get("trades", []):
        tsym = str(t.get("symbol", "")).upper()
        base = {"id": t.get("id"), "symbol": tsym, "side": t.get("side"), "qty": num(t.get("qty")),
                "entry_price": num(t.get("entry_price"), 2), "stop": num(t.get("stop"), 2),
                "target": num(t.get("target"), 2), "opened_at": to_utc(t.get("opened_at"))}
        if t.get("status") == "open":
            px = last_of(tsym)
            unreal = tc.trade_pnl(t, px) if px else None
            rows.append(dict(base, kind="position", last=num(px, 2), unrealized_pnl=num(unreal, 2)))
        elif t.get("status") == "closed":
            rows.append(dict(base, kind="trade", exit_price=num(t.get("exit_price"), 2),
                             exit_reason=t.get("exit_reason"), gross_pnl=num(t.get("gross_pnl", t.get("pnl")), 2),
                             fees=num(t.get("fees") or 0, 2), pnl=num(t.get("pnl"), 2),
                             r_multiple=num(t.get("r_multiple"), 2), closed_at=to_utc(t.get("closed_at"))))

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
    if args.original_watchlist and Path(args.original_watchlist).exists():
        w = json.loads(Path(args.original_watchlist).read_text(encoding="utf-8"))
        hits = sorted(w.get("hits") or [], key=lambda h: -(float(h.get("price") or 0) * float(h.get("volume") or 0)))
        rows.append({"kind": "scanmeta", "id": "scanmeta-original", "market": "original", "as_of": w.get("created_at"),
                     "scanned": w.get("scanned"), "universe": w.get("universe"), "matched": len(hits),
                     "source": f"original pack, {w.get('mode')} scan, {w.get('as_of')}", "incomplete": False})
        for rank, h in enumerate(hits[:args.top_scan], 1):
            rows.append({"kind": "scan", "id": f"scan-original-{h['symbol']}", "market": "original", "rank": rank,
                         "symbol": h["symbol"], "setup": ", ".join(h.get("setups") or []), "last": num(h.get("price"), 2),
                         "rsi": num(h.get("rsi"), 1), "dollar_volume": num(float(h.get("price") or 0) * float(h.get("volume") or 0), 0),
                         "date": h.get("close_date")})
    if args.candidates and Path(args.candidates).exists():
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
