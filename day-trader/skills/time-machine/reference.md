# Time Machine reference

Details behind `scripts/time_machine.py`. Load this when the user asks how the odds are made,
when an edge case shows up in the notes, or when you chain the JSON into another skill.

## Method, step by step

1. **Bars.** `tc.fetch_bars(SYM, "5m", "60d")`, regular session (09:30-16:00 ET) only, grouped by
   ET date. A repeated live bar (Yahoo sometimes sends the current bar twice, or adds a separate
   row stamped with the last trade time, e.g. 10:17:43, that may carry only the latest price) is
   merged into its 5-minute slot: first open, highest high, lowest low, last close.
2. **Library.** Past sessions = dates before today. Today's session joins only after today's
   close (then the replays are for the next session). Sessions with under 12 bars (1 hour) are
   skipped as glitches; fewer than 5 usable sessions is an error (exit 1). The most recent
   `--sessions` (default 59) are used.
3. **Shapes.** For session *j* and slot *k*: `r_close = close/prev_close`, `r_high = high/prev_close`,
   `r_low = low/prev_close`, where prev_close is the previous bar's close (the first bar of the
   day uses its own open). A missing slot *between* a day's first and last bar (no trades in
   those 5 minutes) becomes a flat bar `(1, 1, 1)` and the next bar carries the move. Slots before
   a day's first bar or after its last (late open, half day) do not exist for that day.
4. **Start.** `--at`, else now rounded down to the 5-minute grid when the market is open and
   today has bars, else 09:30. The fill is assumed at the start of that 5-minute bar at the
   entry price. On early-close days that Yahoo reports (`tradingPeriods` /
   `currentTradingPeriod`), today's replays end at that close.
5. **Replays (slot-aligned block bootstrap).** A path starts at 1.0 (= the entry). At the start
   of every block (`--block`, default 3 bars = 15 minutes) it draws a past session uniformly
   among those that have a bar at the current slot and copies that session's shapes for the
   next block of slots. If the session ran out of bars mid-block (half day), it switches to a
   random session that has the slot. Per bar: `high = prev*r_high`, `low = prev*r_low`,
   `close = prev*r_close`.
6. **Outcome.** Long: `low <= stop` -> STOP; `high >= target` -> TARGET; checked in that order, so
   a bar touching both is a STOP (and counted in `ambiguous`). Short: mirrored (run in 1/price
   space with the same code). A touch exactly at the level counts. No hit by the last bar ->
   OPEN AT CLOSE with R = (last close - entry) / (entry - stop) for a long (mirrored for a short).
7. **Statistics.** `target_first_pct + stop_first_pct + open_at_close_pct = 100` (largest-remainder
   rounding). `*_se` = sqrt(p(1-p)/paths) in percentage points: Monte Carlo noise only.
   `expected_r` = (targets x R:R - stops + sum of open R) / paths. Minutes to resolution are to the
   end of the bar where the level was hit.
8. **History check.** For each library session: entry = close of the last bar before the start
   slot (or the 09:30 open), stop/target at the plan's % distances, real bars walked to the
   replay end with the same rules. Days with no bar at or after the start, or that only began
   trading after it (late open after a halt, or a partial first day of Yahoo's window), are
   `skipped` - never entered at a later clock time.
9. **Trend check.** `trend.avg_move_pct` = the library sessions' average % move from the start
   slot to the end; `t_stat` = mean / (stdev / sqrt(n)). `trend.alt` = the same replays (same
   seed) on the other library: trend removed (each slot's shapes divided by that slot's average
   `r_close`, so the average bar has no drift but keeps its range) - or, with `--detrend`, trend
   left in. A note is added when the two verdicts differ, or the expected value differs by
   >= 0.1R, or target-first by >= 5 points.
10. **Seed.** Default = first 48 bits of sha256("SYMBOL|YYYY-MM-DD"). Same symbol, same day, same
    flags -> the same numbers. The SVG's 300 drawn paths are the first 300 replays; their
    continuation after an exit uses a separate generator, so `--svg` never changes the odds.

## Why these choices
* **Same clock slots:** intraday volatility is U-shaped (big at the open and close, quiet at
  lunch). Sampling bars from any time of day would make a 10:00 entry look calmer and a 12:30
  entry look wilder than they are.
* **Blocks:** 5-minute returns are mildly autocorrelated (momentum bursts); copying 3 bars at a
  time keeps that texture. `--block 1` destroys it, larger blocks keep more but resample fewer
  distinct days.
* **Stop on ambiguous bars:** inside one 5-minute bar we cannot know which level traded first;
  assuming the worse case keeps the odds from flattering tight plans. A high `ambiguous_pct`
  (> 5%, noted in the output) means the levels are tight for 5-minute data.
* **Trend check:** with ~59 days, the average daily drift has a standard error of roughly the
  daily range / 8. Replays that inherit it can tilt the odds by 10+ points on a stock that simply
  rallied lately. Showing both answers keeps that visible.

## Warnings and notes you may see
* `the last price X is already through your stop` / `past your target` - today's price has moved;
  the odds only describe a fill at the entry.
* `the entry X is more than 1R from the last price` - a pending order; the odds assume the fill.
* `today is unusually volatile` (today's range so far >= 2x a typical day's by the start time) -
  replays come from ordinary days and likely understate how fast a level gets hit. `unusually
  quiet` (<= 0.5x) - the opposite.
* `the latest 5-minute bar started at ...` - stale data: halt, early close or delayed feed.
* `the saved plan is from <date>` - levels may be stale.
* `only N past sessions` (< 20) - rough odds. `newest 5-minute data is from ...` (> 7 days) -
  halted/delisted/stale.
* `past sessions only have bars until HH:MM` - the replays stop early (only half days at that time).

## JSON fields (`--json`)
* Plan: `symbol, side, entry, stop, target, rr, stop_pct, target_pct, levels_source{entry,stop,
  target: flag|saved plan}, side_source`.
* Window: `start, end, start_slot, end_slot, minutes_simulated, end_capped, mode (now|at|open),
  session (today|next), market_open, last_price, as_of`.
* Sample: `paths, block, seed, sessions_used, sessions_at_start, sessions_requested,
  first_session, last_session`.
* Odds: `counts{target,stop,open,ambiguous}, target_first_pct, stop_first_pct, open_at_close_pct,
  ambiguous_pct, target_first_se, stop_first_se, open_at_close_se, avg_r_open, expected_r,
  breakeven_win_rate_pct, resolved_target_share_pct, median_minutes_to_resolution,
  median_minutes_to_target, median_minutes_to_stop, day_sample_uncertainty_pct`.
* `history{sessions, target, stop, open, ambiguous, skipped, target_pct, stop_pct, open_pct,
  avg_r_open, expected_r, results[{date, entry, outcome, at, r}]}`.
* `trend{sessions, avg_move_pct, t_stat, detrended, alt{target_first_pct, stop_first_pct,
  open_at_close_pct, expected_r, verdict}}`, `today_range_ratio`.
* `verdict, verdict_notes[], warnings[], notes[], svg, saved, plan_file, dropped_sections[],
  disclaimer`.

## Saved plan section (`--save`)
`plans/<SYM>.json` gets `time_machine{side, entry, stop, target, rr, start, mode,
target_first_pct, stop_first_pct, neither_pct (= open_at_close_pct), open_at_close_pct,
ambiguous_pct, expected_r, avg_r_open, paths, sessions, block, seed, detrended, verdict,
history{sessions,target,stop,open}, as_of}`. Readers should check its levels still match the
plan's before quoting it.
