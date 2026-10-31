#!/usr/bin/env python3
"""Paper broker: an order ticket that waits for your explicit "yes". PAPER ONLY.

There is no live mode, no --live flag and no code path that contacts a real broker or an exchange
account. The only network calls are public price data (through trader_common) used to simulate
fills: Yahoo Finance for US stocks; for crypto, Binance's public market data by default (or Coinbase
Exchange's, with the crypto_exchange setting) - no account, no key.

Crypto pairs (BTC-USDT, ETH-USDT, BTC-USD, ...) trade 24/7 and their day is the UTC day: tickets
can be confirmed on weekends too (still not past the 23:55 UTC time stop), sizes are fractional
(the pair's size step), prices sit on the pair's tick, and every paper fill pays the
crypto_fee_pct fee - trades store gross_pnl and fees, and pnl / r_multiple are net of fees. Each
crypto ticket and trade records the exchange that priced it (`exchange`) and is filled, synced and
closed on that exchange's prices only, even after the setting changes.

  ticket    create a pending ticket after validating the levels and the daily budget (outside the
            session: the same checks, printed as a PREVIEW that is not saved and cannot be confirmed)
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
  python3 scripts/broker.py ticket --symbol BTC-USDT --qty 0.01 --entry 101250 --stop 99000 --target 104000
  python3 scripts/broker.py sync --market crypto               # the UTC clock and day for the summary

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
MIN_CONFIRM_SECONDS = 10        # a "yes" cannot arrive in the same breath as the ticket
EXIT_BLOCKED = 3                # refused by the daily budget
MAX_QTY = 1_000_000
MAX_PRICE = 10_000_000.0       # sanity cap on any price typed on the command line
MAX_DAYS = 3650
MAX_LEVERAGE = 4.0              # notional above 4x the paper account is refused (intraday margin cap)
MAX_SYNC_AGE_DAYS = 55          # older open items are not replayed from 5-minute bars
CRYPTO_FALLBACK_TICK = 1e-8     # crypto price rounding when the product's tick is unknown
STALE_QUOTE_SECONDS = 15 * 60
LOCK_WAIT_SECONDS = 15.0
LOCK_STALE_SECONDS = 120.0
SIDE_WORD = {"long": "BUY", "short": "SELL SHORT"}
REASON_WORD = {"stop": "STOP", "target": "TARGET", "eod": "TIME STOP", "manual": "MANUAL"}
PAPER = "PAPER · REAL MONEY: OFF"
NUMERIC_SETTINGS = ("account_size", "risk_per_trade_pct", "max_position_pct", "daily_max_loss",
                    "max_open_risk", "max_trades_per_day", "max_consecutive_losses",
                    "min_reward_risk", "ticket_ttl_minutes", "crypto_fee_pct")


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def px(x: float, tick: Optional[float] = None) -> float:
    """Round a price to its tick, half-up: stocks cents at $1 and above, $0.0001 below; crypto the
    product's quote increment (`tick`, e.g. 0.01 for BTC-USD)."""
    x = float(x)
    if not math.isfinite(x) or abs(x) >= MAX_PRICE:
        return x                     # nonsense prices are rejected by the callers, not crashed on here
    d = Decimal(repr(x))
    if tick:
        t = Decimal(repr(float(tick)))
        return float((d / t).quantize(Decimal(1), rounding=ROUND_HALF_UP) * t)
    return float(d.quantize(Decimal("0.01") if abs(d) >= 1 else Decimal("0.0001"), rounding=ROUND_HALF_UP))


def sub_penny(x: float) -> bool:
    return math.isfinite(x) and abs(x) >= 1 and abs(px(x) - x) > 1e-9


def off_tick(x: float, tick: float) -> bool:
    """True when `x` is not a multiple of the price tick (crypto's version of the sub-penny rule)."""
    return math.isfinite(x) and abs(x) < MAX_PRICE and abs(px(x, tick) - x) > tick * 1e-6


def decimals_of(step: float) -> int:
    """0.01 -> 2, 1e-8 -> 8, 1 -> 0."""
    return max(0, -int(math.floor(math.log10(step) + 1e-9)))


def step_text(step: float) -> str:
    """A tick or size step as typed: 0.01, 0.00000001."""
    return f"{step:.{decimals_of(step)}f}"


def same_level(a: Any, b: Any, tick: Optional[float] = None) -> bool:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if tick:
        return abs(a - b) <= tick / 2 + 1e-12
    return abs(a - b) <= (0.005 if min(a, b) >= 1 else 0.00005) + 1e-9


# ---------------------------------------------------------------------------
# Markets: every ticket and trade belongs to one (US stocks, or 24/7 crypto on UTC days)
# ---------------------------------------------------------------------------

def row_market(row: Any) -> tc.Market:
    """The market of a ticket, trade or event: from its symbol (BTC-USD -> crypto), else its
    `market` field, else the default market."""
    row = row if isinstance(row, dict) else {}
    sym = str(row.get("symbol") or "").strip()
    if sym:
        return tc.market_for(sym)
    return tc.MARKETS.get(str(row.get("market") or "").strip().lower()) or tc.market_for(None)


def is_crypto(row: Any) -> bool:
    return row_market(row).key == "crypto"


# Crypto rows also belong to one exchange: the one whose public prices made the ticket. They are
# filled, synced, marked and closed on that exchange only - never on the other one - even after the
# crypto_exchange setting (Binance by default, or Coinbase) changes.
LEGACY_EXCHANGE = "coinbase"      # crypto rows from before tickets recorded it (v1.1.0 priced on Coinbase)
EXCHANGE_ENV = "DAYTRADER_CRYPTO_EXCHANGE"


def row_exchange(row: Any) -> Optional[str]:
    """The exchange that priced a crypto ticket, trade or sync event ('binance' | 'coinbase'): its
    `exchange` field, else Coinbase (rows written before the field existed). None for stocks."""
    row = row if isinstance(row, dict) else {}
    if not is_crypto(row):
        return None
    return str(row.get("exchange") or LEGACY_EXCHANGE).strip().lower()


def order_exchange(order: Dict[str, Any]) -> Optional[str]:
    """The exchange a new crypto order is priced on: its own, else the configured one. None for stocks."""
    if not is_crypto(order):
        return None
    return str(order.get("exchange") or tc.crypto_exchange()).strip().lower()


def exchange_label(exchange: Optional[str]) -> str:
    """'Binance' / 'Coinbase' (None: the configured exchange; an unknown name is shown as stored)."""
    if not exchange:
        return tc.exchange_name()
    return tc.CRYPTO_EXCHANGES[exchange]["name"] if exchange in tc.CRYPTO_EXCHANGES else str(exchange)


def exchange_problem(exchange: Optional[str]) -> Optional[str]:
    """Why a row's exchange cannot be priced (an unknown name in a hand-edited ledger), else None."""
    if exchange is None or exchange in tc.CRYPTO_EXCHANGES:
        return None
    return (f"ledger.json names exchange {exchange!r} - expected {' or '.join(tc.CRYPTO_EXCHANGES)}; fix it there "
            "(a ticket can still be cancelled, a trade closed by hand with --price)")


@contextmanager
def exchange_scope(exchange: Optional[str]) -> Iterator[None]:
    """Inside the block crypto prices come from `exchange` - the exchange that priced a ticket or
    trade - whatever the crypto_exchange setting says now. It uses tc's own override
    ($DAYTRADER_CRYPTO_EXCHANGE, restored afterwards), so every fetch still goes through tc.fetch_bars
    and tc.quote. None (stocks, or a new order on the configured exchange): no change."""
    if not exchange:
        yield
        return
    problem = exchange_problem(exchange)
    if problem:
        raise tc.StateError(problem)
    old = os.environ.get(EXCHANGE_ENV)
    os.environ[EXCHANGE_ENV] = exchange
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(EXCHANGE_ENV, None)
        else:
            os.environ[EXCHANGE_ENV] = old


FeedKey = Tuple[str, Optional[str]]


def feed_key(row: Dict[str, Any]) -> FeedKey:
    """The price feed a row needs: (symbol, None) for stocks, (symbol, its exchange) for crypto."""
    return str(row.get("symbol") or "").upper(), row_exchange(row)


def feed_sort(item: Tuple[FeedKey, Any]) -> Tuple[str, str]:
    return item[0][0], item[0][1] or ""


def feed_errors(errors: Dict[FeedKey, str]) -> List[Dict[str, Any]]:
    """JSON rows for fetch errors: {symbol, error}, plus the exchange for crypto."""
    out = []
    for (sym, ex), msg in sorted(errors.items(), key=feed_sort):
        out.append({"symbol": sym, "error": msg, **({"exchange": ex} if ex else {})})
    return out


def quote_currency(symbol: str, exchange: Optional[str]) -> str:
    """The currency a pair is priced in on the exchange: BTC-USD is a USDT pair on Binance."""
    quote = tc.crypto_pair(symbol)[1]
    return "USDT" if quote == "USD" and exchange == "binance" else quote


def min_order_text(amount: float, symbol: str, exchange: Optional[str]) -> str:
    """An exchange's minimum order value in the pair's quote currency: '$1.00' or '5 USDT'."""
    quote = quote_currency(symbol, exchange)
    return tc.fmt_money(amount) if quote == "USD" else f"{amount:g} {quote}"


def rules_name(exchange: Optional[str]) -> str:
    """What the exchange calls a pair's size step and price tick: 'Binance pair rules' (symbol
    filters) or 'Coinbase product rules'."""
    return f"{exchange_label(exchange)} {'product' if exchange == 'coinbase' else 'pair'} rules"


def usd_pair_note(symbol: str, exchange: Optional[str]) -> Optional[str]:
    """Binance's few USD pairs are thin: a -USD symbol is priced from its USDT pair there - say so."""
    if exchange != "binance" or tc.crypto_pair(symbol)[1] != "USD":
        return None
    return (f"{symbol} is priced from Binance's {tc.binance_symbol(symbol)} pair (Binance's few USD pairs are thin; "
            "USDT trades close to, not exactly at, $1)")


def _positive_number(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        return None
    return float(v)


def row_tick(row: Dict[str, Any]) -> Optional[float]:
    """Price tick of a row: None for stocks (cents rule), the stored product tick for crypto."""
    if not is_crypto(row):
        return None
    return _positive_number(row.get("tick")) or CRYPTO_FALLBACK_TICK


def row_qty(row: Dict[str, Any]) -> Any:
    """Whole shares for stocks, a fractional size for crypto."""
    return float(row["qty"]) if is_crypto(row) else int(row["qty"])


def fee_rate(row: Dict[str, Any]) -> float:
    """Paper fee per fill as a fraction of notional: 0 for stocks; for crypto the row's own
    fee_pct (the fee its fills pay), else the crypto_fee_pct setting."""
    if not is_crypto(row):
        return 0.0
    v = row.get("fee_pct")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0:
        return float(v) / 100.0
    return tc.fee_pct(tc.CRYPTO) / 100.0


def fee_amount(rate: float, price: float, qty: float) -> float:
    return round(rate * float(price) * float(qty), 2)


def max_loss(side: str, entry: float, stop: float, qty: float, rate: float) -> float:
    """Dollars lost if the stop fills, fees of both fills included (stocks: rate 0). Unrounded."""
    per = (entry - stop) if side == "long" else (stop - entry)
    return max(0.0, per * qty + rate * (entry + stop) * qty)


def unit_word(symbol: Any) -> str:
    """'shares' for stocks, the base currency for crypto ('BTC' for BTC-USDT, BTCUSDT or BTC-USD)."""
    sym = str(symbol or "").upper()
    return tc.crypto_pair(sym)[0] if tc.is_crypto_symbol(sym) else "shares"


def qty_text(row: Dict[str, Any]) -> str:
    """A ticket's or trade's size as printed: 8, or 0.01234567 for crypto."""
    q = row.get("qty")
    if is_crypto(row) and isinstance(q, (int, float)) and not isinstance(q, bool):
        return tc.fmt_qty(q)
    return str(q)


def floor_step(x: float, step: float) -> float:
    n = math.floor(x / step + 1e-9)
    return round(n * step, decimals_of(step))


def off_step(q: float, step: float) -> bool:
    n = q / step
    return abs(n - round(n)) > 1e-6


def default_time_stop() -> str:
    """The desk is flat by 15:55 ET (stocks) or 23:55 UTC (crypto) - the active market's."""
    return tc.market().time_stop


def paper_label(row: Dict[str, Any]) -> str:
    """'PAPER · REAL MONEY: OFF', plus whose prices for crypto: '· Binance prices' / '· Coinbase prices'."""
    return f"{PAPER} · {exchange_label(row_exchange(row))} prices" if is_crypto(row) else PAPER


def local_day(stamp: Any) -> str:
    """Calendar date of an ISO stamp in the active market's zone ('' when absent)."""
    if not stamp:
        return ""
    ts = parse_ts(stamp)
    return tc.et_date(ts) if ts is not None else str(stamp)[:10]


def row_hm(row: Dict[str, Any], stamp: Any, today: Optional[str] = None) -> str:
    """hm() in the row's own market zone; labelled when that is not the zone on screen."""
    m = row_market(row)
    with tc.market_scope(m):
        text = hm(stamp, today)
    return text if m.key == tc.market().key or text == "-" else f"{text} {m.tz_label}"


def parse_ts(stamp: Any) -> Optional[float]:
    """ISO stamp (naive = the active market's wall clock: ET, UTC for crypto) -> unix seconds, or None."""
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
    """Unix seconds of `day` at `minute` (after midnight in the market's zone: ET, or UTC for crypto)."""
    return tc.parse_et(f"{day}T{tc.hhmm(minute)}").timestamp()


def time_stop_range() -> Tuple[int, int]:
    """Allowed time stops: stocks 09:35-16:00 ET, crypto 00:05-23:59 UTC."""
    return tc.MARKET_OPEN + 5, min(tc.MARKET_CLOSE, 24 * 60 - 1)


def parse_time_stop(value: Any) -> int:
    """'15:55' -> minutes after midnight; must be inside the regular session (09:35-16:00 ET)."""
    m = tc.parse_hhmm(str(value))
    first, last = time_stop_range()
    if not first <= m <= last:
        raise ValueError(f"time stop must be between {tc.hhmm(first)} and {tc.hhmm(last)} {tc.tz_label()}, "
                         f"got {value}")
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
    if tc.is_crypto_symbol(sym):
        return sym
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", sym):
        raise ValueError(f"not a ticker symbol: {value!r}")
    return sym


def order_window_problem(now: datetime, time_stop: int, ttl_minutes: Optional[int] = None) -> Optional[str]:
    """Why a paper order can't be placed now (None = fine). With ttl_minutes: ticket creation.
    Crypto never closes: only its time stop (default 23:55 UTC) applies."""
    m = minute_f(now)
    tz, opens = tc.tz_label(), tc.hhmm(tc.MARKET_OPEN)
    if not tc.market().every_day and now.weekday() >= 5:
        return (f"the market is closed ({now:%A}); paper orders fill only on weekdays "
                f"{opens}-{tc.hhmm(time_stop)} {tz} - create the ticket during the session")
    if m >= time_stop:
        return (f"it is {now:%H:%M} {tz}, at or past the {tc.hhmm(time_stop)} {tz} time stop - no new paper "
                "day trades after it")
    if ttl_minutes is None and m < tc.MARKET_OPEN:
        return f"pre-market ({now:%H:%M} {tz}): paper fills start at the {opens} {tz} open"
    if ttl_minutes is not None and m + ttl_minutes <= tc.MARKET_OPEN:
        return (f"pre-market ({now:%H:%M} {tz}): a ticket made now expires in {ttl_minutes} min, before "
                f"the {opens} {tz} open - create it closer to the open")
    return None


def preview_banner(now: datetime, cutoff: int, close_min: Optional[int] = None) -> str:
    """The last line of a ticket made outside the session: a preview, nothing to confirm."""
    m = minute_f(now)
    trading_day = tc.market().every_day or now.weekday() < 5
    tz, opens = tc.tz_label(), f"{tc.hhmm(tc.MARKET_OPEN)} {tc.tz_label()}"
    if trading_day and tc.MARKET_OPEN <= m < (close_min or tc.MARKET_CLOSE):
        head = f"PAST THE {tc.hhmm(cutoff)} {tz} TIME STOP"
    else:
        head = "MARKET CLOSED"
    if tc.market().every_day:            # 24/7: the next session starts at the coming midnight
        when = f"after {opens}"
    else:
        when = f"after {opens}" if trading_day and m < tc.MARKET_OPEN else f"after {opens} on the next trading day"
    return f"{head} — preview only. Re-run ticket {when} to get a ticket you can confirm."


def price_arg(x: float, tick: Optional[float] = None) -> str:
    """A price as a command-line argument (no thousands separators; crypto: the tick's decimals)."""
    if tick:
        return f"{x:.{decimals_of(tick)}f}"
    return f"{x:.2f}" if abs(x) >= 1 else f"{x:.4f}"


def tm_display(tm: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Time-machine odds as the time machine prints them: its saved one-decimal `display_pct`
    (largest-remainder rounded, adds up to 100.0), else the raw *_pct values (shown with :.1f)."""
    disp = tm.get("display_pct") if isinstance(tm.get("display_pct"), dict) else {}
    out: Dict[str, Optional[float]] = {}
    for key in ("target_first", "stop_first", "open_at_close"):
        for v in (disp.get(key), tm.get(f"{key}_pct")):
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                out[key] = float(v)
                break
        else:
            out[key] = None
    return out


def fmt_odds(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.1f}%"


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
                  session_end: Optional[int] = None, tick: Optional[float] = None) -> Dict[str, Any]:
    """Replay an open paper trade over completed 5-minute bars of its own session.

    Returns {"status": "closed", reason, price, ts, ambiguous, note} | {"status": "open",
    checked_through} | {"status": "issue", issue}. `bars` must be normalize_bars() output.
    Day trades never carry overnight: the bar that ends at or after the time stop closes the
    trade at its close ("eod"); if the session ended early, the last bar of the day does.
    Runs in the active market (a crypto trade's session is its UTC day). `tick`: see px().
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
            return {"status": "closed", "reason": hit[0], "price": px(hit[1], tick), "ts": b.ts + BAR,
                    "ambiguous": hit[2], "note": None}
        if tc.minute_of_day(b.ts) + 5 >= t_eff:
            return {"status": "closed", "reason": "eod", "price": px(b.close, tick), "ts": b.ts + BAR,
                    "ambiguous": False, "note": None}
        last = b
    if session_over(day, now_ts, session_end):
        if last is not None:
            return {"status": "closed", "reason": "eod", "price": px(last.close, tick), "ts": last.ts + BAR,
                    "ambiguous": False,
                    "note": f"no bar reached the {tc.hhmm(t_eff)} time stop on {day}; closed at the "
                            f"last bar ({tc.et_hhmm(last.ts + BAR)} {tc.tz_label()})"}
        return {"status": "issue",
                "issue": f"no 5-minute bars on {day} after the fill - close it by hand with --price"}
    return {"status": "open", "checked_through": (last.ts + BAR) if last else None}


def walk_working(side: str, limit: float, confirmed_ts: float, bars: Sequence[tc.Bar], now_ts: float,
                 time_stop: int, session_end: Optional[int] = None, tick: Optional[float] = None) -> Dict[str, Any]:
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
                return {"status": "filled", "price": px(limit, tick), "ts": confirmed_ts, "bar": b.ts}
        elif side == "long" and b.low < limit:
            return {"status": "filled", "price": px(min(limit, b.open), tick), "ts": b.ts, "bar": b.ts}
        elif side == "short" and b.high > limit:
            return {"status": "filled", "price": px(max(limit, b.open), tick), "ts": b.ts, "bar": b.ts}
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
    """Record a filled ticket as an open paper trade (and link the two). Runs in the ticket's
    market. A crypto fill pays the ticket's fee_pct on its notional (`entry_fee`, `fees`), and the
    trade keeps the ticket's exchange: its exits are priced there too."""
    tick = row_tick(k)
    trade = {
        "id": next_trade_id(led, tc.et_date(ts)), "ticket_id": k["id"], "symbol": k["symbol"],
        "market": row_market(k).key,
        "side": k["side"], "qty": row_qty(k), "entry_price": px(price, tick), "stop": float(k["stop"]),
        "target": None if k.get("target") is None else float(k["target"]),
        "setup": k.get("setup"), "order_type": k.get("type", "limit"), "planned_entry": k.get("entry"),
        "time_stop": k.get("time_stop") or default_time_stop(), "time_machine": k.get("time_machine"),
        "mode": "paper", "opened_at": iso(ts), "status": "open", "exit_price": None,
        "closed_at": None, "exit_reason": None, "pnl": None, "r_multiple": None,
    }
    if is_crypto(k):
        rate = fee_rate(k)
        entry_fee = fee_amount(rate, trade["entry_price"], trade["qty"])
        trade.update(exchange=row_exchange(k), tick=tick, fee_pct=round(rate * 100, 6), entry_fee=entry_fee,
                     exit_fee=None, fees=entry_fee, gross_pnl=None)
    led["trades"].append(trade)
    k.update(status="confirmed", trade_id=trade["id"], filled_at=trade["opened_at"], fill_price=trade["entry_price"])
    return trade


def close_trade(t: Dict[str, Any], price: float, stamp: str, reason: str, ambiguous: bool = False) -> None:
    """Record the exit. Crypto: the exit fill pays its fee too; pnl and r_multiple are net of both
    fees (gross_pnl - fees), R = pnl / the price risk at the stop."""
    t["exit_price"] = px(price, row_tick(t))
    t["closed_at"] = stamp
    t["exit_reason"] = reason
    t["status"] = "closed"
    pnl = tc.trade_pnl(t, t["exit_price"])
    risk = tc.trade_initial_risk(t)
    if is_crypto(t):
        rate = fee_rate(t)
        entry_fee = t.get("entry_fee")
        if isinstance(entry_fee, bool) or not isinstance(entry_fee, (int, float)) or not math.isfinite(entry_fee):
            entry_fee = fee_amount(rate, t["entry_price"], t["qty"])
        t["exit_fee"] = fee_amount(rate, t["exit_price"], t["qty"])
        t["fees"] = round(float(entry_fee) + t["exit_fee"], 2)
        t["gross_pnl"] = round(pnl, 2)
        pnl = t["gross_pnl"] - t["fees"]
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
            and local_day(k.get("confirmed_at")) == day]


def working_rows(orders: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Working orders as open trades at their limit (how the budget counts them)."""
    return [{"id": k.get("id"), "symbol": k.get("symbol"), "market": k.get("market"), "fee_pct": k.get("fee_pct"),
             "side": k.get("side"), "qty": k.get("qty"), "entry_price": k.get("entry"), "stop": k.get("stop"),
             "status": "open", "opened_at": k.get("confirmed_at")} for k in orders]


def budget_rows(trades: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Trades as the shared budget check should count them (copies; pure):

    * stamps in the active market's zone, so tc.day_summary's day and loss-streak order hold
      when stock (ET) and crypto (UTC) stamps share the ledger;
    * crypto: the stop moved by the fees of both fills, so tc.trade_initial_risk - the open risk -
      is the whole loss if the stop fills, fees included (stocks are unchanged)."""
    out = []
    for t in trades:
        c = dict(t)
        for field in ("opened_at", "closed_at"):
            ts = parse_ts(c.get(field))
            if ts is not None:
                c[field] = iso(ts)
        rate = fee_rate(c)
        if rate > 0:
            try:
                e, s = float(c["entry_price"]), float(c["stop"])
            except (KeyError, TypeError, ValueError):
                pass                 # malformed: tc.trade_initial_risk reports it
            else:
                pad = rate * (e + s)
                c["stop"] = s - pad if c.get("side") == "long" else s + pad
        out.append(c)
    return out


def budget_ledger(led: Dict[str, Any], day: str, exclude: Optional[str] = None) -> Dict[str, Any]:
    """The ledger as tc.budget_check should see it: today's working orders reserve their risk and
    a trade slot (tc.day_summary only counts trades, so it would miss them)."""
    synth = working_rows(working_orders(led, day, exclude))
    return {"tickets": [], "trades": budget_rows(rows(led, "trades") + synth)}


def working_risk(orders: Sequence[Dict[str, Any]]) -> float:
    """Risk reserved by working orders, counted exactly as tc.day_summary counts open risk
    (directional: 0 if a hand-edited stop sits past the limit; crypto fees included)."""
    return round(sum(tc.trade_initial_risk(r) for r in budget_rows(working_rows(orders))), 2)


def safe_working_risk(orders: Sequence[Dict[str, Any]]) -> float:
    """working_risk for display: a malformed order counts 0 here (the budget check refuses it)."""
    total = 0.0
    for k in orders:
        try:
            total += working_risk([k])
        except tc.StateError:
            continue
    return round(total, 2)


def check_budget(led: Dict[str, Any], new_risk: float, settings: Dict[str, Any], day: str,
                 exclude: Optional[str] = None) -> Dict[str, Any]:
    try:
        res = tc.budget_check(new_risk, settings=settings, ledger=budget_ledger(led, day, exclude), day=day)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise tc.StateError(f"ledger.json has a malformed trade or order ({e!r}) - fix or remove it") from None
    wk = working_orders(led, day, exclude)
    res["working_orders"] = len(wk)
    res["working_risk"] = working_risk(wk)
    return res


# ---------------------------------------------------------------------------
# Ticket construction (pure)
# ---------------------------------------------------------------------------

def _plan_levels_match(plan: Optional[Dict[str, Any]], side: str, entry: float, stop: float,
                       target: Optional[float] = None, with_target: bool = False,
                       tick: Optional[float] = None) -> bool:
    if not isinstance(plan, dict) or plan.get("side") not in (None, side):
        return False
    ok = same_level(plan.get("entry"), entry, tick) and same_level(plan.get("stop"), stop, tick)
    if with_target:
        pt = plan.get("target")
        ok = ok and ((pt is None and target is None) or same_level(pt, target, tick))
    return ok


def untested_note(symbol: str, plan: Optional[Dict[str, Any]], side: str, entry: float, stop: float,
                  target: Optional[float], stale: bool = False, tick: Optional[float] = None) -> str:
    """The note for levels the time machine has not tested (no odds, or odds for other levels)."""
    if target is None:
        return "not tested by the time machine for these levels (it needs a target to measure the odds)"
    if _plan_levels_match(plan, side, entry, stop, target, with_target=True, tick=tick):
        cmd = f"time_machine.py {symbol} --save"
    else:
        cmd = (f"time_machine.py {symbol} --entry {price_arg(entry, tick)} --stop {price_arg(stop, tick)} "
               f"--target {price_arg(target, tick)} --save")
    return (f"not tested by the time machine for these levels (run {cmd})"
            + (" - the plan's odds were for other levels" if stale else ""))


def resolve_order(symbol: str, plan: Optional[Dict[str, Any]], *, today: str, side: Optional[str] = None,
                  entry: Optional[float] = None, stop: Optional[float] = None,
                  target: Optional[float] = None, qty: Optional[float] = None, order_type: str = "limit",
                  setup: Optional[str] = None, time_stop: Optional[str] = None,
                  last_price: Optional[float] = None, tick: Optional[float] = None,
                  step: Optional[float] = None, fee_pct: float = 0.0) -> Dict[str, Any]:
    """Work out the order from flags or the saved plan. Levels come ALL from flags or ALL from the
    plan (never mixed). Raises ValueError with an actionable message on bad input.

    Crypto (runs in its market): `tick` is the product's price tick (None = unknown: prices are not
    checked against it), `step` its size step, `fee_pct` the paper fee each fill will pay."""
    warnings: List[str] = []
    notes: List[str] = []
    market = tc.market_for(symbol)
    crypto = market.key == "crypto"
    ptick = (tick or CRYPTO_FALLBACK_TICK) if crypto else None     # what prices are rounded to
    unit = unit_word(symbol)
    if plan is not None and not isinstance(plan, dict):
        raise tc.StateError(f"the saved {symbol} plan is not an object; fix or delete it")

    def bad_tick(v: float) -> bool:
        return off_tick(v, tick) if crypto and tick else (not crypto and sub_penny(v))

    if any(v is not None for v in (entry, stop, target)):
        source = "flags"
        if stop is None:
            raise ValueError("--stop is required with explicit levels (every paper ticket needs a stop)")
        for name, v in (("entry", entry), ("stop", stop), ("target", target)):
            if v is not None and bad_tick(v):
                if crypto:
                    raise ValueError(f"--{name} {v} is not on {symbol}'s price tick {step_text(tick)} "  # type: ignore[arg-type]
                                     f"(e.g. {price_arg(px(v, tick), tick)})")
                raise ValueError(f"--{name} {v:g} has sub-penny digits; stocks at $1 and up trade in "
                                 f"cents (e.g. {px(v):.2f})")
        if entry is None:
            if order_type != "market":
                raise ValueError("--entry (your limit price) is required for a limit order")
            if last_price is None:
                raise ValueError("a market ticket without --entry needs the current price")
            entry = px(last_price, ptick)
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
        rounded = {n: px(v, ptick) for n, v in (("entry", entry), ("stop", stop), ("target", target))
                   if v is not None and bad_tick(v)}
        if rounded and crypto:
            notes.append(f"plan prices rounded to the {step_text(tick)} tick: "  # type: ignore[arg-type]
                         + ", ".join(f"{n} {price_arg(v, tick)}" for n, v in rounded.items()))
        elif rounded:
            notes.append("plan prices rounded to cents: " + ", ".join(f"{n} {v:.2f}" for n, v in rounded.items()))
        if rounded:
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
    step = float(step) if step else tc.qty_step(market)
    sizing = plan.get("sizing") if isinstance(plan, dict) else None
    if qty is None:
        if isinstance(sizing, dict) and sizing.get("side", side) == side \
                and same_level(sizing.get("entry"), entry, tick) and same_level(sizing.get("stop"), stop, tick):
            try:
                raw = float(sizing.get("shares") or 0)
                qty = (floor_step(raw, step) if crypto else int(raw)) if math.isfinite(raw) else 0
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0 if crypto else qty < 1:
                raise ValueError(f"the saved {symbol} sizing is 0 {unit} (the stop is too wide for the risk "
                                 "budget); give --qty or re-plan the stop")
            qty_source = "plan sizing"
        elif isinstance(sizing, dict):
            raise ValueError(f"the saved {symbol} sizing was for other levels; give --qty, or re-size: "
                             f"risk.py size --symbol {symbol} --save")
        elif crypto:
            raise ValueError(f"give --qty ({unit}, fractions allowed, e.g. 0.01), or size the trade first: "
                             f"risk.py size --symbol {symbol} --save")
        else:
            raise ValueError(f"give --qty (shares), or size the trade first: risk.py size --symbol {symbol} --save")
    if crypto:
        check_crypto_qty(qty, symbol)
        if off_step(float(qty), step):
            floored = floor_step(float(qty), step)
            hint = f"e.g. {tc.fmt_qty(floored)}" if floored > 0 else f"the smallest size is {step_text(step)}"
            typed = format(Decimal(repr(float(qty))), "f")          # 0.000015, not 1.5e-05
            raise ValueError(f"--qty {typed} is not a multiple of {symbol}'s size step {step_text(step)} ({hint})")
        qty = float(qty)
    elif qty != int(qty) or qty < 1 or qty > MAX_QTY:
        raise ValueError(f"--qty must be a whole number of shares from 1 to {MAX_QTY:,}")

    # Plan extras that belong to these levels: setup label, time stop, time-machine odds.
    matches = _plan_levels_match(plan, side, entry, stop, tick=tick)
    if setup is None and matches:
        setup = plan.get("setup") or None  # type: ignore[union-attr]
    ts_value = time_stop
    if ts_value is None and matches:
        ts_value = ((plan.get("exit_plan") or {}) if isinstance(plan.get("exit_plan"), dict) else {}).get("time_stop")  # type: ignore[union-attr]
        if ts_value is not None:
            try:
                parse_time_stop(ts_value)
                notes.append(f"time stop {ts_value} {tc.tz_label()} from the saved exit plan")
            except ValueError:
                first, last = time_stop_range()
                notes.append(f"ignored the plan's time stop {ts_value!r} (not {tc.hhmm(first)}-{tc.hhmm(last)})")
                ts_value = None
    t_stop = parse_time_stop(ts_value or default_time_stop())
    tm = None
    tm_plan = plan.get("time_machine") if isinstance(plan, dict) else None
    if isinstance(tm_plan, dict) and _plan_levels_match(tm_plan, side, entry, stop, target, with_target=True, tick=tick):
        tm = {k: tm_plan.get(k) for k in ("verdict", "target_first_pct", "stop_first_pct",
                                          "open_at_close_pct", "expected_r", "as_of")}
        if isinstance(tm_plan.get("display_pct"), dict):
            tm["display_pct"] = tm_plan["display_pct"]
    else:
        notes.append(untested_note(symbol, plan, side, entry, stop, target, isinstance(tm_plan, dict), ptick))
    out = {"symbol": symbol, "market": market.key, "side": side, "qty": qty if crypto else int(qty),
           "type": order_type, "entry": float(entry),
           "stop": float(stop), "target": None if target is None else float(target),
           "setup": setup, "time_stop": tc.hhmm(t_stop), "time_stop_min": t_stop,
           "levels_source": source, "qty_source": qty_source, "time_machine": tm,
           "warnings": warnings, "notes": notes}
    if crypto:
        out.update(tick=ptick, qty_step=step, fee_pct=float(fee_pct))
    return out


def check_crypto_qty(qty: Any, symbol: str) -> None:
    if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty) or not 0 < qty <= MAX_QTY:
        raise ValueError(f"--qty must be a positive amount of {unit_word(symbol)} (fractions allowed, e.g. 0.01) "
                         f"up to {MAX_QTY:,}")


def order_fees(order: Dict[str, Any]) -> Tuple[float, float, float]:
    """(fee rate, fees if the stop fills, loss at the stop incl. fees) of an order dict. Stocks: (0, 0, risk)."""
    rate = fee_rate(order)
    e, s, q = float(order["entry"]), float(order["stop"]), float(order["qty"])
    return rate, rate * (e + s) * q, max_loss(order["side"], e, s, q, rate)


def order_checks(order: Dict[str, Any], settings: Dict[str, Any], last: Optional[Dict[str, Any]],
                 today: str, preview: bool = False) -> Tuple[List[str], List[str]]:
    """Warnings and notes about a proposed order (pure). Raises ValueError for hard limits.
    `preview`: the market is closed, so the last price is the last session's and nothing fills now."""
    warnings: List[str] = []
    notes: List[str] = []
    side, e, s, t, q = order["side"], order["entry"], order["stop"], order["target"], order["qty"]
    crypto = is_crypto(order)
    ex = order_exchange(order)
    rate, fees_at_stop, loss = order_fees(order)
    notional = e * q
    acct = settings["account_size"]
    if acct > 0 and notional > MAX_LEVERAGE * acct + 1e-9:
        raise ValueError(f"notional {tc.fmt_money(notional)} is more than {MAX_LEVERAGE:g}x the "
                         f"{tc.fmt_money(acct)} paper account - reduce --qty")
    if acct > 0 and notional > acct + 1e-9:
        warnings.append(f"notional {tc.fmt_money(notional)} is more than the {tc.fmt_money(acct)} paper "
                        + ("account - spot crypto is paid in full (no margin)" if crypto else "account - this needs margin"))
    elif acct > 0 and notional > acct * settings["max_position_pct"] / 100 + 1e-9:
        warnings.append(f"notional is {notional / acct * 100:.0f}% of the account, over your "
                        f"{settings['max_position_pct']:g}% max position size")
    min_funds = _positive_number(order.get("min_funds"))      # Coinbase min_market_funds / Binance NOTIONAL
    if crypto and min_funds and notional < min_funds:
        warnings.append(f"notional {tc.fmt_money(notional)} is below {exchange_label(ex)}'s "
                        f"{min_order_text(min_funds, order['symbol'], ex)} minimum order for {order['symbol']} - "
                        "a real order this small would be rejected")
    budget = acct * settings["risk_per_trade_pct"] / 100
    if budget > 0 and loss > budget * 1.01 + 0.01:
        fees = f" (incl. {tc.fmt_money(fees_at_stop)} of fees at the stop)" if fees_at_stop else ""
        warnings.append(f"risk {tc.fmt_money(loss)}{fees} is over your per-trade risk budget {tc.fmt_money(budget)} "
                        f"({settings['risk_per_trade_pct']:g}% of the account) - the risk skill sizes to it")
    if t is None:
        warnings.append("no target: the trade exits only at the stop, by hand, or at the time stop")
    else:
        rr = tc.reward_risk(side, e, s, t)
        if rr < settings["min_reward_risk"] - 1e-9:
            warnings.append(f"R:R {rr:.2f} is below your minimum {settings['min_reward_risk']:g}")
        elif rate and loss > 0 and (abs(t - e) * q - rate * (e + t) * q) / loss < settings["min_reward_risk"] - 1e-9:
            warnings.append(f"after fees the R:R is {(abs(t - e) * q - rate * (e + t) * q) / loss:.2f} (reward net "
                            f"of fees / loss at the stop incl. fees), below your minimum {settings['min_reward_risk']:g}")
    if crypto:
        notes.append(fee_note(order))
        if side == "short":
            notes.append(f"spot crypto cannot be sold short on {exchange_label(ex)} (that needs margin or "
                         "derivatives); the paper short is simulated like a stock short")
    if last:
        p = last["price"]
        qd = last.get("date")
        if qd and qd != today:
            notes.append(f"latest {order['symbol']} quote is from {qd} "
                         + ("(the last session's price)" if preview else "(holiday, halt or delayed data?)"))
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
                             f"{tc.fmt_price(p)}: " + ("at that price a confirmed ticket would fill right away"
                                                       if preview else
                                                       f"on yes it fills right away at about {tc.fmt_price(p)}"))
            else:
                notes.append(f"last {tc.fmt_price(p)} is {'above' if side == 'long' else 'below'} your limit: "
                             + ("at that price a confirmed ticket would rest" if preview else "on yes it rests")
                             + " as a working day order until a 5-minute bar trades through "
                             f"{tc.fmt_price(e)}")
    return warnings, notes


def fee_note(order: Dict[str, Any]) -> str:
    """The crypto fee line: what the paper fees cost, in dollars and in R, at these levels."""
    rate, fees_at_stop, _ = order_fees(order)
    e, s, t, q = order["entry"], order["stop"], order["target"], order["qty"]
    if not rate:
        return "paper fees are off (crypto_fee_pct is 0) - a real exchange charges a fee on every fill"
    risk = abs(e - s) * q
    parts = [f"paper fees {rate * 100:g}% per fill (crypto_fee_pct): round-trip fees ≈ "
             f"{fees_at_stop / risk if risk > 0 else 0:.2f}R at these levels",
             f"{tc.fmt_money(fees_at_stop)} if the stop fills"]
    if t is not None:
        parts.append(f"{tc.fmt_money(rate * (e + t) * q)} at the target")
    return " · ".join(parts) + " - P&L and R are net of fees"


def preview_order(order: Dict[str, Any]) -> Dict[str, Any]:
    """The ticket's order fields without an id, status or expiry (also what a preview shows).
    `risk` is the price risk |entry - stop| x qty; crypto adds `fees_at_stop` and `max_loss` (the
    loss if the stop fills, fees included - what the budget counts)."""
    e, s, t = order["entry"], order["stop"], order["target"]
    out = {
        "symbol": order["symbol"], "market": order.get("market") or row_market(order).key,
        "side": order["side"], "qty": order["qty"], "type": order["type"],
        "entry": e, "stop": s, "target": t, "setup": order["setup"], "time_stop": order["time_stop"],
        "risk": round(abs(e - s) * order["qty"], 2),
        "rr": None if t is None else round(tc.reward_risk(order["side"], e, s, t), 2),
        "time_machine": order["time_machine"],
        "levels_source": order["levels_source"], "qty_source": order["qty_source"],
    }
    if is_crypto(order):
        rate, fees_at_stop, loss = order_fees(order)
        out.update(exchange=order_exchange(order), tick=order.get("tick"), qty_step=order.get("qty_step"),
                   fee_pct=round(rate * 100, 6), fees_at_stop=round(fees_at_stop, 2), max_loss=round(loss, 2))
    return out


def new_ticket(led: Dict[str, Any], order: Dict[str, Any], now: datetime, settings: Dict[str, Any],
               session_end: Optional[int] = None) -> Dict[str, Any]:
    """A pending ticket; it expires after the TTL or at the (early-close adjusted) time stop."""
    today = now.strftime("%Y-%m-%d")
    cutoff = effective_time_stop(order["time_stop_min"], session_end)
    expires = min(now.timestamp() + settings["ticket_ttl_minutes"] * 60, at_minute(today, cutoff))
    ticket = {"id": tc.new_id("TKT", [str(k.get("id", "")) for k in rows(led, "tickets")])}
    ticket.update(preview_order(order))
    ticket.update(status="pending", mode="paper", created_at=iso(now.timestamp()), expires_at=iso(expires),
                  trade_id=None)
    return ticket


def symbol_exposure(led: Dict[str, Any], symbol: str, today: str) -> List[Dict[str, Any]]:
    """Open trades and today's working orders in `symbol` (crypto: with the exchange that priced them)."""
    out = [dict(kind="trade", id=t.get("id"), side=t.get("side"), qty=t.get("qty"), exchange=row_exchange(t))
           for t in rows(led, "trades") if t.get("status") == "open" and str(t.get("symbol", "")).upper() == symbol]
    out += [dict(kind="working order", id=k.get("id"), side=k.get("side"), qty=k.get("qty"), exchange=row_exchange(k))
            for k in working_orders(led, today) if str(k.get("symbol", "")).upper() == symbol]
    return out


# ---------------------------------------------------------------------------
# Sync (pure apply + I/O fetch)
# ---------------------------------------------------------------------------

def sync_targets(led: Dict[str, Any], now: datetime, only: Optional[Set[str]] = None) -> Dict[FeedKey, Optional[str]]:
    """{(symbol, exchange): bar range to fetch (None = too old for 5-minute data)} for open/working
    items; the exchange is None for stocks, else the one that priced the row. Stocks: Yahoo's 5d /
    60d. Crypto: just the UTC days since the oldest item (the exchanges page their 5-minute candles)."""
    oldest: Dict[FeedKey, float] = {}
    items = [(k, "confirmed_at") for k in rows(led, "tickets") if k.get("status") == "working"]
    items += [(t, "opened_at") for t in rows(led, "trades") if t.get("status") == "open"]
    for row, field in items:
        if only is not None and str(row.get("id", "")).upper() not in only:
            continue
        ts = parse_ts(row.get(field))
        key = feed_key(row)
        if ts is None or not key[0]:
            continue
        oldest[key] = min(oldest.get(key, ts), ts)
    out: Dict[FeedKey, Optional[str]] = {}
    for key, ts in oldest.items():
        with tc.market_scope(key[0]):
            age = (tc.to_et(now.timestamp()).date() - tc.to_et(ts).date()).days
            if tc.market().key == "crypto":
                out[key] = f"{max(age, 0) + 2}d" if age <= MAX_SYNC_AGE_DAYS else None
            else:
                out[key] = "5d" if age <= 4 else ("60d" if age <= MAX_SYNC_AGE_DAYS else None)
    return out


@contextmanager
def same_instant(now: Optional[datetime]) -> Iterator[None]:
    """Pin a naive $DAYTRADER_NOW (read in each active market's own zone: ET, UTC for crypto) to
    the command's instant while another market's bars are fetched, so a replay's crypto candles
    stop at the same 'now' as the command (e.g. a US-view sync of a crypto trade)."""
    old = os.environ.get("DAYTRADER_NOW")
    if old is None or now is None:
        yield
        return
    os.environ["DAYTRADER_NOW"] = now.isoformat()
    try:
        yield
    finally:
        os.environ["DAYTRADER_NOW"] = old


def fetch_sync_data(targets: Dict[FeedKey, Optional[str]], now: Optional[datetime] = None
                    ) -> Tuple[Dict[FeedKey, Tuple[List[tc.Bar], Dict[str, Any]]], Dict[FeedKey, str]]:
    """5-minute bars per feed: stocks from Yahoo, crypto from the exchange that priced the rows."""
    data: Dict[FeedKey, Tuple[List[tc.Bar], Dict[str, Any]]] = {}
    errors: Dict[FeedKey, str] = {}
    for key, rng in sorted(targets.items(), key=feed_sort):
        sym, ex = key
        if exchange_problem(ex):
            errors[key] = exchange_problem(ex)  # type: ignore[assignment]
            continue
        with same_instant(now), tc.market_scope(sym), exchange_scope(ex):
            if rng is None:
                errors[key] = (f"opened more than {MAX_SYNC_AGE_DAYS} days ago - "
                               + ("too old to replay from 5-minute bars" if tc.market().key == "crypto" else
                                  "Yahoo keeps only 60 days of 5-minute bars")
                               + "; close it by hand with --price")
                continue
            try:
                bars, meta = tc.fetch_bars(sym, "5m", rng)
                data[key] = (normalize_bars(bars), meta or {})
            except tc.FetchError as e:
                errors[key] = str(e)
    return data, errors


def apply_sync(led: Dict[str, Any], data: Dict[FeedKey, Tuple[List[tc.Bar], Dict[str, Any]]], now: datetime,
               only: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Fill working orders and close open trades in `led` (mutated) from pre-fetched bars. Pure.

    Each row is replayed in its own market (a crypto order's session is its UTC day), whatever
    market the command runs in, on the bars of its own feed (`feed_key`: a crypto row only ever
    sees the candles of the exchange that priced it). With `only` (ids), a working order in it that
    fills brings its new trade along, so the trade's exits are applied in the same pass."""
    now_ts = now.timestamp()
    out: Dict[str, Any] = {"events": [], "open": [], "working": [], "issues": []}
    only = None if only is None else set(only)

    def wanted(row: Dict[str, Any]) -> bool:
        return only is None or str(row.get("id", "")).upper() in only

    def sync_working(k: Dict[str, Any]) -> None:
        sym = str(k.get("symbol") or "").upper()
        conf = parse_ts(k.get("confirmed_at"))
        try:
            side, limit = str(k["side"]), float(k["entry"])
            float(k["stop"]), row_qty(k)
            t_stop = parse_time_stop(k.get("time_stop") or default_time_stop())
            if conf is None or side not in ("long", "short"):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            out["issues"].append({"id": k.get("id"), "symbol": sym, "issue": "malformed working order - cancel it"})
            return
        if feed_key(k) not in data:
            return
        bars, meta = data[feed_key(k)]
        r = walk_working(side, limit, conf, bars, now_ts, t_stop, session_end_minute(meta, tc.et_date(conf)),
                         row_tick(k))
        mkt = row_market(k).key
        src = {"exchange": row_exchange(k)} if is_crypto(k) else {}
        if r["status"] == "filled":
            trade = open_trade(led, k, r["price"], r["ts"])
            if only is not None:
                only.add(trade["id"].upper())
            out["events"].append({"kind": "filled", "ticket_id": k["id"], "trade_id": trade["id"], "symbol": sym,
                                  "market": mkt, **src, "side": side, "qty": trade["qty"],
                                  "price": trade["entry_price"], "at": trade["opened_at"], "bar": iso(r["bar"])})
        elif r["status"] == "expired":
            k.update(status="expired", expired_at=iso(r["ts"]))
            out["events"].append({"kind": "expired", "ticket_id": k["id"], "symbol": sym, "market": mkt, **src,
                                  "side": side, "qty": k.get("qty"), "price": limit, "at": k["expired_at"]})
        else:
            k["synced_at"] = iso(now_ts)
            out["working"].append({"ticket_id": k["id"], "symbol": sym, "market": mkt, **src, "side": side,
                                   "qty": k.get("qty"), "limit": limit,
                                   "checked_through": r["checked_through"] and iso(r["checked_through"])})

    def sync_trade(t: Dict[str, Any]) -> None:
        sym = str(t.get("symbol") or "").upper()
        opened = parse_ts(t.get("opened_at"))
        try:
            side, fill, stop = str(t["side"]), float(t["entry_price"]), float(t["stop"])
            target = None if t.get("target") is None else float(t["target"])
            float(t["qty"])
            t_stop = parse_time_stop(t.get("time_stop") or default_time_stop())
            if opened is None or side not in ("long", "short"):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            out["issues"].append({"id": t.get("id"), "symbol": sym,
                                  "issue": "malformed trade - fix it in ledger.json or close it with --price"})
            return
        if feed_key(t) not in data:
            return
        bars, meta = data[feed_key(t)]
        r = walk_position(side, fill, stop, target, opened, bars, now_ts, t_stop,
                          session_end_minute(meta, tc.et_date(opened)), row_tick(t))
        mkt = row_market(t).key
        src = {"exchange": row_exchange(t)} if is_crypto(t) else {}
        if r["status"] == "closed":
            close_trade(t, r["price"], iso(r["ts"]), r["reason"], r["ambiguous"])
            ev = {"kind": "closed", "trade_id": t["id"], "symbol": sym, "market": mkt, **src, "side": side,
                  "qty": t["qty"], "reason": r["reason"], "price": t["exit_price"], "at": t["closed_at"],
                  "bar": iso(r["ts"] - BAR), "pnl": t["pnl"],
                  "r_multiple": t["r_multiple"], "ambiguous": r["ambiguous"]}
            if is_crypto(t):
                ev.update(gross_pnl=t["gross_pnl"], fees=t["fees"])
            if r.get("note"):
                ev["note"] = r["note"]
            out["events"].append(ev)
        elif r["status"] == "issue":
            out["issues"].append({"id": t["id"], "symbol": sym, "issue": r["issue"]})
        else:
            t["synced_at"] = iso(now_ts)
            out["open"].append({"trade_id": t["id"], "symbol": sym, "market": mkt, **src, "side": side,
                                "qty": t["qty"], "entry_price": fill, "stop": stop, "target": target,
                                "checked_through": r["checked_through"] and iso(r["checked_through"])})

    for k in rows(led, "tickets"):
        if k.get("status") == "working" and wanted(k):
            with tc.market_scope(row_market(k)):
                sync_working(k)
    for t in rows(led, "trades"):
        if t.get("status") == "open" and wanted(t):
            with tc.market_scope(row_market(t)):
                sync_trade(t)
    return out


def prefetch_sync(now: datetime, only: Optional[Set[str]] = None) -> Tuple[Dict[FeedKey, Any], Dict[FeedKey, str]]:
    """Fetch bars for open/working items outside the ledger lock (network can be slow)."""
    return fetch_sync_data(sync_targets(load_ledger(), now, only), now)


def sync_notes(res: Dict[str, Any], errors: Dict[FeedKey, str]) -> List[str]:
    out = [f"sync: {event_text(e)}" for e in res["events"]]
    out += [f"could not sync {e['symbol']}: {e['error']}" for e in feed_errors(errors)]
    out += [f"{i['id']} {i['symbol']}: {i['issue']}" for i in res["issues"]]
    return out


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def get_quote(symbol: str) -> Dict[str, Any]:
    """tc.quote plus the quote's date/age in the active market's zone (None without a timestamp)."""
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
    None. Best-effort, and the same request tc.quote makes (cached 15 s), so normally no extra fetch.
    Crypto never closes early (and has no trading periods): None without a fetch."""
    if tc.is_crypto_symbol(symbol):
        return None
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


def product_rules(symbol: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The configured exchange's size step, price tick and minimum order for a crypto pair (public
    data, cached a day; Binance LOT_SIZE / PRICE_FILTER / NOTIONAL, Coinbase base_increment /
    quote_increment / min_market_funds), or (None, why) when it cannot be reached."""
    try:
        return tc.product_info(symbol), None
    except tc.FetchError as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_ticket(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    sym = parse_symbol(args.symbol)
    market = tc.use_market_for(sym)            # crypto: the UTC clock, 24/7, fractional sizes, fees
    crypto = market.key == "crypto"
    ex = tc.crypto_exchange() if crypto else None   # the exchange this ticket is priced on, for its whole life
    now = tc.now_et()
    today = now.strftime("%Y-%m-%d")
    settings = load_settings()
    if args.time_stop is not None:
        parse_time_stop(args.time_stop)
    if args.qty is not None:
        if crypto:
            check_crypto_qty(args.qty, sym)
        elif args.qty != int(args.qty) or not 1 <= args.qty <= MAX_QTY:
            raise ValueError(f"--qty must be a whole number of shares from 1 to {MAX_QTY:,}")
    plan = tc.load_plan(sym)
    pre_warnings: List[str] = []
    pre_notes: List[str] = []
    tick = step = min_funds = None
    if crypto:
        info, info_err = product_rules(sym)
        if info:
            tick, step, min_funds = info["quote_increment"], info["base_increment"], info.get("min_market_funds")
            if info.get("trading_disabled") or str(info.get("status") or "online").lower() != "online":
                pre_warnings.append(f"{exchange_label(ex)} lists {sym} as {info.get('status') or 'unknown'}"
                                    + (" with trading disabled" if info.get("trading_disabled") else "")
                                    + " - its prices may be stale")
        else:
            pre_notes.append(f"{rules_name(ex)} unavailable ({info_err}): size step "
                             f"{step_text(tc.qty_step())} assumed, prices not checked against {sym}'s tick")
        usd = usd_pair_note(sym, ex)
        if usd:
            pre_notes.append(usd)
    quote, quote_err = best_effort_quote(sym)
    if args.order_type == "market" and args.entry is None and args.stop is not None and quote is None:
        raise tc.FetchError(f"a market ticket without --entry needs the current price: {quote_err}")
    order = resolve_order(sym, plan, today=today, side=args.side, entry=args.entry, stop=args.stop,
                          target=args.target, qty=args.qty, order_type=args.order_type, setup=args.setup,
                          time_stop=args.time_stop, last_price=quote["price"] if quote else None,
                          tick=tick, step=step, fee_pct=tc.fee_pct(market, settings))
    if crypto:
        order["exchange"] = ex
        try:
            order["min_funds"] = float(min_funds) if min_funds is not None else None
        except (TypeError, ValueError):
            order["min_funds"] = None
        sizing = plan.get("sizing") if isinstance(plan, dict) else None
        sized_on = sizing.get("exchange") if isinstance(sizing, dict) else None
        if order["qty_source"] == "plan sizing" and sized_on and sized_on != ex:
            pre_warnings.append(f"the saved {sym} sizing was made for {exchange_label(sized_on)} (its size step and "
                                f"fee); this ticket uses {exchange_label(ex)} prices - re-size it: risk.py size "
                                f"--symbol {sym} --save")
    early = early_close_today(sym, today) if quote is not None else None   # no 2nd slow retry when offline
    cutoff = effective_time_stop(order["time_stop_min"], early)
    # Outside the session (weekend, pre-open, past the time stop) everything is still checked, but
    # the result is a PREVIEW: nothing is saved, so there is nothing a yes could confirm.
    closed = order_window_problem(now, cutoff, settings["ticket_ttl_minutes"])
    warnings, notes = pre_warnings + list(order["warnings"]), list(order["notes"]) + pre_notes
    if closed:
        closed += early_close_note(early)
        if args.time_stop is None and order["time_stop"] != default_time_stop():
            closed += (f" (the {order['time_stop']} time stop comes from the saved exit plan; pass --time-stop "
                       "to choose another)")
        notes.insert(0, f"no ticket: {closed}")
    w2, n2 = order_checks(order, settings, quote, today, preview=bool(closed))
    warnings += w2
    notes += n2
    if quote is None:
        notes.append(f"live price unavailable ({quote_err}); confirm will fetch it")
    data, errors = prefetch_sync(now)
    risk = round(order_fees(order)[2], 2)         # the loss if the stop fills (crypto: fees included)
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
            other = f", priced on {exchange_label(x['exchange'])}" if crypto and x["exchange"] != ex else ""
            warnings.append(f"adds to your {x['side']} {sym} {x['kind']} ({x['id']}, "
                            f"{qty_text(dict(x, symbol=sym))} {unit_word(sym)}{other})")
        budget = check_budget(led, risk, settings, today)
        res: Dict[str, Any] = {"action": "ticket", "preview": bool(closed), "mode": "paper", "real_money": False,
                               "last_price": quote["price"] if quote else None, "budget": budget,
                               "account_size": settings["account_size"],
                               "ticket_ttl_minutes": settings["ticket_ttl_minutes"],
                               "warnings": warnings, "notes": notes, "sync": synced,
                               "disclaimer": tc.DISCLAIMER}
        if closed:
            res.update(market_closed=closed, banner=preview_banner(now, cutoff, early))
        if not budget["ok"]:
            tc.save_ledger(led)
            if closed:
                notes.append(f"checked against today's ({today}) budget; realized losses, the trade count and "
                             "the loss streak start fresh on the next session's date")
            res.update(ok=False, blocked=True, reasons=budget["reasons"],
                       order={k: v for k, v in order.items() if k not in ("warnings", "notes", "time_stop_min")})
            return res, EXIT_BLOCKED
        if closed:
            tc.save_ledger(led)                      # only what sync/expiry changed; no ticket
            k = dict(preview_order(order), status="preview", mode="paper")
            res.update(ok=True, status="preview", order=k, card=f"{order_words(k)} · {paper_label(k)} · {res['banner']}")
            return res, 0
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
    tid = args.ticket_id.strip().upper()
    k0 = find(load_ledger(), "tickets", tid)
    if k0 is not None:
        tc.set_market(row_market(k0))           # the ticket's clock: ET, or UTC for crypto (24/7)
    now = tc.now_et()
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    settings = load_settings()
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
    t_stop = parse_time_stop(k0.get("time_stop") or default_time_stop())
    window = order_window_problem(now, t_stop)
    if window:
        raise ValueError(f"{tid} not confirmed: {window}")
    sym = str(k0["symbol"]).upper()
    ex = row_exchange(k0)                     # crypto: filled on the exchange the ticket was priced on, only
    with exchange_scope(ex):
        quote = get_quote(sym)                               # FetchError -> exit 2
    if quote["date"] and quote["date"] != today:
        raise ValueError(f"{tid} not confirmed: the latest {sym} price is from {quote['date']}, not today "
                         "(market holiday, halt or stale data) - no paper fill")
    early = early_close_today(sym, today)
    cutoff = effective_time_stop(t_stop, early)
    if minute_f(now) >= cutoff:
        raise ValueError(f"{tid} not confirmed: it is {now:%H:%M} {tc.tz_label()}, at or past the {tc.hhmm(cutoff)} "
                         f"{tc.tz_label()} time stop{early_close_note(early)} - no paper fill after it")
    warnings: List[str] = []
    notes: List[str] = []
    if ex and ex != tc.crypto_exchange():
        notes.append(f"{tid} was made on {exchange_label(ex)} prices, so it fills and is managed on "
                     f"{exchange_label(ex)} (at its {fee_rate(k0) * 100:g}% fee); the crypto_exchange setting now "
                     f"says {exchange_label(None)}, which new tickets use")
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
        qty = row_qty(k)
        tick = row_tick(k)
        rate = fill_fee_rate(k, settings)                     # the fee this fill pays (0 for stocks)
        price = quote["price"]
        if k.get("type") == "market":
            fill: Optional[float] = px(price, tick)
        elif (side == "long" and price <= limit) or (side == "short" and price >= limit):
            fill = px(price, tick)     # marketable limit: fills at the market, no worse than the limit
        else:
            fill = None                # rests as a working day order
        if fill is not None:
            errs = tc.validate_levels(side, fill, stop, target)
            if errs:
                tc.save_ledger(led)
                raise ValueError(f"{tid} not filled at {tc.fmt_price(fill)}: " + "; ".join(errs) +
                                 " - the price moved through your levels; cancel it and re-plan")
        at = fill if fill is not None else limit
        risk = abs(at - stop) * qty + rate * (at + stop) * qty     # the loss if the stop fills, fees included
        budget = check_budget(led, risk, settings, today, exclude=tid)
        res: Dict[str, Any] = {"action": "confirm", "mode": "paper", "real_money": False, "quote": quote,
                               "budget": budget, "warnings": warnings, "notes": notes, "sync": synced,
                               "disclaimer": tc.DISCLAIMER}
        if not budget["ok"]:
            tc.save_ledger(led)
            res.update(ok=False, confirmed=False, blocked=True, reasons=budget["reasons"], ticket=k)
            return res, EXIT_BLOCKED
        k["confirmed_at"] = iso(now_ts)
        k["confirm_price"] = px(price, tick)
        if is_crypto(k):
            k["fee_pct"] = round(rate * 100, 6)
        trade = None
        if fill is not None:
            trade = open_trade(led, k, fill, now_ts)
            planned = abs(limit - stop) * qty + rate * (limit + stop) * qty
            if k.get("type") == "market" and risk > planned + 0.005:
                notes.append(f"market fill {tc.fmt_price(fill)} vs reference {tc.fmt_price(limit)}: risk is "
                             f"{tc.fmt_money(risk)} instead of {tc.fmt_money(planned)}")
        else:
            k["status"] = "working"
        tc.save_ledger(led)
    res.update(ok=True, confirmed=True, status="filled" if trade else "working", ticket=k, trade=trade,
               fill_price=trade["entry_price"] if trade else None)
    return res, 0


def fill_fee_rate(k: Dict[str, Any], settings: Dict[str, Any]) -> float:
    """The paper fee (a fraction) a ticket's fill pays: 0 for stocks; crypto the crypto_fee_pct
    setting - unless the ticket was priced on another exchange than the setting names now: then the
    fee the ticket was made with, so its prices and its fees come from the same exchange."""
    if not is_crypto(k):
        return 0.0
    if row_exchange(k) != tc.crypto_exchange():
        return fee_rate(k)
    return tc.fee_pct(tc.CRYPTO, settings) / 100.0


def refuse_unless_pending(k: Dict[str, Any], status: str) -> None:
    tid = k.get("id")
    if status == "pending":
        return
    if status == "expired":
        raise ValueError(f"{tid} expired at {hm(k.get('expires_at'))} {tc.tz_label()} without a yes - create a new ticket")
    if status == "cancelled":
        why = f" ({k['cancel_reason']})" if k.get("cancel_reason") else ""
        raise ValueError(f"{tid} was cancelled{why} - create a new ticket")
    if status == "confirmed":
        raise ValueError(f"{tid} is already confirmed (trade {k.get('trade_id') or '?'}) - a ticket opens at most "
                         "one position")
    if status == "working":
        raise ValueError(f"{tid} is already confirmed and working as a limit order - run sync, or cancel it")
    raise ValueError(f"{tid} has status {status!r} and cannot be confirmed")


def view_market(args: argparse.Namespace) -> tc.Market:
    """Whose clock and calendar day a symbol-less command shows (sync, positions, tickets,
    history): --market, else $DAYTRADER_MARKET, else US. Each row still runs in its own market."""
    return tc.set_market(getattr(args, "market", None) or tc.market_for(None))


def cmd_sync(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    view_market(args)
    now = tc.now_et()
    led0 = load_ledger()
    targets = sync_targets(led0, now)
    data, errors = fetch_sync_data(targets, now)
    fetch_errors = [errors[k] for k, rng in sorted(targets.items(), key=feed_sort) if rng is not None and k in errors]
    if fetch_errors and not data:
        raise tc.FetchError(fetch_errors[0])         # nothing could be checked at all
    with ledger_lock():
        led = load_ledger()
        res = apply_sync(led, data, now)
        expired = expire_pending(led, now.timestamp())
        tc.save_ledger(led)
    res.update(ok=True, action="sync", as_of=iso(now.timestamp()), expired_tickets=expired,
               errors=feed_errors(errors),
               summary=safe_summary(led, now.strftime("%Y-%m-%d")), disclaimer=tc.DISCLAIMER)
    return res, 0


def traded_range(bars: Sequence[tc.Bar], since_ts: Optional[float],
                 now_ts: float) -> Optional[Tuple[float, float]]:
    """(low, high) of the 5-minute bars from the one holding `since_ts` (the fill) through the bar in
    progress at `now_ts`, or None when there are no such bars. Pure."""
    if since_ts is None:
        return None
    seen = [b for b in bars if b.ts + BAR > since_ts and b.ts < now_ts]
    if not seen:
        return None
    return min(b.low for b in seen), max(b.high for b in seen)


def cmd_close(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    tid = args.trade_id.strip().upper()
    t0 = find(load_ledger(), "trades", tid)
    if t0 is not None:
        tc.set_market(row_market(t0))           # the trade's clock: ET, or UTC for crypto
    now = tc.now_et()
    now_ts = now.timestamp()
    if args.price is not None and (not math.isfinite(args.price) or not 0 < args.price < MAX_PRICE):
        raise ValueError(f"--price must be a positive price below {MAX_PRICE:,.0f}")
    if t0 is None:
        hint = " - that is a ticket id; use cancel" if tid.startswith("TKT-") else " - see: broker.py positions"
        raise ValueError(f"no trade {tid}{hint}")
    if t0.get("status") != "open":
        raise ValueError(f"{tid} is already closed ({t0.get('exit_reason')} at {tc.fmt_price(t0.get('exit_price'))})")
    sym = str(t0.get("symbol", "")).upper()
    feed = feed_key(t0)                       # crypto: its bars and quote come from the exchange that priced it
    notes: List[str] = []
    data, errors = prefetch_sync(now, {tid})
    if errors:
        notes.append(f"could not check stops/targets first: {errors.get(feed, next(iter(errors.values())))}")
    preview = copy.deepcopy(load_ledger())
    apply_sync(preview, data, now, {tid})
    pt = find(preview, "trades", tid)
    quote = None
    if pt is not None and pt.get("status") == "open" and args.price is not None:
        # A manual exit records real-looking P&L, and realized profit widens today's loss budget, so
        # the price must be one that traded since the fill (checked whenever the bars are available).
        span = traded_range(data.get(feed, ([], {}))[0], parse_ts(pt.get("opened_at")), now_ts)
        slack = row_tick(t0) / 2 if is_crypto(t0) else 0.005       # half a tick (stocks: half a cent)
        if span is None:
            notes.append(f"--price {tc.fmt_price(args.price)} not checked against traded prices "
                         "(no 5-minute bars since the fill)")
        elif not span[0] - slack <= args.price <= span[1] + slack:
            raise ValueError(f"--price {tc.fmt_price(args.price)} is outside what {sym} traded since the fill "
                             f"({tc.fmt_price(span[0])}-{tc.fmt_price(span[1])} in the 5-minute bars to "
                             f"{tc.et_hhmm(now_ts)} {tc.tz_label()}) - a manual exit must be at a price that "
                             "traded; omit --price to close at the current price")
    if pt is not None and pt.get("status") == "open" and args.price is None:
        with exchange_scope(feed[1]):
            quote = get_quote(sym)                                # FetchError -> exit 2
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
                         f"at {hm(t['closed_at'])} {tc.tz_label()} - nothing left to close")
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
    tid = args.ticket_id.strip().upper()
    notes: List[str] = []
    data: Dict[str, Any] = {}
    k0 = find(load_ledger(), "tickets", tid)
    if k0 is not None:
        tc.set_market(row_market(k0))
    now = tc.now_et()
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
    view_market(args)
    now = tc.now_et()
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    led = load_ledger()
    open_trades = [t for t in rows(led, "trades") if t.get("status") == "open"]
    working = [k for k in rows(led, "tickets") if k.get("status") == "working"]
    quotes: Dict[FeedKey, Dict[str, Any]] = {}
    errors: Dict[FeedKey, str] = {}
    for key in sorted({feed_key(r) for r in open_trades + working}, key=lambda k: (k[0], k[1] or "")):
        sym, ex = key                       # crypto: quoted on the exchange that priced the trade
        problem = exchange_problem(ex)
        if problem:
            errors[key] = problem
            continue
        with tc.market_scope(sym or tc.market()), exchange_scope(ex):
            q, err = best_effort_quote(sym)
        if q:
            quotes[key] = q
        else:
            errors[key] = err or "no quote"
    positions, warnings = [], []
    for t in open_trades:
        row = dict(t)
        if is_crypto(t):
            row["exchange"] = row_exchange(t)            # whose quote this is (a row from v1.1.0: Coinbase)
        q = quotes.get(feed_key(t))
        row.update(last=None, unrealized=None, unrealized_r=None, initial_risk=None)
        try:
            risk = tc.trade_initial_risk(t)
            row["initial_risk"] = round(risk, 2)
            if q:
                pnl = tc.trade_pnl(t, q["price"])
                if is_crypto(t):          # net of both fills' fees, as if closed now
                    rate = fee_rate(t)
                    fees = fee_amount(rate, t["entry_price"], t["qty"]) + fee_amount(rate, q["price"], t["qty"])
                    row.update(unrealized_gross=round(pnl, 2), fees_if_closed=round(fees, 2))
                    pnl -= fees
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
        with tc.market_scope(row_market(t)):          # its own day: the UTC day for crypto
            opened_day, its_today = local_day(t.get("opened_at")), tc.et_date(now_ts)
        if opened_day != its_today:
            warnings.append(f"{t.get('id')} was opened {opened_day or 'None'} - run sync (day trades are "
                            "closed at that day's time stop)")
        positions.append(row)
    work_rows = []
    for k in working:
        q = quotes.get(feed_key(k))
        work_rows.append(dict(k, last=q["price"] if q else None, **({"exchange": row_exchange(k)} if is_crypto(k) else {})))
    pending = [k for k in rows(led, "tickets") if ticket_status(k, now_ts) == "pending"]
    tot_unreal = [p["unrealized"] for p in positions if p["unrealized"] is not None]
    res = {"ok": True, "action": "positions", "as_of": iso(now_ts), "positions": positions, "working": work_rows,
           "pending_tickets": [k["id"] for k in pending],
           "working_risk": safe_working_risk(working_orders(led, today)),
           "unrealized_total": round(sum(tot_unreal), 2) if tot_unreal and len(tot_unreal) == len(positions) else None,
           "summary": safe_summary(led, today), "warnings": warnings, "errors": feed_errors(errors),
           "mode": "paper", "real_money": False, "disclaimer": tc.DISCLAIMER}
    return res, 0


def safe_summary(led: Dict[str, Any], day: str) -> Dict[str, Any]:
    """tc.day_summary for `day` in the active market (open risk with crypto fees, as the budget counts it)."""
    try:
        return tc.day_summary({"tickets": [], "trades": budget_rows(rows(led, "trades"))}, day)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise tc.StateError(f"ledger.json has a malformed trade ({e!r}) - fix or remove it") from None


def cmd_tickets(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    view_market(args)
    now = tc.now_et()
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    out = []
    for k in rows(load_ledger(), "tickets"):
        st = ticket_status(k, now_ts)
        if args.all or st in ("pending", "working") or local_day(k.get("created_at")) == today:
            out.append(dict(k, status=st))
    return {"ok": True, "action": "tickets", "as_of": iso(now_ts), "all": bool(args.all), "tickets": out}, 0


def cmd_history(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    if not 1 <= args.days <= MAX_DAYS:
        raise ValueError(f"--days must be from 1 to {MAX_DAYS}")
    view_market(args)
    now = tc.now_et()
    since = (now.date() - timedelta(days=args.days - 1)).isoformat()
    trades = sorted((t for t in rows(load_ledger(), "trades") if t.get("status") == "closed"
                     and local_day(t.get("closed_at")) >= since),
                    key=lambda t: (parse_ts(t.get("closed_at")) or 0.0, str(t.get("closed_at"))))
    pnls = [float(t.get("pnl") or 0) for t in trades]
    rs = [float(t["r_multiple"]) for t in trades if isinstance(t.get("r_multiple"), (int, float))]
    wins, losses = sum(1 for p in pnls if p > 0), sum(1 for p in pnls if p < 0)
    totals = {"trades": len(trades), "wins": wins, "losses": losses, "flat": len(trades) - wins - losses,
              "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
              "net_pnl": round(sum(pnls), 2), "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
              "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None}
    if any(is_crypto(t) for t in trades):      # crypto P&L is net of fees: show what they took
        fees = sum(float(t.get("fees") or 0) for t in trades if is_crypto(t))
        totals.update(fees=round(fees, 2), gross_pnl=round(sum(pnls) + fees, 2))
    return {"ok": True, "action": "history", "days": args.days, "since": since, "trades": trades,
            "totals": totals, "mode": "paper", "disclaimer": tc.DISCLAIMER}, 0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def order_words(k: Dict[str, Any]) -> str:
    e = tc.fmt_price(k.get("entry"))
    price = f"LIMIT {e}" if k.get("type", "limit") == "limit" else f"MARKET (ref {e})"
    target = tc.fmt_price(k["target"]) if k.get("target") is not None else "none"
    return (f"{SIDE_WORD.get(k.get('side'), '?')} {qty_text(k)} {k.get('symbol')} · {price} · "
            f"TARGET {target} · STOP {tc.fmt_price(k.get('stop'))}")


def card_line(k: Dict[str, Any]) -> str:
    return f"{order_words(k)} · {paper_label(k)} · WAITING FOR YOUR YES — reply yes to confirm ticket {k.get('id')}"


def qty_cell(row: Dict[str, Any]) -> Any:
    """A size for tc.table: the raw value for stocks, fmt_qty for crypto (no 1e-05 notation)."""
    return qty_text(row) if is_crypto(row) and row.get("qty") is not None else row.get("qty")


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
    tz = tc.tz_label()
    if res.get("blocked"):
        o = res["order"]
        fees = order_fees(o)[1]
        risk = abs(o["entry"] - o["stop"]) * o["qty"] + fees
        incl = f" (incl. {tc.fmt_money(fees)} of fees at the stop)" if fees else ""
        lines = [f"TICKET REFUSED · {order_words(o)} · {paper_label(o)}",
                 f"Risk {tc.fmt_money(risk)}{incl} - BLOCKED by today's budget:"]
        lines += [f"  - {r}" for r in res["reasons"]]
        lines += _flag_lines(res)
        lines += ["No ticket was created. Check: budget skill (budget.py status)."]
        if res.get("preview"):
            lines.append(res["banner"])
        lines.append(tc.DISCLAIMER)
        return "\n".join(lines)
    preview = bool(res.get("preview"))
    k = res["order"] if preview else res["ticket"]
    e, s, t, q = k["entry"], k["stop"], k["target"], k["qty"]
    per = abs(e - s)
    crypto = is_crypto(k)
    lines = ["ORDER TICKET · PREVIEW (not a ticket - nothing to confirm)" if preview else f"ORDER TICKET · {k['id']}",
             f"{order_words(k)} · {paper_label(k)}"]
    rr = f" · reward {tc.fmt_money(abs(t - e) * q)} · R:R {k['rr']:.2f}" if t is not None else " · no target"
    per_word = unit_word(k.get("symbol")) if crypto else "share"
    lines.append(f"  Risk      {tc.fmt_money(k['risk'])} ({tc.fmt_price(per)}/{per_word} x {qty_text(k)}){rr}")
    new_risk = k["risk"]
    if crypto:
        rate, fees_at_stop, loss = order_fees(k)
        new_risk = round(loss, 2)
        at_target = f" · {tc.fmt_money(rate * (e + t) * q)} at the target" if t is not None else ""
        lines.append(f"  Fees      {rate * 100:g}% per fill (paper) · {tc.fmt_money(fees_at_stop)} if the stop fills -> "
                     f"loss at the stop {tc.fmt_money(loss)}{at_target}")
    acct = res.get("account_size")
    pct = f" ({e * q / acct * 100:.1f}% of the {tc.fmt_money(acct)} paper account)" if acct else ""
    last = f" · last {tc.fmt_price(res['last_price'])}" if res.get("last_price") is not None else ""
    lines.append(f"  Notional  {tc.fmt_money(e * q)}{pct}{last}")
    lines.append(f"  Budget    {budget_line(res['budget'], new_risk)}")
    if preview:
        ttl = res.get("ticket_ttl_minutes")
        life = f"a real ticket lasts {ttl} min" if ttl else "a real ticket expires after a few minutes"
        lines.append(f"  Expires   - (preview; {life}) · day order · flat by {k['time_stop']} {tz}")
    else:
        mins = int(round((parse_ts(k['expires_at']) - parse_ts(k['created_at'])) / 60))  # type: ignore[operator]
        lines.append(f"  Expires   {hm(k['expires_at'])} {tz} ({mins} min) · day order · flat by {k['time_stop']} {tz}")
    extra = []
    if k.get("setup"):
        extra.append(f"setup {k['setup']}")
    tm = k.get("time_machine")
    if tm and tm.get("verdict"):
        odds = tm_display(tm)
        extra.append(f"time-machine: {tm['verdict']} (target first {fmt_odds(odds['target_first'])} / stop first "
                     f"{fmt_odds(odds['stop_first'])})")
    if extra:
        lines.append("  Plan      " + " · ".join(extra))
    lines += _flag_lines(res)
    lines.append(res["banner"] if preview else f"WAITING FOR YOUR YES — reply yes to confirm ticket {k['id']}")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def render_confirm(res: Dict[str, Any]) -> str:
    k = res["ticket"]
    tz = tc.tz_label()
    if res.get("blocked"):
        lines = [f"NOT CONFIRMED · {k['id']} · BLOCKED by today's budget:"]
        lines += [f"  - {r}" for r in res["reasons"]]
        lines += _flag_lines(res)
        lines.append(f"The ticket stays pending until {hm(k.get('expires_at'))} {tz}. {paper_label(k)}.")
        return "\n".join(lines)
    if not res.get("confirmed"):
        return "\n".join([f"ORDER TICKET · {k['id']} · status {k.get('status')} · expires {hm(k.get('expires_at'))} {tz}",
                          f"{order_words(k)} · {paper_label(k)}", f"NOT CONFIRMED: {res['reason']}."])
    if res["status"] == "filled":
        tr = res["trade"]
        risk = tc.trade_initial_risk(tr)
        target = tc.fmt_price(tr["target"]) if tr.get("target") is not None else "none"
        lines = [f"FILLED (paper) · {SIDE_WORD[tr['side']]} {qty_text(tr)} {tr['symbol']} @ "
                 f"{tc.fmt_price(tr['entry_price'])} · {tr['id']} · {paper_label(tr)}",
                 f"  STOP {tc.fmt_price(tr['stop'])} · TARGET {target} · risk {tc.fmt_money(risk)} · flat by "
                 f"{tr['time_stop']} {tz}"]
        if is_crypto(tr):
            lines.append(f"  Fees      entry fee {tc.fmt_money(tr.get('entry_fee'))} ({tr.get('fee_pct'):g}% of the fill); "
                         "the exit pays the same rate - P&L and R are net of fees")
        lines.append("  `sync` applies the stop, target and time stop from 5-minute bars; `positions` shows live P&L.")
    else:
        side = k["side"]
        lines = [f"WORKING (paper) · {SIDE_WORD[side]} {qty_text(k)} {k['symbol']} LIMIT {tc.fmt_price(k['entry'])} · "
                 f"{k['id']} · {paper_label(k)}",
                 f"  last {tc.fmt_price(res['quote']['price'])} is {'above' if side == 'long' else 'below'} your limit. "
                 f"`sync` fills it only if a later 5-minute bar trades through {tc.fmt_price(k['entry'])} (or "
                 f"the current bar closes through it) before {k['time_stop']} {tz}; otherwise it expires (day order)."]
    lines += _flag_lines(res)
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def event_text(e: Dict[str, Any]) -> str:
    """One sync event as a line, with times in the event's own market (ET, or UTC for crypto)."""
    with tc.market_scope(row_market(e)):
        side = str(e.get("side", "")).upper()
        tz = tc.tz_label()
        when = f"in the {hm(e['bar'])} {tz} bar" if e.get("bar") else f"at {hm(e.get('at'))} {tz}"
        if e["kind"] == "filled":
            return (f"{e['ticket_id']} {e['symbol']}: working {side} order FILLED @ {tc.fmt_price(e['price'])} "
                    f"{when} -> {e['trade_id']}")
        if e["kind"] == "expired":
            return (f"{e['ticket_id']} {e['symbol']}: working {side} limit {tc.fmt_price(e['price'])} EXPIRED unfilled "
                    f"at {hm(e['at'])} {tz} (day order)")
    amb = " (stop and target in the same bar - counted as stop)" if e.get("ambiguous") else ""
    note = f" - {e['note']}" if e.get("note") else ""
    fees = f" (net of {tc.fmt_money(e['fees'])} fees)" if e.get("fees") is not None else ""
    return (f"{e['trade_id']} {e['symbol']} {side} {qty_text(e)}: {REASON_WORD.get(e['reason'], e['reason'])} "
            f"@ {tc.fmt_price(e['price'])} {when} · {tc.fmt_money(e['pnl'], signed=True)} "
            f"{fmt_r(e['r_multiple'])}{fees}{amb}{note}")


def render_sync(res: Dict[str, Any]) -> str:
    lines = [f"SYNC · {hm(res['as_of'])} {tc.tz_label()} · paper ledger"]
    if not (res["events"] or res["open"] or res["working"] or res["issues"] or res["errors"]):
        lines.append("Nothing to sync: no open paper trades or working orders.")
    lines += [f"  {event_text(e)}" for e in res["events"]]
    for o in res["open"]:
        with tc.market_scope(row_market(o)):
            thru = (f" (checked through {hm(o['checked_through'])} {tc.tz_label()})" if o.get("checked_through")
                    else " (no completed bar since the fill yet)")
        target = tc.fmt_price(o["target"]) if o.get("target") is not None else "none"
        lines.append(f"  open: {o['trade_id']} {o['symbol']} {o['side'].upper()} {qty_text(o)} @ "
                     f"{tc.fmt_price(o['entry_price'])} · stop {tc.fmt_price(o['stop'])} · target {target}{thru}")
    for w in res["working"]:
        lines.append(f"  working: {w['ticket_id']} {w['symbol']} {SIDE_WORD.get(w['side'], '?')} {qty_text(w)} "
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
    fees = f" (net of {tc.fmt_money(t['fees'])} fees)" if is_crypto(t) and t.get("fees") is not None else ""
    lines = [f"{head} · {t['id']} {t['symbol']} {t['side'].upper()} {qty_text(t)} · "
             f"{REASON_WORD.get(t['exit_reason'], t['exit_reason'])} @ {tc.fmt_price(t['exit_price'])} · "
             f"{tc.fmt_money(t['pnl'], signed=True)} {fmt_r(t.get('r_multiple'))}{fees} · {paper_label(t)}"]
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
    lines = [f"PAPER POSITIONS · {hm(res['as_of'])} {tc.tz_label()} · REAL MONEY: OFF"]
    if res["positions"]:
        body = []
        for p in res["positions"]:
            body.append([p.get("id"), p.get("symbol"), str(p.get("side", "")).upper(), qty_cell(p),
                         tc.fmt_price(p.get("entry_price")), tc.fmt_price(p.get("stop")),
                         tc.fmt_price(p.get("target")), tc.fmt_price(p.get("last")),
                         tc.fmt_money(p.get("unrealized"), signed=True), fmt_r(p.get("unrealized_r")),
                         row_hm(p, p.get("opened_at"), today)])
        lines.append(tc.table(body, ["TRADE", "SYM", "SIDE", "QTY", "ENTRY", "STOP", "TARGET", "LAST", "P&L", "R",
                                     "OPENED"]))
        if res["unrealized_total"] is not None:
            lines.append(f"Unrealized total {tc.fmt_money(res['unrealized_total'], signed=True)}")
        sources = sorted({exchange_label(row_exchange(p)) for p in res["positions"] if is_crypto(p)})
        if sources:              # each crypto trade is marked on the exchange that priced it
            lines.append(f"Crypto P&L is net of the paper fees of both fills ({' and '.join(sources)} prices).")
    else:
        lines.append("No open paper positions.")
    if res["working"]:
        lines.append("Working orders:")
        lines.append(tc.table([[k.get("id"), order_words(k), tc.fmt_price(k.get("last")),
                                row_hm(k, k.get("confirmed_at"), today)]
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
            when = f"until {k.get('time_stop') or row_market(k).time_stop}"
        else:
            when = row_hm(k, k.get("filled_at") or k.get("expired_at") or k.get("cancelled_at") or k.get("expires_at"),
                          today)
        body.append([k.get("id"), k.get("status"), order_words(k).split(" · TARGET")[0],
                     tc.fmt_price(k.get("stop")), tc.fmt_price(k.get("target")), tc.fmt_money(k.get("risk")),
                     row_hm(k, k.get("created_at"), today), when, k.get("trade_id") or ""])
    return tc.table(body, ["TICKET", "STATUS", "ORDER", "STOP", "TARGET", "RISK", "CREATED", "UNTIL/DONE", "TRADE"])


def render_history(res: Dict[str, Any]) -> str:
    tot = res["totals"]
    if not res["trades"]:
        return f"No closed paper trades since {res['since']}."
    crypto = any(is_crypto(t) for t in res["trades"])
    view = tc.market().key

    def closed(t: Dict[str, Any]) -> str:
        text = str(t.get("closed_at"))[:16].replace("T", " ")
        m = row_market(t)
        return text if m.key == view else f"{text} {m.tz_label}"

    body = [[closed(t), t.get("id"), t.get("symbol"),
             str(t.get("side", "")).upper(), qty_cell(t), tc.fmt_price(t.get("entry_price")),
             tc.fmt_price(t.get("exit_price")), REASON_WORD.get(t.get("exit_reason"), t.get("exit_reason")),
             tc.fmt_money(t.get("pnl"), signed=True), fmt_r(t.get("r_multiple"))]
            + ([tc.fmt_money(t.get("fees")) if is_crypto(t) else "-"] if crypto else []) for t in res["trades"]]
    headers = ["CLOSED", "TRADE", "SYM", "SIDE", "QTY", "ENTRY", "EXIT", "REASON", "P&L", "R"] + (["FEES"] if crypto else [])
    lines = [f"PAPER HISTORY · since {res['since']} ({res['days']} days)", tc.table(body, headers)]
    wr = f"{tot['win_rate_pct']:.0f}%" if tot["win_rate_pct"] is not None else "-"
    avg = fmt_r(tot["avg_r"])
    fees = f" · crypto fees {tc.fmt_money(tot['fees'])} (P&L and R are net of them)" if tot.get("fees") is not None else ""
    lines.append(f"{tot['trades']} trades · {tot['wins']} wins / {tot['losses']} losses · win rate {wr} · net "
                 f"{tc.fmt_money(tot['net_pnl'], signed=True)} · avg {avg}{fees}")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def qty_arg(value: str) -> float:
    """--qty: whole shares for stocks, a fractional size such as 0.01 for crypto (`ticket` checks
    which one the symbol allows)."""
    try:
        v = float(value.replace(",", "").strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a quantity, got {value!r}") from None
    if not math.isfinite(v):
        raise argparse.ArgumentTypeError(f"expected a quantity, got {value!r}")
    return int(v) if v.is_integer() and abs(v) <= MAX_QTY * 10 else v


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

    t = add("ticket", "create a pending paper order ticket (validates levels and the daily budget); outside "
                      "the session it prints a preview that cannot be confirmed")
    t.add_argument("--symbol", required=True, help="ticker, e.g. TSLA, or a crypto pair, e.g. BTC-USDT (BTC-USD "
                                                   "works too; prices from the crypto_exchange setting)")
    t.add_argument("--qty", type=qty_arg, help="shares; crypto: a fractional size such as 0.01 (default: the "
                                                "plan's sizing from the risk skill)")
    t.add_argument("--side", choices=("long", "short"), help="default: the plan's side, else inferred from the levels")
    t.add_argument("--entry", type=float, help="limit price (market: reference price)")
    t.add_argument("--stop", type=float, help="stop price (required unless the levels come from the saved plan)")
    t.add_argument("--target", type=float, help="target price (optional)")
    t.add_argument("--type", dest="order_type", choices=("limit", "market"), default="limit",
                   help="limit (default): never pay more than --entry; market: fill at the price on confirm")
    t.add_argument("--setup", help="label for the journal, e.g. orb (default: the plan's setup)")
    t.add_argument("--time-stop", help="be flat by HH:MM, ET (crypto: UTC) (default: the exit plan's, else "
                                       "15:55 ET / 23:55 UTC)")

    c = add("confirm", "confirm a ticket - ONLY after the user's explicit yes in a later message")
    c.add_argument("ticket_id", help="the exact ticket id, e.g. TKT-20261030-001")
    c.add_argument("--yes", action="store_true", help="the user explicitly said yes to this ticket")

    def market_flag(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--market", choices=("us", "crypto"),
                        help="whose clock and day to show: us (ET, default) or crypto (UTC); "
                             "every trade still follows its own market")

    market_flag(add("sync", "apply stops, targets and the time stop from 5-minute bars; fill working limit orders"))

    cl = add("close", "close an open paper trade at the current price or --price")
    cl.add_argument("trade_id", help="e.g. TRADE-20261030-001")
    cl.add_argument("--price", type=float, help="exit price (default: the current quote)")

    ca = add("cancel", "cancel a pending ticket or a working order")
    ca.add_argument("ticket_id")

    market_flag(add("positions", "open paper trades with live unrealized P&L, plus working orders"))

    tk = add("tickets", "today's tickets plus anything pending or working")
    tk.add_argument("--all", action="store_true", help="every ticket in the ledger")
    market_flag(tk)

    h = add("history", "closed paper trades")
    h.add_argument("--days", type=int, default=30, help="look-back in calendar days (default 30)")
    market_flag(h)
    return p


COMMANDS = {
    "ticket": (cmd_ticket, render_ticket), "confirm": (cmd_confirm, render_confirm),
    "sync": (cmd_sync, render_sync), "close": (cmd_close, render_close), "cancel": (cmd_cancel, render_cancel),
    "positions": (cmd_positions, render_positions), "tickets": (cmd_tickets, render_tickets),
    "history": (cmd_history, render_history),
}


FRONT_KEYS = ("ok", "action", "status", "preview", "confirmed", "blocked", "reasons", "card", "ticket", "trade")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    fn, render = COMMANDS[args.cmd]
    res, code = fn(args)
    res = dict([(k, res[k]) for k in FRONT_KEYS if k in res] + [(k, v) for k, v in res.items() if k not in FRONT_KEYS])
    tc.emit(res, getattr(args, "json", False), render)
    return code


if __name__ == "__main__":
    tc.run(main)
