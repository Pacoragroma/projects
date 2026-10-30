---
name: time-machine
description: >-
  Day-trading desk: the Time Machine. Before risking a dollar, replay the rest of today's session
  10,000 times from the stock's own 5-minute bars at the same clock times, plus a history check
  of past sessions entered at the same time, and show the honest odds that a trade hits its
  target first, its stop first, or neither by the close - with expected value in R, break-even
  win rate, trend check and a verdict (ODDS LEAN YOUR WAY / CLOSE TO A COIN FLIP / ODDS LEAN
  AGAINST YOU). Reads the saved trade plan or takes entry/stop/target; can draw an SVG and save
  the odds. Use when the user asks "what are the odds?", "test my trade", "plan a trade on TSLA
  and test it", "plan and test a trade", "will it hit my target or my stop first?", "backtest
  this setup", "simulate the rest of the day", "replay this 10,000 times", "is this trade worth
  taking?", "run the time machine on TSLA", or right after the exit skill sets a plan. Free
  Yahoo Finance data, no API key. Simulation, not a guarantee; not financial advice.
---
# Time Machine

"If I get filled at my entry now, how often does this stock reach my target before my stop?"
The Time Machine answers with the stock's own data: it stitches together 10,000 versions of the
rest of the session from the 5-minute bars of its last ~60 sessions at the same clock times,
walks each one bar by bar against your stop and target, and checks the answer against what
really happened on every one of those past days from the same time. It reports the odds, the
expected value per trade in R, and a plain verdict. Paper planning only.

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
python3 scripts/time_machine.py TSLA --entry 364.18 --stop 358.32 --target 370.04
python3 scripts/time_machine.py TSLA                          # levels from the saved plan (exit skill --save)
python3 scripts/time_machine.py TSLA --at 10:15               # as if filled at 10:15 ET
python3 scripts/time_machine.py TSLA --side short --entry 364.18 --stop 368.00 --target 356.50
python3 scripts/time_machine.py TSLA --paths 20000 --seed 7 --block 3 --sessions 59
python3 scripts/time_machine.py TSLA --detrend                # odds with the recent trend taken out
python3 scripts/time_machine.py TSLA --svg tsla_replays.svg   # draw 300 of the replays
python3 scripts/time_machine.py TSLA --save --json            # store the odds in plans/TSLA.json
```

Flags:
* `--entry/--stop/--target`: each defaults to the saved plan's value (`plans/<SYM>.json` in
  `$DAYTRADER_HOME`, default `~/.day-trader`). All three must end up known.
* `--side long|short`: default inferred from the levels (stop below entry = long).
* `--at HH:MM`: start time ET (09:30-15:55, rounded down to the 5-minute grid). Default: now,
  rounded down, if the market is open and today has bars; otherwise a full session from the
  09:30 open (the output says "market closed - simulating the next session from the open").
* `--paths 10000` (100-200,000), `--sessions 59` (most recent past sessions to learn from),
  `--block 3` (bars copied from one past day at a time), `--seed N` (default: from symbol + date,
  so reruns the same day give the same numbers).
* `--detrend`: make the trend-removed replays the headline (see the trend check below).
* `--svg FILE`: ~800x450 chart of 300 replays (green = hit target first, red = stop first, grey =
  open at close), the entry, dashed target/stop lines and the three percentages. Tell the user
  the file path; it is illustrative.
* `--save`: merge `time_machine {...}` (with the levels it was computed for) into the plan,
  including `display_pct {target_first, stop_first, open_at_close}`: the one-decimal odds exactly
  as printed, so risk and broker quote the same numbers. If the flags changed the plan's levels,
  `sizing`/`exit_plan` computed for the old levels are cleared - re-run them. `--json`:
  machine-readable output for chaining (same `display_pct`).

## How it works
* **Data:** Yahoo 5-minute bars for the last 60 days, regular session only. The library is the
  most recent past sessions before today (today's own session is used only once it is closed).
  Bars after "now" are never used, so replays with `$DAYTRADER_NOW` are honest.
* **Bar shapes:** for every past session and 5-minute slot (09:30 ... 15:55), close, high and
  low divided by the previous bar's close (the first bar uses its own open: no overnight gaps).
* **Replays:** each path starts at your entry at the start time and walks to the close. Every
  `--block` bars it picks a random past session and copies its next bars' shapes at the *same
  clock slots* - this keeps the open's big swings, the lunch lull and the closing push, plus
  short-term momentum inside a block. Half days only supply the slots they traded.
* **Outcome per path:** first bar where the low reaches the stop = STOP, the high reaches the
  target = TARGET (mirrored for a short). **Both inside one bar counts as STOP** (conservative;
  shown as "both in one bar"). Neither by the last bar = OPEN AT CLOSE, valued in R at the close.
* **Expected value** = P(target) x R:R - P(stop) x 1 + P(open) x average open R. Break-even
  needs more than 1/(1 + R:R) of *resolved* trades to hit the target.
* **History check:** every past session, entered at the same clock time at that day's price,
  with the stop and target at the same % distances, walked through the real bars.
* **Trend check:** the replays inherit the last ~60 days' average drift at each clock slot, which
  is mostly noise. The same replays are re-run with each slot's average drift removed (volatility
  kept); both answers are shown. The drift is measured where it decides the trade: from the start
  to the median time the replays reach the stop or target, in % and in R (vs the stop distance),
  plus the whole window to the close. `t` is the drift's t-statistic: |t| < 2 means
  indistinguishable from noise.
* **Verdict:** ODDS LEAN YOUR WAY (expected value > +0.1R and target% > stop%), ODDS LEAN AGAINST
  YOU (expected value < -0.1R), else CLOSE TO A COIN FLIP.
* **Limits:** fills assumed exactly at entry, stop and target (no slippage, gaps, commissions);
  60 days is a small sample (± after each odds line is simulation noise only - the real
  uncertainty is several points wider, as the output says); news days, earnings and halts are
  not modelled; exchange holidays are not modelled; Yahoo data can be delayed.
* Method details, edge cases and every JSON field: [reference.md](reference.md).

## Reading the output / how to present it to the user
* Lead with the three odds exactly as printed (HIT TARGET FIRST / HIT STOP FIRST / OPEN AT
  CLOSE), then expected value and the break-even line, then the history check, then the
  VERDICT. Use the words "replays" or "simulation", never "prediction".
* Always mention the trend check when a `-` note mentions "time-of-day drift" (the
  trend-removed replays differ by >= 5 points or >= 0.1R, or flip the verdict): the edge may
  just be the last two months' drift. Quote the note's own numbers (drift %, R, t); if it says
  the odds are "sensitive to small time-of-day drifts", say that rather than calling it a trend.
* If the history check and the replays disagree by more than ~10 points, say so - with ~59
  days, both are rough.
* Relay every `!` warning (price already through the stop/target, stale plan, unusually volatile
  day, stale or thin data) in plain words. `-` lines are context; always relay the one that says
  the saved entry is a trigger "from the finished <date> session" - those odds are for a replay,
  so re-plan from the next open before trading it.
* A good one-line summary: "In 10,000 replays from TSLA's last 59 sessions, the target came
  first 29% of the time, the stop 26%, and 45% were still open at the close: about +0.04R per
  trade - close to a coin flip."

## Next step on the desk
* **risk** skill: size the position from the stop (`risk.py size --symbol TSLA` reads the plan).
* **budget** check, then the **broker** skill's paper ticket - which waits for the user's yes.
* If the odds look poor, go back to the **exit** skill to rethink the stop/target, or the
  **entry** skill to wait for a better trigger - do not just widen the stop to improve the odds.

## Rules
* Not financial advice. These are simulations of past behaviour, not guarantees. Never say a
  trade "will" work or quote the odds as certain; always include that ± covers simulation noise
  only.
* Never invent or round away the numbers. Report what the script printed. Never re-run with
  different seeds or settings to shop for a better verdict; if you re-run, show both results.
* The verdict is about odds, not a recommendation to trade. A favourable verdict does not
  replace the risk and budget checks.
* Paper trading only. This skill places nothing; orders go through the broker skill and only
  after the user's explicit yes.
* If the script exits with code 2 ("data error: ..."), tell the user which host failed (it is in
  the message, usually `query1`/`query2.finance.yahoo.com`). In sandboxes the host may need to
  be allow-listed. Never invent prices, headlines, or odds to fill the gap. Exit code 1 means
  bad input or too little history: show the error and fix the flags or levels.
