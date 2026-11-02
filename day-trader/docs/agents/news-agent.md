# News agent: one per group of up to five ideas

The main session spawns these with the Agent tool (`general-purpose`, `run_in_background: false`)
**in the same tool block as the `desk_check.py` call**, so the news is read while the numbers run
(RUNBOOK §3a). Fill the placeholders, then pass everything below the line as the agent's prompt:

| Placeholder | Value |
|---|---|
| `{{DATE}}` | today, `YYYY-MM-DD` |
| `{{RUN}}` | the run's id for file names: `<date>-<HHMM>` (`2026-11-02-0950`), or `<date>-premarket` |
| `{{OUT}}` | the session's scratchpad desk folder (`$OUT`) |
| `{{GROUP}}` | 0, 1, 2 ... (one file per agent) |
| `{{IDEAS_JSON}}` | this group's rows from `$OUT/ideas-<RUN>.json` (symbol, side, entry, setup, note) |
| `{{STUDY}}` | the pre-market study's path, `$OUT/premarket/study-<date>.json`, or `none` |
| `{{MINUTES}}` | the budget: 3 in the sweep and the crypto run, 6 in the pre-market study |

Each agent writes one JSON file, `{{OUT}}/sweep/news-{{RUN}}-{{GROUP}}.json`, which
`tools/desk_decide.py --news` reads. Nothing in it decides anything: the written red-flag definition
(RUNBOOK §3) is the only test, and the main session applies it.

---

Paper-trading research only: you place no orders and never touch /root/.day-trader or
/root/.day-trader-shadow. Today is {{DATE}}. The desk trades the ideas that pass its rules within
minutes, so you have a hard budget of {{MINUTES}} minutes: be quick, cite what you read, write
"unknown" for what you could not check, and stop at the budget even if a symbol is incomplete.

Ideas (JSON): {{IDEAS_JSON}}

For EACH symbol, in this order:
1. If {{STUDY}} is a file, read that symbol's row from it with Bash:
   `python3 -I -c "import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps([{k: x.get(k) for k in ('symbol','news')} for x in d['symbols'] if x['symbol'] in sys.argv[2:]]))" {{STUDY}} SYM ...`
   Start from its news row (red_flag, reason, next earnings, events) and look only for anything
   newer than this morning.
2. `SK=/home/user/projects/day-trader/skills; cd /home/user/projects/day-trader && python3 $SK/news/scripts/news.py SYM --since 24h`
   (headlines with impact tags; count them).
3. ONE WebSearch (mode "standard") per symbol, "SYM stock today": the catalyst behind the move and
   anything scheduled today.

A red flag needs evidence (a headline, a filing or an exchange notice, with its URL) of one of these,
and nothing else counts:
- the move is driven by an unconfirmed takeover report or a rumor;
- a share offering, a dilution vote or a lock-up expiry is announced or pending today;
- a trading halt;
- a delisting notice, a going-concern warning, a fraud allegation or a regulatory investigation
  announced in the last 5 days;
- a guidance cut in the last 5 days (a cut of any guided line counts, even inside a beat);
- an earnings release or a guidance call scheduled during today's regular session.

Not a red flag: an earnings report that is already out (today's or Friday's gap), an analyst
downgrade, a sector selloff, a report scheduled after today's close.

Write ONE file with the Write tool, `{{OUT}}/sweep/news-{{RUN}}-{{GROUP}}.json`, exactly this shape:

```json
{"date": "{{DATE}}", "run": "{{RUN}}", "group": {{GROUP}}, "symbols": [
  {"symbol": "SYM", "red_flag": false, "red_flag_reason": "none",
   "catalyst": "one line", "next_earnings": "date or unknown", "events_today": "... or none",
   "sentiment": "bullish|bearish|mixed|none", "headline_count_24h": 0, "sources": ["https://..."]}
]}
```

Every symbol of the group must appear; write "unknown" rather than leave one out. Then return one
line: the symbols with a red flag (or "no red flags") and the file's path. Nothing else.
