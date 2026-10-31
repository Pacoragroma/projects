# Runbook: operating the paper desk

**Short version: scan → take the best hits through the desk in a throwaway home → copy the
passing plan to the real desk → paper ticket and a push notification → owner's yes → sync →
refresh the dashboard → back up. Everything is paper.**

Define these in **every** shell call (shell state does not persist between calls):

```bash
SK=/home/user/projects/day-trader/skills          # our 11 skills
TOOLS=/home/user/projects/day-trader/tools
OUT=<this session's scratchpad>/desk; mkdir -p $OUT/scan    # ALL outputs go here, never inside the public repo
ORIG=$(ls -d /root/.claude/plugins/cache/day-trader-skill/day-trader-skill/*/ 2>/dev/null | tail -1)   # the video's pack
```

The desk's state lives in `$DAYTRADER_HOME`, default `~/.day-trader`: the owner's real paper desk.
For analysis that must not touch it, use a throwaway home:

```bash
export DAYTRADER_HOME=$(mktemp -d)
cp ~/.day-trader/settings.json "$DAYTRADER_HOME"/
```

## 1. Set up a fresh container

1. **Repo.** `git -C /home/user/projects fetch origin && git -C /home/user/projects checkout
   claude/day-trader-skills && git -C /home/user/projects pull --ff-only`. The handoff docs and
   tools must be present (`HANDOFF.md`, `day-trader/tools/desk_restore.py`).
2. **Network.** `python3 /home/user/projects/day-trader/tools/smoke_live.py` runs 17 live checks
   across the 9 data hosts in about 15 s; all passed on 2026-10-31. A blocked host prints
   `data error: ... may need to be allow-listed`. Yahoo sometimes answers 429 for a few minutes;
   retry later.
3. **Plugins.** `claude plugin list`. If either is missing:
   ```bash
   claude plugin marketplace add Pacoragroma/projects && claude plugin install day-trader@pacoragroma-projects
   claude plugin marketplace add Jakeschincariol/day-trader-skill && claude plugin install day-trader-skill@day-trader-skill
   ```
   The original pack was audited at commit `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd`: skills only,
   no hooks or MCP servers, data hosts only. After a reinstall, compare `gitCommitSha` for
   `day-trader-skill` in `~/.claude/plugins/installed_plugins.json`. If it differs, re-audit before
   running it. After a code change to our pack is pushed: `claude plugin marketplace update
   pacoragroma-projects && claude plugin update day-trader@pacoragroma-projects`. Plugin skills
   load in the next session; in the current one, run the scripts by path.
4. **Restore the paper desk, only if `~/.day-trader/settings.json` is missing.** If it exists,
   keep it (it is newer than any backup) and back it up instead (§4). To restore:
   - `ArtifactData get` with the dashboard's `url`, `collection: data/users/me`,
     `doc_id: desk_state`, `out_dir: $OUT/backup`. It saves to
     `$OUT/backup/data/users/me/desk_state.json`, with a literal `me`.
   - Then `python3 $TOOLS/desk_restore.py $OUT/backup/data/users/me/desk_state.json
     --candidates-out $OUT/candidates.json`.

   It refuses to overwrite an existing desk unless `--force`. Then check:
   - `python3 $SK/risk/scripts/risk.py show`: $25,000, $50 a trade, 20 trades, $1,000 daily max
     loss, $500 open risk, tilt guard 20;
   - `python3 $SK/budget/scripts/budget.py status --market crypto`: OPEN FOR TRADING;
   - `python3 $SK/alerts/scripts/alerts.py list`: the armed alerts.
5. **The original pack's workspace** (for stock sweeps): `mkdir -p /home/user/original-desk &&
   cd /home/user/original-desk && python3 ${ORIG}trader-scanner/scan.py universe`. About 1.5 s;
   writes `trader/universe.txt` (5,942 symbols).

## 2. Scan for setups

**What the owner wants from this step:** look through a big number of charts and surface the few
that behave unusually (breakouts, breakdowns, gaps, volume spikes, tight coils). That is
"discrepancies we can take advantage of".

| Scan | Command | Time | Notes |
|---|---|---|---|
| Crypto, all Binance USDT pairs | `python3 $SK/scanner/scripts/scan.py --market crypto --top 0 --json --quiet > $OUT/scan/crypto.json` | ~1 min | 508 pairs. Crypto defaults: min price 0, min $5M/day. Gap setups are skipped (24/7) |
| US stocks, whole market (daily) | `python3 $SK/scanner/scripts/scan.py --top 0 --json --quiet > $OUT/scan/us.json` | ~2-3 min | 5,907 stocks; min $5 and $20M/day. Best after the close or before the open |
| Watchlists | `python3 $SK/scanner/scripts/scan.py --symbols TSLA,NVDA` and separately `--symbols BTC-USDT,ETH-USDT` | seconds | One market per call: mixing crypto and stocks exits 1 |
| Original pack, daily (before the open) | `cd /home/user/original-desk && python3 ${ORIG}trader-scanner/scan.py daily --show 25` | ~2.5 min | 20-day closing-high breakouts, bull flags, MA20 reclaims, oversold, oversold bounces. Writes `trader/watchlist.json` |
| Original pack, **intraday ORB sweep** | `cd /home/user/original-desk && python3 ${ORIG}trader-scanner/scan.py orb --json` | ~4-5 min | After 09:50 ET only: which stocks closed a 5-minute bar above their 15-minute opening range (the video's TSLA moment). If it says it is too early, wait 60 s and re-run. It overwrites `trader/watchlist.json` (the daily table then shows the sweep). Hits have no volume: rank by `pct_over` and check liquidity with `analyst.py brief`. Later triggers are common, so offer the owner extra sweeps at 10:05 and 10:20 ET |

Scanner JSON (ours) has `results[]` rows with `symbol`, `score` (0-100), `bias`, `setups[{setup,
detail, levels, key_level}]`, `last`, `change_pct`, `rvol`, `atr_pct`, `dollar_volume_20d`,
`sma20/50`, `rsi14`, `high_20d` and `low_20d`. The top level has `scanned`, `matched`,
`skipped_by_reason`, `filtered_by_reason` and `notes`.

## 3. Take a candidate through the desk

Run each candidate in its own throwaway home (see the top). Parallel agents work well; the
2026-10-31 run used one agent per candidate plus an independent skeptic for every proposed trade.

```bash
python3 $SK/news/scripts/news.py SYM --since 72h                  # what moved it; crypto reads Yahoo as BTC-USD
python3 $SK/analyst/scripts/analyst.py brief SYM                  # trend, levels, ATR, rel. volume, SEC (stocks)
python3 $SK/entry/scripts/entry.py SYM --setup orb|vwap|level|pdh|pdl [--level X] [--or-start HH:MM] [--side long|short|both]
python3 $SK/exit/scripts/exit_plan.py SYM --entry E --side long|short --method atr|structure|percent|manual --rr 2 --save
python3 $SK/time-machine/scripts/time_machine.py SYM --save --json   # stocks out of hours: next session from the open; --at 10:00 for later fills
python3 $SK/risk/scripts/risk.py size --symbol SYM --save --json     # --save: the ticket takes its size from the plan
```

**Pass rules. All must hold; otherwise skip, wait or watch:**

- a live entry now (triggered, or at market for a stated reason);
- Time Machine verdict is not `ODDS LEAN AGAINST YOU` (crypto verdicts are judged after fees);
- expected R after fees above +0.05;
- liquid: our size is trivial against the daily dollar volume;
- no red-flag news;
- crypto: **long only** (spot cannot be shorted);
- stocks: watch the **short-sale restriction** (SSR, Reg SHO Rule 201). A stock that fell 10% or
  more the day before can't be shorted on a downtick all day.

**A pass becomes a paper trade on the real desk** (`DAYTRADER_HOME` unset, i.e. `~/.day-trader`):

```bash
cp "$THROWAWAY_HOME/plans/<SYM>.json" ~/.day-trader/plans/           # canonical file name, e.g. BTC-USDT.json
python3 $SK/budget/scripts/budget.py check --symbol SYM              # exit 0 OK, 3 BLOCKED
python3 $SK/broker/scripts/broker.py ticket --symbol SYM             # levels and size come from the plan
#  -> send a PushNotification: ticket id, order, expiry (tickets last 15 minutes) - the owner may not be watching
#  -> show the card to the owner and STOP. Out of hours (stocks) or past 23:55 UTC (crypto) it is only a PREVIEW (no id).
python3 $SK/broker/scripts/broker.py confirm TKT-YYYYMMDD-NNN --yes  # ONLY after the owner's explicit yes, in a later message
python3 $SK/broker/scripts/broker.py sync --market crypto            # stops, targets, time stop from 5m bars (or --market us)
python3 $SK/broker/scripts/broker.py positions|tickets|history --market crypto
python3 $SK/broker/scripts/broker.py close TRADE-ID [--price X]      # --price must have traded since the fill (5m bars)
python3 $SK/budget/scripts/budget.py status --market crypto          # "RISKED SO FAR"
python3 $SK/analyst/scripts/analyst.py review                        # journal stats once trades exist
```

Without the copied plan, `budget.py check --symbol` exits 1 ("no saved plan") and `broker.py
ticket` exits 1 ("give --qty ... or size the trade first"). The alternative is to pass `--entry
--stop --qty` to the check and `--entry --stop --target --qty` to the ticket.

Exit codes across the pack: `0` ok; `1` bad input or state, including usage errors; `2` a data
source is unreachable (the one-line message names the host); `3` blocked by the daily budget.

## 4. The dashboard

- **What it is:** a claude.ai artifact of the **Dashboard** type, "Paper Trading Desk". Its URL
  is in the owner's handoff prompt. It has one dataset, `desk`: a JSON file asset holding one array
  of rows tagged by `kind` (`account, plan, ticket, position, trade, scanmeta, scan, candidate,
  candle, log`). The page (`files/index.html`) splits the rows with `dash.calc` and draws:
  - tiles: status, last price, realized P&L, open risk, loss budget left, trades today;
  - the three scanner tables and the candidates table;
  - a price chart with plan lines and fill dots;
  - the plan and odds card, tickets waiting for a yes, positions, closed trades, and Activity.
- **Before the first write in a new session**, read the artifact once (`Artifact read`, or `list`
  with `scope: files`). The Artifact tool refuses a publish to an artifact the conversation hasn't
  read.
- **Every refresh replaces the whole dataset.** Pass every input each time, or those tables go
  empty. `desk_snapshot.py` exits 1 if an input path doesn't exist. A full refresh prints about
  `scanmeta 3, scan 32, candidate 10`.
- **Refresh**, after every desk step worth seeing:
  1. ```bash
     python3 $TOOLS/desk_snapshot.py --symbol BTC-USDT --out $OUT/desk.json \
       --scan $OUT/scan/crypto.json --scan $OUT/scan/us.json \
       --original-watchlist /home/user/original-desk/trader/watchlist.json \
       --candidates $OUT/candidates.json --event "What happened" --detail "One plain line"
     ```
     `--symbol` is the charted symbol. `--event` appends to `~/.day-trader/desk_log.jsonl`, which
     feeds Activity.
  2. `Artifact` publish with `url` = the dashboard, `file_path` = `$OUT/desk.json`,
     `asset: true`. It returns `/_blob/<id>`.
  3. `ArtifactData get {collection: "datasets", doc_id: "desk"}` for its `version`, then `update`
     with `{"source": {"kind": "file", "url": "/_blob/<id>", "name": "desk.json", "format":
     "json"}, "updated": {"at": "<ISO UTC>", "by": "Claude"}}` and `if_version`.
- **Back up** after anything that changes the desk. This is the only copy that survives a
  recycled container:
  1. `python3 $TOOLS/desk_backup.py --out $OUT/desk_state.json --candidates $OUT/candidates.json`
     (or `--previous <last backup>` to carry its candidates over).
  2. `ArtifactData get {collection: "data/users/me", doc_id: "desk_state"}` for the version.
  3. `ArtifactData set` with `file_path: $OUT/desk_state.json` and `if_version`.
- **The page's own code** is the store document `files/index.html` (`{text}`). Before editing it,
  read the type's `SKILL.md`, then `artifact-type/reference/one-writer.md` and
  `design-defaults.md` (`Artifact read` with `path`). The page follows the "one writer" rules:
  fixed text in HTML, data-driven parts as `data-draw` drawings, every number from the dataset.
  Write with `ArtifactData set` and `if_version`. Then read `data/users/me` doc `check`: it may
  say `checking`, or be absent until the owner opens the page.

## 5. Alerts and the watcher

```bash
python3 $SK/alerts/scripts/alerts.py list
python3 $SK/alerts/scripts/alerts.py add SYM --above X|--below X|--pct-move P|--vwap-cross [--once] [--note "..."]
python3 $SK/alerts/scripts/alerts.py from-plan SYM                 # entry/stop/target alerts from the saved plan
python3 $SK/alerts/scripts/alerts.py check --json                  # "fired" is a LIST of fired alerts
python3 $SK/alerts/scripts/alerts.py remove ALR-ID|--symbol SYM|--fired|--all
```

- **`above`/`below` alerts fire on a touch** (a 5-minute bar's high or low), not on a close. A
  restored alert can also fire on a touch from hours ago. On a fire:
  1. Read the fire time.
  2. Re-check live with `entry.py SYM --setup level --level X`.
  3. Only then take the pair through §3. A stale or wick-only touch is a watch, not a trade.
- **The watcher** is a shell loop. Start it only if none is running: `pgrep -f 'alerts.py check
  --json'`. Launch it with the Bash tool's `run_in_background: true` and **`timeout: 7200000`**;
  the default 30-minute limit would kill it. 115 loops of about 60 s fit in the 2-hour maximum.
  Restart it once after a fire (for the remaining alerts) or after its window ends; the 2-hourly
  crypto job also restarts it.

  ```bash
  cd /home/user/projects/day-trader && for i in $(seq 1 115); do
    out=$(python3 skills/alerts/scripts/alerts.py check --json 2>/dev/null)
    n=$(printf '%s' "$out" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('fired') or []))" 2>/dev/null || echo 0)
    if [ "${n:-0}" -gt 0 ] 2>/dev/null; then echo "ALERT FIRED $(date -u +%H:%M) UTC"; printf '%s' "$out"; exit 0; fi
    sleep 60; done; echo "watcher window ended, no alert"
  ```

## 6. Scheduled jobs, and taking them over after a session transfer

Scheduled tasks fire into the session that created them. On 2026-10-31 the old session owned two:

| Name | Schedule | Prompt |
|---|---|---|
| `Crypto desk re-scan (weekend)` | cron `40 */2 * * *` UTC; disables itself after Mon 2026-11-02 14:00 UTC | Restore if needed → crypto scan (and missing stock scans) → alerts → §3 for anything new → copy plan, ticket, PushNotification → refresh and back up → restart the watcher |
| `Monday stock sweep at the open` | once, `2026-11-02T14:50:00Z` | Make sure the original pack is installed (audited sha) → ORB sweep → rank by `pct_over`, liquidity → §3 → tickets plus PushNotification → refresh and back up → offer 10:05/10:20 ET sweeps → disable the crypto job |

The owner's account also has an unrelated task, `PR review digest`. Leave it alone.

**Taking over** (the owner's handoff prompt has the old ids):

1. Make sure the old session is idle: `get_session <old id>`, and `get_trigger` on each old id
   (`last_fired_at`). A run still in flight will write a newer backup; wait for it, then restore.
2. Copy each prompt verbatim: `get_trigger <old id>` → `derived_state.prompt`. The stored
   prompts are current and already carry the fixes (`--save`, plan copy, PushNotification,
   absolute `$OUT` paths, restore via `desk_restore.py`).
3. Re-create them in the new session:
   - `create_trigger` with the same name, `cron_expression: "40 */2 * * *"` (a non-zero minute
     is stored verbatim and keeps the :40 phase), `initiation: human_request`;
   - `send_later` with `at: 2026-11-02T14:50:00Z`, the same name and `initiation: human_request`.

   Skip any whose time has passed.
4. Disable the old ones **by id** (`update_trigger enabled: false`): names are now duplicated.
   Verify with `list_triggers` that only the new pair is enabled, with `persistent_session_id` =
   this session (`get_session` with no args gives it).
5. Tell the old session: `send_message` to it, "Handoff done: stop your alert watcher (TaskStop)
   and take no more desk actions." Ask the owner whether to archive it. `archive_session` also
   kills its container and watcher.

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
| Crypto exchange | Binance (default) | `risk.py config --crypto-exchange binance\|coinbase`, or `$DAYTRADER_CRYPTO_EXCHANGE` |
| Crypto paper fee | 0.1% per fill (follows the exchange; Coinbase 0.6%) | `risk.py config --crypto-fee-pct X` |
| Ticket life | 15 minutes | `ticket_ttl_minutes` in `settings.json` |
| SEC contact | not set | `$DAYTRADER_CONTACT` or `contact_email` in `settings.json`. **Ask the owner first**: it would also ride in the private backup |

Environment variables:

| Variable | Effect |
|---|---|
| `DAYTRADER_HOME` | state folder |
| `DAYTRADER_MARKET=us\|crypto` | default market for symbol-less commands |
| `DAYTRADER_NOW` | replay clock |
| `DAYTRADER_CRYPTO_EXCHANGE` | crypto price source |
| `DAYTRADER_CONTACT` | SEC User-Agent email |
| `DAYTRADER_NO_CACHE` | skip the download cache |
| `DAYTRADER_PDF_ENGINE` | Pelosi PDF reader |
| `DAYTRADER_FIXTURES` | offline tests |

## 8. Change the code and release

```bash
cd /home/user/projects/day-trader
python3 tools/sync_common.py                 # after editing shared/trader_common.py (never edit the 11 copies)
python3 -m unittest discover -s tests        # 945 tests, ~2 min, offline
/root/.local/bin/python3.9 -m unittest discover -s tests   # if uv's 3.9 is installed; CI runs 3.9 and 3.12 anyway
python3 tools/sync_common.py --check && python3 tools/package_skills.py --check
python3 tools/smoke_live.py                  # live, whenever data parsing changed
```

- Pull first (the owner merges PRs into this branch too). Commit to `claude/day-trader-skills`,
  the default branch the plugin installs from, then `git push origin claude/day-trader-skills`.
- Version lives in three places: `day-trader/.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json` and `VERSION` in `shared/trader_common.py`.
- CI (`.github/workflows/day-trader.yml`) installs Python with uv, because `actions/setup-python`
  has no 3.9 build for current Ubuntu runners.
- **Public repo:** never put the owner's email, the dashboard URL, task ids, ledgers or keys in
  files. `.gitignore` blocks stray `*.json` at the repo root and in `day-trader/`. Commit trailers
  carry a session link by convention; that is expected.

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `data error: ... blocked by proxy ... may need to be allow-listed` (exit 2) | The environment's network policy. The owner adds the host under Network access → Allowed domains |
| Yahoo `HTTP 429` | Yahoo rate-limits shared cloud IPs for a while. Retry later; crypto is unaffected |
| SEC `HTTP 403` | No contact email in the User-Agent. Ask the owner about `DAYTRADER_CONTACT`; news and the brief skip SEC meanwhile |
| `api.binance.com` HTTP 451 | Binance blocks US IPs on its main API. The pack uses `data-api.binance.vision`, which works |
| `budget.py check --symbol` says "no saved plan", or `ticket` asks for `--qty` | The plan wasn't copied to `~/.day-trader/plans/` (§3) |
| A crypto plan "not found" | BTCUSDT and BTC-USDT share one plan file. **BTC-USD is a separate plan**, so use the `-USDT` spelling throughout |
| A stock ticket is only a PREVIEW | Market closed or past 15:55 ET. Re-run `ticket` during the session |
| Dashboard tables went empty | A refresh without that input (§4), or the dataset `update` was skipped or its `if_version` was stale |
| An alert "fired" but nothing happened | It was a touch (wick) or happened hours ago. Re-check with `entry.py --setup level` (§5) |
