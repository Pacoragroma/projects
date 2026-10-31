#!/usr/bin/env python3
"""Exit plan: set the stop, the target and the time stop BEFORE you enter ("THE PLAN").

Stops come from today's 5-minute bars (ATR or the recent swing low/high), a fixed percentage,
or a price you give. The target is a reward:risk multiple of the stop distance (or a price you
give). The plan also reports risk and reward per share, the break-even win rate, the stop
distance in ATRs and % of price, sanity warnings, a time stop and a breakeven rule.

Usage:
  python3 scripts/exit_plan.py TSLA --entry 364.18                      # 1.5 ATR stop, 2R target
  python3 scripts/exit_plan.py TSLA --entry 364.18 --rr 1               # 1R target
  python3 scripts/exit_plan.py TSLA --entry 364.18 --method structure --lookback 12
  python3 scripts/exit_plan.py TSLA --entry 364.18 --pct 1.5            # percent stop
  python3 scripts/exit_plan.py TSLA --entry 364.18 --stop 358.32 --target 370.04
  python3 scripts/exit_plan.py TSLA --side short --entry 364.18 --save  # store in plans/TSLA.json
  python3 scripts/exit_plan.py TSLA --method manual --save              # reuse the saved plan's entry+stop
  python3 scripts/exit_plan.py TSLA --entry 364.18 --json
  python3 scripts/exit_plan.py BTC-USD --save                           # crypto: UTC day, 23:55 time stop

Crypto (a Coinbase product such as BTC-USD) trades 24/7: times are UTC, "today" is the UTC day,
the default time stop is 23:55 UTC and prices round to the product's price increment.

Exit codes: 0 ok, 1 bad input (or not enough bars for the method), 2 data source unreachable.
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
from datetime import datetime  # noqa: E402
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal  # noqa: E402
from typing import Any, Dict, List, Optional, Sequence, Tuple  # noqa: E402

METHODS = ("atr", "structure", "percent", "manual")
DEFAULTS: Dict[str, Any] = {"rr": 2.0, "atr_mult": 1.5, "pct": 1.0, "lookback": 12}
# The default time stop is the active market's (tc.market().time_stop: 15:55 ET, 23:55 UTC for crypto).
LAST_HHMM = 23 * 60 + 59     # latest time HH:MM can name (a 24/7 session closes at 24:00)
BAR_SECONDS = 300
ATR_N = 14
STRUCTURE_BUFFER_ATR = 0.1   # structure stop sits this many ATRs beyond the swing low/high
TIGHT_ATR = 0.5              # stop closer than this -> "likely to be wicked out"
WIDE_ATR = 4.0               # stop farther than this -> very wide for a day trade
FAR_ENTRY_ATR = 2.0          # entry this far from the last price -> remind it assumes a fill
FULL_SESSION_BARS = 36       # a past session counts for the average day range with >= 3 hours of bars
STALE_DATA_SECONDS = 600     # market open but no bar finished in the last 10 minutes
FEES_WARN_R = 0.25           # crypto: round-trip paper fees this big a share of R -> a warning, not a note
# Which flags belong to which methods (a flag given with another method is an error, not ignored).
FLAG_METHODS = {"stop": ("manual",), "pct": ("percent",), "lookback": ("structure",),
                "atr_mult": ("atr", "structure")}
# Plan sections other skills computed for specific levels -> the levels they depend on.
DEPENDENT_SECTIONS = {"time_machine": ("side", "entry", "stop", "target"),
                      "sizing": ("side", "entry", "stop")}


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

def _crypto() -> bool:
    return tc.market().key == "crypto"


def _tz() -> str:
    return tc.tz_label()


def tick(price: float, rounding: str = ROUND_HALF_UP, increment: Optional[float] = None) -> float:
    """Round to the quoting tick: cents at $1 and up, $0.0001 below $1 (half-up, as people round).

    Crypto: the product's price increment (Coinbase quote_increment) when given; without it,
    cents at $1 and up and 4 significant digits below (sub-cent coins quote far below $0.0001).
    The value is first cleaned to 8 decimals so float noise (98.49999999999999) cannot flip a
    floor/ceiling rounding by a whole tick.
    """
    clean = Decimal(repr(float(price))).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    if increment is None and _crypto() and 0 < clean < 1:
        increment = min(0.0001, 10.0 ** (int(math.floor(math.log10(float(clean)))) - 3))
    if increment:
        step = Decimal(repr(float(increment)))
        return float((clean / step).quantize(Decimal(1), rounding=rounding) * step)
    places = Decimal("0.01") if clean >= 1 else Decimal("0.0001")
    return float(clean.quantize(places, rounding=rounding))


def regular_bars(bars: Sequence[tc.Bar]) -> List[tc.Bar]:
    """Regular-session bars, oldest first, one per 5-minute slot (Yahoo can repeat the live bar)."""
    by_slot: Dict[int, tc.Bar] = {}
    for b in sorted(bars, key=lambda x: x.ts):
        if tc.in_regular_session(b.ts):
            by_slot[b.ts // BAR_SECONDS] = b   # later stamp wins: it is the fresher copy
    # Stamp each bar at its slot start (a live bar can carry its last-trade time, e.g. 09:42:43).
    return [b if b.ts == k * BAR_SECONDS else tc.Bar(k * BAR_SECONDS, b.open, b.high, b.low, b.close, b.volume)
            for k, b in sorted(by_slot.items())]


REPLAY_SLACK = 60   # a bar starting this long after "now" means the data runs past now (a replay)


def usable_bars(bars: Sequence[tc.Bar], now_ts: float) -> List[tc.Bar]:
    """Bars that had started by `now_ts` (never peek at bars from the future in a replay).

    Live, Yahoo's bar in progress holds only what has traded so far, so it is kept. When the data
    runs past `now_ts` (replaying a past session with $DAYTRADER_NOW), the bar in progress at
    `now_ts` already holds its whole 5 minutes - up to 5 minutes of the future - so only bars
    finished by `now_ts` are kept.
    """
    regular = regular_bars(bars)
    if any(b.ts >= now_ts + REPLAY_SLACK for b in regular):
        return [b for b in regular if b.ts + BAR_SECONDS <= now_ts]
    return [b for b in regular if b.ts < now_ts]


def session_true_ranges(bars: Sequence[tc.Bar]) -> List[float]:
    """True ranges where the first bar of each session is just high-low: an overnight gap is
    not intraday volatility and would inflate a 5-minute ATR."""
    out: List[float] = []
    prev: Optional[tc.Bar] = None
    prev_day = None
    every_day = tc.market().every_day   # 24/7 (crypto): midnight UTC is not a gap, the price runs on
    for b in bars:
        day = b.date
        if prev is not None and (day == prev_day or every_day):
            pc = prev.close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
        else:
            out.append(b.high - b.low)
        prev, prev_day = b, day
    return out


def _stamp(b: tc.Bar) -> str:
    return f"{b.date} {b.time}"


def intraday_atr(bars: Sequence[tc.Bar], today: Optional[str], n: int = ATR_N,
                 now_ts: Optional[float] = None) -> Tuple[Optional[float], Dict[str, Any]]:
    """ATR(n) of today's completed 5-minute bars (Wilder); with fewer than n today, the plain
    average true range of the last n completed bars across sessions. Returns (atr, info)."""
    complete = [b for b in bars if now_ts is None or b.ts + BAR_SECONDS <= now_ts]
    todays = [b for b in complete if b.date == today]
    info: Dict[str, Any] = {"n": n, "source": None, "bars": len(complete), "today_bars": len(todays),
                            "first": None, "last": None}
    if len(todays) >= n:
        window, value, source = todays, tc.atr(todays, n), "today"
    elif len(complete) >= n:
        window = complete[-n:]
        value, source = sum(session_true_ranges(complete)[-n:]) / n, "recent"
    else:
        return None, info
    info.update(source=source, bars=len(window), first=_stamp(window[0]), last=_stamp(window[-1]))
    return value, info


def structure_level(bars: Sequence[tc.Bar], side: str, lookback: int) -> Dict[str, Any]:
    """Lowest low (long) / highest high (short) of the last `lookback` bars, incl. a forming bar."""
    window = list(bars)[-lookback:]
    if not window:
        raise ValueError("no 5-minute bars for a structure stop - use --method percent or manual")
    if side == "long":
        ext = min(window, key=lambda b: (b.low, -b.ts))
        level = ext.low
    else:
        ext = max(window, key=lambda b: (b.high, b.ts))
        level = ext.high
    return {"level": level, "at": _stamp(ext), "lookback": lookback, "window_bars": len(window),
            "window_first": _stamp(window[0]), "window_last": _stamp(window[-1]),
            "sessions": len({b.date for b in window})}


def average_session_range(bars: Sequence[tc.Bar], today: Optional[str]) -> Tuple[Optional[float], int]:
    """Mean high-low of the complete sessions before `today` (for a target-realism check).

    Crypto: whole UTC days only (>= 90% of the 288 slots), since the first day of the 5-day
    window starts at the current time of day and is partial."""
    full = FULL_SESSION_BARS
    if tc.market().every_day:
        full = int(0.9 * (tc.MARKET_CLOSE - tc.MARKET_OPEN) // (BAR_SECONDS // 60))
    ranges = [max(b.high for b in s) - min(b.low for b in s)
              for d, s in tc.group_sessions(bars).items()
              if (today is None or d < today) and len(s) >= full]
    return (sum(ranges) / len(ranges), len(ranges)) if ranges else (None, 0)


def parse_time_stop(value: Any) -> int:
    """'15:55' -> minutes after midnight; must fall inside the regular session (09:35-16:00 ET;
    crypto: 00:05-23:59 UTC)."""
    m = value if isinstance(value, int) else tc.parse_hhmm(str(value))
    latest = min(tc.MARKET_CLOSE, LAST_HHMM)
    if not tc.MARKET_OPEN + 5 <= m <= latest:
        what = "the UTC day" if tc.market().every_day else "regular session"
        raise ValueError(f"time stop must be between {tc.hhmm(tc.MARKET_OPEN + 5)} and {tc.hhmm(latest)} "
                         f"{_tz()} ({what}), got {tc.hhmm(m) if isinstance(m, int) else value}")
    return m


def _finite_positive(value: Any, name: str, upper: Optional[float] = None) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    if upper is not None and v > upper:
        raise ValueError(f"{name} must be at most {upper:g}, got {v:g}")
    return v


def _dist(x: float) -> float:
    """A price distance (risk, ATR) for JSON: 4 decimals; sub-cent crypto keeps 4 significant digits."""
    if _crypto() and 0 < abs(x) < 0.01:
        return float(f"{x:.4g}")
    return round(x, 4)


def fmt_rr(rr: float) -> str:
    """2.0 -> '2.0', 1.5 -> '1.5', 1.99 -> '1.99'."""
    s = f"{rr:.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def make_plan(side: str, entry: float, method: str, bars: Sequence[tc.Bar],
              params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Stop, target and the numbers around them. Pure: no I/O.

    bars: regular-session 5-minute bars up to now, oldest first (a forming last bar is fine).
    params: stop (manual), target (instead of rr), rr, atr_mult, pct, lookback, time_stop
      'HH:MM', today 'YYYY-MM-DD', now_ts (bars ending after it are not complete),
      min_rr (warn below), last_price (stale-plan checks), market_open (False: last_price is the
      previous close, so those checks are worded as conditional on the open), tick (crypto: the
      product's price increment), fee_pct (crypto: paper fee per fill, % - adds `fees` in R).
    Defaults follow the active market (time stop 15:55 ET; 23:55 UTC for crypto).
    """
    p = dict(DEFAULTS, time_stop=tc.market().time_stop)
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    increment = p.get("tick")

    def tk(price: float, rounding: str = ROUND_HALF_UP) -> float:
        return tick(price, rounding, increment)

    side = str(side or "").lower()
    if side not in ("long", "short"):
        raise ValueError(f"side must be long or short, not {side!r}")
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; choose from {', '.join(METHODS)}")
    entry = tk(_finite_positive(entry, "entry"))
    rr_req = _finite_positive(p["rr"], "--rr", upper=50)
    atr_mult = _finite_positive(p["atr_mult"], "--atr-mult", upper=20)
    pct = _finite_positive(p["pct"], "--pct")
    if pct >= 100:
        raise ValueError(f"--pct must be below 100, got {pct:g}")
    lookback = int(p["lookback"])
    max_lookback = (tc.MARKET_CLOSE - tc.MARKET_OPEN) // (BAR_SECONDS // 60)   # one session: 78 (288 for crypto)
    if not 1 <= lookback <= max_lookback:
        raise ValueError(f"--lookback must be between 1 and {max_lookback} bars, got {lookback}")
    time_stop = tc.hhmm(parse_time_stop(p["time_stop"]))
    bars = list(bars or [])
    today = p.get("today") or (bars[-1].date if bars else None)
    sign = 1 if side == "long" else -1           # stop = entry - sign * distance
    below = "below" if side == "long" else "above"
    warnings: List[str] = []
    notes: List[str] = []

    atr_value, atr_info = intraday_atr(bars, today, now_ts=p.get("now_ts"))
    if atr_info["source"] == "recent":
        first, last = atr_info["first"], atr_info["last"]
        span = f"{first} to {last[11:] if first[:10] == last[:10] else last} {_tz()}"
        if atr_info["today_bars"]:
            ready = tc.hhmm(tc.MARKET_OPEN + ATR_N * BAR_SECONDS // 60)
            notes.append(f"today has only {atr_info['today_bars']} completed 5-minute bars, so ATR uses the "
                         f"last {ATR_N} bars across sessions ({span}); re-run after about {ready} {_tz()} for "
                         "today's own ATR")
        elif tc.market().every_day:   # 24/7: yesterday's last bars run straight into today's
            notes.append(f"no completed 5-minute bars today (UTC) yet, so ATR uses the last {ATR_N} bars "
                         f"({span})")
        else:
            notes.append(f"no 5-minute bars today yet, so ATR uses the last {ATR_N} bars of the previous "
                         f"session ({span}); the open usually swings more than the close")

    def need_atr(what: str) -> float:
        if atr_value is None:
            raise ValueError(f"{what} needs ATR({ATR_N}) of 5-minute bars, but only {atr_info['bars']} "
                             "completed bars are available - use --method percent --pct X or "
                             "--stop PRICE instead")
        if atr_value <= 0:
            raise ValueError(f"ATR({ATR_N}) is zero (the last bars did not move - halted or no trades?) "
                             "- use --method percent --pct X or --stop PRICE instead")
        return atr_value

    used = method
    structure: Optional[Dict[str, Any]] = None
    stop_raw: Optional[float] = None
    if method == "manual":
        if p.get("stop") is None:
            raise ValueError("the manual method needs a stop price (--stop PRICE)")
        stop_raw = _finite_positive(p["stop"], "stop")
    elif method == "percent":
        stop_raw = entry * (1 - sign * pct / 100.0)
    elif method == "structure":
        a = need_atr("the structure stop's buffer")
        structure = structure_level(bars, side, lookback)
        structure["buffer"] = _dist(STRUCTURE_BUFFER_ATR * a)
        # round away from the entry so the stop stays beyond the swing level even with a tiny ATR
        cand = tk(structure["level"] - sign * STRUCTURE_BUFFER_ATR * a,
                    ROUND_FLOOR if side == "long" else ROUND_CEILING)
        structure["stop"] = cand
        extreme = "low" if side == "long" else "high"
        if tc.market().every_day:
            pass   # 24/7: a window across midnight UTC is one continuous chart, no overnight gap
        elif structure["sessions"] > 1:
            notes.append(f"the {lookback}-bar window reaches back to {structure['window_first']} ET "
                         "(previous session)")
        elif today and structure["window_last"][:10] < today:
            notes.append(f"the {lookback}-bar window is from the previous session "
                         f"({structure['window_first']} to {structure['window_last'][11:]} ET), not today: "
                         "an overnight gap can leave that level far from today's price")
        if (cand >= entry) if side == "long" else (cand <= entry):
            notes.append(f"structure stop {tc.fmt_price(cand)} (the {lookback}-bar {extreme} "
                         f"{tc.fmt_price(structure['level'])} at {structure['at']} {_tz()}, "
                         f"{'minus' if side == 'long' else 'plus'} 0.1 ATR) is not {below} the entry "
                         f"{tc.fmt_price(entry)}, so the ATR stop is used instead")
            used = "atr"
        else:
            stop_raw = cand
    if used == "atr":
        stop_raw = entry - sign * atr_mult * need_atr("the ATR stop")

    if stop_raw is None:  # pragma: no cover - every branch above sets it or raises
        raise ValueError(f"no stop could be computed for method {method}")
    if stop_raw <= 0:
        raise ValueError(f"the {used} stop would be at or below zero ({stop_raw:.4f}) - use a tighter stop")
    stop = tk(stop_raw)
    if stop == entry:
        raise ValueError(f"the stop rounds to the entry price {tc.fmt_price(entry)} - widen it")
    errs = tc.validate_levels(side, entry, stop, None)
    if errs:
        other = "short" if side == "long" else "long"
        raise ValueError(f"{'; '.join(errs)} (entry {tc.fmt_price(entry)}, stop {tc.fmt_price(stop)}) - "
                         f"pass --side {other} if you meant a {other}")
    risk = abs(entry - stop)

    if p.get("target") is not None:
        target = tk(_finite_positive(p["target"], "target"))
        rr_requested: Optional[float] = None
    else:
        raw_target = entry + sign * rr_req * risk
        if raw_target <= 0:
            raise ValueError(f"a {rr_req:g}R target for this short would be at or below zero - lower --rr "
                             "or tighten the stop")
        target = tk(raw_target)
        rr_requested = rr_req
    errs = tc.validate_levels(side, entry, stop, target)
    if errs:
        raise ValueError(f"{'; '.join(errs)} (entry {tc.fmt_price(entry)}, target {tc.fmt_price(target)})")

    reward = abs(target - entry)
    rr = reward / risk
    stop_atr = risk / atr_value if atr_value else None
    target_atr = reward / atr_value if atr_value else None

    if stop_atr is None:
        if bars:
            notes.append(f"fewer than {ATR_N} completed 5-minute bars, so no ATR: the stop-distance "
                         "sanity check was skipped")
    elif stop_atr < TIGHT_ATR:
        warnings.append(f"stop is only {stop_atr:.2f} ATR from the entry - likely to be wicked out by "
                        "normal 5-minute noise")
    elif stop_atr > WIDE_ATR:
        warnings.append(f"stop is {stop_atr:.1f} ATR from the entry - very wide for a day trade "
                        "(small size, distant target)")
    min_rr = p.get("min_rr")
    if min_rr and round(rr, 2) < float(min_rr) - 1e-9:
        warnings.append(f"R:R {fmt_rr(rr)} is below your minimum of {fmt_rr(float(min_rr))} "
                        "(setting min_reward_risk)")
    day_range, day_count = average_session_range(bars, today)
    if day_range and reward > day_range:
        what, over = (("UTC-day", f"{day_count} days") if tc.market().every_day
                      else ("full-session", f"{day_count} sessions"))
        warnings.append(f"the target needs a {tc.fmt_price(reward)} move, more than the average {what} "
                        f"range ({tc.fmt_price(day_range)} over the last {over}): that would "
                        "be an unusually big day")
    last = p.get("last_price")
    if last:
        last = float(last)
        lp, sp, ep = tc.fmt_price(last), tc.fmt_price(stop), tc.fmt_price(entry)
        # With the market closed the last price is the previous regular close, not where the stock
        # trades now (pre-market gaps), so the checks below are conditional on the open.
        closed = p.get("market_open") is False
        last_txt = f"the last close {lp}" if closed else f"the last price {lp}"
        if (last <= stop) if side == "long" else (last >= stop):
            # Price is through the stop: fine for an order still waiting above (long) / below (short)
            # the market, never for a market order now.
            act, how = ("buy", "a buy-stop filled on the way up") if side == "long" else \
                ("short", "a sell-stop filled on the way down")
            if closed:
                warnings.append(f"{last_txt} is already {below} the stop {sp}: if it is still there at the "
                                f"open, this plan only works as a pending {act} at {ep} ({how}) - check "
                                "the pre-market price")
            else:
                warnings.append(f"{last_txt} is already {below} the stop {sp}: this plan only works "
                                f"as a pending {act} at {ep} ({how}) - do not {act} at the market")
        elif (last >= target) if side == "long" else (last <= target):
            beyond = f"{last_txt} is already {'above' if side == 'long' else 'below'} the target " \
                     f"{tc.fmt_price(target)}"
            warnings.append(f"{beyond}: if it opens there, the move has happened - re-plan from the opening "
                            "price" if closed else
                            f"{beyond}: the move has happened - re-plan from the current price")
        elif atr_value and abs(entry - last) / atr_value > FAR_ENTRY_ATR:
            notes.append(f"the entry is {abs(entry - last) / atr_value:.1f} ATR "
                         f"{'above' if entry > last else 'below'} {last_txt} - "
                         f"these levels assume you get filled at {tc.fmt_price(entry)}")

    fees = None
    fee = float(p.get("fee_pct") or 0.0)
    if fee > 0:   # crypto: the paper broker charges fee% of notional on the entry and the exit fill
        rate = fee / 100.0
        at_stop, at_target = rate * (entry + stop) / risk, rate * (entry + target) / risk
        win, loss = rr - at_target, 1.0 + at_stop
        be_fees = 100.0 * loss / (win + loss) if win > 0 else None
        fees = {"fee_pct": fee, "round_trip_r": round(2 * rate * entry / risk, 3),
                "at_stop_r": round(at_stop, 3), "at_target_r": round(at_target, 3),
                "breakeven_win_rate_after_fees_pct": None if be_fees is None else round(be_fees, 1)}
        if fees["round_trip_r"] >= FEES_WARN_R or be_fees is None:   # the card's Fees row shows the rest
            warnings.append(f"paper fees ({fee:g}% per fill) cost about {fees['round_trip_r']:.2f}R per round "
                            f"trip at these levels: the stop loses about {loss:.2f}R and "
                            + (f"the target nets only {win:.2f}R (break-even win rate after fees {be_fees:.1f}%)"
                               if be_fees is not None else
                               f"even the target {'loses' if win < 0 else 'makes'} {abs(win):.2f}R after fees - "
                               f"this stop is too close to the entry to pay for {fee:g}% per fill"))

    be_at = tk(entry + sign * risk)
    if (be_at < target) if side == "long" else (be_at > target):
        management = {"breakeven_at": be_at,
                      "rule": f"once {tc.fmt_price(be_at)} trades (+1R), move the stop to "
                              f"{tc.fmt_price(entry)} (breakeven); never widen the stop"}
    else:
        management = {"breakeven_at": None,
                      "rule": f"the target is only +{fmt_rr(rr)}R, so no breakeven step: leave the stop "
                              "until the target, the stop or the time stop; never widen the stop"}

    return {
        "side": side,
        "entry": entry,
        "stop": stop,
        "target": target,
        "method": used,
        "method_requested": method,
        "rr": round(rr, 2),
        "rr_requested": rr_requested,
        "risk_per_share": _dist(risk),
        "reward_per_share": _dist(reward),
        "stop_pct": round(risk / entry * 100, 2),
        "target_pct": round(reward / entry * 100, 2),
        "breakeven_win_rate_pct": round(100.0 / (1.0 + rr), 1),
        "atr": None if atr_value is None else _dist(atr_value),
        "atr_info": atr_info,
        "stop_atr": None if stop_atr is None else round(stop_atr, 2),
        "target_atr": None if target_atr is None else round(target_atr, 2),
        "avg_session_range": None if day_range is None else _dist(day_range),
        "params": {"atr_mult": atr_mult if used == "atr" else None,
                   "pct": pct if method == "percent" else None,
                   "lookback": lookback if method == "structure" else None},
        "structure": structure,
        "time_stop": time_stop,
        "management": management,
        "warnings": warnings,
        "notes": notes,
        **({"fees": fees} if fees else {}),   # crypto only, so stock plans print exactly as before
    }


def session_end_minute(meta: Dict[str, Any], day: str) -> Optional[int]:
    """Regular-session close (minutes ET) that Yahoo reports for `day`, e.g. 780 on a half day."""
    periods: List[Dict[str, Any]] = []
    cur = (meta or {}).get("currentTradingPeriod")
    if isinstance(cur, dict) and isinstance(cur.get("regular"), dict):
        periods.append(cur["regular"])
    tp = (meta or {}).get("tradingPeriods")
    if isinstance(tp, dict):          # includePrePost=true shape: {"pre": .., "regular": .., "post": ..}
        tp = tp.get("regular")
    if isinstance(tp, list):          # [[{start, end, ...}], ...] - one inner list per day
        for row in tp:
            rows = row if isinstance(row, list) else [row]
            periods.extend(x for x in rows if isinstance(x, dict))
    for per in periods:
        try:
            start, end = int(per["start"]), int(per["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if tc.et_date(start) == day and tc.et_date(end) == day and end > start:
            return tc.minute_of_day(end)
    return None


def time_context(now: datetime, time_stop_min: int, session_end: Optional[int] = None,
                 last_bar: Optional[tc.Bar] = None) -> Dict[str, Any]:
    """Early-close adjustment, minutes left and stale-data checks for the time stop. Pure."""
    notes: List[str] = []
    warnings: List[str] = []
    ts_min = time_stop_min
    today = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute
    # Early close today (Yahoo's trading period); after that close the plan is for the next session.
    if session_end is not None and session_end < tc.MARKET_CLOSE and now_min < session_end \
            and ts_min > session_end - 5:
        new = max(tc.MARKET_OPEN + 5, session_end - 5)
        notes.append(f"early close at {tc.hhmm(session_end)} ET today: time stop moved from "
                     f"{tc.hhmm(ts_min)} to {tc.hhmm(new)}")
        ts_min = new
    market_open = tc.is_market_open(now) and (session_end is None or now_min < session_end)
    minutes_left = None
    if market_open:
        minutes_left = ts_min - now_min
        tz, every_day = _tz(), tc.market().every_day
        if minutes_left <= 0:
            when = (f"until the next UTC day ({tc.hhmm(tc.MARKET_OPEN)} {tz})" if every_day else "today")
            warnings.append(f"it is {tc.hhmm(now_min)} {tz}, past the {tc.hhmm(ts_min)} time stop - "
                            f"no new day trades {when}")
        elif minutes_left < 30:
            warnings.append(f"only {minutes_left} minutes left before the {tc.hhmm(ts_min)} time stop")
        if last_bar is not None and now_min >= tc.MARKET_OPEN + 10:
            if last_bar.date != today:
                why = ("delayed data or no trades - is the product still trading?" if every_day
                       else "delayed data, a market holiday, or the stock is halted")
                warnings.append(f"no 5-minute bars from today yet (latest {_stamp(last_bar)} {tz}) - {why}")
            elif now.timestamp() - (last_bar.ts + BAR_SECONDS) > STALE_DATA_SECONDS:
                age = int((now.timestamp() - last_bar.ts) // 60)
                why = "no trades or delayed data?" if every_day else "halted or delayed data?"
                warnings.append(f"the latest 5-minute bar started at {last_bar.time} {tz} ({age} min ago) - "
                                f"{why} Check before trading")
    elif now.weekday() < 5 and now_min < tc.MARKET_OPEN:
        notes.append("pre-market: these levels are for today's session; re-run after the open for "
                     "today's own ATR")
    else:
        notes.append("market closed: these levels are for the next session; re-run after the open "
                     "for a fresh ATR")
    return {"time_stop_min": ts_min, "market_open": market_open, "minutes_left": minutes_left,
            "notes": notes, "warnings": warnings}


def resolve_method(method: Optional[str], given: Sequence[str]) -> str:
    """Pick the method from --method or from the method-specific flags given; reject mismatches.

    given: names of the method-specific options the user passed (stop, pct, lookback, atr_mult).
    """
    flag = {k: "--" + k.replace("_", "-") for k in FLAG_METHODS}
    if method is None:
        implied = sorted({FLAG_METHODS[k][0] for k in given if k != "atr_mult"})
        if len(implied) > 1:
            names = ", ".join(flag[k] for k in given if k != "atr_mult")
            raise ValueError(f"{names} belong to different stop methods - pick one (or set --method)")
        method = implied[0] if implied else "atr"
    for k in given:
        if method not in FLAG_METHODS[k]:
            raise ValueError(f"{flag[k]} does not apply to --method {method} "
                             f"(it is for --method {' or '.join(FLAG_METHODS[k])})")
    return method


def exit_plan_section(res: Dict[str, Any]) -> Dict[str, Any]:
    """The `exit_plan` block stored in plans/<SYM>.json."""
    return {
        "method": res["method"],
        "method_requested": res["method_requested"],
        "atr": res["atr"],
        "rr": res["rr"],
        "time_stop": res["time_stop"],
        "notes": list(res["warnings"]) + list(res["notes"]),
        "risk_per_share": res["risk_per_share"],
        "reward_per_share": res["reward_per_share"],
        "breakeven_win_rate_pct": res["breakeven_win_rate_pct"],
        "stop_atr": res["stop_atr"],
        "stop_pct": res["stop_pct"],
        "breakeven_at": res["management"]["breakeven_at"],
        "management": res["management"]["rule"],
        "params": {k: v for k, v in res["params"].items() if v is not None},
        "as_of": res.get("as_of"),
    }


def _same_level(a: Any, b: Any) -> bool:
    """Equal at tick precision (an unrounded 364.1834 from another skill equals 364.18)."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a > 0 and b > 0:
        return tick(a) == tick(b)
    return a == b


def finished_trigger_warning(plan: Dict[str, Any], sym: str, entry: float, today: str) -> Optional[str]:
    """The entry skill's note when the plan's entry is a trigger from a session that is already over.

    entry --save after the close (or of a past --date) stores entry_signal.session_over and a note;
    a trigger saved live whose session date is now in the past has played out just the same.
    """
    sig = plan.get("entry_signal")
    if not isinstance(sig, dict) or not isinstance(sig.get("entry"), (int, float)) \
            or not _same_level(sig["entry"], entry):
        return None
    day = str(sig.get("date") or "")
    if not (sig.get("session_over") or sig.get("replay") or (day and day < today)):
        return None
    note = sig.get("note") or (
        f"trigger is from the finished {day} {_tz()} session - re-plan from the current price"
        if tc.market().every_day else f"trigger is from the finished {day} session - re-plan from the next open")
    when = f" at {sig['triggered_at']} {_tz()}" if sig.get("triggered_at") else ""
    what = f"{sig.get('label') or sig.get('setup') or 'entry'}{when}"
    return f"the saved {sym} entry {tc.fmt_price(entry)} is an entry-skill trigger ({what}): {note}"


def stale_sections(existing: Optional[Dict[str, Any]], res: Dict[str, Any]) -> List[str]:
    """Sections of the saved plan that were computed for other levels than `res`.

    A null/empty section (the entry skill resets them to null when its levels change) holds
    nothing to clear, so it is not reported.
    """
    if not existing:
        return []
    return [name for name, keys in DEPENDENT_SECTIONS.items()
            if existing.get(name) and not all(_same_level(existing.get(k), res[k]) for k in keys)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _number(value: str) -> float:
    try:
        v = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value!r}")
    return v


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None


def build_parser() -> argparse.ArgumentParser:
    p = tc.make_parser("Set the stop, target and time stop before you enter a day trade (THE PLAN). "
                       "Paper planning only - not financial advice.")
    p.add_argument("symbol", help="ticker, e.g. TSLA, or a Coinbase product such as BTC-USD")
    p.add_argument("--entry", type=_number, help="planned entry price (default: the saved plan's "
                   "entry, else the current price)")
    p.add_argument("--side", type=str.lower, choices=("long", "short"),
                   help="default: the saved plan's side, else long (with --stop: inferred from it)")
    p.add_argument("--method", type=str.lower, choices=METHODS,
                   help="stop method (default atr; --stop implies manual, --pct percent, "
                        "--lookback structure)")
    p.add_argument("--stop", type=_number, help="manual stop price (implies --method manual)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--rr", type=_number, help="target = entry +/- RR x risk (default 2.0)")
    g.add_argument("--target", type=_number, help="fixed target price instead of --rr")
    p.add_argument("--atr-mult", type=_number, help="ATR multiple for the atr stop (default 1.5)")
    p.add_argument("--pct", type=_number, help="percent stop distance (default 1.0)")
    p.add_argument("--lookback", type=_int, help="bars for the structure stop's swing low/high "
                   "(default 12 = one hour)")
    p.add_argument("--time-stop", metavar="HH:MM", help="be flat by this time (default 15:55 ET; "
                   "23:55 UTC for crypto)")
    p.add_argument("--save", action="store_true", help="merge the plan into plans/<SYMBOL>.json")

    def error(message: str) -> None:  # bad flags are bad input: exit 1, not 2 (= data error)
        p.print_usage(sys.stderr)
        p.exit(1, f"{p.prog}: error: {message}\n")
    p.error = error  # type: ignore[assignment]
    return p


def parse_symbol(raw: str) -> str:
    sym = (raw or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.\-=^]{0,14}", sym):
        raise ValueError(f"not a ticker symbol: {raw!r}")
    return sym


def _not_found(err: Exception) -> bool:
    msg = str(err)
    return any(s in msg for s in ("HTTP 404", "Yahoo: Not Found", "No data found", "no chart data"))


def price_increment(sym: str) -> Tuple[Optional[float], Optional[str]]:
    """Crypto: (Coinbase quote_increment, None), or (None, note) when the product list is
    unreachable - tick() then keeps cents (4 significant digits below $1). Stocks: (None, None)."""
    if not _crypto():
        return None, None
    try:
        return float(tc.product_info(sym)["quote_increment"]), None
    except (tc.FetchError, KeyError, TypeError, ValueError) as e:
        return None, (f"{sym} price increment unavailable ({e}); prices are rounded to cents "
                      "(4 significant digits below $1)")


def _plan_number(plan: Dict[str, Any], key: str) -> Optional[float]:
    try:
        v = float(plan.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def current_price(sym: str, bars: Sequence[tc.Bar], now_ts: float) -> Tuple[float, str, Optional[str]]:
    """(price, source, note) - the live quote, or the last 5-minute close when replaying the past."""
    try:
        q = tc.quote(sym)
    except tc.FetchError as e:
        if not bars:
            raise
        return bars[-1].close, "last 5-minute close", f"quote unavailable ({e}); using the last 5-minute close"
    t = q.get("time")
    if bars and isinstance(t, (int, float)) and t > now_ts + 60:   # quote is newer than "now": a replay
        return bars[-1].close, "last 5-minute close", None
    return float(q["price"]), "current price", None


def plan_path(sym: str) -> str:
    return f"plans/{tc._safe_symbol(sym)}.json"


def save(res: Dict[str, Any], existing: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """Merge into plans/<SYM>.json; drop sections that were computed for the old levels."""
    dropped = stale_sections(existing, res)
    merged = tc.save_plan({"symbol": res["symbol"], "side": res["side"], "entry": res["entry"],
                           "stop": res["stop"], "target": res["target"],
                           "exit_plan": exit_plan_section(res)})
    if dropped:
        for k in dropped:
            merged.pop(k, None)
        tc.save_json(plan_path(res["symbol"]), merged)
    return merged, dropped


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    sym = parse_symbol(args.symbol)
    tc.use_market_for(sym)               # crypto (BTC-USD): UTC days, 24/7, Coinbase prices
    given = [k for k in ("stop", "pct", "lookback", "atr_mult") if getattr(args, k) is not None]
    method = resolve_method(args.method, given)
    time_stop_min = parse_time_stop(args.time_stop or tc.market().time_stop)
    now = tc.now_et()
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    settings = tc.load_settings()
    plan = tc.load_plan(sym) or {}
    if not isinstance(plan, dict):
        raise tc.StateError(f"{tc.home() / plan_path(sym)} is not a trade plan object; fix or delete it")
    plan_entry, plan_stop = _plan_number(plan, "entry"), _plan_number(plan, "stop")
    plan_side = plan.get("side") if plan.get("side") in ("long", "short") else None
    warnings: List[str] = []
    notes: List[str] = []

    stop = args.stop
    stop_source = "--stop" if stop is not None else None
    if method == "manual" and stop is None:
        if plan_stop is None:
            raise ValueError(f"--method manual needs --stop PRICE (no saved {sym} plan stop to reuse)")
        stop, stop_source = plan_stop, "saved plan"

    # 5-minute bars: required for atr/structure, optional context for percent/manual.
    source = "Coinbase" if _crypto() else "Yahoo"
    increment: Optional[float] = None
    try:
        raw, meta = tc.fetch_bars(sym, "5m", "5d")
    except tc.FetchError as e:
        if _not_found(e):
            if _crypto():
                raise ValueError(f"Coinbase has no product {sym} - check the symbol (e.g. BTC-USD)") from e
            raise ValueError(f"Yahoo Finance has no data for {sym} - check the symbol") from e
        if method in ("atr", "structure") or (args.entry is None and plan_entry is None):
            raise
        raw, meta = [], {}
        warnings.append(f"5-minute data unavailable, so the ATR sanity checks were skipped ({e})")
    else:
        increment, inc_note = price_increment(sym)   # crypto only; the data host answered, so try it
        if inc_note:
            notes.append(inc_note)
    bars = usable_bars(raw, now_ts)
    if method in ("atr", "structure") and not bars:
        raise ValueError(f"{source} returned no regular-session 5-minute bars for {sym} in the last 5 days "
                         "(halted or not trading?) - use --pct or --stop with --entry instead")

    last_price = bars[-1].close if bars else None
    if args.entry is not None:
        entry, entry_source = args.entry, "--entry"
    elif plan_entry is not None:
        entry, entry_source = plan_entry, "saved plan"
    else:
        entry, entry_source, note = current_price(sym, bars, now_ts)
        last_price = entry
        if note:
            notes.append(note)
    made = str(plan.get("updated_at") or plan.get("created_at") or "")[:10]
    plan_is_old = bool(made) and made < today
    if plan_is_old and "saved plan" in (entry_source, stop_source):
        warnings.append(f"the saved {sym} plan is from {made} - its levels may be stale; pass --entry "
                        "(and --stop) to plan from today's price")
    if entry_source == "saved plan":
        played_out = finished_trigger_warning(plan, sym, entry, today)
        if played_out:
            warnings.append(played_out)

    if args.side:
        side, side_source = args.side, "--side"
    elif plan_side:
        side, side_source = plan_side, "saved plan"
    elif method == "manual" and stop is not None and stop != entry:
        side, side_source = ("long" if stop < entry else "short"), "inferred from the stop"
    else:
        side, side_source = "long", "default"
    if side_source == "inferred from the stop" and side == "short":
        notes.append("side short inferred from the stop being above the entry (pass --side to be explicit)")
    elif side_source == "saved plan" and entry_source != "saved plan":
        if plan_is_old and stop_source != "saved plan":
            # Only the direction comes from an old plan: easy to miss, and it flips the whole plan.
            warnings.append(f"side {side} comes from the saved {sym} plan from {made} - pass --side long "
                            "or --side short if today's trade is different")
        else:
            notes.append(f"side {side} taken from the saved {sym} plan (pass --side to change it)")

    tctx = time_context(now, time_stop_min, session_end_minute(meta, today), bars[-1] if bars else None)
    res = make_plan(side, entry, method, bars, {
        "stop": stop, "target": args.target, "rr": args.rr, "atr_mult": args.atr_mult,
        "pct": args.pct, "lookback": args.lookback, "time_stop": tc.hhmm(tctx["time_stop_min"]),
        "today": today, "now_ts": now_ts, "min_rr": settings.get("min_reward_risk"),
        "last_price": last_price, "market_open": tctx["market_open"], "tick": increment,
        "fee_pct": tc.fee_pct(settings=settings)})
    res = dict({"symbol": sym}, **res)
    res["warnings"] = res["warnings"] + warnings + tctx["warnings"]
    res["notes"] = res["notes"] + notes + tctx["notes"]
    res.update({
        "as_of": now.isoformat(timespec="seconds"),
        "entry_source": entry_source, "stop_source": stop_source, "side_source": side_source,
        "last_price": None if last_price is None else tick(last_price, increment=increment),
        "market_open": tctx["market_open"], "minutes_to_time_stop": tctx["minutes_left"],
        "saved": False, "plan_file": None, "dropped_sections": [],
        "disclaimer": tc.DISCLAIMER,
    })
    if args.save:
        _, dropped = save(res, plan or None)
        res.update(saved=True, plan_file=str(tc.home() / plan_path(sym)), dropped_sections=dropped)
    tc.emit(res, args.json, render)
    return 0


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------

def method_label(res: Dict[str, Any]) -> str:
    m, prm = res["method"], res["params"]
    if res["method_requested"] == "structure" and m == "structure":
        s = res["structure"]
        ext = "low" if res["side"] == "long" else "high"
        at = s["at"][-5:] if s["at"][:10] == str(res.get("as_of", ""))[:10] else s["at"]
        return (f"structure stop ({s['lookback']}-bar {ext} {tc.fmt_price(s['level'])} at "
                f"{at} {'-' if res['side'] == 'long' else '+'} 0.1 ATR)")
    if m == "atr":
        tail = "; structure fell back" if res["method_requested"] == "structure" else ""
        return f"ATR stop ({prm['atr_mult']:g} x ATR{tail})"
    if m == "percent":
        return f"percent stop ({prm['pct']:g}%)"
    return "manual stop"


def _delta(x: float, entry: float) -> str:
    d = x - entry
    sign = "+" if d > 0 else "-"
    places = 2 if entry >= 1 else 4
    size = tc.fmt_price(abs(d)) if _crypto() and entry < 1 else f"{abs(d):,.{places}f}"   # sub-cent coins
    return f"{sign}{size} ({sign}{abs(d) / entry * 100:.2f}%)"


WIDTH = 100


def _wrap(text: str, first: str, rest: str) -> List[str]:
    if "://" in text:  # keep URLs (in data errors) on one line so they stay copyable
        return [first + text]
    # a no-break space keeps " - " from starting a line, where it would read as a bullet
    out = textwrap.wrap(text.replace(" - ", "\u00a0- "), WIDTH, initial_indent=first,
                        subsequent_indent=rest, break_on_hyphens=False, break_long_words=False)
    return [line.replace("\u00a0", " ") for line in out] or [first.rstrip()]


def render(res: Dict[str, Any]) -> str:
    fp = tc.fmt_price
    e, s, t = res["entry"], res["stop"], res["target"]
    atr_txt = lambda x: "" if x is None else f" · {x:.2f} ATR"  # noqa: E731
    src = {"--entry": "from --entry", "saved plan": "from the saved plan",
           "current price": "current price", "last 5-minute close": "last 5-minute close"}
    left = res.get("minutes_to_time_stop")
    left_txt = ""
    if left and left > 0:
        left_txt = f" ({left // 60}h {left % 60:02d}m left)" if left >= 60 else f" ({left}m left)"
    rows = [
        ("Entry", fp(e), src.get(res["entry_source"], res["entry_source"])),
        ("Stop", fp(s), f"{_delta(s, e)}{atr_txt(res['stop_atr'])}"
                        + (" · from the saved plan" if res.get("stop_source") == "saved plan" else "")),
        ("Target", fp(t), f"{_delta(t, e)}{atr_txt(res['target_atr'])}"),
        ("R:R", f"{res['rr']:.2f}", f"break-even win rate {res['breakeven_win_rate_pct']:.1f}% "
                                     "(before costs)"),
        ("Time stop", res["time_stop"], f"{_tz()} - flat by then, win or lose{left_txt}"),
    ]
    fees = res.get("fees")
    if fees:   # crypto: what the paper broker's fees do to this plan, in R
        rows.append(("Fees", f"{fees['round_trip_r']:.2f}R",
                     f"round trip at {fees['fee_pct']:g}% per fill (paper broker)"
                     + ("" if fees.get("breakeven_win_rate_after_fees_pct") is None else
                        f" · break-even win rate after fees {fees['breakeven_win_rate_after_fees_pct']:.1f}%")))
    if res["atr"] is not None:
        info = res["atr_info"]
        where = (f"today's {info['bars']} bars" if info["source"] == "today"
                 else f"last {info['bars']} bars from {info['first']}")
        last = f" · last {fp(res['last_price'])}" if res.get("last_price") else ""
        rows.append((f"ATR({info['n']})", fp(res["atr"]), f"5-minute, {where}{last}"))
    w1 = max(len(r[0]) for r in rows + [("Manage", "", "")])
    w2 = max(len(r[1]) for r in rows)
    lines = [f"THE PLAN — {res['symbol']} {res['side'].upper()} · {method_label(res)}",
             f"ENTRY {fp(e)} / STOP {fp(s)} / TARGET {fp(t)} / R:R {fmt_rr(res['rr'])}", ""]
    for a, b, c in rows:
        lines.extend(_wrap(c, f"  {a.ljust(w1)}  {b.rjust(w2)}  ", " " * (w1 + w2 + 6)))
    lines.extend(_wrap(res["management"]["rule"], f"  {'Manage'.ljust(w1)}  ", " " * (w1 + 4)))
    if res["warnings"] or res["notes"]:
        lines.append("")
        for w in res["warnings"]:
            lines.extend(_wrap(w, "  ! ", "    "))
        for n in res["notes"]:
            lines.extend(_wrap(n, "  - ", "    "))
    lines.append("")
    if res["saved"]:
        lines.append(f"Saved to {res['plan_file']}")
        if res["dropped_sections"]:
            lines.append(f"Cleared {', '.join(res['dropped_sections'])} from the plan: computed for the old "
                         "levels - re-run them.")
    else:
        lines.append("Not saved - add --save to store it in the trade plan.")
    lines.append("Next: time-machine for the honest odds of these levels, then risk to size the position.")
    lines.append(res["disclaimer"])
    return "\n".join(lines)


if __name__ == "__main__":
    tc.run(main)
