# Paper broker: reference

Load this when you need exact fill rules, ticket states, the ledger format or JSON fields.
`SKILL.md` has the rules Claude must follow.

## Ticket life cycle

```
ticket ──► pending ──confirm --yes──► confirmed (filled now → trade opened)
              │                  └──► working ──sync──► confirmed (filled → trade opened)
              │                          │      └────► expired   (unfilled at the time stop)
              │                          └─cancel──► cancelled
              ├── TTL / time stop passes ──► expired
              ├── cancel ──► cancelled
              └── new ticket, same symbol ──► cancelled ("superseded by TKT-...")
```

* `pending`: waits for the user's yes. Expires at `created_at + ticket_ttl_minutes` (settings,
  default 15), or at the ticket's time stop if that comes first (on an early-close day, 5 minutes
  before that close).
* `working`: a confirmed limit order that wasn't marketable at confirmation. It is a **day
  order**. It reserves its risk and one trade slot in today's budget until it fills, expires or
  is cancelled.
* `confirmed`: filled. `trade_id` points at the paper trade.
* A ticket opens at most one trade. Ids: `TKT-YYYYMMDD-NNN` (day of creation) and
  `TRADE-YYYYMMDD-NNN` (day of the fill).
* **Preview** (not a state): `ticket` outside the session - a weekend, pre-market when the ticket
  would expire before 09:30, at or after the (early-close adjusted) time stop - runs every check
  (levels, notional, open positions, the budget; open trades are synced first) but saves no
  ticket and supersedes nothing. Exit 0 with `status: "preview"`, or exit 3 when the budget
  blocks. The banner is `MARKET CLOSED — preview only. Re-run ticket after 09:30 ET [on the next
  trading day] to get a ticket you can confirm.`, or `PAST THE HH:MM ET TIME STOP — ...` between
  the time stop and the close.
* **Crypto** never closes: weekends included, a ticket is confirmable from 00:00 UTC up to its time
  stop (default 23:55 UTC). After it the ticket is a preview: `PAST THE 23:55 UTC TIME STOP —
  preview only. Re-run ticket after 00:00 UTC to get a ticket you can confirm.` The yes rule, the
  10-second rule, the expiry and the budget re-check at confirm are the same as for stocks.

## Markets

| | US stocks | Crypto (`BTC-USDT`, `ETH-USDT`, `BTCUSDT`, `BTC-USD`, ... any `BASE-QUOTE` pair) |
|---|---|---|
| prices | Yahoo Finance | Binance by default (or Coinbase): public market data from `data-api.binance.vision`, or `api.exchange.coinbase.com` with the `crypto_exchange` setting `coinbase`; no account, no key |
| clock / day | US/Eastern, weekdays 09:30-16:00 | UTC, every day 00:00-24:00 (a session = a UTC day) |
| default time stop | 15:55 ET | 23:55 UTC |
| size (`--qty`) | whole shares | fractional, a multiple of the pair's size step (Binance `LOT_SIZE` stepSize, Coinbase `base_increment`; else 1e-8) |
| price tick | cents at $1+ (`sub-penny` refused) | the pair's tick (Binance `PRICE_FILTER` tickSize, Coinbase `quote_increment`; off-tick refused) |
| minimum order | none | Binance `NOTIONAL` minNotional (5 USDT for BTCUSDT), Coinbase `min_market_funds`: a smaller notional gets a warning |
| paper fees | none | `crypto_fee_pct` % of notional on every fill (unset = the exchange's entry-tier taker fee: Binance 0.1, Coinbase 0.6) |

* `--market us|crypto` on `sync`, `positions`, `tickets`, `history` picks whose clock and "today" are
  shown (default `$DAYTRADER_MARKET`, else US). Each ticket and trade is always replayed, dated and
  expired in its own market; times of the other market carry their label (`05:15 UTC`).
* `ticket` reads the pair rules (Binance `exchangeInfo`, Coinbase `/products`; cached a day). Offline
  it assumes the 1e-8 size step, does not check the tick, and says so in a note.
* **One exchange per ticket, for life.** A crypto ticket records the exchange whose prices made it
  (`exchange`: `binance` | `coinbase`) and its trade inherits it. `confirm`, `sync`, `close`,
  `cancel` and `positions` price that row on its own exchange only - never the other one - whatever
  the `crypto_exchange` setting (or `$DAYTRADER_CRYPTO_EXCHANGE`) says now. A ticket confirmed after
  the setting changed fills on its own exchange at its own `fee_pct`, with a note saying so. Crypto
  rows from before the field existed (v1.1.0) were priced on Coinbase and stay on Coinbase. On
  Binance a `-USD` symbol is priced from its USDT pair (`BTC-USD` → `BTCUSDT`; a note says so).

## Fill rules

| Situation | Result |
|---|---|
| market, on `confirm --yes` | filled at the current quote (`tc.quote`) |
| buy limit, quote ≤ limit | filled at the quote (at or better than the limit) |
| buy limit, quote > limit | `working` |
| sell-short limit, quote ≥ limit | filled at the quote |
| sell-short limit, quote < limit | `working` |
| fill price already through the stop or past the target | refused (exit 1), ticket stays pending |
| crypto, any fill | the price rounded to the pair's tick; pays `fee_pct` % of its notional |
| working buy limit, later bar with `low < limit` | filled at `min(limit, bar open)` |
| working buy limit, confirmation bar closes `< limit` | filled at the limit |
| working short limit, later bar with `high > limit` | filled at `max(limit, bar open)` |
| working short limit, confirmation bar closes `> limit` | filled at the limit |

* Only bars that **start at or after** the confirmation can fill a working order through their
  high/low. The bar in progress at that moment mixes earlier prints, so only its **close**
  counts: the market was on the far side of the limit at confirmation, so a close through the
  limit means it crossed afterwards → filled at the limit, `opened_at` = the confirmation time.
* A later bar that opens beyond the limit (a gap) fills at that open.
* Touching the limit exactly is not a fill. The price must trade through it.
* Before filling, `confirm` checks that it is a weekday, between 09:30 ET and the ticket's time
  stop (5 minutes before the close on an early-close day, from Yahoo's trading period). The quote
  must be from today (when Yahoo timestamps it). The ticket must be at least 10 seconds old.
* `cancel` on a working order first replays the bars since the confirmation. If the order had
  already filled, the fill (and any stop/target/time-stop exit) is recorded and the cancel is
  refused (exit 1), so a fill can never be erased after the fact. Without price data it cancels
  and says it could not check.
* `close TRADE-... --price X` records a manual exit at X only if X traded since the fill: it must
  lie within the low-high of the 5-minute bars from the fill bar through the bar in progress
  (half a cent of slack), else exit 1 and nothing changes. A made-up exit price would record
  made-up P&L, and realized profit widens the day's loss budget. Without bars (offline, or a trade
  older than Yahoo's 60 days of 5-minute data) X is accepted with a note that it was not checked.

## Exit rules (`sync`, and `close`/`confirm`/`ticket`/`cancel` sync first)

The trade is replayed over **completed** 5-minute regular-session bars of its own session,
starting with the bar that contains the fill:

1. **Fill bar.** Only the stop is checked (`low ≤ stop` for a long). It exits at the stop, or at
   the fill price if the fill was already beyond the stop. The target is never taken in the fill
   bar, because its high may have printed before the fill.
2. **Later bars**, in order:
   * The bar opens at or beyond the stop → `stop` at the open (gap slippage).
   * The bar opens at or beyond the target → `target` at the open (a real gap), or at the
     target price when the fill bar had already closed beyond it (it crossed the target after
     the fill, where a resting target order sells at its price).
   * Stop touched (`low ≤ stop`, or `high ≥ stop` for a short) → `stop` at the stop price. If the
     target was also touched in that bar, the exit is still the stop, with
     `ambiguous_exit: true`.
   * Target touched → `target` at the target price.
   * The bar *ends* at or after the time stop → `eod` at that bar's close. With the default
     15:55 time stop, that is the close of the 15:50-15:55 bar.
3. **Session over** (a later day, or past the close) and no bar reached the time stop, e.g. a
   half day without Yahoo's trading period → `eod` at the last bar's close, with a note.
4. **Early close** known from Yahoo's `tradingPeriods`: the time stop moves to 5 minutes before
   that close.

`closed_at` is the end of the bar that triggered the exit. `close` uses the time of the command.
`pnl = (exit - entry) x qty` (short: `entry - exit`), and
`r_multiple = pnl / ((entry_price - stop) x qty)` (short: `stop - entry_price`), rounded to 2
decimals; `null` when that risk is 0 (a fill already at or through the stop). Open risk in
`positions`, `sync` and the budget check uses the same directional risk (`tc.trade_initial_risk`).

Crypto fees: `entry_fee = fee_pct% x entry_price x qty` at the fill, `exit_fee = fee_pct% x
exit_price x qty` at the exit (both to the cent), `fees = entry_fee + exit_fee`, `gross_pnl` as
above, and **`pnl = gross_pnl - fees`**; `r_multiple` is that net `pnl` over the price risk, so a
stop-out is worse than -1R by the fees. `positions` shows crypto P&L net of both fees (as if closed
at the last price), plus `unrealized_gross` and `fees_if_closed`.

## Budget check

`tc.budget_check(risk)` runs on the ledger plus today's working orders, each treated as an open
trade at its limit. It blocks (exit 3) when one of these is true:

* worst-case loss today (realized loss + open risk + new risk) > `daily_max_loss`;
* open risk + new risk > `max_open_risk`;
* trades today ≥ `max_trades_per_day`;
* the loss streak ≥ `max_consecutive_losses` (tilt guard).

`ticket` uses the planned risk. `confirm` uses the risk at the actual fill price, which can be
higher for a market order. Both sync open trades first, so stops already hit count.

For crypto every risk the budget counts - the new ticket's, open trades', working orders' - is the
loss if the stop fills **including the fees of both fills**: `|entry - stop| x qty + fee_pct% x
(entry + stop) x qty` (the ticket's `max_loss`; `risk` stays the price risk). The day is the
calendar day of the ticket's market (the UTC day for crypto); every trade in the ledger is placed
on that day by its own time.

Settings come from the risk and budget skills (`settings.json`). `ticket` also warns when:

* risk is above the per-trade budget (`account_size x risk_per_trade_pct`);
* notional is above `max_position_pct` or the account size;
* R:R is below `min_reward_risk`;
* there is no target;
* the saved plan is from an earlier day;
* the order adds to an existing position.

It refuses notional above 4x the account.

## Ledger (`$DAYTRADER_HOME/ledger.json`, default `~/.day-trader`)

```json
{"tickets": [{"id": "TKT-20261030-001", "symbol": "TSLA", "side": "long", "qty": 8, "type": "limit",
              "entry": 364.18, "stop": 358.32, "target": 370.04, "setup": "orb", "time_stop": "15:55",
              "risk": 46.88, "rr": 1.0, "status": "confirmed", "mode": "paper",
              "created_at": "2026-10-30T10:15:00-04:00", "expires_at": "2026-10-30T10:30:00-04:00",
              "confirmed_at": "2026-10-30T10:16:00-04:00", "confirm_price": 364.10,
              "filled_at": "2026-10-30T10:16:00-04:00", "fill_price": 364.10,
              "trade_id": "TRADE-20261030-001",
              "time_machine": {"verdict": "...", "target_first_pct": 29.4, "stop_first_pct": 25.5, "...": "..."},
              "levels_source": "flags|plan", "qty_source": "flag|plan sizing"}],
 "trades":  [{"id": "TRADE-20261030-001", "ticket_id": "TKT-20261030-001", "symbol": "TSLA",
              "side": "long", "qty": 8, "entry_price": 364.10, "stop": 358.32, "target": 370.04,
              "setup": "orb", "order_type": "limit", "planned_entry": 364.18, "time_stop": "15:55",
              "time_machine": null, "mode": "paper", "opened_at": "2026-10-30T10:16:00-04:00",
              "status": "closed", "exit_price": 370.04, "closed_at": "2026-10-30T10:35:00-04:00",
              "exit_reason": "target", "pnl": 47.52, "r_multiple": 1.03}]}
```

Other fields that can appear:

* tickets: `cancelled_at`, `cancel_reason`, `expired_at`, `synced_at`;
* trades: `ambiguous_exit`, `synced_at`.
* every ticket and trade carries `market` (`us` | `crypto`). Crypto tickets add `exchange`, `tick`,
  `qty_step`, `fee_pct`, `fees_at_stop`, `max_loss`; crypto trades add `exchange`, `tick`, `fee_pct`,
  `entry_fee`, `exit_fee`, `fees`, `gross_pnl` (ids, stamps and days in UTC):

```json
{"id": "TRADE-20261031-001", "symbol": "BTC-USDT", "market": "crypto", "side": "long", "qty": 0.01,
 "entry_price": 101234.56, "stop": 99000.0, "target": 104000.0, "time_stop": "23:55",
 "opened_at": "2026-10-31T04:41:00+00:00", "status": "closed", "exit_price": 104000.0,
 "closed_at": "2026-10-31T05:10:00+00:00", "exit_reason": "target", "exchange": "binance",
 "tick": 0.01, "fee_pct": 0.1, "entry_fee": 1.01, "exit_fee": 1.04, "fees": 2.05, "gross_pnl": 27.65,
 "pnl": 25.6, "r_multiple": 1.15}
```

Writes are atomic, behind a lock file (`ledger.lock`). Only this skill writes the ledger.

## JSON output by command

* `ticket`:
  * fields: `ok, action, status: "pending", preview: false, card, ticket, budget, last_price,
    account_size, ticket_ttl_minutes, superseded[], warnings[], notes[], sync, awaiting, mode:
    "paper", real_money: false`;
  * preview (exit 0): `status: "preview", preview: true, order {symbol, side, qty, type, entry,
    stop, target, setup, time_stop, risk, rr, time_machine, levels_source, qty_source, status:
    "preview"}, card, banner, market_closed` (why there is no ticket); no `ticket`, `awaiting`;
  * blocked (exit 3): `ok: false, blocked: true, reasons[], order, budget` (plus `preview: true`,
    `banner` outside the session).
  * `ticket.time_machine` (when the plan's odds were computed for exactly these levels):
    `verdict, target_first_pct, stop_first_pct, open_at_close_pct, expected_r, as_of` and, when
    the time machine saved it, `display_pct {target_first, stop_first, open_at_close}` (the
    one-decimal values it printed; the card shows those, else the raw values with one decimal).
    Otherwise a note: `not tested by the time machine for these levels (run time_machine.py ...)`.
* `confirm`:
  * fields: `ok, action, status: "filled"|"working", confirmed, ticket, trade (null if
    working), fill_price, quote {price, time, date}, budget, warnings[], notes[], sync`;
  * without `--yes`: `ok: false, confirmed: false, reason, ticket, card` (exit 1);
  * blocked: `blocked: true, reasons[]` (exit 3).
* `sync`:
  * `events[]`, each one of:
    * `{kind: "filled", ticket_id, trade_id, price, at, bar}`;
    * `{kind: "closed", trade_id, reason, price, at, bar, pnl, r_multiple, ambiguous}`;
    * `{kind: "expired", ticket_id, price, at}`;
  * also: `open[]` (with `checked_through`), `working[]`, `issues[]`,
    `errors[{symbol, error}]` (crypto: plus `exchange`), `expired_tickets[]`, `summary` (`tc.day_summary`).
* `close`: `closed_by: "manual"|"sync", trade, quote, notes[]`.
* `cancel`: `ticket, was` (the status it had), `notes[]`.
* `positions`: `positions[]` (each trade plus `last, unrealized, unrealized_r,
  initial_risk`), `working[]`, `pending_tickets[]`, `working_risk`, `unrealized_total`,
  `summary`, `warnings[]`, `errors[]`.
* `tickets`: `tickets[]` (status shown as `expired` once past `expires_at`).
* `history`: `days, since, trades[]`, and `totals {trades, wins, losses, flat, win_rate_pct,
  net_pnl, avg_r, best, worst}` (plus `fees, gross_pnl` when crypto trades are in the window).
* `sync` events (and `open[]`, `working[]` rows) of crypto trades add `market`, `exchange`, and
  closes `fees`, `gross_pnl`.

## Data

* Fills use `tc.quote` (Yahoo 1-minute chart, `regularMarketPrice`; crypto: the ticker of the
  ticket's exchange - Binance's 24 h ticker, or Coinbase's).
* Exits use `tc.fetch_bars(symbol, "5m", "5d")`, or `"60d"` for items more than 4 days old.
  Yahoo keeps 60 days of 5-minute bars. Crypto: the row's exchange's 5-minute candles for the UTC
  days since the oldest open item (`"2d"` for today's; Binance pages 1,000 candles, Coinbase 300).
* Offline tests and demos: set `$DAYTRADER_FIXTURES`. Replays: set `$DAYTRADER_NOW` (naive = the
  command's market clock: ET, or UTC for a crypto ticket/trade or `--market crypto`; `Z` works).

```bash
python3 scripts/broker.py ticket --symbol ETH-USDT --qty 0.25 --type market --stop 3450 --target 3650
python3 scripts/broker.py positions --market crypto
python3 scripts/broker.py history --days 7 --market crypto --json
```
