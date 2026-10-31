---
name: broker
description: >-
  Day-trading desk: the PAPER-ONLY order ticket that waits for the user's explicit "yes". Builds a
  ticket ("BUY 8 TSLA · LIMIT 364.18 · TARGET 370.04 · STOP 358.32 · PAPER · REAL MONEY: OFF") from
  given levels or the saved trade plan, refuses it when today's loss budget says no, and opens a
  simulated position only after the user replies yes. Then applies the stop, the target and the
  time stop from 5-minute bars, fills resting limit orders, closes trades by hand, and lists
  positions with live P&L, tickets and trade history. US stocks, and crypto 24/7 on Coinbase prices
  (fractional sizes, paper fees). Use when the user says "place the trade", "buy 8 TSLA at 364.18",
  "paper trade BTC", "buy 0.01 BTC-USD", "send the order", "short NVDA here", "yes, confirm it",
  "cancel that ticket", "close my TSLA trade", "did my stop hit?", "what are my positions?", "show
  my paper trades". There is no live mode: nothing can place a real order. Free public prices,
  no API key or exchange account. Not financial advice.
---
# Paper Broker

The last desk before a trade: an order ticket that waits for your "yes". It checks the levels and
today's budget, shows the ticket, and does nothing until the user explicitly says yes to that
ticket. Then it opens a **paper** position, simulates the fill from live prices, and manages the
exits from 5-minute bars. Paper trading only: there is no live mode, no `--live` flag and no code
path that contacts a real broker. The paper ledger it keeps is what budget, risk and analyst read.

## Run
> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder (`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.

```bash
python3 scripts/broker.py ticket --symbol TSLA --qty 8 --entry 364.18 --stop 358.32 --target 370.04
python3 scripts/broker.py ticket --symbol TSLA                 # levels, size and time stop from the saved plan
python3 scripts/broker.py ticket --symbol TSLA --qty 8 --type market --stop 358.32 --target 370.04
python3 scripts/broker.py ticket --symbol NVDA --qty 20 --side short --entry 120.50 --stop 121.70 --target 118.10
python3 scripts/broker.py ticket --symbol BTC-USD --qty 0.01 --entry 101250 --stop 99000 --target 104000
python3 scripts/broker.py confirm TKT-20261030-001             # shows the ticket, confirms nothing (exit 1)
python3 scripts/broker.py confirm TKT-20261030-001 --yes       # ONLY after the user's yes, in a later message
python3 scripts/broker.py sync                                 # apply stops/targets/time stop, fill working orders
python3 scripts/broker.py sync --market crypto                 # same, on the UTC clock and day
python3 scripts/broker.py positions
python3 scripts/broker.py close TRADE-20261030-001             # exit at the current price
python3 scripts/broker.py close TRADE-20261030-001 --price 366.10
python3 scripts/broker.py cancel TKT-20261030-001
python3 scripts/broker.py tickets --all
python3 scripts/broker.py history --days 30 --json
```

`ticket` flags: `--symbol` (required), `--qty N` (default: the plan's `sizing` from the risk
skill, if it was computed for these levels; crypto takes fractions like `0.01`), `--side
long|short` (default: the plan's side, else inferred: stop below entry = long),
`--entry/--stop/--target` (all from flags or all from the saved plan, never mixed; a stop is
mandatory, a target optional), `--type limit|market` (default `limit`; a market ticket may omit
`--entry`), `--setup orb` (journal label), `--time-stop HH:MM` (default the exit plan's, else
15:55 ET / 23:55 UTC). `sync`, `positions`, `tickets`, `history` take `--market us|crypto`: whose
clock and "today" to show (default US); each trade follows its own market. All take `--json`.
Exit codes: 0 ok (also a `ticket` preview), 1 bad input or refused (no `--yes`, expired, used,
price through the stop, unknown flag), 2 data source down (`data error: ...`), 3 blocked by the
daily budget. Ledger schema and JSON fields: `reference.md`.

## How it works
* **ticket** validates the levels (`tc.validate_levels`; stocks in cents, crypto on the product's
  price tick), refuses notional above 4x the paper account, and runs the daily budget check
  (`tc.budget_check`) with the ticket's risk `|entry - stop| x qty` (crypto: plus the fees at the
  stop). Today's working limit orders count as open risk and as trades. Blocked: exit 3, no ticket.
  Otherwise a `pending` ticket expires after `ticket_ttl_minutes` (15) or at the time stop. A new
  ticket replaces an older pending one in the same symbol. A ticket opposite an open position or
  working order in that symbol is refused. Stocks: confirmable on weekdays before the time stop
  (5 min before an early close Yahoo reports; full holidays are caught at confirm).
* **Outside the session** (weekend, before ~09:15, after the time stop) `ticket` runs the same
  checks and prints the full card as a **PREVIEW**: exit 0, `status: "preview"`, no ticket id,
  nothing saved, nothing to confirm. It ends "MARKET CLOSED — preview only. Re-run ticket after
  09:30 ET to get a ticket you can confirm." A budget block still exits 3.
* **Crypto** (`BTC-USD`, `ETH-USD`, ...): Coinbase Exchange public prices, 24/7, the UTC day.
  Weekends are open; only the 23:55 UTC time stop applies (after it: "PAST THE 23:55 UTC TIME STOP
  — preview only. Re-run ticket after 00:00 UTC ..."). Sizes are fractional (the product's size
  step). Every paper fill pays `crypto_fee_pct` (default 0.6%) of its notional: trades store
  `fees` and `gross_pnl`, and `pnl` / `r_multiple` are **net of fees**. Card: `PAPER · REAL MONEY:
  OFF · Coinbase prices`. Spot crypto cannot be shorted on Coinbase; a paper short is simulated.
* **confirm --yes** is the only path that opens a position. It refuses expired, cancelled and
  used tickets, and a yes less than 10 seconds after the ticket. It needs the session (stocks
  09:30 ET to the time stop) and today's quote. It syncs open trades first (stops already hit
  count), then re-runs the budget check at the actual fill price. **Market** fills at the current
  price; a **limit** fills at the market price when that is at or better than the limit, else it
  rests as a `working` day order. A fill already through the stop or past the target is refused.
* **sync** replays completed 5-minute bars since the fill (`$DAYTRADER_NOW` replays never see the
  future). Stop or target touched → out at that price; a gap beyond a level exits at the open;
  both in one bar → **stop**, flagged `ambiguous`. The fill bar only checks the stop. Time stop:
  the close of the bar ending at 15:55 ET / 23:55 UTC (or the ticket's, or 5 min before an early
  close); day trades never carry overnight. Working limits fill when a later bar trades *through*
  the limit (or the confirmation bar closes through it); unfilled at the time stop → `expired`.
  Each close records `exit_price, exit_reason (stop|target|eod|manual), pnl, r_multiple`.
* **close** first syncs the trade. If its stop or target had already been hit, it reports that
  and changes nothing else. `--price` must be a price that traded since the fill (checked against
  the 5-minute bars; without bars a note says it was not checked). **cancel** of a working order
  first checks the bars: if it had already filled, the fill (and any exit) is recorded and nothing
  is cancelled (exit 1). **positions** shows unrealized P&L at the latest quote (best-effort).
* **Limits:** a simulation: no queue position, partial fills, stock commissions, borrow fees or
  halts; prices can be delayed; a stop fills at its price unless the bar gapped.

## Reading the output / how to present it to the user
* After `ticket`, show the card line exactly (`BUY 8 TSLA · LIMIT 364.18 · TARGET 370.04 · STOP
  358.32 · PAPER · REAL MONEY: OFF`). Then give the risk in $, the R:R, the budget line, the expiry
  time and every `!` warning; for crypto also the `Fees` line (what the fees cost, in $ and R).
  End with the ticket's own prompt: **"reply yes to confirm ticket TKT-..."**. Then stop and wait.
* A **preview** (`status: "preview"`, "ORDER TICKET · PREVIEW"): show the card and numbers, say it
  is a preview (market closed or past the time stop) and give its last line. **Never ask for a yes
  on a preview**: there is no ticket to confirm.
* Relay the "not tested by the time machine for these levels" note (no odds in the plan, or odds
  for other levels); offer the command it names. Odds print as the time machine printed them.
* Blocked (exit 3): lead with "BLOCKED", list the reasons, and say nothing was created. Suggest
  the budget skill; never work around it (e.g. by splitting the order).
* After `confirm`: FILLED (price, trade id) or WORKING (it rests until a bar trades through the
  limit). Mention `sync` and `positions`. After `sync`/`close`: for each exit give reason, price,
  bar time, P&L in $ and R (crypto: "net of $X fees"). Say "counted as stop" for `ambiguous` exits.
* JSON: `ok, action, status, preview, ticket{id, symbol, market, side, qty, type, entry, stop,
  target, risk, rr, status, expires_at, time_stop, setup, time_machine}, trade{id, market,
  entry_price, opened_at, status, exit_price, exit_reason, pnl, r_multiple}, budget, warnings[],
  notes[], real_money: false`. A preview has `order{...}` (no id) and `banner` instead of `ticket`.

## Next step on the desk
* Before a ticket: **exit** (the plan), **time-machine** (the odds), **risk** (the size,
  `risk.py size --symbol TSLA --save`) and **budget** (`budget.py check --risk 46.88`).
* After a fill: **alerts** (`alerts.py from-plan TSLA`), then `sync` during the day. After the
  close: **analyst** (`analyst.py review`) reviews the paper journal this ledger keeps.

## Rules
* **Paper only.** Live trading is intentionally not supported. There is no live mode, and
  `--live` / `--mode live` are rejected. If asked to trade real money, say so plainly. Never
  look for, suggest or build a way to reach a real broker, exchange account or API key.
* **The yes rule.** Show the ticket, then wait for the user's explicit yes **in a later
  message**. Never run `confirm --yes` in the same turn that created the ticket. Never chain
  `ticket ... && confirm --yes`, and never pass `--yes` on the user's behalf. Crypto too, 24/7.
* A preview is not a ticket. Never ask for a yes on it and never treat a yes to a preview as a yes
  to a later ticket: re-run `ticket` in the session and ask again for that ticket id.
* A yes given before the ticket existed ("buy it and confirm", "auto-confirm") does not count:
  the user must have seen this ticket. A yes counts **only for that specific ticket id**. If the
  levels, size or side change, make a new ticket and ask again. One yes never covers several
  tickets or later tickets.
* Anything other than a clear yes is **not** a yes: "maybe", "looks good?", "what do you
  think?", "ok but lower the stop", silence. Ask, or cancel the ticket.
* If the ticket expired or the budget blocks the confirm, say so. Do not re-create and confirm
  it without a fresh yes to the new ticket id.
* Never move a stop wider or retry a refused fill to get around a rule. Report only what the
  script printed. Never invent fills, prices or P&L.
* The ledger (`ledger.json`) and `settings.json` live in `$DAYTRADER_HOME` (default `~/.day-trader`).
  In a sandbox (the Claude apps) that folder may be gone next conversation: before the user leaves,
  offer both as files to download; when they upload them later, copy them back there first.
* Not financial advice. Paper results and fills are simulations, not guarantees (`tc.DISCLAIMER`).
* If a command exits 2 ("data error: ..."), tell the user which host failed. It is in the
  message: `query1`/`query2.finance.yahoo.com` for stocks, `api.exchange.coinbase.com` for crypto.
  In sandboxes the host may need to be allow-listed. Never invent prices, headlines, or odds to
  fill the gap. A limit `ticket`, `tickets`, `history` and `cancel` work offline. `confirm`,
  `sync` and `close` without `--price` need prices.
