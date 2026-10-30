---
name: alerts
description: >-
  Day-trading desk: price alerts so the user does not have to stare at charts. Set alerts when a
  stock trades above or below a price, moves a set percent (from now or from yesterday's close), or
  crosses VWAP; turn a saved trade plan's entry, stop and target into alerts in one step; then check
  them or watch them on a loop. Touches between checks are caught from the 5-minute bars' highs and
  lows. Use when the user says "alert me if TSLA hits 370", "tell me when NVDA breaks 180", "ping me
  if it drops below my stop", "let me know if AMD moves 3%", "watch for a VWAP reclaim", "set alerts
  for my plan", "did any of my alerts go off?", "what alerts do I have?", "remove the TSLA alerts",
  or "watch my levels while I'm away". Free Yahoo Finance data, no API key. Alerts only notify -
  nothing here places an order; paper trading only.
---
# Alerts

The desk member who watches the price levels. Each alert is stored in `alerts.json`; `check` downloads
each symbol's 5-minute bars once and fires every alert whose condition was met **since the alert was
created**, using bar highs/lows so a spike between two checks still counts. `watch` repeats the check
on a timer and prints alerts as they fire.

## Run
> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder (`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.

```bash
python3 scripts/alerts.py add TSLA --above 370.04 --note "target"   # trades at or above
python3 scripts/alerts.py add TSLA --below 358.32 --once             # one-shot: switches off after firing
python3 scripts/alerts.py add NVDA --pct-move 3                      # 3% either way from the price now
python3 scripts/alerts.py add NVDA --pct-move -2 --ref prev-close    # down 2% on the day (+2 = up only)
python3 scripts/alerts.py add AMD --pct-move 1.5 --ref 160           # band around a price you choose
python3 scripts/alerts.py add AMD --vwap-cross                       # 5m close across VWAP, either way
python3 scripts/alerts.py add AMD --vwap-cross above                 # only reclaims (below = only losses)
python3 scripts/alerts.py from-plan TSLA                             # entry/stop/target of the saved plan
python3 scripts/alerts.py from-plan TSLA --levels stop,target --repeat
python3 scripts/alerts.py list
python3 scripts/alerts.py list --symbol TSLA --active
python3 scripts/alerts.py check
python3 scripts/alerts.py check --symbol TSLA,NVDA --json
python3 scripts/alerts.py watch --interval 60 --max-minutes 30 --bell
python3 scripts/alerts.py watch --quiet --json
python3 scripts/alerts.py remove ALR-20261030-001 ALR-20261030-002
python3 scripts/alerts.py remove --symbol TSLA
python3 scripts/alerts.py remove --fired
python3 scripts/alerts.py remove --all
```

Symbols are Yahoo tickers (`BRK.B`, `^GSPC` for an index). `add` takes exactly one of `--above
PRICE`, `--below PRICE`, `--pct-move PCT` or `--vwap-cross [any|above|below]` (put it after the
symbol), plus `--note TEXT`, `--once`, `--rearm-pct PCT` (default 0.25), and for `--pct-move` only
`--ref PRICE|prev-close`. `add` and `from-plan` download the current
price first (to arm the alert and warn if price is already past the level), so they need data too.
`from-plan` alerts are one-shot unless `--repeat`; re-running it replaces that symbol's earlier plan
alerts for the same levels. Every command takes `--json`.

`watch` checks every `--interval` seconds (default 60, min 15) for up to `--max-minutes` (default
390, max 600). It stops early when no active alerts are left or the market closes (16:00 ET); started
while the market is closed (evening, weekend) it runs one check and stops. It runs in the foreground:
in a chat keep `--max-minutes` inside your command timeout or run it in the background, and remember
the user is only notified while something is reading its output. `--bell` rings the terminal bell on
each fire; without `--quiet` it also prints a status line per check and each new warning once (stale
data, a symbol failing, plan changed); `--quiet` prints only fires and the final summary; with `--json`
it prints one JSON object per check, then one with `"final": true`. Ctrl-C stops it (exit 130). A data
failure on the first check exits 2 at once; later, a failing check is retried, and 3 in a row end the
watch (final summary with `"stopped": "data error"`, then exit 2).

## How it works
- **Data:** one Yahoo `5m` chart request per symbol per check (`5d` range; `1mo`/`60d` when an alert
  has not been checked for longer), regular session 09:30-16:00 ET only, plus Yahoo's last price folded
  into the bar still forming. Bars are snapped to the 5-minute grid and rounded to 4 decimals.
- **Only post-creation prices count.** Each alert keeps a cursor (bars already processed are not
  re-read). The bar the alert was created in may have made its high/low earlier, so it counts only if
  it later makes a *new* high/low past the snapshot taken at creation, or closes beyond the level.
- **above / below:** fires when a bar's high >= level (low <= level), including the bar still forming.
  A gap through the level fires at the open, reported as "gapped". Report time = that bar's window.
- **Armed / re-arm:** an alert is *armed* only while price is on the near side. A repeating alert
  fires at most once per bar, then waits until a **completed** 5-minute bar closes back across the
  level by `--rearm-pct` (0.25% default, e.g. below 369.11 for 370.04) before it can fire again - a
  stock chopping around a level does not ping every minute. Added while price is already past the
  level, it starts *waiting* (the output says so) and fires only after a move back and through again.
- **pct-move:** two levels (ref ±PCT%, rounded to cents), each armed/re-armed like above/below. The
  reference is the price when added, `--ref PRICE`, or `--ref prev-close` (last 5-minute close of the
  previous session - close to, not exactly, the official close; added after 16:00 ET it is today's
  close, the reference for tomorrow). The band does not trail price.
- **vwap-cross:** session VWAP from 09:30 (typical price x volume). Fires when a **completed** bar
  closes on the other side of VWAP than the previous observation. The first two bars of each session
  only set the side (earliest cross: the 09:40 bar, at 09:45); an overnight gap is not a cross.
  `above`/`below` fire only on that direction (the other direction silently re-arms it). Needs
  volume: symbols Yahoo reports without volume (indexes like `^VIX`, FX) are refused - use levels.
- `--once`: switches off after its first fire (status `fired`; clean up with `remove --fired`).
- Plan alerts (from `plans/<SYM>.json`): long = stop *below*, target *above*; short = mirror; entry =
  whichever way price must travel to reach it (a long already above its entry gets a pullback alert,
  with a warning). `check` notes when the saved plan's levels changed since the alerts were set.
- **Limits:** no pre-/after-market prices; 5-minute resolution (a fire is placed in a bar, not to
  the second); Yahoo data can lag or be revised; touches older than Yahoo's ~60 days of 5-minute
  history cannot be checked; exchange holidays and half days are not modelled (a holiday just shows
  nothing new). Alerts fire only when `check`/`watch` runs - there is no background service.

## Reading the output / how to present it to the user
- `check` prints `ALERTS CHECK · 10:12 ET · 7 active alerts on 2 symbols`, then each fire as
  `FIRED  ALR-...  TSLA  above 351.00` + `traded above 351.00 10:05-10:10 ET (high 351.11) · last
  348.91 · "note" · re-arms after a 5m close back across`, then a "Still watching" table (id, symbol,
  alert, status armed|waiting|armed up/down only, last, % away, times fired, note) and why any
  *waiting* alert is waiting.
- Lead with what fired: symbol, level, **when** (the bar window), and where price is **now** - a level
  touched at 10:05 and given back by 10:12 is not the same as price sitting above it. Say "touched" or
  "traded at", never "broke out" or "confirmed".
- A stop alert firing means price reached the stop level; for a paper position, the broker's `sync`
  records the exit. Say so instead of implying a fill happened here.
- "Nothing fired" is an answer too: give the nearest armed alert and its distance.
- Warnings on `add` (already past the level, % far away - typo?, stale plan) must be relayed.
- JSON (`check`): `as_of, market_open, checked, active, fired [{id, symbol, kind, condition, leg,
  level, how (touch|gap|close), price, date, bar_time, bar_complete, bar {open, high, low, close}, vwap, last_price,
  note, once, deactivated, source, plan_role}], alerts [{id, symbol, condition, status, distance_pct,
  waiting_for, fire_count, last_fired, levels [{dir, level, armed}], ...}], symbols {SYM: {range,
  bars, last, last_bar}}, errors {SYM: msg}, notes []`. One symbol failing lands in `errors`; the
  others are still checked.

## Next step on the desk
- Usual flow: **entry** (trigger) -> **exit** (`--save` the stop/target) -> `alerts.py from-plan SYM`
  so the user hears when price reaches the entry, stop or target -> **time-machine** for the odds ->
  **risk** to size -> **budget** check -> paper **broker** ticket (waits for the user's yes).
- When a stop or target alert fires on an open paper trade, run the broker's `sync` / `positions`.
- When an entry alert fires, re-check the setup with the **entry** skill before acting on it.

## Rules
- Not financial advice. An alert is a price notification, not a signal to trade; never say "buy
  now" because an alert fired, and never claim certainty about what price does next.
- Paper trading only: this skill places no orders. Orders go through the broker skill, which waits
  for the user's explicit yes in a later message.
- Never report an alert as fired unless the script printed it, and never invent a price or a time.
- Do not leave a long `watch` running without telling the user; prefer `check` when they ask "did
  anything go off?".
- Exit code 1 = bad input or state (the message says what to fix). If the script exits with code 2
  ("data error: ..."), tell the user which host failed (it is in the message, e.g.
  `query1.finance.yahoo.com`) - in sandboxes the host may need to be allow-listed. Never invent
  prices, headlines, or odds to fill the gap; the alerts stay saved and unchanged.
