#!/usr/bin/env python3
"""Risk: position sizing and pre-trade risk checks for a paper day trade.

Sizes the trade so that hitting the stop loses at most your risk budget (default 0.2% of the
account), caps it by the max position size and buying power, and checks it before you ask the
broker for a ticket: reward:risk, a stop that is inside the noise, 0 shares, margin / buying
power, today's loss budget (from the paper ledger), and a pattern-day-trader heads-up.

Crypto (BTC-USDT, ETH-USDT, BTC-USD, ... - Binance prices by default, or Coinbase with the
crypto_exchange setting; 24/7, the UTC day) is sized in fractions, floored to the pair's size step,
and the paper fees of the entry and the stop exit (crypto_fee_pct; unset = the exchange's
entry-tier taker fee) count toward the loss: "BUY 0.02274 BTC-USDT". The card says what the fees
cost in R. Spot crypto has no margin and no PDT rule: leverage stays 1x unless you pass --leverage.

Usage:
  python3 scripts/risk.py size --entry 364.18 --stop 358.32                  # BUY 8 shares
  python3 scripts/risk.py size --symbol TSLA                                 # levels from plans/TSLA.json
  python3 scripts/risk.py size --symbol TSLA --entry 364.18 --stop 358.32 --target 370.04 --save
  python3 scripts/risk.py size --symbol TSLA --stop 358.32                   # entry = current price
  python3 scripts/risk.py size --entry 364.18 --stop 370.04 --risk-dollars 100   # stop above = short
  python3 scripts/risk.py size --entry 50 --stop 49.9 --max-position-pct 200 --leverage 4
  python3 scripts/risk.py config --account 30000 --risk-pct 0.25 --max-position-pct 25 --min-rr 1.5
  python3 scripts/risk.py config                                             # show settings
  python3 scripts/risk.py show                                               # settings + open risk today
  python3 scripts/risk.py --json size --symbol TSLA                          # --json works anywhere
  python3 scripts/risk.py size --symbol BTC-USDT --entry 100000 --stop 98000 --target 104000
  python3 scripts/risk.py config --crypto-exchange coinbase                  # crypto prices: binance | coinbase
  python3 scripts/risk.py config --crypto-fee-pct 0.075                      # your own fee tier

Exit codes: 0 ok, 1 bad input or state (including `size --save` that could not save),
2 data source unreachable (only `size` without an entry anywhere fetches the current price).
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trader_common as tc  # noqa: E402

import argparse  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Any, Dict, Iterable, List, Optional, Sequence  # noqa: E402

PDT_MIN_EQUITY = 25000.0      # FINRA pattern-day-trader minimum equity (as of this writing)
PDT_NOTE = ("pattern-day-trader rules may limit day trades depending on your broker and current "
            "FINRA rules")
NOISE_STOP_PCT = 0.1          # stop closer than this % of price is inside normal noise
BIG_RISK_PCT = 2.0            # risking more than this % of the account on one trade -> warn
TINY_RISK_PCT = 0.01          # below this the user probably typed a fraction, not a percent
MAX_RISK_PCT = 100.0
MAX_POSITION_PCT = 400.0      # 4x intraday buying power is the most any account gets
LEVERAGES = (1, 2, 4)         # cash, Reg T margin, pattern-day-trader intraday margin
EPS = 1e-9
# Plan sections other skills computed for specific levels; stale once the levels change.
DEPENDENT_SECTIONS = ("exit_plan", "time_machine")
NUMERIC_SETTINGS = ("account_size", "risk_per_trade_pct", "max_position_pct", "min_reward_risk",
                    "daily_max_loss", "max_open_risk", "crypto_fee_pct")
MAX_FEE_PCT = 5.0             # a paper fee per fill above this is a typo, not a fee tier
BIG_FEE_R = 1.0               # round-trip fees at or above this many R -> warn
INT_SETTINGS = ("max_trades_per_day", "max_consecutive_losses")
CAP_LABELS = {"risk": "risk budget", "max_position_pct": "max position size",
              "buying_power": "buying power"}


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

def _floor_shares(x: float) -> int:
    """Whole shares; the epsilon keeps float noise (9.99999999) from costing a share."""
    return max(0, int(math.floor(x + EPS)))


def _decimals(step: float) -> int:
    return max(0, -int(math.floor(math.log10(step) + 1e-9)))


def _floor_step(x: float, step: float) -> float:
    """Round down to a multiple of a crypto size step (e.g. 1e-8 BTC)."""
    return max(0.0, round(math.floor(x / step + EPS) * step, _decimals(step)))


def unit_word(symbol: Optional[str]) -> str:
    """'shares' for stocks; the base currency for crypto ('BTC' for BTC-USDT or BTCUSDT), 'units'
    without a symbol."""
    if symbol and tc.is_crypto_symbol(symbol):
        return tc.crypto_pair(symbol)[0]
    return "units" if tc.market().fractional and not symbol else "shares"


# ---------------------------------------------------------------------------
# Crypto exchange (Binance by default, or Coinbase): names, fees, pair rules
# ---------------------------------------------------------------------------

def exchange_name(exchange: Optional[str] = None) -> str:
    """'Binance' / 'Coinbase': `exchange`, else the configured crypto price source."""
    return tc.CRYPTO_EXCHANGES[exchange]["name"] if exchange else tc.exchange_name()


def exchange_fee_pct(exchange: Optional[str] = None) -> float:
    """The exchange's entry-tier taker fee in % (Binance 0.1, Coinbase 0.6): the crypto_fee_pct
    tc.load_settings() uses when the user has not set their own."""
    return float(tc.CRYPTO_EXCHANGES[exchange or tc.crypto_exchange()]["fee_pct"])


def default_settings() -> Dict[str, Any]:
    """tc.DEFAULT_SETTINGS with the crypto fee of the configured exchange (not one fixed number),
    as tc.load_settings() fills it in when no fee is saved."""
    ex = tc.crypto_exchange()
    return dict(tc.DEFAULT_SETTINGS, crypto_exchange=ex, crypto_fee_pct=exchange_fee_pct(ex))


def rules_name(exchange: Optional[str] = None) -> str:
    """What the exchange calls a pair's size step and price tick: 'Binance pair rules' (symbol
    filters) or 'Coinbase product rules'."""
    ex = exchange or tc.crypto_exchange()
    return f"{exchange_name(ex)} {'product' if ex == 'coinbase' else 'pair'} rules"


def quote_currency(symbol: str, exchange: Optional[str] = None) -> str:
    """The currency a pair is priced in on the exchange: BTC-USD is a USDT pair on Binance."""
    quote = tc.crypto_pair(symbol)[1]
    return "USDT" if quote == "USD" and (exchange or tc.crypto_exchange()) == "binance" else quote


def min_order_text(amount: float, symbol: str, exchange: Optional[str] = None) -> str:
    """An exchange's minimum order value in the pair's quote currency: '$1.00' or '5 USDT'."""
    quote = quote_currency(symbol, exchange)
    return tc.fmt_money(amount) if quote == "USD" else f"{amount:g} {quote}"


def usd_pair_note(symbol: str, exchange: Optional[str] = None) -> Optional[str]:
    """Binance's few USD pairs are thin: a -USD symbol is priced from its USDT pair there - say so."""
    if (exchange or tc.crypto_exchange()) != "binance" or tc.crypto_pair(symbol)[1] != "USD":
        return None
    return (f"{symbol} is priced from Binance's {tc.binance_symbol(symbol)} pair (Binance's few USD pairs are thin; "
            "USDT trades close to, not exactly at, $1)")


def size_text(q: Any, fractional: bool) -> str:
    """'8' / '1,200' for shares (as before); '0.02946375' for crypto."""
    if fractional and isinstance(q, (int, float)):
        return tc.fmt_qty(q)
    return f"{q:,}" if isinstance(q, int) else str(q)


def _same(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))


def _num(value: Any) -> Optional[float]:
    """A positive finite price from a plan field, else None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def infer_side(entry: float, stop: float) -> str:
    if stop < entry:
        return "long"
    if stop > entry:
        return "short"
    raise ValueError("entry and stop must differ")


def resolve_levels(plan: Optional[Dict[str, Any]], entry: Optional[float] = None,
                   stop: Optional[float] = None, target: Optional[float] = None,
                   side: Optional[str] = None, last_price: Optional[float] = None) -> Dict[str, Any]:
    """Merge the flags with the saved plan (flags win) and validate the result.

    * entry: --entry, else the plan's entry, else `last_price` (the current quote).
    * stop: --stop, else the plan's stop.
    * side: --side; else the plan's side when any level came from the plan (so a typo cannot
      silently flip a long into a short); else inferred (stop below entry = long).
    * target: --target; else the plan's target only if it was set for this side and entry.
    """
    plan = plan or {}
    p_entry, p_stop, p_target = _num(plan.get("entry")), _num(plan.get("stop")), _num(plan.get("target"))
    p_side = plan.get("side") if plan.get("side") in ("long", "short") else None
    src: Dict[str, str] = {}
    notes: List[str] = []

    if entry is not None:
        src["entry"] = "flag"
    elif p_entry is not None:
        entry, src["entry"] = p_entry, "plan"
    elif last_price is not None:
        entry, src["entry"] = float(last_price), "quote"
    else:
        raise ValueError("no entry price: pass --entry (or --symbol SYM to use its saved plan "
                         "or current price)")
    if stop is not None:
        src["stop"] = "flag"
    elif p_stop is not None:
        stop, src["stop"] = p_stop, "plan"
    else:
        raise ValueError("no stop price: pass --stop (the stop decides the size)")

    from_plan = "plan" in (src["entry"], src["stop"])
    if side:
        src["side"] = "flag"
    elif p_side and from_plan:
        side, src["side"] = p_side, "plan"
    else:
        side, src["side"] = infer_side(entry, stop), "inferred"
        if p_side and p_side != side:
            notes.append(f"side {side.upper()} follows your levels (the saved plan was {p_side})")

    if target is not None:
        src["target"] = "flag"
    elif p_target is not None:
        if side == p_side and _same(entry, p_entry):
            target, src["target"] = p_target, "plan"
        else:
            notes.append(f"the saved target {tc.fmt_price(p_target)} was not used: it was set for a "
                         "different entry or side (pass --target)")

    errs = tc.validate_levels(side, float(entry), float(stop), None if target is None else float(target))
    if errs:
        where = ", ".join(f"{k} {tc.fmt_price(v)} ({_SOURCE_WORDS[src[k]]})"
                          for k, v in (("entry", entry), ("stop", stop), ("target", target))
                          if v is not None)
        raise ValueError(f"{'; '.join(errs)} - {side} with {where}")
    return {"side": side, "entry": float(entry), "stop": float(stop),
            "target": None if target is None else float(target), "sources": src, "notes": notes}


_SOURCE_WORDS = {"flag": "your flag", "plan": "saved plan", "quote": "current price",
                 "inferred": "inferred"}


def levels_changed(plan: Optional[Dict[str, Any]], lv: Dict[str, Any]) -> bool:
    """True when side/entry/stop/target differ from the saved plan's."""
    if not plan:
        return False
    return not (plan.get("side") == lv["side"]
                and _same(_num(plan.get("entry")), lv["entry"])
                and _same(_num(plan.get("stop")), lv["stop"])
                and _same(_num(plan.get("target")), lv["target"]))


def tm_levels_match(tm: Dict[str, Any], lv: Dict[str, Any]) -> bool:
    """True when the time machine's saved odds were computed for exactly these side/levels."""
    return (tm.get("side") in (None, lv["side"]) and _num(tm.get("entry")) is not None
            and _same(_num(tm.get("entry")), lv["entry"]) and _same(_num(tm.get("stop")), lv["stop"])
            and _same(_num(tm.get("target")), lv["target"]))


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


def _price_arg(x: float) -> str:
    return f"{x:.2f}" if abs(x) >= 1 else f"{x:.4f}"


def untested_note(sym: str, lv: Dict[str, Any], in_plan: bool, stale: bool = False) -> str:
    """The note for levels the time machine has not tested (no odds, or odds for other levels).
    `in_plan`: the saved plan holds these levels, so the time machine can read them from it."""
    if lv["target"] is None:
        return "not tested by the time machine for these levels (it needs a target to measure the odds)"
    cmd = (f"time_machine.py {sym} --save" if in_plan else
           f"time_machine.py {sym} --entry {_price_arg(lv['entry'])} --stop {_price_arg(lv['stop'])} "
           f"--target {_price_arg(lv['target'])} --save")
    return (f"not tested by the time machine for these levels (run {cmd})"
            + (" - the plan's odds were for other levels" if stale else ""))


def size_position(side: str, entry: float, stop: float, target: Optional[float] = None,
                  settings: Optional[Dict[str, Any]] = None, account: Optional[float] = None,
                  risk_pct: Optional[float] = None, risk_dollars: Optional[float] = None,
                  max_position_pct: Optional[float] = None,
                  leverage: Optional[int] = None, market: Optional[tc.Market] = None,
                  step: Optional[float] = None) -> Dict[str, Any]:
    """Whole shares so a stop-out loses at most the risk budget, capped by max position and
    buying power (account x leverage). Uses tc.position_size for the first two caps.

    Crypto (`market`, default the active one): a fractional size floored to `step` (the pair's
    size increment, default 1e-8), and the paper fees of the entry and the stop exit count toward
    the loss, so `max_loss` = risk_used + fees_at_stop stays within the budget. R is still the stop
    distance; `fees_r` says what the round trip costs in R. Without `settings` the defaults apply,
    with the configured exchange's fee (Binance 0.1%, Coinbase 0.6% per fill)."""
    s = default_settings() if settings is None else settings
    m = market or tc.market()
    frac = m.fractional
    step = float(step) if step else tc.qty_step(m)
    fee = tc.fee_pct(m, s)
    errs = tc.validate_levels(side, entry, stop, target)
    if errs:
        raise ValueError("; ".join(errs))
    acct = float(account if account is not None else s["account_size"])
    if not math.isfinite(acct) or acct <= 0:
        raise ValueError("account size must be positive - set it with: risk.py config --account N")
    if risk_dollars is not None:
        pct, budget, budget_source = None, float(risk_dollars), "risk_dollars"
    else:
        pct = float(risk_pct if risk_pct is not None else s["risk_per_trade_pct"])
        budget, budget_source = acct * pct / 100.0, "risk_pct"
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("the risk budget must be positive (--risk-pct / --risk-dollars, or "
                         "risk.py config --risk-pct X)")
    cap_pct = float(max_position_pct if max_position_pct is not None else s["max_position_pct"])
    if not math.isfinite(cap_pct) or cap_pct <= 0:
        raise ValueError("max position % must be positive (--max-position-pct, or risk.py config)")
    lev = int(leverage) if leverage is not None else 1
    if lev not in LEVERAGES:
        raise ValueError(f"leverage must be one of {', '.join(map(str, LEVERAGES))}")

    base = tc.position_size(entry, stop, account=acct, risk_dollars=budget,
                            max_position_pct=cap_pct, settings=s, step=step, fee=fee)
    per_share = abs(entry - stop)
    rate = fee / 100.0
    loss_per = per_share + rate * (entry + stop)      # = per_share for stocks (no fee)
    max_notional = acct * cap_pct / 100.0
    buying_power = acct * lev
    if frac:
        limits: Dict[str, Any] = {"risk": _floor_step(budget / loss_per, step),
                                  "max_position_pct": _floor_step(max_notional / entry, step),
                                  "buying_power": _floor_step(buying_power / entry, step)}
        shares: Any = float(base["shares"])
    else:
        limits = {"risk": _floor_shares(budget / per_share),
                  "max_position_pct": _floor_shares(max_notional / entry),
                  "buying_power": _floor_shares(buying_power / entry)}
        shares = int(base["shares"])
    capped_by = base["capped_by"]
    if shares > limits["buying_power"]:
        shares, capped_by = limits["buying_power"], "buying_power"

    risk_used = shares * per_share
    fees_at_stop = shares * rate * (entry + stop)
    notional = shares * entry
    out: Dict[str, Any] = {
        "market": m.key,
        "side": side,
        "action": "BUY" if side == "long" else "SELL SHORT",
        "shares": shares,
        "entry": entry, "stop": stop, "target": target,
        "risk_per_share": round(per_share, 4),
        "stop_pct": round(per_share / entry * 100.0, 4),
        "risk_budget": round(budget, 2),
        "risk_budget_source": budget_source,
        "risk_pct": pct,
        "risk_used": round(risk_used, 2),
        "risk_used_pct": round(risk_used / acct * 100.0, 4),
        "notional": round(notional, 2),
        "position_pct": round(notional / acct * 100.0, 2),
        "account": acct,
        "max_position_pct": cap_pct,
        "max_notional": round(max_notional, 2),
        "leverage": lev,
        "leverage_given": leverage is not None,
        "buying_power": round(buying_power, 2),
        "capped_by": capped_by,
        "limits": limits,
        "max_loss": round(risk_used + fees_at_stop, 2),
        "rr": None, "reward_per_share": None, "reward_at_target": None, "breakeven_win_rate_pct": None,
    }
    if frac:
        out.update(exchange=s.get("crypto_exchange") or tc.crypto_exchange(),
                   step=step, fee_pct=round(fee, 6), fees_at_stop=round(fees_at_stop, 2),
                   loss_per_unit=round(loss_per, 8), fees_r=round(rate * (entry + stop) / per_share, 4),
                   fees_at_target=None, rr_after_fees=None)
    if target is not None:
        rr = tc.reward_risk(side, entry, stop, target)
        reward = abs(target - entry)
        out.update(rr=round(rr, 4), reward_per_share=round(reward, 4),
                   reward_at_target=round(shares * reward, 2),
                   breakeven_win_rate_pct=round(100.0 / (1.0 + rr), 2))
        if frac:   # reward net of both fees / loss at the stop incl. both fees
            out.update(fees_at_target=round(shares * rate * (entry + target), 2),
                       rr_after_fees=round((reward - rate * (entry + target)) / loss_per, 4))
    return out


def _local_day(stamp: Any) -> str:
    """Calendar date of an ISO stamp in the active market's zone (ET; UTC for crypto)."""
    d = _parse_time(stamp)
    return d.strftime("%Y-%m-%d") if d is not None else str(stamp or "")[:10]


def row_fee_rate(row: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> float:
    """Paper fee per fill (a fraction) of a ledger row: 0 for stocks; crypto: its fee_pct, else the setting."""
    if not tc.is_crypto_symbol(str(row.get("symbol") or "")):
        return 0.0
    v = _num(row.get("fee_pct"))
    return (v if v is not None else tc.fee_pct(tc.CRYPTO, settings)) / 100.0


def budget_row(t: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A trade as the broker's budget check counts it (a copy): stamps in the active market's zone
    (so tc.day_summary's day and loss-streak order hold with ET and UTC stamps in one ledger), and
    for crypto the stop moved by both fills' fees, so tc.trade_initial_risk is the whole loss if the
    stop fills. Stocks: same numbers as before."""
    c = dict(t)
    for field in ("opened_at", "closed_at"):
        d = _parse_time(c.get(field))
        if d is not None:
            c[field] = d.isoformat(timespec="seconds")
    rate = row_fee_rate(c, settings)
    e, s = _num(c.get("entry_price")), _num(c.get("stop"))
    if rate > 0 and e is not None and s is not None:
        c["stop"] = s - rate * (e + s) if c.get("side") == "long" else s + rate * (e + s)
    return c


def budget_ledger(ledger: Dict[str, Any], day: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The ledger as the broker's budget check sees it (pure).

    A working limit order confirmed on `day` can still fill without another check, so the broker
    counts it as an open trade opened today: its risk and its trade slot are already committed.
    tc.day_summary only looks at trades, so without this the risk skill would say OK to a trade
    the broker then refuses. Rows added here carry `_working: True`. Crypto rows count the fees
    at the stop (see budget_row); `day` is a day of the active market.
    """
    trades = [t for t in ledger.get("trades") or [] if isinstance(t, dict)]
    linked = {t.get("ticket_id") for t in trades if t.get("ticket_id")}
    synth = []
    for k in ledger.get("tickets") or []:
        if (not isinstance(k, dict) or k.get("status") != "working" or k.get("id") in linked
                or _local_day(k.get("confirmed_at")) != day):
            continue
        synth.append({"id": k.get("id"), "symbol": k.get("symbol"), "side": k.get("side"),
                      "qty": k.get("qty"), "entry_price": k.get("entry"), "stop": k.get("stop"),
                      "fee_pct": k.get("fee_pct"),
                      "status": "open", "opened_at": k.get("confirmed_at"), "_working": True})
    return {"tickets": [], "trades": [budget_row(t, settings) for t in trades + synth]}


def budget_view(bc: Dict[str, Any], risk_per_share: float, risk_used: float,
                ledger: Optional[Dict[str, Any]] = None, step: float = 1.0) -> Dict[str, Any]:
    """tc.budget_check result + what is left after this trade and the most shares that fit now.
    `ledger` is the budget_ledger() view the check ran on (for the working-order share of risk).
    Crypto: `risk_per_share` is the loss per unit incl. fees, `step` the size increment."""
    lim, summ = bc["limits"], bc["summary"]
    used = float(bc["loss_budget_used"])
    count_blocked = (summ["trades_today"] >= lim["max_trades_per_day"]
                     or summ["loss_streak"] >= lim["max_consecutive_losses"])
    # trade count / tilt guard block any new risk, whatever dollars are left
    room = 0.0 if count_blocked else max(0.0, min(float(lim["daily_max_loss"]) - used,
                                                  float(lim["max_open_risk"]) - float(summ["open_risk"])))
    if count_blocked or risk_per_share <= 0:
        max_shares: Any = 0
    else:
        max_shares = _floor_shares(room / risk_per_share) if step >= 1 else _floor_step(room / risk_per_share, step)
    working = [t for t in (ledger or {}).get("trades") or [] if t.get("_working")]
    return {
        "ok": bool(bc["ok"]),
        "reasons": list(bc["reasons"]),
        "date": summ["date"],
        "realized_pnl": summ["realized_pnl"],
        "open_risk": summ["open_risk"],
        "working_order_risk": round(sum(tc.trade_initial_risk(t) for t in working), 2),
        "working_orders": len(working),
        "open_positions": summ["open_positions"] - len(working),
        "trades_today": summ["trades_today"],
        "loss_streak": summ["loss_streak"],
        "loss_budget_used": round(used, 2),
        "loss_budget_left": bc["loss_budget_left"],
        "loss_budget_left_after": round(max(0.0, float(lim["daily_max_loss"]) - used - risk_used), 2),
        "max_risk_now": round(room, 2),
        "max_shares_now": max_shares,
        "limits": dict(lim),
    }


def _ceil_to(x: float, places: int) -> float:
    """Round up (so a suggested setting is always enough), ignoring float noise."""
    f = 10 ** places
    return math.ceil(x * f - 1e-9) / f


def zero_units_reason(sz: Dict[str, Any], unit: str) -> str:
    """Crypto: why the size came out at 0 - every cap that allows less than one size step."""
    lim, step = sz["limits"], sz["step"]
    one = f"one {tc.fmt_qty(step)} {unit} step"
    parts: List[str] = []
    if lim["risk"] == 0:
        parts.append(f"the risk budget {tc.fmt_money(sz['risk_budget'])} does not cover {one} at "
                     f"{tc.fmt_money(sz['loss_per_unit'])}/{unit} (stop distance + fees)")
    if lim["max_position_pct"] == 0:
        parts.append(f"the max position {tc.fmt_money(sz['max_notional'])} is smaller than {one}")
    if lim["buying_power"] == 0:
        parts.append(f"the {tc.fmt_money(sz['buying_power'])} buying power is smaller than {one}")
    return f"0 {unit}: " + "; and ".join(parts or ["no size fits"])


def zero_shares_reason(sz: Dict[str, Any]) -> str:
    """Why the size came out at 0 shares: every cap that allows 0, and what 1 share would need."""
    entry, acct, lim = sz["entry"], sz["account"], sz["limits"]
    per = abs(sz["entry"] - sz["stop"])
    parts: List[str] = []
    if lim["risk"] == 0:
        pct = _ceil_to(per / acct * 100.0, 4)
        flags = f"--risk-dollars {_ceil_to(per, 2):.2f}"
        if pct <= MAX_RISK_PCT:
            flags += f" or --risk-pct {pct:g}"
        parts.append(f"the stop is {tc.fmt_money(per)}/share away but the risk budget is "
                     f"{tc.fmt_money(sz['risk_budget'])}; one share needs {tc.fmt_money(per)} of risk "
                     f"({flags}) or a stop within {tc.fmt_money(sz['risk_budget'])} of the entry")
    need_pct = _ceil_to(entry / acct * 100.0, 2)     # max position % that fits one share
    if need_pct > MAX_POSITION_PCT:
        parts.append(f"one share costs {tc.fmt_money(entry)}, more than 4x the {tc.fmt_money(acct)} "
                     "account - no max position or leverage setting allows it")
    else:
        if lim["max_position_pct"] == 0:
            parts.append(f"one share costs {tc.fmt_money(entry)}, more than the max position "
                         f"{tc.fmt_money(sz['max_notional'])} ({sz['max_position_pct']:g}% of the account); "
                         f"one share needs --max-position-pct {need_pct:g}")
        if lim["buying_power"] == 0:
            lev = next((x for x in LEVERAGES if acct * x >= entry - EPS), LEVERAGES[-1])
            parts.append(f"one share costs {tc.fmt_money(entry)}, more than the "
                         f"{tc.fmt_money(sz['buying_power'])} buying power at {sz['leverage']}x; it needs "
                         f"--leverage {lev} (only if your account really has that margin)")
    return "0 shares: " + "; and ".join(parts)


def risk_checks(sz: Dict[str, Any], settings: Dict[str, Any], budget: Optional[Dict[str, Any]] = None,
                open_items: Iterable[str] = (), plan_date: Optional[str] = None,
                today: Optional[str] = None, unit: str = "shares") -> List[str]:
    """Warnings for a sized trade (pure). Empty list = nothing to flag. Crypto sizes (sz market
    "crypto") also get the fee checks; the stock-only margin/PDT rules do not apply to them."""
    w: List[str] = []
    crypto = sz.get("market") == "crypto"
    if sz["shares"] == 0:          # first: the text card points at the first warning
        w.append(zero_units_reason(sz, unit) if crypto else zero_shares_reason(sz))
    elif sz["capped_by"] == "buying_power":
        w.append(f"size capped by {sz['leverage']}x buying power ({tc.fmt_money(sz['buying_power'])}); pass "
                 "--leverage 2 or 4 only if your account really has that margin")
    min_rr = float(settings.get("min_reward_risk") or 0)
    if sz["rr"] is not None and sz["rr"] < min_rr - 1e-6:
        w.append(f"R:R {sz['rr']:.2f} is below your minimum of {min_rr:.2f} (min_reward_risk): the target "
                 "does not pay enough for the risk")
    elif crypto and sz.get("rr_after_fees") is not None and sz["rr_after_fees"] < min_rr - 1e-6:
        w.append(f"after fees the R:R is {sz['rr_after_fees']:.2f} (reward net of fees / loss at the stop incl. "
                 f"fees), below your minimum of {min_rr:.2f}: the fees eat the edge")
    if crypto and sz.get("fee_pct") and sz["fees_r"] >= BIG_FEE_R - 1e-9:
        w.append(f"round-trip fees ≈ {sz['fees_r']:.2f}R at these levels - the paper fees alone cost more than "
                 "the stop distance; a wider stop and target (fewer units) or a lower fee tier changes that")
    if sz["stop_pct"] < NOISE_STOP_PCT:
        w.append(f"the stop is only {tc.fmt_money(sz['risk_per_share'])} ({sz['stop_pct']:.3f}%) from the "
                 f"entry - under {NOISE_STOP_PCT:g}% of price is inside normal noise and the spread; "
                 "expect to be stopped out by noise, and slippage is a big share of the risk")
    if sz["notional"] > sz["account"] + 0.005:
        w.append(f"the position ({tc.fmt_money(sz['notional'])}) is larger than the account "
                 f"({tc.fmt_money(sz['account'])}): it needs margin ({sz['leverage']}x buying power)")
    if crypto and sz["leverage"] > 1:
        w.append(f"spot crypto is paid in full - {sz['leverage']}x buying power assumes margin or derivatives "
                 "that a spot account does not have")
    elif sz["leverage"] == 4 and sz["account"] < PDT_MIN_EQUITY:
        w.append(f"4x intraday buying power normally needs a pattern-day-trader margin account with at "
                 f"least {tc.fmt_money(PDT_MIN_EQUITY)} equity")
    if sz["side"] == "short" and sz["leverage_given"] and sz["leverage"] == 1:
        w.append("a cash account (1x) cannot sell short - shorting needs a margin account")
    if sz["risk_used_pct"] > BIG_RISK_PCT:
        w.append(f"this risks {sz['risk_used_pct']:.2f}% of the account on one trade - above "
                 f"{BIG_RISK_PCT:g}%, an ordinary losing streak does real damage")
    if sz["risk_pct"] is not None and sz["risk_pct"] < TINY_RISK_PCT:
        w.append(f"risk per trade is only {sz['risk_pct']:g}% of the account - the risk % is a percent "
                 "(0.2 means 0.2%, not 20%)")
    if budget is not None and not budget["ok"]:
        for r in budget["reasons"]:
            w.append(f"daily budget: {r}")
        n = budget["max_shares_now"]
        if 0 < n < sz["shares"] and crypto:
            w.append(f"daily budget: at most {tc.fmt_qty(n)} {unit} ({tc.fmt_money(n * sz['loss_per_unit'])} risk "
                     "incl. fees) fit what is left today")
        elif 0 < n < sz["shares"]:
            w.append(f"daily budget: at most {n} shares ({tc.fmt_money(n * sz['risk_per_share'])} risk) "
                     "fit what is left today")
    for item in open_items:
        w.append(f"you already have {item}")
    if plan_date and today and plan_date != today:
        w.append(f"the saved plan is from {plan_date} - check its levels still make sense today")
    if sz["account"] < PDT_MIN_EQUITY and not crypto:      # FINRA's PDT rule is about stocks
        w.append(f"account under {tc.fmt_money(PDT_MIN_EQUITY)}: {PDT_NOTE} (a heads-up, not a block)")
    return w


def settings_warnings(s: Dict[str, Any]) -> List[str]:
    """Inconsistencies between the sizing settings and the daily budget limits (pure)."""
    w: List[str] = []
    per_trade = s["account_size"] * s["risk_per_trade_pct"] / 100.0
    if s["account_size"] <= 0:
        w.append("account size is 0 - set it with: risk.py config --account N")
        return w
    if s["risk_per_trade_pct"] <= 0:
        w.append("risk per trade is 0% - every size will be 0 shares; set --risk-pct")
    elif s["risk_per_trade_pct"] < TINY_RISK_PCT:
        w.append(f"risk per trade is only {s['risk_per_trade_pct']:g}% - the setting is a percent "
                 "(0.2 means 0.2%)")
    if s["risk_per_trade_pct"] > BIG_RISK_PCT:
        w.append(f"risking {s['risk_per_trade_pct']:g}% per trade is aggressive - above {BIG_RISK_PCT:g}%, "
                 "an ordinary losing streak does real damage")
    if per_trade > s["daily_max_loss"] + EPS:
        w.append(f"one full-size trade risks {tc.fmt_money(per_trade)}, more than the "
                 f"{tc.fmt_money(s['daily_max_loss'])} daily max loss - the budget will block it "
                 "(budget.py set --daily-max-loss X, or lower --risk-pct)")
    elif per_trade > s["max_open_risk"] + EPS:
        w.append(f"one full-size trade risks {tc.fmt_money(per_trade)}, more than the "
                 f"{tc.fmt_money(s['max_open_risk'])} max open risk - the budget will block it")
    if s["max_position_pct"] > 100:
        w.append(f"max position {s['max_position_pct']:g}% of the account needs margin - pass "
                 "--leverage 2 or 4 to `size`, or it is capped at 1x buying power")
    if s["account_size"] < PDT_MIN_EQUITY:
        w.append(f"account under {tc.fmt_money(PDT_MIN_EQUITY)}: {PDT_NOTE} (a heads-up, not a block)")
    return w


def _parse_time(value: Any) -> Optional[datetime]:
    """ISO stamp -> aware ET datetime (naive stamps are ET wall-clock), or None."""
    if not value:
        return None
    try:
        return tc.parse_et(str(value))
    except (TypeError, ValueError, OverflowError):
        return None


def ledger_items(ledger: Dict[str, Any], now: datetime, symbol: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Open paper positions and working/pending orders (optionally for one symbol)."""
    sym = symbol.upper() if symbol else None
    out: Dict[str, List[Dict[str, Any]]] = {"open": [], "working": [], "pending": []}
    for t in ledger.get("trades") or []:
        if not isinstance(t, dict) or (sym and str(t.get("symbol", "")).upper() != sym):
            continue
        if t.get("status") == "open":
            out["open"].append(t)
        elif t.get("status") == "working":
            out["working"].append(t)
    for k in ledger.get("tickets") or []:
        if not isinstance(k, dict) or (sym and str(k.get("symbol", "")).upper() != sym):
            continue
        if k.get("status") == "working":
            out["working"].append(k)
        elif k.get("status") == "pending":
            exp = _parse_time(k.get("expires_at"))
            if exp is None or exp > now:
                out["pending"].append(k)
    return out


def _qty(row: Dict[str, Any]) -> str:
    q = row.get("qty")
    if isinstance(q, float) and not q.is_integer():
        return tc.fmt_qty(q)          # a crypto size (":g" would cut 0.02946375 to 6 digits)
    return f"{q:g}" if isinstance(q, (int, float)) else str(q or "?")


def describe_items(items: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    out = []
    for t in items["open"]:
        out.append(f"an open {t.get('side', '?')} paper position in {t.get('symbol')}: {_qty(t)} "
                   f"{unit_word(str(t.get('symbol') or '') or None)} @ "
                   f"{tc.fmt_price(_num(t.get('entry_price')))} ({t.get('id', '?')})")
    for t in items["working"]:
        out.append(f"a working {t.get('side', '?')} order for {_qty(t)} {t.get('symbol')} ({t.get('id', '?')})")
    for t in items["pending"]:
        out.append(f"a pending ticket {t.get('id', '?')} for {_qty(t)} {t.get('symbol')} waiting for "
                   "a yes")
    return out


def fee_note(sz: Dict[str, Any]) -> str:
    """Crypto: what the paper fees do to this trade, in dollars and in R - shown, not hidden."""
    if not sz.get("fee_pct"):
        return "paper fees are off (crypto_fee_pct is 0) - a real exchange charges a fee on every fill"
    parts = [f"fees: {sz['fee_pct']:g}% per fill (paper, crypto_fee_pct) · round-trip fees ≈ {sz['fees_r']:.2f}R "
             "at these levels", f"{tc.fmt_money(sz['fees_at_stop'])} if the stop fills"]
    if sz.get("fees_at_target") is not None:
        parts.append(f"{tc.fmt_money(sz['fees_at_target'])} at the target · R:R after fees {sz['rr_after_fees']:.2f}")
    return " · ".join(parts) + " - the broker's P&L and R are net of fees"


def sizing_section(sz: Dict[str, Any], budget: Dict[str, Any], warnings: List[str], as_of: str) -> Dict[str, Any]:
    """What `--save` merges into the plan as `sizing`."""
    keys = ("shares", "side", "entry", "stop", "target", "risk_per_share", "risk_budget", "risk_pct",
            "risk_used", "notional", "account", "max_position_pct", "leverage", "capped_by", "rr")
    sec = {k: sz[k] for k in keys}
    if sz.get("market") == "crypto":     # what the broker and the budget need to count the fees
        sec.update({k: sz[k] for k in ("market", "exchange", "step", "fee_pct", "fees_at_stop", "max_loss",
                                       "fees_r")})        # exchange: whose size step and fee these are
    sec.update(budget_ok=budget["ok"], warnings=list(warnings), as_of=as_of)
    return sec


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _positive(value: str) -> float:
    try:
        v = float(value.replace(",", "").lstrip("$"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value!r}")
    return v


def _nonneg(value: str) -> float:
    try:
        v = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from None
    if not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError(f"expected a number >= 0, got {value!r}")
    return v


def _percent(value: str) -> float:
    return _positive(value.strip().rstrip("%"))


def _leverage(value: str) -> int:
    try:
        v = float(value.strip().lower().rstrip("x"))
    except ValueError:
        v = float("nan")
    if v not in LEVERAGES:
        raise argparse.ArgumentTypeError(
            f"leverage must be 1, 2 or 4 (cash, margin, intraday day-trader margin), got {value!r}")
    return int(v)


def parse_symbol(raw: str) -> str:
    sym = (raw or "").strip().upper()
    if tc.is_crypto_symbol(sym):
        return sym
    if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.\-=^]{0,14}", sym):
        raise ValueError(f"not a ticker symbol: {raw!r}")
    return sym


def plan_file(sym: str) -> str:
    return f"plans/{re.sub(r'[^A-Z0-9._-]', '_', sym.strip().upper())}.json"


def _exit1_on_usage_error(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    def error(message: str) -> None:  # bad flags are bad input: exit 1, not 2 (= data error)
        p.print_usage(sys.stderr)
        p.exit(1, f"{p.prog}: error: {message}\n")
    p.error = error  # type: ignore[assignment]
    return p


def build_parser() -> argparse.ArgumentParser:
    p = _exit1_on_usage_error(tc.make_parser(
        "Position sizing and pre-trade risk checks for paper day trades. Not financial advice."))
    sub = p.add_subparsers(dest="command", metavar="{size,config,show}")
    sub.required = True

    s = _exit1_on_usage_error(sub.add_parser(
        "size", help="shares to trade so a stop-out loses at most your risk budget",
        description="Size a trade from entry and stop (or a saved plan) and run the risk checks."))
    s.add_argument("symbol_pos", nargs="?", metavar="SYMBOL", help="ticker (same as --symbol)")
    s.add_argument("--symbol", help="ticker; without prices, its saved plan supplies them")
    s.add_argument("--entry", type=_positive, help="entry price (default: saved plan, else current price)")
    s.add_argument("--stop", type=_positive, help="stop price (default: saved plan)")
    s.add_argument("--target", type=_positive, help="target price, for R:R (default: saved plan)")
    s.add_argument("--side", type=str.lower, choices=("long", "short"),
                   help="default: the saved plan's side, else inferred (stop below entry = long)")
    s.add_argument("--account", type=_positive, help="account size in $ (default: settings)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--risk-pct", type=_percent, help="risk per trade as %% of the account (default: settings)")
    g.add_argument("--risk-dollars", type=_positive, help="risk per trade in $")
    s.add_argument("--max-position-pct", type=_percent, help="max position as %% of the account (default: settings)")
    s.add_argument("--leverage", type=_leverage, help="buying power multiple: 1 (cash, default), 2 or 4")
    s.add_argument("--save", action="store_true", help="merge `sizing` into plans/<SYMBOL>.json (needs a symbol)")
    tc.add_json_flag(s)

    c = _exit1_on_usage_error(sub.add_parser(
        "config", help="show or change the sizing settings",
        description="With no flags, show the settings. Flags change and save them."))
    c.add_argument("--account", type=_positive, help="account size in $")
    c.add_argument("--risk-pct", type=_percent, help="risk per trade, %% of the account (e.g. 0.2)")
    c.add_argument("--max-position-pct", type=_percent, help="max position, %% of the account (e.g. 25)")
    c.add_argument("--min-rr", type=_nonneg, help="warn when reward:risk is below this (0 = off)")
    tiers = ", ".join(f"{x['name']} {x['fee_pct']:g}" for x in tc.CRYPTO_EXCHANGES.values())
    c.add_argument("--crypto-fee-pct", type=_nonneg,
                   help=f"paper fee per crypto fill, %% of notional (default: the crypto exchange's entry-tier "
                        f"taker fee - {tiers}; at most {MAX_FEE_PCT:g})")
    c.add_argument("--crypto-exchange", type=str.lower, choices=tuple(tc.CRYPTO_EXCHANGES),
                   help=f"where crypto prices come from (default {tc.DEFAULT_SETTINGS['crypto_exchange']}): "
                        "public market data, no account or API key")
    tc.add_json_flag(c)

    sh = _exit1_on_usage_error(sub.add_parser(
        "show", help="settings plus today's open risk and loss budget",
        description="Settings, today's paper P&L, open risk, positions, working orders and sized plans."))
    sh.add_argument("--market", choices=("us", "crypto"),
                    help="whose day is today: us (US/Eastern, default) or crypto (the UTC day)")
    tc.add_json_flag(sh)
    return p


def clean_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Settings with numeric fields coerced (a hand-edited settings.json gives a clear error)."""
    s = dict(raw)
    for k in NUMERIC_SETTINGS + INT_SETTINGS:
        try:
            v = float(s[k])
            if not math.isfinite(v):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise tc.StateError(f"settings.json has a bad value for {k}: {s.get(k)!r} - fix it with "
                                "risk.py config / budget.py set, or delete the file") from None
        s[k] = int(v) if k in INT_SETTINGS else v
    return s


def safe_trade_risk(t: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> float:
    """tc.trade_initial_risk as the budget counts it (crypto: incl. the fees at the stop)."""
    return tc.trade_initial_risk(budget_row(t, settings))


def safe_budget_check(new_risk: float, settings: Dict[str, Any], ledger: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return tc.budget_check(new_risk, settings=settings, ledger=ledger)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise tc.StateError(f"ledger.json has a malformed trade or working order ({e!r}) - fix or "
                            "remove it") from None


def _price_note(price: float, name: str, tick: Optional[float] = None, sym: Optional[str] = None) -> Optional[str]:
    """Stocks: sub-penny digits. Crypto: off the product's price tick (`tick`; None = unknown, no note)."""
    if tc.market().key == "crypto":
        if tick and abs(round(price / tick) * tick - price) > tick * 1e-6:
            return f"{name} {price} is not on {sym or 'the product'}'s price tick {tc.fmt_qty(tick)}"
        return None
    if price >= 1 and abs(round(price, 2) - price) > 1e-9:
        return f"{name} {price:g} has sub-penny digits; stocks at $1+ are quoted in cents"
    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_size(args: argparse.Namespace) -> int:
    if args.symbol and args.symbol_pos and parse_symbol(args.symbol) != parse_symbol(args.symbol_pos):
        raise ValueError(f"two different symbols given: {args.symbol_pos} and --symbol {args.symbol}")
    raw_sym = args.symbol or args.symbol_pos
    sym = parse_symbol(raw_sym) if raw_sym else None
    market = tc.use_market_for(sym)      # crypto: the UTC day, fractional sizes, paper fees
    crypto = market.key == "crypto"
    ex = tc.crypto_exchange() if crypto else None     # whose prices, pair rules and fee (Binance by default)
    unit = unit_word(sym)
    if args.save and not sym:
        raise ValueError("--save needs --symbol (the sizing is stored in that ticker's plan)")
    if args.risk_pct is not None and args.risk_pct > MAX_RISK_PCT:
        raise ValueError("--risk-pct is a percent of the account and cannot exceed 100")
    if args.max_position_pct is not None and args.max_position_pct > MAX_POSITION_PCT:
        raise ValueError(f"--max-position-pct cannot exceed {MAX_POSITION_PCT:g} (4x buying power)")

    settings = clean_settings(tc.load_settings())
    plan = tc.load_plan(sym) if sym else None
    if plan is not None and not isinstance(plan, dict):
        raise tc.StateError(f"{plan_file(sym)} is not a plan object - fix or delete it")  # type: ignore[arg-type]
    if args.stop is None and _num((plan or {}).get("stop")) is None:
        if sym and plan is None:
            raise ValueError(f"no saved plan for {sym}: pass --stop (and --entry, or the current price "
                             "is used)")
        if sym:
            raise ValueError(f"the saved plan for {sym} has no usable stop: pass --stop")
        raise ValueError("pass --entry and --stop (or --symbol SYM to use its saved plan)")

    now = tc.now_et()
    notes: List[str] = []
    last_price = None
    if args.entry is None and _num((plan or {}).get("entry")) is None:
        if not sym:
            raise ValueError("pass --entry (or --symbol SYM to use the current price)")
        q = tc.quote(sym)
        qt = q.get("time")
        if isinstance(qt, (int, float)) and os.environ.get("DAYTRADER_NOW") and qt > now.timestamp() + 60:
            raise ValueError("the live quote is newer than $DAYTRADER_NOW (a replay) - pass --entry")
        last_price = float(q["price"])
        when = ""
        if isinstance(qt, (int, float)):
            qd = tc.to_et(qt)
            when = f" at {qd.strftime('%H:%M' if qd.date() == now.date() else '%Y-%m-%d %H:%M')} {tc.tz_label()}"
        notes.append(f"entry = last price {tc.fmt_price(last_price)}{when} ({exchange_name(ex) if crypto else 'Yahoo'}); "
                     "a market order fills wherever price is when it executes")

    step = tick = min_funds = None
    if crypto and sym:          # the exchange's size step and price tick (public, cached a day; best effort)
        try:
            info = tc.product_info(sym)
            step, tick, min_funds = info["base_increment"], info["quote_increment"], _num(info.get("min_market_funds"))
        except tc.FetchError as e:
            notes.append(f"{rules_name(ex)} unavailable ({e}): size step {tc.fmt_qty(tc.qty_step())} "
                         "assumed, prices not checked against the tick")
        usd = usd_pair_note(sym, ex)
        if usd:
            notes.append(usd)
    lv = resolve_levels(plan, args.entry, args.stop, args.target, args.side, last_price)
    notes = lv["notes"] + notes
    sz = size_position(lv["side"], lv["entry"], lv["stop"], lv["target"], settings,
                       account=args.account, risk_pct=args.risk_pct, risk_dollars=args.risk_dollars,
                       max_position_pct=args.max_position_pct, leverage=args.leverage, market=market, step=step)
    ledger = tc.load_ledger()
    view = budget_ledger(ledger, now.strftime("%Y-%m-%d"), settings)
    if crypto:     # the budget counts the whole loss at the stop: price risk + both fills' fees
        budget = budget_view(safe_budget_check(sz["max_loss"], settings, view),
                             sz["loss_per_unit"], sz["max_loss"], view, sz["step"])
    else:
        budget = budget_view(safe_budget_check(sz["risk_used"], settings, view),
                             sz["risk_per_share"], sz["risk_used"], view)
    items = describe_items(ledger_items(ledger, now, sym)) if sym else []
    used_plan = "plan" in lv["sources"].values()
    plan_date = str(plan.get("updated_at") or plan.get("created_at") or "")[:10] if plan and used_plan else None
    warnings = risk_checks(sz, settings, budget, items, plan_date or None, now.strftime("%Y-%m-%d"), unit)
    if crypto and min_funds and 0 < sz["notional"] < min_funds:
        warnings.append(f"notional {tc.fmt_money(sz['notional'])} is below {exchange_name(ex)}'s "
                        f"{min_order_text(min_funds, sym, ex)} minimum order for {sym} - a real order this small "  # type: ignore[arg-type]
                        "would be rejected")

    if used_plan:
        notes.insert(0, "levels from the saved plan: " + ", ".join(
            k for k in ("side", "entry", "stop", "target") if lv["sources"].get(k) == "plan"))
    if lv["sources"]["side"] == "inferred" and lv["side"] == "short" and not any(n.startswith("side ") for n in notes):
        notes.append("side SHORT inferred: the stop is above the entry (for a long, the stop must be below "
                     "the entry)")
    if sz["target"] is None and not any("saved target" in n for n in notes):
        notes.append("no target: add --target to check reward:risk")
    if lv["side"] == "short" and crypto:
        notes.append(f"spot crypto cannot be sold short on {exchange_name(ex)} (that needs margin or derivatives); "
                     "the paper broker simulates the short like a stock short")
    elif lv["side"] == "short":
        notes.append("short selling needs a margin account and shares available to borrow "
                     "(hard-to-borrow fees may apply)")
    for name in ("entry", "stop", "target"):
        n = _price_note(sz[name], name, tick, sym) if sz[name] is not None else None
        if n:
            notes.append(n)
    if crypto:
        notes.append(fee_note(sz))
    tm = (plan or {}).get("time_machine")
    tm_ok = isinstance(tm, dict) and tm_levels_match(tm, lv)
    odds = tm_display(tm) if tm_ok else None  # type: ignore[arg-type]
    if odds and odds["target_first"] is not None and odds["stop_first"] is not None:
        notes.append(f"time-machine: target first {odds['target_first']:.1f}% / stop first "
                     f"{odds['stop_first']:.1f}% - {tm.get('verdict') or ''}".rstrip(" -"))  # type: ignore[union-attr]
    elif sym:
        in_plan = bool(plan) and not levels_changed(plan, lv)
        notes.append(untested_note(sym, lv, in_plan or (args.save and sz["shares"] > 0),
                                   isinstance(tm, dict) and not tm_ok))
    notes.append("risk assumes the stop fills at its price; gaps, halts and slippage can lose more")

    res: Dict[str, Any] = {"symbol": sym}
    res.update(sz)
    res.update({
        "order": f"{sz['action']} {size_text(sz['shares'], crypto)} {sym or unit}",
        "sizing_note": size_explanation(sz, unit),
        "budget": budget,
        "warnings": warnings,
        "notes": notes,
        "sources": lv["sources"],
        "plan_found": plan is not None,
        "saved": False, "plan_file": None, "dropped_sections": [], "save_error": None,
        "as_of": now.isoformat(timespec="seconds"),
        "disclaimer": tc.DISCLAIMER,
    })
    code = 0
    if crypto:
        res.update(unit=unit, tick=tick, min_order=min_funds)
    if args.save:
        if sz["shares"] == 0:
            res["save_error"] = f"not saved: the size is 0 {unit}"
            code = 1
        else:
            dropped = save_sizing(sym, lv, sizing_section(sz, budget, warnings, res["as_of"]), plan)
            res.update(saved=True, plan_file=str(tc.home() / plan_file(sym)), dropped_sections=dropped)
    tc.emit(res, args.json, render_size)
    return code


def save_sizing(sym: str, lv: Dict[str, Any], section: Dict[str, Any],
                plan: Optional[Dict[str, Any]]) -> List[str]:
    """Merge `sizing` (and the levels) into the plan; drop sections computed for old levels."""
    payload: Dict[str, Any] = {"symbol": sym, "side": lv["side"], "entry": lv["entry"],
                               "stop": lv["stop"], "sizing": section}
    if lv["target"] is not None:
        payload["target"] = lv["target"]
    dropped: List[str] = []
    if plan and levels_changed(plan, lv):
        # Only sections with content: the entry skill resets them to null when its levels change.
        dropped = [k for k in DEPENDENT_SECTIONS if plan.get(k)]
        if lv["target"] is None and plan.get("target") is not None:
            dropped.append("target")
    merged = tc.save_plan(payload)
    if dropped:
        for k in dropped:
            merged.pop(k, None)
        tc.save_json(plan_file(sym), merged)
    return dropped


def saved_exchange() -> Optional[str]:
    """The crypto exchange settings.json names (None when it names none: the default applies)."""
    stored = tc.load_json("settings.json", {})
    value = stored.get("crypto_exchange") if isinstance(stored, dict) else None
    return str(value).strip().lower() if value else None


def settings_view(s: Dict[str, Any]) -> Dict[str, Any]:
    ex = s.get("crypto_exchange") or tc.crypto_exchange()
    view = {k: s[k] for k in NUMERIC_SETTINGS + INT_SETTINGS}
    view["crypto_exchange"] = ex
    return {"settings": view,
            "risk_per_trade_dollars": round(s["account_size"] * s["risk_per_trade_pct"] / 100.0, 2),
            "max_position_dollars": round(s["account_size"] * s["max_position_pct"] / 100.0, 2),
            # crypto prices: the exchange in use, its entry-tier fee, and the choice saved in settings.json
            "crypto": {"exchange": ex, "name": exchange_name(ex), "fee_pct": s["crypto_fee_pct"],
                       "exchange_fee_pct": exchange_fee_pct(ex), "saved_exchange": saved_exchange()},
            "warnings": settings_warnings(s)}


def crypto_relevant(v: Dict[str, Any]) -> bool:
    """Whether the settings card shows the crypto row: a crypto view or change, a fee that is not the
    exchange's own, or a non-default exchange saved by the user. A stock-only card stays as it was."""
    c = v.get("crypto") or {}
    if {"crypto_fee_pct", "crypto_exchange"} & set(v.get("changed") or ()) or v.get("market") == "crypto":
        return True
    if c and abs(float(c["fee_pct"]) - float(c["exchange_fee_pct"])) > 1e-9:
        return True
    return (c.get("saved_exchange") or tc.DEFAULT_SETTINGS["crypto_exchange"]) != tc.DEFAULT_SETTINGS["crypto_exchange"]


def cmd_config(args: argparse.Namespace) -> int:
    updates: Dict[str, Any] = {}
    if args.account is not None:
        updates["account_size"] = args.account
    if args.risk_pct is not None:
        if args.risk_pct > MAX_RISK_PCT:
            raise ValueError("--risk-pct is a percent of the account and cannot exceed 100")
        updates["risk_per_trade_pct"] = args.risk_pct
    if args.max_position_pct is not None:
        if args.max_position_pct > MAX_POSITION_PCT:
            raise ValueError(f"--max-position-pct cannot exceed {MAX_POSITION_PCT:g} (4x buying power)")
        updates["max_position_pct"] = args.max_position_pct
    if args.min_rr is not None:
        updates["min_reward_risk"] = args.min_rr
    if args.crypto_fee_pct is not None:
        if args.crypto_fee_pct > MAX_FEE_PCT:
            raise ValueError(f"--crypto-fee-pct is the fee per fill in percent (0.1 = 0.1%); above "
                             f"{MAX_FEE_PCT:g} is not a real fee tier")
        updates["crypto_fee_pct"] = args.crypto_fee_pct
    if args.crypto_exchange is not None:
        updates["crypto_exchange"] = args.crypto_exchange
    s = clean_settings(tc.save_settings(updates) if updates else tc.load_settings())
    res = settings_view(s)
    res["changed"] = sorted(updates)
    res["settings_file"] = str(tc.home() / "settings.json")
    res["notes"] = exchange_notes(updates, s)
    tc.emit(res, args.json, render_config)
    return 0


def exchange_notes(updates: Dict[str, Any], s: Dict[str, Any]) -> List[str]:
    """What else a --crypto-exchange change means: an environment override that still wins, and a
    fee of the user's own that does not follow the exchange."""
    if "crypto_exchange" not in updates:
        return []
    env = (os.environ.get("DAYTRADER_CRYPTO_EXCHANGE") or "").strip().lower()
    if env and env != updates["crypto_exchange"]:
        return [f"$DAYTRADER_CRYPTO_EXCHANGE={env} overrides the saved setting while it is set: crypto prices "
                f"still come from {exchange_name(env) if env in tc.CRYPTO_EXCHANGES else env} here"]
    ex = s["crypto_exchange"]
    name, fee = exchange_name(ex), exchange_fee_pct(ex)
    stored = tc.load_json("settings.json", {})
    own_fee = "crypto_fee_pct" not in updates and isinstance(stored, dict) and "crypto_fee_pct" in stored
    notes = [f"new crypto sizes and tickets use {name} prices and pair rules"
             + ("" if own_fee or "crypto_fee_pct" in updates else f", and its {fee:g}% entry-tier taker fee")
             + "; paper trades already open keep the exchange that priced them"]
    if own_fee:
        notes.append(f"your own crypto_fee_pct of {s['crypto_fee_pct']:g}% stays; {name}'s entry-tier taker fee is "
                     f"{fee:g}% (risk.py config --crypto-fee-pct {fee:g})")
    return notes


def cmd_show(args: argparse.Namespace) -> int:
    tc.set_market(args.market or tc.market_for(None))     # whose day is "today" (UTC for crypto)
    s = clean_settings(tc.load_settings())
    ledger = tc.load_ledger()
    now = tc.now_et()
    view = settings_view(s)
    full_risk = view["risk_per_trade_dollars"]
    budget_led = budget_ledger(ledger, now.strftime("%Y-%m-%d"), s)
    bc = safe_budget_check(0.0, s, budget_led)
    nxt = safe_budget_check(full_risk, s, budget_led) if full_risk > 0 else bc
    today = budget_view(bc, 0.0, 0.0, budget_led)
    for k in ("max_shares_now", "loss_budget_left_after"):   # per-trade numbers; meaningless here
        today.pop(k, None)
    items = ledger_items(ledger, now)
    positions = []
    for t in items["open"]:
        try:
            risk = round(safe_trade_risk(t, s), 2)   # directional, as the budget counts it (crypto: + fees)
        except (KeyError, TypeError, ValueError, tc.StateError):
            risk = None
        positions.append({"id": t.get("id"), "symbol": t.get("symbol"), "side": t.get("side"),
                          "qty": t.get("qty"), "entry_price": _num(t.get("entry_price")),
                          "stop": _num(t.get("stop")), "target": _num(t.get("target")),
                          "risk": risk, "opened_at": t.get("opened_at")})
    orders = [{"id": t.get("id"), "symbol": t.get("symbol"), "side": t.get("side"), "qty": t.get("qty"),
               "entry": _num(t.get("entry") or t.get("entry_price")), "stop": _num(t.get("stop")),
               "status": t.get("status")} for t in items["working"] + items["pending"]]
    sized = []
    for p in tc.list_plans():
        sec = p.get("sizing") if isinstance(p, dict) else None
        if not isinstance(sec, dict):
            continue
        fresh = (p.get("side") == sec.get("side") and _same(_num(p.get("entry")), _num(sec.get("entry")))
                 and _same(_num(p.get("stop")), _num(sec.get("stop"))))
        sized.append({"symbol": p.get("symbol"), "side": sec.get("side"), "shares": sec.get("shares"),
                      "entry": _num(sec.get("entry")), "stop": _num(sec.get("stop")),
                      "risk_used": sec.get("max_loss", sec.get("risk_used")), "as_of": sec.get("as_of"),
                      "current": fresh})
    res = dict(view)
    res.update({
        "today": today,
        "next_trade": {"risk": full_risk, "ok": nxt["ok"], "reasons": nxt["reasons"]},
        "open_positions": positions,
        "orders": orders,
        "sized_plans": sized,
        "market": tc.market().key,
        "as_of": now.isoformat(timespec="seconds"),
        "disclaimer": tc.DISCLAIMER,
    })
    tc.emit(res, args.json, render_show)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "size":
        return cmd_size(args)
    if args.command == "config":
        return cmd_config(args)
    return cmd_show(args)


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------

def _pct(x: float) -> str:
    """Percent with enough digits to be honest for small numbers (0.19%, 11.7%)."""
    return f"{x:.2f}%" if abs(x) < 10 else f"{x:.1f}%"


def size_explanation(sz: Dict[str, Any], unit: str = "shares") -> str:
    lim = sz["limits"]
    budget = tc.fmt_money(sz["risk_budget"])
    if sz["risk_pct"] is not None:
        budget += f" ({sz['risk_pct']:g}% of the account)"
    if sz.get("market") == "crypto":
        q = tc.fmt_qty
        if sz["capped_by"] == "risk":
            return (f"risk budget {budget} / {tc.fmt_money(sz['loss_per_unit'])} per {unit} (stop distance + fees) = "
                    f"{q(lim['risk'])} {unit} (max position allows {q(lim['max_position_pct'])})")
        if sz["capped_by"] == "max_position_pct":
            return (f"max position {sz['max_position_pct']:g}% of the account = {tc.fmt_money(sz['max_notional'])}"
                    f" -> {q(lim['max_position_pct'])} {unit} (the risk budget alone allows {q(lim['risk'])})")
        return (f"buying power {tc.fmt_money(sz['buying_power'])} at {sz['leverage']}x -> {q(lim['buying_power'])} "
                f"{unit} (the risk budget alone allows {q(lim['risk'])})")
    if sz["capped_by"] == "risk":
        return (f"risk budget {budget} / {tc.fmt_money(sz['risk_per_share'])} per share = {lim['risk']:,} "
                f"shares (max position allows {lim['max_position_pct']:,})")
    if sz["capped_by"] == "max_position_pct":
        return (f"max position {sz['max_position_pct']:g}% of the account = {tc.fmt_money(sz['max_notional'])}"
                f" -> {lim['max_position_pct']:,} shares (the risk budget alone allows {lim['risk']:,})")
    return (f"buying power {tc.fmt_money(sz['buying_power'])} at {sz['leverage']}x -> {lim['buying_power']:,} "
            f"shares (the risk budget alone allows {lim['risk']:,})")


def _num_text(x: float) -> str:
    """1 -> '1.0', 1.25 -> '1.25' (settings read better with one decimal)."""
    return f"{x:.1f}" if abs(x * 10 - round(x * 10)) < 1e-9 else f"{x:g}"


def _when(stamp: Any, today: str) -> str:
    """'10:15' for today, '10-29 10:15' for another day, '-' if unparseable."""
    d = _parse_time(stamp)
    if d is None:
        return "-"
    return d.strftime("%H:%M") if d.strftime("%Y-%m-%d") == today else d.strftime("%m-%d %H:%M")


def _row(label: str, value: str) -> str:
    return f"  {label:<14}{value}"


def render_size(r: Dict[str, Any]) -> str:
    crypto = r.get("market") == "crypto"
    unit = r.get("unit") or "shares"
    head = " · ".join(x for x in ("POSITION SIZE", r["symbol"], r["side"].upper()) if x)
    card = f"{r['order']} @ {tc.fmt_price(r['entry'])} · STOP {tc.fmt_price(r['stop'])}"
    if r["target"] is not None:
        card += f" · TARGET {tc.fmt_price(r['target'])}"
    card += " · PAPER" + (f" · {exchange_name(r.get('exchange'))} prices" if crypto else "")
    if not r["shares"]:
        card = (f"0 {unit.upper() if crypto else 'SHARES'} - no trade at entry {tc.fmt_price(r['entry'])} / stop "
                f"{tc.fmt_price(r['stop'])} with this risk (see the first ! line)")
    lines = [head, card]
    if crypto:
        fee_per = r["loss_per_unit"] - r["risk_per_share"]
        lines.append(_row(f"risk/{unit}", f"{tc.fmt_money(r['risk_per_share'])} ({_pct(r['stop_pct'])} of price) + "
                                          f"{tc.fmt_money(fee_per)} fees = {tc.fmt_money(r['loss_per_unit'])}"))
        lines.append(_row("$ at risk", f"{tc.fmt_money(r['max_loss'])} ({tc.fmt_money(r['risk_used'])} + "
                                       f"{tc.fmt_money(r['fees_at_stop'])} fees at the stop) of a "
                                       f"{tc.fmt_money(r['risk_budget'])} budget "
                                       f"({_pct(r['max_loss'] / r['account'] * 100.0)} of the account)"))
    else:
        lines.append(_row("risk/share", f"{tc.fmt_money(r['risk_per_share'])} ({_pct(r['stop_pct'])} of price)"))
        lines.append(_row("$ at risk", f"{tc.fmt_money(r['risk_used'])} of a {tc.fmt_money(r['risk_budget'])} "
                                       f"budget ({_pct(r['risk_used_pct'])} of the account)"))
    lines.append(_row("notional", f"{tc.fmt_money(r['notional'])} ({_pct(r['position_pct'])} of a "
                                  f"{tc.fmt_money(r['account'])} account)"))
    if r["rr"] is not None:
        after = f" · after fees {r['rr_after_fees']:.2f}" if crypto and r.get("rr_after_fees") is not None else ""
        lines.append(_row("R:R", f"{r['rr']:.2f}{after} · reward at target {tc.fmt_money(r['reward_at_target'])} · "
                                 f"break-even win rate {r['breakeven_win_rate_pct']:.1f}%"))
    lines.append(_row("size set by", r["sizing_note"]))
    b = r["budget"]
    status = "OK" if b["ok"] else "BLOCKED - the broker will refuse this ticket"
    budget = (f"{status} · loss budget left {tc.fmt_money(b['loss_budget_left'])} -> "
              f"{tc.fmt_money(b['loss_budget_left_after'])} after this trade")
    if not b["ok"]:
        budget = (f"{status} · loss budget left {tc.fmt_money(b['loss_budget_left'])} · room for new "
                  f"risk {tc.fmt_money(b['max_risk_now'])}")
    lines.append(_row("daily budget", budget))
    lines += [f"! {w}" for w in r["warnings"]]
    lines += [f"- {n}" for n in r["notes"]]
    if r["saved"]:
        msg = f"Saved sizing to {r['plan_file']}."
        if r["dropped_sections"]:
            msg += (f" Cleared {', '.join(r['dropped_sections'])}: computed for the old levels - re-run "
                    "exit / time-machine.")
        lines.append(msg)
    elif r["save_error"]:
        lines.append(f"Not saved: {r['save_error'].replace('not saved: ', '')}.")
    elif r["symbol"]:
        lines.append("Not saved - add --save to store this sizing in the trade plan.")
    if r["shares"] and b["ok"] and crypto:
        where = f"--symbol {r['symbol']}" if r["symbol"] else "--market crypto"
        lines.append(f"Next: budget skill check ({where} --risk {r['max_loss']:.2f}), then a paper broker ticket for "
                     f"{tc.fmt_qty(r['shares'])} {r['symbol'] or unit} - it waits for your yes.")
    elif r["shares"] and b["ok"]:
        lines.append(f"Next: budget skill check (--risk {r['risk_used']:.2f}), then a paper broker ticket for "
                     f"{r['shares']:,} shares - it waits for your yes.")
    elif r["shares"]:
        fit = b["max_shares_now"]
        fit_text = f"{tc.fmt_qty(fit)} {unit}" if crypto else f"{fit:,} shares"
        lines.append("Next: no ticket for this size while the daily budget blocks it"
                     + (f" (at most {fit_text} fit what is left)" if 0 < fit < r["shares"] else "") + ".")
    lines.append(r["disclaimer"])
    return "\n".join(lines)


def _settings_lines(v: Dict[str, Any]) -> List[str]:
    s = v["settings"]
    return [
        _row("account", tc.fmt_money(s["account_size"])),
        _row("risk/trade", f"{s['risk_per_trade_pct']:g}% = {tc.fmt_money(v['risk_per_trade_dollars'])}"),
        _row("max position", f"{s['max_position_pct']:g}% of the account = "
                             f"{tc.fmt_money(v['max_position_dollars'])}"),
        _row("min R:R", f"{_num_text(s['min_reward_risk'])}" + (" (off)" if s["min_reward_risk"] == 0 else "")),
        _row("daily limits", f"max loss {tc.fmt_money(s['daily_max_loss'])} · max open risk "
                             f"{tc.fmt_money(s['max_open_risk'])} · {s['max_trades_per_day']} trades · "
                             f"stop after {s['max_consecutive_losses']} losses in a row"),
    ] + ([_row("crypto fees", crypto_fee_text(v))] if crypto_relevant(v) else [])


def crypto_fee_text(v: Dict[str, Any]) -> str:
    """'0.1% per fill (paper; Binance prices)', plus the exchange's own fee when yours differs."""
    c, fee = v["crypto"], float(v["settings"]["crypto_fee_pct"])
    text = f"{fee:g}% per fill (paper; {c['name']} prices)"
    if abs(fee - float(c["exchange_fee_pct"])) > 1e-9:
        text += f" · {c['name']}'s entry-tier taker fee is {float(c['exchange_fee_pct']):g}%"
    return text


def render_config(r: Dict[str, Any]) -> str:
    lines = ["RISK SETTINGS"]
    lines += _settings_lines(r)
    lines += [f"! {w}" for w in r["warnings"]]
    lines += [f"- {n}" for n in r.get("notes") or []]
    if r["changed"]:
        lines.append(f"Saved {', '.join(r['changed'])} to {r['settings_file']}.")
    else:
        crypto = (" --crypto-fee-pct X --crypto-exchange " + "|".join(tc.CRYPTO_EXCHANGES)) if crypto_relevant(r) else ""
        lines.append(f"Change with: risk.py config --account N --risk-pct X --max-position-pct X --min-rr X{crypto} "
                     "(daily limits: budget skill).")
    return "\n".join(lines)


def render_show(r: Dict[str, Any]) -> str:
    t = r["today"]
    lines = ["RISK SETTINGS"]
    lines += _settings_lines(r)
    lines.append(f"TODAY {t['date']} (" + ("the UTC day, " if r.get("market") == "crypto" else "") + "paper ledger)")
    lines.append(_row("realized P&L", f"{tc.fmt_money(t['realized_pnl'], signed=True)} · "
                                      f"loss streak {t['loss_streak']}"))
    open_line = (f"{tc.fmt_money(t['open_risk'])} across {t['open_positions']} open "
                 f"position{'s' if t['open_positions'] != 1 else ''}")
    if t["working_orders"]:
        open_line += (f" + {t['working_orders']} working order{'s' if t['working_orders'] != 1 else ''} "
                      f"({tc.fmt_money(t['working_order_risk'])})")
    lines.append(_row("open risk", f"{open_line} (max {tc.fmt_money(t['limits']['max_open_risk'])})"))
    lines.append(_row("loss budget", f"{tc.fmt_money(t['loss_budget_used'])} used of "
                                     f"{tc.fmt_money(t['limits']['daily_max_loss'])} -> "
                                     f"{tc.fmt_money(t['loss_budget_left'])} left"))
    lines.append(_row("trades today", f"{t['trades_today']} of {t['limits']['max_trades_per_day']}"))
    lines.append(_row("room for risk", tc.fmt_money(t["max_risk_now"])))
    nx = r["next_trade"]
    lines.append(_row("next trade", f"a full-size {tc.fmt_money(nx['risk'])} risk trade is "
                                    + ("OK" if nx["ok"] else "BLOCKED")))
    lines += [f"  ! {x}" for x in nx["reasons"]]
    if r["open_positions"]:
        lines.append("OPEN POSITIONS")
        rows = [[p["id"] or "?", p["symbol"] or "?", p["side"] or "?", p["qty"], tc.fmt_price(p["entry_price"]),
                 tc.fmt_price(p["stop"]), tc.fmt_money(p["risk"]), _when(p["opened_at"], t["date"])]
                for p in r["open_positions"]]
        lines.append(tc.table(rows, ["id", "symbol", "side", "qty", "entry", "stop", "risk", "opened"]))
    if r["orders"]:
        lines.append("WORKING / PENDING (today's working orders count as open risk and a trade; "
                     "pending tickets do not until confirmed)")
        rows = [[o["id"] or "?", o["symbol"] or "?", o["side"] or "?", o["qty"], tc.fmt_price(o["entry"]),
                 tc.fmt_price(o["stop"]), o["status"]] for o in r["orders"]]
        lines.append(tc.table(rows, ["id", "symbol", "side", "qty", "entry", "stop", "status"]))
    if r["sized_plans"]:
        lines.append("SIZED PLANS")
        rows = [[p["symbol"] or "?", p["side"] or "?", p["shares"], tc.fmt_price(p["entry"]), tc.fmt_price(p["stop"]),
                 tc.fmt_money(p["risk_used"]) if isinstance(p["risk_used"], (int, float)) else "-",
                 str(p["as_of"] or "-")[:16].replace("T", " "),
                 "current" if p["current"] else "levels changed - re-size"] for p in r["sized_plans"]]
        lines.append(tc.table(rows, ["symbol", "side", "shares", "entry", "stop", "risk", "sized", "status"]))
    lines += [f"! {w}" for w in r["warnings"]]
    lines.append(r["disclaimer"])
    return "\n".join(lines)


if __name__ == "__main__":
    tc.run(main)
