---
name: risk
description: >-
  Day-trading desk: position sizing and pre-trade risk checks. Works out how many shares to trade
  so hitting the stop loses at most the risk budget (default 0.2% of a $25,000 paper account),
  capped by max position size and buying power, and prints "BUY 8 TSLA" (or "SELL SHORT")
  with dollars at risk, notional, R:R and what capped the size. Crypto (Coinbase prices) is sized
  in fractions, paper fees counted: "BUY 0.02946375 BTC-USD". Checks R:R below your minimum, a
  stop inside the noise, 0 shares, buying power, today's loss budget from the paper ledger, an
  open position, fees in R and a PDT heads-up. Use when the user asks "how many shares should
  I buy?", "size this trade", "position size with a stop at 358.32?", "paper trade BTC", "how much
  BTC-USD should I buy?", "how much should I risk?", "am I risking too much?", "risk $100 on
  this", "set my account to 30k / risk 0.5% per trade", "how much risk do I have open?", or after
  the exit skill sets the stop. Reads the saved plan. Paper only; not financial advice.
---
# Risk

The desk member who decides **how big** before anyone decides **whether**. Given an entry and a
stop (yours, or the saved trade plan's), it sizes the position so a stop-out costs at most your
risk budget, caps it by max position size and buying power, and runs the pre-trade checks. It
also holds the sizing settings (account size, risk per trade, max position, minimum R:R) and shows
today's open risk and loss budget from the paper ledger.

## Run
> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder (`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.

```bash
python3 scripts/risk.py size --entry 364.18 --stop 358.32                   # -> BUY 8 shares
python3 scripts/risk.py size --symbol TSLA                                  # levels from the saved plan
python3 scripts/risk.py size --symbol TSLA --entry 364.18 --stop 358.32 --target 370.04 --save
python3 scripts/risk.py size TSLA --stop 358.32                             # no plan: entry = current price
python3 scripts/risk.py size --entry 364.18 --stop 370.04 --risk-dollars 100    # stop above = short
python3 scripts/risk.py size --entry 364.18 --stop 358.32 --account 10000 --risk-pct 0.5
python3 scripts/risk.py size --entry 50 --stop 49.9 --max-position-pct 200 --leverage 4
python3 scripts/risk.py config                                              # show settings
python3 scripts/risk.py config --account 30000 --risk-pct 0.25 --max-position-pct 25 --min-rr 1.5
python3 scripts/risk.py show                                                # settings + today's open risk
python3 scripts/risk.py size --symbol TSLA --json                           # --json on any command
python3 scripts/risk.py size --symbol BTC-USD --entry 100000 --stop 98000 --target 104000   # fractional
python3 scripts/risk.py config --crypto-fee-pct 0.4                         # your Coinbase fee tier
python3 scripts/risk.py show --market crypto                                # today = the UTC day
```

`size` flags:
* `--symbol SYM` (or a bare `SYM`): reads `plans/SYM.json`. Flags override single plan levels.
* `--entry`, `--stop`, `--target`: prices. With no entry anywhere (no flag, no plan), the current
  Yahoo price is used. The stop always comes from you or the plan.
* `--side long|short`: default = the plan's side when any level came from the plan, else
  inferred (stop below entry = long, above = short).
* `--account N`, `--risk-pct X` (percent: 0.2 = 0.2%) **or** `--risk-dollars N`,
  `--max-position-pct X`: one-off overrides of the settings.
* `--leverage 1|2|4`: buying power = account × leverage. Default 1 (cash).
* `--save`: merge `sizing {...}` (and the levels) into the plan. Needs a symbol. A 0-share size is
  not saved (exit 1).

`config` flags: `--account`, `--risk-pct`, `--max-position-pct`, `--min-rr` (0 = off),
`--crypto-fee-pct` (paper fee per crypto fill, default 0.6). No flags = show. `show --market
crypto` counts today as the UTC day. Daily limits (max loss, open risk, trades, tilt guard) belong
to the **budget** skill.

Exit codes: 0 ok · 1 bad input or state · 2 data source down (only when the current price is
fetched). A blocked daily budget is reported, not an error: check JSON `budget.ok`.

## How it works
* **Shares** = floor(risk budget ÷ |entry − stop|). Risk budget = account × risk% (default
  $25,000 × 0.2% = $50) or `--risk-dollars`. Whole shares only, rounded down.
* **Caps** (smallest wins, reported as `capped_by`): `risk`; `max_position_pct` (notional ≤ max
  position % of the account, default 25%); `buying_power` (notional ≤ account × leverage).
  Example: 364.18 / 358.32 → $5.86/share → $50 / $5.86 = 8 shares ($46.88 at risk, $2,913 notional).
* **R:R** (with a target) = reward ÷ risk per share, plus reward in $ and break-even win rate
  1/(1+R:R).
* **Checks** (`warnings`): 0 shares first, naming every cap that allows 0 and what 1 share would
  need (`--risk-dollars`/`--risk-pct`, a tighter stop, `--max-position-pct`, `--leverage`); R:R below
  `min_reward_risk`; stop < 0.1% of price (noise); buying-power cap;
  notional above the account (margin); 4x leverage on an account under $25,000; short with 1x
  (cash accounts cannot short); more than 2% of the account at risk; a risk % below 0.01 (typed a
  fraction?); the daily budget (the shared budget check: worst-case loss today, open risk, trades
  per day, tilt guard) with the most shares that still fit. Like the broker, it counts today's
  working limit orders as open risk and a trade. Then: an open position, working order or
  pending ticket in the same ticker; a saved plan from an earlier day; and under $25,000, "pattern-
  day-trader rules may limit day trades depending on your broker and current FINRA rules" (a
  heads-up, not a block).
* **Plan merge:** a saved target is only reused if it was set for the same side and entry. When
  `--save` changes the plan's levels, the `exit_plan` and `time_machine` sections (and a target
  that no longer applies) are cleared, because they were computed for the old levels.
* **Crypto** (`BTC-USD`, ...; Coinbase prices, 24/7, the UTC day): the size is fractional, floored
  to the product's size step (Coinbase `base_increment`, else 1e-8), and the paper fees of the
  entry and of the stop exit count toward the loss: units = budget ÷ (|entry − stop| + fee% ×
  (entry + stop)). `max_loss` = `risk_used` + `fees_at_stop` stays within the budget, and that is
  the risk the budget and the broker count. R is still the stop distance: the card says
  "round-trip fees ≈ 2.39R at these levels" and the R:R after fees, and warns when fees reach 1R
  or push the R:R after fees under your minimum. Spot crypto has no margin (leverage 1x unless you
  pass it), cannot be shorted on Coinbase, and has no PDT rule. Off-tick prices get a note.
* **Limits:** the stop is assumed to fill at its price. Gaps, halts and slippage can lose more.
  Stock commissions and borrow fees are not included. The account size is what you set; nothing
  reads a real brokerage account. PDT and margin rules vary by broker and can change.

## Reading the output / how to present it to the user
* Lead with the order line as printed (`BUY 8 TSLA @ 364.18 · STOP 358.32 · TARGET 370.04 ·
  PAPER`), then $ at risk, notional and % of account, R:R, and what set the size. For crypto also
  the fees: $ at the stop, round-trip fees in R and the R:R after fees - never hide them.
* Relay every `!` line (JSON `warnings`) in plain words. `-` lines (`notes`) are context: where
  the levels came from, short-selling needs, and the time-machine odds when the plan's odds were
  computed for exactly these levels (printed as the time machine printed them, from its
  `display_pct`). Otherwise a note says "not tested by the time machine for these levels (run
  time_machine.py SYM --save)": relay it and offer that command before a ticket.
* If the daily budget says BLOCKED, say so first: the broker will refuse the ticket. Offer the
  "at most N shares fit" figure or waiting until tomorrow. Do not suggest raising limits to get
  around it.
* JSON (`size`): `symbol, side, action, shares, order, entry, stop, target, risk_per_share,
  stop_pct, risk_budget, risk_budget_source, risk_pct, risk_used, risk_used_pct, notional,
  position_pct, account, max_position_pct, max_notional, leverage, buying_power, capped_by, limits
  {risk, max_position_pct, buying_power} (shares each cap allows), rr, reward_per_share,
  reward_at_target, breakeven_win_rate_pct, budget {ok, reasons, loss_budget_left,
  loss_budget_left_after, max_risk_now, max_shares_now, open_risk (incl. working orders),
  working_order_risk, working_orders, open_positions, realized_pnl, trades_today, loss_streak,
  limits}, warnings[], notes[], sources, saved, plan_file, dropped_sections, save_error, market,
  max_loss`; crypto adds `unit, step, tick, fee_pct, fees_at_stop, loss_per_unit, fees_r,
  fees_at_target, rr_after_fees` (`shares` is then a fractional size).
* `show` JSON: `settings, risk_per_trade_dollars, max_position_dollars, warnings, today {...},
  next_trade {risk, ok, reasons}, open_positions[], orders[], sized_plans[]`.

## Next step on the desk
* **budget** skill (`budget.py check --risk <risk_used>`; crypto: `--symbol BTC-USD --risk
  <max_loss>`) before any ticket.
* **broker** skill: a paper ticket for exactly `shares`, which waits for the user's explicit yes.
* No stop yet? Use the **exit** skill first. Want the odds for these levels? **time-machine**.

## Rules
* Not financial advice. Sizing limits the loss **if the stop fills**. It does not make a trade
  good. Never call a size "safe", and never present R:R or break-even rates as odds.
* Paper only. This skill places nothing. Orders go through the broker skill, only after the user's
  explicit yes in a later message.
* Never raise the risk %, the account size or the leverage on the user's behalf to make a size
  "work", or to get past a blocked budget. Show what it would take and let the user decide. Use
  `--leverage 2|4` only if the user says their account has that margin.
* Report only what the script printed. Never invent prices, account sizes or budget numbers.
* If the script exits with code 2 ("data error: ..."), tell the user which host failed (it is in
  the message, usually `query1`/`query2.finance.yahoo.com`, or `api.exchange.coinbase.com` for
  crypto). In sandboxes the host may need to be allow-listed. Never invent prices, headlines, or odds to fill the gap. Sizing still works
  offline when the user gives `--entry` and `--stop`. Exit code 1 means bad input: show the
  error and ask.
