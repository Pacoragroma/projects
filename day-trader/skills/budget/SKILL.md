---
name: budget
description: >-
  Day-trading desk: daily guard-rails for the paper account. Shows the "RISKED SO FAR" card (realized
  P&L, open risk, loss budget used and left with a bar, trades today vs max, loss streak) and a big
  status: OPEN FOR TRADING, CAUTION (75% of the loss budget used, last trade, one loss from the tilt
  guard) or STOP TRADING (daily max loss, max open risk, max trades or the tilt guard hit). `check`
  says OK or BLOCKED for a new trade's dollar risk and must run before every paper ticket. Also sets
  the limits and shows per-day history. Use when the user asks "how much have I risked today?",
  "can I still trade?", "am I done for the day?", "how much budget is left?", "check the budget
  for this trade", "is $46.88 of risk OK?", "set my daily max loss to $200", "max 4 trades a day",
  "stop me after 3 losses", "how did my last 10 days go?", or before any broker ticket. Reads the
  local paper ledger; no API key. Paper only; not financial advice.
---
# Budget

The desk's guard-rails. Four daily limits sit over the paper ledger: **max daily loss**, **max open
risk**, **max trades per day** and a **tilt guard** (stop after N losses in a row). `status` is the
"RISKED SO FAR" card, `check` is the gate Claude runs before any ticket, `set` changes the limits
and `history` shows how past days went against them. Everything is local. Only `status --mark`
fetches prices.

## Run
> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder (`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.

```bash
python3 scripts/budget.py status                          # RISKED SO FAR card for today
python3 scripts/budget.py                                 # same as status
python3 scripts/budget.py status --mark                   # + live unrealized P&L of open positions
python3 scripts/budget.py status --date 2026-10-29        # how a past day stood at its close
python3 scripts/budget.py check --risk 46.88              # exit 0 = OK, 3 = BLOCKED
python3 scripts/budget.py check --entry 364.18 --stop 358.32 --qty 8
python3 scripts/budget.py check --symbol TSLA             # saved plan's entry/stop + sizing shares
python3 scripts/budget.py check --symbol TSLA --qty 8
python3 scripts/budget.py set                             # show the limits
python3 scripts/budget.py set --daily-max-loss 200 --max-open-risk 150 --max-trades 4 --max-losses 3
python3 scripts/budget.py history --days 10
python3 scripts/budget.py check --risk 46.88 --json       # --json on any command, for chaining
```

* `check`: the new trade's risk in dollars: `--risk X` (from the risk skill's `risk_used`), **or**
  `--entry`, `--stop` and `--qty` (risk = |entry − stop| × shares). With `--symbol`, any of the three
  not given comes from `plans/SYM.json` (shares from its `sizing`, written by `risk.py size --save`).
* `set` flags: `--daily-max-loss $`, `--max-open-risk $`, `--max-trades N`, `--max-losses N` (all > 0).
  No flags = show. Loosening a limit while today's status is STOP TRADING is refused unless
  `--force` is passed. Tightening is always allowed.
* `history --days N`: calendar days ending today (default 10). Only days with paper trades are listed.
  Flags (vs the current limits): MAX LOSS (realized P&L hit −max at any point that day, even if it
  recovered), OVER MAX TRADES, TILT (longest losing run that day reached the tilt guard).

Exit codes: **0** ok (for `check`: OK to proceed) · **1** bad input or state (bad flags, corrupt
ledger/settings, refused loosening) · **2** data source down (`status --mark` only) · **3** `check`
BLOCKED. Only exit 0 from `check` means go.

## How it works
* **Loss budget used** = today's realized loss (0 if you are up) + open risk. Profits do not extend
  the budget. **Open risk** = Σ |entry − stop| × qty over open paper positions, whenever opened,
  plus **working orders** (confirmed limit orders not yet filled). Those can fill without another
  check, so the budget counts their risk now, and each is a trade on the day it was confirmed.
  **Pending tickets** (no yes yet) are only listed. The broker re-checks the budget when one is
  confirmed.
* **check** applies the same rules as the broker's ticket check (the shared `budget_check`): BLOCKED
  if worst-case loss today (used + new risk) would exceed the daily max loss, if open risk + new risk
  would exceed max open risk, if today already has max-trades trades, or if the loss streak has hit
  the tilt guard. Exactly at a limit is still OK. Working orders make it stricter than the broker,
  never looser.
* **Status:** STOP TRADING when any limit is hit. Each reason says whether you are done for the day
  (realized max loss, max trades, tilt guard) or blocked only until a position closes (the budget or
  max open risk is full of open risk). CAUTION when ≥ 75% of the loss budget is used, one trade is
  left, one more loss trips the tilt guard, the room left is smaller than a standard trade
  (account × risk %, from the risk skill), or (`--mark`) a position trades through its stop.
* **Room for the next trade** = min(loss budget left, max open risk − open risk), rounded down to
  the cent, 0 when stopped. Checking exactly that amount passes; one cent more is BLOCKED.
* "Today" is the US/Eastern date (`$DAYTRADER_NOW` overrides). Trades count on the day they were
  opened, P&L on the day they closed. The loss streak counts today's closed trades from the latest
  back. A win or a scratch resets it, and it starts fresh each day.
* `--date` rebuilds that day as it stood at its close: trades closed later count as open.
* `--mark` fetches each open symbol's last price (Yahoo). It is informational. The budget still
  counts open positions at their full stop risk.
* The ledger is validated first. An open/closed/working trade with no usable qty, entry or stop, an
  unreadable `opened_at`/`closed_at`, a closed trade with no P&L or close time, or a ledger,
  settings or plan file that is not a JSON object, is an error (exit 1). The guard-rail fails closed
  rather than under-count risk. A closed trade missing `pnl` gets it from `exit_price`.
* Limits: paper ledger only (trades made elsewhere are invisible); exchange holidays not modelled;
  a stop is not a guaranteed fill, so real losses can exceed planned risk.

## Reading the output / how to present it to the user
```
RISKED SO FAR · Fri 2026-10-30 · 11:05 ET (market open)
STATUS: OPEN FOR TRADING
Loss budget    [############........]  $86.88 of $150.00 used (57.9%) · $63.12 left
  realized P&L  -$40.00  (1 closed: 0 W / 1 L)
  open risk     $46.88 of $150.00 max (1 position)
Trades today   2 of 5
Loss streak    1 in a row (the tilt guard stops you at 3)
Room for the next trade: $63.12 of risk (a standard trade risks $50.00)
```
* Lead with the status line exactly (`STATUS: CAUTION`), then every `x` (limit hit) and `!`
  (caution) line in plain words. Then: loss budget used/left with the bar, realized P&L, open
  risk, trades today vs max, loss streak, room for the next trade.
* For `check`, say OK or BLOCKED first, with each reason. On OK, give the "after this trade" line
  and any `!` warnings. On BLOCKED, give "Most you can risk on a new trade right now". If the
  risk skill's sizing is too big, offer to re-size to that amount. Do not suggest raising limits.
* JSON (`status`): `status, limits_hit[{code, scope: today|until_close, text}], cautions[],
  loss_budget{limit, used, left, used_pct, realized_loss, open_risk}, realized_pnl, closed_today,
  wins, losses, trades_today, max_trades_per_day, risked_today, loss_streak,
  max_consecutive_losses, open_risk{total, positions, working_orders, limit}, room_for_new_risk,
  standard_trade_risk, positions[], working_orders[], pending_tickets[], marks?, view (live |
  end_of_day), as_of`.
  `check`: `ok, verdict, new_risk, reasons[], warnings[], max_new_risk_now, max_shares_now,
  after{loss_budget_used, loss_budget_left, open_risk, trade_number}, before{...},
  working_order_risk, limits, exit_code`. `history`: `rows[{date, trades, closed, wins, losses,
  scratches, realized_pnl, realized_low, r_total, risked, max_loss_streak, still_open, flags[]}],
  totals`.

## Next step on the desk
* OK → the **broker** skill writes the paper ticket (`broker.py ticket ...`), which waits for the
  user's explicit yes.
* BLOCKED for size → back to the **risk** skill (`risk.py size --risk-dollars <room>`).
* STOP TRADING for the day → the **analyst** skill (`analyst.py review`) to look at what happened.

## Rules
* Run `check` before every ticket. If it is BLOCKED (exit 3) or fails (exit 1/2), do not create the
  ticket. Say why and stop.
* Never raise a limit, pass `--force`, or re-split a trade into smaller pieces to get past a block on
  your own. Limits are for exactly the moment the user wants to break them. Only if the user
  explicitly insists after you have reminded them of the reason, run `set` with `--force`, and say
  that this loosens their own guard-rail.
* Never call a STOP TRADING day "almost OK", and never invent or estimate budget numbers. Report only
  what the script printed. Not financial advice. Paper trading only.
* If the script exits with code 2 ("data error: ..."), tell the user which host failed (it is in the
  message, usually `query1`/`query2.finance.yahoo.com`). In sandboxes the host may need to be
  allow-listed. Never invent prices, headlines, or odds to fill the gap. Plain `status` (without
  `--mark`) works offline.
