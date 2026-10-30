---
name: entry
description: >-
  Day-trading desk: watch a stock's 5-minute chart bar by bar and say the moment an entry setup
  triggers - opening-range breakout (ORB), VWAP reclaim, a break of a price level you name, or a
  break of the previous day's high/low - with the entry price, a suggested stop, and what price has
  done since. Use when the user asks "did TSLA break its opening range", "is the ORB long on yet",
  "tell me when NVDA reclaims VWAP", "watch AMD for a break of 165", "let me know when it takes out
  yesterday's high", "wait for the entry", "what would the 15-minute ORB have done on Tuesday", or
  wants a trigger before setting a stop and target. Free Yahoo Finance 5-minute data, no API key.
  Reports mechanical triggers, not advice; paper trading only.
---
# Entry

The desk member who stares at the 5-minute chart so the user does not have to. Pick a setup
(default: the 15-minute opening-range breakout), and it walks today's completed 5-minute bars in
order and reports the first one that triggers: when, which side, at what price, the level it broke,
a suggested stop, and how far price has moved since. If nothing has triggered it says WAITING and
exactly what would trigger it; past the cutoff it says EXPIRED.

## Run
> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder (`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.

```bash
python3 scripts/entry.py TSLA                                    # ORB(15), either side, cutoff 11:30
python3 scripts/entry.py TSLA --setup orb --or-minutes 30 --side long --min-rvol 1.5
python3 scripts/entry.py NVDA --setup vwap --cutoff 15:00
python3 scripts/entry.py AMD --level 165 --side long --cutoff 15:55 # --level implies --setup level
python3 scripts/entry.py AAPL --setup pdh --cutoff 15:55            # long only (pdl = short only)
python3 scripts/entry.py TSLA --watch --interval 60 --max-minutes 30 --save
python3 scripts/entry.py TSLA --date 2026-10-29 --json              # replay a past session
```

Flags: `--setup orb|vwap|level|pdh|pdl` (default `orb`), `--side long|short|both` (default `both`
for orb/vwap/level = whichever triggers first; pdh is long-only, pdl short-only), `--or-minutes N`
(orb only, multiple of 5, default 15), `--level PRICE`, `--cutoff HH:MM` (ET, default 11:30: no
trigger by then = EXPIRED), `--min-rvol X` (volume filter, off by default), `--date YYYY-MM-DD`
(replay, last ~60 days), `--watch` with `--interval SECONDS` (default 60, min 15) and
`--max-minutes M` (default 120), `--save` (write a trigger into the saved plan), `--json`.

The 11:30 default cutoff suits the morning ORB. For a VWAP, level or PDH/PDL watch, or any request
made after 11:30, pass a later cutoff (e.g. `--cutoff 15:55`); otherwise the result is EXPIRED at
once (the output adds a note when the default cutoff caused the expiry).

`--watch` prints one status line per check and the full card at the end; it stops on TRIGGERED,
EXPIRED, `--max-minutes`, or Ctrl-C (exit 130). If the first check cannot reach Yahoo it exits 2 at
once; after that it rides out up to 2 failed checks in a row. It runs in the foreground, so in a chat keep
`--max-minutes` inside your command timeout (or run it in the background), or simply re-run the
one-shot check when the user asks again. With `--json`, `--watch` prints one JSON object per line
per check, then a last one with `"final": true`.

## How it works
- Data: Yahoo 5-minute bars (`5d`; `60d` for a past `--date`), regular session 09:30-16:00 ET,
  plus the previous session for PDH/PDL. Bars are snapped to the 5-minute grid.
- **Completed bars only.** A bar counts once its 5 minutes are over (start + 5 min <= now); the bar
  still forming never triggers. `triggered_at` is the trigger bar's *close* time (10:10 bar ->
  10:15). Entry = that bar's close. Only bars closing by `--cutoff` can trigger.
- `orb`: opening range = high/low of the first `--or-minutes` (09:30-09:45 by default). Long = first
  later bar that closes above the OR high, short = closes below the OR low. Stop = the other side
  of the range (OR mid also reported). If the stock printed nothing in the window (late open/halt),
  the range is taken from its first N minutes of trading, with a note.
- `vwap`: session VWAP (typical price x volume, from 09:30). Long = a bar closes above VWAP right
  after a bar that closed at/below it; short = mirror. Earliest trigger: the 3rd bar. Stop = lower of
  the trigger bar's low and VWAP (short: higher of its high and VWAP).
- `level`: long = a close above the level after price has been at/below it (the previous close
  counts, so a gap through the level triggers on the first bar's close); short = mirror. If price
  has been above the level all along, a long is *not armed* until it closes back at/below it.
- `pdh` / `pdl`: a close above the previous session's high / below its low. Stop for level, pdh
  and pdl = the trigger bar's low (short: high), or the level itself if the bar never traded back
  through it.
- `--min-rvol X`: the trigger bar's volume must be >= X times the average of up to 12 earlier bars of
  the session (needs at least 3; earlier bars cannot qualify). Breaks that fail it are listed as
  "skipped" and the scan continues to the next bar. Early bars are naturally heavy, so 1.5 is strict.
- Status: TRIGGERED, WAITING, or EXPIRED (cutoff reached, or the session ended - half days come from
  Yahoo's trading periods). Exchange holidays are not modelled: a weekday holiday reads WAITING
  with a "no bars yet" note. On a weekend it replays the last session and says so.
- Limits: close-based 5-minute signals lag intrabar moves; Yahoo data can be delayed or revised;
  no pre-market data (the OR and VWAP start at 09:30); the suggested stop is structural, not sized.

## Reading the output / how to present it to the user
Example: `TSLA 5m · ORB(15) · TRIGGERED 10:15 ET · LONG @ 364.18 (OR 358.32–363.10)`, then the
trigger bar (OHLC, volume, rvol), `suggested stop 358.32 (OR low) · risk 5.86/share (1.61%)`, a
"since trigger" line (last price, move in $ and R, high/low, whether the stop was hit - the bar still
forming counts here, since its prices already traded; it never counts for the trigger), a context line
(VWAP, day range, PDH/PDL, previous close), and notes.
- Lead with status, side, entry, time, and the level broken. Quote the numbers as printed.
- TRIGGERED a while ago: say how long ago and where price is now. If a note says the stop was
  already hit, the trigger failed - say so plainly. If price is >= 1R past entry, warn that
  chasing gives a worse entry than the signal.
- WAITING: give the exact trigger ("a 5-minute close above 363.10, 0.06% away") and the cutoff.
  "not armed" means price must first close back through the level.
- EXPIRED: no setup today on those terms; do not stretch the rules to find one.
- A `replay` result is history (what the setup did that day), not a live signal.
- JSON fields: `symbol, date, replay, as_of, setup, label, side, status, triggered_at, entry,
  suggested_stop, stop_basis, risk_per_share, risk_pct, reference {or_high, or_low, or_mid, ... |
  vwap | level | pdh | pdl}, bars_checked, last_price, trigger_bar, rvol, since_trigger {minutes,
  last, move, move_r, high, low, max_favorable_r, max_adverse_r, stop_hit, stop_hit_at,
  stop_hit_in_forming_bar},
  watching [{side, level, armed, distance, distance_pct}], rejected [], context {vwap, high, low,
  pdh, pdl, prev_close}, cutoff, expired_reason, notes [], saved`.

## Next step on the desk
- `--save` writes `symbol, side, entry, stop (= suggested stop), setup, entry_signal {...}` into
  `plans/<SYM>.json`. If the saved plan had a target, exit plan, time-machine odds or sizing for a
  different entry, those are cleared (set to null) because they no longer apply - the output says so.
- Then the **exit** skill sets stop and target before entering (`exit_plan.py TSLA` reads the saved
  entry and side; add `--method manual --stop <suggested stop>` to keep this stop).
- Then **time-machine** for the honest odds, **risk** to size it, **budget** to check the day's
  limits, and the paper **broker** ticket, which waits for the user's explicit yes.

## Rules
- Not financial advice. A trigger is a mechanical chart condition, not a prediction or a
  recommendation; never say "buy now" or promise follow-through. Many breakouts fail.
- Never claim a trigger the script did not print, and never call a trigger from the live price
  of the bar still forming.
- Paper trading only. This skill places nothing; `--save` only writes a plan file. Orders go
  through the broker skill and only after the user's explicit yes.
- Exit code 1 = bad input (the message says what to fix, e.g. `--setup level needs --level PRICE`).
- If the script exits with code 2 ("data error: ..."), tell the user which host failed (it is in
  the message, e.g. `query1.finance.yahoo.com`) - in sandboxes the host may need to be
  allow-listed. Never invent prices, headlines, or odds to fill the gap.
