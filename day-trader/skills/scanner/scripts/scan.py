#!/usr/bin/env python3
"""Scanner: rank US stocks by daily-chart day-trading setups.

Fetches ~6 months of daily bars per symbol from Yahoo Finance, drops illiquid names, and
looks for breakouts, breakdowns, gaps, unusual volume, trend pullbacks and NR7 coils on the
latest bar (today's bar while the market is open, with volume compared at time-of-day pace).

Usage:
  python3 scripts/scan.py --symbols TSLA,NVDA,AAPL           # quick: only these symbols
  python3 scripts/scan.py --limit 300 --top 15               # first 300 names of the universe
  python3 scripts/scan.py --setups breakout,gap_up           # whole market (~5,900 names, minutes)
  python3 scripts/scan.py --min-price 10 --min-dollar-volume 50M --top 25 --json

Exit codes: 0 ok (even with zero matches), 1 bad input, 2 data source unreachable.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trader_common as tc  # noqa: E402

import argparse  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections import Counter  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402
from datetime import date as _date, datetime  # noqa: E402
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence  # noqa: E402

SETUPS = ("breakout", "breakdown", "gap_up", "gap_down", "unusual_volume", "pullback", "nr7")
MIN_BARS = 60            # need this many daily bars (50-SMA + 20-day lookback headroom)
LOOKBACK = 20            # prior bars for the 20-day high/low and average volume
BREAKOUT_RVOL = 1.5      # breakout/breakdown volume confirmation
UNUSUAL_RVOL = 3.0
GAP_PCT = 3.0
PULLBACK_MIN_SPREAD = 0.25  # 20-SMA must lead the 50-SMA by this many ATRs (not a flat line)
STALE_DAYS = 6           # last bar older than this many calendar days -> skip (halted/delisted)
MIN_PROGRESS = 0.03      # floor on the expected-volume share (first minutes after the open)
ABORT_AFTER = 30         # stop after this many download failures in a row (source down/throttled)
FIRST_WAVE_MIN = 3       # ...but at the start, give up once the first max(workers, 3) all failed
NOT_FOUND_ABORT = 50     # ...or when the first this-many symbols are all "not found"
PROGRESS_EVERY = 250
EPS = 1e-9               # thresholds are inclusive; don't lose an exact 3.0% gap to float error
DEFAULT_MIN_PRICE = 5.0
DEFAULT_MIN_DOLLAR_VOLUME = 20_000_000.0
NR7_WEIGHT = 0.6         # a coil has not done anything yet: rank it below active setups (max 60)

# Typical cumulative share of a US stock's daily volume by minutes after the 09:30 open
# (U-shaped intraday profile; the last ~8% is the closing auction, counted only once the day
# is complete). Used to compare a partial day's volume with a full-day average at fair pace.
VOLUME_CURVE = (
    (0, 0.0), (5, 0.03), (15, 0.065), (30, 0.115), (60, 0.195), (90, 0.26), (120, 0.32),
    (150, 0.37), (180, 0.42), (210, 0.47), (240, 0.52), (270, 0.57), (300, 0.63),
    (330, 0.69), (360, 0.76), (390, 0.92),
)

LEVEL_LABELS = {
    "high_20d": "20d high", "low_20d": "20d low", "prior_close": "gap fill",
    "day_high": "day high", "day_low": "day low", "sma20": "20-SMA", "range_high": "NR7 high",
}


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _px(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    return round(x, 2) if abs(x) >= 1 else round(x, 4)


def normalize_daily(bars: Iterable[tc.Bar], today: str, before_open: bool = False) -> List[tc.Bar]:
    """Daily bars, one per ET date, oldest first.

    Merges duplicate rows for the same date (Yahoo sometimes sends the live row next to the
    day's own row), drops rows dated after `today` (replays via $DAYTRADER_NOW) and, before
    the open, a row for `today` (that session has not started)."""
    by_date: Dict[str, tc.Bar] = {}
    for b in sorted(bars, key=lambda x: x.ts):
        d = b.date
        if d > today or (before_open and d == today):
            continue
        vol = b.volume if math.isfinite(b.volume) and b.volume > 0 else 0.0
        cur = by_date.get(d)
        if cur is None:
            by_date[d] = tc.Bar(b.ts, b.open, b.high, b.low, b.close, vol)
        else:
            by_date[d] = tc.Bar(cur.ts, cur.open, max(cur.high, b.high), min(cur.low, b.low),
                                b.close, max(cur.volume, vol))
    return [by_date[d] for d in sorted(by_date)]


def volume_progress(minutes_since_open: float, session_minutes: float = 390.0) -> float:
    """Expected share (0..1) of a full day's volume traded `minutes_since_open` into a session."""
    if session_minutes <= 0 or minutes_since_open >= session_minutes:
        return 1.0
    x = max(0.0, minutes_since_open) * 390.0 / session_minutes   # half days: compress the curve
    for (x0, y0), (x1, y1) in zip(VOLUME_CURVE, VOLUME_CURVE[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return VOLUME_CURVE[-1][1]


def session_bounds(now: datetime, meta: Optional[Dict[str, Any]] = None) -> tuple:
    """(start, end) unix seconds of today's regular session; Yahoo's currentTradingPeriod
    knows early closes (half days), else 09:30-16:00 ET."""
    today = now.strftime("%Y-%m-%d")
    reg = (((meta or {}).get("currentTradingPeriod") or {}).get("regular")) or {}
    try:
        start, end = int(reg["start"]), int(reg["end"])
        if end > start and tc.et_date(start) == today:
            return start, end
    except (KeyError, TypeError, ValueError):
        pass
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return int(midnight + tc.MARKET_OPEN * 60), int(midnight + tc.MARKET_CLOSE * 60)


def day_progress(last: tc.Bar, now: datetime, meta: Optional[Dict[str, Any]] = None) -> float:
    """1.0 if `last` is a completed day, else the expected volume share traded by `now`."""
    if last.date != now.strftime("%Y-%m-%d"):
        return 1.0
    start, end = session_bounds(now, meta)
    t = now.timestamp()
    if t >= end:
        return 1.0
    # Data stamped at/after this session's close is a completed day even though `now` is
    # earlier (a $DAYTRADER_NOW replay of a past session): don't pace a full day's volume.
    stamp = (meta or {}).get("regularMarketTime")
    if isinstance(stamp, (int, float)) and stamp >= end:
        return 1.0
    return volume_progress((t - start) / 60.0, (end - start) / 60.0)


def metrics_at(bars: Sequence[tc.Bar], i: int, progress: float = 1.0) -> Dict[str, Any]:
    """Indicator snapshot for bar i (needs i >= 50). ATR is as of the prior bar, so today's
    move is measured against volatility known before today; SMAs/RSI include bar i."""
    b, p = bars[i], bars[i - 1]
    win = bars[i - LOOKBACK:i]
    avg_vol = sum(x.volume for x in win) / len(win)
    closes = [x.close for x in bars[:i + 1]]
    partial = progress < 1.0
    expected = avg_vol * (max(progress, MIN_PROGRESS) if partial else 1.0)
    return {
        "date": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
        "volume": b.volume, "prior_close": p.close, "prior_high": p.high, "prior_low": p.low,
        "avg_volume": avg_vol,
        "dollar_volume": sum(x.close * x.volume for x in win) / len(win),
        "high_20d": max(x.high for x in win), "low_20d": min(x.low for x in win),
        "atr": tc.atr(bars[:i], 14),
        "sma20": tc.sma(closes, 20)[-1], "sma50": tc.sma(closes, 50)[-1],
        "rsi": tc.rsi(closes, 14)[-1],
        "rvol": b.volume / expected if expected > 0 else None,
        "ranges7": [x.high - x.low for x in bars[i - 6:i + 1]],
        "partial": partial, "progress": progress,
    }


def _volume_component(rvol: Optional[float]) -> float:
    return _clamp(math.log(rvol) / math.log(5)) if rvol and rvol > 0 else 0.0   # 1x->0, 5x->1


def _move_component(m: Dict[str, Any], direction: int) -> float:
    atr = m["atr"]
    if not atr or atr <= 0:
        return 0.0
    change = m["close"] - m["prior_close"]
    x = abs(change) if direction == 0 else change * direction
    return _clamp(x / atr / 2.0)                                               # 2 ATR -> 1


def _trend_component(m: Dict[str, Any], direction: int) -> float:
    """Half for how far price is beyond the 50-SMA, half for how far the 20-SMA leads the
    50-SMA, both in the setup's direction and in ATRs (1 ATR or more counts fully)."""
    c, s20, s50, atr = m["close"], m["sma20"], m["sma50"], m["atr"]
    if s20 is None or s50 is None:
        return 0.0
    d = 1 if direction > 0 else -1
    if not atr or atr <= 0:
        return 0.5 * float(d * (c - s50) > 0) + 0.5 * float(d * (s20 - s50) > 0)
    return 0.5 * _clamp(d * (c - s50) / atr) + 0.5 * _clamp(d * (s20 - s50) / atr)


def _setup(name: str, bias: Optional[str], v: float, mv: float, tr: float, detail: str,
           levels: Dict[str, Optional[float]], key: str, weight: float = 1.0) -> Dict[str, Any]:
    return {
        "setup": name,
        "score": int(round(100 * weight * (0.4 * v + 0.4 * mv + 0.2 * tr))),
        "bias": bias,
        "detail": detail,
        "levels": {k: _px(x) for k, x in levels.items()},
        "key_level": key,
        "components": {"volume": round(v, 3), "move": round(mv, 3), "trend": round(tr, 3)},
    }


def setups_from_metrics(m: Dict[str, Any], coil: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """All setups present in snapshot `m`, best score first. `coil` is the last *completed*
    bar's snapshot, used for NR7 (defaults to `m`)."""
    coil = coil or m
    fp = tc.fmt_price
    c, o, h, l, pc = m["close"], m["open"], m["high"], m["low"], m["prior_close"]
    rvol, atr = m["rvol"], m["atr"]
    verb = "trading" if m["partial"] else "closed"
    pace = " (pace-adjusted)" if m["partial"] else ""
    chg = (c / pc - 1) * 100
    out: List[Dict[str, Any]] = []

    def common(direction: int) -> tuple:
        return _volume_component(rvol), _move_component(m, direction), _trend_component(m, direction)

    if rvol is not None and rvol >= BREAKOUT_RVOL - EPS:
        if c > m["high_20d"]:
            out.append(_setup(
                "breakout", "long", *common(1),
                f"{verb} {(c / m['high_20d'] - 1) * 100:.1f}% above the 20-day high "
                f"{fp(m['high_20d'])} on {rvol:.1f}x volume{pace}",
                {"high_20d": m["high_20d"], "day_low": l, "prior_close": pc}, "high_20d"))
        if c < m["low_20d"]:
            out.append(_setup(
                "breakdown", "short", *common(-1),
                f"{verb} {(1 - c / m['low_20d']) * 100:.1f}% below the 20-day low "
                f"{fp(m['low_20d'])} on {rvol:.1f}x volume{pace}",
                {"low_20d": m["low_20d"], "day_high": h, "prior_close": pc}, "low_20d"))

    gap = (o / pc - 1) * 100
    if abs(gap) >= GAP_PCT - EPS:
        up = gap > 0
        hold = ("holding above the open" if c >= o else "faded below the open") if up else \
            ("holding below the open" if c <= o else "bounced above the open")
        filled = (l <= pc) if up else (h >= pc)
        out.append(_setup(
            "gap_up" if up else "gap_down", "long" if up else "short", *common(1 if up else -1),
            f"gapped {gap:+.1f}% (open {fp(o)} vs prior close {fp(pc)}), {hold}, "
            f"{'now' if m['partial'] else 'closed'} {chg:+.1f}% on the day"
            + ("; gap already filled" if filled else ""),
            {"prior_close": pc, "open": o, "day_high": h, "day_low": l}, "prior_close"))

    if rvol is not None and rvol >= UNUSUAL_RVOL - EPS:
        d = 1 if c >= pc else -1
        out.append(_setup(
            "unusual_volume", "long" if d > 0 else "short", *common(d),
            f"volume {rvol:.1f}x the 20-day average{pace} on a {chg:+.1f}% move",
            {"day_high": h, "day_low": l, "prior_close": pc}, "day_high" if d > 0 else "day_low"))

    s20, s50, rsi_v = m["sma20"], m["sma50"], m["rsi"]
    if (s20 is not None and s50 is not None and atr and rsi_v is not None
            and s20 - s50 >= PULLBACK_MIN_SPREAD * atr - EPS and abs(l - s20) <= atr + EPS
            and c > s20 and 40.0 - EPS <= rsi_v <= 60.0 + EPS):
        out.append(_setup(
            "pullback", "long", *common(1),
            f"uptrend (20-SMA {fp(s20)} > 50-SMA {fp(s50)}); low {fp(l)} came within "
            f"{abs(l - s20) / atr:.1f} ATR of the 20-SMA and price {verb} above it; RSI {rsi_v:.0f}",
            {"sma20": s20, "sma50": s50, "day_low": l}, "sma20"))

    r7 = coil["ranges7"]
    rng = r7[-1]
    if (rng > 0 and coil["volume"] > 0 and rng < min(r7[:-1])
            and coil["high"] <= coil["prior_high"] and coil["low"] >= coil["prior_low"]):
        catr = coil["atr"]
        tight = _clamp(1 - rng / catr) if catr else 0.0
        dry = _clamp((1 - coil["rvol"]) / 0.5) if coil["rvol"] is not None else 0.0
        trend = max(_trend_component(coil, 1), _trend_component(coil, -1))
        rh, rl = coil["high"], coil["low"]
        if coil is m:
            when, watch = "today's", "next session"
        else:
            when, watch = f"yesterday's ({coil['date']})", "today"
        status = ""
        if coil is not m:
            if c > rh:
                status = f" - already trading above {fp(rh)}"
            elif c < rl:
                status = f" - already trading below {fp(rl)}"
        s = _setup("nr7", "either", dry, tight, trend,
                   f"{when} range {fp(rng)} was the narrowest of 7 days and inside the prior day; "
                   f"watch a break of {fp(rh)} / {fp(rl)} {watch}{status}",
                   {"range_high": rh, "range_low": rl}, "range_high", weight=NR7_WEIGHT)
        s["components"] = {"volume_dryup": round(dry, 3), "tightness": round(tight, 3),
                           "trend": round(trend, 3)}
        s["as_of"] = coil["date"]
        out.append(s)

    out.sort(key=lambda s: (-s["score"], SETUPS.index(s["setup"])))
    return out


def detect_setups(bars: Sequence[tc.Bar], progress: float = 1.0) -> List[Dict[str, Any]]:
    """Setups on the last bar of `bars` (daily, oldest first). `progress` < 1 marks the last
    bar as today's unfinished session (see volume_progress); NR7 then uses yesterday's bar."""
    bars = list(bars)
    if len(bars) < MIN_BARS:
        return []
    m = metrics_at(bars, len(bars) - 1, progress)
    coil = m if progress >= 1.0 else metrics_at(bars, len(bars) - 2, 1.0)
    return setups_from_metrics(m, coil)


def _key_level_text(row: Dict[str, Any]) -> str:
    if not row["setups"]:
        return ""
    s = row["setups"][0]
    lv = s["levels"]
    if s["setup"] == "nr7":
        return f"{tc.fmt_price(lv['range_high'])}/{tc.fmt_price(lv['range_low'])} (NR7 range)"
    return f"{tc.fmt_price(lv[s['key_level']])} ({LEVEL_LABELS.get(s['key_level'], s['key_level'])})"


def evaluate_symbol(symbol: str, bars: Sequence[tc.Bar], now: datetime,
                    meta: Optional[Dict[str, Any]] = None, *,
                    min_price: float = DEFAULT_MIN_PRICE,
                    min_dollar_volume: float = DEFAULT_MIN_DOLLAR_VOLUME,
                    wanted: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """One symbol through history checks, liquidity filters and setup detection.

    Returns {"symbol", "status": ok|none|filtered|skipped, "reason"?, "row"?}."""
    today = now.strftime("%Y-%m-%d")
    before_open = now.hour * 60 + now.minute < tc.MARKET_OPEN
    bars = normalize_daily(bars, today, before_open)
    if len(bars) < MIN_BARS:
        return {"symbol": symbol, "status": "skipped", "kind": "short history",
                "reason": f"only {len(bars)} daily bars, need {MIN_BARS}"}
    last = bars[-1]
    age = (_date.fromisoformat(today) - _date.fromisoformat(last.date)).days
    if age > STALE_DAYS:
        return {"symbol": symbol, "status": "skipped", "kind": "stale",
                "reason": f"no trading since {last.date} (halted or delisted?)"}
    progress = day_progress(last, now, meta)
    m = metrics_at(bars, len(bars) - 1, progress)
    if m["close"] < min_price:
        return {"symbol": symbol, "status": "filtered", "kind": "price",
                "reason": f"price {tc.fmt_price(m['close'])} < {tc.fmt_money(min_price)}"}
    if m["dollar_volume"] < min_dollar_volume:
        return {"symbol": symbol, "status": "filtered", "kind": "dollar volume",
                "reason": f"20-day avg $ volume ${tc.fmt_big(m['dollar_volume'])} < "
                          f"${tc.fmt_big(min_dollar_volume)}"}
    coil = m if progress >= 1.0 else metrics_at(bars, len(bars) - 2, 1.0)
    found = setups_from_metrics(m, coil)
    if wanted is not None:
        keep = set(wanted)
        found = [s for s in found if s["setup"] in keep]
    meta = meta or {}
    pc = m["prior_close"]
    row = {
        "symbol": symbol.upper(),
        "name": meta.get("longName") or meta.get("shortName") or "",
        "score": found[0]["score"] if found else 0,
        "bias": found[0]["bias"] if found else None,
        "setups": found,
        "date": m["date"],
        "partial_day": m["partial"],
        "volume_progress": round(progress, 3),
        "last": _px(m["close"]),
        "prior_close": _px(pc),
        "change_pct": round((m["close"] / pc - 1) * 100, 2),
        "gap_pct": round((m["open"] / pc - 1) * 100, 2),
        "rvol": None if m["rvol"] is None else round(m["rvol"], 2),
        "volume": m["volume"],
        "avg_volume_20d": round(m["avg_volume"]),
        "dollar_volume_20d": round(m["dollar_volume"]),
        "atr14": _px(m["atr"]),
        "atr_pct": None if not m["atr"] else round(m["atr"] / m["close"] * 100, 2),
        "sma20": _px(m["sma20"]), "sma50": _px(m["sma50"]),
        "rsi14": None if m["rsi"] is None else round(m["rsi"], 1),
        "high_20d": _px(m["high_20d"]), "low_20d": _px(m["low_20d"]),
    }
    if found:
        k = found[0]["key_level"]
        row["key_level"] = {"name": k, "price": found[0]["levels"].get(k)}
    return {"symbol": symbol.upper(), "status": "ok" if found else "none", "row": row}


def _is_not_found(msg: str) -> bool:
    return any(s in msg for s in ("HTTP 404", "Not Found", "No data found", "no chart data"))


def scan_one(entry: Dict[str, Any], now: datetime, ttl: Optional[float] = None,
             **opts: Any) -> Dict[str, Any]:
    """Fetch + evaluate one universe row. Never raises: failures come back as a status."""
    sym = entry["symbol"]
    try:
        bars, meta = tc.fetch_bars(sym, "1d", "6mo", ttl=ttl)
    except tc.FetchError as e:
        msg = str(e)
        return {"symbol": sym, "status": "notfound" if _is_not_found(msg) else "failed",
                "kind": "not found" if _is_not_found(msg) else "download failed", "reason": msg}
    except Exception as e:  # malformed payload, local cache I/O trouble, ...
        return {"symbol": sym, "status": "failed", "kind": "download failed",
                "reason": f"{sym}: {type(e).__name__}: {e}"}
    try:
        res = evaluate_symbol(sym, bars, now, meta, **opts)
    except Exception as e:  # one odd series must not sink a 5,900-symbol scan
        return {"symbol": sym, "status": "skipped", "kind": "error",
                "reason": f"could not evaluate: {type(e).__name__}: {e}"}
    if "row" in res:
        res["row"]["name"] = res["row"]["name"] or entry.get("name") or ""
        res["row"]["exchange"] = entry.get("exchange") or ""
    return res


def rank(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(rows, key=lambda r: (-r["score"], -(r["rvol"] or 0.0), r["symbol"]))


def run_scan(universe: Sequence[Dict[str, Any]], now: datetime, *, workers: int = 8,
             top: int = 25, ttl: Optional[float] = None,
             progress: Optional[Callable[[str], None]] = None, **opts: Any) -> Dict[str, Any]:
    """Scan `universe` rows ({symbol, name?, exchange?}) concurrently and rank the matches."""
    total = len(universe)
    t0 = time.monotonic()
    matches: List[Dict[str, Any]] = []
    quiet_rows: List[Dict[str, Any]] = []   # passed filters, no setup
    skip_kinds: Counter = Counter()
    filter_kinds: Counter = Counter()
    skipped: List[Dict[str, str]] = []
    filtered: List[Dict[str, str]] = []
    failures: List[Dict[str, Any]] = []
    fetched = consecutive = done = 0
    stop_reason: Optional[str] = None

    stop = threading.Event()

    def work(entry: Dict[str, Any]) -> Dict[str, Any]:
        if stop.is_set():   # aborting: don't start new downloads
            return {"symbol": entry["symbol"], "status": "cancelled"}
        return scan_one(entry, now, ttl, **opts)

    interrupted = False
    ex = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = [ex.submit(work, e) for e in universe]
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            st = r["status"]
            if st in ("failed", "notfound"):
                failures.append(r)
                consecutive = consecutive + 1 if st == "failed" else consecutive
            else:
                fetched += 1
                consecutive = 0
            if st in ("failed", "notfound", "skipped"):
                skip_kinds[r["kind"]] += 1
                skipped.append({"symbol": r["symbol"], "reason": r["reason"]})
            elif st == "filtered":
                filter_kinds[r["kind"]] += 1
                filtered.append({"symbol": r["symbol"], "reason": r["reason"]})
            elif st == "ok":
                matches.append(r["row"])
            else:
                quiet_rows.append(r["row"])
            if progress and done % PROGRESS_EVERY == 0 and done < total:
                progress(f"  {done:,}/{total:,} symbols · {len(matches)} with setups · "
                         f"{sum(skip_kinds.values())} skipped · {time.monotonic() - t0:.0f}s")
            if fetched == 0 and (consecutive >= min(max(workers, FIRST_WAVE_MIN), ABORT_AFTER)
                                 or len(failures) >= NOT_FOUND_ABORT):
                break   # the whole first wave failed: the source is down, blocked or changed
            if consecutive >= ABORT_AFTER:
                stop_reason = (f"stopped early: {consecutive} downloads in a row failed "
                               f"(last: {r['reason']})")
                break
    except KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        stop.set()
        # Let in-flight downloads finish (at most one per worker) unless the user hit Ctrl-C.
        ex.shutdown(wait=not interrupted, cancel_futures=True)

    if fetched == 0 and failures:
        if all(f["status"] == "notfound" for f in failures) and len(failures) < NOT_FOUND_ABORT:
            names = ", ".join(f["symbol"] for f in failures[:8])
            raise ValueError(f"Yahoo Finance has no data for {names} - check the symbol(s)")
        first = next((f for f in failures if f["status"] == "failed"), failures[0])
        raise tc.FetchError(first["reason"] if first["status"] == "failed" else
                            f"Yahoo Finance knew none of the first {len(failures)} symbols "
                            f"(last: {first['reason']}) - the API may have changed")

    ranked = rank(matches)
    small = total <= 100
    return {
        "as_of": now.isoformat(timespec="seconds"),
        "market_open": tc.is_market_open(now),
        "partial_day": any(r["partial_day"] for r in matches + quiet_rows),
        "scanned": done,
        "universe_size": total,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "skipped": sum(skip_kinds.values()),
        "skipped_by_reason": dict(skip_kinds),
        "filtered": sum(filter_kinds.values()),
        "filtered_by_reason": dict(filter_kinds),
        "passed_filters": len(matches) + len(quiet_rows),
        "matched": len(ranked),
        "results": ranked[:top] if top else ranked,
        "no_setup": sorted(r["symbol"] for r in quiet_rows) if small else None,
        "skipped_symbols": sorted(skipped, key=lambda x: x["symbol"]) if small else skipped[:20],
        "filtered_symbols": sorted(filtered, key=lambda x: x["symbol"]) if small else None,
        "incomplete": stop_reason is not None,
        "warning": stop_reason,
        "filters": {"min_price": opts.get("min_price", DEFAULT_MIN_PRICE),
                    "min_dollar_volume": opts.get("min_dollar_volume", DEFAULT_MIN_DOLLAR_VOLUME),
                    "setups": list(opts.get("wanted") or [])},
    }


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

_SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,11}")


def parse_symbol_list(text: str) -> List[str]:
    """'tsla, nvda AAPL' -> ['TSLA', 'NVDA', 'AAPL'] (deduped, Yahoo spelling treated alike)."""
    out, seen = [], set()
    for raw in re.split(r"[\s,]+", text or ""):
        s = raw.strip().upper().lstrip("$")
        if not s:
            continue
        if not _SYMBOL_RE.fullmatch(s):
            raise ValueError(f"not a ticker symbol: {raw!r}")
        key = tc.yahoo_symbol(s)
        if key not in seen:
            seen.add(key)
            out.append(s)
    if not out:
        raise ValueError("--symbols needs at least one ticker, e.g. --symbols TSLA,NVDA")
    return out


def parse_setups(text: Optional[str]) -> Optional[List[str]]:
    if not text or text.strip().lower() == "all":
        return None
    out = []
    for raw in re.split(r"[\s,]+", text.strip()):
        if not raw:
            continue
        s = raw.lower().replace("-", "_")
        if s not in SETUPS:
            raise ValueError(f"unknown setup {raw!r}; choose from {', '.join(SETUPS)}")
        if s not in out:
            out.append(s)
    return out or None


def parse_amount(text: Any) -> float:
    """'20000000', '20,000,000', '20M', '$1.5B', '500k', '2e7' -> dollars."""
    text = str(text)
    if re.fullmatch(r"\s*\$?\s*\d{1,3}(?:,\d{3})+(?:\.\d*)?\s*[kmbt]?\s*", text, re.I):
        text = text.replace(",", "")   # thousands separators only ("1,5M" stays an error)
    m = re.fullmatch(r"\s*\$?\s*(\d+(?:\.\d*)?|\.\d+)(?:e(\d+))?\s*([kmbt]?)\s*", text, re.I)
    if not m:
        raise ValueError(f"expected an amount like 20000000 or 20M, got {text!r}")
    v = float(m.group(1)) * (10 ** int(m.group(2)) if m.group(2) else 1)
    return v * {"": 1, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[m.group(3).lower()]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def short_reason(reason: str, symbol: str = "") -> str:
    """Drop the 'SYM: ' prefix and long URLs (keep the host) for one-line display."""
    r = reason[len(symbol) + 2:] if symbol and reason.startswith(symbol + ": ") else reason
    r = re.sub(r"\s*\(expected \S+\)", "", r)
    return re.sub(r"https?://([^/\s]+)\S*", r"\1", r)


def render(res: Dict[str, Any]) -> str:
    now = tc.parse_et(res["as_of"])
    if res["partial_day"]:
        state = ("market open - today's bar is in progress; volume is compared at time-of-day "
                 "pace (rvol is a projection)")
    elif res["market_open"]:
        state = "market open"
    elif now.weekday() >= 5:
        state = "market closed (weekend) - latest completed session"
    elif now.hour * 60 + now.minute < tc.MARKET_OPEN:
        state = "pre-market - latest completed session (today's gaps show after 09:30 ET)"
    else:
        state = "market closed - latest completed session"
    lines = [f"SCANNER · {now.strftime('%a %Y-%m-%d %H:%M')} ET · {state}"]
    if res["warning"]:
        lines.append(f"WARNING: {res['warning']} - results below are partial")
    rows = res["results"]
    if rows:
        table = [[i, r["symbol"], "+".join(s["setup"] for s in r["setups"]), r["score"],
                  tc.fmt_price(r["last"]), tc.fmt_pct(r["change_pct"], 1, signed=True),
                  "-" if r["rvol"] is None else f"{r['rvol']:.1f}x", _key_level_text(r)]
                 for i, r in enumerate(rows, 1)]
        lines += ["", tc.table(table, ["#", "symbol", "setups", "score", "last", "chg",
                                       "rvol", "key level"]), ""]
        lines.append("Top setups:")
        width = max(len(r["symbol"]) for r in rows[:5])
        for r in rows[:5]:
            for j, s in enumerate(r["setups"]):
                lead = r["symbol"].ljust(width) if j == 0 else " " * width
                lines.append(f"  {lead}  {s['setup']} {s['score']}: {s['detail']}")
    else:
        lines += ["", "No setups matched."]
    if res.get("no_setup"):
        wanted = res["filters"]["setups"]
        label = f"No matching setup ({', '.join(wanted)})" if wanted else "No setup today"
        lines.append(f"{label}: {', '.join(res['no_setup'])}")
    if res.get("filtered_symbols"):
        lines.append("Filtered out: " + "; ".join(f"{x['symbol']} ({x['reason']})"
                                                  for x in res["filtered_symbols"]))
    if res.get("skipped_symbols") and res["universe_size"] <= 100:
        lines.append("Skipped: " + "; ".join(f"{x['symbol']} ({short_reason(x['reason'], x['symbol'])})"
                                             for x in res["skipped_symbols"]))
    skip = f"{res['skipped']:,} skipped"
    if res["skipped_by_reason"]:
        skip += ": " + ", ".join(f"{n} {k}" for k, n in sorted(res["skipped_by_reason"].items()))
    shown = f"top {len(rows)} shown" if res["matched"] > len(rows) else f"{len(rows)} shown"
    lines += ["",
              f"Scanned {_plural(res['scanned'], 'symbol')} in {res['elapsed_s']:.1f}s ({skip}) · "
              f"{res['passed_filters']:,} passed the liquidity filters ({res['filtered']:,} below) · "
              f"{res['matched']:,} matched, {shown}",
              f"Filters: last >= {tc.fmt_money(res['filters']['min_price'])}, 20-day avg $ volume >= "
              f"${tc.fmt_big(res['filters']['min_dollar_volume'])}"
              + (f", setups: {', '.join(res['filters']['setups'])}" if res["filters"]["setups"] else ""),
              "Scanner hits are candidates to research, not trade signals. " + tc.DISCLAIMER]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = tc.make_parser("Scan US stocks for daily-chart setups (breakout, breakdown, gaps, unusual "
                       "volume, pullback, NR7) and rank them.")
    p.add_argument("--symbols", help="comma-separated tickers to scan instead of the whole market")
    p.add_argument("--limit", type=int, help="scan only the first N symbols of the universe")
    p.add_argument("--setups", help=f"only these setups (comma-separated): {', '.join(SETUPS)}")
    p.add_argument("--top", type=int, default=25, help="rows to show (default 25, 0 = all)")
    p.add_argument("--min-price", type=float, default=DEFAULT_MIN_PRICE,
                   help="minimum last price (default 5)")
    p.add_argument("--min-dollar-volume", default=str(int(DEFAULT_MIN_DOLLAR_VOLUME)),
                   help="minimum 20-day average close x volume, e.g. 20M (default 20,000,000)")
    p.add_argument("--include-etfs", action="store_true", help="add ETFs to the market universe")
    p.add_argument("--workers", type=int, default=8, help="parallel downloads (default 8, max 32)")
    p.add_argument("--quiet", action="store_true", help="no progress lines on stderr")

    def error(message: str) -> None:  # bad flags are bad input: exit 1, not 2 (= data error)
        p.print_usage(sys.stderr)
        p.exit(1, f"{p.prog}: error: {message}\n")
    p.error = error  # type: ignore[assignment]
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    wanted = parse_setups(args.setups)
    min_dv = parse_amount(args.min_dollar_volume)
    if args.min_price < 0 or not math.isfinite(args.min_price):
        raise ValueError("--min-price must be >= 0")
    if args.top < 0:
        raise ValueError("--top must be >= 0")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be >= 1")
    if not 1 <= args.workers <= 32:
        raise ValueError("--workers must be between 1 and 32")
    say = None if args.quiet else (lambda msg: print(msg, file=sys.stderr, flush=True))

    if args.symbols:
        universe = [{"symbol": s} for s in parse_symbol_list(args.symbols)]
    else:
        universe = tc.us_symbols(include_etfs=args.include_etfs)
        if not universe:
            raise tc.FetchError("the Nasdaq Trader symbol lists came back empty")
    if args.limit:
        universe = universe[:args.limit]
    seen, deduped = set(), []
    for row in universe:
        key = tc.yahoo_symbol(row["symbol"])
        if key not in seen:
            seen.add(key)
            deduped.append(row)

    now = tc.now_et()
    if say and len(deduped) > PROGRESS_EVERY:
        say(f"scanning {len(deduped):,} symbols with {args.workers} workers - a full-market scan "
            "takes a few minutes (use --limit or --symbols for a quick run)")
    res = run_scan(deduped, now, workers=args.workers, top=args.top,
                   ttl=120 if tc.is_market_open(now) else 900, progress=say,
                   min_price=args.min_price, min_dollar_volume=min_dv, wanted=wanted)
    res["filters"]["universe"] = ("symbols" if args.symbols else
                                  "us_common+etfs" if args.include_etfs else "us_common")
    res["disclaimer"] = tc.DISCLAIMER
    tc.emit(res, args.json, render)
    return 0


if __name__ == "__main__":
    try:
        tc.run(main)
    except SystemExit as e:
        if e.code == 130:   # Ctrl-C: don't let interpreter exit join in-flight download threads
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except Exception:
                    pass
            os._exit(130)
        raise
