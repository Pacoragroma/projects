---
name: exit
description: >-
  Day-trading desk: set the stop-loss, profit target and time stop BEFORE entering a trade
  ("THE PLAN"). Builds the stop from today's 5-minute bars (1.5 x ATR by default, or below the
  recent swing low / above the swing high), a fixed percent, or the user's own price; puts the
  target at a reward:risk multiple (default 2R) or a given price; and reports risk and reward per
  share, R:R, the break-even win rate, stop distance in ATRs and % of price, a flat-by-15:55 time
  stop and a move-to-breakeven rule, with warnings when the stop is too tight or too wide. Saves to
  the shared trade plan. Use when the user asks "where should my stop go?", "set a stop and target
  for TSLA", "what's my exit plan?", "give me a 2R target from 364.18", "is my stop too tight?",
  "plan the trade before I get in", "plan a trade on TSLA and test it", "plan and test a trade",
  "where do I take profit?", "stop 358.32, what's my R:R?", or right after the entry skill triggers. Free Yahoo Finance data, no API key. Not financial advice.
---
# Exit Plan

Decide how you get out before you get in. Given a ticker and an entry price, this desk sets the
stop, the target and the time stop, and shows what the trade risks and needs: risk per share,
reward:risk, the win rate you need just to break even, and how the stop compares with the
stock's normal 5-minute swings (ATR). The result is "THE PLAN" card. With `--save`, the plan is
merged into the shared trade plan that time-machine, risk, alerts and broker read.

## Full desk run ("plan a trade on SYM and test it")

When the user asks to plan and test a trade end to end, run the desk in this order. Every step reads
and updates the shared plan (`plans/<SYM>.json`). Run sibling skills' scripts from their own folders.

1. **scanner** — `python3 scripts/scan.py --symbols SYM`: any setup on the daily chart?
2. **news** — `python3 scripts/news.py SYM`: anything that could move it today?
3. **entry** — `python3 scripts/entry.py SYM --save`: if TRIGGERED, the trigger becomes the entry
   and the opening-range stop.
4. **exit** — after a trigger: `python3 scripts/exit_plan.py SYM --method manual --rr 1 --save`
   (keep the entry's stop, 1R target). No trigger: `python3 scripts/exit_plan.py SYM --save`
   (current price, ATR stop, 2R target).
5. **time-machine** — `python3 scripts/time_machine.py SYM --save`: the honest odds for those levels.
6. **risk** — `python3 scripts/risk.py size --symbol SYM --save`: how many shares.
7. **budget** — `python3 scripts/budget.py check --symbol SYM`: exit 3 means BLOCKED; stop there.
8. **broker** — `python3 scripts/broker.py ticket --symbol SYM`: show the ticket and **stop**. Confirm
   only after the user's explicit yes in a later message.

Skip any step the user rules out. Show the plan card, the Time Machine verdict and the ticket together,
and say plainly that it is paper trading and not financial advice.

## Run
> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder (`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.

```bash
python3 scripts/exit_plan.py TSLA --entry 364.18                     # 1.5 x ATR stop, 2R target
python3 scripts/exit_plan.py TSLA --entry 364.18 --rr 1              # 1R target
python3 scripts/exit_plan.py TSLA --entry 364.18 --method structure --lookback 12
python3 scripts/exit_plan.py TSLA --entry 364.18 --pct 1.5           # stop 1.5% away
python3 scripts/exit_plan.py TSLA --entry 364.18 --stop 358.32 --target 370.04
python3 scripts/exit_plan.py TSLA --side short --entry 364.18 --atr-mult 2
python3 scripts/exit_plan.py TSLA --method manual --save             # reuse the saved plan's entry + stop
python3 scripts/exit_plan.py TSLA --entry 364.18 --time-stop 11:30 --save --json
```

Flags:
* `--entry X`: planned fill price. Default: the saved plan's entry (e.g. from the entry
  skill), else the current price.
* `--side long|short`: default is the saved plan's side. With `--stop` and no plan, a stop
  above the entry means short. Otherwise long.
* `--method atr|structure|percent|manual`: default `atr`. `--stop` implies `manual`, `--pct`
  implies `percent` and `--lookback` implies `structure`. A flag that belongs to another method
  is an error, never silently ignored.
* `--atr-mult 1.5` (atr), `--lookback 12` bars (structure), `--pct 1.0` (percent),
  `--stop X` (manual). `--method manual` without `--stop` reuses the saved plan's stop.
* Target: `--rr 2.0` (default) or a fixed `--target X`. You cannot pass both.
* `--time-stop HH:MM`: be flat by this time ET. Default 15:55, allowed 09:35-16:00.
* `--save`: merge `symbol, side, entry, stop, target, exit_plan {...}` into
  `plans/<SYM>.json` in `$DAYTRADER_HOME` (default `~/.day-trader`). `--json`: machine-readable.

## How it works
* **Data:** Yahoo Finance 5-minute bars for the last 5 days, regular session only. ATR uses
  only *completed* bars. The structure low/high also counts the bar that is still forming.
  Bars after "now" are never used. In a replay with `$DAYTRADER_NOW`, the bar in progress
  already holds its full 5 minutes, so it is dropped too.
* **ATR(14):** Wilder's ATR of today's completed 5-minute bars. If today has fewer than 14
  (before about 10:40 ET, pre-market, weekends), it is the average true range of the last 14
  completed bars across sessions. The first bar of each session counts only its own high-low,
  so an overnight gap does not inflate it. The output says which bars were used.
* **Stops:** `atr` = entry ∓ mult × ATR. `structure` = lowest low (long) / highest high (short)
  of the last `--lookback` bars, ∓ 0.1 ATR buffer. If that is not below (above) the entry, it
  falls back to the ATR stop and says so. `percent` = entry × (1 ∓ pct/100). `manual` = your price.
* **Target** = entry ± rr × |entry − stop|. Prices are rounded to cents ($0.0001 below $1),
  half-up. Structure stops round away from the entry so they stay beyond the swing.
  The R:R shown is computed from the rounded prices.
* **Break-even win rate** = 1 / (1 + R:R), before commissions and slippage.
* **Checks** (warnings):
  * stop < 0.5 ATR ("likely to be wicked out") or > 4 ATR ("very wide");
  * R:R below your `min_reward_risk` setting;
  * target farther than the stock's average full-session range;
  * last price already through the stop (the plan only works as a pending order) or past the
    target. With the market closed it is the last close, so check the pre-market price;
  * a saved plan from an earlier day (its entry, stop or side reused);
  * a saved entry that is an entry-skill trigger from a session that is already over (`entry.py
    --save` after the close or for a past `--date`, or a trigger from an earlier day): the warning
    repeats the entry skill's note, e.g. "trigger is from the finished 2026-10-30 session - re-plan
    from the next open";
  * stale, halted or holiday data;
  * less than 30 minutes, or no time at all, before the time stop.
* **Time stop:** flat by 15:55 ET by default. On early-close days (from Yahoo's trading period)
  it moves to 5 minutes before the close.
* **Management:** once +1R trades, move the stop to breakeven. There is no breakeven step when
  the target is at +1R or closer. Never widen the stop.
* **Without data:** `percent` and `manual` still work when Yahoo is unreachable, provided you
  give the entry. They print a warning that the ATR checks were skipped. `atr` and `structure`
  exit 2.
* **Limits:**
  * ATR measures recent noise, not direction.
  * A stop order is not a guaranteed price: gaps, halts and fast markets can fill beyond it.
  * Exchange holidays are not modelled.
  * Yahoo data can be delayed.

## Reading the output / how to present it to the user
* Lead with the card's two top lines exactly as printed, e.g.
  `THE PLAN — TSLA LONG · manual stop` and
  `ENTRY 364.18 / STOP 358.32 / TARGET 370.04 / R:R 1.0`.
  Then give risk/share, stop % and ATRs, the break-even win rate, the time stop and the
  management rule.
* Always relay every `!` warning (JSON `warnings`) in plain words. They are the point of planning
  first. `-` lines (JSON `notes`) are context: which ATR was used, structure fallback, inferred
  side, early close. Mention them when relevant.
* Explain break-even simply: "at 2R you need to win more than 33% of trades like this to break even".
* JSON fields:
  * levels and sizing per share: `symbol, side, entry, stop, target, method, method_requested,
    rr, rr_requested, risk_per_share, reward_per_share, stop_pct, target_pct,
    breakeven_win_rate_pct`;
  * ATR: `atr, atr_info {source: today|recent, bars, today_bars, first, last}, stop_atr,
    target_atr, avg_session_range`;
  * method detail: `params, structure`;
  * timing and management: `time_stop, minutes_to_time_stop, market_open, management
    {breakeven_at, rule}`;
  * messages: `warnings[], notes[]`;
  * provenance: `entry_source, stop_source, side_source, last_price, as_of`;
  * save result: `saved, plan_file, dropped_sections`.
* With `--save`, "Cleared time_machine/sizing" means those plan sections were computed for the
  old levels and were removed. Re-run time-machine (and risk) for the new levels.

## Next step on the desk
* **time-machine** skill: replay the rest of the session thousands of times to get the honest
  odds of target-first vs stop-first for these exact levels. It reads the saved plan.
* **risk** skill: size the position from this stop (`risk.py size --symbol TSLA` reads the
  plan).
* **alerts** skill (`alerts.py from-plan TSLA`): price alerts at entry, stop and target.
* Then **budget** check and the paper **broker** ticket, which needs the user's explicit yes.

## Rules
* Not financial advice. A plan is a set of exits, not a prediction. Never say the target "will"
  be hit, and never present ATR, R:R or the break-even rate as odds. For odds, use time-machine.
* Set the exits before the entry. After entry, the stop may only move toward profit (the
  breakeven rule), never wider. If the user wants to widen a stop mid-trade, say plainly that
  this breaks the plan.
* Never invent prices, ATRs or levels. Report only what the script printed. If the user's
  numbers fail validation (e.g. a long stop above the entry), show the error and ask.
* Paper trading only. This skill places nothing. Orders go through the broker skill and only
  after the user's explicit yes.
* If the script exits with code 2 ("data error: ..."), tell the user which host failed (it is in
  the message, usually `query1`/`query2.finance.yahoo.com`). In sandboxes the host may need to
  be allow-listed. Never invent prices, headlines, or odds to fill the gap. A `percent` or
  `manual` plan with an explicit `--entry` still works offline, without the ATR checks; say
  that the checks were skipped. Exit code 1 means bad input: fix the flags or levels shown in
  the error.
