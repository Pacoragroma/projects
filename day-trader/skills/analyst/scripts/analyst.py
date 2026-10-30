#!/usr/bin/env python3
"""Analyst: a pre-trade ticker brief and a post-trade review of your paper journal.

brief   Trend (price vs the 20/50-day SMAs, 20-day regression slope), key levels (prior day,
        20-day and 6-month high/low, SMAs, today's VWAP), daily ATR(14) in $ and %, average
        daily dollar volume, relative volume (vs the same time of day when 5-minute bars are
        available, else vs the 20-day average), today's gap, and SEC context (company, shares
        outstanding, the last 30 days of filings: 8-K items, 10-Q/10-K, possible dilution,
        insider Form 4s). Daily bars are required; 5-minute bars and SEC data are optional.
review  Analytics of the closed paper trades in ledger.json (written by the broker skill):
        win rate, average win/loss in $ and R, profit factor, expectancy, max drawdown,
        best/worst trade, breakdowns by setup, side, entry hour, time-of-day window, exit
        reason and Time Machine verdict, and plain-language takeaways.

Usage:
  python3 scripts/analyst.py brief TSLA
  python3 scripts/analyst.py brief TSLA --no-sec              # Yahoo only, skip SEC EDGAR
  python3 scripts/analyst.py brief NVDA --filing-days 90 --json
  python3 scripts/analyst.py review                            # closed trades, last 30 days
  python3 scripts/analyst.py review --days 90 --symbol TSLA --json

Exit codes: 0 ok, 1 bad input or state, 2 data source unreachable.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trader_common as tc  # noqa: E402

import argparse  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import textwrap  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple  # noqa: E402

DAILY_RANGE = "6mo"
INTRADAY_RANGE = "1mo"           # ~21 sessions of 5m bars: a 20-session time-of-day baseline
BAR_SECONDS = 300
OPEN_MIN, CLOSE_MIN = tc.MARKET_OPEN, tc.MARKET_CLOSE
RVOL_SESSIONS = 20
MIN_RVOL_SESSIONS = 3
SLOPE_FLAT_PCT = 0.05            # |20-day regression slope| under this (% per day) reads as flat
FLAT_GAP_PCT = 0.25              # |gap| under this % is a flat open
STALE_DAILY_DAYS = 4             # calendar days without a new daily bar before we call data stale
QUIET_MINUTES = 30               # no 5m bar this long in market hours -> halt / early close / delay
FILING_DAYS = 30
SHARES_URL = ("https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/dei/"
              "EntityCommonStockSharesOutstanding.json")
SHARES_STALE_DAYS = 400
SYMBOL_RE = re.compile(r"^\^?[A-Z0-9][A-Z0-9.\-=]{0,14}$")
MARKET_WORDS = {"open": "market open", "pre-market": "pre-market", "after-hours": "after the close",
                "closed": "market closed"}

# Review
SCRATCH_EPS = 0.005              # |P&L| below half a cent is a scratch, not a win or a loss
MIN_BUCKET = 3                   # a bucket needs this many trades before a takeaway mentions it
EXIT_LABELS = {"target": "target", "stop": "stop", "eod": "time stop", "manual": "manual"}
EXIT_ORDER = ["target", "stop", "time stop", "manual", "other"]
PHASES = (("pre-market", 0, OPEN_MIN), ("09:30-10:30", OPEN_MIN, 630), ("10:30-14:00", 630, 840),
          ("14:00-16:00", 840, CLOSE_MIN), ("after-hours", CLOSE_MIN, 24 * 60))
TM_ORDER = ["leaned your way", "coin flip", "leaned against you", "not checked"]


# ---------------------------------------------------------------------------
# Small helpers (pure)
# ---------------------------------------------------------------------------

def _num(x: Any) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _rpx(x: Optional[float]) -> Optional[float]:
    """Round a price for JSON: cents at $1 and up, 4 decimals below."""
    if x is None:
        return None
    return round(x, 2 if abs(x) >= 1 else 4)


def _rdiff(x: Optional[float], ref: Optional[float]) -> Optional[float]:
    """Round a price difference at the tick of the price it belongs to."""
    if x is None:
        return None
    v = round(x, 2 if ref is None or abs(ref) >= 1 else 4)
    return 0.0 if v == 0 else v


def _r(x: Optional[float], nd: int = 2) -> Optional[float]:
    if x is None:
        return None
    v = round(x, nd)
    return 0.0 if v == 0 else v   # no "-0.0"


def _tm_pct(tm: Dict[str, Any], key: str) -> Optional[float]:
    """The Time Machine's `key` odds ('target_first' / 'stop_first') as its card printed them: the
    one-decimal `display_pct` it saved (so the review quotes the same numbers as the time-machine,
    risk and broker cards), else the raw `<key>_pct` of an older ticket."""
    disp = tm.get("display_pct")
    shown = _num(disp.get(key)) if isinstance(disp, dict) else None
    return shown if shown is not None else _num(tm.get(f"{key}_pct"))


def _pct_from(price: Optional[float], ref: Optional[float]) -> Optional[float]:
    """How far `price` is from `ref`, in % of ref."""
    if price is None or not ref:
        return None
    return (price / ref - 1.0) * 100.0


def fmt_diff(x: float, ref: Optional[float]) -> str:
    """Signed price difference at the price's tick: +1.13 (or +0.0123 under $1)."""
    return f"{x:+,.2f}" if ref is None or abs(ref) >= 1 else f"{x:+.4f}"


def fmt_r(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:+.2f}R"


def _plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def parse_stamp(value: Any) -> Optional[datetime]:
    """ISO timestamp (offset, 'Z' or naive = ET wall clock) -> aware ET datetime, or None."""
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return tc.parse_et(s)
    except (TypeError, ValueError, OverflowError):
        return None


def market_phase(now: datetime) -> str:
    m = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "closed"
    if m < OPEN_MIN:
        return "pre-market"
    if m >= CLOSE_MIN:
        return "after-hours"
    return "open"


# ---------------------------------------------------------------------------
# Bars (pure)
# ---------------------------------------------------------------------------

def _session_ts(day: str) -> int:
    return int(tc.parse_et(f"{day}T09:30").timestamp())


def clean_daily(bars: Sequence[tc.Bar], today: str, now_minute: int) -> List[tc.Bar]:
    """One daily bar per trading date (the later copy wins), restamped 09:30 ET, none after today.

    Today's bar is dropped before 09:30 ET (it would be pre-market noise or, in a replay, the
    future). A bar stamped in the evening ET is a UTC-midnight stamp: it belongs to the next date.
    """
    by_date: Dict[str, tc.Bar] = {}
    for b in sorted(bars, key=lambda x: x.ts):
        dt = tc.to_et(b.ts)
        if dt.hour >= 18:
            dt += timedelta(days=1)
        day = dt.strftime("%Y-%m-%d")
        if day > today or (day == today and now_minute < OPEN_MIN):
            continue
        by_date[day] = b
    return [tc.Bar(_session_ts(d), b.open, b.high, b.low, b.close, b.volume)
            for d, b in sorted(by_date.items())]


def clean_intraday(bars: Sequence[tc.Bar], now_ts: float) -> List[tc.Bar]:
    """Regular-session 5m bars, one per slot (Yahoo can repeat the live bar), started before now."""
    by_slot: Dict[int, tc.Bar] = {}
    for b in sorted(bars, key=lambda x: x.ts):
        if tc.in_regular_session(b.ts):
            by_slot[b.ts // BAR_SECONDS] = b   # later copy is the fresher one
    out: List[tc.Bar] = []
    for k, b in sorted(by_slot.items()):
        start = k * BAR_SECONDS
        if start >= now_ts:
            continue
        out.append(b if b.ts == start else tc.Bar(start, b.open, b.high, b.low, b.close, b.volume))
    return out


def aggregate(bars: Sequence[tc.Bar]) -> tc.Bar:
    """Intraday bars -> one session bar."""
    return tc.Bar(bars[0].ts, bars[0].open, max(b.high for b in bars), min(b.low for b in bars),
                  bars[-1].close, sum(b.volume for b in bars))


def regression_slope(values: Sequence[float]) -> Optional[float]:
    """Least-squares slope per step."""
    n = len(values)
    if n < 2:
        return None
    mx = (n - 1) / 2.0
    my = sum(values) / n
    den = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (v - my) for i, v in enumerate(values)) / den


# ---------------------------------------------------------------------------
# Brief: analysis (pure)
# ---------------------------------------------------------------------------

def trend_view(closes: Sequence[float], last: float) -> Dict[str, Any]:
    """Price vs the 20/50-day SMAs of completed sessions plus the 20-day regression slope."""
    sma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
    slope_pct = None
    if len(closes) >= 20:
        w = closes[-20:]
        slope_pct = regression_slope(w) / (sum(w) / 20) * 100.0  # type: ignore[operator]
    direction = None
    if slope_pct is not None:
        direction = ("rising" if slope_pct > SLOPE_FLAT_PCT else
                     "falling" if slope_pct < -SLOPE_FLAT_PCT else "flat")
    if sma20 is None:
        label = "UNKNOWN"
    else:
        ref = sma50 if sma50 is not None else None
        up = last > sma20 and (ref is None or sma20 > ref) and direction == "rising"
        down = last < sma20 and (ref is None or sma20 < ref) and direction == "falling"
        label = ("UPTREND" if up else "DOWNTREND" if down else
                 "SIDEWAYS" if direction == "flat" else "MIXED")
    parts: List[str] = []
    for n, v in ((20, sma20), (50, sma50)):
        if v is not None:
            parts.append(f"{'above' if last >= v else 'below'} the {n}-day SMA {tc.fmt_price(v)} "
                         f"({_pct_from(last, v):+.1f}%)")
    if sma20 is not None and sma50 is not None:
        parts.append(f"20-day SMA {'above' if sma20 >= sma50 else 'below'} the 50-day")
    if slope_pct is not None:
        parts.append(f"20-day slope {slope_pct:+.2f}%/day ({direction})")
    if sma20 is None:
        parts.append(f"only {_plural(len(closes), 'completed daily bar')} - need 20 for a trend read")
    elif sma50 is None:
        parts.append(f"only {len(closes)} daily bars, no 50-day SMA")
    return {"label": label, "sma20": _rpx(sma20), "sma50": _rpx(sma50),
            "price_vs_sma20_pct": _r(_pct_from(last, sma20)), "price_vs_sma50_pct": _r(_pct_from(last, sma50)),
            "slope_20d_pct_per_day": _r(slope_pct, 3), "slope_dir": direction,
            "slope_20d_pct_total": _r(None if slope_pct is None else slope_pct * 19),
            "detail": "; ".join(parts)}


def key_levels(hist: Sequence[tc.Bar], last: float, atr: Optional[float], sma20: Optional[float],
               sma50: Optional[float], vwap: Optional[float]) -> List[Dict[str, Any]]:
    """Support/resistance ladder, highest first. Equal prices are merged into one row."""
    raw: List[Tuple[str, float]] = []
    if hist:
        p = hist[-1]
        raw += [("prior day high", p.high), ("prior day low", p.low), ("prior day close", p.close)]
        w = hist[-20:]
        if len(w) >= 2:
            raw += [(f"{len(w)}-day high", max(b.high for b in w)), (f"{len(w)}-day low", min(b.low for b in w))]
        if len(hist) > 20:
            span = "6-month" if len(hist) >= 100 else f"{len(hist)}-day"
            raw += [(f"{span} high", max(b.high for b in hist)), (f"{span} low", min(b.low for b in hist))]
    if sma20 is not None:
        raw.append(("20-day SMA", sma20))
    if sma50 is not None:
        raw.append(("50-day SMA", sma50))
    if vwap is not None:
        raw.append(("today's VWAP", vwap))
    merged: Dict[float, List[str]] = {}
    for name, price in raw:
        key = _rpx(price)
        names = merged.setdefault(key, [])  # type: ignore[arg-type]
        if name not in names:
            names.append(name)
    out = []
    for price in sorted(merged, reverse=True):
        out.append({"name": " = ".join(merged[price]), "price": price,
                    "from_last_pct": _r(_pct_from(price, last)),
                    "from_last_atr": _r((price - last) / atr) if atr else None})
    return out


def time_of_day_rvol(today_bars: Sequence[tc.Bar], past: Dict[str, List[tc.Bar]],
                     now_minute: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Today's volume through the last completed 5m slot vs the average of past sessions through
    the same clock time. Returns (result, reason-if-none)."""
    cutoff = min((now_minute // 5) * 5, CLOSE_MIN)
    if today_bars:
        last_end = tc.minute_of_day(today_bars[-1].ts) + 5
        if cutoff - last_end >= QUIET_MINUTES:   # early close or halt: compare through the last bar
            cutoff = last_end
    if cutoff <= OPEN_MIN:
        return None, "no completed 5-minute bar yet today"
    today_vol = sum(b.volume for b in today_bars if tc.minute_of_day(b.ts) < cutoff)
    cums: List[float] = []
    for day in sorted(past, reverse=True):
        s = past[day]
        if not s or tc.minute_of_day(s[-1].ts) + 5 < cutoff - QUIET_MINUTES:
            continue  # a half day (or a gap in the data) that ended before this clock time
        cums.append(sum(b.volume for b in s if tc.minute_of_day(b.ts) < cutoff))
        if len(cums) >= RVOL_SESSIONS:
            break
    if len(cums) < MIN_RVOL_SESSIONS:
        return None, f"only {len(cums)} comparable past sessions in the 5-minute data"
    base = sum(cums) / len(cums)
    if base <= 0:
        return None, "past sessions show no volume by this time"
    return {"value": round(today_vol / base, 2), "method": "time_of_day", "through": tc.hhmm(cutoff),
            "sessions": len(cums), "volume": round(today_vol), "baseline": round(base)}, None


def daily_rvol(volume: float, base: Sequence[tc.Bar]) -> Optional[Dict[str, Any]]:
    w = [b.volume for b in base[-20:]]
    if len(w) < 5:
        return None
    avg = sum(w) / len(w)
    if avg <= 0:
        return None
    return {"value": round(volume / avg, 2), "method": "daily", "sessions": len(w),
            "volume": round(volume), "baseline": round(avg)}


def gap_view(session: tc.Bar, prev_close: Optional[float], atr: Optional[float],
             bars: Sequence[tc.Bar] = ()) -> Optional[Dict[str, Any]]:
    """Open vs prior close, in % and ATRs, and whether price has traded back to the prior close."""
    if not prev_close or prev_close <= 0:
        return None
    g = session.open - prev_close
    pct = g / prev_close * 100.0
    direction = "flat" if abs(pct) < FLAT_GAP_PCT else ("up" if g > 0 else "down")
    filled = filled_at = None
    if direction == "up":
        filled = session.low <= prev_close
    elif direction == "down":
        filled = session.high >= prev_close
    if filled:
        for b in bars:
            if (b.low <= prev_close) if direction == "up" else (b.high >= prev_close):
                filled_at = b.time
                break
    return {"open": _rpx(session.open), "prior_close": _rpx(prev_close), "gap": _rdiff(g, prev_close),
            "gap_pct": _r(pct), "gap_atr": _r(g / atr) if atr else None, "direction": direction,
            "filled": filled, "filled_at": filled_at}


# ---------------------------------------------------------------------------
# Brief: SEC context (pure parsing + a thin fetcher)
# ---------------------------------------------------------------------------

_DILUTION = re.compile(r"^(S-1|S-3|F-1|F-3)(ASR|MEF)?(/A)?$|^424B[1-8]$")
_OWNERSHIP = re.compile(r"^(SC|SCHEDULE) 13([DG])(/A)?$")
_PERIODIC = {"10-Q": "quarterly report", "10-K": "annual report", "10-Q/A": "quarterly report (amended)",
             "10-K/A": "annual report (amended)", "10-KT": "transition report",
             "20-F": "annual report (foreign issuer)", "40-F": "annual report (Canadian issuer)"}
_EVENTS = {"8-K", "8-K/A", "6-K", "6-K/A"}
_INSIDER = {"4": "form4", "4/A": "form4", "3": "form3", "3/A": "form3", "5": "form5", "5/A": "form5",
            "144": "form144", "144/A": "form144"}


def classify_filing(f: Dict[str, Any]) -> Dict[str, Any]:
    """Add kind/label/dilution to one filing from tc.sec_recent_filings."""
    form = str(f.get("form") or "").strip().upper()
    items = [str(i) for i in (f.get("items") or [])]
    item_rows = [{"code": i, "name": tc.EIGHT_K_ITEMS.get(i, f"item {i}")} for i in items]
    out = {"form": form, "filed": f.get("filed"), "url": f.get("url"), "items": item_rows,
           "kind": "other", "label": form, "dilution": False}
    if form in _EVENTS:
        shown = [r["name"] for r in item_rows if r["code"] != "9.01" or len(item_rows) == 1]
        label = "; ".join(shown) if shown else ("foreign issuer report" if form.startswith("6-K")
                                                 else "current report")
        out.update(kind="event", label=label + (" (amended)" if form.endswith("/A") else ""),
                   dilution="3.02" in items)
    elif form in _PERIODIC:
        out.update(kind="periodic", label=_PERIODIC[form])
    elif _DILUTION.match(form):
        what = ("prospectus" if form.startswith("424B") else
                "shelf registration" if form[:3] in ("S-3", "F-3") else "registration statement")
        out.update(kind="offering", label=what, dilution=True)
    elif _OWNERSHIP.match(form):
        activist = _OWNERSHIP.match(form).group(2) == "D"  # type: ignore[union-attr]
        out.update(kind="ownership",
                   label="activist / 5%+ holder (13D)" if activist else "passive 5%+ holder (13G)")
    elif form in _INSIDER:
        out.update(kind="insider", label=_INSIDER[form])
    return out


def summarize_filings(filings: Sequence[Dict[str, Any]], since: str, today: str) -> Dict[str, Any]:
    """Newest-first notable filings in [since, today], insider filing counts and other counts."""
    notable: List[Dict[str, Any]] = []
    insider = {"form4": 0, "form144": 0, "form3": 0, "form5": 0}
    other: Dict[str, int] = {}
    for f in filings:
        filed = str(f.get("filed") or "")
        if not filed or filed < since or filed > today:
            continue
        c = classify_filing(f)
        if c["kind"] == "insider":
            insider[c["label"]] += 1
        elif c["kind"] == "other":
            other[c["form"] or "?"] = other.get(c["form"] or "?", 0) + 1
        else:
            notable.append(c)
    notable.sort(key=lambda c: c["filed"] or "", reverse=True)
    return {"since": since, "notable": notable, "insider": insider, "other": other,
            "dilution_flags": sum(1 for c in notable if c["dilution"]),
            "earnings_8k": any(any(i["code"] == "2.02" for i in c["items"]) for c in notable)}


def latest_shares(concept: Any, today: str) -> Optional[Dict[str, Any]]:
    """Latest dei:EntityCommonStockSharesOutstanding from a companyconcept payload.

    Uses the most recent cover-page date ('end'); several values in the same filing for that date
    are separate share classes and are summed.
    """
    units = (concept or {}).get("units") if isinstance(concept, dict) else None
    if not isinstance(units, dict):
        return None
    rows = units.get("shares")
    if not isinstance(rows, list):
        rows = next((v for v in units.values() if isinstance(v, list)), [])
    good = [r for r in rows if isinstance(r, dict) and _num(r.get("val")) is not None and _num(r.get("val")) > 0
            and isinstance(r.get("end"), str) and r["end"] <= today
            and (not r.get("filed") or str(r["filed"]) <= today)]
    if not good:
        return None
    end = max(r["end"] for r in good)
    same = [r for r in good if r["end"] == end]
    filed = max(str(r.get("filed") or "") for r in same)
    same = [r for r in same if str(r.get("filed") or "") == filed]
    accn = max(str(r.get("accn") or "") for r in same)
    vals: List[float] = []
    for r in same:
        if str(r.get("accn") or "") == accn and float(r["val"]) not in vals:
            vals.append(float(r["val"]))
    form = next((r.get("form") for r in same if str(r.get("accn") or "") == accn), None)
    return {"shares": int(sum(vals)), "as_of": end, "filed": filed or None, "form": form, "classes": len(vals)}


def fetch_sec(symbol: str, today: str, filing_days: int) -> Dict[str, Any]:
    """SEC EDGAR context; every part is optional and failures are recorded, never raised."""
    out: Dict[str, Any] = {"status": "ok", "cik": None, "name": None, "sic": None, "shares": None,
                           "filings": None, "errors": [], "notes": []}
    try:
        cik = tc.sec_cik(symbol)
    except tc.FetchError as e:
        out.update(status="error", errors=[str(e)])
        return out
    except (AttributeError, TypeError, KeyError, ValueError) as e:   # JSON, but not the ticker map's shape
        out.update(status="error", errors=[f"unexpected reply from {tc.SEC_TICKERS_URL} ({type(e).__name__}: {e})"])
        return out
    if not cik:
        out["status"] = "not_found"
        out["notes"].append(f"{symbol} is not in SEC's ticker list (ETF, fund, or foreign issuer?)")
        return out
    out["cik"] = cik
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=filing_days)).strftime("%Y-%m-%d")
    try:
        subs = tc.sec_submissions(cik)
        out["name"] = subs.get("name") or None
        out["sic"] = subs.get("sicDescription") or None
        filings = subs.get("filings") or {}
        dates = [d for d in ((filings.get("recent") or {}).get("filingDate") or []) if d]
        out["filings"] = summarize_filings(tc.sec_recent_filings(symbol, limit=5000, since=since), since, today)
        out["filings"]["window_days"] = filing_days
        if dates and min(dates) > since and filings.get("files"):
            out["notes"].append(f"SEC's recent-filings list only reaches back to {min(dates)}; older filings "
                                "in the window are not counted")
    except (tc.FetchError, AttributeError, TypeError, KeyError) as e:
        out["errors"].append(f"filings: {e}")
    try:
        concept = tc.get_json(SHARES_URL.format(cik=cik), ttl=86400)
        out["shares"] = latest_shares(concept, today)
        if out["shares"] is None:
            out["notes"].append("no shares-outstanding value in SEC's XBRL data")
    except tc.FetchError as e:
        if "HTTP 404" in str(e):
            out["notes"].append("shares outstanding not reported in SEC's XBRL data")
        else:
            out["errors"].append(f"shares outstanding: {e}")
    if out["errors"]:
        out["status"] = "partial" if (out["filings"] or out["shares"] or out["name"]) else "error"
    return out


# ---------------------------------------------------------------------------
# Brief: assembly (pure given the fetched inputs)
# ---------------------------------------------------------------------------

def build_brief(symbol: str, daily_raw: Sequence[tc.Bar], intraday_raw: Sequence[tc.Bar], now: datetime,
                meta: Optional[Dict[str, Any]] = None, sec: Optional[Dict[str, Any]] = None,
                intraday_error: Optional[str] = None, replay: bool = False) -> Dict[str, Any]:
    """Everything the brief shows. Daily bars are required; the rest degrades gracefully.

    `replay` ($DAYTRADER_NOW set) only changes a warning: a daily bar for today then holds the
    whole day, including trading after `now`.
    """
    symbol = symbol.upper()
    meta = meta or {}
    today = now.strftime("%Y-%m-%d")
    now_ts = now.timestamp()
    now_minute = now.hour * 60 + now.minute
    phase = market_phase(now)
    notes: List[str] = []
    warnings: List[str] = []

    if not daily_raw:
        raise tc.FetchError(f"{symbol}: Yahoo returned no daily bars")
    daily = clean_daily(daily_raw, today, now_minute)
    if not daily:
        raise ValueError(f"no {symbol} daily bars on or before {today} (Yahoo's {DAILY_RANGE} window starts "
                         f"{tc.et_date(min(b.ts for b in daily_raw))})")
    hist = [b for b in daily if b.date < today]
    today_daily = daily[-1] if daily[-1].date == today else None

    intraday = clean_intraday(intraday_raw, now_ts)
    opening_bar_pending = False
    if replay:
        # Live, the bar in progress is Yahoo's "now". In a replay that bar is finished, so its close and
        # high/low are the future: keep only bars that ended by `now`.
        done = [b for b in intraday if b.ts + BAR_SECONDS <= now_ts]
        opening_bar_pending = (any(b.date == today for b in intraday)
                               and not any(b.date == today for b in done))
        intraday = done
    sessions5 = tc.group_sessions(intraday)
    today_bars = sessions5.get(today, [])
    past5 = {d: s for d, s in sessions5.items() if d < today}
    if intraday_error:
        notes.append(f"5-minute bars unavailable ({intraday_error}); relative volume uses daily volume")

    # The session the brief describes: today's if it has started, else the last completed one.
    if today_daily is not None and now_minute >= CLOSE_MIN:
        session, source = today_daily, "daily"        # after the close the daily bar is official
    elif today_bars:
        session, source = aggregate(today_bars), "5m"
    elif today_daily is not None and not opening_bar_pending:
        session, source = today_daily, "daily"
        if not intraday_error:
            notes.append("no 5-minute bars for today; today's numbers come from the daily bar")
        if replay and now_minute < CLOSE_MIN:
            warnings.append(f"replay: without 5-minute bars, today's numbers come from the daily bar, which "
                            f"includes trading after {now.strftime('%H:%M')} ET")
    else:
        session, source = None, "daily"
    is_today = session is not None
    if session is None:
        if not hist:
            raise tc.FetchError(f"{symbol}: Yahoo returned no usable daily bars")
        session = hist[-1]
        base = hist[:-1]
        if opening_bar_pending:
            notes.append(f"replay at {now.strftime('%H:%M')} ET: today's first 5-minute bar is not finished yet - "
                         "showing the last completed session")
        elif phase == "open":
            warnings.append("market hours but no bars for today yet (holiday, halt or delayed data?)")
        if (now.date() - datetime.strptime(session.date, "%Y-%m-%d").date()).days > STALE_DAILY_DAYS:
            warnings.append(f"latest daily bar is from {session.date} - stale data (halted or delisted?)")
    else:
        base = hist
    session_5m = today_bars if is_today else past5.get(session.date, [])
    prev = base[-1] if base else None
    last = session.close

    if is_today and phase == "open" and today_bars:
        quiet = now_minute - (tc.minute_of_day(today_bars[-1].ts) + 5)
        if quiet >= QUIET_MINUTES:
            warnings.append(f"no 5-minute bars since {tc.hhmm(tc.minute_of_day(today_bars[-1].ts) + 5)} ET "
                            "(halt, early close or delayed data?)")

    atr_hist = tc.atr(hist, 14)
    trend = trend_view([b.close for b in hist], last)
    vwap = tc.vwap(today_bars)[-1] if is_today and today_bars else None
    levels = key_levels(hist, _rpx(last), atr_hist, trend["sma20"], trend["sma50"], vwap)

    w20 = hist[-20:]
    liquidity = {"avg_dollar_volume_20d": round(sum(b.close * b.volume for b in w20) / len(w20)) if w20 else None,
                 "avg_volume_20d": round(sum(b.volume for b in w20) / len(w20)) if w20 else None,
                 "sessions": len(w20)}

    rvol: Optional[Dict[str, Any]] = None
    too_early = is_today and phase == "open" and now_minute < OPEN_MIN + 5
    if too_early:
        notes.append("relative volume needs one completed 5-minute bar (after 09:35 ET)")
    elif is_today and today_bars:
        rvol, why = time_of_day_rvol(today_bars, past5, now_minute)
        if rvol is None:
            notes.append(f"time-of-day relative volume unavailable ({why})")
    if rvol is None and not too_early:
        rvol = daily_rvol(session.volume, base)
        if rvol is not None and is_today and phase == "open":
            rvol["partial_day"] = True
            notes.append("relative volume compares today's volume so far with a full average day - "
                         "it reads low early in the session")

    session_view = {
        "date": session.date, "is_today": is_today, "source": source,
        "open": _rpx(session.open), "high": _rpx(session.high), "low": _rpx(session.low), "last": _rpx(last),
        "prev_close": _rpx(prev.close) if prev else None,
        "change": _rdiff(last - prev.close, last) if prev else None,
        "change_pct": _r(_pct_from(last, prev.close)) if prev else None,
        "range": _rdiff(session.high - session.low, last),
        "range_atr_pct": _r((session.high - session.low) / atr_hist * 100, 0) if atr_hist else None,
        "volume": round(session.volume), "vwap": _rpx(vwap),
        "bars_5m": len(session_5m) or None,
        "first_bar": session_5m[0].time if session_5m else None,
        "last_bar": session_5m[-1].time if session_5m else None,
    }
    if prev is None:
        notes.append("no earlier daily bar: change and gap need a prior close (new listing?)")
    if phase == "pre-market" and not is_today:
        notes.append("pre-market trading is not included; today's gap and relative volume appear after the "
                     "09:30 ET open")

    name = None
    if sec and sec.get("name"):
        name = sec["name"]
    elif meta.get("longName") or meta.get("shortName"):
        name = meta.get("longName") or meta.get("shortName")
    sec_view = sec if sec is not None else {"status": "skipped"}
    if sec_view.get("shares") and last:
        sh = sec_view["shares"]
        if str(sh.get("form") or "").upper().startswith("20-F"):
            # A 20-F counts ordinary shares, but an ADR's price is per ADS (TSM: 1 ADS = 5 shares).
            sh["market_cap_approx"] = None
            notes.append(f"SEC's share count comes from a 20-F (foreign issuer) and counts ordinary shares; if "
                         f"{symbol} is an ADR, one ADS can stand for several shares, so no market cap is estimated")
        else:
            sh["market_cap_approx"] = round(sh["shares"] * last)
            sh["market_cap_price"] = _rpx(last)   # several classes are all priced at this ticker's price
        if sh["as_of"] < (now - timedelta(days=SHARES_STALE_DAYS)).strftime("%Y-%m-%d"):
            sh["stale"] = True
            warnings.append(f"SEC's latest shares-outstanding figure is from {sh['as_of']} - may be stale")

    return {
        "symbol": symbol, "name": name, "as_of": now.isoformat(timespec="seconds"), "market": phase,
        "price": _rpx(last),
        "session": session_view,
        "prior_day": ({"date": hist[-1].date, "high": _rpx(hist[-1].high), "low": _rpx(hist[-1].low),
                       "close": _rpx(hist[-1].close), "volume": round(hist[-1].volume)} if hist else None),
        "trend": trend,
        "levels": levels,
        "volatility": {"atr14": _rpx(atr_hist), "atr14_pct": _r(atr_hist / last * 100) if atr_hist else None,
                       "basis": f"{len(hist)} completed daily bars before {today}"},
        "liquidity": liquidity,
        "relative_volume": rvol,
        "gap": gap_view(session, prev.close if prev else None, atr_hist, session_5m) if prev else None,
        "sec": sec_view,
        "sources": {"daily": f"ok ({len(daily)} bars)",
                    "intraday": f"error: {intraday_error}" if intraday_error else f"ok ({len(intraday)} bars)",
                    "sec": sec_view.get("status")},
        "notes": notes + list(sec_view.get("notes") or []),
        "warnings": warnings,
        "disclaimer": tc.DISCLAIMER,
    }


def gather_brief(symbol: str, now: datetime, with_sec: bool = True,
                 filing_days: int = FILING_DAYS) -> Dict[str, Any]:
    """Fetch (daily required; 5m and SEC optional) and build the brief."""
    daily, meta = tc.fetch_bars(symbol, "1d", DAILY_RANGE)
    intraday: List[tc.Bar] = []
    intraday_error = None
    try:
        intraday, _ = tc.fetch_bars(symbol, "5m", INTRADAY_RANGE)
    except tc.FetchError as e:
        intraday_error = str(e)
    sec = fetch_sec(symbol, now.strftime("%Y-%m-%d"), filing_days) if with_sec else None
    return build_brief(symbol, daily, intraday, now, meta=meta, sec=sec, intraday_error=intraday_error,
                       replay=bool(os.environ.get("DAYTRADER_NOW")))


# ---------------------------------------------------------------------------
# Brief: text
# ---------------------------------------------------------------------------

def _wrap(label: str, text: str, width: int = 100) -> str:
    return textwrap.fill(text, width=width, initial_indent=f"{label:<12}", subsequent_indent=" " * 12)


def render_brief(b: Dict[str, Any]) -> str:
    s = b["session"]
    now = parse_stamp(b["as_of"])
    stamp = now.strftime("%a %Y-%m-%d %H:%M") if now else b["as_of"]
    name = f" · {b['name']}" if b.get("name") else ""
    lines = [f"BRIEF · {b['symbol']}{name} · {stamp} ET · {MARKET_WORDS.get(b['market'], b['market'])}"]
    chg = ""
    if s["change"] is not None:
        chg = (f"  {fmt_diff(s['change'], s['last'])} ({s['change_pct']:+.2f}%) vs prior close "
               f"{tc.fmt_price(s['prev_close'])}")
    rng = f"range {tc.fmt_price(s['low'])}-{tc.fmt_price(s['high'])}"
    if s["is_today"]:
        vw = f" · VWAP {tc.fmt_price(s['vwap'])}" if s.get("vwap") is not None else ""
        lines.append(f"LAST {tc.fmt_price(s['last'])}{chg} · open {tc.fmt_price(s['open'])} · {rng}{vw}")
    else:
        lines.append(f"LAST SESSION {s['date']} · close {tc.fmt_price(s['last'])}{chg} · "
                     f"open {tc.fmt_price(s['open'])} · {rng}")
    lines.append("")
    t = b["trend"]
    lines.append(_wrap("TREND", f"{t['label']} · {t['detail']}"))
    v = b["volatility"]
    if v["atr14"] is not None:
        day = "today's" if s["is_today"] else "that session's"
        rng_txt = (f" · {day} range {tc.fmt_price(s['range'])} = {s['range_atr_pct']:.0f}% of ATR"
                   if s.get("range_atr_pct") is not None else "")
        lines.append(_wrap("VOLATILITY", f"ATR(14) {tc.fmt_price(v['atr14'])} = {v['atr14_pct']:.2f}% of price"
                                         f"{rng_txt}"))
    else:
        lines.append(_wrap("VOLATILITY", "ATR(14) needs 14 completed daily bars"))
    liq = b["liquidity"]
    if liq["avg_dollar_volume_20d"] is not None:
        lines.append(_wrap("LIQUIDITY", f"avg ${tc.fmt_big(liq['avg_dollar_volume_20d'])}/day traded "
                                        f"({tc.fmt_big(liq['avg_volume_20d'])} shares), "
                                        f"last {liq['sessions']} sessions"))
    rv = b["relative_volume"]
    if rv:
        who = "today" if s["is_today"] else f"{s['date']}"
        if rv["method"] == "time_of_day":
            span = "full session" if rv["through"] == tc.hhmm(CLOSE_MIN) else f"through {rv['through']} ET"
            txt = (f"{rv['value']:.2f}x · {span} vs the same time on {rv['sessions']} prior "
                   f"sessions ({tc.fmt_big(rv['volume'])} vs avg {tc.fmt_big(rv['baseline'])})")
        else:
            txt = (f"{rv['value']:.2f}x · {who}'s volume {tc.fmt_big(rv['volume'])} vs the "
                   f"{rv['sessions']}-day average {tc.fmt_big(rv['baseline'])}"
                   + (" (partial day)" if rv.get("partial_day") else ""))
        lines.append(_wrap("REL VOLUME", txt))
    g = b["gap"]
    if g:
        atr_txt = f", {g['gap_atr']:+.2f} ATR" if g["gap_atr"] is not None else ""
        when = "" if s["is_today"] else f"{s['date']} opened "
        if g["direction"] == "flat":
            txt = f"{when}flat ({g['gap_pct']:+.2f}% vs prior close {tc.fmt_price(g['prior_close'])})"
        else:
            gap = fmt_diff(g["gap"], g["prior_close"])
            txt = (f"{when}{g['direction']} {g['gap_pct']:+.2f}% ({gap}{atr_txt}) from "
                   f"{tc.fmt_price(g['prior_close'])}")
        if g["filled"] is True:
            txt += " · filled" + (f" ({g['filled_at']} bar)" if g.get("filled_at") else "")
        elif g["filled"] is False:
            txt += " · not filled"
        lines.append(_wrap("GAP", txt))
    lines.append("")
    at_last = [lv["name"] for lv in b["levels"] if lv["price"] == b["price"]]
    others = [lv for lv in b["levels"] if lv["price"] != b["price"]]
    width = max([len(lv["name"]) for lv in others] + [10])
    lines.append("KEY LEVELS".ljust(16 + width) + "from last")
    last_row = f"> {tc.fmt_price(b['price']):>10}  " + " = ".join(["LAST"] + at_last)
    placed = False
    for lv in others:
        if not placed and lv["price"] < b["price"]:
            lines.append(last_row)
            placed = True
        atr_txt = f"  {lv['from_last_atr']:+.2f} ATR" if lv["from_last_atr"] is not None else ""
        lines.append(f"  {tc.fmt_price(lv['price']):>10}  {lv['name']:<{width}}  "
                     f"{lv['from_last_pct']:+6.1f}%{atr_txt}")
    if not placed:
        lines.append(last_row)
    lines.append("")
    lines.extend(render_sec(b["sec"], b["symbol"]))
    for n in b["notes"]:
        lines.append(f"- {n}")
    for w in b["warnings"]:
        lines.append(f"! {w}")
    lines.append(b["disclaimer"])
    return "\n".join(lines)


def render_sec(sec: Dict[str, Any], symbol: str) -> List[str]:
    st = sec.get("status")
    if st == "skipped":
        return ["SEC         skipped (--no-sec)"]
    if st == "not_found":
        return ["SEC         " + ((sec.get("notes") or [f"{symbol} not found"])[0])]
    if st == "error":
        return [_wrap("SEC", "unavailable - " + "; ".join(sec.get("errors") or ["unknown error"]))]
    head = [x for x in (sec.get("name"), f"CIK {sec['cik']}" if sec.get("cik") else None,
                        sec.get("sic")) if x]
    out = [_wrap("SEC", " · ".join(head))]
    sh = sec.get("shares")
    if sh:
        cls = f", {sh['classes']} share classes" if sh.get("classes", 1) > 1 else ""
        cap = ""
        if sh.get("market_cap_approx"):
            cap = f" · market cap ≈ ${tc.fmt_big(sh['market_cap_approx'])}"
            if sh.get("classes", 1) > 1 and sh.get("market_cap_price") is not None:
                cap += f" if all {sh['classes']} classes traded at {tc.fmt_price(sh['market_cap_price'])}"
        out.append(" " * 12 + f"shares outstanding {tc.fmt_big(sh['shares'])} (as of {sh['as_of']}"
                   f"{', ' + sh['form'] if sh.get('form') else ''}{cls}){cap}")
    f = sec.get("filings")
    if f:
        out.append(" " * 12 + f"filings in the last {f.get('window_days', FILING_DAYS)} days (since {f['since']}):")
        shown = f["notable"][:10]
        fw = max([len(c["form"]) for c in shown] + [6])
        for c in shown:
            flag = " · possible dilution" if c["dilution"] else ""
            out.append(" " * 14 + f"{c['filed']}  {c['form']:<{fw}}  {c['label']}{flag}")
        if len(f["notable"]) > len(shown):
            out.append(" " * 14 + f"... and {len(f['notable']) - len(shown)} more (see --json)")
        if not f["notable"]:
            out.append(" " * 14 + "no 8-K, 10-Q/10-K, offering or 13D/13G filings")
        ins = f["insider"]
        bits = []
        if ins["form4"]:
            bits.append(f"{ins['form4']} Form 4 (insider trades)")
        if ins["form144"]:
            bits.append(f"{ins['form144']} Form 144 (planned insider sales)")
        if ins["form3"] + ins["form5"]:
            bits.append(f"{ins['form3'] + ins['form5']} Form 3/5")
        n_other = sum(f["other"].values())
        if n_other:
            bits.append(_plural(n_other, "other filing"))
        out.append(" " * 14 + ("insiders: " + " · ".join(bits) if bits else "no insider (Form 4) filings"))
    for e in sec.get("errors") or []:
        out.append(" " * 12 + f"(unavailable: {e})")
    return out


# ---------------------------------------------------------------------------
# Review: journal analytics (pure)
# ---------------------------------------------------------------------------

def tm_lean(tm: Any) -> str:
    if not isinstance(tm, dict) or not tm.get("verdict"):
        return "not checked"
    v = str(tm["verdict"]).upper()
    if "YOUR WAY" in v:
        return "leaned your way"
    if "AGAINST" in v:
        return "leaned against you"
    if "COIN" in v:
        return "coin flip"
    return "not checked"


def phase_of(minute: Optional[int]) -> str:
    if minute is None:
        return "unknown"
    for name, lo, hi in PHASES:
        if lo <= minute < hi:
            return name
    return "unknown"


def trade_row(t: Dict[str, Any], tickets: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize one closed ledger trade; None if it lacks a P&L or any timestamp."""
    side = str(t.get("side") or "").strip().lower()
    entry, exit_, stop, qty = (_num(t.get(k)) for k in ("entry_price", "exit_price", "stop", "qty"))
    pnl = _num(t.get("pnl"))
    if pnl is None and None not in (entry, exit_, qty) and side in ("long", "short"):
        pnl = (exit_ - entry) * qty if side == "long" else (entry - exit_) * qty  # type: ignore[operator]
    if pnl is None:
        return None
    r = _num(t.get("r_multiple"))
    if r is None and None not in (entry, stop, qty):
        risk = abs(entry - stop) * abs(qty)  # type: ignore[operator]
        r = pnl / risk if risk > 0 else None
    opened, closed = parse_stamp(t.get("opened_at")), parse_stamp(t.get("closed_at"))
    when = closed or opened
    if when is None:
        return None
    tm = t.get("time_machine")
    if not (isinstance(tm, dict) and tm.get("verdict")):
        k = tickets.get(str(t.get("ticket_id")))
        tm = k.get("time_machine") if k and isinstance(k.get("time_machine"), dict) else tm
    hold = (closed - opened).total_seconds() / 60.0 if opened and closed and closed >= opened else None
    setup = "-".join(str(t.get("setup") or "").lower().split()) or "unlabeled"   # "Gap and go" -> "gap-and-go"
    return {"id": str(t.get("id") or "?"), "symbol": str(t.get("symbol") or "?").upper(),
            "side": side if side in ("long", "short") else "unknown", "setup": setup,
            "pnl": pnl, "r": r, "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"),
            "entry_minute": opened.hour * 60 + opened.minute if opened else None,
            "closed_date": when.strftime("%Y-%m-%d"), "closed_ts": when.timestamp(),
            "hold_minutes": hold, "exit": EXIT_LABELS.get(str(t.get("exit_reason") or ""), "other"),
            "tm_lean": tm_lean(tm), "tm": tm if isinstance(tm, dict) else None}


def journal_rows(ledger: Dict[str, Any], since: str, until: str, symbol: Optional[str] = None
                 ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Closed trades whose close date is in [since, until], oldest close first, plus counts."""
    trades = ledger.get("trades")
    tickets = ledger.get("tickets")
    if not isinstance(trades, list) or not isinstance(tickets, list):
        raise tc.StateError("ledger.json: 'trades' and 'tickets' must be lists - fix or delete the file")
    by_id = {str(k["id"]): k for k in tickets if isinstance(k, dict) and k.get("id")}
    rows: List[Dict[str, Any]] = []
    counts = {"open": 0, "skipped": 0, "outside_window": 0}
    for t in trades:
        if not isinstance(t, dict):
            counts["skipped"] += 1
            continue
        if symbol and str(t.get("symbol") or "").upper() != symbol:
            continue
        if t.get("status") == "open":
            counts["open"] += 1
            continue
        if t.get("status") != "closed":
            continue
        row = trade_row(t, by_id)
        if row is None:
            counts["skipped"] += 1
        elif not since <= row["closed_date"] <= until:
            counts["outside_window"] += 1
        else:
            rows.append(row)
    rows.sort(key=lambda r: (r["closed_ts"], r["id"]))
    return rows, counts


def bucket_stats(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    pnls = [r["pnl"] for r in rows]
    wins = [p for p in pnls if p > SCRATCH_EPS]
    losses = [p for p in pnls if p < -SCRATCH_EPS]
    rs = [r["r"] for r in rows if r["r"] is not None]
    gross_loss = -sum(losses)
    return {"n": n, "wins": len(wins), "losses": len(losses), "scratches": n - len(wins) - len(losses),
            "win_rate_pct": _r(len(wins) / n * 100, 1) if n else None,
            "net_pnl": round(sum(pnls), 2), "avg_pnl": _r(_mean(pnls)),
            "avg_r": _r(_mean(rs), 3), "total_r": _r(sum(rs)) if rs else None,
            "profit_factor": _r(sum(wins) / gross_loss) if gross_loss > 0 else None}


def group_stats(rows: Sequence[Dict[str, Any]], key: Callable[[Dict[str, Any]], str],
                order: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    if order is not None:
        keys = [k for k in order if k in groups] + sorted(k for k in groups if k not in order)
    else:
        keys = sorted(groups, key=lambda k: (-len(groups[k]), k))
    return [dict(key=k, **bucket_stats(groups[k])) for k in keys]


def max_drawdown(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Largest peak-to-trough fall of cumulative closed P&L (as a positive dollar amount)."""
    equity = peak = 0.0
    peak_at: Optional[str] = None
    best = {"usd": 0.0, "peak_at": None, "trough_at": None}
    for r in rows:
        equity += r["pnl"]
        if equity > peak:
            peak, peak_at = equity, r["closed_at"]
        if peak - equity > best["usd"] + 1e-9:
            best = {"usd": peak - equity, "peak_at": peak_at, "trough_at": r["closed_at"]}
    best["usd"] = round(best["usd"], 2)
    return best


def streaks(rows: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    """(longest winning streak, longest losing streak); a scratch ends both."""
    w = l = bw = bl = 0
    for r in rows:
        if r["pnl"] > SCRATCH_EPS:
            w, l = w + 1, 0
        elif r["pnl"] < -SCRATCH_EPS:
            w, l = 0, l + 1
        else:
            w = l = 0
        bw, bl = max(bw, w), max(bl, l)
    return bw, bl


def _trade_brief(r: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    return {k: r[k] for k in ("id", "symbol", "side", "setup", "pnl", "r", "closed_at", "exit")}


def review_stats(rows: Sequence[Dict[str, Any]], account: Optional[float] = None) -> Dict[str, Any]:
    """Journal analytics over normalized rows (oldest close first)."""
    s = bucket_stats(rows)
    winners = [r for r in rows if r["pnl"] > SCRATCH_EPS]
    losers = [r for r in rows if r["pnl"] < -SCRATCH_EPS]
    rs = [r["r"] for r in rows if r["r"] is not None]
    avg_win_r = _mean([r["r"] for r in winners if r["r"] is not None])
    avg_loss_r = _mean([r["r"] for r in losers if r["r"] is not None])
    payoff = avg_win_r / abs(avg_loss_r) if avg_win_r and avg_loss_r else None
    hold = lambda rr: _r(_mean([r["hold_minutes"] for r in rr if r["hold_minutes"] is not None]), 1)  # noqa: E731
    bw, bl = streaks(rows)
    dd = max_drawdown(rows)
    if account:
        dd["pct_account"] = _r(dd["usd"] / account * 100)
    tm_rows = [r for r in rows if r["tm"] and _tm_pct(r["tm"], "target_first") is not None
               and _tm_pct(r["tm"], "stop_first") is not None]
    tm_check = None
    if tm_rows:
        k = len(tm_rows)
        tm_check = {
            "n": k,
            "predicted_target_first_pct": _r(_mean([_tm_pct(r["tm"], "target_first") for r in tm_rows]), 1),
            "predicted_stop_first_pct": _r(_mean([_tm_pct(r["tm"], "stop_first") for r in tm_rows]), 1),
            "actual_target_pct": _r(sum(r["exit"] == "target" for r in tm_rows) / k * 100, 1),
            "actual_stop_pct": _r(sum(r["exit"] == "stop" for r in tm_rows) / k * 100, 1),
        }
        tm_check["actual_other_pct"] = _r(100 - tm_check["actual_target_pct"] - tm_check["actual_stop_pct"], 1)
    return {
        "count": s["n"], "wins": s["wins"], "losses": s["losses"], "scratches": s["scratches"],
        "win_rate_pct": s["win_rate_pct"],
        # Break-even (1 / (1 + payoff)) is a share of decided trades: scratches neither win nor lose.
        "win_rate_ex_scratches_pct": (_r(s["wins"] / (s["wins"] + s["losses"]) * 100, 1)
                                      if s["wins"] + s["losses"] else None),
        "net_pnl": s["net_pnl"],
        "net_pnl_pct_account": _r(s["net_pnl"] / account * 100) if account else None,
        "account_size": account,
        "avg_win": _r(_mean([r["pnl"] for r in winners])), "avg_loss": _r(_mean([r["pnl"] for r in losers])),
        "avg_win_r": _r(avg_win_r, 3), "avg_loss_r": _r(avg_loss_r, 3), "payoff_ratio": _r(payoff),
        "breakeven_win_rate_pct": _r(100 / (1 + payoff), 1) if payoff else None,
        "profit_factor": s["profit_factor"], "expectancy_r": s["avg_r"], "expectancy_usd": s["avg_pnl"],
        "total_r": s["total_r"], "trades_with_r": len(rs),
        "max_drawdown": dd,
        "best_trade": _trade_brief(max(rows, key=lambda r: (r["pnl"], r["r"] or 0)) if rows else None),
        "worst_trade": _trade_brief(min(rows, key=lambda r: (r["pnl"], r["r"] or 0)) if rows else None),
        "max_win_streak": bw, "max_loss_streak": bl,
        "avg_hold_minutes": {"all": hold(rows), "wins": hold(winners), "losses": hold(losers)},
        "by_setup": group_stats(rows, lambda r: r["setup"]),
        "by_side": group_stats(rows, lambda r: r["side"], ["long", "short"]),
        "by_hour": group_stats(rows, lambda r: (f"{r['entry_minute'] // 60:02d}:00" if r["entry_minute"] is not None
                                                else "unknown"), [f"{h:02d}:00" for h in range(24)]),
        "by_window": group_stats(rows, lambda r: phase_of(r["entry_minute"]), [p[0] for p in PHASES]),
        "by_setup_side": group_stats(rows, lambda r: f"{r['setup']} {r['side']}"),
        "by_setup_side_window": group_stats(
            rows, lambda r: f"{r['setup']} {r['side']} {phase_of(r['entry_minute'])}"),
        "by_time_machine": group_stats(rows, lambda r: r["tm_lean"], TM_ORDER),
        "by_exit": group_stats(rows, lambda r: r["exit"], EXIT_ORDER),
        "by_symbol": group_stats(rows, lambda r: r["symbol"]),
        "time_machine_check": tm_check,
    }


def _bucket_name(key: str, dim: str) -> str:
    """'orb long 09:30-10:30' -> 'orb longs entered 09:30-10:30'."""
    parts = key.split(" ", 2)
    setup = "unlabeled-setup" if parts[0] == "unlabeled" else parts[0]
    side = parts[1] + "s" if len(parts) > 1 and parts[1] in ("long", "short") else "trades"
    if dim == "setup_side_window" and len(parts) == 3:
        return f"{setup} {side} entered {parts[2]}"
    return f"{setup} {side}"


def takeaways(st: Dict[str, Any]) -> List[str]:
    """Plain-language observations, hedged for sample size. Deterministic."""
    n = st["count"]
    if n == 0:
        return []
    out: List[str] = []
    if n < 10:
        out.append(f"Only {_plural(n, 'closed trade')}: far too few to tell skill from luck - read everything "
                   "here as a hint, not a verdict.")
    elif n < 30:
        out.append(f"{n} trades is still a small sample - a few more trades can flip any pattern below.")
    e = st["expectancy_r"]
    if e is not None:
        pf = st["profit_factor"]
        pf_txt = f", profit factor {pf:.2f}" if pf is not None else ", no losing trades"
        if e > 0.05:
            out.append(f"Overall positive so far: {fmt_r(e)} per trade{pf_txt}.")
        elif e < -0.05:
            out.append(f"Overall negative so far: {fmt_r(e)} per trade{pf_txt} - this mix of trades loses money "
                       "on paper.")
        else:
            out.append(f"Overall about break-even: {fmt_r(e)} per trade{pf_txt}.")
    if st["payoff_ratio"] and st["wins"] and st["losses"]:
        wr, be = st["win_rate_ex_scratches_pct"], st["breakeven_win_rate_pct"]
        lead = (f"Leaving out {_plural(st['scratches'], 'scratch', 'scratches')}, you win" if st["scratches"]
                else "You win")
        out.append(f"{lead} {wr:.0f}% of trades and your average win is {st['payoff_ratio']:.2f}x your average "
                   f"loss; that payoff needs a win rate above {be:.0f}% to make money - you are "
                   f"{'above' if wr > be else 'below'} it.")
    if st["losses"] >= 2 and st["avg_loss_r"] is not None and st["avg_loss_r"] < -1.15:
        out.append(f"Your average loss is {fmt_r(st['avg_loss_r'])}, bigger than the 1R you planned - look for "
                   "moved stops, late manual exits or gaps through the stop.")
    for dim in ("setup_side_window", "setup_side"):
        q = [b for b in st[f"by_{dim}"] if b["n"] >= MIN_BUCKET and b["avg_r"] is not None]
        if len(q) < 2:
            continue
        pos = [b for b in q if b["avg_r"] > 0]
        if len(pos) == 1:
            b = pos[0]
            out.append(f"Your {_bucket_name(b['key'], dim)} are your only positive bucket among those with "
                       f"{MIN_BUCKET}+ trades ({fmt_r(b['avg_r'])} per trade over {b['n']}).")
        else:
            best, worst = max(q, key=lambda b: b["avg_r"]), min(q, key=lambda b: b["avg_r"])
            if best["avg_r"] > 0:
                out.append(f"Best bucket: {_bucket_name(best['key'], dim)} ({fmt_r(best['avg_r'])} per trade over "
                           f"{best['n']}).")
            if worst is not best and worst["avg_r"] < 0:
                out.append(f"Weakest bucket: {_bucket_name(worst['key'], dim)} ({fmt_r(worst['avg_r'])} per trade "
                           f"over {worst['n']}) - the first candidate to cut or rework.")
        break
    win = [b for b in st["by_window"] if b["n"] >= MIN_BUCKET and b["avg_r"] is not None]
    if len(win) >= 2:
        best, worst = max(win, key=lambda b: b["avg_r"]), min(win, key=lambda b: b["avg_r"])
        if best["avg_r"] > 0 > worst["avg_r"]:
            out.append(f"Time of day matters here: entries {best['key']} ET average {fmt_r(best['avg_r'])} "
                       f"({best['n']} trades) vs {fmt_r(worst['avg_r'])} for {worst['key']} ({worst['n']}).")
    tm = {b["key"]: b for b in st["by_time_machine"]}
    checked = n - (tm["not checked"]["n"] if "not checked" in tm else 0)
    if checked == 0:
        out.append("None of these trades carried a Time Machine verdict - run time-machine with --save before "
                   "the ticket so the next review can show whether the odds helped.")
    else:
        parts = [f"{k} {fmt_r(tm[k]['avg_r'])} over {tm[k]['n']}" for k in TM_ORDER[:3] if k in tm]
        out.append("By Time Machine verdict: " + "; ".join(parts) + ".")
        ag = tm.get("leaned against you")
        if ag:
            out.append(f"You took {_plural(ag['n'], 'trade')} the Time Machine said leaned against you "
                       f"(net {tc.fmt_money(ag['net_pnl'], signed=True)}).")
    c = st["time_machine_check"]
    if c and c["n"] >= 5:
        out.append(f"Calibration: for {c['n']} trades the Time Machine gave target-first "
                   f"{c['predicted_target_first_pct']:.1f}% on average; {c['actual_target_pct']:.0f}% actually "
                   "exited at the target" + (" (small sample - wide error bars)." if c["n"] < 30 else "."))
    h = st["avg_hold_minutes"]
    if (st["wins"] >= MIN_BUCKET and st["losses"] >= MIN_BUCKET and h["wins"] and h["losses"]
            and h["losses"] > 1.5 * h["wins"] and h["losses"] - h["wins"] >= 10):
        out.append(f"You hold losers longer than winners ({h['losses']:.0f} vs {h['wins']:.0f} min on average) "
                   "- a classic sign of hoping instead of following the stop.")
    ex = {b["key"]: b for b in st["by_exit"]}
    if n >= 4 and ex.get("time stop") and ex["time stop"]["n"] * 2 >= n:
        out.append(f"{ex['time stop']['n']} of {n} trades ran to the time stop - targets may be too far for one "
                   "session (time-machine's 'open at close' odds show how often that happens).")
    man = ex.get("manual")
    planned = [r for k, r in ex.items() if k in ("target", "stop", "time stop")]
    planned_n = sum(b["n"] for b in planned)
    if man and man["n"] >= MIN_BUCKET and planned_n >= MIN_BUCKET and man["avg_r"] is not None:
        planned_r = [b["total_r"] for b in planned if b["total_r"] is not None]
        planned_avg = sum(planned_r) / planned_n if planned_r else None
        if planned_avg is not None and man["avg_r"] < planned_avg - 0.2:
            out.append(f"Manual exits average {fmt_r(man['avg_r'])} vs {fmt_r(planned_avg)} for planned exits "
                       "(stop/target/time stop) - overriding the plan cost you on paper.")
    return out


def build_review(ledger: Dict[str, Any], days: int, today: str, symbol: Optional[str] = None,
                 account: Optional[float] = None) -> Dict[str, Any]:
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows, counts = journal_rows(ledger, since, today, symbol)
    st = review_stats(rows, account)
    st.update(days=days, since=since, until=today, symbol=symbol, open_trades=counts["open"],
              skipped_rows=counts["skipped"], older_closed_trades=counts["outside_window"],
              takeaways=takeaways(st), trades=[_trade_brief(r) for r in rows], disclaimer=tc.DISCLAIMER)
    return st


# ---------------------------------------------------------------------------
# Review: text
# ---------------------------------------------------------------------------

def _bucket_table(title: str, buckets: Sequence[Dict[str, Any]]) -> str:
    rows = [[b["key"], b["n"], tc.fmt_pct(b["win_rate_pct"]), tc.fmt_money(b["net_pnl"], signed=True),
             fmt_r(b["avg_r"]), "-" if b["profit_factor"] is None else f"{b['profit_factor']:.2f}"]
            for b in buckets]
    return tc.table(rows, [title, "trades", "win%", "net P&L", "avg R", "PF"])


def render_review(r: Dict[str, Any]) -> str:
    who = f" · {r['symbol']}" if r.get("symbol") else ""
    lines = [f"JOURNAL REVIEW{who} · last {_plural(r['days'], 'day')} ({r['since']} to {r['until']}) · "
             f"{_plural(r['count'], 'closed paper trade')}"]
    if r["count"] == 0:
        lines.append("No closed paper trades in this window. Trades are recorded by the broker skill (paper only) "
                     "after you confirm a ticket; exits are applied by `broker.py sync`.")
        if r["older_closed_trades"]:
            lines.append(f"- {_plural(r['older_closed_trades'], 'closed trade')} fall outside the window - "
                         "try a larger --days.")
        if r["open_trades"]:
            lines.append(f"- {_plural(r['open_trades'], 'open trade')} not reviewed until closed.")
        lines.append(r["disclaimer"])
        return "\n".join(lines)
    acct = f" ({r['net_pnl_pct_account']:+.2f}% of the {tc.fmt_money(r['account_size'])} paper account)" \
        if r.get("net_pnl_pct_account") is not None else ""
    lines.append(f"Net P&L {tc.fmt_money(r['net_pnl'], signed=True)}{acct} · expectancy {fmt_r(r['expectancy_r'])} "
                 f"({tc.fmt_money(r['expectancy_usd'], signed=True)}) per trade · total {fmt_r(r['total_r'])}")
    pf = "∞ (no losses)" if r["profit_factor"] is None and r["wins"] else (
        "-" if r["profit_factor"] is None else f"{r['profit_factor']:.2f}")
    wl = f"Win rate {tc.fmt_pct(r['win_rate_pct'])} ({r['wins']}W / {r['losses']}L / {r['scratches']} scratch)"
    if r["wins"]:
        wl += f" · avg win {tc.fmt_money(r['avg_win'], signed=True)} ({fmt_r(r['avg_win_r'])})"
    if r["losses"]:
        wl += f" · avg loss {tc.fmt_money(r['avg_loss'], signed=True)} ({fmt_r(r['avg_loss_r'])})"
    lines.append(f"{wl} · profit factor {pf}")
    dd = r["max_drawdown"]
    dd_txt = "none" if not dd["usd"] else (
        f"-{tc.fmt_money(dd['usd'])}" + (f" ({dd['pct_account']:.2f}% of account)" if dd.get("pct_account") else "")
        + f", {str(dd['peak_at'] or 'window start')[:16].replace('T', ' ')} to "
          f"{str(dd['trough_at'])[:16].replace('T', ' ')}")
    lines.append(f"Max drawdown {dd_txt} · longest losing streak {r['max_loss_streak']}"
                 f" · winning streak {r['max_win_streak']}")
    b, w = r["best_trade"], r["worst_trade"]
    lines.append(f"Best {tc.fmt_money(b['pnl'], signed=True)} {fmt_r(b['r'])} {b['symbol']} {b['side']} "
                 f"{str(b['closed_at'])[:10]} · worst {tc.fmt_money(w['pnl'], signed=True)} {fmt_r(w['r'])} "
                 f"{w['symbol']} {w['side']} {str(w['closed_at'])[:10]}")
    h = r["avg_hold_minutes"]
    if h["all"] is not None:
        hold = f"Avg hold {h['all']:.0f} min"
        if h["wins"] is not None:
            hold += f" · winners {h['wins']:.0f} min"
        if h["losses"] is not None:
            hold += f" · losers {h['losses']:.0f} min"
        lines.append(hold)
    for title, key in (("setup", "by_setup"), ("side", "by_side"), ("entry hour ET", "by_hour"),
                       ("entry window ET", "by_window"), ("time machine", "by_time_machine"), ("exit", "by_exit"),
                       ("symbol", "by_symbol")):
        if key == "by_symbol" and len(r[key]) < 2:
            continue
        lines.append("")
        lines.append(_bucket_table(title, r[key]))
    c = r["time_machine_check"]
    if c:
        lines.append("")
        lines.append(f"Time Machine check ({_plural(c['n'], 'trade')}): predicted target-first "
                     f"{c['predicted_target_first_pct']:.1f}% / stop-first {c['predicted_stop_first_pct']:.1f}% · "
                     f"actual exits target {c['actual_target_pct']:.0f}% / stop {c['actual_stop_pct']:.0f}% / "
                     f"other {c['actual_other_pct']:.0f}%")
    lines.append("")
    lines.append("TAKEAWAYS")
    lines.extend(f"- {t}" for t in r["takeaways"])
    extra = []
    if r["open_trades"]:
        extra.append(f"{_plural(r['open_trades'], 'open trade')} not included (run broker.py sync to apply exits)")
    if r["skipped_rows"]:
        extra.append(f"{_plural(r['skipped_rows'], 'malformed ledger row')} skipped")
    if extra:
        lines.append("")
        lines.extend(f"- {x}" for x in extra)
    lines.append(r["disclaimer"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _symbol(value: str) -> str:
    s = (value or "").strip().upper()
    if not SYMBOL_RE.match(s):
        raise argparse.ArgumentTypeError(f"{value!r} is not a ticker symbol (e.g. TSLA, BRK.B)")
    return s


def _int_range(lo: int, hi: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            v = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}, got {v}")
        return v
    return parse


def build_parser() -> argparse.ArgumentParser:
    # tc's parser (and its subparsers): bad flags are bad input, exit 1 (2 means a data source is down).
    p = tc.make_parser("Analyst: pre-trade ticker brief and post-trade review of the paper journal.")
    sub = p.add_subparsers(dest="command", metavar="{brief,review}")
    b = sub.add_parser("brief", help="trend, key levels, ATR, liquidity, relative volume, gap and SEC context")
    b.add_argument("symbol", type=_symbol, help="ticker, e.g. TSLA")
    b.add_argument("--no-sec", action="store_true", help="skip SEC EDGAR (Yahoo data only)")
    b.add_argument("--filing-days", type=_int_range(1, 365), default=FILING_DAYS,
                   help=f"SEC filings window in calendar days (default {FILING_DAYS})")
    tc.add_json_flag(b)
    r = sub.add_parser("review", help="analytics of closed paper trades from the broker's ledger")
    r.add_argument("--days", type=_int_range(1, 3650), default=30,
                   help="today and the previous N-1 calendar days, by close date (default 30)")
    r.add_argument("--symbol", type=_symbol, help="only this ticker")
    tc.add_json_flag(r)
    return p


def cmd_brief(args: argparse.Namespace, as_json: bool) -> int:
    brief = gather_brief(args.symbol, tc.now_et(), with_sec=not args.no_sec, filing_days=args.filing_days)
    tc.emit(brief, as_json, render_brief)
    return 0


def cmd_review(args: argparse.Namespace, as_json: bool) -> int:
    settings = tc.load_settings()
    account = _num(settings.get("account_size"))
    try:
        ledger = tc.load_ledger()
    except AttributeError:  # valid JSON but not an object
        raise tc.StateError(f"{tc.state_path(tc.LEDGER_FILE)} is not a ledger object; fix or delete it") from None
    review = build_review(ledger, args.days, tc.today_et(), args.symbol,
                          account if account and account > 0 else None)
    tc.emit(review, as_json, render_review)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 1
    as_json = bool(getattr(args, "json", False))
    return cmd_brief(args, as_json) if args.command == "brief" else cmd_review(args, as_json)


if __name__ == "__main__":
    tc.run(main)
