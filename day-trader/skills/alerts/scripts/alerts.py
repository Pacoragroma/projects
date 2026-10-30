#!/usr/bin/env python3
"""Alerts: price alerts so you don't have to stare at charts.

Alert kinds (one per alert):
  above L       fires when price trades at or above L (5-minute bar high >= L)
  below L       fires when price trades at or below L (5-minute bar low <= L)
  pct-move P    fires when price trades P% away from a reference price (the price when the
                alert was added, or --ref PRICE|prev-close). "3" = either way, "+3" up only, "-3" down only
  vwap-cross    fires when a completed 5-minute bar closes on the other side of session VWAP
                (either way by default; "above" = reclaims VWAP, "below" = loses it)

Touches are found from the bars' highs/lows since the alert was created, so a spike between two
checks is not missed. A repeating alert fires at most once per bar and re-arms only after a
5-minute bar closes back across the level (by --rearm-pct, default 0.25%); --once alerts switch off
after firing.

Usage:
  python3 scripts/alerts.py add TSLA --above 370.04 --note "plan target"
  python3 scripts/alerts.py add TSLA --below 358.32 --once
  python3 scripts/alerts.py add NVDA --pct-move 3                 # 3% either way from now
  python3 scripts/alerts.py add NVDA --pct-move -2 --ref prev-close
  python3 scripts/alerts.py add AMD --vwap-cross                   # or: --vwap-cross above
  python3 scripts/alerts.py from-plan TSLA                         # entry/stop/target of the saved plan
  python3 scripts/alerts.py list
  python3 scripts/alerts.py check --json
  python3 scripts/alerts.py watch --interval 60 --max-minutes 120 --bell
  python3 scripts/alerts.py remove ALR-20261030-001 | --symbol TSLA | --fired | --all

Exit codes: 0 ok, 1 bad input/state, 2 data source unreachable, 130 watch stopped with Ctrl-C.
State: $DAYTRADER_HOME/alerts.json.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trader_common as tc  # noqa: E402

import argparse  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple  # noqa: E402

ALERTS_FILE = "alerts.json"
BAR_SECONDS = 300               # 5-minute bars
DEFAULT_REARM_PCT = 0.25        # a repeating alert re-arms after a 5m close this % back across
MAX_FIRES_KEPT = 20             # fire history kept per alert
STALE_MINUTES = 15              # warn when the newest bar is this old while the market is open
FAR_LEVEL_PCT = 50.0            # warn (typo?) when a level is this far from the price
PLAN_ROLES = ("entry", "stop", "target")
VWAP_DIRECTIONS = ("any", "above", "below")
EPS = 1e-9


# ---------------------------------------------------------------------------
# Pure core: bars
# ---------------------------------------------------------------------------

def _px(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    return round(x, 2) if abs(x) >= 1 else round(x, 4)


def slot_of(ts: float) -> int:
    """Start of the 5-minute bar containing `ts` (ET offsets are whole hours, so UTC grid works)."""
    t = int(ts)
    return t - t % BAR_SECONDS


def normalize_bars(bars: Sequence[tc.Bar]) -> List[tc.Bar]:
    """Snap bars to the 5-minute grid, merge duplicates, round to 4 decimals, oldest first.

    Yahoo may stamp the bar still forming with its last trade time (10:22:37) instead of its start
    (10:20), and sends float32-ish prices (370.0399780) - rounding keeps exact-level touches exact.
    """
    out: Dict[int, tc.Bar] = {}
    for b in sorted(bars, key=lambda x: x.ts):
        start = slot_of(b.ts)
        o, h, l, c = (round(float(x), 4) for x in (b.open, b.high, b.low, b.close))
        v = float(b.volume or 0)
        cur = out.get(start)
        if cur is None:
            out[start] = tc.Bar(start, o, max(h, o, c), min(l, o, c), c, v)
        else:
            out[start] = tc.Bar(start, cur.open, max(cur.high, h, c), min(cur.low, l, c), c,
                                max(cur.volume, v))
    return [out[k] for k in sorted(out)]


def quote_from_meta(meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """{'price', 'time'} from Yahoo chart meta (regularMarketPrice/Time), or None."""
    meta = meta or {}
    try:
        price = float(meta.get("regularMarketPrice"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(price) and price > 0):
        return None
    t = meta.get("regularMarketTime")
    try:
        t = int(t) if t is not None else None
    except (TypeError, ValueError):
        t = None
    return {"price": price, "time": t}


def _quote_tick(quote: Any, now_ts: int) -> Optional[Tuple[float, int]]:
    """A quote (float = observed now, or {'price', 'time'}) as (price, unix time); None if unusable."""
    if quote is None:
        return None
    if isinstance(quote, (int, float)):
        price, t = float(quote), now_ts
    else:
        try:
            price = float(quote.get("price"))
        except (TypeError, ValueError, AttributeError):
            return None
        t = quote.get("time")
        if t is None:
            return None  # cannot place it in time; the bars already carry the latest prices
        t = int(t)
    if not (math.isfinite(price) and price > 0) or t > now_ts:
        return None
    return round(price, 4), t


def prepare_bars(bars: Sequence[tc.Bar], quote: Any = None, now_ts: Optional[int] = None) -> List[tc.Bar]:
    """Regular-session 5m bars that had started by `now_ts`, with the live quote folded in.

    The quote extends the bar it falls in (or opens a new one-tick bar for the bar still forming),
    so a check sees a touch even when Yahoo's bar series lags the last trade. A one-tick bar is
    never made for a slot that has already ended: it would count as complete and move alerts'
    cursors past real bars Yahoo has not delivered yet.
    """
    out = [b for b in normalize_bars(bars)
           if tc.in_regular_session(b.ts) and (now_ts is None or b.ts < now_ts)]
    tick = _quote_tick(quote, now_ts if now_ts is not None else int(time.time()))
    if tick and tc.in_regular_session(tick[1]):
        p, t = tick
        slot = slot_of(t)
        forming = now_ts is None or slot + BAR_SECONDS > now_ts
        if out and out[-1].ts == slot:
            b = out[-1]
            out[-1] = tc.Bar(b.ts, b.open, max(b.high, p), min(b.low, p), p if forming else b.close, b.volume)
        elif forming and (not out or out[-1].ts < slot):
            out.append(tc.Bar(slot, p, p, p, p, 0.0))
    return out


def _newer_tick(prepared: Sequence[tc.Bar], tick: Optional[Tuple[float, int]]) -> bool:
    """True when the quote is from a bar slot after every prepared bar (Yahoo's bars lag)."""
    return bool(tick) and tc.in_regular_session(tick[1]) and (
        not prepared or tick[1] >= prepared[-1].ts + BAR_SECONDS)


def latest_price(prepared: Sequence[tc.Bar], quote: Any, now_ts: int) -> Optional[float]:
    tick = _quote_tick(quote, now_ts)
    if _newer_tick(prepared, tick):
        return tick[0]
    return prepared[-1].close if prepared else (tick[0] if tick else None)


def _sessions(bars: Sequence[tc.Bar]) -> Dict[str, List[tc.Bar]]:
    out: Dict[str, List[tc.Bar]] = {}
    for b in bars:
        out.setdefault(b.date, []).append(b)
    return out


def _sign(x: float, tol: float) -> int:
    return 1 if x > tol else (-1 if x < -tol else 0)


def market_context(bars: Sequence[tc.Bar], quote: Any, now: datetime) -> Dict[str, Any]:
    """Price facts an alert is created against: last price, current bar, VWAP side, previous close."""
    now_ts = int(now.timestamp())
    prepared = prepare_bars(bars, quote, now_ts)
    tick = _quote_tick(quote, now_ts)
    if _newer_tick(prepared, tick):
        last, last_ts = tick            # newer than every bar Yahoo sent
    elif prepared:
        last, last_ts = prepared[-1].close, prepared[-1].ts
    elif tick:
        last, last_ts = tick
    else:
        raise ValueError("no price in the data (no regular-session bars yet?)")
    today = now.strftime("%Y-%m-%d")
    after_close = now.weekday() < 5 and now.hour * 60 + now.minute >= tc.MARKET_CLOSE
    slot = slot_of(now_ts)
    cur = prepared[-1] if prepared and prepared[-1].ts == slot else None
    if cur is not None:
        snapshot = {"ts": slot, "high": cur.high, "low": cur.low}
    elif tc.in_regular_session(now_ts):
        snapshot = {"ts": slot, "high": last, "low": last}  # nothing printed yet in this bar
    else:
        snapshot = None
    sessions = _sessions(prepared)
    todays = sessions.get(today, [])
    vw = side = None
    # Too little data to tell counts as "has volume"; indexes/FX on Yahoo report 0 on every bar.
    has_volume = len(prepared) < 2 or any(b.volume > 0 for b in prepared)
    # After the bell the next session's first bars set the side (a gap is not a cross), so do not
    # claim one from today's finished session. One bar: VWAP is just its own typical price.
    if not after_close and len(todays) >= 2 and any(b.volume > 0 for b in todays):
        vw = tc.vwap(todays)[-1]
        side = _sign(last - vw, 1e-6 * vw) or None
    # "Previous close" = the last finished session: today's, once today's session is over.
    earlier = [d for d in sessions if d < today or (after_close and d == today)]
    prev_close = sessions[earlier[-1]][-1].close if earlier else None
    return {"last": last, "last_ts": last_ts, "snapshot": snapshot, "vwap": vw, "vwap_side": side,
            "vwap_day": today if side else None, "prev_close": prev_close,
            "market_open": tc.is_market_open(now), "has_volume": has_volume}


# ---------------------------------------------------------------------------
# Pure core: alerts
# ---------------------------------------------------------------------------

def parse_pct_move(value: str) -> Tuple[float, str]:
    """'3' -> (3.0, 'both'), '+3' -> (3.0, 'up'), '-2.5' -> (2.5, 'down')."""
    s = str(value).strip().rstrip("%").strip()
    m = re.fullmatch(r"([+-]?)(\d+(?:\.\d*)?|\.\d+)", s)
    if not m:
        raise ValueError(f"--pct-move must be a percentage like 3, +3 or -3, got {value!r}")
    pct = float(m.group(2))
    if not 0 < pct <= 50:
        raise ValueError("--pct-move must be between 0 and 50 (percent)")
    return pct, {"+": "up", "-": "down"}.get(m.group(1), "both")


def _leg(direction: str, level: float, ref: float) -> Dict[str, Any]:
    # Armed only while price is on the near side; created beyond the level = waits to re-arm.
    armed = ref < level if direction == "up" else ref > level
    return {"dir": direction, "level": level, "armed": armed, "last_fire_bar": None,
            "beyond_at_creation": not armed}


def make_alert(symbol: str, kind: str, ctx: Dict[str, Any], now: datetime, existing_ids: Sequence[str],
               level: Optional[float] = None, pct: Optional[float] = None, direction: str = "both",
               ref: Optional[float] = None, ref_basis: str = "price", vwap_direction: str = "any",
               once: bool = False, note: str = "", rearm_pct: float = DEFAULT_REARM_PCT,
               source: str = "manual", plan_role: Optional[str] = None) -> Dict[str, Any]:
    """A new alert dict (state included). `ctx` comes from market_context at `now`."""
    now_ts = int(now.timestamp())
    last = float(ctx["last"])
    a: Dict[str, Any] = {
        "id": tc.new_id("ALR", existing_ids), "symbol": symbol.upper(), "kind": kind,
        "once": bool(once), "note": note, "source": source, "plan_role": plan_role,
        "rearm_pct": float(rearm_pct), "status": "active",
        "created_at": now.isoformat(timespec="seconds"), "created_ts": now_ts,
        "created_price": last, "cursor": slot_of(now_ts), "snapshot": ctx.get("snapshot"),
        "fire_count": 0, "fires": [], "last_price": last,
        "last_checked": now.isoformat(timespec="seconds"),
    }
    if kind in ("above", "below"):
        if level is None or not (math.isfinite(level) and level > 0):
            raise ValueError("the alert level must be a positive price")
        a["level"] = round(float(level), 4)
        a["legs"] = [_leg("up" if kind == "above" else "down", a["level"], last)]
    elif kind == "pct_move":
        if pct is None or not 0 < pct <= 50:
            raise ValueError("--pct-move must be between 0 and 50 (percent)")
        base = float(ref) if ref is not None else last
        if not (math.isfinite(base) and base > 0):
            raise ValueError("--ref must be a positive price")
        a.update(pct=float(pct), direction=direction, ref_price=round(base, 4), ref_basis=ref_basis)
        legs = []
        if direction in ("both", "up"):
            legs.append(_leg("up", _px(base * (1 + pct / 100.0)), last))
        if direction in ("both", "down"):
            legs.append(_leg("down", _px(base * (1 - pct / 100.0)), last))
        a["legs"] = legs
    elif kind == "vwap_cross":
        if vwap_direction not in VWAP_DIRECTIONS:
            raise ValueError(f"--vwap-cross must be one of {', '.join(VWAP_DIRECTIONS)}")
        a.update(direction=vwap_direction, vwap_side=ctx.get("vwap_side"), vwap_day=ctx.get("vwap_day"),
                 last_vwap=_px(ctx.get("vwap")))
    else:
        raise ValueError(f"unknown alert kind {kind!r}")
    return a


def condition_text(a: Dict[str, Any]) -> str:
    """Short human label: 'above 370.04', '±3% from 362.50', 'VWAP cross'."""
    k = a["kind"]
    if k in ("above", "below"):
        return f"{k} {tc.fmt_price(a['level'])}"
    if k == "pct_move":
        sign = {"both": "±", "up": "+", "down": "-"}[a["direction"]]
        basis = " (prev close)" if a.get("ref_basis") == "prev-close" else ""
        return f"{sign}{a['pct']:g}% from {tc.fmt_price(a['ref_price'])}{basis}"
    d = a.get("direction", "any")
    return "VWAP cross" if d == "any" else f"VWAP cross {d}"


def rearm_threshold(a: Dict[str, Any], leg: Dict[str, Any]) -> float:
    buf = float(a.get("rearm_pct", DEFAULT_REARM_PCT)) / 100.0
    return leg["level"] * (1 - buf) if leg["dir"] == "up" else leg["level"] * (1 + buf)


def _effective_range(a: Dict[str, Any], b: tc.Bar) -> Tuple[float, float, bool]:
    """(high, low, whole_bar) of the part of `b` that traded after the alert was created.

    A bar that started before creation may have made its high/low earlier. Compared with the
    snapshot taken at creation, a *new* high/low is certainly post-creation; otherwise only the
    close (later than creation) is known to count. Conservative: no fire on pre-alert prices.
    """
    created = int(a["created_ts"])
    if b.ts >= created:
        return b.high, b.low, True
    snap = a.get("snapshot") or {}
    if snap.get("ts") == b.ts:
        hi = b.high if b.high > float(snap["high"]) + EPS else b.close
        lo = b.low if b.low < float(snap["low"]) - EPS else b.close
        return max(hi, b.close), min(lo, b.close), False
    return b.close, b.close, False


def _event(a: Dict[str, Any], b: tc.Bar, complete: bool, **extra: Any) -> Dict[str, Any]:
    e = {"id": a["id"], "symbol": a["symbol"], "kind": a["kind"], "condition": condition_text(a),
         "date": b.date, "bar_time": b.time, "bar_ts": b.ts, "bar_complete": complete,
         "bar": {"open": b.open, "high": b.high, "low": b.low, "close": b.close},
         "note": a.get("note") or "", "once": a["once"], "source": a.get("source"),
         "plan_role": a.get("plan_role")}
    e.update(extra)
    return e


def _eval_levels(a: Dict[str, Any], bars: Sequence[tc.Bar], now_ts: int) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for b in bars:
        if b.ts < a["cursor"]:
            continue
        complete = b.ts + BAR_SECONDS <= now_ts
        hi, lo, whole = _effective_range(a, b)
        bar_events = []
        for leg in a["legs"]:
            up, lvl = leg["dir"] == "up", leg["level"]
            if leg["armed"] and leg.get("last_fire_bar") != b.ts and ((hi >= lvl) if up else (lo <= lvl)):
                gap = whole and ((b.open >= lvl) if up else (b.open <= lvl))
                bar_events.append(_event(a, b, complete, leg=leg["dir"], level=lvl,
                                         how="gap" if gap else "touch", price=b.open if gap else lvl))
                leg["armed"] = False
                leg["last_fire_bar"] = b.ts
            # Re-arm on a completed bar's close back across: the close comes after any touch in
            # the bar, so "touched, then moved back" is certain - and a bar fires at most once.
            if not leg["armed"] and complete:
                thr = rearm_threshold(a, leg)
                if (b.close < thr) if up else (b.close > thr):
                    leg["armed"] = True
                    leg["beyond_at_creation"] = False
        events.extend(bar_events)
        if bar_events and a["once"]:
            a["status"] = "fired"
            return events
        if complete:
            a["cursor"] = b.ts + BAR_SECONDS
    return events


def _eval_vwap(a: Dict[str, Any], bars: Sequence[tc.Bar], now_ts: int) -> List[Dict[str, Any]]:
    """Close-based VWAP crosses on completed bars. A session's first two bars only set the side."""
    events: List[Dict[str, Any]] = []
    want = a.get("direction", "any")
    for day, sbars in _sessions(bars).items():
        vw = tc.vwap(sbars)
        volume = 0.0
        for i, b in enumerate(sbars):
            volume += b.volume
            if b.ts < a["cursor"]:
                continue
            if b.ts + BAR_SECONDS > now_ts:
                break  # the bar still forming: its close is not final
            # No volume yet (indexes and FX report none): there is no VWAP, only each bar's own
            # typical price, so nothing to cross.
            a["last_vwap"] = _px(vw[i]) if volume > 0 else None
            s = _sign(b.close - vw[i], 1e-6 * vw[i]) if i >= 1 and volume > 0 else 0
            if s:
                if a.get("vwap_day") != day or not a.get("vwap_side"):
                    a["vwap_side"], a["vwap_day"] = s, day
                elif s != a["vwap_side"]:
                    a["vwap_side"] = s
                    d = "above" if s > 0 else "below"
                    if want in ("any", d):
                        events.append(_event(a, b, True, leg=d, level=_px(vw[i]), vwap=_px(vw[i]),
                                             how="close", price=b.close))
            a["cursor"] = b.ts + BAR_SECONDS
            if events and a["once"]:
                a["status"] = "fired"
                return events
    return events


def evaluate(alert: Dict[str, Any], bars: Sequence[tc.Bar], quote: Any = None,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """Run one alert over `bars` (+ optional live quote) up to `now`. Pure: the input is not changed.

    Returns {'fired': bool, 'events': [...], 'alert': updated alert, 'last_price', 'notes'}.
    Only prices from after the alert's creation count; bars already processed are skipped (the
    alert's `cursor`), and the bar still forming is re-checked next time without double-firing.
    """
    a = copy.deepcopy(alert)
    now = now or tc.now_et()
    now_ts = int(now.timestamp())
    notes: List[str] = []
    if a.get("status") != "active":
        return {"fired": False, "events": [], "alert": a, "last_price": a.get("last_price"), "notes": notes}
    prepared = prepare_bars(bars, quote, now_ts)
    if prepared and prepared[0].ts > a["cursor"] + 4 * 86400 and prepared[0].ts > a["created_ts"]:
        notes.append(f"{a['symbol']}: the data starts {prepared[0].date}; touches before that cannot be checked")
    if a["kind"] == "vwap_cross":
        events = _eval_vwap(a, prepared, now_ts)
    else:
        events = _eval_levels(a, prepared, now_ts)
    last = latest_price(prepared, quote, now_ts)
    if last is not None:
        a["last_price"] = last
    a["last_checked"] = now.isoformat(timespec="seconds")
    for e in events:
        e["last_price"] = a.get("last_price")
        e["deactivated"] = a["status"] != "active"
    if events:
        a["fire_count"] = int(a.get("fire_count") or 0) + len(events)
        hist = list(a.get("fires") or [])
        hist.extend({k: e[k] for k in ("date", "bar_time", "leg", "level", "how", "price")} for e in events)
        a["fires"] = hist[-MAX_FIRES_KEPT:]
        if a["status"] != "active":
            a["fired_at"] = now.isoformat(timespec="seconds")
    return {"fired": bool(events), "events": events, "alert": a, "last_price": a.get("last_price"),
            "notes": notes}


def status_text(a: Dict[str, Any]) -> str:
    if a.get("status") != "active":
        return "fired" if a.get("status") == "fired" else str(a.get("status"))
    if a["kind"] == "vwap_cross":
        side, want = a.get("vwap_side"), a.get("direction", "any")
        if not side or (want == "above" and side > 0) or (want == "below" and side < 0):
            return "waiting"
        return "armed"
    armed = [leg for leg in a["legs"] if leg["armed"]]
    if len(armed) == len(a["legs"]):
        return "armed"
    if armed:
        return f"armed {'up' if armed[0]['dir'] == 'up' else 'down'} only"
    return "waiting"


def distance_pct(a: Dict[str, Any], last: Optional[float] = None) -> Optional[float]:
    """% from the last price to the nearest armed level (signed: + = level above price)."""
    last = a.get("last_price") if last is None else last
    if not last:
        return None
    if a["kind"] == "vwap_cross":
        v = a.get("last_vwap")
        return (v - last) / last * 100 if v else None
    legs = [leg for leg in a.get("legs", []) if leg["armed"]] or a.get("legs", [])
    if not legs:
        return None
    return min(((leg["level"] - last) / last * 100 for leg in legs), key=abs)


def waiting_reason(a: Dict[str, Any]) -> str:
    """What an alert is waiting for, in words."""
    if a.get("status") != "active":
        f = (a.get("fires") or [None])[-1]
        when = f" {f['date']} {f['bar_time']} ET" if f else ""
        return f"fired{when}; one-shot, now off"
    if a["kind"] == "vwap_cross":
        d = a.get("direction", "any")
        side = a.get("vwap_side")
        if not side:
            return "side vs VWAP not set yet (needs two completed bars this session)"
        now_side = "above" if side > 0 else "below"
        nxt = "below" if side > 0 else "above"
        if d in ("any", nxt):
            return f"price {now_side} VWAP - fires on a 5m close {nxt} it"
        return f"price {now_side} VWAP - re-arms on a 5m close {nxt} it, fires on the next close {now_side}"
    fires = [f"{'>=' if leg['dir'] == 'up' else '<='} {tc.fmt_price(leg['level'])}"
             for leg in a["legs"] if leg["armed"]]
    parts = [f"fires at {' or '.join(fires)}"] if fires else []
    for leg in a["legs"]:
        if not leg["armed"]:
            word, back = ("above", "below") if leg["dir"] == "up" else ("below", "above")
            why = f"was already at/{word}" if leg.get("beyond_at_creation") else "fired at"
            parts.append(f"{why} {tc.fmt_price(leg['level'])} - re-arms on a 5m close {back} "
                         f"{tc.fmt_price(rearm_threshold(a, leg))}")
    return "; ".join(parts)


def summary_row(a: Dict[str, Any]) -> Dict[str, Any]:
    d = distance_pct(a)
    last_fire = (a.get("fires") or [None])[-1]
    return {"id": a["id"], "symbol": a["symbol"], "kind": a["kind"], "condition": condition_text(a),
            "status": status_text(a), "once": a["once"], "last_price": a.get("last_price"),
            "distance_pct": None if d is None else round(d, 2), "waiting_for": waiting_reason(a),
            "fire_count": a.get("fire_count", 0), "last_fired": last_fire, "note": a.get("note") or "",
            "source": a.get("source"), "plan_role": a.get("plan_role"),
            "levels": [{"dir": leg["dir"], "level": leg["level"], "armed": leg["armed"]}
                       for leg in a.get("legs", [])],
            "vwap": a.get("last_vwap") if a["kind"] == "vwap_cross" else None,
            "created_at": a.get("created_at"), "last_checked": a.get("last_checked")}


def same_condition(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a["symbol"] != b["symbol"] or a["kind"] != b["kind"]:
        return False
    keys = {"above": ("level",), "below": ("level",), "pct_move": ("pct", "direction", "ref_price"),
            "vwap_cross": ("direction",)}[a["kind"]]
    return all(a.get(k) == b.get(k) for k in keys) and a["once"] == b["once"]


def plan_levels(plan: Dict[str, Any]) -> Tuple[str, float, float, Optional[float]]:
    """(side, entry, stop, target) of a saved plan, validated."""
    try:
        side = str(plan.get("side") or "")
        entry, stop = float(plan["entry"]), float(plan["stop"])
        target = None if plan.get("target") is None else float(plan["target"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("the saved plan needs numeric entry and stop prices") from None
    errs = tc.validate_levels(side, entry, stop, target)
    if errs:
        raise ValueError("saved plan is invalid: " + "; ".join(errs))
    return side, entry, stop, target


def plan_alert_specs(plan: Dict[str, Any], last: float,
                     roles: Sequence[str] = PLAN_ROLES) -> Tuple[List[Tuple[str, str, float]], List[str]]:
    """[(role, 'above'|'below', level)] for a saved plan, plus warnings. Direction-aware:
    long = stop below / target above, short = mirror; entry = whichever way price must travel."""
    side, entry, stop, target = plan_levels(plan)
    long = side == "long"
    sym = plan.get("symbol", "")
    specs: List[Tuple[str, str, float]] = []
    warns: List[str] = []
    if "entry" in roles:
        if last < entry:
            kind = "above"
            if not long:
                warns.append(f"{sym} {tc.fmt_price(last)} is already below the short entry: the entry "
                             f"alert fires on a bounce back up to {tc.fmt_price(entry)}")
        elif last > entry:
            kind = "below"
            if long:
                warns.append(f"{sym} {tc.fmt_price(last)} is already above the long entry: the entry "
                             f"alert fires on a pullback to {tc.fmt_price(entry)}")
        else:
            kind = "above" if long else "below"
            warns.append(f"{sym} is at the entry {tc.fmt_price(entry)} right now: the entry alert waits "
                         f"for a move away and back")
        specs.append(("entry", kind, entry))
    if "stop" in roles:
        specs.append(("stop", "below" if long else "above", stop))
        if (last <= stop) if long else (last >= stop):
            warns.append(f"{sym} {tc.fmt_price(last)} is already through the plan's stop "
                         f"{tc.fmt_price(stop)} - the plan looks stale")
    if "target" in roles:
        if target is None:
            warns.append(f"the {sym} plan has no target yet - no target alert (set one with the exit skill)")
        else:
            specs.append(("target", "above" if long else "below", target))
            if (last >= target) if long else (last <= target):
                warns.append(f"{sym} {tc.fmt_price(last)} is already through the plan's target "
                             f"{tc.fmt_price(target)}")
    return specs, warns


def plan_drift(a: Dict[str, Any], plan: Optional[Dict[str, Any]]) -> Optional[str]:
    """A note when a plan alert no longer matches the saved plan."""
    role = a.get("plan_role")
    if a.get("source") != "plan" or not role or a.get("status") != "active":
        return None
    if not plan:
        return f"{a['id']}: the {a['symbol']} plan is gone - remove this alert or set a new plan"
    cur = plan.get(role)
    if cur is None or abs(float(cur) - float(a.get("level", 0))) > 1e-6:
        return (f"{a['id']}: the {a['symbol']} plan {role} is now {tc.fmt_price(None if cur is None else float(cur))}"
                f" (alert at {tc.fmt_price(a.get('level'))}) - run from-plan {a['symbol']} again")
    return None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_KIND_KEYS = {"above": ("level", "legs"), "below": ("level", "legs"),
              "pct_move": ("pct", "direction", "ref_price", "legs"), "vwap_cross": ()}


def _valid_alert(a: Any) -> bool:
    """Enough structure for evaluate/list not to crash (a hand-edited file can break it)."""
    if not isinstance(a, dict) or a.get("kind") not in _KIND_KEYS:
        return False
    if not all(k in a for k in ("id", "symbol", "status", "created_ts", "cursor", "once") + _KIND_KEYS[a["kind"]]):
        return False
    legs = a.get("legs", [])
    return isinstance(legs, list) and all(isinstance(g, dict) and g.get("dir") in ("up", "down")
                                          and isinstance(g.get("level"), (int, float)) and "armed" in g
                                          for g in legs)


def load_state() -> Dict[str, Any]:
    data = tc.load_json(ALERTS_FILE, {"version": 1, "alerts": []})
    alerts = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(alerts, list):
        raise tc.StateError(f"{tc.state_path(ALERTS_FILE)} is not an alerts file; fix or delete it")
    bad = [str(a.get("id", "?")) if isinstance(a, dict) else "?" for a in alerts if not _valid_alert(a)]
    if bad:
        raise tc.StateError(f"{tc.state_path(ALERTS_FILE)} has malformed alert(s) {', '.join(bad)}; "
                            f"fix or delete the file (or remove those entries)")
    return data


def load_alerts() -> List[Dict[str, Any]]:
    return load_state()["alerts"]


def save_alerts(alerts: List[Dict[str, Any]]) -> None:
    """Write the alert list, remembering the highest ID ever issued so IDs are never reused."""
    try:
        last = load_state().get("last_id") or ""
    except tc.StateError:
        last = ""
    ids = [a["id"] for a in alerts] + ([last] if last else [])
    tc.save_json(ALERTS_FILE, {"version": 1, "last_id": max(ids) if ids else "", "alerts": alerts})


def issued_ids() -> List[str]:
    """IDs to avoid when making a new one: current alerts plus the last one ever issued."""
    st = load_state()
    return [a["id"] for a in st["alerts"]] + ([st["last_id"]] if st.get("last_id") else [])


def store_updates(updated: Dict[str, Dict[str, Any]]) -> None:
    """Write evaluated alerts back, re-reading the file first so an alert added or removed by
    another process while we were fetching is kept / stays removed."""
    def progress(x: Dict[str, Any]) -> Tuple[int, int, int]:
        return (int(x.get("status") != "active"), int(x.get("cursor") or 0), int(x.get("fire_count") or 0))

    def fresh(a: Dict[str, Any]) -> Dict[str, Any]:
        u = updated.get(a["id"])
        if u is None or u.get("created_ts") != a.get("created_ts"):
            return a
        # Another check (e.g. a background watch) may have saved this alert from newer data in
        # the meantime: never rewind it, or the next check would report an old touch again.
        return u if progress(u) >= progress(a) else a
    save_alerts([fresh(a) for a in load_alerts()])


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def parse_symbol(raw: str) -> str:
    sym = (raw or "").strip().upper()
    if not re.fullmatch(r"\^?[A-Z0-9][A-Z0-9.\-=]{0,14}", sym):  # ^VIX, BRK.B, ES=F, EURUSD=X
        raise ValueError(f"not a ticker symbol: {raw!r}")
    return sym


def range_for(cursor_ts: int, now_ts: int) -> str:
    """Smallest Yahoo 5m range that reaches back to `cursor_ts`."""
    days = (now_ts - cursor_ts) / 86400.0
    if days <= 4:
        return "5d"
    if days <= 25:
        return "1mo"
    return "60d"


def fetch(symbol: str, range_: str = "5d", ttl: Optional[float] = None) -> Tuple[List[tc.Bar], Dict[str, Any]]:
    try:
        bars, meta = tc.fetch_bars(symbol, "5m", range_, ttl=ttl)
    except tc.FetchError as e:
        msg = str(e)
        if "HTTP 404" in msg or "Yahoo: Not Found" in msg:
            raise ValueError(f"{symbol}: Yahoo has no chart for this symbol (unknown or delisted ticker?)") from e
        raise
    return bars, meta


def context_for(symbol: str) -> Tuple[Dict[str, Any], datetime]:
    bars, meta = fetch(symbol, "5d")
    now = tc.now_et()
    try:
        return market_context(bars, quote_from_meta(meta), now), now
    except ValueError as e:
        raise ValueError(f"{symbol}: {e}") from None


def check(symbols: Optional[Sequence[str]] = None, ttl: Optional[float] = None) -> Dict[str, Any]:
    """Fetch each symbol once, evaluate its active alerts, save state, return the report."""
    alerts = load_alerts()
    want = {s.upper() for s in symbols} if symbols else None
    active = [a for a in alerts if a.get("status") == "active" and (want is None or a["symbol"] in want)]
    now = tc.now_et()
    now_ts = int(now.timestamp())
    report: Dict[str, Any] = {"as_of": now.isoformat(timespec="seconds"), "market_open": tc.is_market_open(now),
                              "checked": len(active), "fired": [], "symbols": {}, "errors": {},
                              "notes": [], "alerts": [], "disclaimer": tc.DISCLAIMER}
    by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for a in active:
        by_sym.setdefault(a["symbol"], []).append(a)
    updated: Dict[str, Dict[str, Any]] = {}
    fetch_errors: List[str] = []
    for sym, group in by_sym.items():
        rng = range_for(min(int(a["cursor"]) for a in group), now_ts)
        try:
            bars, meta = fetch(sym, rng, ttl)
        except (tc.FetchError, ValueError) as e:
            report["errors"][sym] = str(e)
            if isinstance(e, tc.FetchError):
                fetch_errors.append(str(e))
            continue
        quote = quote_from_meta(meta)
        prepared = prepare_bars(bars, quote, now_ts)
        info: Dict[str, Any] = {"range": rng, "bars": len(prepared),
                                "last": latest_price(prepared, quote, now_ts),
                                "last_bar": prepared[-1].time if prepared else None}
        minute = now.hour * 60 + now.minute
        if (report["market_open"] and minute >= tc.MARKET_OPEN + STALE_MINUTES and prepared
                and prepared[-1].ts + BAR_SECONDS < now_ts - STALE_MINUTES * 60):
            report["notes"].append(f"{sym}: newest bar is {prepared[-1].date} {prepared[-1].time} ET - "
                                   f"data delayed, trading halted, or a market holiday?")
        report["symbols"][sym] = info
        plan, plan_ok = None, any(a.get("source") == "plan" for a in group)
        if plan_ok:
            try:
                plan = tc.load_plan(sym)
            except tc.StateError as e:
                plan_ok = False
                report["notes"].append(f"{sym}: could not read the saved plan ({e})")
        for a in group:
            res = evaluate(a, prepared, quote, now)
            updated[a["id"]] = res["alert"]
            report["fired"].extend(res["events"])
            report["notes"].extend(n for n in res["notes"] if n not in report["notes"])
            drift = plan_drift(res["alert"], plan) if plan_ok else None
            if drift:
                report["notes"].append(drift)
    if by_sym and not report["symbols"]:
        if fetch_errors:
            raise tc.FetchError(fetch_errors[0])
        raise ValueError("; ".join(report["errors"].values()))
    if updated:
        store_updates(updated)
    final = [updated.get(a["id"], a) for a in alerts]
    report["alerts"] = [summary_row(a) for a in final
                        if a.get("status") == "active" and (want is None or a["symbol"] in want)]
    report["active"] = len(report["alerts"])
    report["fired"].sort(key=lambda e: (e["bar_ts"], e["symbol"]))
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _clock(ts: int, today: str) -> str:
    d = tc.to_et(ts)
    return d.strftime("%H:%M") if d.strftime("%Y-%m-%d") == today else d.strftime("%a %m-%d %H:%M")


def _window(e: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """'10:05-10:10 ET' for the bar an event came from (cut at now for the bar still forming)."""
    now = now or tc.now_et()
    today = now.strftime("%Y-%m-%d")
    end = e["bar_ts"] + BAR_SECONDS
    if not e.get("bar_complete"):
        end = min(end, int(now.timestamp()))
    a, b = _clock(e["bar_ts"], today), tc.to_et(end).strftime("%H:%M")
    return f"{a}-{b} ET"


def event_line(e: Dict[str, Any], now: Optional[datetime] = None) -> str:
    now = now or tc.now_et()
    today = now.strftime("%Y-%m-%d")
    if e["kind"] == "vwap_cross":
        what = (f"closed {e['leg']} VWAP {tc.fmt_price(e['vwap'])} at "
                f"{_clock(e['bar_ts'] + BAR_SECONDS, today)} ET (5m close {tc.fmt_price(e['price'])})")
    else:
        word = "above" if e["leg"] == "up" else "below"
        if e["how"] == "gap":
            what = (f"gapped {word} {tc.fmt_price(e['level'])} - opened {tc.fmt_price(e['price'])} "
                    f"at {_clock(e['bar_ts'], today)} ET")
        else:
            ext = e["bar"]["high"] if e["leg"] == "up" else e["bar"]["low"]
            what = (f"traded {word} {tc.fmt_price(e['level'])} {_window(e, now)} "
                    f"({'high' if e['leg'] == 'up' else 'low'} {tc.fmt_price(ext)})")
    tail = [f"last {tc.fmt_price(e.get('last_price'))}"]
    if e.get("note"):
        tail.append(f'"{e["note"]}"')
    if e.get("deactivated"):
        tail.append("one-shot, now off")
    elif e["kind"] == "vwap_cross":
        tail.append("repeats on the next cross")
    else:
        tail.append("re-arms after a 5m close back across")
    return f"FIRED  {e['id']}  {e['symbol']}  {e['condition']}\n       {what} · " + " · ".join(tail)


def _away(r: Dict[str, Any]) -> str:
    d = r.get("distance_pct")
    if d is None:
        return "-"
    if r["kind"] == "vwap_cross":
        return f"VWAP {tc.fmt_price(r.get('vwap'))}"
    return f"{d:+.2f}%"


def alerts_table(rows: Sequence[Dict[str, Any]], with_reason: bool = True) -> str:
    out = []
    for r in rows:
        cond = r["condition"] + (" · once" if r["once"] else "")
        note = r.get("note") or ""
        out.append([r["id"], r["symbol"], cond, r["status"], tc.fmt_price(r.get("last_price")), _away(r),
                    str(r.get("fire_count", 0)), note])
    t = tc.table(out, ["id", "symbol", "alert", "status", "last", "away", "fired", "note"])
    if with_reason:
        waits = [f"  {r['id']}: {r['waiting_for']}" for r in rows if r["status"] != "armed"]
        if waits:
            t += "\n" + "\n".join(waits)
    return t


def render_check(rep: Dict[str, Any]) -> str:
    when = tc.parse_et(rep["as_of"]).strftime("%H:%M")
    syms = len(rep["symbols"]) + len(rep["errors"])
    head = (f"ALERTS CHECK · {when} ET · {rep['checked']} active alert{'s' if rep['checked'] != 1 else ''} "
            f"on {syms} symbol{'s' if syms != 1 else ''}")
    if not rep["market_open"]:
        head += " · market closed"
    lines = [head]
    if rep["checked"] == 0:
        lines.append("No active alerts. Add one: python3 scripts/alerts.py add TSLA --above 370.04")
        return "\n".join(lines)
    if rep["fired"]:
        lines.extend(event_line(e) for e in rep["fired"])
    else:
        lines.append("Nothing fired.")
    if rep["alerts"]:
        lines.append("")
        lines.append("Still watching:")
        lines.append(alerts_table(rep["alerts"]))
    for sym, msg in rep["errors"].items():
        lines.append(f"! {sym}: {msg}")
    for n in rep["notes"]:
        lines.append(f"note: {n}")
    if rep["fired"]:
        lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def render_list(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return "No alerts set. Add one: python3 scripts/alerts.py add TSLA --above 370.04"
    active = sum(1 for r in rows if r["status"] != "fired")
    fired = len(rows) - active
    checked = max((r.get("last_checked") or "" for r in rows), default="")
    head = f"ALERTS · {active} active" + (f", {fired} fired (one-shot)" if fired else "")
    if checked:
        head += f" · prices as of {tc.parse_et(checked).strftime('%m-%d %H:%M')} ET (run check to refresh)"
    return head + "\n" + alerts_table(rows)


def render_added(res: Dict[str, Any]) -> str:
    lines = []
    for a, r in zip(res["added"], (summary_row(x) for x in res["added"])):
        role = f" ({a['plan_role']})" if a.get("plan_role") else ""
        lines.append(f"Added {a['id']}{role} · {a['symbol']} {r['condition']}"
                     f"{' · once' if a['once'] else ''} · {r['status']}")
        lines.append(f"  last {tc.fmt_price(a['last_price'])} · {r['waiting_for']}"
                     + (f" ({_away(r)} away)" if r["status"] == "armed" and r["distance_pct"] is not None
                        and a["kind"] != "vwap_cross" else ""))
        if a.get("note") and not a.get("plan_role"):
            lines.append(f"  note: {a['note']}")
    for d in res.get("duplicates", []):
        lines.append(f"Already set: {d['id']} · {d['symbol']} {condition_text(d)} ({status_text(d)}) - not added again")
    if res.get("replaced"):
        lines.append(f"Replaced {len(res['replaced'])} earlier plan alert(s): {', '.join(res['replaced'])}")
    for w in res.get("warnings", []):
        lines.append(f"warning: {w}")
    if res["added"]:
        lines.append("Alerts fire only when checked: run `alerts.py check` when asked, or `alerts.py watch` to poll.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_add(args: argparse.Namespace) -> Dict[str, Any]:
    sym = parse_symbol(args.symbol)
    note = (args.note or "").strip()
    if len(note) > 200:
        raise ValueError("--note is limited to 200 characters")
    rearm = DEFAULT_REARM_PCT if args.rearm_pct is None else args.rearm_pct
    if not (math.isfinite(rearm) and 0 <= rearm <= 5):
        raise ValueError("--rearm-pct must be between 0 and 5 (percent)")
    if args.ref is not None and args.pct_move is None:
        raise ValueError("--ref only applies to --pct-move")
    kw: Dict[str, Any] = {}
    if args.above is not None or args.below is not None:
        kind = "above" if args.above is not None else "below"
        level = args.above if args.above is not None else args.below
        if not (math.isfinite(level) and level > 0):
            raise ValueError(f"--{kind} must be a positive price")
        kw["level"] = level
    elif args.pct_move is not None:
        kind = "pct_move"
        kw["pct"], kw["direction"] = parse_pct_move(args.pct_move)
        ref_raw = args.ref
        if ref_raw is not None and ref_raw.strip().lower() not in ("prev-close", "prev_close", "close"):
            try:
                kw["ref"] = float(ref_raw)
            except ValueError:
                raise ValueError(f"--ref must be a price or prev-close, got {ref_raw!r}") from None
            if not (math.isfinite(kw["ref"]) and kw["ref"] > 0):
                raise ValueError("--ref must be a positive price")
            kw["ref_basis"] = "manual"
    else:
        kind = "vwap_cross"
        kw["vwap_direction"] = args.vwap_cross
    load_alerts()  # fail on a corrupt state file before downloading anything
    ctx, now = context_for(sym)
    if kind == "vwap_cross" and not ctx["has_volume"]:
        raise ValueError(f"{sym}: Yahoo reports no volume for it (an index or FX pair?), so there is no "
                         f"VWAP to cross - use --above/--below or --pct-move instead")
    alerts = load_alerts()
    warnings: List[str] = []
    if kind == "pct_move" and args.ref is not None and "ref" not in kw:
        if ctx["prev_close"] is None:
            raise ValueError(f"{sym}: no previous session in the data to take the close from")
        kw["ref"], kw["ref_basis"] = ctx["prev_close"], "prev-close"
    new = make_alert(sym, kind, ctx, now, issued_ids(), once=args.once, note=note,
                     rearm_pct=rearm, **kw)
    dup = next((a for a in alerts if a.get("status") == "active" and same_condition(a, new)), None)
    if dup:
        return {"added": [], "duplicates": [dup], "warnings": warnings}
    if kind in ("above", "below"):
        away = abs(new["level"] - ctx["last"]) / ctx["last"] * 100
        if away > FAR_LEVEL_PCT:
            warnings.append(f"{new['level']:g} is {away:.0f}% from the last price {tc.fmt_price(ctx['last'])} - typo?")
        if not new["legs"][0]["armed"]:
            word = "above" if kind == "above" else "below"
            warnings.append(f"{sym} is already at/{word} {tc.fmt_price(new['level'])} (last {tc.fmt_price(ctx['last'])}): "
                            f"this alert waits for a move back across before it can fire")
    if kind == "pct_move" and not all(leg["armed"] for leg in new["legs"]):
        warnings.append(f"{sym} {tc.fmt_price(ctx['last'])} is already outside the band around "
                        f"{tc.fmt_price(new['ref_price'])}; that side waits for a move back inside")
    if kind == "vwap_cross" and not new.get("vwap_side"):
        warnings.append("too early in the session to say which side of VWAP price is on; the next "
                        "completed 5m bars set it" if ctx["market_open"] else
                        "market closed: the next session's first two 5m bars set the side of VWAP "
                        "(earliest cross 09:45 ET)")
    alerts.append(new)
    save_alerts(alerts)
    return {"added": [new], "duplicates": [], "warnings": warnings}


def cmd_from_plan(args: argparse.Namespace) -> Dict[str, Any]:
    sym = parse_symbol(args.symbol)
    roles = [r.strip().lower() for r in (args.levels or ",".join(PLAN_ROLES)).split(",") if r.strip()]
    if not roles or any(r not in PLAN_ROLES for r in roles):
        raise ValueError(f"--levels takes a comma list of {', '.join(PLAN_ROLES)}")
    rearm = DEFAULT_REARM_PCT if args.rearm_pct is None else args.rearm_pct
    if not (math.isfinite(rearm) and 0 <= rearm <= 5):
        raise ValueError("--rearm-pct must be between 0 and 5 (percent)")
    plan = tc.load_plan(sym)
    if not plan:
        raise ValueError(f"no saved plan for {sym} - make one with the exit skill "
                         f"(exit_plan.py {sym} --entry PRICE --save) or add alerts by hand")
    side, entry, stop, target = plan_levels(plan)  # validate before downloading
    load_alerts()
    ctx, now = context_for(sym)
    specs, warnings = plan_alert_specs(plan, ctx["last"], roles)
    alerts = load_alerts()
    replaced = [a["id"] for a in alerts
                if a["symbol"] == sym and a.get("source") == "plan" and a.get("plan_role") in roles]
    ids = issued_ids()
    added = []
    for role, kind, level in specs:
        a = make_alert(sym, kind, ctx, now, ids, level=level, once=not args.repeat,
                       note=f"{side} plan {role}", rearm_pct=rearm, source="plan", plan_role=role)
        ids.append(a["id"])
        added.append(a)
    save_alerts([a for a in alerts if a["id"] not in replaced] + added)
    return {"symbol": sym, "plan": {"side": side, "entry": entry, "stop": stop, "target": target,
                                    "setup": plan.get("setup"), "updated_at": plan.get("updated_at")},
            "added": added, "replaced": replaced, "duplicates": [], "warnings": warnings}


def render_from_plan(res: Dict[str, Any]) -> str:
    p = res["plan"]
    levels = f"entry {tc.fmt_price(p['entry'])} / stop {tc.fmt_price(p['stop'])} / target {tc.fmt_price(p['target'])}"
    kind = "one-shot" if all(a["once"] for a in res["added"]) else "repeating"
    head = (f"{res['symbol']} {p['side'].upper()} plan · {levels} · "
            f"{len(res['added'])} {kind} alert{'s' if len(res['added']) != 1 else ''}")
    return head + "\n" + render_added(res)


def cmd_remove(args: argparse.Namespace) -> Dict[str, Any]:
    selectors = sum(bool(x) for x in (args.ids, args.all, args.symbol, args.fired))
    if selectors != 1:
        raise ValueError("remove takes alert IDs, or one of --symbol SYM, --fired, --all")
    alerts = load_alerts()
    if args.all:
        gone = [a["id"] for a in alerts]
    elif args.fired:
        gone = [a["id"] for a in alerts if a.get("status") == "fired"]
    elif args.symbol:
        sym = parse_symbol(args.symbol)
        gone = [a["id"] for a in alerts if a["symbol"] == sym]
    else:
        index = {a["id"].upper(): a["id"] for a in alerts}
        missing = [i for i in args.ids if i.strip().upper() not in index]
        if missing:
            raise ValueError(f"no alert with id {', '.join(missing)} (see: alerts.py list)")
        gone = [index[i.strip().upper()] for i in args.ids]
    remaining = [a for a in alerts if a["id"] not in set(gone)]
    save_alerts(remaining)
    return {"removed": gone, "remaining": len(remaining)}


def render_removed(res: Dict[str, Any]) -> str:
    if not res["removed"]:
        return f"Nothing to remove. {res['remaining']} alert(s) remain."
    return f"Removed {len(res['removed'])}: {', '.join(res['removed'])}. {res['remaining']} alert(s) remain."


def _status_line(rep: Dict[str, Any]) -> str:
    when = tc.parse_et(rep["as_of"]).strftime("%H:%M")
    armed = [r for r in rep["alerts"] if r["status"] != "waiting"]
    parts = [f"{when} ET", f"{rep['active']} active"]
    now = tc.parse_et(rep["as_of"])
    if now.weekday() < 5 and now.hour * 60 + now.minute < tc.MARKET_OPEN:
        parts.append("pre-market (alerts use regular-session prices from 09:30 ET)")
    parts.append(f"{len(rep['fired'])} fired" if rep["fired"] else "nothing new")
    near = [r for r in armed if r["distance_pct"] is not None and r["kind"] != "vwap_cross"]
    if near:
        r = min(near, key=lambda x: abs(x["distance_pct"]))
        parts.append(f"nearest {r['symbol']} {r['condition']} ({r['distance_pct']:+.2f}% away)")
    if rep["errors"]:
        parts.append("errors: " + ", ".join(rep["errors"]))
    return " · ".join(parts)


def market_done_for_day(now: datetime) -> bool:
    return now.weekday() >= 5 or now.hour * 60 + now.minute >= tc.MARKET_CLOSE


def watch(symbols: Optional[Sequence[str]], interval: float, max_minutes: float, as_json: bool,
          bell: bool = False, quiet: bool = False, sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> int:
    """Re-run check every `interval` seconds; print fires as they happen."""
    alerts = load_alerts()
    want = {s.upper() for s in symbols} if symbols else None
    if not any(a.get("status") == "active" and (want is None or a["symbol"] in want) for a in alerts):
        raise ValueError("no active alerts to watch - add one first (alerts.py add TSLA --above 370.04)")
    started = clock()
    closed_at_start = market_done_for_day(tc.now_et())
    fired: List[Dict[str, Any]] = []
    polls = failures = 0
    stopped = "max-minutes"
    last_rep: Optional[Dict[str, Any]] = None
    said: set = set()  # notes/errors already printed (stale data, plan changed, symbol failing)
    data_error: Optional[tc.FetchError] = None
    try:
        while True:
            polls += 1
            try:
                rep = check(symbols, ttl=0)
            except tc.FetchError as e:
                failures += 1
                if last_rep is None:  # down from the start: say so now
                    raise
                if failures >= 3:     # down mid-run: still print the summary, then exit 2
                    stopped, data_error = "data error", e
                    break
                print(f"{tc.now_et().strftime('%H:%M')} ET  data error (retrying): {e}", file=sys.stderr, flush=True)
            else:
                failures = 0
                last_rep = rep
                fired.extend(rep["fired"])
                if as_json:
                    print(json.dumps(dict(rep, poll=polls), default=str), flush=True)
                else:
                    for e in rep["fired"]:
                        print(("\a" if bell else "") + event_line(e), flush=True)
                    if not quiet:
                        print(_status_line(rep), flush=True)
                        news = [f"! {s}: {m}" for s, m in rep["errors"].items()] + [f"note: {n}" for n in rep["notes"]]
                        for line in news:
                            if line not in said:
                                said.add(line)
                                print("  " + line, flush=True)
                if rep["active"] == 0:
                    stopped = "no active alerts left"
                    break
            if closed_at_start or market_done_for_day(tc.now_et()):
                stopped = "market closed"
                break
            if clock() - started + interval > max_minutes * 60 + 1:
                break
            sleep(interval)
    except KeyboardInterrupt:
        stopped = "interrupted"
    summary = {"final": True, "stopped": stopped, "polls": polls, "fired": fired,
               "active": last_rep["active"] if last_rep else None,
               "alerts": last_rep["alerts"] if last_rep else [], "disclaimer": tc.DISCLAIMER}
    if data_error is not None:
        summary["error"] = str(data_error)
    if as_json:
        print(json.dumps(summary, default=str))
    else:
        why = {"max-minutes": f"after {max_minutes:g} min", "interrupted": "(Ctrl-C)",
               "market closed": "- market closed for the day", "no active alerts left": "- no active alerts left",
               "data error": "- the data source failed 3 checks in a row"}[stopped]
        print(f"Stopped watching {why}: {polls} check{'s' if polls != 1 else ''}, "
              f"{len(fired)} alert{'s' if len(fired) != 1 else ''} fired.")
        if closed_at_start:
            print("The market is closed, so prices will not move until the next session; ran one check.")
        if fired:
            print(tc.DISCLAIMER)
    if data_error is not None:
        raise data_error
    return 130 if stopped == "interrupted" else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = tc.make_parser("Price alerts for the day-trading desk: levels, % moves and VWAP crosses, "
                       "checked against Yahoo 5-minute bars so touches between checks are not missed.")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    a = sub.add_parser("add", help="add an alert: --above X | --below X | --pct-move P | --vwap-cross")
    a.add_argument("symbol", help="ticker, e.g. TSLA")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--above", type=float, metavar="PRICE", help="fire when price trades at or above PRICE")
    g.add_argument("--below", type=float, metavar="PRICE", help="fire when price trades at or below PRICE")
    g.add_argument("--pct-move", metavar="PCT",
                   help="fire on a PCT%% move from the reference price: 3 = either way, +3 up, -3 down")
    g.add_argument("--vwap-cross", nargs="?", const="any", choices=VWAP_DIRECTIONS, metavar="any|above|below",
                   help="fire when a 5m bar closes across session VWAP (default: either way)")
    a.add_argument("--ref", metavar="PRICE|prev-close",
                   help="reference for --pct-move (default: the price now)")
    a.add_argument("--note", help="free text shown when the alert fires")
    a.add_argument("--once", action="store_true", help="switch the alert off after it fires once")
    a.add_argument("--rearm-pct", type=float, metavar="PCT",
                   help=f"repeating alerts re-arm after a 5m close this %% back across (default {DEFAULT_REARM_PCT})")
    tc.add_json_flag(a)

    f = sub.add_parser("from-plan", help="add entry/stop/target alerts from the saved plan")
    f.add_argument("symbol")
    f.add_argument("--levels", metavar="entry,stop,target", help="which plan levels (default: all three)")
    f.add_argument("--repeat", action="store_true", help="re-arm after firing (default: one-shot)")
    f.add_argument("--rearm-pct", type=float, metavar="PCT",
                   help=f"with --repeat: re-arm buffer in %% (default {DEFAULT_REARM_PCT})")
    tc.add_json_flag(f)

    ls = sub.add_parser("list", help="show alerts (no download)")
    ls.add_argument("--symbol", help="only this ticker")
    ls.add_argument("--active", action="store_true", help="hide one-shot alerts that already fired")
    tc.add_json_flag(ls)

    r = sub.add_parser("remove", help="remove alerts by ID, --symbol, --fired or --all")
    r.add_argument("ids", nargs="*", metavar="ID")
    r.add_argument("--all", action="store_true", help="remove every alert")
    r.add_argument("--symbol", help="remove every alert for this ticker")
    r.add_argument("--fired", action="store_true", help="remove one-shot alerts that already fired")
    tc.add_json_flag(r)

    c = sub.add_parser("check", help="fetch prices once and fire alerts whose condition was met")
    c.add_argument("--symbol", help="only these tickers (comma-separated)")
    tc.add_json_flag(c)

    w = sub.add_parser("watch", help="re-check every --interval seconds and print alerts as they fire")
    w.add_argument("--symbol", help="only these tickers (comma-separated)")
    w.add_argument("--interval", type=float, default=60.0, help="seconds between checks (default 60, min 15)")
    w.add_argument("--max-minutes", type=float, default=390.0, help="stop after this long (default 390)")
    w.add_argument("--bell", action="store_true", help="ring the terminal bell when an alert fires")
    w.add_argument("--quiet", action="store_true", help="print only fired alerts and the final summary")
    tc.add_json_flag(w)
    return p


def _symbols(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    return [parse_symbol(s) for s in raw.split(",") if s.strip()] or None


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # usage errors exit 1 (tc's parser); a stock argparse would exit 2
        if e.code in (1, 2):
            return 1
        raise
    as_json = bool(getattr(args, "json", False))
    if args.cmd == "add":
        tc.emit(cmd_add(args), as_json, render_added)
    elif args.cmd == "from-plan":
        tc.emit(cmd_from_plan(args), as_json, render_from_plan)
    elif args.cmd == "list":
        alerts = load_alerts()
        if args.symbol:
            sym = parse_symbol(args.symbol)
            alerts = [a for a in alerts if a["symbol"] == sym]
        if args.active:
            alerts = [a for a in alerts if a.get("status") == "active"]
        rows = [summary_row(a) for a in alerts]
        tc.emit({"alerts": rows, "count": len(rows),
                 "active": sum(1 for a in alerts if a.get("status") == "active")}, as_json,
                lambda d: render_list(d["alerts"]))
    elif args.cmd == "remove":
        tc.emit(cmd_remove(args), as_json, render_removed)
    elif args.cmd == "check":
        tc.emit(check(_symbols(args.symbol)), as_json, render_check)
    elif args.cmd == "watch":
        if not (math.isfinite(args.interval) and args.interval >= 15):
            raise ValueError("--interval must be at least 15 seconds")
        if not (math.isfinite(args.max_minutes) and 0 < args.max_minutes <= 600):
            raise ValueError("--max-minutes must be between 0 and 600")
        return watch(_symbols(args.symbol), args.interval, args.max_minutes, as_json, args.bell, args.quiet)
    return 0


if __name__ == "__main__":
    tc.run(main)
