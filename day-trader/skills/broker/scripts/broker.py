#!/usr/bin/env python3
"""Paper broker: an order ticket that waits for your explicit "yes". PAPER ONLY.

There is no live mode, no --live flag and no code path that contacts a real broker. The only
network calls are Yahoo Finance price data (through trader_common) used to simulate fills.

  ticket    create a pending ticket after validating the levels and the daily budget
  confirm   the ONLY path that opens a paper position, and only with --yes
  sync      apply stops / targets / the time stop from 5-minute bars; fill working limit orders
  close     manual exit at the current quote or a given price
  cancel    cancel a pending ticket or a working order
  positions open paper trades with live unrealized P&L (best-effort), plus working orders
  tickets   today's tickets (and anything still pending or working); --all for every ticket
  history   closed paper trades of the last N days

Usage:
  python3 scripts/broker.py ticket --symbol TSLA --qty 8 --entry 364.18 --stop 358.32 --target 370.04
  python3 scripts/broker.py ticket --symbol TSLA               # levels (and size) from the saved plan
  python3 scripts/broker.py ticket --symbol TSLA --qty 8 --type market --stop 358.32 --target 370.04
  python3 scripts/broker.py confirm TKT-20261030-001           # shows the ticket; does NOT confirm
  python3 scripts/broker.py confirm TKT-20261030-001 --yes     # only after the user's explicit yes
  python3 scripts/broker.py sync
  python3 scripts/broker.py positions
  python3 scripts/broker.py close TRADE-20261030-001 --price 366.10
  python3 scripts/broker.py cancel TKT-20261030-001
  python3 scripts/broker.py tickets --all
  python3 scripts/broker.py history --days 30 --json

Exit codes: 0 ok, 1 bad input or refused (e.g. confirm without --yes, expired ticket),
2 data source unreachable, 3 blocked by the daily budget.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trader_common as tc  # noqa: E402

import argparse  # noqa: E402
import copy  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from decimal import ROUND_HALF_UP, Decimal  # noqa: E402
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple  # noqa: E402

BAR = 300                       # 5-minute bars, in seconds
DEFAULT_TIME_STOP = "15:55"     # the desk is flat by 15:55 ET
MIN_CONFIRM_SECONDS = 10        # a "yes" cannot arrive in the same breath as the ticket
EXIT_BLOCKED = 3                # refused by the daily budget
MAX_QTY = 1_000_000
MAX_PRICE = 10_000_000.0       # sanity cap on any price typed on the command line
MAX_DAYS = 3650
MAX_LEVERAGE = 4.0              # notional above 4x the paper account is refused (intraday margin cap)
STALE_QUOTE_SECONDS = 15 * 60
LOCK_WAIT_SECONDS = 15.0
LOCK_STALE_SECONDS = 120.0
SIDE_WORD = {"long": "BUY", "short": "SELL SHORT"}
REASON_WORD = {"stop": "STOP", "target": "TARGET", "eod": "TIME STOP", "manual": "MANUAL"}
PAPER = "PAPER · REAL MONEY: OFF"
NUMERIC_SETTINGS = ("account_size", "risk_per_trade_pct", "max_position_pct", "daily_max_loss",
                    "max_open_risk", "max_trades_per_day", "max_consecutive_losses",
                    "min_reward_risk", "ticket_ttl_minutes")


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def px(x: float) -> float:
    """Round a price to its tick, half-up: cents at $1 and above, $0.0001 below."""
    x = float(x)
    if not math.isfinite(x) or abs(x) >= MAX_PRICE:
        return x                     # nonsense prices are rejected by the callers, not crashed on here
    d = Decimal(repr(x))
    return float(d.quantize(Decimal("0.01") if abs(d) >= 1 else Decimal("0.0001"), rounding=ROUND_HALF_UP))


def sub_penny(x: float) -> bool:
    return math.isfinite(x) and abs(x) >= 1 and abs(px(x) - x) > 1e-9


def same_level(a: Any, b: Any) -> bool:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= (0.005 if min(a, b) >= 1 else 0.00005) + 1e-9


def parse_ts(stamp: Any) -> Optional[float]:
    """ISO stamp (naive = ET wall clock) -> unix seconds, or None."""
    if not stamp:
        return None
    try:
        return tc.parse_et(str(stamp)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def iso(ts: float) -> str:
    return tc.to_et(ts).isoformat(timespec="seconds")


def minute_f(dt: datetime) -> float:
    return dt.hour * 60 + dt.minute + dt.second / 60.0


def at_minute(day: str, minute: int) -> float:
    """Unix seconds of `day` at `minute` (after midnight ET)."""
    return tc.parse_et(f"{day}T{tc.hhmm(minute)}").timestamp()


def parse_time_stop(value: Any) -> int:
    """'15:55' -> minutes after midnight; must be inside the regular session (09:35-16:00)."""
    m = tc.parse_hhmm(str(value))
    if not tc.MARKET_OPEN + 5 <= m <= tc.MARKET_CLOSE:
        raise ValueError(f"time stop must be between 09:35 and 16:00 ET, got {value}")
    return m


def effective_time_stop(time_stop: int, session_end: Optional[int] = None) -> int:
    """On an early-close day the time stop moves to 5 minutes before that close."""
    if session_end is not None and tc.MARKET_OPEN < session_end < tc.MARKET_CLOSE:
        return max(tc.MARKET_OPEN + 5, min(time_stop, session_end - 5))
    return time_stop


def fmt_r(r: Optional[float]) -> str:
    return "-" if r is None else f"{r:+.2f}R"


def hm(stamp: Any, today: Optional[str] = None) -> str:
    ts = parse_ts(stamp)
    if ts is None:
        return "-"
    d = tc.to_et(ts)
    return d.strftime("%H:%M") if today is None or d.strftime("%Y-%m-%d") == today else d.strftime("%m-%d %H:%M")


def parse_symbol(value: str) -> str:
    sym = (value or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", sym):
        raise ValueError(f"not a ticker symbol: {value!r}")
    return sym


def order_window_problem(now: datetime, time_stop: int, ttl_minutes: Optional[int] = None) -> Optional[str]:
    """Why a paper order can't be placed now (None = fine). With ttl_minutes: ticket creation."""
    m = minute_f(now)
    if now.weekday() >= 5:
        return (f"the market is closed ({now:%A}); paper orders fill only on weekdays "
                f"09:30-{tc.hhmm(time_stop)} ET - create the ticket during the session")
    if m >= time_stop:
        return (f"it is {now:%H:%M} ET, at or past the {tc.hhmm(time_stop)} ET time stop - no new paper "
                "day trades after it")
    if ttl_minutes is None and m < tc.MARKET_OPEN:
        return f"pre-market ({now:%H:%M} ET): paper fills start at the 09:30 ET open"
    if ttl_minutes is not None and m + ttl_minutes <= tc.MARKET_OPEN:
        return (f"pre-market ({now:%H:%M} ET): a ticket made now expires in {ttl_minutes} min, before "
                "the 09:30 ET open - create it closer to the open")
    return None


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------

def normalize_bars(bars: Sequence[tc.Bar]) -> List[tc.Bar]:
    """Regular-session bars, one per 5-minute slot, stamped at the slot start.

    Yahoo sometimes repeats the live bar with its last-trade time (e.g. 10:17:43); copies in the
    same slot are merged so no high or low is lost.
    """
    by_slot: Dict[int, tc.Bar] = {}
    for b in sorted(bars, key=lambda x: x.ts):
        if not tc.in_regular_session(b.ts):
            continue
        k = b.ts // BAR
        prev = by_slot.get(k)
        if prev is None:
            by_slot[k] = tc.Bar(k * BAR, b.open, b.high, b.low, b.close, b.volume)
        else:
            by_slot[k] = tc.Bar(k * BAR, prev.open, max(prev.high, b.high), min(prev.low, b.low),
                                b.close, max(prev.volume, b.volume))
    return [by_slot[k] for k in sorted(by_slot)]


def session_end_minute(meta: Optional[Dict[str, Any]], day: str) -> Optional[int]:
    """Regular-session close (minutes ET) Yahoo reports for `day`, e.g. 780 on a half day."""
    periods: List[Dict[str, Any]] = []
    cur = (meta or {}).get("currentTradingPeriod")
    if isinstance(cur, dict) and isinstance(cur.get("regular"), dict):
        periods.append(cur["regular"])
    tp = (meta or {}).get("tradingPeriods")
    if isinstance(tp, dict):
        tp = tp.get("regular")
    if isinstance(tp, list):
        for row in tp:
            rows = row if isinstance(row, list) else [row]
            periods.extend(x for x in rows if isinstance(x, dict))
    for per in periods:
        try:
            start, end = int(per["start"]), int(per["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start and tc.et_date(start) == day and tc.et_date(end) == day:
            return tc.minute_of_day(end)
    return None


def session_over(day: str, now_ts: float, session_end: Optional[int] = None) -> bool:
    today = tc.et_date(now_ts)
    if today != day:
        return today > day
    return tc.minute_of_day(now_ts) >= (session_end or tc.MARKET_CLOSE)


# ---------------------------------------------------------------------------
# Fill and exit simulation (pure)
# ---------------------------------------------------------------------------

def bar_exit(side: str, fill_price: float, stop: float, target: Optional[float], b: tc.Bar,
             in_fill_bar: bool, prev_close: Optional[float] = None) -> Optional[Tuple[str, float, bool]]:
    """(reason, exit price, ambiguous) if this bar takes the position out, else None.

    * Touches count: low <= stop (long) is a stop, high >= target is a target.
    * Stop and target both touched in one bar -> STOP (conservative), flagged ambiguous, unless
      the bar *opened* beyond one of them (the open is the first print, so that one came first).
    * Opening beyond the stop fills the stop at the open (gap slippage). Opening beyond the
      target fills at the open only on a real gap (`prev_close`, the previous bar's close, short
      of the target); if the previous bar already closed beyond it - possible only after the fill
      bar, whose target is not checked - price crossed the target on the way, so it fills there.
    * The fill bar mixes prints from before the fill, so only the stop is checked there (never
      the target), at the stop or at the fill price if the fill was already through the stop.
    """
    if side == "long":
        if in_fill_bar:
            return ("stop", min(stop, fill_price), False) if b.low <= stop else None
        if b.open <= stop:
            return "stop", b.open, False
        if target is not None and b.open >= target:
            return "target", (b.open if prev_close is None or prev_close < target else target), False
        s_hit, t_hit = b.low <= stop, target is not None and b.high >= target
    else:
        if in_fill_bar:
            return ("stop", max(stop, fill_price), False) if b.high >= stop else None
        if b.open >= stop:
            return "stop", b.open, False
        if target is not None and b.open <= target:
            return "target", (b.open if prev_close is None or prev_close > target else target), False
        s_hit, t_hit = b.high >= stop, target is not None and b.low <= target
    if s_hit:
        return "stop", stop, bool(t_hit)
    if t_hit:
        return "target", float(target), False  # type: ignore[arg-type]
    return None


def walk_position(side: str, fill_price: float, stop: float, target: Optional[float], opened_ts: float,
                  bars: Sequence[tc.Bar], now_ts: float, time_stop: int,
                  session_end: Optional[int] = None) -> Dict[str, Any]:
    """Replay an open paper trade over completed 5-minute bars of its own session.

    Returns {"status": "closed", reason, price, ts, ambiguous, note} | {"status": "open",
    checked_through} | {"status": "issue", issue}. `bars` must be normalize_bars() output.
    Day trades never carry overnight: the bar that ends at or after the time stop closes the
    trade at its close ("eod"); if the session ended early, the last bar of the day does.
    """
    day = tc.et_date(opened_ts)
    t_eff = effective_time_stop(time_stop, session_end)
    fill_slot = int(opened_ts) - int(opened_ts) % BAR
    last: Optional[tc.Bar] = None
    for b in bars:
        if b.ts < fill_slot or b.date != day:
            continue
        if b.ts + BAR > now_ts:
            break                        # still forming (or after "now" in a replay)
        hit = bar_exit(side, fill_price, stop, target, b, b.ts == fill_slot, last.close if last else None)
        if hit:
            return {"status": "closed", "reason": hit[0], "price": px(hit[1]), "ts": b.ts + BAR,
                    "ambiguous": hit[2], "note": None}
        if tc.minute_of_day(b.ts) + 5 >= t_eff:
            return {"status": "closed", "reason": "eod", "price": px(b.close), "ts": b.ts + BAR,
                    "ambiguous": False, "note": None}
        last = b
    if session_over(day, now_ts, session_end):
        if last is not None:
            return {"status": "closed", "reason": "eod", "price": px(last.close), "ts": last.ts + BAR,
                    "ambiguous": False,
                    "note": f"no bar reached the {tc.hhmm(t_eff)} time stop on {day}; closed at the "
                            f"last bar ({tc.et_hhmm(last.ts + BAR)} ET)"}
        return {"status": "issue",
                "issue": f"no 5-minute bars on {day} after the fill - close it by hand with --price"}
    return {"status": "open", "checked_through": (last.ts + BAR) if last else None}


def walk_working(side: str, limit: float, confirmed_ts: float, bars: Sequence[tc.Bar], now_ts: float,
                 time_stop: int, session_end: Optional[int] = None) -> Dict[str, Any]:
    """Fill a working limit (day) order from completed 5-minute bars.

    A bar must trade THROUGH the limit (low < limit for a buy, high > limit for a short); a touch
    is not a fill. Fill at the limit, or at the bar's open if it opened beyond it (a gap).
    The bar in progress at the confirmation mixes prints from before the order existed, so its
    high/low don't count. But the market was on the far side of the limit when the order went
    working (else it would have filled then), so if that bar CLOSES through the limit, price
    crossed it after the confirmation: filled at the limit, at the confirmation time.
    Unfilled orders expire at the time stop. Result "ts" is the fill time, "bar" its bar start.
    """
    day = tc.et_date(confirmed_ts)
    t_eff = effective_time_stop(time_stop, session_end)
    first = int(math.ceil(confirmed_ts / BAR)) * BAR
    last: Optional[tc.Bar] = None
    for b in bars:
        if b.ts + BAR <= confirmed_ts or b.date != day:
            continue
        if b.ts + BAR > now_ts or tc.minute_of_day(b.ts) >= t_eff:
            break
        if b.ts < first:                 # the bar in progress at the confirmation
            if (b.close < limit) if side == "long" else (b.close > limit):
                return {"status": "filled", "price": px(limit), "ts": confirmed_ts, "bar": b.ts}
        elif side == "long" and b.low < limit:
            return {"status": "filled", "price": px(min(limit, b.open)), "ts": b.ts, "bar": b.ts}
        elif side == "short" and b.high > limit:
            return {"status": "filled", "price": px(max(limit, b.open)), "ts": b.ts, "bar": b.ts}
        last = b
    today = tc.et_date(now_ts)
    if today > day or (today == day and tc.minute_of_day(now_ts) >= t_eff + 5):
        return {"status": "expired", "ts": at_minute(day, t_eff)}
    return {"status": "working", "checked_through": (last.ts + BAR) if last else None}


# ---------------------------------------------------------------------------
# Ledger state
# ---------------------------------------------------------------------------

def load_ledger() -> Dict[str, Any]:
    try:
        led = tc.load_ledger()
    except AttributeError:          # ledger.json holds something other than an object
        led = None
    if not isinstance(led, dict) or not isinstance(led.get("tickets"), list) \
            or not isinstance(led.get("trades"), list):
        raise tc.StateError(f"{tc.state_path(tc.LEDGER_FILE)} is not a paper ledger "
                            '(expected {"tickets": [...], "trades": [...]}); fix or move it')
    return led


@contextmanager
def ledger_lock() -> Iterator[None]:
    """Cross-process lock around read-modify-write of ledger.json (O_EXCL lock file)."""
    path = tc.state_path("ledger.lock")
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise tc.StateError(f"{path} is held by another broker command; wait, or delete the "
                                    "file if no broker command is running") from None
            time.sleep(0.05)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def rows(led: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    return [r for r in led.get(key) or [] if isinstance(r, dict)]


def find(led: Dict[str, Any], key: str, ident: str) -> Optional[Dict[str, Any]]:
    want = (ident or "").strip().upper()
    for r in rows(led, key):
        if str(r.get("id", "")).upper() == want:
            return r
    return None


def ticket_status(k: Dict[str, Any], now_ts: float) -> str:
    """Stored status, except a pending ticket past expires_at reads as expired."""
    st = str(k.get("status") or "")
    if st == "pending":
        exp = parse_ts(k.get("expires_at"))
        if exp is None or now_ts >= exp:
            return "expired"
    return st


def expire_pending(led: Dict[str, Any], now_ts: float) -> List[str]:
    out = []
    for k in rows(led, "tickets"):
        if k.get("status") == "pending" and ticket_status(k, now_ts) == "expired":
            k["status"] = "expired"
            k["expired_at"] = k.get("expires_at") or iso(now_ts)
            out.append(str(k.get("id")))
    return out


def next_trade_id(led: Dict[str, Any], day: str) -> str:
    stem = f"TRADE-{day.replace('-', '')}-"
    ids = [str(t.get("id", "")) for t in rows(led, "trades")]
    n = [int(x[len(stem):]) for x in ids if x.startswith(stem) and x[len(stem):].isdigit()]
    return f"{stem}{(max(n) + 1) if n else 1:03d}"


def open_trade(led: Dict[str, Any], k: Dict[str, Any], price: float, ts: float) -> Dict[str, Any]:
    """Record a filled ticket as an open paper trade (and link the two)."""
    trade = {
        "id": next_trade_id(led, tc.et_date(ts)), "ticket_id": k["id"], "symbol": k["symbol"],
        "side": k["side"], "qty": int(k["qty"]), "entry_price": px(price), "stop": float(k["stop"]),
        "target": None if k.get("target") is None else float(k["target"]),
        "setup": k.get("setup"), "order_type": k.get("type", "limit"), "planned_entry": k.get("entry"),
        "time_stop": k.get("time_stop") or DEFAULT_TIME_STOP, "time_machine": k.get("time_machine"),
        "mode": "paper", "opened_at": iso(ts), "status": "open", "exit_price": None,
        "closed_at": None, "exit_reason": None, "pnl": None, "r_multiple": None,
    }
    led["trades"].append(trade)
    k.update(status="confirmed", trade_id=trade["id"], filled_at=trade["opened_at"], fill_price=trade["entry_price"])
    return trade


def close_trade(t: Dict[str, Any], price: float, stamp: str, reason: str, ambiguous: bool = False) -> None:
    t["exit_price"] = px(price)
    t["closed_at"] = stamp
    t["exit_reason"] = reason
    t["status"] = "closed"
    pnl = tc.trade_pnl(t, t["exit_price"])
    risk = tc.trade_initial_risk(t)
    t["pnl"] = round(pnl, 2)
    t["r_multiple"] = round(pnl / risk, 2) if risk > 0 else None
    if ambiguous:
        t["ambiguous_exit"] = True


# ---------------------------------------------------------------------------
# Settings and budget
# ---------------------------------------------------------------------------

def load_settings() -> Dict[str, Any]:
    s = tc.load_settings()
    for k in NUMERIC_SETTINGS:
        try:
            v = float(s[k])
            if not math.isfinite(v) or v < 0 or isinstance(s[k], bool):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise tc.StateError(f"settings.json has a bad value for {k}: {s.get(k)!r} - fix it with the "
                                "risk or budget skill, or delete the file") from None
        s[k] = int(v) if isinstance(tc.DEFAULT_SETTINGS[k], int) else v
    if s["ticket_ttl_minutes"] < 1:
        raise tc.StateError("settings.json: ticket_ttl_minutes must be at least 1")
    return s


def working_orders(led: Dict[str, Any], day: str, exclude: Optional[str] = None) -> List[Dict[str, Any]]:
    return [k for k in rows(led, "tickets") if k.get("status") == "working" and k.get("id") != exclude
            and str(k.get("confirmed_at") or "")[:10] == day]


def budget_ledger(led: Dict[str, Any], day: str, exclude: Optional[str] = None) -> Dict[str, Any]:
    """The ledger as tc.budget_check should see it: today's working orders reserve their risk and
    a trade slot (tc.day_summary only counts trades, so it would miss them)."""
    synth = [{"id": k["id"], "symbol": k.get("symbol"), "side": k.get("side"), "qty": k.get("qty"),
              "entry_price": k.get("entry"), "stop": k.get("stop"), "status": "open",
              "opened_at": k.get("confirmed_at")} for k in working_orders(led, day, exclude)]
    return {"tickets": [], "trades": rows(led, "trades") + synth}


def check_budget(led: Dict[str, Any], new_risk: float, settings: Dict[str, Any], day: str,
                 exclude: Optional[str] = None) -> Dict[str, Any]:
    try:
        res = tc.budget_check(new_risk, settings=settings, ledger=budget_ledger(led, day, exclude), day=day)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise tc.StateError(f"ledger.json has a malformed trade or order ({e!r}) - fix or remove it") from None
    wk = working_orders(led, day, exclude)
    res["working_orders"] = len(wk)
    res["working_risk"] = round(sum(abs(float(k["entry"]) - float(k["stop"])) * float(k["qty"]) for k in wk), 2)
    return res


# ---------------------------------------------------------------------------
# Ticket construction (pure)
# ---------------------------------------------------------------------------

def _plan_levels_match(plan: Optional[Dict[str, Any]], side: str, entry: float, stop: float,
                       target: Optional[float] = None, with_target: bool = False) -> bool:
    if not isinstance(plan, dict) or plan.get("side") not in (None, side):
        return False
    ok = same_level(plan.get("entry"), entry) and same_level(plan.get("stop"), stop)
    if with_target:
        pt = plan.get("target")
        ok = ok and ((pt is None and target is None) or same_level(pt, target))
    return ok


def resolve_order(symbol: str, plan: Optional[Dict[str, Any]], *, today: str, side: Optional[str] = None,
                  entry: Optional[float] = None, stop: Optional[float] = None,
                  target: Optional[float] = None, qty: Optional[int] = None, order_type: str = "limit",
                  setup: Optional[str] = None, time_stop: Optional[str] = None,
                  last_price: Optional[float] = None) -> Dict[str, Any]:
    """Work out the order from flags or the saved plan. Levels come ALL from flags or ALL from the
    plan (never mixed). Raises ValueError with an actionable message on bad input."""
    warnings: List[str] = []
    notes: List[str] = []
    if plan is not None and not isinstance(plan, dict):
        raise tc.StateError(f"the saved {symbol} plan is not an object; fix or delete it")
    if any(v is not None for v in (entry, stop, target)):
        source = "flags"
        if stop is None:
            raise ValueError("--stop is required with explicit levels (every paper ticket needs a stop)")
        for name, v in (("entry", entry), ("stop", stop), ("target", target)):
            if v is not None and sub_penny(v):
                raise ValueError(f"--{name} {v:g} has sub-penny digits; stocks at $1 and up trade in "
                                 f"cents (e.g. {px(v):.2f})")
        if entry is None:
            if order_type != "market":
                raise ValueError("--entry (your limit price) is required for a limit order")
            if last_price is None:
                raise ValueError("a market ticket without --entry needs the current price")
            entry = px(last_price)
            notes.append(f"no --entry: reference entry = the last price {tc.fmt_price(entry)}")
        if side is None:
            side = "long" if stop < entry else "short"
            notes.append(f"side inferred from the levels: {side} (stop {'below' if side == 'long' else 'above'} entry)")
    else:
        source = "plan"
        if not plan:
            raise ValueError(f"no saved plan for {symbol}: give --entry and --stop (and --target), or "
                             "build a plan first with the exit skill")
        try:
            entry, stop = float(plan["entry"]), float(plan["stop"])
            target = None if plan.get("target") is None else float(plan["target"])
        except (KeyError, TypeError, ValueError):
            raise tc.StateError(f"the saved {symbol} plan has no usable entry/stop; rebuild it with the "
                                "exit skill") from None
        plan_side = plan.get("side")
        if side and plan_side and side != plan_side:
            raise ValueError(f"--side {side} conflicts with the saved {symbol} plan ({plan_side}); give "
                             f"explicit --entry/--stop for a {side}")
        side = side or plan_side or ("long" if stop < entry else "short")
        rounded = {n: px(v) for n, v in (("entry", entry), ("stop", stop), ("target", target))
                   if v is not None and sub_penny(v)}
        if rounded:
            notes.append("plan prices rounded to cents: " + ", ".join(f"{n} {v:.2f}" for n, v in rounded.items()))
            entry, stop = rounded.get("entry", entry), rounded.get("stop", stop)
            target = rounded.get("target", target)
        updated = str(plan.get("updated_at") or plan.get("created_at") or "")[:10]
        if updated and updated != today:
            warnings.append(f"the saved {symbol} plan is from {updated} - check the levels still make sense today")
        if order_type == "market":
            notes.append(f"market order: the plan's entry {tc.fmt_price(entry)} is only a reference; the "
                         "fill happens at the price when you confirm")
    errs = tc.validate_levels(side, entry, stop, target)
    if errs:
        raise ValueError("; ".join(errs))

    # Size: --qty, else the plan's sizing section if it was computed for these exact levels.
    qty_source = "flag"
    sizing = plan.get("sizing") if isinstance(plan, dict) else None
    if qty is None:
        if isinstance(sizing, dict) and sizing.get("side", side) == side \
                and same_level(sizing.get("entry"), entry) and same_level(sizing.get("stop"), stop):
            try:
                qty = int(float(sizing.get("shares") or 0))
            except (TypeError, ValueError):
                qty = 0
            if qty < 1:
                raise ValueError(f"the saved {symbol} sizing is 0 shares (the stop is too wide for the risk "
                                 "budget); give --qty or re-plan the stop")
            qty_source = "plan sizing"
        elif isinstance(sizing, dict):
            raise ValueError(f"the saved {symbol} sizing was for other levels; give --qty, or re-size: "
                             f"risk.py size --symbol {symbol} --save")
        else:
            raise ValueError(f"give --qty (shares), or size the trade first: risk.py size --symbol {symbol} --save")
    if qty < 1 or qty > MAX_QTY:
        raise ValueError(f"--qty must be a whole number of shares from 1 to {MAX_QTY:,}")

    # Plan extras that belong to these levels: setup label, time stop, time-machine odds.
    matches = _plan_levels_match(plan, side, entry, stop)
    if setup is None and matches:
        setup = plan.get("setup") or None  # type: ignore[union-attr]
    ts_value = time_stop
    if ts_value is None and matches:
        ts_value = ((plan.get("exit_plan") or {}) if isinstance(plan.get("exit_plan"), dict) else {}).get("time_stop")  # type: ignore[union-attr]
        if ts_value is not None:
            try:
                parse_time_stop(ts_value)
                notes.append(f"time stop {ts_value} ET from the saved exit plan")
            except ValueError:
                notes.append(f"ignored the plan's time stop {ts_value!r} (not 09:35-16:00)")
                ts_value = None
    t_stop = parse_time_stop(ts_value or DEFAULT_TIME_STOP)
    tm = None
    tm_plan = plan.get("time_machine") if isinstance(plan, dict) else None
    if isinstance(tm_plan, dict):
        if _plan_levels_match(tm_plan, side, entry, stop, target, with_target=True):
            tm = {k: tm_plan.get(k) for k in ("verdict", "target_first_pct", "stop_first_pct",
                                              "open_at_close_pct", "expected_r", "as_of")}
        else:
            notes.append("the plan's time-machine odds were for other levels - not attached")
    return {"symbol": symbol, "side": side, "qty": int(qty), "type": order_type, "entry": float(entry),
            "stop": float(stop), "target": None if target is None else float(target),
            "setup": setup, "time_stop": tc.hhmm(t_stop), "time_stop_min": t_stop,
            "levels_source": source, "qty_source": qty_source, "time_machine": tm,
            "warnings": warnings, "notes": notes}


def order_checks(order: Dict[str, Any], settings: Dict[str, Any], last: Optional[Dict[str, Any]],
                 today: str) -> Tuple[List[str], List[str]]:
    """Warnings and notes about a proposed order (pure). Raises ValueError for hard limits."""
    warnings: List[str] = []
    notes: List[str] = []
    side, e, s, t, q = order["side"], order["entry"], order["stop"], order["target"], order["qty"]
    risk = abs(e - s) * q
    notional = e * q
    acct = settings["account_size"]
    if acct > 0 and notional > MAX_LEVERAGE * acct + 1e-9:
        raise ValueError(f"notional {tc.fmt_money(notional)} is more than {MAX_LEVERAGE:g}x the "
                         f"{tc.fmt_money(acct)} paper account - reduce --qty")
    if acct > 0 and notional > acct + 1e-9:
        warnings.append(f"notional {tc.fmt_money(notional)} is more than the {tc.fmt_money(acct)} paper "
                        "account - this needs margin")
    elif acct > 0 and notional > acct * settings["max_position_pct"] / 100 + 1e-9:
        warnings.append(f"notional is {notional / acct * 100:.0f}% of the account, over your "
                        f"{settings['max_position_pct']:g}% max position size")
    budget = acct * settings["risk_per_trade_pct"] / 100
    if budget > 0 and risk > budget * 1.01 + 0.01:
        warnings.append(f"risk {tc.fmt_money(risk)} is over your per-trade risk budget {tc.fmt_money(budget)} "
                        f"({settings['risk_per_trade_pct']:g}% of the account) - the risk skill sizes to it")
    if t is None:
        warnings.append("no target: the trade exits only at the stop, by hand, or at the time stop")
    else:
        rr = tc.reward_risk(side, e, s, t)
        if rr < settings["min_reward_risk"] - 1e-9:
            warnings.append(f"R:R {rr:.2f} is below your minimum {settings['min_reward_risk']:g}")
    if last:
        p = last["price"]
        qd = last.get("date")
        if qd and qd != today:
            notes.append(f"latest {order['symbol']} quote is from {qd} (holiday, halt or delayed data?)")
        through_stop = p <= s if side == "long" else p >= s
        past_target = t is not None and (p >= t if side == "long" else p <= t)
        if through_stop:
            warnings.append(f"last price {tc.fmt_price(p)} is already through the stop {tc.fmt_price(s)} - "
                            "confirm will refuse to fill")
        elif past_target:
            warnings.append(f"last price {tc.fmt_price(p)} is already past the target {tc.fmt_price(t)}")
        elif order["type"] == "market":
            notes.append(f"market order: fills at the price when you confirm (last {tc.fmt_price(p)}), not "
                         "exactly the entry shown; the stop distance and risk move with it")
        else:
            marketable = p <= e if side == "long" else p >= e
            if marketable:
                notes.append(f"limit is {'at/above' if side == 'long' else 'at/below'} the last price "
                             f"{tc.fmt_price(p)}: on yes it fills right away at about {tc.fmt_price(p)}")
            else:
                notes.append(f"last {tc.fmt_price(p)} is {'above' if side == 'long' else 'below'} your limit: "
                             "on yes it rests as a working day order until a 5-minute bar trades through "
                             f"{tc.fmt_price(e)}")
    return warnings, notes


def new_ticket(led: Dict[str, Any], order: Dict[str, Any], now: datetime, settings: Dict[str, Any],
               session_end: Optional[int] = None) -> Dict[str, Any]:
    """A pending ticket; it expires after the TTL or at the (early-close adjusted) time stop."""
    today = now.strftime("%Y-%m-%d")
    cutoff = effective_time_stop(order["time_stop_min"], session_end)
    expires = min(now.timestamp() + settings["ticket_ttl_minutes"] * 60, at_minute(today, cutoff))
    e, s, t = order["entry"], order["stop"], order["target"]
    return {
        "id": tc.new_id("TKT", [str(k.get("id", "")) for k in rows(led, "tickets")]),
        "symbol": order["symbol"], "side": order["side"], "qty": order["qty"], "type": order["type"],
        "entry": e, "stop": s, "target": t, "setup": order["setup"], "time_stop": order["time_stop"],
        "risk": round(abs(e - s) * order["qty"], 2),
        "rr": None if t is None else round(tc.reward_risk(order["side"], e, s, t), 2),
        "status": "pending", "mode": "paper", "created_at": iso(now.timestamp()), "expires_at": iso(expires),
        "trade_id": None, "time_machine": order["time_machine"],
        "levels_source": order["levels_source"], "qty_source": order["qty_source"],
    }


def symbol_exposure(led: Dict[str, Any], symbol: str, today: str) -> List[Dict[str, Any]]:
    """Open trades and today's working orders in `symbol`."""
    out = [dict(kind="trade", id=t.get("id"), side=t.get("side"), qty=t.get("qty"))
           for t in rows(led, "trades") if t.get("status") == "open" and str(t.get("symbol", "")).upper() == symbol]
    out += [dict(kind="working order", id=k.get("id"), side=k.get("side"), qty=k.get("qty"))
            for k in working_orders(led, today) if str(k.get("symbol", "")).upper() == symbol]
    return out


# ---------------------------------------------------------------------------
# Sync (pure apply + I/O fetch)
# ---------------------------------------------------------------------------

def sync_targets(led: Dict[str, Any], now: datetime, only: Optional[Set[str]] = None) -> Dict[str, Optional[str]]:
    """{symbol: Yahoo range to fetch (None = too old for 5-minute data)} for open/working items."""
    oldest: Dict[str, float] = {}
    items = [(k, "confirmed_at") for k in rows(led, "tickets") if k.get("status") == "working"]
    items += [(t, "opened_at") for t in rows(led, "trades") if t.get("status") == "open"]
    for row, field in items:
        if only is not None and str(row.get("id", "")).upper() not in only:
            continue
        ts = parse_ts(row.get(field))
        sym = str(row.get("symbol") or "").upper()
        if ts is None or not sym:
            continue
        oldest[sym] = min(oldest.get(sym, ts), ts)
    out: Dict[str, Optional[str]] = {}
    for sym, ts in oldest.items():
        age = (now.date() - tc.to_et(ts).date()).days
        out[sym] = "5d" if age <= 4 else ("60d" if age <= 55 else None)
    return out


def fetch_sync_data(targets: Dict[str, Optional[str]]) -> Tuple[Dict[str, Tuple[List[tc.Bar], Dict[str, Any]]], Dict[str, str]]:
    data: Dict[str, Tuple[List[tc.Bar], Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    for sym, rng in sorted(targets.items()):
        if rng is None:
            errors[sym] = ("opened more than 55 days ago - Yahoo keeps only 60 days of 5-minute bars; "
                           "close it by hand with --price")
            continue
        try:
            bars, meta = tc.fetch_bars(sym, "5m", rng)
            data[sym] = (normalize_bars(bars), meta or {})
        except tc.FetchError as e:
            errors[sym] = str(e)
    return data, errors


def apply_sync(led: Dict[str, Any], data: Dict[str, Tuple[List[tc.Bar], Dict[str, Any]]], now: datetime,
               only: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Fill working orders and close open trades in `led` (mutated) from pre-fetched bars. Pure.

    With `only` (ids), a working order in it that fills brings its new trade along, so the trade's
    exits are applied in the same pass."""
    now_ts = now.timestamp()
    out: Dict[str, Any] = {"events": [], "open": [], "working": [], "issues": []}
    only = None if only is None else set(only)

    def wanted(row: Dict[str, Any]) -> bool:
        return only is None or str(row.get("id", "")).upper() in only

    for k in rows(led, "tickets"):
        if k.get("status") != "working" or not wanted(k):
            continue
        sym = str(k.get("symbol") or "").upper()
        conf = parse_ts(k.get("confirmed_at"))
        try:
            side, limit = str(k["side"]), float(k["entry"])
            float(k["stop"]), int(k["qty"])
            t_stop = parse_time_stop(k.get("time_stop") or DEFAULT_TIME_STOP)
            if conf is None or side not in ("long", "short"):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            out["issues"].append({"id": k.get("id"), "symbol": sym, "issue": "malformed working order - cancel it"})
            continue
        if sym not in data:
            continue
        bars, meta = data[sym]
        r = walk_working(side, limit, conf, bars, now_ts, t_stop, session_end_minute(meta, tc.et_date(conf)))
        if r["status"] == "filled":
            trade = open_trade(led, k, r["price"], r["ts"])
            if only is not None:
                only.add(trade["id"].upper())
            out["events"].append({"kind": "filled", "ticket_id": k["id"], "trade_id": trade["id"], "symbol": sym,
                                  "side": side, "qty": trade["qty"], "price": trade["entry_price"],
                                  "at": trade["opened_at"], "bar": iso(r["bar"])})
        elif r["status"] == "expired":
            k.update(status="expired", expired_at=iso(r["ts"]))
            out["events"].append({"kind": "expired", "ticket_id": k["id"], "symbol": sym, "side": side,
                                  "qty": k.get("qty"), "price": limit, "at": k["expired_at"]})
        else:
            k["synced_at"] = iso(now_ts)
            out["working"].append({"ticket_id": k["id"], "symbol": sym, "side": side, "qty": k.get("qty"),
                                   "limit": limit, "checked_through": r["checked_through"] and iso(r["checked_through"])})

    for t in rows(led, "trades"):
        if t.get("status") != "open" or not wanted(t):
            continue
        sym = str(t.get("symbol") or "").upper()
        opened = parse_ts(t.get("opened_at"))
        try:
            side, fill, stop = str(t["side"]), float(t["entry_price"]), float(t["stop"])
            target = None if t.get("target") is None else float(t["target"])
            float(t["qty"])
            t_stop = parse_time_stop(t.get("time_stop") or DEFAULT_TIME_STOP)
            if opened is None or side not in ("long", "short"):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            out["issues"].append({"id": t.get("id"), "symbol": sym,
                                  "issue": "malformed trade - fix it in ledger.json or close it with --price"})
            continue
        if sym not in data:
            continue
        bars, meta = data[sym]
        r = walk_position(side, fill, stop, target, opened, bars, now_ts, t_stop,
                          session_end_minute(meta, tc.et_date(opened)))
        if r["status"] == "closed":
            close_trade(t, r["price"], iso(r["ts"]), r["reason"], r["ambiguous"])
            ev = {"kind": "closed", "trade_id": t["id"], "symbol": sym, "side": side, "qty": t["qty"],
                  "reason": r["reason"], "price": t["exit_price"], "at": t["closed_at"],
                  "bar": iso(r["ts"] - BAR), "pnl": t["pnl"],
                  "r_multiple": t["r_multiple"], "ambiguous": r["ambiguous"]}
            if r.get("note"):
                ev["note"] = r["note"]
            out["events"].append(ev)
        elif r["status"] == "issue":
            out["issues"].append({"id": t["id"], "symbol": sym, "issue": r["issue"]})
        else:
            t["synced_at"] = iso(now_ts)
            out["open"].append({"trade_id": t["id"], "symbol": sym, "side": side, "qty": t["qty"],
                                "entry_price": fill, "stop": stop, "target": target,
                                "checked_through": r["checked_through"] and iso(r["checked_through"])})
    return out


def prefetch_sync(now: datetime, only: Optional[Set[str]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Fetch bars for open/working items outside the ledger lock (network can be slow)."""
    return fetch_sync_data(sync_targets(load_ledger(), now, only))


def sync_notes(res: Dict[str, Any], errors: Dict[str, str]) -> List[str]:
    out = [f"sync: {event_text(e)}" for e in res["events"]]
    out += [f"could not sync {sym}: {msg}" for sym, msg in sorted(errors.items())]
    out += [f"{i['id']} {i['symbol']}: {i['issue']}" for i in res["issues"]]
    return out


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def get_quote(symbol: str) -> Dict[str, Any]:
    """tc.quote plus the quote's ET date/age (None when Yahoo gave no timestamp)."""
    q = tc.quote(symbol)
    price = q.get("price")
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        raise tc.FetchError(f"{symbol}: no usable price in the quote")
    out = {"symbol": symbol, "price": float(price), "time": None, "date": None}
    try:
        qt = int(q["time"]) if q.get("time") else None
    except (TypeError, ValueError):
        qt = None
    if qt:
        out["time"], out["date"] = iso(qt), tc.et_date(qt)
    return out


def early_close_today(symbol: str, day: str) -> Optional[int]:
    """Today's close (minutes ET) when Yahoo's trading period shows an early close (half days), else
    None. Best-effort, and the same request tc.quote makes (cached 15 s), so normally no extra fetch."""
    try:
        _, meta = tc.fetch_bars(symbol, "1m", "1d", ttl=15)
    except tc.FetchError:
        return None
    end = session_end_minute(meta, day)
    return end if end is not None and end < tc.MARKET_CLOSE else None


def early_close_note(close_min: Optional[int]) -> str:
    return f" (the market closes early today, at {tc.hhmm(close_min)} ET)" if close_min else ""


def best_effort_quote(symbol: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return get_quote(symbol), None
    except tc.FetchError as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_ticket(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    now = tc.now_et()
    today = now.strftime("%Y-%m-%d")
    sym = parse_symbol(args.symbol)
    settings = load_settings()
    if args.time_stop is not None:
        parse_time_stop(args.time_stop)
    if args.qty is not None and not 1 <= args.qty <= MAX_QTY:
        raise ValueError(f"--qty must be a whole number of shares from 1 to {MAX_QTY:,}")
    window = order_window_problem(now, parse_time_stop(args.time_stop or DEFAULT_TIME_STOP),
                                  settings["ticket_ttl_minutes"])
    if window:
        raise ValueError(window)
    plan = tc.load_plan(sym)
    quote, quote_err = best_effort_quote(sym)
    if args.order_type == "market" and args.entry is None and args.stop is not None and quote is None:
        raise tc.FetchError(f"a market ticket without --entry needs the current price: {quote_err}")
    order = resolve_order(sym, plan, today=today, side=args.side, entry=args.entry, stop=args.stop,
                          target=args.target, qty=args.qty, order_type=args.order_type, setup=args.setup,
                          time_stop=args.time_stop, last_price=quote["price"] if quote else None)
    early = early_close_today(sym, today) if quote is not None else None   # no 2nd slow retry when offline
    window = order_window_problem(now, effective_time_stop(order["time_stop_min"], early),
                                  settings["ticket_ttl_minutes"])
    if window:
        window += early_close_note(early)
        if args.time_stop is None and order["time_stop"] != DEFAULT_TIME_STOP:
            window += (f" (the {order['time_stop']} time stop comes from the saved exit plan; pass --time-stop "
                       "to choose another)")
        raise ValueError(window)
    warnings, notes = list(order["warnings"]), list(order["notes"])
    w2, n2 = order_checks(order, settings, quote, today)
    warnings += w2
    notes += n2
    if quote is None:
        notes.append(f"live price unavailable ({quote_err}); confirm will fetch it")
    data, errors = prefetch_sync(now)
    risk = round(abs(order["entry"] - order["stop"]) * order["qty"], 2)
    with ledger_lock():
        led = load_ledger()
        synced = apply_sync(led, data, now)
        expire_pending(led, now.timestamp())
        notes += sync_notes(synced, errors)
        for x in symbol_exposure(led, sym, today):
            if x["side"] != order["side"]:
                tc.save_ledger(led)
                raise ValueError(f"you have a {x['side']} {sym} {x['kind']} ({x['id']}); a {order['side']} "
                                 "ticket would net against it in a real account - close or cancel it first")
            warnings.append(f"adds to your {x['side']} {sym} {x['kind']} ({x['id']}, {x['qty']} shares)")
        budget = check_budget(led, risk, settings, today)
        res: Dict[str, Any] = {"action": "ticket", "mode": "paper", "real_money": False,
                               "last_price": quote["price"] if quote else None, "budget": budget,
                               "account_size": settings["account_size"],
                               "warnings": warnings, "notes": notes, "sync": synced,
                               "disclaimer": tc.DISCLAIMER}
        if not budget["ok"]:
            tc.save_ledger(led)
            res.update(ok=False, blocked=True, reasons=budget["reasons"],
                       order={k: v for k, v in order.items() if k not in ("warnings", "notes", "time_stop_min")})
            return res, EXIT_BLOCKED
        ticket = new_ticket(led, order, now, settings, early)
        superseded = []
        for k in rows(led, "tickets"):
            if k.get("status") == "pending" and str(k.get("symbol", "")).upper() == sym:
                k.update(status="cancelled", cancelled_at=ticket["created_at"],
                         cancel_reason=f"superseded by {ticket['id']}")
                superseded.append(k["id"])
        led["tickets"].append(ticket)
        tc.save_ledger(led)
    if superseded:
        notes.append(f"replaced the earlier pending {sym} ticket(s) {', '.join(superseded)} - a yes now "
                     f"only counts for {ticket['id']}")
    res.update(ok=True, status="pending", ticket=ticket, card=card_line(ticket), superseded=superseded,
               awaiting="the user's explicit yes, in a later message, for this ticket id")
    return res, 0


def cmd_confirm(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    now = tc.now_et()
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    tid = args.ticket_id.strip().upper()
    settings = load_settings()
    k0 = find(load_ledger(), "tickets", tid)
    if k0 is None:
        hint = " - that is a trade id; use close" if tid.startswith("TRADE-") else " - list them with: broker.py tickets"
        raise ValueError(f"no ticket {tid}{hint}")
    status = ticket_status(k0, now_ts)
    if status == "expired" and k0.get("status") == "pending":
        with ledger_lock():
            led = load_ledger()
            expire_pending(led, now_ts)
            tc.save_ledger(led)
    refuse_unless_pending(k0, status)
    if not args.yes:
        return {"ok": False, "action": "confirm", "confirmed": False, "ticket": k0, "card": card_line(k0),
                "reason": "--yes is required, and only after the user explicitly said yes to "
                          f"{tid} in a later message"}, 1
    created = parse_ts(k0.get("created_at"))
    if created is not None and now_ts - created < MIN_CONFIRM_SECONDS:
        raise ValueError(f"{tid} was created {max(0, int(now_ts - created))} s ago: show the ticket to the user "
                         "and wait for their explicit yes in a later message (when replaying with "
                         "$DAYTRADER_NOW, move it forward)")
    t_stop = parse_time_stop(k0.get("time_stop") or DEFAULT_TIME_STOP)
    window = order_window_problem(now, t_stop)
    if window:
        raise ValueError(f"{tid} not confirmed: {window}")
    sym = str(k0["symbol"]).upper()
    quote = get_quote(sym)                                   # FetchError -> exit 2
    if quote["date"] and quote["date"] != today:
        raise ValueError(f"{tid} not confirmed: the latest {sym} price is from {quote['date']}, not today "
                         "(market holiday, halt or stale data) - no paper fill")
    early = early_close_today(sym, today)
    cutoff = effective_time_stop(t_stop, early)
    if minute_f(now) >= cutoff:
        raise ValueError(f"{tid} not confirmed: it is {now:%H:%M} ET, at or past the {tc.hhmm(cutoff)} ET time stop"
                         f"{early_close_note(early)} - no paper fill after it")
    warnings: List[str] = []
    notes: List[str] = []
    qts = parse_ts(quote["time"])
    if qts is not None and now_ts - qts > STALE_QUOTE_SECONDS:
        warnings.append(f"the last {sym} trade was {int((now_ts - qts) // 60)} min ago - thin or halted?")
    data, errors = prefetch_sync(now)
    with ledger_lock():
        led = load_ledger()
        k = find(led, "tickets", tid)
        if k is None:
            raise ValueError(f"no ticket {tid}")
        refuse_unless_pending(k, ticket_status(k, now_ts))
        synced = apply_sync(led, data, now)
        notes += sync_notes(synced, errors)
        side, limit, stop = k["side"], float(k["entry"]), float(k["stop"])
        target = None if k.get("target") is None else float(k["target"])
        qty = int(k["qty"])
        price = quote["price"]
        if k.get("type") == "market":
            fill: Optional[float] = px(price)
        elif (side == "long" and price <= limit) or (side == "short" and price >= limit):
            fill = px(price)           # marketable limit: fills at the market, no worse than the limit
        else:
            fill = None                # rests as a working day order
        if fill is not None:
            errs = tc.validate_levels(side, fill, stop, target)
            if errs:
                tc.save_ledger(led)
                raise ValueError(f"{tid} not filled at {tc.fmt_price(fill)}: " + "; ".join(errs) +
                                 " - the price moved through your levels; cancel it and re-plan")
        risk = abs((fill if fill is not None else limit) - stop) * qty
        budget = check_budget(led, risk, settings, today, exclude=tid)
        res: Dict[str, Any] = {"action": "confirm", "mode": "paper", "real_money": False, "quote": quote,
                               "budget": budget, "warnings": warnings, "notes": notes, "sync": synced,
                               "disclaimer": tc.DISCLAIMER}
        if not budget["ok"]:
            tc.save_ledger(led)
            res.update(ok=False, confirmed=False, blocked=True, reasons=budget["reasons"], ticket=k)
            return res, EXIT_BLOCKED
        k["confirmed_at"] = iso(now_ts)
        k["confirm_price"] = px(price)
        trade = None
        if fill is not None:
            trade = open_trade(led, k, fill, now_ts)
            planned = abs(limit - stop) * qty
            if k.get("type") == "market" and risk > planned + 0.005:
                notes.append(f"market fill {tc.fmt_price(fill)} vs reference {tc.fmt_price(limit)}: risk is "
                             f"{tc.fmt_money(risk)} instead of {tc.fmt_money(planned)}")
        else:
            k["status"] = "working"
        tc.save_ledger(led)
    res.update(ok=True, confirmed=True, status="filled" if trade else "working", ticket=k, trade=trade,
               fill_price=trade["entry_price"] if trade else None)
    return res, 0


def refuse_unless_pending(k: Dict[str, Any], status: str) -> None:
    tid = k.get("id")
    if status == "pending":
        return
    if status == "expired":
        raise ValueError(f"{tid} expired at {hm(k.get('expires_at'))} ET without a yes - create a new ticket")
    if status == "cancelled":
        why = f" ({k['cancel_reason']})" if k.get("cancel_reason") else ""
        raise ValueError(f"{tid} was cancelled{why} - create a new ticket")
    if status == "confirmed":
        raise ValueError(f"{tid} is already confirmed (trade {k.get('trade_id') or '?'}) - a ticket opens at most "
                         "one position")
    if status == "working":
        raise ValueError(f"{tid} is already confirmed and working as a limit order - run sync, or cancel it")
    raise ValueError(f"{tid} has status {status!r} and cannot be confirmed")


def cmd_sync(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    now = tc.now_et()
    led0 = load_ledger()
    targets = sync_targets(led0, now)
    data, errors = fetch_sync_data(targets)
    fetch_errors = [errors[s] for s, rng in sorted(targets.items()) if rng is not None and s in errors]
    if fetch_errors and not data:
        raise tc.FetchError(fetch_errors[0])         # nothing could be checked at all
    with ledger_lock():
        led = load_ledger()
        res = apply_sync(led, data, now)
        expired = expire_pending(led, now.timestamp())
        tc.save_ledger(led)
    res.update(ok=True, action="sync", as_of=iso(now.timestamp()), expired_tickets=expired,
               errors=[{"symbol": s, "error": m} for s, m in sorted(errors.items())],
               summary=safe_summary(led, now.strftime("%Y-%m-%d")), disclaimer=tc.DISCLAIMER)
    return res, 0


def cmd_close(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    now = tc.now_et()
    now_ts = now.timestamp()
    tid = args.trade_id.strip().upper()
    if args.price is not None and (not math.isfinite(args.price) or not 0 < args.price < MAX_PRICE):
        raise ValueError(f"--price must be a positive price below {MAX_PRICE:,.0f}")
    t0 = find(load_ledger(), "trades", tid)
    if t0 is None:
        hint = " - that is a ticket id; use cancel" if tid.startswith("TKT-") else " - see: broker.py positions"
        raise ValueError(f"no trade {tid}{hint}")
    if t0.get("status") != "open":
        raise ValueError(f"{tid} is already closed ({t0.get('exit_reason')} at {tc.fmt_price(t0.get('exit_price'))})")
    sym = str(t0.get("symbol", "")).upper()
    notes: List[str] = []
    data, errors = prefetch_sync(now, {tid})
    if errors:
        notes.append(f"could not check stops/targets first: {errors.get(sym, next(iter(errors.values())))}")
    preview = copy.deepcopy(load_ledger())
    apply_sync(preview, data, now, {tid})
    pt = find(preview, "trades", tid)
    quote = None
    if pt is not None and pt.get("status") == "open" and args.price is None:
        quote = get_quote(sym)                                    # FetchError -> exit 2
        if quote["date"] and quote["date"] != now.strftime("%Y-%m-%d"):
            notes.append(f"the latest {sym} price is from {quote['date']}")
        if not tc.is_market_open(now):
            notes.append("market closed: closed at the last regular-session price")
    with ledger_lock():
        led = load_ledger()
        synced = apply_sync(led, data, now, {tid})
        t = find(led, "trades", tid)
        if t is None:
            raise ValueError(f"no trade {tid}")
        if t.get("status") != "open":
            tc.save_ledger(led)
            closed_by = "sync" if synced["events"] else "already"
            if closed_by == "already":
                raise ValueError(f"{tid} is already closed")
            notes.append(f"{tid} had already been closed by its {REASON_WORD.get(t['exit_reason'], t['exit_reason'])} "
                         f"at {hm(t['closed_at'])} ET - nothing left to close")
        else:
            closed_by = "manual"
            price = args.price if args.price is not None else (quote or {}).get("price")
            if price is None:
                raise tc.FetchError(f"{sym}: no price to close at")
            close_trade(t, price, iso(now_ts), "manual")
        tc.save_ledger(led)
    return {"ok": True, "action": "close", "closed_by": closed_by, "trade": t, "quote": quote,
            "notes": notes, "mode": "paper", "real_money": False, "disclaimer": tc.DISCLAIMER}, 0


def cmd_cancel(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    now = tc.now_et()
    tid = args.ticket_id.strip().upper()
    notes: List[str] = []
    data: Dict[str, Any] = {}
    k0 = find(load_ledger(), "tickets", tid)
    if k0 is not None and k0.get("status") == "working":
        # A working order may already have traded through its limit. Record that fill (and any exit)
        # first, so cancelling can never erase a fill - or its loss - after the fact.
        data, errors = prefetch_sync(now, {tid})
        if errors:
            notes.append(f"could not check whether it had already filled: {next(iter(errors.values()))}")
    with ledger_lock():
        led = load_ledger()
        k = find(led, "tickets", tid)
        if k is None:
            hint = " - that is a trade id; use close" if tid.startswith("TRADE-") else " - see: broker.py tickets"
            raise ValueError(f"no ticket {tid}{hint}")
        synced = apply_sync(led, data, now, {tid})
        if synced["events"]:
            tc.save_ledger(led)
        status = ticket_status(k, now.timestamp())
        if status == "confirmed":
            if any(e["kind"] == "filled" for e in synced["events"]):
                raise ValueError(f"{tid} had already filled before this cancel - nothing to cancel: "
                                 + "; ".join(event_text(e) for e in synced["events"])
                                 + ". See positions / history")
            raise ValueError(f"{tid} already filled - close the trade {k.get('trade_id')} instead")
        if status not in ("pending", "working"):
            raise ValueError(f"{tid} is {status}; nothing to cancel")
        k.update(status="cancelled", cancelled_at=iso(now.timestamp()), cancel_reason="cancelled by user")
        tc.save_ledger(led)
    return {"ok": True, "action": "cancel", "ticket": k, "was": status, "notes": notes}, 0


def cmd_positions(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    now = tc.now_et()
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    led = load_ledger()
    open_trades = [t for t in rows(led, "trades") if t.get("status") == "open"]
    working = [k for k in rows(led, "tickets") if k.get("status") == "working"]
    quotes: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}
    for sym in sorted({str(r.get("symbol", "")).upper() for r in open_trades + working}):
        q, err = best_effort_quote(sym)
        if q:
            quotes[sym] = q
        else:
            errors[sym] = err or "no quote"
    positions, warnings = [], []
    for t in open_trades:
        row = dict(t)
        q = quotes.get(str(t.get("symbol", "")).upper())
        row.update(last=None, unrealized=None, unrealized_r=None, initial_risk=None)
        try:
            risk = tc.trade_initial_risk(t)
            row["initial_risk"] = round(risk, 2)
            if q:
                pnl = tc.trade_pnl(t, q["price"])
                row.update(last=q["price"], unrealized=round(pnl, 2),
                           unrealized_r=round(pnl / risk, 2) if risk > 0 else None)
                stop, target = float(t["stop"]), t.get("target")
                long_ = t["side"] == "long"
                if (q["price"] <= stop) if long_ else (q["price"] >= stop):
                    warnings.append(f"{t['id']} {t['symbol']}: last {tc.fmt_price(q['price'])} is through the stop - run sync")
                elif target is not None and ((q["price"] >= float(target)) if long_ else (q["price"] <= float(target))):
                    warnings.append(f"{t['id']} {t['symbol']}: last {tc.fmt_price(q['price'])} is past the target - run sync")
        except (KeyError, TypeError, ValueError):
            warnings.append(f"{t.get('id')}: malformed trade in ledger.json")
        if str(t.get("opened_at") or "")[:10] != today:
            warnings.append(f"{t.get('id')} was opened {str(t.get('opened_at'))[:10]} - run sync (day trades are "
                            "closed at that day's time stop)")
        positions.append(row)
    work_rows = []
    for k in working:
        q = quotes.get(str(k.get("symbol", "")).upper())
        work_rows.append(dict(k, last=q["price"] if q else None))
    pending = [k for k in rows(led, "tickets") if ticket_status(k, now_ts) == "pending"]
    tot_unreal = [p["unrealized"] for p in positions if p["unrealized"] is not None]
    res = {"ok": True, "action": "positions", "as_of": iso(now_ts), "positions": positions, "working": work_rows,
           "pending_tickets": [k["id"] for k in pending],
           "working_risk": round(sum(abs(float(k.get("entry") or 0) - float(k.get("stop") or 0)) * float(k.get("qty") or 0)
                                     for k in working_orders(led, today)), 2),
           "unrealized_total": round(sum(tot_unreal), 2) if tot_unreal and len(tot_unreal) == len(positions) else None,
           "summary": safe_summary(led, today), "warnings": warnings,
           "errors": [{"symbol": s, "error": m} for s, m in sorted(errors.items())],
           "mode": "paper", "real_money": False, "disclaimer": tc.DISCLAIMER}
    return res, 0


def safe_summary(led: Dict[str, Any], day: str) -> Dict[str, Any]:
    try:
        return tc.day_summary({"tickets": [], "trades": rows(led, "trades")}, day)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise tc.StateError(f"ledger.json has a malformed trade ({e!r}) - fix or remove it") from None


def cmd_tickets(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    now = tc.now_et()
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    out = []
    for k in rows(load_ledger(), "tickets"):
        st = ticket_status(k, now_ts)
        if args.all or st in ("pending", "working") or str(k.get("created_at") or "")[:10] == today:
            out.append(dict(k, status=st))
    return {"ok": True, "action": "tickets", "as_of": iso(now_ts), "all": bool(args.all), "tickets": out}, 0


def cmd_history(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    if not 1 <= args.days <= MAX_DAYS:
        raise ValueError(f"--days must be from 1 to {MAX_DAYS}")
    now = tc.now_et()
    since = (now.date() - timedelta(days=args.days - 1)).isoformat()
    trades = sorted((t for t in rows(load_ledger(), "trades") if t.get("status") == "closed"
                     and str(t.get("closed_at") or "")[:10] >= since), key=lambda t: str(t.get("closed_at")))
    pnls = [float(t.get("pnl") or 0) for t in trades]
    rs = [float(t["r_multiple"]) for t in trades if isinstance(t.get("r_multiple"), (int, float))]
    wins, losses = sum(1 for p in pnls if p > 0), sum(1 for p in pnls if p < 0)
    totals = {"trades": len(trades), "wins": wins, "losses": losses, "flat": len(trades) - wins - losses,
              "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
              "net_pnl": round(sum(pnls), 2), "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
              "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None}
    return {"ok": True, "action": "history", "days": args.days, "since": since, "trades": trades,
            "totals": totals, "mode": "paper", "disclaimer": tc.DISCLAIMER}, 0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def order_words(k: Dict[str, Any]) -> str:
    e = tc.fmt_price(k.get("entry"))
    price = f"LIMIT {e}" if k.get("type", "limit") == "limit" else f"MARKET (ref {e})"
    target = tc.fmt_price(k["target"]) if k.get("target") is not None else "none"
    return (f"{SIDE_WORD.get(k.get('side'), '?')} {k.get('qty')} {k.get('symbol')} · {price} · "
            f"TARGET {target} · STOP {tc.fmt_price(k.get('stop'))}")


def card_line(k: Dict[str, Any]) -> str:
    return f"{order_words(k)} · {PAPER} · WAITING FOR YOUR YES — reply yes to confirm ticket {k.get('id')}"


def _flag_lines(res: Dict[str, Any]) -> List[str]:
    return [f"  ! {w}" for w in res.get("warnings") or []] + [f"  - {n}" for n in res.get("notes") or []]


def budget_line(b: Dict[str, Any], new_risk: float) -> str:
    s, lim = b["summary"], b["limits"]
    trades = s["trades_today"] + 1
    if not b["ok"]:
        return "BLOCKED"
    left = max(0.0, b["loss_budget_left"] - new_risk)
    extra = f" (incl. {b['working_orders']} working order{'s' * (b['working_orders'] != 1)})" if b.get("working_orders") else ""
    return (f"OK · {tc.fmt_money(left)} of {tc.fmt_money(lim['daily_max_loss'])} daily loss budget left after "
            f"this · trade {trades} of {lim['max_trades_per_day']} today{extra}")


def render_ticket(res: Dict[str, Any]) -> str:
    if res.get("blocked"):
        o = res["order"]
        risk = abs(o["entry"] - o["stop"]) * o["qty"]
        lines = [f"TICKET REFUSED · {order_words(o)} · {PAPER}",
                 f"Risk {tc.fmt_money(risk)} - BLOCKED by today's budget:"]
        lines += [f"  - {r}" for r in res["reasons"]]
        lines += _flag_lines(res)
        lines += ["No ticket was created. Check: budget skill (budget.py status).", tc.DISCLAIMER]
        return "\n".join(lines)
    k = res["ticket"]
    e, s, t, q = k["entry"], k["stop"], k["target"], k["qty"]
    per = abs(e - s)
    lines = [f"ORDER TICKET · {k['id']}", f"{order_words(k)} · {PAPER}"]
    rr = f" · reward {tc.fmt_money(abs(t - e) * q)} · R:R {k['rr']:.2f}" if t is not None else " · no target"
    lines.append(f"  Risk      {tc.fmt_money(k['risk'])} ({tc.fmt_price(per)}/share x {q}){rr}")
    acct = res.get("account_size")
    pct = f" ({e * q / acct * 100:.1f}% of the {tc.fmt_money(acct)} paper account)" if acct else ""
    last = f" · last {tc.fmt_price(res['last_price'])}" if res.get("last_price") is not None else ""
    lines.append(f"  Notional  {tc.fmt_money(e * q)}{pct}{last}")
    lines.append(f"  Budget    {budget_line(res['budget'], k['risk'])}")
    mins = int(round((parse_ts(k['expires_at']) - parse_ts(k['created_at'])) / 60))  # type: ignore[operator]
    lines.append(f"  Expires   {hm(k['expires_at'])} ET ({mins} min) · day order · flat by {k['time_stop']} ET")
    extra = []
    if k.get("setup"):
        extra.append(f"setup {k['setup']}")
    tm = k.get("time_machine")
    if tm and tm.get("verdict"):
        extra.append(f"time-machine: {tm['verdict']} (target first {tm.get('target_first_pct')}% / stop first "
                     f"{tm.get('stop_first_pct')}%)")
    if extra:
        lines.append("  Plan      " + " · ".join(extra))
    lines += _flag_lines(res)
    lines.append(f"WAITING FOR YOUR YES — reply yes to confirm ticket {k['id']}")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def render_confirm(res: Dict[str, Any]) -> str:
    k = res["ticket"]
    if res.get("blocked"):
        lines = [f"NOT CONFIRMED · {k['id']} · BLOCKED by today's budget:"]
        lines += [f"  - {r}" for r in res["reasons"]]
        lines += _flag_lines(res)
        lines.append(f"The ticket stays pending until {hm(k.get('expires_at'))} ET. {PAPER}.")
        return "\n".join(lines)
    if not res.get("confirmed"):
        return "\n".join([f"ORDER TICKET · {k['id']} · status {k.get('status')} · expires {hm(k.get('expires_at'))} ET",
                          f"{order_words(k)} · {PAPER}", f"NOT CONFIRMED: {res['reason']}."])
    if res["status"] == "filled":
        tr = res["trade"]
        risk = tc.trade_initial_risk(tr)
        target = tc.fmt_price(tr["target"]) if tr.get("target") is not None else "none"
        lines = [f"FILLED (paper) · {SIDE_WORD[tr['side']]} {tr['qty']} {tr['symbol']} @ {tc.fmt_price(tr['entry_price'])}"
                 f" · {tr['id']} · {PAPER}",
                 f"  STOP {tc.fmt_price(tr['stop'])} · TARGET {target} · risk {tc.fmt_money(risk)} · flat by "
                 f"{tr['time_stop']} ET",
                 "  `sync` applies the stop, target and time stop from 5-minute bars; `positions` shows live P&L."]
    else:
        side = k["side"]
        lines = [f"WORKING (paper) · {SIDE_WORD[side]} {k['qty']} {k['symbol']} LIMIT {tc.fmt_price(k['entry'])} · "
                 f"{k['id']} · {PAPER}",
                 f"  last {tc.fmt_price(res['quote']['price'])} is {'above' if side == 'long' else 'below'} your limit. "
                 f"`sync` fills it only if a later 5-minute bar trades through {tc.fmt_price(k['entry'])} (or "
                 f"the current bar closes through it) before {k['time_stop']} ET; otherwise it expires (day order)."]
    lines += _flag_lines(res)
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def event_text(e: Dict[str, Any]) -> str:
    side = str(e.get("side", "")).upper()
    when = f"in the {hm(e['bar'])} ET bar" if e.get("bar") else f"at {hm(e.get('at'))} ET"
    if e["kind"] == "filled":
        return (f"{e['ticket_id']} {e['symbol']}: working {side} order FILLED @ {tc.fmt_price(e['price'])} "
                f"{when} -> {e['trade_id']}")
    if e["kind"] == "expired":
        return (f"{e['ticket_id']} {e['symbol']}: working {side} limit {tc.fmt_price(e['price'])} EXPIRED unfilled "
                f"at {hm(e['at'])} ET (day order)")
    amb = " (stop and target in the same bar - counted as stop)" if e.get("ambiguous") else ""
    note = f" - {e['note']}" if e.get("note") else ""
    return (f"{e['trade_id']} {e['symbol']} {side} {e['qty']}: {REASON_WORD.get(e['reason'], e['reason'])} "
            f"@ {tc.fmt_price(e['price'])} {when} · {tc.fmt_money(e['pnl'], signed=True)} "
            f"{fmt_r(e['r_multiple'])}{amb}{note}")


def render_sync(res: Dict[str, Any]) -> str:
    lines = [f"SYNC · {hm(res['as_of'])} ET · paper ledger"]
    if not (res["events"] or res["open"] or res["working"] or res["issues"] or res["errors"]):
        lines.append("Nothing to sync: no open paper trades or working orders.")
    lines += [f"  {event_text(e)}" for e in res["events"]]
    for o in res["open"]:
        thru = f" (checked through {hm(o['checked_through'])} ET)" if o.get("checked_through") else " (no completed bar since the fill yet)"
        target = tc.fmt_price(o["target"]) if o.get("target") is not None else "none"
        lines.append(f"  open: {o['trade_id']} {o['symbol']} {o['side'].upper()} {o['qty']} @ "
                     f"{tc.fmt_price(o['entry_price'])} · stop {tc.fmt_price(o['stop'])} · target {target}{thru}")
    for w in res["working"]:
        lines.append(f"  working: {w['ticket_id']} {w['symbol']} {SIDE_WORD.get(w['side'], '?')} {w['qty']} "
                     f"LIMIT {tc.fmt_price(w['limit'])}")
    lines += [f"  ! {i['id']} {i['symbol']}: {i['issue']}" for i in res["issues"]]
    lines += [f"  ! could not sync {e['symbol']}: {e['error']}" for e in res["errors"]]
    if res["expired_tickets"]:
        lines.append(f"  - expired unconfirmed tickets: {', '.join(res['expired_tickets'])}")
    s = res["summary"]
    lines.append(f"Today: realized {tc.fmt_money(s['realized_pnl'], signed=True)} · open risk "
                 f"{tc.fmt_money(s['open_risk'])} · {s['open_positions']} open · {s['trades_today']} trades")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def render_close(res: Dict[str, Any]) -> str:
    t = res["trade"]
    head = "CLOSED (paper)" if res["closed_by"] == "manual" else "ALREADY CLOSED (paper)"
    lines = [f"{head} · {t['id']} {t['symbol']} {t['side'].upper()} {t['qty']} · "
             f"{REASON_WORD.get(t['exit_reason'], t['exit_reason'])} @ {tc.fmt_price(t['exit_price'])} · "
             f"{tc.fmt_money(t['pnl'], signed=True)} {fmt_r(t.get('r_multiple'))} · {PAPER}"]
    lines += [f"  - {n}" for n in res["notes"]]
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def render_cancel(res: Dict[str, Any]) -> str:
    k = res["ticket"]
    lines = [f"CANCELLED · {k['id']} ({res['was']}) · {order_words(k)} · nothing was filled"]
    lines += [f"  - {n}" for n in res.get("notes") or []]
    return "\n".join(lines)


def render_positions(res: Dict[str, Any]) -> str:
    today = res["as_of"][:10]
    lines = [f"PAPER POSITIONS · {hm(res['as_of'])} ET · REAL MONEY: OFF"]
    if res["positions"]:
        body = []
        for p in res["positions"]:
            body.append([p.get("id"), p.get("symbol"), str(p.get("side", "")).upper(), p.get("qty"),
                         tc.fmt_price(p.get("entry_price")), tc.fmt_price(p.get("stop")),
                         tc.fmt_price(p.get("target")), tc.fmt_price(p.get("last")),
                         tc.fmt_money(p.get("unrealized"), signed=True), fmt_r(p.get("unrealized_r")),
                         hm(p.get("opened_at"), today)])
        lines.append(tc.table(body, ["TRADE", "SYM", "SIDE", "QTY", "ENTRY", "STOP", "TARGET", "LAST", "P&L", "R",
                                     "OPENED"]))
        if res["unrealized_total"] is not None:
            lines.append(f"Unrealized total {tc.fmt_money(res['unrealized_total'], signed=True)}")
    else:
        lines.append("No open paper positions.")
    if res["working"]:
        lines.append("Working orders:")
        lines.append(tc.table([[k.get("id"), order_words(k), tc.fmt_price(k.get("last")), hm(k.get("confirmed_at"), today)]
                               for k in res["working"]], ["TICKET", "ORDER", "LAST", "CONFIRMED"]))
    if res["pending_tickets"]:
        lines.append(f"Pending tickets waiting for a yes: {', '.join(res['pending_tickets'])}")
    lines += [f"  ! {w}" for w in res["warnings"]]
    lines += [f"  ! no live price for {e['symbol']}: {e['error']}" for e in res["errors"]]
    s = res["summary"]
    reserved = f" (+{tc.fmt_money(res['working_risk'])} reserved by working orders)" if res.get("working_risk") else ""
    lines.append(f"Today: realized {tc.fmt_money(s['realized_pnl'], signed=True)} · open risk "
                 f"{tc.fmt_money(s['open_risk'])}{reserved} · trades {s['trades_today']} · loss streak {s['loss_streak']}")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def render_tickets(res: Dict[str, Any]) -> str:
    if not res["tickets"]:
        return "No tickets" + ("." if res["all"] else " today (use --all for older ones).")
    today = res["as_of"][:10]
    body = []
    for k in res["tickets"]:
        if k.get("status") == "working":
            when = f"until {k.get('time_stop') or DEFAULT_TIME_STOP}"
        else:
            when = hm(k.get("filled_at") or k.get("expired_at") or k.get("cancelled_at") or k.get("expires_at"), today)
        body.append([k.get("id"), k.get("status"), order_words(k).split(" · TARGET")[0],
                     tc.fmt_price(k.get("stop")), tc.fmt_price(k.get("target")), tc.fmt_money(k.get("risk")),
                     hm(k.get("created_at"), today), when, k.get("trade_id") or ""])
    return tc.table(body, ["TICKET", "STATUS", "ORDER", "STOP", "TARGET", "RISK", "CREATED", "UNTIL/DONE", "TRADE"])


def render_history(res: Dict[str, Any]) -> str:
    tot = res["totals"]
    if not res["trades"]:
        return f"No closed paper trades since {res['since']}."
    body = [[str(t.get("closed_at"))[:16].replace("T", " "), t.get("id"), t.get("symbol"),
             str(t.get("side", "")).upper(), t.get("qty"), tc.fmt_price(t.get("entry_price")),
             tc.fmt_price(t.get("exit_price")), REASON_WORD.get(t.get("exit_reason"), t.get("exit_reason")),
             tc.fmt_money(t.get("pnl"), signed=True), fmt_r(t.get("r_multiple"))] for t in res["trades"]]
    lines = [f"PAPER HISTORY · since {res['since']} ({res['days']} days)",
             tc.table(body, ["CLOSED", "TRADE", "SYM", "SIDE", "QTY", "ENTRY", "EXIT", "REASON", "P&L", "R"])]
    wr = f"{tot['win_rate_pct']:.0f}%" if tot["win_rate_pct"] is not None else "-"
    avg = fmt_r(tot["avg_r"])
    lines.append(f"{tot['trades']} trades · {tot['wins']} wins / {tot['losses']} losses · win rate {wr} · net "
                 f"{tc.fmt_money(tot['net_pnl'], signed=True)} · avg {avg}")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = tc.make_parser("Paper broker: order tickets that wait for your explicit yes. PAPER ONLY - "
                       "there is no live mode.")
    p.allow_abbrev = False
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, description=help_, allow_abbrev=False)
        tc.add_json_flag(sp)
        return sp

    t = add("ticket", "create a pending paper order ticket (validates levels and the daily budget)")
    t.add_argument("--symbol", required=True, help="ticker, e.g. TSLA")
    t.add_argument("--qty", type=int, help="shares (default: the plan's sizing from the risk skill)")
    t.add_argument("--side", choices=("long", "short"), help="default: the plan's side, else inferred from the levels")
    t.add_argument("--entry", type=float, help="limit price (market: reference price)")
    t.add_argument("--stop", type=float, help="stop price (required unless the levels come from the saved plan)")
    t.add_argument("--target", type=float, help="target price (optional)")
    t.add_argument("--type", dest="order_type", choices=("limit", "market"), default="limit",
                   help="limit (default): never pay more than --entry; market: fill at the price on confirm")
    t.add_argument("--setup", help="label for the journal, e.g. orb (default: the plan's setup)")
    t.add_argument("--time-stop", help="be flat by HH:MM ET (default: the exit plan's, else 15:55)")

    c = add("confirm", "confirm a ticket - ONLY after the user's explicit yes in a later message")
    c.add_argument("ticket_id", help="the exact ticket id, e.g. TKT-20261030-001")
    c.add_argument("--yes", action="store_true", help="the user explicitly said yes to this ticket")

    add("sync", "apply stops, targets and the time stop from 5-minute bars; fill working limit orders")

    cl = add("close", "close an open paper trade at the current price or --price")
    cl.add_argument("trade_id", help="e.g. TRADE-20261030-001")
    cl.add_argument("--price", type=float, help="exit price (default: the current quote)")

    ca = add("cancel", "cancel a pending ticket or a working order")
    ca.add_argument("ticket_id")

    add("positions", "open paper trades with live unrealized P&L, plus working orders")

    tk = add("tickets", "today's tickets plus anything pending or working")
    tk.add_argument("--all", action="store_true", help="every ticket in the ledger")

    h = add("history", "closed paper trades")
    h.add_argument("--days", type=int, default=30, help="look-back in calendar days (default 30)")
    return p


COMMANDS = {
    "ticket": (cmd_ticket, render_ticket), "confirm": (cmd_confirm, render_confirm),
    "sync": (cmd_sync, render_sync), "close": (cmd_close, render_close), "cancel": (cmd_cancel, render_cancel),
    "positions": (cmd_positions, render_positions), "tickets": (cmd_tickets, render_tickets),
    "history": (cmd_history, render_history),
}


FRONT_KEYS = ("ok", "action", "status", "confirmed", "blocked", "reasons", "card", "ticket", "trade")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    fn, render = COMMANDS[args.cmd]
    res, code = fn(args)
    res = dict([(k, res[k]) for k in FRONT_KEYS if k in res] + [(k, v) for k, v in res.items() if k not in FRONT_KEYS])
    tc.emit(res, getattr(args, "json", False), render)
    return code


if __name__ == "__main__":
    tc.run(main)
