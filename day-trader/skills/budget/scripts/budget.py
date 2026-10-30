#!/usr/bin/env python3
"""Budget: daily guard-rails over the paper ledger.

Max daily loss, max open risk, max trades per day and a tilt guard (stop after N losses in a
row). `status` prints the desk's "RISKED SO FAR" card, `check` says OK or BLOCKED for a new
trade's risk (run it before every ticket), `set` changes the limits and `history` shows past
days. Everything is read from the local paper ledger; only `status --mark` fetches prices.

Usage:
  python3 scripts/budget.py status                          # RISKED SO FAR card for today
  python3 scripts/budget.py status --mark                   # + live P&L of open positions (Yahoo)
  python3 scripts/budget.py status --date 2026-10-29        # how a past day ended
  python3 scripts/budget.py check --risk 46.88              # exit 0 = OK, 3 = BLOCKED
  python3 scripts/budget.py check --entry 364.18 --stop 358.32 --qty 8
  python3 scripts/budget.py check --symbol TSLA             # saved plan's levels + sizing
  python3 scripts/budget.py set --daily-max-loss 200 --max-trades 4
  python3 scripts/budget.py set                             # show the limits
  python3 scripts/budget.py history --days 10
  python3 scripts/budget.py status --json                   # --json on any command

Exit codes: 0 ok (check: OK) · 1 bad input or state · 2 data source down (status --mark only)
· 3 check BLOCKED.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trader_common as tc  # noqa: E402

import argparse  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from typing import Any, Dict, List, Optional, Sequence, Tuple  # noqa: E402

EXIT_OK, EXIT_BLOCKED = 0, 3
EPS = 1e-9
CAUTION_PCT = 75.0            # loss budget this full -> CAUTION
BIG_DAILY_LOSS_PCT = 5.0      # warn when the daily max loss is more than this % of the account
BAR_WIDTH = 20

OPEN, CAUTION, STOP = "OPEN FOR TRADING", "CAUTION", "STOP TRADING"

# settings key -> (CLI flag, kind, label)
LIMITS: Dict[str, Tuple[str, str, str]] = {
    "daily_max_loss": ("--daily-max-loss", "money", "daily max loss"),
    "max_open_risk": ("--max-open-risk", "money", "max open risk"),
    "max_trades_per_day": ("--max-trades", "count", "max trades per day"),
    "max_consecutive_losses": ("--max-losses", "count", "tilt guard (losses in a row)"),
}
NUMERIC_SETTINGS = ("account_size", "risk_per_trade_pct", "daily_max_loss", "max_open_risk")
INT_SETTINGS = ("max_trades_per_day", "max_consecutive_losses")
TRADE_STATUSES = ("open", "closed", "working")   # anything else (cancelled, ...) never traded


# ---------------------------------------------------------------------------
# Pure core: settings and ledger hygiene
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    """A finite number from an int/float/numeric string, else None (bools are not numbers)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        v = float(str(value).replace(",", "").strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def clean_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Settings with the numbers this skill needs coerced; a bad hand edit is a StateError."""
    s = dict(raw)
    for k in NUMERIC_SETTINGS + INT_SETTINGS:
        v = _num(s.get(k))
        if v is None or v < 0:
            raise tc.StateError(f"settings.json has a bad value for {k}: {s.get(k)!r} - fix it with "
                                "budget.py set / risk.py config, or delete the file")
        s[k] = int(v) if k in INT_SETTINGS else v
    return s


def standard_trade_risk(settings: Dict[str, Any]) -> float:
    """Dollars one full-size trade risks: account x risk per trade %."""
    return round(settings["account_size"] * settings["risk_per_trade_pct"] / 100.0, 2)


def et_stamp(value: Any) -> Any:
    """Normalise an ISO stamp to US/Eastern so its first 10 chars are the ET trading date.

    Naive stamps are already ET wall-clock (the ledger convention). 'Z'/UTC stamps are converted
    (Python 3.9's fromisoformat does not accept 'Z'). Unparseable values are returned unchanged.
    """
    if not isinstance(value, str) or not value.strip():
        return value
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        return tc.parse_et(s).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return value


def _day(stamp: Any) -> str:
    return str(stamp)[:10] if stamp else ""


def _is_date(text: str) -> bool:
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def dated_stamp(value: Any) -> Optional[str]:
    """ET-normalised stamp whose first 10 chars are a real date; None if absent or undatable.

    A stamp Python cannot parse (e.g. 7-digit fractions on 3.9) still dates by its ISO prefix.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    v = et_stamp(value)
    return v if isinstance(v, str) and _is_date(v[:10]) else None


def _ledger_stamp(row: Dict[str, Any], label: str, field: str) -> Optional[str]:
    """`dated_stamp` of row[field]; a present but undatable stamp is a StateError (fail closed:
    a loss or trade the budget cannot place on a day would silently drop out of today)."""
    value = row.get(field)
    v = dated_stamp(value)
    if v is None and not (value is None or (isinstance(value, str) and not value.strip())):
        raise tc.StateError(f"ledger.json: {label} has an unreadable {field} ({value!r}) - the budget cannot "
                            "tell which day it counts on; fix it (ISO time like 2026-10-30T10:05:00-04:00) "
                            "or remove it")
    return v


def _label(row: Dict[str, Any], kind: str, i: int) -> str:
    return str(row.get("id") or f"{kind}[{i}]")


def _need(row: Dict[str, Any], label: str, field: str, value: Any, positive: bool = True) -> float:
    v = _num(value)
    if v is None or (positive and v <= 0):
        raise tc.StateError(f"ledger.json: {label} ({row.get('status')}) has no valid {field} "
                            f"({value!r}) - the budget cannot measure its risk; fix or remove it")
    return v


def _order_entry(row: Dict[str, Any]) -> Any:
    """Entry of an order that may not be filled yet (limit price)."""
    for k in ("entry_price", "limit_price", "limit", "entry"):
        if _num(row.get(k)) is not None:
            return row.get(k)
    return None


def normalize_ledger(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A validated copy of the ledger the budget can trust.

    * trades keep only open / closed / working (a cancelled order never traded);
    * open, closed and working trades must have numeric qty, entry and stop (else StateError -
      the guard-rail fails closed rather than under-count risk);
    * closed trades need closed_at, and a pnl (computed from exit_price when missing);
    * opened_at / closed_at are normalised to ET.
    Working orders (unfilled limit orders, as a trade or ticket with status "working") and live
    pending tickets are listed separately.
    """
    if not isinstance(raw, dict):
        raise tc.StateError("ledger.json must be a JSON object with 'tickets' and 'trades' lists")
    trades_in, tickets_in = raw.get("trades") or [], raw.get("tickets") or []
    if not isinstance(trades_in, list) or not isinstance(tickets_in, list):
        raise tc.StateError("ledger.json: 'trades' and 'tickets' must be lists")
    trades: List[Dict[str, Any]] = []
    for i, t in enumerate(trades_in):
        if not isinstance(t, dict):
            raise tc.StateError(f"ledger.json: trades[{i}] is not an object - fix or remove it")
        status = str(t.get("status") or "").strip().lower()
        if status not in TRADE_STATUSES:
            continue
        label = _label(t, "trades", i)
        c = dict(t)
        c["status"] = status
        c["side"] = str(t.get("side") or "").strip().lower()
        if c["side"] not in ("long", "short"):
            raise tc.StateError(f"ledger.json: {label} has side {t.get('side')!r} (expected long/short)")
        c["qty"] = _need(t, label, "qty", t.get("qty"))
        entry_raw = _order_entry(t) if status == "working" else t.get("entry_price")
        c["entry_price"] = _need(t, label, "entry_price", entry_raw)
        c["stop"] = _need(t, label, "stop", t.get("stop"))
        c["opened_at"] = _ledger_stamp(t, label, "opened_at")
        if status == "closed":
            c["closed_at"] = _ledger_stamp(t, label, "closed_at")
            if not c["closed_at"]:
                raise tc.StateError(f"ledger.json: {label} is closed but has no closed_at - fix or remove it")
            pnl = _num(t.get("pnl"))
            if pnl is None:
                exit_price = _num(t.get("exit_price"))
                if exit_price is None or exit_price <= 0:
                    raise tc.StateError(f"ledger.json: {label} is closed but has no pnl or exit_price")
                pnl = round(tc.trade_pnl(c, exit_price), 2)
            c["pnl"] = pnl
            r_mult = _num(t.get("r_multiple"))
            if r_mult is None:   # P&L in units of the trade's initial risk
                risk = tc.trade_initial_risk(c)
                r_mult = round(pnl / risk, 2) if risk > 0 else None
            c["r_multiple"] = r_mult
        trades.append(c)
    by_id = {t.get("id"): t for t in trades if t.get("id")}
    linked = {t.get("ticket_id") for t in trades if t.get("ticket_id")}
    working = [t for t in trades if t["status"] == "working"]
    pending: List[Dict[str, Any]] = []
    for i, k in enumerate(tickets_in):
        if not isinstance(k, dict):
            raise tc.StateError(f"ledger.json: tickets[{i}] is not an object - fix or remove it")
        status = str(k.get("status") or "").strip().lower()
        if status == "working":
            if k.get("id") in linked or (k.get("trade_id") and k.get("trade_id") in by_id):
                continue   # already represented by its trade
            label = _label(k, "tickets", i)
            c = dict(k)
            c["status"], c["_ticket"] = "working", True
            c["side"] = str(k.get("side") or "").strip().lower()
            c["qty"] = _need(k, label, "qty", k.get("qty"))
            c["entry_price"] = _need(k, label, "entry/limit price", _order_entry(k))
            c["stop"] = _need(k, label, "stop", k.get("stop"))
            # A trade on the day it was confirmed (as the broker counts it); unknown -> today.
            c["opened_at"] = dated_stamp(k.get("confirmed_at"))
            working.append(c)
        elif status == "pending":
            c = dict(k)
            q, e, s = _num(k.get("qty")), _num(k.get("entry")), _num(k.get("stop"))
            c["risk"] = round(abs(e - s) * q, 2) if None not in (q, e, s) else None
            pending.append(c)
    return {"trades": trades, "working": working, "pending": pending}


def live_pending(pending: Sequence[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """Pending tickets that have not expired yet."""
    out = []
    for k in pending:
        exp = et_stamp(k.get("expires_at"))
        try:
            alive = not exp or tc.parse_et(str(exp)) > now
        except (ValueError, OverflowError):
            alive = True
        if alive:
            out.append(k)
    return out


def order_risk(row: Dict[str, Any]) -> float:
    return round(abs(float(row["entry_price"]) - float(row["stop"])) * float(row["qty"]), 2)


def budget_view(norm: Dict[str, Any], day: str, today: str, now_iso: str) -> Dict[str, Any]:
    """The ledger exactly as the budget counts it on `day` (a tc ledger: tickets + trades).

    Today: working orders count as open trades opened today - they can fill without another
    check, so their risk is already committed. A past day: the ledger as it stood at that day's
    close (later trades dropped; trades closed after it are open again; no working orders).
    """
    trades: List[Dict[str, Any]] = []
    for t in norm["trades"]:
        opened, closed = _day(t.get("opened_at")), _day(t.get("closed_at"))
        if day >= today:
            c = dict(t)
            if t["status"] == "working":
                c["status"], c["_working"] = "open", True
                c["opened_at"] = t.get("opened_at") or now_iso
            trades.append(c)
            continue
        if t["status"] == "working":
            continue
        if opened > day or (not opened and (t["status"] != "closed" or closed > day)):
            continue
        c = dict(t)
        if t["status"] == "closed" and closed > day:
            c["status"] = "open"
            for k in ("pnl", "closed_at", "exit_price", "exit_reason", "r_multiple"):
                c.pop(k, None)
        trades.append(c)
    if day >= today:
        for w in norm["working"]:
            if w.get("_ticket"):
                c = dict(w)
                c.update(status="open", _working=True, opened_at=w.get("opened_at") or now_iso,
                         id=w.get("trade_id") or w.get("id"))
                trades.append(c)
    return {"tickets": [], "trades": trades}


# ---------------------------------------------------------------------------
# Pure core: the guard-rails
# ---------------------------------------------------------------------------

def cents_floor(x: float) -> float:
    """Round down to the cent (with a hair of float tolerance): the most risk that surely fits."""
    return math.floor(x * 100.0 + 1e-6) / 100.0


def _limit_reasons(bc: Dict[str, Any]) -> List[Dict[str, str]]:
    """Limits already hit (each blocks any new risk). scope: 'today' = done for the day,
    'until_close' = no room until an open position closes.

    Limits are compared at whole cents (a $99.999 limit leaves no cent of room at $99.99 used),
    so "not stopped" always means at least $0.01 of checkable room.
    """
    s, lim = bc["summary"], bc["limits"]
    out: List[Dict[str, str]] = []
    max_loss, max_open = float(lim["daily_max_loss"]), float(lim["max_open_risk"])
    max_loss_c, max_open_c = cents_floor(max_loss), cents_floor(max_open)
    realized_loss = max(0.0, -float(s["realized_pnl"]))
    used, open_risk = float(bc["loss_budget_used"]), float(s["open_risk"])
    if max_loss_c <= 0:
        out.append({"code": "daily_max_loss", "scope": "today",
                    "text": "the daily max loss is set to $0 - no new risk is allowed "
                            "(budget.py set --daily-max-loss X)"})
    elif realized_loss >= max_loss_c - EPS:
        out.append({"code": "daily_max_loss", "scope": "today",
                    "text": f"daily max loss reached: realized {tc.fmt_money(-realized_loss)} vs the "
                            f"{tc.fmt_money(max_loss)} limit - done for today"})
    elif used >= max_loss_c - EPS:
        out.append({"code": "loss_budget_full", "scope": "until_close",
                    "text": f"loss budget full: {tc.fmt_money(realized_loss)} realized loss + "
                            f"{tc.fmt_money(open_risk)} open risk = {tc.fmt_money(used)} of "
                            f"{tc.fmt_money(max_loss)} - no new trades until a position closes"})
    if max_open_c <= 0:
        out.append({"code": "max_open_risk", "scope": "today",
                    "text": "max open risk is set to $0 - no new positions are allowed "
                            "(budget.py set --max-open-risk X)"})
    elif open_risk >= max_open_c - EPS:
        out.append({"code": "max_open_risk", "scope": "until_close",
                    "text": f"open risk {tc.fmt_money(open_risk)} is at the {tc.fmt_money(max_open)} "
                            "max - no new positions until one closes"})
    n, max_n = int(s["trades_today"]), int(lim["max_trades_per_day"])
    if n >= max_n:
        out.append({"code": "max_trades", "scope": "today",
                    "text": (f"max trades reached: {n} of {max_n} today - done for today" if max_n
                             else "max trades per day is set to 0 - no trades are allowed")})
    streak, max_l = int(s["loss_streak"]), int(lim["max_consecutive_losses"])
    if streak >= max_l:
        out.append({"code": "tilt_guard", "scope": "today",
                    "text": (f"tilt guard: {streak} losses in a row - stop for today" if max_l
                             else "the tilt guard is set to 0 losses - no trades are allowed")})
    return out


def room_for_new_risk(bc: Dict[str, Any], reasons: Optional[Sequence[Dict[str, str]]] = None) -> float:
    """Most dollars of new risk a trade could take right now without breaking a limit."""
    if reasons is None:
        reasons = _limit_reasons(bc)
    if reasons:
        return 0.0
    s, lim = bc["summary"], bc["limits"]
    # From the raw limits, not budget_check's rounded loss_budget_left (which can round UP with a
    # sub-cent limit), floored to the cent so that checking exactly this amount always passes.
    room = min(float(lim["daily_max_loss"]) - float(bc["loss_budget_used"]),
               float(lim["max_open_risk"]) - float(s["open_risk"]))
    return max(0.0, cents_floor(room))


def assess(view: Dict[str, Any], settings: Dict[str, Any], day: str) -> Dict[str, Any]:
    """Today's (or `day`'s) budget status: OPEN FOR TRADING / CAUTION / STOP TRADING."""
    bc = tc.budget_check(0.0, settings=settings, ledger=view, day=day)
    s, lim = bc["summary"], bc["limits"]
    reasons = _limit_reasons(bc)
    room = room_for_new_risk(bc, reasons)
    std = standard_trade_risk(settings)
    max_loss = float(lim["daily_max_loss"])
    used = float(bc["loss_budget_used"])
    raw_pct = used / max_loss * 100.0 if max_loss > 0 else 100.0
    # Truncated, never rounded up: 74.96% must not read as "75%" next to OPEN FOR TRADING.
    used_pct = math.floor(raw_pct * 10.0 + 1e-6) / 10.0
    cautions: List[str] = []
    if not reasons:
        if raw_pct >= CAUTION_PCT - EPS:     # the unrounded share: 74.96% is not yet 75%
            cautions.append(f"{math.floor(raw_pct + 1e-6):.0f}% of today's loss budget is used "
                            f"({tc.fmt_money(used)} of {tc.fmt_money(max_loss)})")
        max_n, n = int(lim["max_trades_per_day"]), int(s["trades_today"])
        if max_n >= 2 and n == max_n - 1:
            cautions.append(f"1 trade left today ({n} of {max_n} used)")
        max_l, streak = int(lim["max_consecutive_losses"]), int(s["loss_streak"])
        if max_l >= 2 and streak == max_l - 1:
            cautions.append(f"{streak} loss{'es' if streak != 1 else ''} in a row - one more trips the "
                            "tilt guard")
        if std > 0 and room < std - EPS:
            cautions.append(f"room for new risk is {tc.fmt_money(room)}, less than a standard trade "
                            f"({tc.fmt_money(std)}) - size down or sit out")
    working = [t for t in view["trades"] if t.get("_working")]
    positions = [t for t in view["trades"] if t.get("status") == "open" and not t.get("_working")]
    working_risk = round(sum(order_risk(t) for t in working), 2)
    return {
        "date": day,
        "status": STOP if reasons else (CAUTION if cautions else OPEN),
        "limits_hit": reasons,
        "cautions": cautions,
        "loss_budget": {"limit": max_loss, "used": round(used, 2),
                        "left": max(0.0, cents_floor(max_loss - used)), "used_pct": used_pct,
                        "realized_loss": round(max(0.0, -float(s["realized_pnl"])), 2),
                        "open_risk": s["open_risk"]},
        "realized_pnl": s["realized_pnl"],
        "closed_today": s["closed_today"],
        "wins": s["wins"],
        "losses": s["losses"],
        "trades_today": s["trades_today"],
        "max_trades_per_day": int(lim["max_trades_per_day"]),
        "risked_today": s["risked_today"],
        "loss_streak": s["loss_streak"],
        "max_consecutive_losses": int(lim["max_consecutive_losses"]),
        "open_risk": {"total": s["open_risk"], "positions": round(s["open_risk"] - working_risk, 2),
                      "working_orders": working_risk, "limit": float(lim["max_open_risk"])},
        "room_for_new_risk": room,
        "standard_trade_risk": std,
        "positions": [position_row(t) for t in positions],
        "working_orders": [position_row(t) for t in working],
        "limits": lim,
    }


def position_row(t: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": t.get("id"), "symbol": str(t.get("symbol") or "?").upper(), "side": t["side"],
            "qty": t["qty"], "entry": t["entry_price"], "stop": t["stop"],
            "target": _num(t.get("target")), "risk": order_risk(t), "opened_at": t.get("opened_at"),
            "setup": t.get("setup")}


def check_new_risk(view: Dict[str, Any], settings: Dict[str, Any], day: str, new_risk: float,
                   per_share: Optional[float] = None) -> Dict[str, Any]:
    """Would a new trade risking `new_risk` dollars break a daily limit? Same rules as the broker
    (tc.budget_check), with working orders counted as committed risk."""
    if new_risk is None or not math.isfinite(new_risk) or new_risk <= 0:
        raise ValueError("the new trade's risk must be a positive dollar amount")
    bc = tc.budget_check(new_risk, settings=settings, ledger=view, day=day)
    base = assess(view, settings, day)
    s, lim = bc["summary"], bc["limits"]
    used_after = round(float(bc["loss_budget_used"]) + new_risk, 2)
    after = {"loss_budget_used": used_after,
             "loss_budget_left": max(0.0, cents_floor(float(lim["daily_max_loss"]) - used_after)),
             "open_risk": round(float(s["open_risk"]) + new_risk, 2),
             "trade_number": int(s["trades_today"]) + 1}
    warnings: List[str] = []
    if bc["ok"]:
        max_n = int(lim["max_trades_per_day"])
        if max_n >= 2 and after["trade_number"] == max_n:
            warnings.append(f"this would be your last trade today ({max_n} of {max_n})")
        max_l = int(lim["max_consecutive_losses"])
        if max_l >= 1 and int(s["loss_streak"]) == max_l - 1:
            warnings.append("a loss on this trade trips the tilt guard - you would be done for today")
        std = standard_trade_risk(settings)
        if after["loss_budget_left"] < std - EPS:
            warnings.append(f"after this trade only {tc.fmt_money(after['loss_budget_left'])} of loss "
                            f"budget is left (a standard trade risks {tc.fmt_money(std)})")
        if new_risk > std * 1.5 + EPS and std > 0:
            warnings.append(f"this trade risks {tc.fmt_money(new_risk)}, more than your standard "
                            f"{tc.fmt_money(std)} per trade")
    room = base["room_for_new_risk"]
    out = {
        "ok": bool(bc["ok"]),
        "verdict": "OK" if bc["ok"] else "BLOCKED",
        "new_risk": round(new_risk, 4),
        "reasons": list(bc["reasons"]),
        "warnings": warnings,
        "max_new_risk_now": room,
        "max_shares_now": int(math.floor(room / per_share + 1e-9)) if per_share else None,
        "after": after,
        "before": {k: base[k] for k in ("status", "loss_budget", "open_risk", "trades_today",
                                        "loss_streak", "limits_hit")},
        "working_order_risk": base["open_risk"]["working_orders"],
        "limits": lim,
    }
    return out


def mark_positions(positions: Sequence[Dict[str, Any]], prices: Dict[str, float]) -> List[Dict[str, Any]]:
    """Unrealized P&L of open positions at `prices`, flagging any already through stop/target."""
    out = []
    for p in positions:
        row = dict(p)
        last = prices.get(p["symbol"])
        row["last"] = last
        if last is None:
            row.update(unrealized=None, r_now=None, note="no price")
            out.append(row)
            continue
        trade = {"qty": p["qty"], "entry_price": p["entry"], "side": p["side"]}
        pnl = round(tc.trade_pnl(trade, last), 2)
        row["unrealized"] = pnl
        row["r_now"] = round(pnl / p["risk"], 2) if p["risk"] > 0 else None
        long_ = p["side"] == "long"
        tgt = p.get("target")
        if (last <= p["stop"]) if long_ else (last >= p["stop"]):
            row["note"] = "THROUGH STOP - run broker sync"
        elif tgt is not None and ((last >= tgt) if long_ else (last <= tgt)):
            row["note"] = "past target - run broker sync"
        else:
            row["note"] = ""
        out.append(row)
    return out


def limit_notes(settings: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """(warnings, notes) about how the limits fit together and with the sizing settings."""
    warn: List[str] = []
    notes: List[str] = []
    s = settings
    std = standard_trade_risk(s)
    acct = s["account_size"]
    if acct > 0 and s["daily_max_loss"] > acct * BIG_DAILY_LOSS_PCT / 100.0 + EPS:
        warn.append(f"a daily max loss of {tc.fmt_money(s['daily_max_loss'])} is "
                    f"{s['daily_max_loss'] / acct * 100:.1f}% of the {tc.fmt_money(acct)} account - "
                    "a few bad days in a row would do real damage")
    if std > 0:
        if std > s["daily_max_loss"] + EPS:
            warn.append(f"one standard trade risks {tc.fmt_money(std)} (account x risk %), more than the "
                        f"daily max loss - every full-size ticket will be blocked")
        elif std > s["max_open_risk"] + EPS:
            warn.append(f"one standard trade risks {tc.fmt_money(std)}, more than the max open risk - "
                        "every full-size ticket will be blocked")
        else:
            n = int(math.floor(s["daily_max_loss"] / std + 1e-9))
            notes.append(f"the daily max loss covers {n} full-size losing trade{'s' if n != 1 else ''} "
                         f"at {tc.fmt_money(std)} each")
    if s["max_open_risk"] > s["daily_max_loss"] + EPS:
        notes.append("max open risk is above the daily max loss, so the daily max loss binds first "
                     "(open risk counts against it)")
    return warn, notes


def loosened(current: Dict[str, Any], updates: Dict[str, Any]) -> List[str]:
    """Settings keys whose new value is looser (higher) than the current one."""
    return [k for k, v in updates.items() if k in LIMITS and float(v) > float(current[k]) + EPS]


def max_loss_streak(closed: Sequence[Dict[str, Any]]) -> int:
    best = run = 0
    for t in sorted(closed, key=lambda x: str(x.get("closed_at") or "")):
        run = run + 1 if float(t.get("pnl") or 0) < 0 else 0
        best = max(best, run)
    return best


def realized_low(closed: Sequence[Dict[str, Any]]) -> float:
    """Lowest point of the day's running realized P&L (0 if it never went negative). A day that hit
    -$160 and then made back $50 still hit a $150 max loss."""
    run = low = 0.0
    for t in sorted(closed, key=lambda x: str(x.get("closed_at") or "")):
        run += float(t.get("pnl") or 0)
        low = min(low, run)
    return round(low, 2)


def history(norm: Dict[str, Any], settings: Dict[str, Any], end_day: str, days: int) -> Dict[str, Any]:
    """Per-day realized P&L, trades and wins/losses for the `days` calendar days ending `end_day`."""
    end = datetime.strptime(end_day, "%Y-%m-%d")
    start = (end - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    led = {"tickets": [], "trades": [t for t in norm["trades"] if t["status"] in ("open", "closed")]}
    active = sorted({d for t in led["trades"] for d in (_day(t.get("opened_at")), _day(t.get("closed_at")))
                     if d and start <= d <= end_day})
    max_loss = float(settings["daily_max_loss"])
    rows = []
    for d in active:
        s = tc.day_summary(led, d)
        closed = [t for t in led["trades"] if t["status"] == "closed" and _day(t.get("closed_at")) == d]
        rs = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
        streak = max_loss_streak(closed)
        low = realized_low(closed)
        flags = []
        if cents_floor(max_loss) > 0 and low <= -cents_floor(max_loss) + EPS:   # at any point, not just the close
            flags.append("MAX LOSS")
        if s["trades_today"] > settings["max_trades_per_day"]:
            flags.append("OVER MAX TRADES")
        if settings["max_consecutive_losses"] > 0 and streak >= settings["max_consecutive_losses"]:
            flags.append("TILT")
        still_open = sum(1 for t in led["trades"] if _day(t.get("opened_at")) == d and t["status"] == "open")
        rows.append({
            "date": d, "weekday": datetime.strptime(d, "%Y-%m-%d").strftime("%a"),
            "trades": s["trades_today"], "closed": s["closed_today"], "wins": s["wins"],
            "losses": s["losses"], "scratches": s["closed_today"] - s["wins"] - s["losses"],
            "realized_pnl": s["realized_pnl"], "realized_low": low,
            "r_total": round(sum(rs), 2) if rs else None,
            "risked": s["risked_today"], "max_loss_streak": streak, "still_open": still_open,
            "flags": flags,
        })
    closed_n = sum(r["closed"] for r in rows)
    wins = sum(r["wins"] for r in rows)
    traded = [r for r in rows if r["closed"]]
    totals = {
        "days_with_activity": len(rows),
        "trades": sum(r["trades"] for r in rows),
        "closed": closed_n, "wins": wins, "losses": sum(r["losses"] for r in rows),
        "win_rate_pct": round(wins / closed_n * 100.0, 1) if closed_n else None,
        "realized_pnl": round(sum(r["realized_pnl"] for r in rows), 2),
        "green_days": sum(1 for r in traded if r["realized_pnl"] > 0),
        "red_days": sum(1 for r in traded if r["realized_pnl"] < 0),
        "best_day": max(traded, key=lambda r: r["realized_pnl"])["date"] if traded else None,
        "worst_day": min(traded, key=lambda r: r["realized_pnl"])["date"] if traded else None,
        "limit_days": sum(1 for r in rows if r["flags"]),
    }
    return {"start": start, "end": end_day, "days": days, "rows": rows, "totals": totals,
            "limits": {k: settings[k] for k in LIMITS}}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def meter(pct: float, width: int = BAR_WIDTH) -> str:
    """'[#######.............]' for a 0-100+ percentage (capped at full)."""
    filled = int(round(max(0.0, min(100.0, pct)) / 100.0 * width))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _qty(q: Any) -> str:
    return f"{q:g}" if isinstance(q, (int, float)) else str(q)


def render_status(r: Dict[str, Any]) -> str:
    lb, orisk = r["loss_budget"], r["open_risk"]
    live = r["view"] == "live"
    when = (f"{r['weekday']} {r['date']} · {r['time']} ET ({'market open' if r['market_open'] else 'market closed'})"
            if live else f"{r['weekday']} {r['date']} · as of that day's close")
    if live:
        headline = f"STATUS: {r['status']}"
    else:
        headline = "AT THE CLOSE: " + {STOP: "STOP TRADING (limits hit)", CAUTION: "CAUTION"}.get(
            r["status"], "within limits")
    lines = [f"RISKED SO FAR · {when}", headline]
    for x in r["limits_hit"]:
        lines.append(f"  x {x['text']}")
    for c in r["cautions"]:
        lines.append(f"  ! {c}")
    lines.append("")
    lines.append(f"Loss budget    {meter(lb['used_pct'])}  {tc.fmt_money(lb['used'])} of "
                 f"{tc.fmt_money(lb['limit'])} used ({lb['used_pct']:.1f}%) · {tc.fmt_money(lb['left'])} left")
    lines.append(f"  realized P&L  {tc.fmt_money(r['realized_pnl'], signed=True)}  ({r['closed_today']} closed: "
                 f"{r['wins']} W / {r['losses']} L)")
    extra = (f", incl. {tc.fmt_money(orisk['working_orders'])} in working orders"
             if orisk["working_orders"] else "")
    npos = len(r["positions"])
    lines.append(f"  open risk     {tc.fmt_money(orisk['total'])} of {tc.fmt_money(orisk['limit'])} max "
                 f"({npos} position{'s' if npos != 1 else ''}{extra})")
    that = "today" if live else "that day"
    lines.append(f"{'Risked ' + that:<14} {tc.fmt_money(r['risked_today'])} across "
                 f"{_plural(r['trades_today'], 'trade')}")
    lines.append(f"{'Trades ' + that:<14} {r['trades_today']} of {r['max_trades_per_day']}")
    lines.append(f"Loss streak    {r['loss_streak']} in a row (the tilt guard stops you at "
                 f"{r['max_consecutive_losses']})")
    if live:
        lines.append(f"Room for the next trade: {tc.fmt_money(r['room_for_new_risk'])} of risk "
                     f"(a standard trade risks {tc.fmt_money(r['standard_trade_risk'])})")
    marks = {m["id"]: m for m in (r.get("marks") or {}).get("positions", [])}
    if r["positions"]:
        lines.append("")
        lines.append("Open positions" + (" (live marks)" if marks else ""))
        rows = []
        for p in r["positions"]:
            m = marks.get(p["id"], {})
            row = [p["id"] or "-", p["symbol"], p["side"], _qty(p["qty"]), tc.fmt_price(p["entry"]),
                   tc.fmt_price(p["stop"]), tc.fmt_money(p["risk"])]
            if marks:
                row += [tc.fmt_price(m.get("last")), tc.fmt_money(m.get("unrealized"), signed=True),
                        "-" if m.get("r_now") is None else f"{m['r_now']:+.2f}", m.get("note", "")]
            rows.append(row)
        headers = ["id", "symbol", "side", "qty", "entry", "stop", "risk"]
        if marks:
            headers += ["last", "unrealized", "R", "note"]
        lines.append(tc.table(rows, headers))
        if marks:
            tot = r["marks"]["unrealized_total"]
            lines.append(f"Unrealized P&L: {tc.fmt_money(tot, signed=True)} (the budget counts open "
                         "positions at their full stop risk, not at the mark)")
    if r["working_orders"]:
        lines.append("")
        lines.append("Working orders (unfilled limits - counted as committed risk)")
        lines.append(tc.table([[w["id"] or "-", w["symbol"], w["side"], _qty(w["qty"]), tc.fmt_price(w["entry"]),
                                tc.fmt_price(w["stop"]), tc.fmt_money(w["risk"])] for w in r["working_orders"]],
                              ["id", "symbol", "side", "qty", "limit", "stop", "risk"]))
    if r.get("pending_tickets"):
        lines.append("")
        n = len(r["pending_tickets"])
        risk = sum(k["risk"] or 0 for k in r["pending_tickets"])
        lines.append(f"Pending tickets: {n} waiting for a yes ({tc.fmt_money(risk)} of risk) - not counted "
                     "until confirmed; the broker re-checks the budget then")
    for e in (r.get("marks") or {}).get("errors", []):
        lines.append(f"! no live price: {e}")
    lines.append("")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def fine_money(x: float) -> str:
    """$ to the cent, or to 1/100 of a cent when that hides something (sub-penny stocks)."""
    if abs(x - round(x, 2)) < 1e-9:
        return tc.fmt_money(x)
    return f"{'-' if x < 0 else ''}${abs(x):,.4f}"


def render_check(r: Dict[str, Any]) -> str:
    sym = f" · {r['symbol']}" if r.get("symbol") else ""
    lines = [f"BUDGET CHECK{sym} · new risk {fine_money(r['new_risk'])} -> {r['verdict']}"]
    for x in r["reasons"]:
        lines.append(f"  x {x}")
    a, lim = r["after"], r["limits"]
    if r["ok"]:
        lines.append(f"After this trade: worst-case loss today {tc.fmt_money(a['loss_budget_used'])} of "
                     f"{tc.fmt_money(lim['daily_max_loss'])} ({tc.fmt_money(a['loss_budget_left'])} left) · "
                     f"open risk {tc.fmt_money(a['open_risk'])} of {tc.fmt_money(lim['max_open_risk'])} · "
                     f"trade {a['trade_number']} of {lim['max_trades_per_day']}")
    for w in r["warnings"]:
        lines.append(f"  ! {w}")
    if not r["ok"]:
        room = f"Most you can risk on a new trade right now: {tc.fmt_money(r['max_new_risk_now'])}"
        if r.get("max_shares_now") is not None and r.get("risk_per_share"):
            room += f" (= {r['max_shares_now']} shares at {fine_money(r['risk_per_share'])}/share risk)"
        lines.append(room)
    if r["working_order_risk"]:
        lines.append(f"(includes {tc.fmt_money(r['working_order_risk'])} of working orders as committed risk)")
    for n in r.get("notes", []):
        lines.append(f"- {n}")
    if not r["ok"]:
        lines.append("Do not open a ticket.")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def render_limits(r: Dict[str, Any]) -> str:
    s = r["settings"]
    lines = ["BUDGET LIMITS" + (" (saved)" if r["changed"] else "")]
    rows = []
    for k, (flag, kind, label) in LIMITS.items():
        v = s[k]
        val = tc.fmt_money(v) if kind == "money" else str(v)
        old = r["previous"].get(k)
        old_n = _num(old)
        if old is None or (old_n is not None and abs(old_n - float(v)) < EPS):
            chg = ""
        elif old_n is None:
            chg = f"was {old!r} (invalid)"
        else:
            chg = f"was {tc.fmt_money(old_n) if kind == 'money' else int(old_n)}"
        rows.append([label, val, flag, chg])
    lines.append(tc.table(rows, ["limit", "value", "flag", "change"]))
    lines.append(f"Standard trade risk: {tc.fmt_money(r['standard_trade_risk'])} "
                 f"({tc.fmt_money(s['account_size'])} account x {s['risk_per_trade_pct']:g}%; "
                 "change with the risk skill's config)")
    for w in r["warnings"]:
        lines.append(f"! {w}")
    for n in r["notes"]:
        lines.append(f"- {n}")
    if not r["changed"]:
        lines.append("Change with: budget.py set --daily-max-loss X --max-open-risk X --max-trades N --max-losses N")
    return "\n".join(lines)


def render_history(r: Dict[str, Any]) -> str:
    lim = r["limits"]
    lines = [f"BUDGET HISTORY · {r['start']} to {r['end']} ({r['days']} days) · flags vs current limits: "
             f"max loss {tc.fmt_money(lim['daily_max_loss'])}, {lim['max_trades_per_day']} trades, "
             f"tilt at {lim['max_consecutive_losses']}"]
    if not r["rows"]:
        lines.append("No paper trades in this window.")
        return "\n".join(lines)
    rows = [[x["date"], x["weekday"], x["trades"], x["closed"], x["wins"], x["losses"],
             tc.fmt_money(x["realized_pnl"], signed=True),
             "-" if x["r_total"] is None else f"{x['r_total']:+.2f}",
             ", ".join(x["flags"] + ([f"{x['still_open']} still open"] if x["still_open"] else []))]
            for x in r["rows"]]
    lines.append(tc.table(rows, ["date", "day", "trades", "closed", "W", "L", "realized", "R", "flags"]))
    t = r["totals"]
    wr = "-" if t["win_rate_pct"] is None else f"{t['win_rate_pct']:.0f}%"
    lines.append(f"Total: {t['trades']} trades · {t['wins']} W / {t['losses']} L (win rate {wr} of closed) · "
                 f"net {tc.fmt_money(t['realized_pnl'], signed=True)} · {_plural(t['green_days'], 'green day')}, "
                 f"{_plural(t['red_days'], 'red day')} · limits broken on {_plural(t['limit_days'], 'day')}")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _money(value: str) -> float:
    try:
        v = float(value.replace(",", "").replace("$", "").strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a dollar amount, got {value!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive dollar amount, got {value!r}")
    return v


def _price(value: str) -> float:
    try:
        v = float(value.replace(",", "").replace("$", "").strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a price, got {value!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive price, got {value!r}")
    return v


def _count(value: str) -> int:
    try:
        v = int(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
    if v < 1:
        raise argparse.ArgumentTypeError(f"expected a whole number >= 1, got {value!r}")
    return v


def _qty_arg(value: str) -> float:
    try:
        v = float(value.replace(",", "").strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a share quantity, got {value!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive share quantity, got {value!r}")
    return v


def _days(value: str) -> int:
    v = _count(value)
    if v > 3660:
        raise argparse.ArgumentTypeError("--days is at most 3660")
    return v


def _date(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date like 2026-10-29, got {value!r}") from None


def _symbol(value: str) -> str:
    s = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", s):
        raise argparse.ArgumentTypeError(f"not a ticker symbol: {value!r}")
    return s


class _Parser(argparse.ArgumentParser):
    """Bad flags are bad input: exit 1, not argparse's 2 (2 means a data source is down)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    p = tc.make_parser("Daily guard-rails over the paper ledger: max daily loss, max open risk, "
                       "max trades per day and a tilt guard. With no command, shows `status`.")
    p.error = _Parser.error.__get__(p)  # type: ignore[method-assign]
    sub = p.add_subparsers(dest="command", metavar="{status,check,set,history}", parser_class=_Parser)
    st = sub.add_parser("status", help="the RISKED SO FAR card: P&L, open risk, budget left, status")
    st.add_argument("--date", type=_date, help="show how a past day ended (YYYY-MM-DD); default today")
    st.add_argument("--mark", action="store_true",
                    help="also fetch live prices (Yahoo) for open positions' unrealized P&L")
    tc.add_json_flag(st)
    ck = sub.add_parser("check", help="OK / BLOCKED for a new trade's risk (exit 0 OK, 3 BLOCKED)")
    ck.add_argument("--risk", type=_money, help="dollars the new trade risks (|entry - stop| x shares)")
    ck.add_argument("--entry", type=_price, help="entry price (with --stop and --qty instead of --risk)")
    ck.add_argument("--stop", type=_price, help="stop price")
    ck.add_argument("--qty", type=_qty_arg, help="shares")
    ck.add_argument("--symbol", type=_symbol,
                    help="ticker; without --risk, missing levels/shares come from its saved plan")
    tc.add_json_flag(ck)
    se = sub.add_parser("set", help="change the daily limits (no flags = show them)")
    for key, (flag, kind, label) in LIMITS.items():
        se.add_argument(flag, dest=key, type=_money if kind == "money" else _count, help=label)
    se.add_argument("--force", action="store_true",
                    help="allow loosening limits while today's status is STOP TRADING")
    tc.add_json_flag(se)
    hi = sub.add_parser("history", help="per-day realized P&L, trades and wins/losses")
    hi.add_argument("--days", type=_days, default=10, help="calendar days back from today (default 10)")
    tc.add_json_flag(hi)
    return p


def load_settings_raw() -> Dict[str, Any]:
    """tc.load_settings, with a settings.json that is not a JSON object as a clean StateError
    (tc.load_settings raises AttributeError on e.g. a list, which would be a traceback)."""
    try:
        return tc.load_settings()
    except AttributeError:
        raise tc.StateError(f"{tc.state_path('settings.json')} must be a JSON object of settings - fix or "
                            "delete it (defaults come back)") from None


def load_ledger_raw() -> Dict[str, Any]:
    """tc.load_ledger, with a ledger.json that is not a JSON object as a clean StateError."""
    try:
        return tc.load_ledger()
    except AttributeError:
        raise tc.StateError(f"{tc.state_path(tc.LEDGER_FILE)} must be a JSON object with 'tickets' and "
                            "'trades' lists - fix it (the budget will not guess what a broken ledger "
                            "holds)") from None


def _load() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    return clean_settings(load_settings_raw()), normalize_ledger(load_ledger_raw())


def _today_view(settings: Dict[str, Any], norm: Dict[str, Any], day: Optional[str] = None
                ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    now = tc.now_et()
    today = now.strftime("%Y-%m-%d")
    day = day or today
    if day > today:
        raise ValueError(f"{day} is in the future (today is {today} ET)")
    view = budget_view(norm, day, today, now.isoformat(timespec="seconds"))
    return day, view, assess(view, settings, day)


def fetch_marks(positions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    prices: Dict[str, float] = {}
    errors: List[str] = []
    for sym in sorted({p["symbol"] for p in positions}):
        try:
            prices[sym] = float(tc.quote(sym)["price"])
        except tc.FetchError as e:
            errors.append(str(e))
    if positions and not prices:
        raise tc.FetchError(errors[0] if errors else "no prices")
    rows = mark_positions(positions, prices)
    tot = round(sum(m["unrealized"] for m in rows if m.get("unrealized") is not None), 2)
    return {"positions": rows, "unrealized_total": tot, "errors": errors, "prices": prices}


def cmd_status(args: argparse.Namespace) -> int:
    settings, norm = _load()
    now = tc.now_et()
    day, view, r = _today_view(settings, norm, args.date)
    live = day == now.strftime("%Y-%m-%d")
    r.update(view="live" if live else "end_of_day", as_of=now.isoformat(timespec="seconds"),
             time=now.strftime("%H:%M"), market_open=tc.is_market_open(now) if live else False,
             weekday=datetime.strptime(day, "%Y-%m-%d").strftime("%a"),
             pending_tickets=[{"id": k.get("id"), "symbol": k.get("symbol"), "side": k.get("side"),
                               "qty": k.get("qty"), "risk": k.get("risk"), "expires_at": k.get("expires_at")}
                              for k in live_pending(norm["pending"], now)] if live else [],
             disclaimer=tc.DISCLAIMER)
    if not live:
        r["room_for_new_risk"] = None
    if args.mark and live:
        r["marks"] = fetch_marks(r["positions"])
        for m in r["marks"]["positions"]:
            if m.get("note", "").startswith("THROUGH STOP"):
                r["cautions"].append(f"{m['symbol']} ({m['id']}) is trading through its stop at "
                                     f"{tc.fmt_price(m['last'])} - the loss may exceed its "
                                     f"{tc.fmt_money(m['risk'])} planned risk; run the broker's sync")
        if r["cautions"] and r["status"] == OPEN:
            r["status"] = CAUTION
    elif args.mark:
        raise ValueError("--mark only works for today (live prices say nothing about a past close)")
    tc.emit(r, args.json, render_status)
    return EXIT_OK


def resolve_risk(args: argparse.Namespace) -> Dict[str, Any]:
    """New-trade risk from --risk, or |entry - stop| x qty from flags and/or the saved plan."""
    notes: List[str] = []
    if args.risk is not None:
        given = [f for f in ("entry", "stop", "qty") if getattr(args, f) is not None]
        if given:
            raise ValueError(f"give either --risk or --entry/--stop/--qty, not both (got --risk and "
                             f"--{', --'.join(given)})")
        return {"new_risk": args.risk, "source": "--risk", "per_share": None, "notes": notes}
    plan = tc.load_plan(args.symbol) if args.symbol else None
    if plan is not None and not isinstance(plan, dict):
        raise tc.StateError(f"the saved {args.symbol} plan is not a JSON object - fix or delete "
                            f"plans/{args.symbol}.json, or pass --risk / --entry --stop --qty")
    if args.symbol and plan is None and None in (args.entry, args.stop, args.qty):
        raise ValueError(f"no saved plan for {args.symbol} - pass --risk DOLLARS or --entry/--stop/--qty")
    plan = plan or {}
    sizing = plan.get("sizing") if isinstance(plan.get("sizing"), dict) else {}
    entry = args.entry if args.entry is not None else _num(plan.get("entry"))
    stop = args.stop if args.stop is not None else _num(plan.get("stop"))
    qty = args.qty if args.qty is not None else _num(sizing.get("shares"))
    missing = [n for n, v in (("--entry", entry), ("--stop", stop), ("--qty", qty)) if v is None]
    if missing:
        where = f" (not in the {args.symbol} plan either)" if args.symbol else ""
        hint = " - size it first with the risk skill (risk.py size --symbol SYM --save)" \
            if args.symbol and "--qty" in missing else ""
        raise ValueError(f"need --risk DOLLARS, or all of --entry, --stop and --qty; missing "
                         f"{', '.join(missing)}{where}{hint}")
    if entry <= 0 or stop <= 0:
        raise ValueError("entry and stop must be positive prices")
    if qty <= 0:
        raise ValueError("the share quantity is 0 - nothing to check (the stop may be too wide for "
                         "the risk budget)")
    per = abs(entry - stop)
    if per <= 0:
        raise ValueError("entry and stop must differ")
    if sizing and args.qty is None:
        se, ss = _num(sizing.get("entry")), _num(sizing.get("stop"))
        if (se is not None and abs(se - entry) > 1e-9) or (ss is not None and abs(ss - stop) > 1e-9):
            notes.append("the plan's sizing was computed for different levels - re-run risk.py size")
    src = []
    for flag, name in (("entry", "entry"), ("stop", "stop"), ("qty", "shares")):
        src.append(f"{name} from {'flag' if getattr(args, flag) is not None else 'plan'}")
    # Unrounded, as the broker computes it: rounding $63.1225 down to $63.12 could pass a trade
    # the broker's own budget check then refuses (sub-penny prices). Rounded only for display.
    return {"new_risk": per * qty, "source": ", ".join(src), "per_share": per,
            "entry": entry, "stop": stop, "qty": qty, "notes": notes}


def cmd_check(args: argparse.Namespace) -> int:
    settings, norm = _load()
    risk = resolve_risk(args)
    day, view, _ = _today_view(settings, norm)
    r = check_new_risk(view, settings, day, risk["new_risk"], risk["per_share"])
    per = risk["per_share"]
    r.update(symbol=args.symbol, date=day, as_of=tc.iso_now(), risk_source=risk["source"],
             risk_per_share=None if per is None else round(per, 4), notes=risk["notes"],
             exit_code=EXIT_OK if r["ok"] else EXIT_BLOCKED,
             disclaimer=tc.DISCLAIMER)
    for k in ("entry", "stop", "qty"):
        if k in risk:
            r[k] = risk[k]
    if args.symbol:
        same = [t for t in view["trades"] if t.get("status") == "open"
                and str(t.get("symbol") or "").upper() == args.symbol]
        if same:
            ids = ", ".join(str(t.get("id") or "?") for t in same)
            r["notes"].append(f"{args.symbol} already has an open paper position or working order ({ids}); "
                              "it is already counted - if this check is for that same trade, it is "
                              "double-counted here")
    tc.emit(r, args.json, render_check)
    return EXIT_OK if r["ok"] else EXIT_BLOCKED


def cmd_set(args: argparse.Namespace) -> int:
    raw = load_settings_raw()
    updates = {k: getattr(args, k) for k in LIMITS if getattr(args, k, None) is not None}
    previous = {k: raw[k] for k in LIMITS}
    forced: List[str] = []
    if updates:
        # A limit whose stored value is unreadable or 0 (which `set` itself never writes; a 0 limit
        # is STOP TRADING whose reason says to fix it here) is being repaired, not loosened.
        current: Dict[str, float] = {}
        for k, (_, kind, _) in LIMITS.items():
            v = _num(raw.get(k))
            if v is not None and (int(v) if kind == "count" else cents_floor(v)) > 0:
                current[k] = v
        looser = loosened(current, {k: v for k, v in updates.items() if k in current})
        if looser:
            settings, norm = _load()
            _, _, today = _today_view(settings, norm)
            if today["status"] == STOP:
                why = "; ".join(x["text"] for x in today["limits_hit"])
                names = ", ".join(LIMITS[k][2] for k in looser)
                if not args.force:
                    raise ValueError(f"refusing to loosen {names} while today's status is STOP TRADING "
                                     f"({why}). Limits exist for exactly this moment - tightening is "
                                     "always allowed. If you really mean it, rerun with --force.")
                forced.append(f"--force: loosened {names} while STOP TRADING was active ({why})")
        settings = clean_settings(tc.save_settings(updates))
    else:
        settings = clean_settings(raw)
    warn, notes = limit_notes(settings)
    warn = forced + warn
    r = {"changed": bool(updates), "forced": bool(forced), "updated": updates, "previous": previous if updates else {},
         "settings": {k: settings[k] for k in list(LIMITS) + ["account_size", "risk_per_trade_pct"]},
         "standard_trade_risk": standard_trade_risk(settings), "warnings": warn, "notes": notes,
         "settings_file": str(tc.state_path("settings.json"))}
    tc.emit(r, args.json, render_limits)
    return EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    settings, norm = _load()
    r = history(norm, settings, tc.today_et(), args.days)
    r["disclaimer"] = tc.DISCLAIMER
    tc.emit(r, args.json, render_history)
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        args = build_parser().parse_args(["status"] + (["--json"] if args.json else []))
    return {"status": cmd_status, "check": cmd_check, "set": cmd_set,
            "history": cmd_history}[args.command](args)


if __name__ == "__main__":
    tc.run(main)
