#!/usr/bin/env python3
"""Time Machine: replay the rest of the session thousands of times before you risk a dollar.

Takes a trade plan (entry, stop, target) and asks: "if I get filled at the entry now, how often
does the price reach the target before the stop?" It answers two ways:

  1. Monte Carlo: 10,000 replays of the rest of the session, stitched together from the stock's
     own past 5-minute bars at the same clock times (a slot-aligned block bootstrap), and
  2. History check: every past session, entered at the same clock time with the same % stop and
     target, walked through the real bars to the close.

Usage:
  python3 scripts/time_machine.py TSLA --entry 364.18 --stop 358.32 --target 370.04
  python3 scripts/time_machine.py TSLA                         # levels from the saved plan
  python3 scripts/time_machine.py TSLA --at 10:15 --paths 20000 --seed 7
  python3 scripts/time_machine.py TSLA --side short --entry 364.18 --stop 368.00 --target 356.50
  python3 scripts/time_machine.py TSLA --svg tsla_replays.svg --save --json

Exit codes: 0 ok, 1 bad input (or not enough history), 2 data source unreachable.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trader_common as tc  # noqa: E402

import argparse  # noqa: E402
import hashlib  # noqa: E402
import math  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import textwrap  # noqa: E402
from datetime import datetime  # noqa: E402
from collections.abc import Mapping as MappingABC  # noqa: E402
from html import escape  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union  # noqa: E402

BAR_MINUTES = 5
BAR_SECONDS = BAR_MINUTES * 60
SLOTS = (tc.MARKET_CLOSE - tc.MARKET_OPEN) // BAR_MINUTES   # 78 five-minute slots, 09:30..15:55
LAST_SLOT = SLOTS - 1
DEFAULT_PATHS = 10_000
DEFAULT_SESSIONS = 59          # Yahoo keeps about 60 days of 5-minute bars
DEFAULT_BLOCK = 3              # consecutive bars copied from one past session (15 minutes)
SVG_PATHS = 300                # replays drawn in the SVG (the first 300 of the run)
MIN_SESSIONS = 5               # refuse to give odds from fewer past sessions than this
FEW_SESSIONS = 20              # warn below this many
MIN_SESSION_BARS = 12          # a past session needs >= 1 hour of bars to count (drops glitches)
STALE_DATA_SECONDS = 600       # market open but no new bar for 10 minutes -> halted / delayed?
STALE_LIBRARY_DAYS = 7         # newest past session older than this -> stale data warning
AMBIGUOUS_NOTE_PCT = 5.0       # note when this share of replays touched both levels in one bar
EPS = 1e-12                    # float tolerance so a bar that exactly touches a level counts
TREND_MATERIAL_R = 0.1         # trend-removed replays this far off in expected R -> say so
TREND_MATERIAL_PCT = 5.0       # ... or this many points off in target-first %
TREND_EXPLAINS_R = 0.05        # drift to the median exit at least this many R can explain that gap

Shape = Tuple[float, float, float]           # (close, high, low) relative to the previous close
Library = List[List[Optional[Shape]]]        # one row of SLOTS shapes per past session


# ---------------------------------------------------------------------------
# Pure core: slots and the bar-shape library
# ---------------------------------------------------------------------------

def slot_of(ts: float) -> Optional[int]:
    """5-minute slot of the regular session (0 = 09:30 bar, 77 = 15:55 bar), None outside it."""
    m = tc.minute_of_day(ts)
    if m < tc.MARKET_OPEN or m >= tc.MARKET_CLOSE:
        return None
    return (m - tc.MARKET_OPEN) // BAR_MINUTES


def slot_time(slot: int) -> str:
    """Start time (HH:MM ET) of a slot; slot SLOTS is the 16:00 close."""
    return tc.hhmm(tc.MARKET_OPEN + slot * BAR_MINUTES)


def slot_bars(bars: Sequence[tc.Bar]) -> Dict[int, tc.Bar]:
    """{slot: bar} for one session.

    Yahoo can send the live bar twice: once at the slot start and once as a separate row stamped
    with the last trade time (e.g. 10:17:43) that may carry only the latest price. Rows in one
    slot are merged - first open, highest high, lowest low, last close - so the slot keeps its
    real range whichever copy is fuller.
    """
    out: Dict[int, tc.Bar] = {}
    for b in sorted(bars, key=lambda x: x.ts):
        k = slot_of(b.ts)
        if k is None:
            continue
        a = out.get(k)
        out[k] = b if a is None else tc.Bar(a.ts, a.open, max(a.high, b.high), min(a.low, b.low), b.close,
                                            max(a.volume, b.volume))
    return out


def build_library(sessions: Sequence[Sequence[tc.Bar]], n_slots: int = SLOTS) -> Library:
    """Per-session, per-slot bar shapes: close/high/low divided by the previous bar's close.

    The first bar of a session is measured from its own open, so overnight gaps are left out
    (you enter intraday). A slot with no bar *between* the session's first and last bar means
    no trades in those 5 minutes and becomes a flat bar (1, 1, 1); the next bar's shape then
    carries the whole move since the last trade. Slots before the first bar or after the last
    (late open, early close on half days) stay None - they did not exist that day.
    """
    lib: Library = []
    for bars in sessions:
        row: List[Optional[Shape]] = [None] * n_slots
        by = {k: b for k, b in slot_bars(bars).items() if k < n_slots}
        if by:
            ks = sorted(by)
            prev: Optional[float] = None
            for k in range(ks[0], ks[-1] + 1):
                b = by.get(k)
                if b is None:
                    row[k] = (1.0, 1.0, 1.0)
                    continue
                pc = b.open if prev is None else prev
                row[k] = (b.close / pc, max(b.high, b.open, b.close) / pc, min(b.low, b.open, b.close) / pc)
                prev = b.close
        lib.append(row)
    return lib


def invert_library(library: Library) -> Library:
    """Mirror shapes into 1/price space so a short is simulated with the long-side loop.

    A short's stop (price >= stop) is 1/price <= entry/stop; its target (price <= target) is
    1/price >= entry/target. Highs and lows swap under 1/x.
    """
    return [[None if s is None else (1.0 / s[0], 1.0 / s[2], 1.0 / s[1]) for s in row] for row in library]


def detrend_library(library: Library) -> Library:
    """Remove the recent trend: scale each slot's shapes so their average close/prev-close is 1.

    The bar shapes (ranges, wicks, time-of-day volatility) are kept; only the average drift of
    the past sessions at each clock slot is taken out, so the replays no longer assume the
    last ~60 days' up- or down-trend continues.
    """
    out = [list(row) for row in library]
    for k in range(len(library[0]) if library else 0):
        closes = [row[k][0] for row in library if row[k] is not None]
        if not closes:
            continue
        f = len(closes) / sum(closes)
        for row in out:
            s = row[k]
            if s is not None:
                row[k] = (s[0] * f, s[1] * f, s[2] * f)
    return out


def library_trend(library: Library, start_slot: int, end_slot: int) -> Dict[str, Any]:
    """Average % move of the past sessions over the replay window (start bar to end bar), and its
    t-statistic across sessions (|t| < 2: indistinguishable from noise)."""
    moves: List[float] = []
    for row in library:
        if start_slot >= len(row) or row[start_slot] is None:
            continue
        g = 1.0
        for s in row[start_slot:end_slot + 1]:
            if s is not None:
                g *= s[0]
        moves.append(g - 1.0)
    n = len(moves)
    mean = sum(moves) / n if n else 0.0
    sd = statistics.stdev(moves) if n >= 2 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return {"sessions": n, "avg_move_pct": round(mean * 100.0, 3), "t_stat": round(t, 2)}


def slot_drift(library: Library, start_slot: int, end_slot: int) -> float:
    """Cumulative average drift from the start of `start_slot` to the end of `end_slot`, as a
    fraction: the product of each slot's mean close/prev-close across the past sessions, minus 1.

    This is exactly what detrend_library takes out of the replays over those bars.
    """
    g = 1.0
    for k in range(start_slot, end_slot + 1):
        closes = [row[k][0] for row in library if k < len(row) and row[k] is not None]
        if closes:
            g *= sum(closes) / len(closes)
    return g - 1.0


def trend_check(library: Library, start_slot: int, end_slot: int, side: str, entry: float, stop: float,
                median_minutes: Optional[float]) -> Dict[str, Any]:
    """How much drift the replays inherit, measured over the horizon that decides the trade.

    Most replays end at the stop or the target, so the drift that tilts the odds is the one between
    the entry and the typical exit: the horizon runs from the start slot to the median time to
    resolution of the trend-included replays (the whole window if no replay resolved). drift_pct
    is the cumulative per-slot mean drift over it (what the trend-removed replays take out),
    drift_r the same move in R for this trade (positive = in its favour), horizon_t_stat the
    t-statistic of the past sessions' own moves over it. avg_move_pct / t_stat cover the whole
    window, start to close.
    """
    whole = library_trend(library, start_slot, end_slot)
    if median_minutes:
        h_end = min(end_slot, start_slot + max(1, int(math.ceil(median_minutes / BAR_MINUTES))) - 1)
        basis = "median_resolution"
    else:
        h_end, basis = end_slot, "window"
    near = library_trend(library, start_slot, h_end)
    drift = slot_drift(library, start_slot, h_end)
    risk_frac = abs(entry - stop) / entry
    sign = 1.0 if side == "long" else -1.0
    out = dict(whole)
    out.update({
        "horizon": {"start": slot_time(start_slot), "end": slot_time(h_end + 1), "end_slot": h_end,
                    "minutes": (h_end - start_slot + 1) * BAR_MINUTES, "basis": basis},
        "drift_pct": round(drift * 100.0, 3),
        "drift_r": round(sign * drift / risk_frac, 3) if risk_frac > 0 else None,
        "horizon_avg_move_pct": near["avg_move_pct"], "horizon_t_stat": near["t_stat"],
    })
    return out


def split_pct(counts: Sequence[int], total: int, digits: int = 2) -> List[float]:
    """Percentages of `total` rounded to `digits` that add up to exactly 100 (largest remainder)."""
    if total <= 0:
        return [0.0 for _ in counts]
    scale = 100 * 10 ** digits
    floors = [c * scale // total for c in counts]
    rems = [c * scale % total for c in counts]
    for i in sorted(range(len(counts)), key=lambda i: rems[i], reverse=True)[:scale - sum(floors)]:
        floors[i] += 1
    return [f / 10 ** digits for f in floors]


def display_pct(counts: Mapping[str, int], total: int) -> Dict[str, float]:
    """The three odds with one decimal, exactly as the card prints them (largest remainder, so they
    add up to 100.0). Saved in the plan so risk, broker and analyst quote the same numbers."""
    pt, ps, po = split_pct([counts["target"], counts["stop"], counts["open"]], total, 1)
    return {"target_first": pt, "stop_first": ps, "open_at_close": po}


def _se_pct(count: int, n: int) -> float:
    """Standard error of a percentage from n independent replays, in percentage points."""
    if n <= 0:
        return 0.0
    p = count / n
    return round(100.0 * math.sqrt(p * (1.0 - p) / n), 2)


def _median(xs: Sequence[float]) -> Optional[float]:
    return float(statistics.median(xs)) if xs else None


def _check_levels(side: str, entry: float, stop: float, target: Optional[float]) -> None:
    if target is None:
        raise ValueError("a target price is needed to measure target-first vs stop-first")
    errs = tc.validate_levels(side, entry, stop, target)
    if errs:
        raise ValueError("; ".join(errs))


# ---------------------------------------------------------------------------
# Pure core: Monte Carlo
# ---------------------------------------------------------------------------

def simulate(library: Library, start_slot: int, entry: float, stop: float, target: float, side: str,
             n_paths: int, rng: random.Random, block: int = DEFAULT_BLOCK,
             end_slot: Optional[int] = None, keep_paths: int = 0) -> Dict[str, Any]:
    """Replay the rest of the session `n_paths` times with a slot-aligned block bootstrap.

    Each path starts at `entry` at the start of `start_slot` and walks slot by slot to
    `end_slot` (default: the last slot). Every `block` slots it copies the next bars' shapes
    from a randomly chosen past session at the same clock slots (a session with no bar at a
    slot - e.g. a half day's afternoon - is never chosen for it). Per bar: low <= stop -> STOP,
    high >= target -> TARGET (long; mirrored for a short); both in one bar counts as STOP and
    as ambiguous. Neither by the last bar -> OPEN AT CLOSE, valued in R at the last close.

    The run's statistics do not depend on `keep_paths`: the first `keep_paths` paths are also
    recorded (price after each bar, continued past their exit with a separate generator) for
    drawing only.
    """
    _check_levels(side, entry, stop, target)
    if n_paths < 1:
        raise ValueError("need at least 1 path")
    if block < 1:
        raise ValueError("block must be at least 1 bar")
    if not library:
        raise ValueError("no past sessions to replay")
    n_slots = len(library[0])
    last = n_slots - 1 if end_slot is None else min(end_slot, n_slots - 1)
    if not 0 <= start_slot <= last:
        raise ValueError(f"start slot {start_slot} is outside the session (0-{last})")
    shapes = library if side == "long" else invert_library(library)
    eligible = [[j for j, row in enumerate(shapes) if row[k] is not None] for k in range(n_slots)]
    if not eligible[start_slot]:
        raise ValueError(f"no past session has a 5-minute bar at {slot_time(start_slot)} ET to replay from")
    end = start_slot
    while end < last and eligible[end + 1]:
        end += 1

    # Thresholds in "price / entry" space (1/price space for a short), with float tolerance.
    if side == "long":
        up, down = target / entry, stop / entry
    else:
        up, down = entry / target, entry / stop
    up_t, down_t = up * (1.0 - EPS), down * (1.0 + EPS)
    risk_frac = abs(entry - stop) / entry
    rr = tc.reward_risk(side, entry, stop, target)
    long_side = side == "long"

    rand = rng.random
    state = rng.getstate()[1]
    aux = random.Random(sum(state[:16]) ^ 0x5EED)   # drawing-only continuations; never touches rng
    arand = aux.random
    keep = max(0, min(int(keep_paths), n_paths))
    n_target = n_stop = n_open = n_amb = 0
    open_r_sum = 0.0
    mins_target: List[int] = []
    mins_stop: List[int] = []
    samples: List[Dict[str, Any]] = []

    for p in range(n_paths):
        m = 1.0
        k = start_slot
        left = 0
        sess: List[Optional[Shape]] = []
        outcome = 0                      # 0 open, 1 target, 2 stop
        rc = 1.0
        rec: Optional[List[float]] = [] if p < keep else None
        while k <= end:
            if left <= 0:
                cand = eligible[k]
                sess = shapes[cand[int(rand() * len(cand))]]
                left = block
            sh = sess[k]
            if sh is None:               # that session ended early: switch to one that has this slot
                cand = eligible[k]
                sess = shapes[cand[int(rand() * len(cand))]]
                sh = sess[k]
            rc, rh, rl = sh
            if m * rl <= down_t:
                outcome = 2
                if m * rh >= up_t:
                    n_amb += 1
                break
            if m * rh >= up_t:
                outcome = 1
                break
            m *= rc
            if rec is not None:
                rec.append(m)
            k += 1
            left -= 1
        if outcome == 1:
            n_target += 1
            mins_target.append((k - start_slot + 1) * BAR_MINUTES)
        elif outcome == 2:
            n_stop += 1
            mins_stop.append((k - start_slot + 1) * BAR_MINUTES)
        else:
            n_open += 1
            price_ratio = m if long_side else 1.0 / m
            open_r_sum += ((price_ratio - 1.0) if long_side else (1.0 - price_ratio)) / risk_frac
        if rec is not None:
            exit_step = None
            if outcome:                  # finish the picture after the exit (aux generator only)
                exit_step = len(rec) + 1
                m *= rc
                rec.append(m)
                k += 1
                left -= 1
                while k <= end:
                    if left <= 0:
                        cand = eligible[k]
                        sess = shapes[cand[int(arand() * len(cand))]]
                        left = block
                    sh = sess[k]
                    if sh is None:
                        cand = eligible[k]
                        sess = shapes[cand[int(arand() * len(cand))]]
                        sh = sess[k]
                    m *= sh[0]
                    rec.append(m)
                    k += 1
                    left -= 1
            ratios = rec if long_side else [1.0 / x for x in rec]
            samples.append({"outcome": ("open", "target", "stop")[outcome], "exit_step": exit_step,
                            "prices": [entry] + [entry * x for x in ratios]})

    pt, ps, po = split_pct([n_target, n_stop, n_open], n_paths)
    avg_open = open_r_sum / n_open if n_open else None
    exp_r = (n_target * rr - n_stop + open_r_sum) / n_paths
    resolved = n_target + n_stop
    res = {
        "side": side, "entry": entry, "stop": stop, "target": target, "rr": round(rr, 4),
        "start_slot": start_slot, "end_slot": end, "start": slot_time(start_slot),
        "end": slot_time(end + 1), "minutes_simulated": (end - start_slot + 1) * BAR_MINUTES,
        "end_capped": end < last,
        "paths": n_paths, "block": block,
        "sessions_used": sum(1 for row in library if any(s is not None for s in row)),
        "sessions_at_start": len(eligible[start_slot]),
        "counts": {"target": n_target, "stop": n_stop, "open": n_open, "ambiguous": n_amb},
        "target_first_pct": pt, "stop_first_pct": ps, "open_at_close_pct": po,
        "display_pct": display_pct({"target": n_target, "stop": n_stop, "open": n_open}, n_paths),
        "ambiguous_pct": split_pct([n_amb, n_paths - n_amb], n_paths)[0],
        "target_first_se": _se_pct(n_target, n_paths), "stop_first_se": _se_pct(n_stop, n_paths),
        "open_at_close_se": _se_pct(n_open, n_paths),
        "avg_r_open": None if avg_open is None else round(avg_open, 3),
        "expected_r": round(exp_r, 3),
        "breakeven_win_rate_pct": round(100.0 / (1.0 + rr), 2),
        "resolved_target_share_pct": round(100.0 * n_target / resolved, 2) if resolved else None,
        "median_minutes_to_resolution": _median(mins_target + mins_stop),
        "median_minutes_to_target": _median(mins_target),
        "median_minutes_to_stop": _median(mins_stop),
        "sample_paths": samples,
    }
    return res


# ---------------------------------------------------------------------------
# Pure core: history check, verdict, start time
# ---------------------------------------------------------------------------

def replay_history(sessions: Union[Mapping[str, Sequence[tc.Bar]], Sequence[Sequence[tc.Bar]]],
                   start_slot: int, stop_pct: float, target_pct: float, side: str,
                   end_slot: int = LAST_SLOT) -> Dict[str, Any]:
    """Walk every past session's real bars from the same clock time with the same % levels.

    Entry each day = the close of the last bar before `start_slot` (or, when the day's first bar
    is the start bar, e.g. at 09:30, that bar's open). stop_pct / target_pct are signed % distances
    from the entry (e.g. -1.61 / +1.61 for a long). Same rules as `simulate`: first touch wins,
    both in one bar = stop. Sessions with no bar at or after the start (a half day for an
    afternoon start), or that only began trading after it (late open), are skipped - like the
    replays, which never draw a day for a slot it did not trade.
    """
    if side not in ("long", "short"):
        raise ValueError(f"side must be long or short, not {side!r}")
    stop_r, target_r = 1.0 + stop_pct / 100.0, 1.0 + target_pct / 100.0
    _check_levels(side, 1.0, stop_r, target_r)
    risk_frac = abs(stop_pct) / 100.0
    rr = abs(target_pct) / abs(stop_pct)
    items = (list(sessions.items()) if isinstance(sessions, MappingABC)
             else [((s[0].date if s else ""), s) for s in sessions])
    counts = {"target": 0, "stop": 0, "open": 0, "ambiguous": 0, "skipped": 0}
    open_r: List[float] = []
    total_r = 0.0
    results: List[Dict[str, Any]] = []
    for day, bars in items:
        by = slot_bars(bars)
        before = [k for k in by if k < start_slot]
        walk = [by[k] for k in sorted(by) if start_slot <= k <= end_slot]
        if not walk or (not before and slot_of(walk[0].ts) != start_slot):
            counts["skipped"] += 1
            continue
        p0 = by[max(before)].close if before else walk[0].open
        stop_px, target_px = p0 * stop_r, p0 * target_r
        outcome, when, r = "open", None, None
        for b in walk:
            if side == "long":
                hit_stop, hit_target = b.low <= stop_px * (1 + EPS), b.high >= target_px * (1 - EPS)
            else:
                hit_stop, hit_target = b.high >= stop_px * (1 - EPS), b.low <= target_px * (1 + EPS)
            if hit_stop:
                outcome, r = "stop", -1.0
                counts["ambiguous"] += 1 if hit_target else 0
            elif hit_target:
                outcome, r = "target", rr
            if outcome != "open":
                when = tc.hhmm(tc.minute_of_day(b.ts) // BAR_MINUTES * BAR_MINUTES + BAR_MINUTES)
                break
        if outcome == "open":
            last_close = walk[-1].close
            r = ((last_close / p0 - 1.0) if side == "long" else (1.0 - last_close / p0)) / risk_frac
            open_r.append(r)
        counts[outcome] += 1
        total_r += r
        results.append({"date": day, "entry": round(p0, 4), "outcome": outcome, "at": when,
                        "r": round(r, 3)})
    n = counts["target"] + counts["stop"] + counts["open"]
    pt, ps, po = split_pct([counts["target"], counts["stop"], counts["open"]], n)
    return {
        "sessions": n, "target": counts["target"], "stop": counts["stop"], "open": counts["open"],
        "ambiguous": counts["ambiguous"], "skipped": counts["skipped"],
        "target_pct": pt, "stop_pct": ps, "open_pct": po,
        "avg_r_open": round(sum(open_r) / len(open_r), 3) if open_r else None,
        "expected_r": round(total_r / n, 3) if n else None,
        "results": results,
    }


def verdict(result: Mapping[str, Any]) -> str:
    """ODDS LEAN YOUR WAY / ODDS LEAN AGAINST YOU / CLOSE TO A COIN FLIP."""
    er = float(result["expected_r"])
    if er > 0.1 and result["target_first_pct"] > result["stop_first_pct"]:
        return "ODDS LEAN YOUR WAY"
    if er < -0.1:
        return "ODDS LEAN AGAINST YOU"
    return "CLOSE TO A COIN FLIP"


def verdict_notes(result: Mapping[str, Any]) -> List[str]:
    """Plain-language context for the verdict: the break-even rule and where the edge comes from."""
    rr = float(result["rr"])
    be = float(result["breakeven_win_rate_pct"])
    share = result.get("resolved_target_share_pct")
    out = [f"with R:R {rr:.2f} you need more than {be:.1f}% of resolved trades (target or stop) to "
           f"hit the target to break even" + ("" if share is None else f"; the replays got {share:.1f}%")]
    if share is None:
        out.append("no replay reached the stop or the target before the close")
    er = float(result["expected_r"])
    if er > 0.1 and result["target_first_pct"] <= result["stop_first_pct"]:
        out.append("the positive expected value comes from the reward:risk, not the odds - the stop is hit "
                   "at least as often as the target")
    elif er >= -0.1 and result["open_at_close_pct"] >= 50:
        out.append("most replays are still open at the close - the outcome mostly depends on where you exit "
                   "at the end of the day")
    trend = result.get("trend") or {}
    alt = trend.get("alt")
    main_v = result.get("verdict") or verdict(result)
    if alt:
        flips = alt["verdict"] != main_v
        material = (abs(float(alt["expected_r"]) - er) >= TREND_MATERIAL_R
                    or abs(alt["target_first_pct"] - result["target_first_pct"]) >= TREND_MATERIAL_PCT)
        if flips or material:
            out.append(_trend_note(result, trend, alt, flips))
    return out


def _drift_words(trend: Mapping[str, Any], side: str) -> Tuple[str, bool]:
    """('+0.11% on average between 09:30 and 10:10, the median time to the stop or target: 0.23R in
    favour of this long (t=1.5, within noise)', whether that drift is big enough to matter)."""
    h = trend.get("horizon") or {}
    drift_pct = float(trend.get("drift_pct", trend.get("avg_move_pct", 0.0)))
    t = float(trend.get("horizon_t_stat", trend.get("t_stat", 0.0)))
    drift_r = trend.get("drift_r")
    span = (f" between {h['start']} and {h['end']}" if h.get("start") else " over this window") + (
        ", the median time to the stop or target" if h.get("basis") == "median_resolution" else "")
    size = "" if drift_r is None else \
        f": {abs(drift_r):.2f}R {'in favour of' if drift_r >= 0 else 'against'} this {side}"
    noise = ", within noise" if abs(t) < 2 else ""
    return (f"{drift_pct:+.2f}% on average{span}{size} (t={t:.1f}{noise})",
            drift_r is not None and abs(drift_r) >= TREND_EXPLAINS_R)


def _trend_note(result: Mapping[str, Any], trend: Mapping[str, Any], alt: Mapping[str, Any], flips: bool) -> str:
    """Why the trend-included and trend-removed replays differ - worded so it never contradicts itself.

    The drift is measured from the entry to the median exit (see trend_check). It is named as the
    cause only when it is big enough (>= 0.05R) and points the way the two answers differ;
    otherwise the note says the odds are sensitive to small slot-by-slot drifts.
    """
    odds = (f"{alt['target_first_pct']:.1f}% target / {alt['stop_first_pct']:.1f}% stop, "
            f"{alt['expected_r']:+.2f}R, {alt['verdict']}")
    words, big = _drift_words(trend, str(result.get("side") or "trade"))
    detrended = bool(trend.get("detrended"))
    gain = float(result["expected_r"]) - float(alt["expected_r"])
    if detrended:
        gain = -gain                     # always: trend-included minus trend-removed expected R
    explains = big and float(trend.get("drift_r") or 0.0) * gain > 0
    if detrended:
        if explains:
            return f"with the time-of-day drift left in, the replays give {odds}; that drift is {words}"
        return (f"with the past sessions' small time-of-day drifts left in, the replays give {odds} - the odds "
                f"are sensitive to them, though they add up to only {words}")
    if explains:
        lead = ("this verdict leans on a time-of-day drift in the past sessions" if flips
                else "part of this result is a time-of-day drift in the past sessions")
        return f"{lead}, {words}; with the drift removed the replays give {odds} - short-term drifts often fade"
    lead = "this verdict" if flips else "this result"
    return (f"{lead} is sensitive to small time-of-day drifts in the past sessions, though they add up to only "
            f"{words}; with them removed the replays give {odds}")


def session_end_minute(meta: Mapping[str, Any], day: str) -> Optional[int]:
    """Regular-session close (minutes ET) Yahoo reports for `day` - e.g. 780 on a half day."""
    periods: List[Mapping[str, Any]] = []
    cur = (meta or {}).get("currentTradingPeriod")
    if isinstance(cur, MappingABC) and isinstance(cur.get("regular"), MappingABC):
        periods.append(cur["regular"])
    tp = (meta or {}).get("tradingPeriods")
    if isinstance(tp, MappingABC):         # includePrePost=true shape: {"pre", "regular", "post"}
        tp = tp.get("regular")
    if isinstance(tp, list):               # [[{start, end, ...}], ...] - one inner list per day
        for row in tp:
            for per in (row if isinstance(row, list) else [row]):
                if isinstance(per, MappingABC):
                    periods.append(per)
    for per in periods:
        try:
            start, end = int(per["start"]), int(per["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start and tc.et_date(start) == day and tc.et_date(end) == day:
            minute = tc.minute_of_day(end)
            if tc.MARKET_OPEN < minute <= tc.MARKET_CLOSE:
                return minute
    return None


def parse_at(value: str) -> Tuple[int, Optional[str]]:
    """--at HH:MM -> (slot, note-if-rounded). Must fall inside 09:30-15:59 ET."""
    minute = tc.parse_hhmm(value)
    if not tc.MARKET_OPEN <= minute < tc.MARKET_CLOSE:
        raise ValueError(f"--at must be a regular-session time from 09:30 to 15:55 ET, not {value}")
    slot = (minute - tc.MARKET_OPEN) // BAR_MINUTES
    note = None
    if minute % BAR_MINUTES:
        note = f"--at {tc.hhmm(minute)} rounded down to the {slot_time(slot)} 5-minute bar"
    return slot, note


def choose_start(now: datetime, at: Optional[str], today_bars: Sequence[tc.Bar],
                 today_close: Optional[int] = None) -> Dict[str, Any]:
    """Where the replays start and end.

    today_bars: today's regular-session bars that had started by `now`.
    today_close: today's close in minutes ET if Yahoo reports one (early-close days).
    --at wins; else "now" (rounded down to the 5-minute grid) when the market is open and today
    has bars; else a full session from the 09:30 open. The session simulated is today's while
    today's close is still ahead, otherwise the next one.
    """
    now_min = now.hour * 60 + now.minute
    weekday = now.weekday() < 5
    close = today_close if today_close and tc.MARKET_OPEN < today_close <= tc.MARKET_CLOSE else tc.MARKET_CLOSE
    before_close = weekday and now_min < close
    market_open = before_close and now_min >= tc.MARKET_OPEN
    end_slot = (close - tc.MARKET_OPEN) // BAR_MINUTES - 1 if before_close else LAST_SLOT
    notes: List[str] = []
    warnings: List[str] = []
    if at is not None:
        slot, note = parse_at(at)
        if note:
            notes.append(note)
        if before_close and slot > end_slot:
            raise ValueError(f"today's session closes early at {tc.hhmm(close)} ET - --at {at} is after "
                             "its last 5-minute bar")
        mode = "at"
        if not before_close:
            notes.append(f"market closed - simulating the next session from {slot_time(slot)} ET")
    elif market_open and today_bars:
        slot = min((now_min - tc.MARKET_OPEN) // BAR_MINUTES, end_slot)
        mode = "now"
        latest = max(today_bars, key=lambda b: b.ts)
        age = now.timestamp() - (latest.ts + BAR_SECONDS)
        if age > STALE_DATA_SECONDS:
            warnings.append(f"the latest 5-minute bar started at {latest.time} ET ({int(age // 60) + 5} min ago) - "
                            "halted, early close or delayed data? Check before trading")
    else:
        slot, mode = 0, "open"
        if before_close and now_min < tc.MARKET_OPEN:
            notes.append("market not open yet - simulating today's session from the 09:30 open")
        elif market_open:
            notes.append("no 5-minute bars from today yet (holiday, halt or delayed data) - simulating a "
                         "full session from the 09:30 open")
        else:
            notes.append("market closed - simulating the next session from the open")
    if before_close and close < tc.MARKET_CLOSE:
        notes.append(f"early close today at {tc.hhmm(close)} ET - replays end there")
    return {"start_slot": slot, "end_slot": end_slot, "mode": mode,
            "session": "today" if before_close else "next",
            "include_today": weekday and not before_close,   # today's session is complete
            "market_open": market_open, "notes": notes, "warnings": warnings}


def today_range_ratio(today_bars: Sequence[tc.Bar], sessions: Sequence[Sequence[tc.Bar]],
                      start_slot: int) -> Optional[float]:
    """Today's high-low range before `start_slot` vs the median past session's over the same slots."""
    def rng(bars: Sequence[tc.Bar]) -> Optional[float]:
        by = slot_bars(bars)
        pre = [by[k] for k in sorted(by) if k < start_slot]
        if len(pre) < 3:
            return None
        return (max(b.high for b in pre) - min(b.low for b in pre)) / pre[0].open

    today = rng(today_bars)
    past = [x for x in (rng(s) for s in sessions) if x is not None and x > 0]
    if today is None or len(past) < MIN_SESSIONS:
        return None
    return today / statistics.median(past)


def default_seed(symbol: str, day: str) -> int:
    """Stable seed from symbol + date, so reruns the same day give the same numbers."""
    return int(hashlib.sha256(f"{symbol.upper()}|{day}".encode("utf-8")).hexdigest()[:12], 16)


# ---------------------------------------------------------------------------
# SVG (pure string building)
# ---------------------------------------------------------------------------

SVG_W, SVG_H = 800, 450
PX0, PX1, PY0, PY1 = 24.0, 548.0, 58.0, 372.0
COLORS = {"bg": "#fbfaf7", "ink": "#1d1d1b", "muted": "#6b6a66", "grid": "#e6e3dc", "shade": "#f2efe8",
          "open": "#8c8c88", "target": "#1f9d55", "stop": "#d64545", "entry": "#e8692e"}


def _quantile(sorted_xs: Sequence[float], q: float) -> float:
    i = min(len(sorted_xs) - 1, max(0, int(q * (len(sorted_xs) - 1))))
    return sorted_xs[i]


def render_svg(res: Mapping[str, Any], samples: Sequence[Mapping[str, Any]],
               today_bars: Sequence[tc.Bar] = ()) -> str:
    """A self-contained ~800x450 SVG of the replays, the levels and the three percentages."""
    c = COLORS
    sym, side = res.get("symbol", ""), res["side"]
    entry, stop, target = res["entry"], res["stop"], res["target"]
    start, end = res["start_slot"], res["end_slot"]
    n_cols = end + 1                                            # x spans 09:30 .. end of last bar
    by_today = {k: b for k, b in slot_bars(today_bars).items() if k < start}

    values = sorted(v for s in samples for v in s["prices"])
    lo = min([stop, target, entry] + ([_quantile(values, 0.01)] if values else []))
    hi = max([stop, target, entry] + ([_quantile(values, 0.99)] if values else []))
    if by_today:   # show today's bars too, unless they are far away (an entry far from the price)
        t_lo, t_hi = min(b.low for b in by_today.values()), max(b.high for b in by_today.values())
        span = hi - lo
        if lo - span <= t_lo and t_hi <= hi + span:
            lo, hi = min(lo, t_lo), max(hi, t_hi)
    pad = (hi - lo) * 0.07 or entry * 0.01
    lo, hi = lo - pad, hi + pad

    def x(boundary: float) -> float:
        return PX0 + (PX1 - PX0) * boundary / n_cols

    def y(price: float) -> float:
        return PY1 - (PY1 - PY0) * (price - lo) / (hi - lo)

    def text(x_: float, y_: float, s: str, size: int = 11, weight: str = "normal", fill: str = c["ink"],
             anchor: str = "start", family: str = "Helvetica, Arial, sans-serif") -> str:
        return (f'<text x="{x_:.1f}" y="{y_:.1f}" font-family="{family}" font-size="{size}" '
                f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>')

    def pill(x_: float, y_: float, s: str, fill: str, anchor: str = "end") -> str:
        w = 7.2 * len(s) + 14
        left = x_ - w if anchor == "end" else x_
        return (f'<rect x="{left:.1f}" y="{y_ - 13:.1f}" width="{w:.1f}" height="18" rx="9" fill="{fill}"/>'
                + text(left + w / 2, y_, s, 11, "bold", "#ffffff", "middle"))

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_W}" height="{SVG_H}" '
           f'viewBox="0 0 {SVG_W} {SVG_H}">',
           f'<title>{escape(f"Time Machine - {sym} {side} replays")}</title>',
           f'<rect width="{SVG_W}" height="{SVG_H}" fill="{c["bg"]}"/>',
           f'<defs><clipPath id="plot"><rect x="{PX0}" y="{PY0}" width="{PX1 - PX0}" '
           f'height="{PY1 - PY0}"/></clipPath></defs>']
    when = "from the open" if res.get("mode") == "open" else f"from {res['start']} ET"
    if res.get("session") == "next":
        when += " (next session)"
    out.append(text(PX0, 32, f"TIME MACHINE · {sym} · {side.upper()} {when}", 18, "bold"))
    # plot frame, the shaded "rest of the day" and the time axis
    out.append(f'<rect x="{PX0}" y="{PY0}" width="{PX1 - PX0}" height="{PY1 - PY0}" fill="#ffffff" '
               f'stroke="{c["ink"]}" stroke-width="1.5"/>')
    out.append(f'<rect x="{x(start):.1f}" y="{PY0}" width="{PX1 - x(start):.1f}" height="{PY1 - PY0}" '
               f'fill="{c["shade"]}"/>')
    labels = [(0, "09:30"), (start, res["start"]), (n_cols, res["end"])]
    if abs(x(30) - x(start)) > 45 and abs(x(30) - x(n_cols)) > 45 and 30 < n_cols:
        labels.append((30, "12:00"))
    for b, lab in sorted(set(labels)):
        if b == start and b != 0 and (abs(x(b) - x(0)) < 40 or abs(x(b) - x(n_cols)) < 40):
            continue
        anchor = "start" if b == 0 else ("end" if b == n_cols else "middle")
        out.append(f'<line x1="{x(b):.1f}" y1="{PY1}" x2="{x(b):.1f}" y2="{PY1 + 5}" stroke="{c["ink"]}"/>')
        out.append(text(x(b), PY1 + 18, lab, 11, "normal", c["muted"], anchor, "Menlo, Consolas, monospace"))
    # today's bars before the start ("your trade" context)
    candles = []
    for k, b in sorted(by_today.items()):
        cx = x(k + 0.5)
        col = c["target"] if b.close >= b.open else c["stop"]
        candles.append(f'<line x1="{cx:.1f}" y1="{y(b.high):.1f}" x2="{cx:.1f}" y2="{y(b.low):.1f}" '
                       f'stroke="{c["ink"]}" stroke-width="1"/>')
        top, bot = y(max(b.open, b.close)), y(min(b.open, b.close))
        bw = max(1.0, (PX1 - PX0) / n_cols * 0.6)
        candles.append(f'<rect x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                       f'height="{max(0.8, bot - top):.1f}" fill="{col}"/>')
    # the replays: grey first, then stops, then targets on top
    lines = {"open": [], "stop": [], "target": []}
    for s in samples:
        pts = " ".join(f"{x(start + i):.1f},{y(v):.1f}" for i, v in enumerate(s["prices"]))
        lines[s["outcome"]].append(f'<polyline points="{pts}"/>')
    out.append('<g clip-path="url(#plot)">')
    out.extend(candles)
    for outcome, opacity in (("open", 0.28), ("stop", 0.35), ("target", 0.35)):
        if lines[outcome]:
            out.append(f'<g fill="none" stroke="{c[outcome]}" stroke-width="0.8" stroke-opacity="{opacity}" '
                       f'stroke-linejoin="round">')
            out.extend(lines[outcome])
            out.append("</g>")
    for price, col in ((target, c["target"]), (stop, c["stop"])):
        out.append(f'<line x1="{PX0}" y1="{y(price):.1f}" x2="{PX1}" y2="{y(price):.1f}" stroke="{col}" '
                   f'stroke-width="2" stroke-dasharray="7 5"/>')
    out.append("</g>")
    t_above = target > stop
    out.append(pill(PX1 - 6, y(target) + (-6 if t_above else 18), f"TARGET {tc.fmt_price(target)}", c["target"]))
    out.append(pill(PX1 - 6, y(stop) + (18 if t_above else -6), f"STOP {tc.fmt_price(stop)}", c["stop"]))
    ex, ey = x(start), y(entry)
    out.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="5.5" fill="{c["entry"]}" stroke="#ffffff" '
               f'stroke-width="1.5"/>')
    out.append(pill(ex + 8 if ex < PX1 - 130 else ex - 8, ey - 10, f"ENTRY {tc.fmt_price(entry)}", c["entry"],
                    "start" if ex < PX1 - 130 else "end"))
    out.append(f'<rect x="{PX0 + 8}" y="{PY0 + 8}" width="250" height="18" fill="#ffffff" '
               f'stroke="{c["ink"]}" stroke-width="0.8"/>')
    out.append(text(PX0 + 14, PY0 + 21, f"{len(samples):,} OF {res['paths']:,} PATHS DRAWN · ILLUSTRATIVE",
                    10, "bold", c["ink"], "start", "Menlo, Consolas, monospace"))
    # right panel: the odds
    rx0, rx1 = 572.0, 780.0
    out.append(text(rx0, 84, f"{res['paths']:,}", 30, "bold"))
    out.append(text(rx0, 102, f"REPLAYS FROM {res['sessions_used']} PAST SESSIONS", 10, "bold", c["muted"],
                    "start", "Menlo, Consolas, monospace"))
    shown = res.get("display_pct") or display_pct(res["counts"], res["paths"])   # same as the text
    rows = (("HIT TARGET FIRST", shown["target_first"], c["target"]),
            ("HIT STOP FIRST", shown["stop_first"], c["stop"]),
            ("OPEN AT CLOSE", shown["open_at_close"], c["open"]))
    for i, (label, pct, col) in enumerate(rows):
        ty = 138 + i * 46
        out.append(text(rx0, ty, label, 11, "bold", c["ink"], "start", "Menlo, Consolas, monospace"))
        out.append(text(rx1, ty, f"{pct:.1f}%", 16, "bold", c["ink"], "end"))
        out.append(f'<rect x="{rx0}" y="{ty + 7}" width="{rx1 - rx0}" height="10" fill="#ffffff" '
                   f'stroke="{c["muted"]}" stroke-width="0.6" stroke-dasharray="2 2"/>')
        out.append(f'<rect x="{rx0}" y="{ty + 7}" width="{(rx1 - rx0) * pct / 100:.1f}" height="10" fill="{col}"/>')
    er = res["expected_r"]
    out.append(text(rx0, 286, "EXPECTED VALUE", 11, "bold", c["ink"], "start", "Menlo, Consolas, monospace"))
    out.append(text(rx1, 286, f"{er:+.2f}R", 16, "bold", c["ink"], "end"))
    out.append(text(rx0, 318, "THE TIME MACHINE'S VERDICT", 10, "bold", c["muted"], "start",
                    "Menlo, Consolas, monospace"))
    for i, part in enumerate(textwrap.wrap(res.get("verdict", ""), 11)[:2]):
        out.append(text(rx0, 342 + i * 22, part, 20, "bold"))
    hist = res.get("history") or {}
    if hist.get("sessions"):
        out.append(text(PX0, 412, f"History, same clock time, last {hist['sessions']} sessions: "
                        f"{hist['target']} target / {hist['stop']} stop / {hist['open']} open at close",
                        11, "normal", c["ink"]))
    out.append(text(PX0, 434, "Simulated from the stock's own past 5-minute bars - not a guarantee. "
                    "Not financial advice. Paper trading only.", 10, "normal", c["muted"]))
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _price(value: str) -> float:
    try:
        v = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a price, got {value!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive price, got {value!r}")
    return v


def _int_in(lo: int, hi: int):
    def parse(value: str) -> int:
        try:
            v = int(value.replace(",", "").replace("_", ""))
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo:,} and {hi:,}, got {v:,}")
        return v
    return parse


def build_parser() -> argparse.ArgumentParser:
    p = tc.make_parser("Replay the rest of the session thousands of times from the stock's own past "
                       "5-minute bars and show the odds of target first, stop first or neither. "
                       "Simulation only - not financial advice.")
    p.add_argument("symbol", help="ticker, e.g. TSLA")
    p.add_argument("--entry", type=_price, help="fill price (default: the saved plan's entry)")
    p.add_argument("--stop", type=_price, help="stop price (default: the saved plan's stop)")
    p.add_argument("--target", type=_price, help="target price (default: the saved plan's target)")
    p.add_argument("--side", type=str.lower, choices=("long", "short"),
                   help="default: inferred from the levels (stop below entry = long)")
    p.add_argument("--at", metavar="HH:MM", help="start time ET (default: now if the market is open, "
                   "else the 09:30 open)")
    p.add_argument("--paths", type=_int_in(100, 200_000), default=DEFAULT_PATHS,
                   help=f"number of replays (default {DEFAULT_PATHS:,})")
    p.add_argument("--sessions", type=_int_in(MIN_SESSIONS, 250), default=DEFAULT_SESSIONS,
                   help=f"most recent past sessions to learn from (default {DEFAULT_SESSIONS})")
    p.add_argument("--block", type=_int_in(1, 24), default=DEFAULT_BLOCK,
                   help=f"bars copied from one past session at a time (default {DEFAULT_BLOCK} = 15 min)")
    p.add_argument("--seed", type=int, help="random seed (default: derived from symbol + date)")
    p.add_argument("--detrend", action="store_true", help="remove the past sessions' average drift from the "
                   "replays (keeps their volatility); the trend-included odds are then shown for comparison")
    p.add_argument("--svg", metavar="FILE", help="also draw 300 replays to this SVG file")
    p.add_argument("--save", action="store_true", help="merge the odds into plans/<SYMBOL>.json")

    def error(message: str) -> None:   # bad flags are bad input: exit 1 (2 means a data error)
        p.print_usage(sys.stderr)
        p.exit(1, f"{p.prog}: error: {message}\n")
    p.error = error  # type: ignore[assignment]
    return p


def parse_symbol(raw: str) -> str:
    sym = (raw or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.\-=^]{0,14}", sym):
        raise ValueError(f"not a ticker symbol: {raw!r}")
    return sym


def _plan_number(plan: Mapping[str, Any], key: str) -> Optional[float]:
    try:
        v = float(plan.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def resolve_levels(sym: str, entry: Optional[float], stop: Optional[float], target: Optional[float],
                   side: Optional[str], plan: Mapping[str, Any], today: str) -> Dict[str, Any]:
    """Levels from the flags, else the saved plan; side from --side, else inferred. Pure."""
    vals: Dict[str, float] = {}
    src: Dict[str, str] = {}
    for key, given in (("entry", entry), ("stop", stop), ("target", target)):
        if given is not None:
            vals[key], src[key] = given, "flag"
        else:
            pv = _plan_number(plan, key)
            if pv is not None:
                vals[key], src[key] = pv, "saved plan"
    missing = [k for k in ("entry", "stop", "target") if k not in vals]
    if missing:
        if not plan:
            raise ValueError(f"need --entry, --stop and --target (there is no saved {sym} plan - the exit "
                             "skill can build one with --save)")
        raise ValueError(f"the saved {sym} plan has no {' or '.join(missing)} - pass "
                         + " ".join(f"--{k} PRICE" for k in missing))
    notes: List[str] = []
    warnings: List[str] = []
    if side:
        side_source = "--side"
    elif vals["stop"] == vals["entry"]:
        raise ValueError("the stop equals the entry - there is no risk to measure")
    else:
        side, side_source = ("long" if vals["stop"] < vals["entry"] else "short"), "inferred from the stop"
        if side == "short":
            notes.append("side short inferred from the stop being above the entry (pass --side to be explicit)")
    _check_levels(side, vals["entry"], vals["stop"], vals["target"])
    from_plan = [k for k in ("entry", "stop", "target") if src[k] == "saved plan"]
    if from_plan and len(from_plan) < 3:
        notes.append(f"{', '.join(from_plan)} from the saved {sym} plan")
    if from_plan:
        made = str(plan.get("updated_at") or plan.get("created_at") or "")[:10]
        if made and made < today:
            warnings.append(f"the saved {sym} plan is from {made} - its levels may be stale; pass --entry, "
                            "--stop and --target to test today's levels")
    if src["entry"] == "saved plan":
        played_out = finished_trigger_note(plan, sym, vals["entry"], today)
        if played_out:
            notes.append(played_out)
    return {"side": side, "entry": vals["entry"], "stop": vals["stop"], "target": vals["target"],
            "sources": src, "side_source": side_source, "notes": notes, "warnings": warnings}


def finished_trigger_note(plan: Mapping[str, Any], sym: str, entry: float, today: str) -> Optional[str]:
    """The entry skill's note when the plan's entry is a trigger from a session that is already over.

    entry --save after the close (or of a past --date) stores entry_signal.session_over and a note;
    a trigger saved live whose session date is now in the past has played out just the same.
    """
    sig = plan.get("entry_signal")
    if not isinstance(sig, MappingABC) or not isinstance(sig.get("entry"), (int, float)) \
            or not _same_level(sig["entry"], entry):
        return None
    day = str(sig.get("date") or "")
    if not (sig.get("session_over") or sig.get("replay") or (day and day < today)):
        return None
    note = sig.get("note") or f"trigger is from the finished {day} session - re-plan from the next open"
    when = f" at {sig['triggered_at']} ET" if sig.get("triggered_at") else ""
    what = f"{sig.get('label') or sig.get('setup') or 'entry'}{when}"
    return f"the saved {sym} entry {tc.fmt_price(entry)} is an entry-skill trigger ({what}): {note}"


def _not_found(err: Exception) -> bool:
    msg = str(err)
    return any(s in msg for s in ("HTTP 404", "Yahoo: Not Found", "No data found"))


def plan_path(sym: str) -> str:
    return f"plans/{tc._safe_symbol(sym)}.json"


# Plan sections other skills computed for specific levels -> the levels they depend on.
DEPENDENT_SECTIONS = {"sizing": ("side", "entry", "stop"), "exit_plan": ("side", "entry", "stop", "target")}


def _same_level(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= (0.005 if min(a, b) >= 1 else 0.00005) + 1e-9
    return a == b


def time_machine_section(res: Mapping[str, Any]) -> Dict[str, Any]:
    """The `time_machine` block stored in plans/<SYM>.json (levels included, so readers can check
    the odds still belong to the plan's levels)."""
    hist = res["history"]
    return {
        "side": res["side"], "entry": res["entry"], "stop": res["stop"], "target": res["target"],
        "rr": res["rr"], "start": res["start"], "mode": res["mode"],
        "target_first_pct": res["target_first_pct"], "stop_first_pct": res["stop_first_pct"],
        "neither_pct": res["open_at_close_pct"], "open_at_close_pct": res["open_at_close_pct"],
        "display_pct": dict(res["display_pct"]),
        "ambiguous_pct": res["ambiguous_pct"], "expected_r": res["expected_r"],
        "avg_r_open": res["avg_r_open"], "paths": res["paths"], "sessions": res["sessions_used"],
        "block": res["block"], "seed": res["seed"], "detrended": res["trend"]["detrended"],
        "verdict": res["verdict"],
        "history": {k: hist[k] for k in ("sessions", "target", "stop", "open")},
        "as_of": res["as_of"],
    }


def save(res: Mapping[str, Any], existing: Optional[Mapping[str, Any]]) -> List[str]:
    """Merge levels + time_machine into the plan; drop sections computed for other levels."""
    # Only sections with content: the entry skill resets them to null when its levels change.
    dropped = [name for name, keys in DEPENDENT_SECTIONS.items()
               if existing and existing.get(name) and not all(_same_level(existing.get(k), res[k]) for k in keys)]
    merged = tc.save_plan({"symbol": res["symbol"], "side": res["side"], "entry": res["entry"],
                           "stop": res["stop"], "target": res["target"],
                           "time_machine": time_machine_section(res)})
    if dropped:
        for k in dropped:
            merged.pop(k, None)
        tc.save_json(plan_path(res["symbol"]), merged)
    return dropped


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    sym = parse_symbol(args.symbol)
    now = tc.now_et()
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    plan = tc.load_plan(sym) or {}
    if not isinstance(plan, dict):
        raise tc.StateError(f"{tc.home() / plan_path(sym)} is not a trade plan object; fix or delete it")
    lv = resolve_levels(sym, args.entry, args.stop, args.target, args.side, plan, today)
    if args.at is not None:
        parse_at(args.at)                                  # bad --at is bad input, before any fetch
    svg_path = Path(args.svg).expanduser() if args.svg else None
    if svg_path is not None and (svg_path.is_dir() or not svg_path.parent.is_dir()):
        raise ValueError(f"--svg {args.svg}: not a writable file path (does the folder exist?)")
    side, entry, stop, target = lv["side"], lv["entry"], lv["stop"], lv["target"]
    warnings: List[str] = list(lv["warnings"])
    notes: List[str] = list(lv["notes"])

    try:
        raw, meta = tc.fetch_bars(sym, "5m", "60d")
    except tc.FetchError as e:
        if _not_found(e):
            raise ValueError(f"Yahoo Finance has no data for {sym} - check the symbol") from e
        raise
    groups = tc.group_sessions(raw)
    today_bars = [b for b in groups.get(today, []) if b.ts <= now_ts]
    st = choose_start(now, args.at, today_bars, session_end_minute(meta, today))
    warnings += st["warnings"]
    notes += st["notes"]

    days = [d for d in groups if d < today or (d == today and st["include_today"])]
    usable = [d for d in days if len(slot_bars(groups[d])) >= MIN_SESSION_BARS]
    if len(usable) < MIN_SESSIONS:
        raise ValueError(f"only {len(usable)} past session(s) of 5-minute data for {sym} - need at least "
                         f"{MIN_SESSIONS} to replay (new listing, halted or thinly traded?)")
    use = usable[-args.sessions:]
    sessions = [groups[d] for d in use]
    thin = [d for d in days if d >= use[0] and d not in usable]
    if thin:
        notes.append(f"skipped {len(thin)} past session(s) with under an hour of 5-minute bars ({', '.join(thin)})")
    if len(use) < args.sessions and args.sessions == DEFAULT_SESSIONS:
        notes.append(f"Yahoo returned {len(use)} usable past sessions (5-minute data only goes back ~60 days)")
    elif len(use) < args.sessions:
        notes.append(f"only {len(use)} usable past sessions available (asked for {args.sessions})")
    if len(use) < FEW_SESSIONS:
        warnings.append(f"only {len(use)} past sessions to learn from - treat these odds as rough")
    newest = datetime.strptime(use[-1], "%Y-%m-%d")
    age_days = (datetime.strptime(today, "%Y-%m-%d") - newest).days
    if age_days > STALE_LIBRARY_DAYS:
        warnings.append(f"the newest 5-minute data is from {use[-1]} ({age_days} days ago) - halted, delisted "
                        "or stale data?")

    seed = args.seed if args.seed is not None else default_seed(sym, today)
    library = build_library(sessions)
    flat = detrend_library(library)
    sim = simulate(flat if args.detrend else library, st["start_slot"], entry, stop, target, side, args.paths,
                   random.Random(seed), args.block, end_slot=st["end_slot"], keep_paths=SVG_PATHS)
    samples = sim.pop("sample_paths")
    # The same replays with the trend switched the other way (common random numbers): how much of
    # the answer is just the last ~60 days' drift?
    alt = simulate(library if args.detrend else flat, st["start_slot"], entry, stop, target, side,
                   args.paths, random.Random(seed), args.block, end_slot=st["end_slot"])
    alt.pop("sample_paths")
    with_trend = alt if args.detrend else sim
    trend = trend_check(library, sim["start_slot"], sim["end_slot"], side, entry, stop,
                        with_trend["median_minutes_to_resolution"])
    trend.update({"detrended": bool(args.detrend), "alt": {
        k: alt[k] for k in ("target_first_pct", "stop_first_pct", "open_at_close_pct", "expected_r")}})
    trend["alt"]["verdict"] = verdict(alt)
    stop_pct = (stop / entry - 1.0) * 100.0
    target_pct = (target / entry - 1.0) * 100.0
    hist = replay_history({d: groups[d] for d in use}, sim["start_slot"], stop_pct, target_pct, side,
                          end_slot=sim["end_slot"])

    if sim["end_capped"]:
        notes.append(f"past sessions only have bars until {sim['end']} ET, so the replays stop there")
    if sim["minutes_simulated"] <= 30:
        notes.append(f"only {sim['minutes_simulated']} minutes of session left after {sim['start']} ET - "
                     f"a trade still open at {sim['end']} is valued at that close")
    if sim["ambiguous_pct"] >= AMBIGUOUS_NOTE_PCT:
        notes.append(f"{sim['ambiguous_pct']:.1f}% of replays touched the stop and the target in the same "
                     "5-minute bar; they count as stops (conservative) - these levels are tight for 5-minute bars")
    session_today = st["session"] == "today"
    last_bar = max(today_bars, key=lambda b: b.ts) if (today_bars and session_today) else None
    last_price = last_bar.close if last_bar else None
    # Compare with today's price only when the start is now or still ahead (not a past --at).
    starts_ahead = st["mode"] == "now" or (st["mode"] == "at" and last_bar is not None
                                           and sim["start_slot"] >= (slot_of(last_bar.ts) or 0))
    if last_price is not None and starts_ahead:
        beyond_stop = last_price <= stop if side == "long" else last_price >= stop
        beyond_target = last_price >= target if side == "long" else last_price <= target
        if beyond_stop:
            warnings.append(f"the last price {tc.fmt_price(last_price)} is already through your stop "
                            f"{tc.fmt_price(stop)} - these odds only apply if you can still get filled at the entry")
        elif beyond_target:
            warnings.append(f"the last price {tc.fmt_price(last_price)} is already past your target "
                            f"{tc.fmt_price(target)} - the move may have happened without you")
        elif abs(last_price - entry) > abs(entry - stop):
            notes.append(f"the entry {tc.fmt_price(entry)} is more than 1R from the last price "
                         f"{tc.fmt_price(last_price)} - the odds assume you get filled at the entry at {sim['start']}")
    ratio = today_range_ratio(today_bars, sessions, sim["start_slot"]) if st["mode"] == "now" else None
    if ratio is not None and ratio >= 2.0:
        warnings.append(f"today is unusually volatile: its range by {sim['start']} is {ratio:.1f}x a typical "
                        "day's - the replays come from ordinary days and likely understate how fast either "
                        "level gets hit")
    elif ratio is not None and ratio <= 0.5:
        notes.append(f"today is unusually quiet: its range by {sim['start']} is {ratio:.1f}x a typical day's - "
                     "the replays may overstate how far the price travels")
    # Rough size of the error from learning on only len(use) days (binomial, floored at p = 10%).
    p_big = max(sim["target_first_pct"], sim["stop_first_pct"]) / 100.0
    day_noise = round(100.0 * math.sqrt(max(p_big * (1.0 - p_big), 0.09) / len(use)), 1)

    res: Dict[str, Any] = {"symbol": sym}
    res.update(sim)
    res.update({
        "stop_pct": round(stop_pct, 3), "target_pct": round(target_pct, 3),
        "mode": st["mode"], "session": st["session"], "market_open": st["market_open"],
        "sessions_requested": args.sessions, "first_session": use[0], "last_session": use[-1],
        "seed": seed, "history": hist, "trend": trend, "day_sample_uncertainty_pct": day_noise,
        "today_range_ratio": None if ratio is None else round(ratio, 2),
        "last_price": last_price,
        "levels_source": lv["sources"], "side_source": lv["side_source"],
    })
    res["verdict"] = verdict(res)
    res["verdict_notes"] = verdict_notes(res)
    res.update({"warnings": warnings, "notes": notes, "as_of": now.isoformat(timespec="seconds"),
                "svg": None, "saved": False, "plan_file": None, "dropped_sections": [],
                "disclaimer": tc.DISCLAIMER})
    if svg_path is not None:
        try:
            svg_path.write_text(render_svg(res, samples, today_bars if session_today else ()), encoding="utf-8")
        except OSError as e:
            raise ValueError(f"could not write --svg {args.svg}: {e.strerror or e}") from e
        res["svg"] = str(svg_path.resolve())
    if args.save:
        res["dropped_sections"] = save(res, plan or None)
        res.update(saved=True, plan_file=str(tc.home() / plan_path(sym)))
    tc.emit(res, args.json, render)
    return 0


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------

WIDTH = 100


def _wrap(text_: str, first: str, rest: str) -> List[str]:
    if "://" in text_:
        return [first + text_]
    out = textwrap.wrap(text_.replace(" - ", "\u00a0- "), WIDTH, initial_indent=first, subsequent_indent=rest,
                        break_on_hyphens=False, break_long_words=False)
    return [line.replace("\u00a0", " ") for line in out] or [first.rstrip()]


def _mins(x: Optional[float]) -> str:
    if x is None:
        return "-"
    x = int(round(x))
    return f"{x // 60}h {x % 60:02d}m" if x >= 60 else f"{x} min"


def render(res: Mapping[str, Any]) -> str:
    fp = tc.fmt_price
    sym, n = res["symbol"], res["paths"]
    c = res["counts"]
    # one-decimal percentages that still add up to 100.0 (saved in the plan as display_pct)
    shown = res.get("display_pct") or display_pct(c, n)
    pt, ps, po = shown["target_first"], shown["stop_first"], shown["open_at_close"]
    when = f"from {res['start']} ET" if res["mode"] != "open" else "from the 09:30 open"
    which = " (next session)" if res["session"] == "next" else ""
    lines = [f"TIME MACHINE · {sym} · {res['side'].upper()} {when}{which} · {n:,} replays from {sym}'s past "
             f"{res['sessions_used']} sessions",
             f"entry {fp(res['entry'])}  target {fp(res['target'])} ({res['target_pct']:+.2f}%)  "
             f"stop {fp(res['stop'])} ({res['stop_pct']:+.2f}%)  R:R {res['rr']:.2f}"]
    amb = f" · {res['ambiguous_pct']:.1f}% both in one bar (counted as stop)" if c["ambiguous"] else ""
    avg = res["avg_r_open"]
    rows = [
        ("HIT TARGET FIRST", pt, f"±{res['target_first_se']:.1f}",
         f"median {_mins(res['median_minutes_to_target'])}" if c["target"] else ""),
        ("HIT STOP FIRST", ps, f"±{res['stop_first_se']:.1f}",
         (f"median {_mins(res['median_minutes_to_stop'])}" if c["stop"] else "") + amb),
        ("OPEN AT CLOSE", po, "" if avg is None else f"(avg {avg:+.2f}R)", ""),
    ]
    for label, pct, a, b in rows:
        lines.append(f"{label:<18} {pct:5.1f}%  {a:<12} {b}".rstrip())
    share = res["resolved_target_share_pct"]
    lines.append(f"Expected value: {res['expected_r']:+.2f}R per trade   Break-even needs > "
                 f"{res['breakeven_win_rate_pct']:.1f}% of resolved trades"
                 + ("" if share is None else f" (replays: {share:.1f}%)"))
    h = res["history"]
    if h["sessions"]:
        hp = " / ".join(f"{x:.0f}%" for x in split_pct([h["target"], h["stop"], h["open"]], h["sessions"], 0))
        skipped = f", {h['skipped']} skipped: no bars then" if h["skipped"] else ""
        lines.extend(_wrap(f"History check (same clock time, last {h['sessions']} sessions{skipped}): "
                           f"{h['target']} target / {h['stop']} stop / {h['open']} open ({hp})", "", "  "))
    else:
        lines.append(f"History check: no past session had bars at {res['start']} ET")
    tr = res["trend"]
    a = tr["alt"]
    h = tr.get("horizon") or {}
    if h.get("start"):
        why = " (the median time to stop/target)" if h.get("basis") == "median_resolution" else ""
        drift_r = "" if tr.get("drift_r") is None else f" = {tr['drift_r']:+.2f}R for this {res['side']}"
        rest = ("" if h.get("end") == res["end"] else
                f"; whole window to {res['end']}: {tr['avg_move_pct']:+.2f}% (t={tr['t_stat']:.1f})")
        lines.extend(_wrap(f"Trend check: from {h['start']} to {h['end']}{why} the past sessions drifted "
                           f"{tr['drift_pct']:+.2f}% on average{drift_r} (t={tr['horizon_t_stat']:.1f}; |t| < 2 is "
                           f"noise){rest}", "", " " * 13))
    else:
        lines.append(f"Trend check: the past sessions drifted {tr['avg_move_pct']:+.2f}% on average from "
                     f"{res['start']} to {res['end']} (t={tr['t_stat']:.1f}; |t| < 2 is noise)")
    lines.append(" " * 13 + ("with the trend left in: " if tr["detrended"] else "trend removed: ")
                 + f"{a['target_first_pct']:.1f}% target / {a['stop_first_pct']:.1f}% stop / "
                 f"{a['open_at_close_pct']:.1f}% open, {a['expected_r']:+.2f}R")
    lines.append(f"VERDICT: {res['verdict']}")
    for v in res["verdict_notes"]:
        lines.extend(_wrap(v, "  - ", "    "))
    lines.extend(_wrap(f"± is replay noise only: the replays recycle {res['sessions_used']} real days, so the "
                       f"odds themselves are uncertain by roughly ±{res['day_sample_uncertainty_pct']:.0f} points or "
                       "more.", "  - ", "    "))
    if res["warnings"] or res["notes"]:
        lines.append("")
        for w in res["warnings"]:
            lines.extend(_wrap(w, "  ! ", "    "))
        for note in res["notes"]:
            lines.extend(_wrap(note, "  - ", "    "))
    lines.append("")
    lines.append(f"Replayed {res['start']}-{res['end']} ET in {res['block']}-bar blocks from sessions "
                 f"{res['first_session']}..{res['last_session']}"
                 + (" with the trend removed" if res["trend"]["detrended"] else "") + f" · seed {res['seed']}")
    if res["svg"]:
        lines.append(f"Chart: {res['svg']}")
    if res["saved"]:
        lines.append(f"Saved to {res['plan_file']}")
        if res["dropped_sections"]:
            lines.append(f"Cleared {', '.join(res['dropped_sections'])} from the plan: computed for the old "
                         "levels - re-run them.")
    else:
        lines.append("Not saved - add --save to store the odds in the trade plan.")
    lines.append("Next: risk to size the position, then a budget check and a paper ticket (needs your yes).")
    lines.append(res["disclaimer"])
    return "\n".join(lines)


if __name__ == "__main__":
    tc.run(main)
