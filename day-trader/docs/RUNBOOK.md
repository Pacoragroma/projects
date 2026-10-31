# Runbook: operating the paper desk

**Short version: scan → take the best hits through the desk → paper ticket → owner's yes →
sync → refresh the dashboard → back up state. Everything is paper. Commands below run from the
repo (`/home/user/projects`) unless a path says otherwise.**

Shorthands used below:

```bash
SK=/home/user/projects/day-trader/skills                     # our 11 skills
TOOLS=/home/user/projects/day-trader/tools
ORIG=$(ls -d /root/.claude/plugins/cache/day-trader-skill/day-trader-skill/*/ 2>/dev/null | tail -1)   # the video's pack
```

The desk's state lives in `$DAYTRADER_HOME`, default `~/.day-trader`. Use the default for the
owner's real paper desk. Use a throwaway `export DAYTRADER_HOME=$(mktemp -d)` (with
`cp ~/.day-trader/settings.json "$DAYTRADER_HOME"/`) for analysis that must not touch it.

## 1. Set up a fresh container

1. **Network.** All of these must answer (`curl -s -o /dev/null -w '%{http_code}' URL`):
   `query1.finance.yahoo.com`, `query2.finance.yahoo.com`, `feeds.finance.yahoo.com`,
   `www.nasdaqtrader.com`, `www.sec.gov`, `data.sec.gov`, `disclosures-clerk.house.gov`,
   `data-api.binance.vision`, `api.exchange.coinbase.com`. The quickest full check is
   `python3 day-trader/tools/smoke_live.py`: 17 checks, about 15 s, and they all passed on
   2026-10-31. Yahoo sometimes answers 429 for a few minutes; just retry later.
2. **Plugins.** `claude plugin list`. If either is missing:
   ```bash
   claude plugin marketplace add Pacoragroma/projects && claude plugin install day-trader@pacoragroma-projects
   claude plugin marketplace add Jakeschincariol/day-trader-skill && claude plugin install day-trader-skill@day-trader-skill
   ```
   After a code change is pushed: `claude plugin marketplace update pacoragroma-projects && claude
   plugin update day-trader@pacoragroma-projects`. The skills load in the next session; within the
   current session, run the scripts by path.
3. **Restore the paper desk** if `~/.day-trader/settings.json` is missing. Read the backup with
   `ArtifactData` (`action: get`, the dashboard's `url`, `collection: data/users/me`,
   `doc_id: desk_state`, `out_dir: <scratch dir>`), then pass the file it reports saving (under
   `<scratch dir>/data/users/<id>/desk_state.json`) to this:
   ```bash
   python3 - /path/to/desk_state.json <<'EOF'
   import json, pathlib, sys
   doc = json.loads(pathlib.Path(sys.argv[1]).read_text()); state = doc.get("data", doc)
   home = pathlib.Path.home() / ".day-trader"; (home / "plans").mkdir(parents=True, exist_ok=True)
   (home / "settings.json").write_text(json.dumps(state["settings"]))
   (home / "ledger.json").write_text(json.dumps(state["ledger"]))
   if state.get("alerts"): (home / "alerts.json").write_text(json.dumps(state["alerts"]))
   for name, plan in (state.get("plans") or {}).items(): (home / "plans" / name).write_text(json.dumps(plan))
   (home / "desk_log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in state["desk_log"]))
   print("restored", sorted(p.name for p in home.iterdir()))
   EOF
   ```
   Keep `state["candidates"]` as a file for the dashboard's candidates table (`--candidates`, §4).
   Then check: `python3 $SK/risk/scripts/risk.py show` and `python3 $SK/budget/scripts/budget.py
   status --market crypto` must show $25,000, $50/trade, 20 trades, $1,000 daily max loss, $500
   open risk, tilt guard 20. `python3 $SK/alerts/scripts/alerts.py list` must show the three alerts
   (unless they fired).
4. **The original pack's workspace** (only for stock sweeps): `mkdir -p /home/user/original-desk
   && cd /home/user/original-desk && python3 $ORIG/trader-scanner/scan.py universe`. Takes about 1
   s; writes `trader/universe.txt` (5,942 symbols).

## 2. Scan for setups

**What the owner wants from this step:** look through a big number of charts and surface the few
that behave unusually (breakouts, breakdowns, gaps, volume spikes, tight coils). That is
"discrepancies we can take advantage of".

| Scan | Command | Time | Notes |
|---|---|---|---|
| Crypto, all Binance USDT pairs | `python3 $SK/scanner/scripts/scan.py --market crypto --top 0 --json --quiet > crypto.json` | ~1 min | 508 pairs; crypto defaults min price 0, min $5M/day; gaps skipped (24/7) |
| US stocks, whole market (daily) | `python3 $SK/scanner/scripts/scan.py --top 0 --json --quiet > us.json` | ~3 min | 5,907 stocks; min $5, $20M/day; best after the close or before the open |
| A watchlist only | `python3 $SK/scanner/scripts/scan.py --symbols TSLA,NVDA,BTC-USDT` | seconds | Don't mix crypto and stocks in one call |
| Original pack, daily (before the open) | `cd /home/user/original-desk && python3 $ORIG/trader-scanner/scan.py daily --show 25` | ~2.5 min | 20-day closing-high breakouts, bull flags, reclaims, oversold; writes `trader/watchlist.json` |
| Original pack, **intraday ORB sweep** | `cd /home/user/original-desk && python3 $ORIG/trader-scanner/scan.py orb --json` | minutes | After 09:50 ET only: which stocks closed a 5-minute bar above their 15-minute opening range. This is the video's TSLA moment |

Scanner JSON: `results[]` rows carry `symbol, score (0-100), bias, setups[{setup, detail, levels,
key_level}], last, change_pct, rvol, atr_pct, dollar_volume_20d, sma20/50, rsi14, high_20d,
low_20d`. Top level: `scanned, matched, skipped_by_reason, filtered_by_reason, notes`.

## 3. Take a candidate through the desk

Run each candidate in its own throwaway `DAYTRADER_HOME` (with the owner's `settings.json` copied
in). Parallel agents work well for this; the 2026-10-31 run used one agent per candidate plus an
independent skeptic for every proposed trade.

```bash
python3 $SK/news/scripts/news.py SYM --since 72h                  # what moved it; crypto reads Yahoo as BTC-USD
python3 $SK/analyst/scripts/analyst.py brief SYM                  # trend, levels, ATR, rel. volume, SEC (stocks)
python3 $SK/entry/scripts/entry.py SYM --setup orb|vwap|level|pdh|pdl [--level X] [--or-start HH:MM] [--side long|short|both]
python3 $SK/exit/scripts/exit_plan.py SYM --entry E --side long|short --method atr|structure|percent|manual --rr 2 --save
python3 $SK/time-machine/scripts/time_machine.py SYM --save --json   # stocks out of hours: next session from the open; --at 10:00 for later fills
python3 $SK/risk/scripts/risk.py size --symbol SYM --json
```

**Pass rules (all must hold, otherwise skip, wait or watch):**

- a live entry now (triggered, or at market for a stated reason);
- Time Machine verdict is not `ODDS LEAN AGAINST YOU` (crypto verdicts are judged after fees);
- expected R after fees above +0.05;
- liquid: our size is trivial against the pair's or stock's daily dollar volume;
- no red-flag news;
- crypto: **long only** (spot cannot be shorted);
- stocks: watch the short-sale restriction (SSR, Reg SHO Rule 201) on anything that fell 10% or
  more the day before. It bans shorting on a downtick all the next day.

**Turning a pass into a paper trade, on the real desk:**

```bash
python3 $SK/budget/scripts/budget.py check --symbol SYM              # exit 0 OK, 3 BLOCKED
python3 $SK/broker/scripts/broker.py ticket --symbol SYM --entry E --stop S --target T [--qty Q] [--type limit|market]
#  -> show the card to the owner and stop. Tickets expire after 15 minutes.
#  -> outside the session (stocks) or past the 23:55 UTC time stop (crypto) it is only a PREVIEW (no id).
python3 $SK/broker/scripts/broker.py confirm TKT-YYYYMMDD-NNN --yes  # ONLY after the owner's explicit yes, in a later message
python3 $SK/broker/scripts/broker.py sync --market crypto            # stops, targets, time stop from 5m bars (also --market us)
python3 $SK/broker/scripts/broker.py positions|tickets|history --market crypto
python3 $SK/broker/scripts/broker.py close TRADE-ID [--price X]      # manual close; --price must lie inside the bar's range
python3 $SK/budget/scripts/budget.py status --market crypto          # "RISKED SO FAR"
python3 $SK/analyst/scripts/analyst.py review                        # journal stats once trades exist
```

Exit codes across the pack: `0` ok, `1` bad input or state (including argparse usage errors), `2`
a data source is unreachable (the one-line message names the host), `3` blocked by the daily
budget.

## 4. The dashboard

- **What it is:** a claude.ai artifact of the **Dashboard** type, named "Paper Trading Desk". The
  owner keeps it open to watch the desk. It has one dataset, `desk`: a JSON file asset with one
  array of rows tagged by `kind` (`account, plan, ticket, position, trade, scanmeta, scan,
  candidate, candle, log`). The page (`files/index.html`) splits the rows with `dash.calc` per
  kind and draws: tiles (status, last price, realized P&L, open risk, loss budget left, trades
  today), the scanner tables (crypto, US, the original pack's), the candidates table, a price
  chart with plan lines and fill dots, the plan and odds card, tickets waiting for a yes,
  positions, closed trades and the Activity feed.
- **Every refresh replaces the whole dataset.** Pass every input each time (`--scan` files,
  `--original-watchlist`, `--candidates`), or those tables go empty. In a fresh container, re-run
  the scans first (§2) and take `candidates` from the backup (§1).
- **Refresh it** (three calls, after every desk step worth seeing):
  1. `python3 $TOOLS/desk_snapshot.py --symbol BTC-USDT --out desk.json --scan crypto.json --scan
     us.json --original-watchlist /home/user/original-desk/trader/watchlist.json --candidates
     candidates.json --event "What happened" --detail "One plain line"`. `--symbol` is the
     charted symbol. `--event` appends to `~/.day-trader/desk_log.jsonl`, which feeds Activity.
  2. `Artifact` publish with `url` = the dashboard, `file_path` = desk.json, `asset: true`. The
     reply gives `/_blob/<id>`.
  3. `ArtifactData` `get` `datasets/desk` (for its `version`), then `update` with
     `{"source": {"kind": "file", "url": "/_blob/<id>", "name": "desk.json", "format": "json"},
     "updated": {"at": "<ISO UTC>", "by": "Claude"}}` and `if_version`.
- **The page's own code** is the store document `files/index.html` (`{text}`). Read it with
  `ArtifactData get`; change it with `set` plus `if_version`. It follows the Dashboard type's
  "one writer" rules: fixed text in HTML, data-driven parts as `data-draw` drawings, every number
  from the dataset. Read the type's `artifact-type/reference/one-writer.md` and
  `design-defaults.md` (`Artifact read`, `path`) before editing. After a page write, read
  `data/users/me` doc `check` (it may be `checking`, or absent until the owner opens the page).
- **Back up the desk state** after anything that changes it: build `{saved_at, settings, ledger,
  alerts, desk_log (list), plans {file: plan}, candidates, note}` from `~/.day-trader` and
  `ArtifactData set` it to `data/users/me` / `desk_state` with `if_version` (read the version
  first). This is the only copy that survives a recycled container.

## 5. Alerts and the watcher

```bash
python3 $SK/alerts/scripts/alerts.py list
python3 $SK/alerts/scripts/alerts.py add SYM --above X|--below X|--pct-move P|--vwap-cross [--once] [--note "..."]
python3 $SK/alerts/scripts/alerts.py from-plan SYM                 # entry/stop/target alerts from the saved plan
python3 $SK/alerts/scripts/alerts.py check --json                  # "fired" is a LIST of fired alerts
python3 $SK/alerts/scripts/alerts.py remove ALR-ID|--symbol SYM|--fired|--all
```

The watcher is a background shell loop (up to 2 h per run; restart it from the scheduled job).
**`fired` is a list**: an earlier version compared it with `0` and raised a false alarm.

```bash
cd /home/user/projects/day-trader && for i in $(seq 1 115); do
  out=$(python3 skills/alerts/scripts/alerts.py check --json 2>/dev/null)
  n=$(printf '%s' "$out" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('fired') or []))" 2>/dev/null || echo 0)
  if [ "${n:-0}" -gt 0 ] 2>/dev/null; then echo "ALERT FIRED $(date -u +%H:%M) UTC"; printf '%s' "$out"; exit 0; fi
  sleep 60; done; echo "watcher window ended, no alert"
```

When one fires, take that pair through §3 again. The alert level alone is not a trade.

## 6. Scheduled jobs and taking them over

Scheduled tasks fire into the session that created them. After a session transfer:

1. `list_triggers`. Find `Crypto desk re-scan (weekend)` (cron `40 */2 * * *` UTC) and
   `Monday stock sweep at the open` (one-shot 2026-11-02T14:50:00Z), owned by the old session.
2. Re-create them in the new session: `create_trigger` with cron `0 */2 * * *` (anchored to the
   creation minute), and `send_later` at `2026-11-02T14:50:00Z`. Their prompts are below; both
   used `initiation: human_request` because the owner asked for them.
3. Disable the old ones: `update_trigger` with `enabled: false` on each old id. If the time has
   already passed, skip that job.

**Crypto re-scan prompt (summary):** restore state if missing (§1) → crypto scan (§2) → `alerts.py
check` → anything fired or new (score ≥ 60, long bias, liquid) goes through §3 in a throwaway home →
only a pass becomes a ticket on the real desk, and the owner is asked for a yes → refresh the
dashboard and back up (§4) → restart the watcher (§5) → reply in 2-3 lines. After Monday
2026-11-02 14:00 UTC it disables itself.

**Monday sweep prompt (summary):** at 09:50 ET → restore state if missing → make sure the
original pack is installed → `scan.py universe` if needed → `scan.py orb --json` across the whole
universe → the strongest, most liquid hits through §3 (live trigger, mind SSR) → tickets for each
pass and a yes request per ticket → refresh the dashboard, back up → disable the weekend crypto
job → short reply.

## 7. Settings and limits

| Setting | Value now | Change with |
|---|---|---|
| Paper account | $25,000 | `risk.py config --account N` |
| Risk per trade | 0.2% ($50) | `risk.py config --risk-pct X` |
| Max position | 25% of the account | `risk.py config --max-position-pct X` |
| Min reward:risk warning | 1.0 | `risk.py config --min-rr X` |
| Max trades a day | **20** (owner) | `budget.py set --max-trades N` |
| Daily max loss | **$1,000** (owner) | `budget.py set --daily-max-loss X` |
| Max open risk | **$500** (owner) | `budget.py set --max-open-risk X` |
| Tilt guard | **20** losses in a row (owner) | `budget.py set --max-losses N` |
| Crypto exchange | Binance (default) | `risk.py config --crypto-exchange binance\|coinbase` or `$DAYTRADER_CRYPTO_EXCHANGE` |
| Crypto paper fee | 0.1% per fill (follows the exchange: Coinbase 0.6%) | `risk.py config --crypto-fee-pct X` |
| Ticket life | 15 minutes | `ticket_ttl_minutes` in `settings.json` |

Other environment variables: `DAYTRADER_HOME` (state folder), `DAYTRADER_MARKET=us|crypto` (default
for symbol-less commands), `DAYTRADER_NOW` (replay clock), `DAYTRADER_CONTACT` (SEC User-Agent
email: ask the owner first), `DAYTRADER_FIXTURES` (offline tests).

## 8. Change the code and release

```bash
cd /home/user/projects/day-trader
python3 tools/sync_common.py                 # after editing shared/trader_common.py (never edit the 11 copies)
python3 -m unittest discover -s tests        # 942 tests, ~2 min, offline
/root/.local/bin/python3.9 -m unittest discover -s tests   # if uv's 3.9 is there; CI runs 3.9 and 3.12 anyway
python3 tools/sync_common.py --check && python3 tools/package_skills.py --check
python3 tools/smoke_live.py                  # live, when data parsing changed
```

- Commit to `claude/day-trader-skills` (the default branch the plugin installs from) with the
  attribution lines the session asks for, then `git push origin claude/day-trader-skills`. Pull
  first: the owner merges PRs into this branch too.
- Version lives in three places: `day-trader/.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json` and `VERSION` in `shared/trader_common.py`.
- CI is `.github/workflows/day-trader.yml`. It installs Python with uv, because
  `actions/setup-python` has no 3.9 build for current Ubuntu runners.
- Never commit personal data, dashboard URLs, task ids, ledgers or keys: the repo is public.

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `data error: ... blocked by proxy ... may need to be allow-listed` (exit 2) | The environment's network policy. The owner adds the host under Network access → Allowed domains |
| Yahoo `HTTP 429` | Yahoo rate-limits shared cloud IPs for a while. Retry later; crypto is unaffected |
| SEC `HTTP 403` | No contact email in the User-Agent. Ask the owner whether to set `DAYTRADER_CONTACT`; news and the brief skip SEC meanwhile |
| `api.binance.com` HTTP 451 | Binance blocks US IPs on its main API. The pack uses `data-api.binance.vision`, which works |
| A crypto plan "not found" under another spelling | Fixed in 1.2.0: BTCUSDT, BTC-USDT and BTC-USD share one plan file |
| A stock ticket is only a PREVIEW | Market closed or past 15:55 ET. Re-run `ticket` during the session |
| Dashboard shows old numbers | Step 3 of §4 (dataset `update`) was skipped, or `if_version` was stale |
