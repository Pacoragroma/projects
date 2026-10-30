#!/usr/bin/env python3
"""Entry: watch a setup on 5-minute bars, bar by bar, and say the moment it triggers.

Setups (all close-based, completed bars only - the bar still forming never triggers):
  orb    opening-range breakout: first 5m close above the high of the first N minutes (long)
         or below its low (short). Default setup, default 15 minutes.
  vwap   VWAP reclaim: a 5m close above session VWAP right after a close at/below it (long);
         short is the mirror (close below after a close at/above).
  level  a 5m close through a price you choose (--level), after trading on the other side of it.
  pdh    a 5m close above the previous session's high (long only).
  pdl    a 5m close below the previous session's low (short only).

Usage:
  python3 scripts/entry.py TSLA                                   # ORB(15), whichever side first
  python3 scripts/entry.py TSLA --setup orb --or-minutes 30 --side long --min-rvol 1.5
  python3 scripts/entry.py TSLA --setup vwap --cutoff 15:00
  python3 scripts/entry.py TSLA --level 365 --side long            # --level implies --setup level
  python3 scripts/entry.py NVDA --setup pdh --watch --interval 60 --max-minutes 90 --save
  python3 scripts/entry.py AMD --date 2026-10-29 --json           # replay a past session

Exit codes: 0 ok (WAITING, TRIGGERED or EXPIRED), 1 bad input, 2 data source unreachable,
130 watch stopped with Ctrl-C.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trader_common as tc  # noqa: E402

import argparse  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from typing import Any, Dict, List, Optional, Sequence, Tuple  # noqa: E402

SETUPS = ("orb", "vwap", "level", "pdh", "pdl")
SETUP_SIDES = {"orb": ("long", "short"), "vwap": ("long", "short"), "level": ("long", "short"),
               "pdh": ("long",), "pdl": ("short",)}
BAR_SECONDS = 300            # 5-minute bars
DEFAULT_OR_MINUTES = 15
DEFAULT_CUTOFF = "11:30"
VWAP_MIN_BARS = 3            # a VWAP reclaim can trigger on the 3rd bar of the session at the earliest
RVOL_LOOKBACK = 12           # --min-rvol compares with up to this many prior bars of the session (1 h)
RVOL_MIN_PRIOR = 3           # ...and needs at least this many, else the bar cannot qualify
STALE_MINUTES = 15           # warn when the newest completed bar is this old during the session
MAX_REJECTED = 10
DERIVED_PLAN_KEYS = ("target", "exit_plan", "time_machine", "sizing")  # computed for a given entry
EPS = 1e-9


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

def _px(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    return round(x, 2) if abs(x) >= 1 else round(x, 4)


def _et_ts(day: str, minute: int) -> float:
    """Unix seconds of `minute` (after midnight ET) on ET date `day`."""
    return tc.parse_et(f"{day}T{tc.hhmm(minute)}").timestamp()


def normalize_bars(bars: Sequence[tc.Bar]) -> List[tc.Bar]:
    """Snap bars to the 5-minute grid, merge duplicates, round prices to 4 decimals, oldest first.

    Yahoo can stamp the bar still forming with the last trade time (e.g. 10:22:37) instead of
    its start (10:20); snapping keeps "is this bar complete?" honest.
    """
    out: Dict[int, tc.Bar] = {}
    for b in sorted(bars, key=lambda x: x.ts):
        start = int(b.ts) - int(b.ts) % BAR_SECONDS
        o, h, l, c = (round(float(x), 4) for x in (b.open, b.high, b.low, b.close))
        v = float(b.volume or 0)
        cur = out.get(start)
        if cur is None:
            out[start] = tc.Bar(start, o, max(h, o, c), min(l, o, c), c, v)
        else:
            out[start] = tc.Bar(start, cur.open, max(cur.high, h, c), min(cur.low, l, c), c,
                                max(cur.volume, v))
    return [out[k] for k in sorted(out)]


def resolve_side(setup: str, side: Optional[str]) -> str:
    """'long' | 'short' | 'both' for this setup (pdh is long-only, pdl short-only)."""
    allowed = SETUP_SIDES[setup]
    s = (side or "").strip().lower()
    if s in ("", "both"):
        return "both" if len(allowed) == 2 else allowed[0]
    if s not in ("long", "short"):
        raise ValueError(f"side must be long, short or both, not {side!r}")
    if s not in allowed:
        other = "pdl" if setup == "pdh" else "pdh"
        raise ValueError(f"{setup} is a {allowed[0]}-only setup; for a {s} use --setup {other}")
    return s


def check_params(setup: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate and normalize parameters (cutoff/session_end become minutes after midnight ET)."""
    setup = (setup or "orb").strip().lower()
    if setup not in SETUPS:
        raise ValueError(f"unknown setup {setup!r}; choose from {', '.join(SETUPS)}")
    p = dict(params or {})
    orm = p.get("or_minutes")
    orm = DEFAULT_OR_MINUTES if orm is None else orm
    if setup == "orb" and (int(orm) != orm or orm < 5 or orm > 180 or int(orm) % 5):
        raise ValueError("--or-minutes must be a multiple of 5 between 5 and 180 (5-minute bars)")
    cutoff = p.get("cutoff")
    cutoff = DEFAULT_CUTOFF if cutoff in (None, "") else cutoff
    try:
        cut = tc.parse_hhmm(cutoff) if isinstance(cutoff, str) else int(cutoff)
    except ValueError as e:
        raise ValueError(f"--cutoff: {e}") from None
    if not tc.MARKET_OPEN + 5 <= cut <= tc.MARKET_CLOSE:
        raise ValueError("--cutoff must be between 09:35 and 16:00 ET")
    if setup == "orb" and cut < tc.MARKET_OPEN + int(orm) + 5:
        raise ValueError(f"--cutoff {tc.hhmm(cut)} leaves no bar after the opening range "
                         f"(it ends {tc.hhmm(tc.MARKET_OPEN + int(orm))} ET)")
    level = p.get("level")
    if setup == "level":
        if level is None:
            raise ValueError("--setup level needs --level PRICE")
        if not (math.isfinite(float(level)) and float(level) > 0):
            raise ValueError("--level must be a positive price")
    min_rvol = float(p.get("min_rvol") or 0.0)
    if not math.isfinite(min_rvol) or min_rvol < 0:
        raise ValueError("--min-rvol must be >= 0")
    end = int(p.get("session_end") or tc.MARKET_CLOSE)
    p.update(setup=setup, or_minutes=int(orm), cutoff=cut, min_rvol=min_rvol, session_end=end,
             level=None if level is None else float(level))
    return p


def setup_label(setup: str, or_minutes: int = DEFAULT_OR_MINUTES) -> str:
    return {"orb": f"ORB({or_minutes})", "vwap": "VWAP RECLAIM", "level": "LEVEL",
            "pdh": "PDH BREAK", "pdl": "PDL BREAK"}[setup]


def bar_rvol(bars: Sequence[tc.Bar], i: int, lookback: int = RVOL_LOOKBACK) -> Optional[float]:
    """Volume of bars[i] / average volume of up to `lookback` bars before it (None if < 3 prior)."""
    prior = bars[max(0, i - lookback):i]
    if len(prior) < RVOL_MIN_PRIOR:
        return None
    avg = sum(b.volume for b in prior) / len(prior)
    return bars[i].volume / avg if avg > 0 else None


def _bar_dict(b: tc.Bar, rvol: Optional[float] = None) -> Dict[str, Any]:
    return {"start": tc.et_hhmm(b.ts), "end": tc.et_hhmm(b.ts + BAR_SECONDS), "open": _px(b.open),
            "high": _px(b.high), "low": _px(b.low), "close": _px(b.close), "volume": b.volume,
            "rvol": None if rvol is None else round(rvol, 2)}


def _watch_item(side: str, level: float, armed: bool, last: Optional[float]) -> Dict[str, Any]:
    dist = None if last is None else level - last
    return {"side": side, "trigger": "close_above" if side == "long" else "close_below",
            "level": _px(level), "armed": armed,
            "distance": None if dist is None else _px(dist),
            "distance_pct": None if dist is None or not last else round(dist / last * 100, 2)}


def _since_trigger(after: Sequence[tc.Bar], side: str, entry: float, stop: float,
                   last: Optional[float], trigger_close_ts: float,
                   now_ts: Optional[float], forming: Optional[tc.Bar] = None) -> Dict[str, Any]:
    """What price did after the trigger bar: completed bars, plus the bar forming now (its high/low
    so far are prices that already traded, so a stop it went through is already hit)."""
    risk = abs(entry - stop)

    def breached(b: tc.Bar) -> bool:
        return (side == "long" and b.low <= stop) or (side == "short" and b.high >= stop)

    hit_at, in_forming = None, False
    for b in after:
        if breached(b):
            hit_at = tc.et_hhmm(b.ts + BAR_SECONDS)
            break
    if hit_at is None and forming is not None and now_ts is not None and breached(forming):
        hit_at, in_forming = tc.et_hhmm(now_ts), True
    seen = list(after) + ([forming] if forming is not None else [])
    last = last if last is not None else entry
    move = (last - entry) if side == "long" else (entry - last)
    hi = max([b.high for b in seen], default=None)
    lo = min([b.low for b in seen], default=None)
    if side == "long":
        mfe = None if hi is None else hi - entry
        mae = None if lo is None else entry - lo
    else:
        mfe = None if lo is None else entry - lo
        mae = None if hi is None else hi - entry
    return {
        "bars": len(after),
        "minutes": None if now_ts is None else max(0, int((now_ts - trigger_close_ts) // 60)),
        "last": _px(last),
        "move": _px(move),
        "move_r": round(move / risk, 2) if risk > 0 else None,
        "high": _px(hi), "low": _px(lo),
        "max_favorable_r": None if mfe is None or risk <= 0 else round(mfe / risk, 2),
        "max_adverse_r": None if mae is None or risk <= 0 else round(mae / risk, 2),
        "stop_hit": hit_at is not None,
        "stop_hit_at": hit_at,
        "stop_hit_in_forming_bar": in_forming,
    }


def find_trigger(session_bars: Sequence[tc.Bar], setup: str = "orb", side: Optional[str] = None,
                 params: Optional[Dict[str, Any]] = None,
                 prev_session: Optional[Sequence[tc.Bar]] = None) -> Dict[str, Any]:
    """Walk one session's 5-minute bars in order and report the first trigger.

    params: or_minutes (15), level, cutoff ('11:30' or minutes), min_rvol (0 = off),
            now_ts (unix seconds; None = replay, every bar complete), date ('YYYY-MM-DD', else
            taken from the bars), session_end (minutes ET, 960; 780 on half days).
    A bar is complete when its start + 5 min <= now. Only bars that close by the cutoff can
    trigger. Status: TRIGGERED, EXPIRED (cutoff reached or session over) or WAITING.
    """
    p = check_params(setup, params)
    setup = p["setup"]
    side = resolve_side(setup, side)
    want = ("long", "short") if side == "both" else (side,)
    bars = sorted(session_bars or [], key=lambda b: b.ts)
    prev = sorted(prev_session or [], key=lambda b: b.ts)
    now_ts = p.get("now_ts")
    day = p.get("date") or (bars[0].date if bars else (tc.et_date(now_ts) if now_ts is not None else None))
    if not day:
        raise ValueError("find_trigger needs session bars, a date, or now_ts")

    open_ts = _et_ts(day, tc.MARKET_OPEN)
    cutoff_ts = _et_ts(day, p["cutoff"])
    end_ts = _et_ts(day, p["session_end"])
    if now_ts is None or now_ts >= end_ts:
        horizon, over = math.inf, True
    else:
        horizon, over = float(now_ts), False
    cutoff_reached = over or horizon >= cutoff_ts
    completed = [b for b in bars if b.ts + BAR_SECONDS <= horizon]
    visible = [b for b in bars if b.ts < horizon]   # started bars (the last may still be forming)
    forming = visible[-1] if visible and visible[-1].ts + BAR_SECONDS > horizon else None
    seen = completed + ([forming] if forming is not None else [])   # prices traded so far
    n_window = sum(1 for b in completed if b.ts + BAR_SECONDS <= cutoff_ts)  # bars allowed to trigger
    last = visible[-1].close if visible else (prev[-1].close if prev else None)
    vw = tc.vwap(completed)
    min_rvol = p["min_rvol"]
    notes: List[str] = []
    rejected: List[Dict[str, Any]] = []
    reference: Dict[str, Any] = {}
    watching: List[Dict[str, Any]] = []
    trig: Optional[Tuple[int, str, Optional[float]]] = None  # (index, side, rvol)

    def qualifies(i: int, s: str) -> Tuple[bool, Optional[float]]:
        rv = bar_rvol(completed, i)
        if min_rvol > 0 and (rv is None or rv + EPS < min_rvol):
            if len(rejected) < MAX_REJECTED:
                why = (f"rvol {rv:.1f}x < {min_rvol:g}x" if rv is not None
                       else f"fewer than {RVOL_MIN_PRIOR} earlier bars to compare volume with")
                rejected.append({"time": tc.et_hhmm(completed[i].ts + BAR_SECONDS), "side": s,
                                 "close": _px(completed[i].close),
                                 "rvol": None if rv is None else round(rv, 2), "reason": why})
            return False, rv
        return True, rv

    if setup == "orb":
        orm = p["or_minutes"]
        or_start, or_end = open_ts, open_ts + orm * 60
        or_bars = [b for b in completed if or_start <= b.ts < or_end]
        if horizon >= or_end and not or_bars and completed:
            # No prints in the normal window (late open / halt): use the first N minutes of trading.
            or_start = completed[0].ts
            or_end = or_start + orm * 60
            or_bars = [b for b in completed if or_start <= b.ts < or_end]
            notes.append(f"no bars {tc.et_hhmm(open_ts)}-{tc.et_hhmm(open_ts + orm * 60)} (late open or "
                         f"halt): opening range taken from {tc.et_hhmm(or_start)}-{tc.et_hhmm(or_end)}")
        or_complete = horizon >= or_end and bool(or_bars)
        reference = {"or_start": tc.et_hhmm(or_start), "or_end": tc.et_hhmm(or_end),
                     "or_bars": len(or_bars), "or_complete": or_complete}
        if or_bars:
            hi, lo = max(b.high for b in or_bars), min(b.low for b in or_bars)
            reference.update(or_high=_px(hi), or_low=_px(lo), or_mid=_px((hi + lo) / 2),
                             or_range=_px(hi - lo))
        if or_complete:
            for i in range(n_window):
                b = completed[i]
                if b.ts < or_end:
                    continue
                s = ("long" if "long" in want and b.close > hi else
                     "short" if "short" in want and b.close < lo else None)
                if s:
                    ok, rv = qualifies(i, s)
                    if ok:
                        trig = (i, s, rv)
                        break
            if trig is None:
                watching = [_watch_item(s, hi if s == "long" else lo, True, last) for s in want]
        elif not cutoff_reached:
            if or_bars:
                notes.append(f"opening range {reference['or_start']}-{reference['or_end']} ET is still "
                             f"forming (high {tc.fmt_price(hi)}, low {tc.fmt_price(lo)} so far)")
            elif horizon < or_end:
                notes.append(f"opening range forms {reference['or_start']}-{reference['or_end']} ET")
        else:
            notes.append("no opening range: no bars in the opening window")

    elif setup == "vwap":
        for i in range(VWAP_MIN_BARS - 1, n_window):
            c, pc = completed[i].close, completed[i - 1].close
            s = ("long" if "long" in want and c > vw[i] and pc <= vw[i - 1] else
                 "short" if "short" in want and c < vw[i] and pc >= vw[i - 1] else None)
            if s:
                ok, rv = qualifies(i, s)
                if ok:
                    trig = (i, s, rv)
                    break
        if trig is not None:
            reference = {"vwap": _px(vw[trig[0]])}
        elif completed:
            reference = {"vwap": _px(vw[-1])}
            lc = completed[-1].close
            if len(completed) < VWAP_MIN_BARS:
                notes.append(f"VWAP reclaim needs {VWAP_MIN_BARS} completed bars "
                             f"(first check at {tc.et_hhmm(open_ts + VWAP_MIN_BARS * BAR_SECONDS)} ET)")
            watching = [_watch_item(s, vw[-1], lc <= vw[-1] if s == "long" else lc >= vw[-1], last)
                        for s in want]
        elif not cutoff_reached:
            notes.append(f"VWAP reclaim needs {VWAP_MIN_BARS} completed bars "
                         f"(first check at {tc.et_hhmm(open_ts + VWAP_MIN_BARS * BAR_SECONDS)} ET)")

    else:  # level, pdh, pdl
        if setup == "level":
            lvl = float(p["level"])
        else:
            if not prev:
                raise ValueError(f"{setup} needs the previous session's bars and there are none in the data")
            lvl = max(b.high for b in prev) if setup == "pdh" else min(b.low for b in prev)
        # Armed = price has been at/below the level (long) or at/above it (short) since the start.
        # The start reference is the previous close (a gap through the level counts as a break).
        start_ref = prev[-1].close if prev else (completed[0].open if completed else None)
        armed = {"long": start_ref is None or start_ref <= lvl,
                 "short": start_ref is None or start_ref >= lvl}
        for i in range(n_window):
            c = completed[i].close
            s = ("long" if "long" in want and armed["long"] and c > lvl else
                 "short" if "short" in want and armed["short"] and c < lvl else None)
            if s:
                ok, rv = qualifies(i, s)
                if ok:
                    trig = (i, s, rv)
                    break
            if c <= lvl:
                armed["long"] = True
            if c >= lvl:
                armed["short"] = True
        key = {"level": "level", "pdh": "pdh", "pdl": "pdl"}[setup]
        reference = {key: _px(lvl)}
        if trig is None:
            watching = [_watch_item(s, lvl, armed[s], last) for s in want]
        elif trig[0] == 0 and start_ref is not None:
            b0 = completed[0]
            if (trig[1] == "long" and b0.open > lvl) or (trig[1] == "short" and b0.open < lvl):
                notes.append(f"gapped through {tc.fmt_price(lvl)} at the open; the first 5m bar held it")

    # -- assemble -------------------------------------------------------------------------
    res: Dict[str, Any] = {
        "setup": setup,
        "label": setup_label(setup, p["or_minutes"]),
        "side": side,
        "status": "WAITING",
        "triggered_at": None,
        "entry": None,
        "suggested_stop": None,
        "stop_basis": None,
        "risk_per_share": None,
        "risk_pct": None,
        "reference": reference,
        "bars_checked": n_window if trig is None else trig[0] + 1,
        "last_price": _px(last),
        "last_source": "bars" if visible else ("prev_close" if prev else None),
        "last_bar": tc.et_hhmm(completed[-1].ts + BAR_SECONDS) if completed else None,
        "trigger_bar": None,
        "rvol": None,
        "since_trigger": None,
        "watching": [],
        "rejected": rejected,
        "cutoff": tc.hhmm(p["cutoff"]),
        "min_rvol": min_rvol or None,
        "session_over": over,
        "expired_reason": None,
        "context": {
            "open": _px(visible[0].open) if visible else None,
            "high": _px(max(b.high for b in seen)) if seen else None,
            "low": _px(min(b.low for b in seen)) if seen else None,
            "vwap": _px(vw[-1]) if vw else None,
            "prev_date": prev[-1].date if prev else None,
            "prev_close": _px(prev[-1].close) if prev else None,
            "pdh": _px(max(b.high for b in prev)) if prev else None,
            "pdl": _px(min(b.low for b in prev)) if prev else None,
        },
        "notes": notes,
    }
    if setup == "level":
        res["level"] = _px(p["level"])
    if setup == "orb":
        res["or_minutes"] = p["or_minutes"]

    if trig is not None:
        i, s, rv = trig
        tb = completed[i]
        entry = tb.close
        if setup == "orb":
            stop = float(reference["or_low"] if s == "long" else reference["or_high"])
            basis = "OR low" if s == "long" else "OR high"
        else:
            lvl = vw[i] if setup == "vwap" else float(next(iter(reference.values())))
            name = {"vwap": "VWAP", "level": "level", "pdh": "PDH", "pdl": "PDL"}[setup]
            if s == "long":
                stop = min(tb.low, lvl)
                basis = "trigger-bar low" if tb.low < lvl else f"back below {name}"
            else:
                stop = max(tb.high, lvl)
                basis = "trigger-bar high" if tb.high > lvl else f"back above {name}"
        stop = float(_px(stop))
        entry_px = float(_px(entry))
        # Sub-penny prints can round entry and stop to the same price: keep the stop one tick away.
        tick = 0.01 if entry_px >= 1 else 0.0001
        if s == "long" and stop >= entry_px:
            stop = float(_px(entry_px - tick))
        elif s == "short" and stop <= entry_px:
            stop = float(_px(entry_px + tick))
        res.update(status="TRIGGERED", side=s, triggered_at=tc.et_hhmm(tb.ts + BAR_SECONDS),
                   entry=entry_px, suggested_stop=stop, stop_basis=basis,
                   risk_per_share=_px(abs(entry_px - stop)),
                   risk_pct=round(abs(entry_px - stop) / entry_px * 100, 2),
                   trigger_bar=_bar_dict(tb, rv), rvol=None if rv is None else round(rv, 2))
        st = _since_trigger(completed[i + 1:], s, entry_px, stop, last, tb.ts + BAR_SECONDS,
                            None if over else horizon, None if over else forming)
        res["since_trigger"] = st
        if st["stop_hit"]:
            when = (f"in the bar forming now (by {st['stop_hit_at']} ET)" if st["stop_hit_in_forming_bar"]
                    else f"at {st['stop_hit_at']} ET")
            notes.append(f"the suggested stop {tc.fmt_price(stop)} was already hit {when} "
                         f"- this trigger has failed")
        elif not over and st["move_r"] is not None and st["move_r"] >= 1.0:
            notes.append(f"price is already {st['move_r']:+.1f}R past the entry - chasing it now means a "
                         f"worse fill and a wider stop")
    elif cutoff_reached:
        res["status"] = "EXPIRED"
        # A cutoff after an early close (half day) can never be reached: say "session over".
        res["expired_reason"] = "cutoff" if (not over or cutoff_ts <= end_ts) else "session over"
    else:
        res["watching"] = watching
        if now_ts is not None and horizon < open_ts:
            notes.append("market not open yet (regular session starts 09:30 ET)")
        elif completed and horizon - (completed[-1].ts + BAR_SECONDS) > STALE_MINUTES * 60:
            notes.append(f"newest completed bar ended {res['last_bar']} ET, "
                         f"{int((horizon - completed[-1].ts - BAR_SECONDS) // 60)} min ago - data delayed "
                         f"or trading halted?")
        elif not completed and horizon >= open_ts + 2 * BAR_SECONDS:
            notes.append("no 5-minute bars yet today (market holiday, halt, or data delay?)")
    return res


def session_end_minute(meta: Optional[Dict[str, Any]], day: str) -> int:
    """Regular-session close for `day` from Yahoo's trading periods (13:00 on half days), else 16:00."""
    meta = meta or {}
    periods: List[Any] = []
    ctp = meta.get("currentTradingPeriod")
    if isinstance(ctp, dict) and isinstance(ctp.get("regular"), dict):
        periods.append(ctp["regular"])
    tp = meta.get("tradingPeriods")
    if isinstance(tp, dict):
        tp = tp.get("regular")
    if isinstance(tp, list):
        for row in tp:
            periods.extend(x for x in (row if isinstance(row, list) else [row]) if isinstance(x, dict))
    best = tc.MARKET_CLOSE
    for per in periods:
        try:
            start, end = int(per["start"]), int(per["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if tc.et_date(start) == day and tc.et_date(end) == day:
            m = tc.minute_of_day(end)
            if tc.MARKET_OPEN + 60 <= m < best:
                best = m
    return best


def pick_session(sessions: Dict[str, List[tc.Bar]], now: datetime,
                 day: Optional[str] = None) -> Tuple[str, bool, List[str]]:
    """(session date, is_replay, notes). Today if it trades; else the latest session as a replay."""
    today = now.strftime("%Y-%m-%d")
    before_close = now.weekday() < 5 and now.hour * 60 + now.minute < tc.MARKET_CLOSE
    if day:
        if day > today:
            raise ValueError(f"--date {day} is in the future (today is {today} ET)")
        if day == today:
            if today in sessions or before_close:
                return today, False, []
            raise ValueError(f"no regular session on {day} (weekend or holiday)")
        if day not in sessions:
            raise ValueError(f"no 5-minute bars for {day} (weekend, holiday, or older than Yahoo's "
                             f"~60 days of 5-minute history)")
        return day, True, []
    if today in sessions or before_close:
        return today, False, []
    if not sessions:
        raise ValueError("no regular-session 5-minute bars in the data")
    last = list(sessions)[-1]
    return last, True, [f"no session today ({today}): replaying the last session, {last}"]


def evaluate(symbol: str, bars: Sequence[tc.Bar], meta: Optional[Dict[str, Any]], now: datetime,
             setup: str = "orb", side: Optional[str] = None, params: Optional[Dict[str, Any]] = None,
             day: Optional[str] = None) -> Dict[str, Any]:
    """Pick the session (today, --date, or the last one), then run find_trigger on it."""
    sessions = tc.group_sessions(normalize_bars(bars))
    session_day, replay, notes = pick_session(sessions, now, day)
    earlier = [d for d in sessions if d < session_day]
    prev = sessions[earlier[-1]] if earlier else None
    p = dict(params or {})
    p.update(date=session_day, now_ts=None if replay else now.timestamp(),
             session_end=session_end_minute(meta, session_day))
    res = find_trigger(sessions.get(session_day, []), setup, side, p, prev)
    out: Dict[str, Any] = {"symbol": symbol.upper(), "interval": "5m", "date": session_day,
                           "as_of": now.isoformat(timespec="seconds"), "replay": replay}
    out.update(res)
    out["notes"] = notes + res["notes"]
    if res["expired_reason"] == "cutoff" and p.get("cutoff") in (None, ""):
        out["notes"].append(f"{res['cutoff']} ET is only the default cutoff; to look for a later trigger "
                            f"re-run with --cutoff HH:MM (up to 16:00)")
    if p["session_end"] < tc.MARKET_CLOSE:
        out["notes"].append(f"short session: regular close {tc.hhmm(p['session_end'])} ET")
    out["disclaimer"] = tc.DISCLAIMER
    return out


def finished_session_note(res: Dict[str, Any]) -> Optional[str]:
    """For a trigger whose session is already over (a replay, or a check after the close): the
    warning that goes with it into the plan, since its entry has already played out."""
    if res.get("status") != "TRIGGERED" or not (res.get("session_over") or res.get("replay")):
        return None
    return f"trigger is from the finished {res.get('date')} session - re-plan from the next open"


def plan_from_result(res: Dict[str, Any], existing: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """The plan fields to save for a TRIGGERED result, and which stale plan sections it clears.

    target / exit_plan / time_machine / sizing in an existing plan were computed for a different
    entry when the entry, stop or side changes, so they are reset (to null) rather than kept.
    A trigger from a finished session is saved too (useful for replays), with
    entry_signal.session_over = true and entry_signal.note saying so; exit and time-machine
    repeat that note when they plan or test from this entry.
    """
    if res.get("status") != "TRIGGERED":
        raise ValueError(f"nothing to save: status is {res.get('status')}, not TRIGGERED")
    signal = {k: res.get(k) for k in ("setup", "label", "side", "date", "triggered_at", "entry",
                                       "suggested_stop", "stop_basis", "reference", "rvol",
                                       "trigger_bar", "cutoff", "min_rvol", "replay", "as_of")}
    if res.get("or_minutes") is not None:
        signal["or_minutes"] = res["or_minutes"]
    note = finished_session_note(res)
    signal["session_over"] = note is not None
    if note:
        signal["note"] = note
    signal["source"] = "entry"
    plan: Dict[str, Any] = {"symbol": res["symbol"], "side": res["side"], "entry": res["entry"],
                            "stop": res["suggested_stop"], "setup": res["setup"], "entry_signal": signal}
    cleared: List[str] = []
    if existing:
        def same(a: Any, b: Any) -> bool:
            try:
                return abs(float(a) - float(b)) < 1e-6
            except (TypeError, ValueError):
                return False
        unchanged = (existing.get("side") == plan["side"] and same(existing.get("entry"), plan["entry"])
                     and same(existing.get("stop"), plan["stop"]))
        if not unchanged:
            for k in DERIVED_PLAN_KEYS:
                if existing.get(k) is not None:
                    plan[k] = None
                    cleared.append(k)
    return plan, cleared


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _ref_summary(res: Dict[str, Any]) -> str:
    r = res.get("reference") or {}
    fp = tc.fmt_price
    if res["setup"] == "orb":
        if r.get("or_high") is None:
            return ""
        return f"OR {fp(r['or_low'])}–{fp(r['or_high'])}" + ("" if r.get("or_complete") else " forming")
    if res["setup"] == "vwap":
        return f"VWAP {fp(r['vwap'])}" if r.get("vwap") is not None else ""
    k = res["setup"]
    return f"{'level' if k == 'level' else k.upper()} {fp(r.get(k))}" if r.get(k) is not None else ""


def _as_of_hhmm(res: Dict[str, Any]) -> str:
    try:
        return datetime.fromisoformat(res["as_of"]).strftime("%H:%M")
    except (KeyError, TypeError, ValueError):
        return "?"


def render_header(res: Dict[str, Any]) -> str:
    parts = [f"{res['symbol']} 5m"]
    if res.get("replay"):
        parts.append(f"{res['date']} replay")
    parts.append(res["label"])
    ref = _ref_summary(res)
    st = res["status"]
    if st == "TRIGGERED":
        parts.append(f"TRIGGERED {res['triggered_at']} ET")
        parts.append(f"{res['side'].upper()} @ {tc.fmt_price(res['entry'])}" + (f" ({ref})" if ref else ""))
    elif st == "WAITING":
        parts.append(f"WAITING as of {_as_of_hhmm(res)} ET")
        if ref:
            parts.append(ref)
        if res.get("last_price") is not None:
            parts.append(f"last {tc.fmt_price(res['last_price'])}"
                         + (" (prev close)" if res.get("last_source") == "prev_close" else ""))
    else:
        why = (f"no trigger by {res['cutoff']} ET" if res.get("expired_reason") == "cutoff"
               else "session over, no trigger")
        parts.append(f"EXPIRED ({why})")
        if ref:
            parts.append(ref)
    return " · ".join(parts)


def render_line(res: Dict[str, Any]) -> str:
    """One status line per --watch poll."""
    bits = [f"{_as_of_hhmm(res)} ET", f"{res['status']:<9}", res["symbol"], res["label"]]
    if res["status"] == "TRIGGERED":
        bits.append(f"{res['side'].upper()} @ {tc.fmt_price(res['entry'])} at {res['triggered_at']}")
    if res.get("last_price") is not None:
        bits.append(f"last {tc.fmt_price(res['last_price'])}")
    for w in res.get("watching") or []:
        op = ">" if w["side"] == "long" else "<"
        pct = f" ({w['distance_pct']:+.2f}%)" if w.get("distance_pct") is not None else ""
        bits.append(f"{w['side']} {op} {tc.fmt_price(w['level'])}{pct}" + ("" if w["armed"] else " not armed"))
    bits.append(f"{res['bars_checked']} bars")
    return "  ".join(bits)


def render(res: Dict[str, Any]) -> str:
    fp = tc.fmt_price
    lines = [render_header(res)]
    r = res.get("reference") or {}
    if res["status"] == "TRIGGERED":
        tb = res["trigger_bar"]
        rv = f"  rvol {tb['rvol']:.1f}x" if tb.get("rvol") is not None else ""
        lines.append(f"  trigger bar {tb['start']}-{tb['end']} ET  O {fp(tb['open'])}  H {fp(tb['high'])}  "
                     f"L {fp(tb['low'])}  C {fp(tb['close'])}  vol {tc.fmt_big(tb['volume'])}{rv}")
        extra = f" · OR mid {fp(r['or_mid'])}" if res["setup"] == "orb" and r.get("or_mid") is not None else ""
        lines.append(f"  suggested stop {fp(res['suggested_stop'])} ({res['stop_basis']}) · risk "
                     f"{fp(res['risk_per_share'])}/share ({res['risk_pct']:.2f}%){extra}")
        st = res.get("since_trigger") or {}
        if st:
            mins = st.get("minutes")
            what = "rest of session" if res.get("session_over") else (
                "since trigger (just now)" if not mins else f"since trigger ({mins} min)")
            word = "close" if res.get("session_over") else "last"
            move_r = f", {st['move_r']:+.2f}R" if st.get("move_r") is not None else ""
            seg = [f"{word} {fp(st['last'])} ({st['move']:+.2f}{move_r})"]
            if st.get("high") is not None:
                seg.append(f"high {fp(st['high'])} · low {fp(st['low'])}")
            if st["stop_hit"]:
                seg.append(f"STOP HIT by {st['stop_hit_at']} ET (bar still forming)"
                           if st.get("stop_hit_in_forming_bar") else f"STOP HIT {st['stop_hit_at']} ET")
            else:
                seg.append("stop not hit")
            lines.append(f"  {what}: " + " · ".join(seg))
    elif res["status"] == "WAITING":
        for w in res.get("watching") or []:
            word = "above" if w["side"] == "long" else "below"
            name = "VWAP " if res["setup"] == "vwap" else ""
            if w["armed"]:
                dist = ""
                d = w.get("distance")
                if d is not None and ((w["side"] == "long" and d <= 0) or (w["side"] == "short" and d >= 0)):
                    need = (f"a close with rvol >= {res['min_rvol']:g}x" if res.get("min_rvol")
                            else "the bar to close")
                    dist = f"  (already {word} at {fp(res['last_price'])} - waiting for {need})"
                elif d is not None:
                    dist = f"  (needs {d:+.2f} / {w['distance_pct']:+.2f}% from last)"
                lines.append(f"  {w['side']:<5} on a 5m close {word} {name}{fp(w['level'])}{dist}")
            else:
                back = "at/below" if w["side"] == "long" else "at/above"
                lines.append(f"  {w['side']:<5} not armed: needs a 5m close {back} {name}{fp(w['level'])} "
                             f"first, then a close {word}")
        vol = f" with rvol >= {res['min_rvol']:g}x" if res.get("min_rvol") else ""
        lines.append(f"  checks each completed 5m bar{vol} until {res['cutoff']} ET "
                     f"({res['bars_checked']} checked so far)")
    else:
        lines.append(f"  {res['bars_checked']} completed bars checked through {res['cutoff']} ET"
                     + (f"; last {fp(res['last_price'])}" if res.get("last_price") is not None else ""))
    c = res.get("context") or {}
    ctx = []
    if c.get("vwap") is not None:
        ctx.append(f"VWAP {fp(c['vwap'])}")
    if c.get("low") is not None:
        ctx.append(f"day {fp(c['low'])}–{fp(c['high'])}")
    if c.get("pdh") is not None:
        ctx.append(f"PDH {fp(c['pdh'])} · PDL {fp(c['pdl'])} · prev close {fp(c['prev_close'])}")
    if ctx:
        lines.append("  " + " · ".join(ctx))
    rej = res.get("rejected") or []
    for x in rej[:3]:
        lines.append(f"  skipped {x['time']} {x['side']} close {fp(x['close'])}: {x['reason']}")
    if len(rej) > 3:
        lines.append(f"  ... and {len(rej) - 3} more bars skipped for light volume")
    for n in res.get("notes") or []:
        lines.append(f"  note: {n}")
    if res.get("saved"):
        sv = res["saved"]
        msg = f"  saved to plan {sv['path']}: {res['side']} entry {fp(res['entry'])} stop {fp(res['suggested_stop'])}"
        if sv.get("cleared"):
            msg += f" (cleared stale {', '.join(sv['cleared'])} - re-run exit and time-machine)"
        lines.append(msg)
    elif res.get("save_skipped"):
        lines.append(f"  not saved: {res['save_skipped']}")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = tc.make_parser("Watch a 5-minute setup (opening-range breakout, VWAP reclaim, price level, "
                       "previous-day high/low break) and report the moment it triggers.")
    p.add_argument("symbol", help="ticker, e.g. TSLA")
    p.add_argument("--setup", type=str.lower, choices=SETUPS,
                   help="orb (default), vwap, level, pdh, pdl; --level alone implies level")
    p.add_argument("--side", type=str.lower, choices=("long", "short", "both"),
                   help="default: both for orb/vwap/level, long for pdh, short for pdl")
    p.add_argument("--or-minutes", type=int, help=f"opening-range length for orb (default {DEFAULT_OR_MINUTES})")
    p.add_argument("--level", type=float, help="price for --setup level")
    p.add_argument("--cutoff", help=f"no trigger after this ET time -> EXPIRED (default {DEFAULT_CUTOFF})")
    p.add_argument("--min-rvol", type=float, help="trigger bar volume >= this x the prior bars' average")
    p.add_argument("--date", help="replay a past session (YYYY-MM-DD, last ~60 days)")
    p.add_argument("--watch", action="store_true", help="re-check every --interval until it triggers or expires")
    p.add_argument("--interval", type=float, help="seconds between checks with --watch (default 60)")
    p.add_argument("--max-minutes", type=float, help="stop watching after this long (default 120)")
    p.add_argument("--save", action="store_true", help="write a trigger into the saved plan (plans/SYM.json)")
    return p


def options_from_args(args: argparse.Namespace, now: datetime) -> Dict[str, Any]:
    sym = (args.symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-^=]{0,14}", sym):
        raise ValueError(f"not a ticker symbol: {args.symbol!r}")
    setup = args.setup
    if args.level is not None and setup not in (None, "level"):
        raise ValueError("--level only applies to --setup level")
    if setup is None:
        setup = "level" if args.level is not None else "orb"
    if args.or_minutes is not None and setup != "orb":
        raise ValueError("--or-minutes only applies to --setup orb")
    if not args.watch and (args.interval is not None or args.max_minutes is not None):
        raise ValueError("--interval and --max-minutes only apply with --watch")
    interval = 60.0 if args.interval is None else args.interval
    max_minutes = 120.0 if args.max_minutes is None else args.max_minutes
    if not (math.isfinite(interval) and interval >= 15):
        raise ValueError("--interval must be at least 15 seconds")
    if not (math.isfinite(max_minutes) and 0 < max_minutes <= 480):
        raise ValueError("--max-minutes must be between 0 and 480")
    day = None
    if args.date:
        try:
            day = datetime.strptime(args.date.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise ValueError(f"--date must be YYYY-MM-DD, got {args.date!r}") from None
        today = now.strftime("%Y-%m-%d")
        if day > today:
            raise ValueError(f"--date {day} is in the future (today is {today} ET)")
        if day < (now - timedelta(days=59)).strftime("%Y-%m-%d"):
            raise ValueError("Yahoo keeps only about 60 days of 5-minute bars; pick a more recent --date")
    params = {"or_minutes": args.or_minutes, "level": args.level, "cutoff": args.cutoff,
              "min_rvol": args.min_rvol}
    check_params(setup, params)          # fail fast on bad input, before any download
    resolve_side(setup, args.side)
    past = day is not None and day != now.strftime("%Y-%m-%d")
    return {"symbol": sym, "setup": setup, "side": args.side, "params": params, "date": day,
            "range": "60d" if past else "5d", "interval": interval, "max_minutes": max_minutes}


def fetch(symbol: str, range_: str, ttl: Optional[float] = None) -> Tuple[List[tc.Bar], Dict[str, Any]]:
    try:
        bars, meta = tc.fetch_bars(symbol, "5m", range_, ttl=ttl)
    except tc.FetchError as e:
        msg = str(e)
        if "HTTP 404" in msg or "Yahoo: Not Found" in msg:
            raise ValueError(f"{symbol}: Yahoo has no chart for this symbol (unknown or delisted ticker?)") from e
        raise
    if not bars:
        raise ValueError(f"{symbol}: Yahoo returned no 5-minute bars (thinly traded or not a US listing?)")
    return bars, meta


def check_once(opts: Dict[str, Any], ttl: Optional[float] = None) -> Dict[str, Any]:
    bars, meta = fetch(opts["symbol"], opts["range"], ttl)
    return evaluate(opts["symbol"], bars, meta, tc.now_et(), opts["setup"], opts["side"],
                    opts["params"], opts["date"])


def save_result(res: Dict[str, Any]) -> None:
    """--save: merge a TRIGGERED result into plans/<SYM>.json (records why when nothing is saved)."""
    if res["status"] != "TRIGGERED":
        res["saved"] = None
        res["save_skipped"] = f"status is {res['status']} (only a TRIGGERED setup is saved)"
        return
    fname = re.sub(r"[^A-Z0-9._-]", "_", res["symbol"].upper()) + ".json"
    existing = tc.load_plan(res["symbol"])
    if existing is not None and not isinstance(existing, dict):
        raise tc.StateError(f"{tc.home() / 'plans' / fname} is not a plan (expected a JSON object); "
                            f"fix or delete it, then re-run with --save")
    plan, cleared = plan_from_result(res, existing)
    tc.save_plan(plan)
    res["saved"] = {"path": str(tc.home() / "plans" / fname), "cleared": cleared}
    note = plan["entry_signal"].get("note")
    if note:
        res["saved"]["note"] = note
        res["notes"].append(f"{note} (saved anyway, for replays; exit and time-machine will flag it)")


def watch(opts: Dict[str, Any], as_json: bool, save: bool) -> int:
    interval, max_s = opts["interval"], opts["max_minutes"] * 60
    polls = int(math.ceil(max_s / interval)) + 1
    started = time.monotonic()
    res: Optional[Dict[str, Any]] = None
    failures, stopped = 0, "max-minutes"
    try:
        for k in range(polls):
            try:
                res = check_once(opts, ttl=0)
            except tc.FetchError as e:
                failures += 1
                # Unreachable from the start (blocked host, no network): fail now, like a one-shot
                # check. Once a check has worked, ride out up to 2 transient errors in a row.
                if res is None or failures >= 3:
                    raise
                print(f"{tc.now_et().strftime('%H:%M')} ET  data error (retrying): {e}", file=sys.stderr)
            else:
                failures = 0
                print(json.dumps(dict(res, poll=k + 1), default=str) if as_json else render_line(res),
                      flush=True)
                if res["status"] != "WAITING":
                    stopped = res["status"].lower()
                    break
            if k + 1 >= polls or time.monotonic() - started + interval > max_s + 1:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        stopped = "interrupted"
    if res is None:
        if stopped == "interrupted":
            return 130
        raise tc.FetchError("no successful check during the watch")
    if save:
        save_result(res)
    res["watch"] = {"stopped": stopped, "interval": interval, "max_minutes": opts["max_minutes"]}
    if stopped == "max-minutes":
        res["notes"].append(f"stopped watching after {opts['max_minutes']:g} min - still WAITING")
    elif stopped == "interrupted":
        res["notes"].append("watch stopped with Ctrl-C")
    if as_json:
        print(json.dumps(dict(res, final=True), default=str))
    else:
        print()
        print(render(res))
    return 130 if stopped == "interrupted" else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # argparse usage errors exit 2, which here means "data source down"
        if e.code == 2:
            return 1
        raise
    opts = options_from_args(args, tc.now_et())
    if args.watch:
        return watch(opts, args.json, args.save)
    res = check_once(opts)
    if args.save:
        save_result(res)
    tc.emit(res, args.json, render)
    return 0


if __name__ == "__main__":
    tc.run(main)
