# Chart agent: one per group of up to ten pre-market candidates

The pre-market study (RUNBOOK §2a) spawns one of these per group with the Agent tool
(`general-purpose`, `run_in_background: false`), all groups in one tool block together with the news
agents and the macro agent. Fill the placeholders, then pass everything below the line as the prompt:

| Placeholder | Value |
|---|---|
| `{{DATE}}` | today, `YYYY-MM-DD` |
| `{{OUT}}` | the session's scratchpad desk folder (`$OUT`) |
| `{{GROUP}}` | 0, 1, 2 ... |
| `{{SYMBOLS_JSON}}` | this group's candidate rows (from `premarket.py candidates`) |

Each agent writes one JSON part, `{{OUT}}/premarket/parts/charts-{{DATE}}-{{GROUP}}.json`, which
`tools/premarket.py merge` reads. It studies; it decides nothing.

---

Paper-trading research only: you place no orders and never touch /root/.day-trader or
/root/.day-trader-shadow. Today is {{DATE}}; the US regular session opens 09:30 ET and the desk's
opening-range sweep runs at 09:50 ET. Study these stocks from their charts before the open:
{{SYMBOLS_JSON}}

The desk's rules for a trade (RUNBOOK §3): a long at a live opening-range break, three standard stops
(atr15 = `--method atr --atr-mult 1.5`, struct = `--method structure --lookback 36`, atr4 =
`--method atr --atr-mult 4`), a 3R target, the best stop by expected R must reach +0.2R. The skills
live in SK=/home/user/projects/day-trader/skills (define SK in EVERY shell call). A Yahoo HTTP 429
is rate limiting: sleep 30 s and retry once, then record it under data_issues and move on. An SEC 403
is expected: ignore SEC lines. Never invent a number: every figure comes from a command's output;
write "unknown" otherwise.

Set up ONCE, then put DAYTRADER_HOME=$TH in EVERY later shell call:
`TH={{OUT}}/homes/pm-charts-{{GROUP}}; rm -rf "$TH"; mkdir -p $TH/plans; cp /root/.day-trader/settings.json $TH/`

For EACH symbol, in /home/user/projects/day-trader:
1. `python3 $SK/analyst/scripts/analyst.py brief SYM --no-sec --json` (trend, levels, ATR, the
   20-day dollar volume, relative volume; the prior day's high, low and close).
2. `python3 $SK/scanner/scripts/scan.py --symbols SYM --top 0 --min-price 0 --min-dollar-volume 0 --json --quiet`
   (last, change_pct, rvol, atr_pct, sma20, sma50, rsi14, high_20d, low_20d, setups; a symbol with
   no setup today is simply absent from results).
3. The replay odds of a long from the open at the prior close with each of the three standard stops
   and a 3R target: for each stop run
   `python3 $SK/exit/scripts/exit_plan.py SYM --entry <prior close> --side long <stop flags> --rr 3 --save --json`
   then `python3 $SK/time-machine/scripts/time_machine.py SYM --save --json` and read `expected_r`,
   `target_first_pct`, `stop_first_pct`, `open_at_close_pct` and `verdict`; once per symbol also
   `python3 $SK/time-machine/scripts/time_machine.py SYM --detrend --json` for the best stop, to
   see whether the edge survives with the past sessions' average drift removed.
4. From the numbers: the trend (above or below the 20 and 50-day averages), the levels that matter
   at the open (the prior day's high and low, the 20-day high, the gap level, round numbers), ATR%
   against a 15-minute range, and whether this chart's history says an opening-range break tends to
   follow through: good = the best stop's expected R is +0.2 or more; poor = stop first in two
   thirds or more of the replays or every stop negative; else mixed.

Write ONE file with the Write tool, `{{OUT}}/premarket/parts/charts-{{DATE}}-{{GROUP}}.json`:

```json
{"date": "{{DATE}}", "group": {{GROUP}}, "symbols": [
  {"symbol": "SYM", "last": 0.0, "prior_high": 0.0, "prior_low": 0.0, "prior_change_pct": 0.0, "high_20d": 0.0,
   "atr_pct": 0.0, "dollar_volume_20d": 0, "trend": "UPTREND|DOWNTREND|...", "key_levels": ["..."],
   "stops": [{"name": "atr15", "stop": 0.0, "target": 0.0, "expected_r": 0.0, "target_first_pct": 0.0,
              "stop_first_pct": 0.0, "open_at_close_pct": 0.0, "verdict": "..."}],
   "best_stop": "atr15|struct|atr4", "detrended_expected_r": 0.0, "orb_follow_through": "good|mixed|poor|unknown",
   "watch_note": "one plain line a trader reads at 09:50 ET", "data_issues": "none or what failed"}
]}
```

Every symbol of the group must appear ("unknown" where a number is missing). Then return one line:
the good and the poor symbols, and the file's path. Nothing else.
