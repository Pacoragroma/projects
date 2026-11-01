# Runbook: operating the paper bot

**Short version: the bot runs itself. A crypto run every 2 hours and a US stock sweep every weekday
at 09:50 ET. Each run: sync both paper accounts → scan → check the ideas → the shadow account
trades every idea, the desk only the ones that pass its rules → refresh the dashboard → back up
both accounts. Everything is paper.**

Define these in **every** shell call (shell state does not persist between calls):

```bash
SK=/home/user/projects/day-trader/skills          # our 11 skills
TOOLS=/home/user/projects/day-trader/tools
OUT=<this session's scratchpad>/desk; mkdir -p $OUT/scan    # ALL outputs go here, never inside the public repo
DESK=/root/.day-trader                            # the desk: trades only what passes its rules
SHADOW=/root/.day-trader-shadow                   # the shadow account: every idea, default plan
ORIG=/home/user/day-trader-skill/                 # the video's pack, pinned to the audited commit (§1)
```

- **Standing order from the owner (2026-10-31):** paper trades need no per-ticket yes. Claude
  confirms the desk's passing tickets itself with `tools/paper_autotrade.py`. Paper only: no
  exchange account, no keys, no real order. The budget limits still apply.
- **Restore first, every turn:** before any desk command (an owner message too, not only a
  scheduled run), if `$DESK/settings.json` or `$SHADOW/settings.json` is missing, the container
  was recycled. Restore that account first (§1.4), then do the work.
- **A throwaway home** for analysis that must not touch either account. Give it a fixed path,
  because a `mktemp` folder is forgotten by the next shell call:

  ```bash
  TH=$OUT/homes/<SYM>-<stop>; rm -rf "${TH:?}"; mkdir -p $TH/plans; cp $DESK/settings.json $TH/   # once, when the check starts
  export DAYTRADER_HOME=$TH                                     # in EVERY later call for that candidate
  ```

## 1. Set up a fresh container

1. **Repo.** `git -C /home/user/projects fetch origin && git -C /home/user/projects checkout
   claude/day-trader-skills && git -C /home/user/projects pull --ff-only`. The handoff docs and
   tools must be present (`HANDOFF.md`, `day-trader/tools/paper_autotrade.py`).
2. **Network.** `python3 /home/user/projects/day-trader/tools/smoke_live.py` runs 17 live checks
   across the 9 data hosts in about 15 s, and should pass 17/17. Two failures aren't blockers:
   - a Yahoo `HTTP 429` (it rate-limits shared cloud IPs for a while): note it and re-run before
     the next stock sweep;
   - an SEC `HTTP 403` (no contact email set).

   Any other failure (a blocked host, Binance): stop and tell the owner. A blocked host prints
   `data error: ... may need to be allow-listed`.
3. **The video's original pack, pinned to the audited commit.** The jobs run it from a clone, never
   from upstream HEAD:

   ```bash
   [ -d $ORIG ] || git clone -q https://github.com/Jakeschincariol/day-trader-skill $ORIG
   git -C $ORIG checkout -q 3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd && git -C $ORIG rev-parse HEAD
   ```

   Commit `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd` was audited: skills only, no hooks or MCP
   servers, data hosts only. A newer upstream version needs a fresh audit before it runs.

   **Plugins** are optional. They make the skills answer plain requests in chat; the jobs run every
   script by path. If wanted:
   - `claude plugin marketplace add Pacoragroma/projects && claude plugin install
     day-trader@pacoragroma-projects`;
   - after a pushed code change: `claude plugin marketplace update pacoragroma-projects && claude
     plugin update day-trader@pacoragroma-projects` (it loads in the next session).
4. **Restore an account, only if its `settings.json` is missing.** After a session transfer, do this
   only once the old session has stood down (§6). For each account:
   - `ArtifactData get` with the dashboard's `url`, `collection: data/users/me`, `out_dir:
     $OUT/backup`. Use `doc_id: desk_state` for the desk and `doc_id: shadow_state` for the
     shadow. The file lands at `$OUT/backup/data/users/me/<doc_id>.json`, with a literal `me`.
   - Then restore:
     - the desk: `python3 $TOOLS/desk_restore.py $OUT/backup/data/users/me/desk_state.json
       --candidates-out $OUT/candidates.json`;
     - the shadow: `DAYTRADER_HOME=$SHADOW python3 $TOOLS/desk_restore.py
       $OUT/backup/data/users/me/shadow_state.json`.

   `desk_restore.py` refuses a home that holds any desk file (settings, ledger, alerts, log or a
   plan) unless `--force`. Then check:
   - `python3 $SK/risk/scripts/risk.py show`: desk $25,000, 1% = $250 a trade, max position 100%
     = $25,000, 20 trades, $1,250 daily max loss, $1,250 open risk, tilt guard 20;
   - `DAYTRADER_HOME=$SHADOW python3 $SK/risk/scripts/risk.py show`: $250,000 at 0.1% = $250 a
     trade, max position 10% = $25,000, limits set so they never block (§7);
   - `python3 $SK/alerts/scripts/alerts.py list`: it matches the backup. Don't re-arm a fired
     alert without asking.
5. **The original pack's workspace** (for stock sweeps): `mkdir -p /home/user/original-desk && cd
   /home/user/original-desk && python3 ${ORIG}trader-scanner/scan.py universe`. About 1.5 s;
   writes `trader/universe.txt` (5,942 symbols).

## 2. Scan for setups

**What the owner wants from this step:** look through a big number of charts and surface the few
that behave unusually (breakouts, breakdowns, gaps, volume spikes, tight coils). That is
"discrepancies we can take advantage of".

| Scan | Command | Time | Notes |
|---|---|---|---|
| Crypto, all Binance USDT pairs | `python3 $SK/scanner/scripts/scan.py --market crypto --top 0 --json --quiet > $OUT/scan/crypto.json` | ~1 min | ~508 pairs. Crypto defaults: min price 0, min $5M/day. Gap setups are skipped (24/7) |
| US stocks, whole market (daily) | `python3 $SK/scanner/scripts/scan.py --top 0 --json --quiet > $OUT/scan/us.json` | ~2-3 min | 5,907 stocks; min $5 and $20M/day. Best after the close or before the open |
| Watchlists | `python3 $SK/scanner/scripts/scan.py --symbols TSLA,NVDA` and separately `--symbols BTC-USDT,ETH-USDT` | seconds | One market per call: mixing crypto and stocks exits 1 |
| Original pack, daily (before the open) | `mkdir -p /home/user/original-desk && cd /home/user/original-desk && python3 ${ORIG}trader-scanner/scan.py daily --show 25` | ~2.5 min | 20-day closing-high breakouts, bull flags, MA20 reclaims, oversold, oversold bounces. Needs the universe (§1.5). Writes `trader/watchlist.json` there, which the dashboard reads |
| Original pack, **intraday ORB sweep** | `mkdir -p /home/user/original-desk && cd /home/user/original-desk && python3 ${ORIG}trader-scanner/scan.py orb --json` | ~4-5 min | After 09:50 ET only: which stocks closed a 5-minute bar above their 15-minute opening range (the video's TSLA moment). If it says it is too early, wait 60 s and re-run. It overwrites `trader/watchlist.json` (the daily table then shows the sweep). Hits have no volume: rank by `pct_over` and check liquidity with `analyst.py brief` |

Scanner JSON (ours) has `results[]` rows with `symbol`, `score` (0-100), `bias`, `setups[{setup,
detail, levels, key_level}]`, `last`, `change_pct`, `rvol`, `atr_pct`, `dollar_volume_20d`,
`sma20/50`, `rsi14`, `high_20d` and `low_20d`. The top level has `scanned`, `matched`,
`skipped_by_reason`, `filtered_by_reason` and `notes`.

## 3. Ideas: the shadow takes each one, the desk only the ones that pass

**What counts as an idea** (fixed for the two-week test, so nothing is cherry-picked):

- **Crypto** (each 2-hourly run):
  - every alert that fired, re-checked live with `entry.py SYM --setup level --level X --side long
    --cutoff 23:55`;
  - every crypto scan hit with a long bias, a score of 60+ and enough liquidity, checked with the
    entry setup that fits it (`--cutoff 23:55`).
- **Stocks** (the weekday sweep): the ORB hits ranked by `pct_over`; up to 10 of the top 15 that
  trade at least $20M a day (`analyst.py brief`). Long, at the trigger bar's close.
- **Live** means `entry.py` says TRIGGERED on a bar that closed in the last 15 minutes. An older
  trigger is a watch, not an idea.
- **Always pass `--cutoff`:** without it, `entry.py` stops looking 2 hours after the session
  start. That is 02:00 UTC for crypto and 11:30 ET for stocks, and any later cross reads EXPIRED.
  Use `--cutoff 23:55` for crypto and `--cutoff 15:55` for stocks.

**The owner's 3:1 rule (2026-11-01), for both accounts:** every target is 3 × the risk
(`--rr 3`), each trade risks $250, and the desk enters only when the replays clearly pay at 3:1
(the pass rules below). A low win rate is fine; a trade expected to lose is not.

**The shadow first, at once** (every live idea, the default plan: a 1.5 × ATR stop, a 3R
target, the default time stop):

```bash
python3 $TOOLS/paper_autotrade.py --home $SHADOW --standing-order "owner 2026-10-31: no per-ticket yes for paper" \
  --plan default --rr 3 SYM --side long --entry <trigger price> --setup "<setup>" --json
# a batch (the stock sweep): --ideas $OUT/ideas-<date>.json  with [{"symbol", "side", "entry", "setup"}]
```

**Then the desk checks the idea in throwaway homes**, one per stop (see the top), with the three
standard stops:
- `<SYM>-atr15`: `--method atr --atr-mult 1.5`;
- `<SYM>-struct`: `--method structure --lookback 36`;
- `<SYM>-atr4`: `--method atr --atr-mult 4`.

```bash
python3 $SK/news/scripts/news.py SYM --since 72h                  # what moved it; crypto reads Yahoo as BTC-USD
python3 $SK/analyst/scripts/analyst.py brief SYM                  # trend, levels, ATR, rel. volume, SEC (stocks)
python3 $SK/entry/scripts/entry.py SYM --setup orb|vwap|level|pdh|pdl [--level X] --side long --cutoff 23:55 --save   # stocks: --cutoff 15:55
python3 $SK/exit/scripts/exit_plan.py SYM --entry E --side long <stop flags> --rr 3 --save
python3 $SK/time-machine/scripts/time_machine.py SYM --save --json   # stocks out of hours: next session from the open; --at 10:00 for later fills
python3 $SK/risk/scripts/risk.py size --symbol SYM --save --json     # --save: the ticket takes its size from the plan
```

**Pass rules. The best of the three stops (by expected R after fees) must meet all of these:**

- a live entry now (above);
- Time Machine verdict is not `ODDS LEAN AGAINST YOU` (crypto verdicts are judged after fees);
- expected R after fees of at least +0.2 at the 3R target (it was +0.05 at 2R until 2026-11-01).
  At 3:1 that means the target comes first in roughly a third of the replays. A 3R trade's
  verdict often reads `CLOSE TO A COIN FLIP` even when it pays, because the stop is hit first
  more often than the far target; the expected R is what decides;
- liquid: our size is trivial against the daily dollar volume;
- no red-flag news;
- crypto: **long only** (spot cannot be shorted);
- stocks, for a short: no **short-sale restriction** (SSR, Reg SHO Rule 201). Once a stock trades
  10% or more below the prior close at any time in a session, it can be shorted only above the
  national best bid for the rest of that day and all of the next trading day. Skip a short if the
  prior day's low was 10%+ under the close before it, or today's low (a gap counts) is 10%+ under
  the prior close.

No vetoes beyond these rules: a reviewer's doubt is recorded in the candidate's reason, not
used to skip a pass.

**A pass becomes a desk trade:**

```bash
unset DAYTRADER_HOME                                                  # from here on: the real desk
cp $OUT/homes/<SYM>-<best>/plans/<SYM>.json $DESK/plans/              # canonical file name, e.g. BTC-USDT.json
python3 $TOOLS/paper_autotrade.py --home $DESK --standing-order "owner 2026-10-31: no per-ticket yes for paper" \
  --plan saved SYM --side long --setup "<setup>" --json               # budget check, ticket, then confirm >= 10 s later
```

`paper_autotrade.py` calls the skills' own CLIs, so every check runs as usual. It:
- skips crypto shorts and a symbol that already has a trade or an order;
- skips previews (market closed, past the time stop) and anything the budget blocks;
- confirms each ticket once it is at least 10 seconds old, which `broker.py` requires;
- writes one desk-log line per trade.

**Record every decision** (pass or skip, with the reason and the odds) in `$OUT/candidates.json`:
one row per symbol, as the dashboard's Candidates table shows.

Other broker commands, any account (`DAYTRADER_HOME=$SHADOW` for the shadow):

```bash
python3 $SK/broker/scripts/broker.py sync                       # fills working orders; stops, targets, time stops from 5m bars
python3 $SK/broker/scripts/broker.py positions|tickets|history --market crypto|us
python3 $SK/broker/scripts/broker.py close TRADE-ID [--price X] # --price must have traded since the fill (5m bars)
python3 $SK/budget/scripts/budget.py status --market crypto     # "RISKED SO FAR"
python3 $SK/analyst/scripts/analyst.py review                   # journal stats once trades exist
```

Exit codes across the pack: `0` ok; `1` bad input or state, including usage errors; `2` a data
source is unreachable (the one-line message names the host); `3` blocked by the daily budget.

## 4. The dashboard and the backups

- **What it is:** a claude.ai artifact of the **Dashboard** type, "Paper Trading Desk". Its URL
  is in the owner's handoff prompt. It has one dataset, `desk`: a JSON file asset holding one array
  of rows tagged by `kind`:
  - the desk: `account` (today, plus all-time `all_*` results), `plan`, `ticket`, `position`,
    `trade`;
  - the shadow: `shadow_account`, `shadow_position`, `shadow_trade`;
  - `equity`: cumulative P&L per account;
  - the scans and the log: `scanmeta`, `scan`, `candidate`, `candle`, `log`.

  The page (`files/index.html`) splits the rows with `dash.calc` and draws:
  - tiles for today;
  - **the two-week test**: desk vs shadow results and an equity chart;
  - the three scanner tables and the candidates;
  - a price chart, the plan and odds, open orders, positions and closed trades;
  - the shadow's trades and the Activity log.
- **Before the first write in a new session**, read the artifact once (`Artifact read`, or `list`
  with `scope: files`). The Artifact tool refuses a publish to an artifact the conversation hasn't
  read.
- **Every refresh replaces the whole dataset.** Pass every input each time, or those tables go
  empty. `desk_snapshot.py` exits 1 if an input path doesn't exist. A full refresh prints about
  `scanmeta 3, scan 32, candidate 10` plus the shadow rows.
- **Refresh**, after every run and every desk step worth seeing:
  1. Sync both accounts first: it expires stale tickets, fills working orders and applies stops,
     targets and time stops. On exit 2 (data down), carry on.
     ```bash
     for H in $DESK $SHADOW; do DAYTRADER_HOME=$H python3 $SK/broker/scripts/broker.py sync; done
     ```
  2. ```bash
     python3 $TOOLS/desk_snapshot.py --symbol BTC-USDT --out $OUT/desk.json \
       --scan $OUT/scan/crypto.json --scan $OUT/scan/us.json \
       --original-watchlist /home/user/original-desk/trader/watchlist.json \
       --candidates $OUT/candidates.json --shadow-home $SHADOW \
       --event "What happened" --detail "One plain line"
     ```
     `--symbol` is the charted symbol. `--event` appends to `$DESK/desk_log.jsonl`, which feeds
     Activity.
  3. `Artifact` publish with `url` = the dashboard, `file_path` = `$OUT/desk.json`,
     `asset: true`. It returns `/_blob/<id>`.
  4. `ArtifactData get {collection: "datasets", doc_id: "desk"}` for its `version`, then `update`
     with `{"source": {"kind": "file", "url": "/_blob/<id>", "name": "desk.json", "format":
     "json"}, "updated": {"at": "<ISO UTC>", "by": "Claude"}}` and `if_version`.
- **Back up both accounts** after anything that changes them. These are the only copies that
  survive a recycled container:
  1. The desk: `python3 $TOOLS/desk_backup.py --out $OUT/desk_state.json --candidates
     $OUT/candidates.json`. Then `ArtifactData get {collection: "data/users/me", doc_id:
     "desk_state"}` for the version, and `set` with `file_path` and `if_version`.
  2. The shadow: `DAYTRADER_HOME=$SHADOW python3 $TOOLS/desk_backup.py --out
     $OUT/shadow_state.json`. Then the same `get` and `set` on `doc_id: "shadow_state"`.

  `desk_backup.py` refuses a home without `desk_log.jsonl`: that one was never restored (a
  recycled container), and its backup would replace the real one.
- **The page's own code** is the store document `files/index.html` (`{text}`). Before editing it,
  read the type's `SKILL.md`, then `artifact-type/reference/one-writer.md` and
  `design-defaults.md` (`Artifact read` with `path`). The page follows the "one writer" rules:
  fixed text in HTML, data-driven parts as `data-draw` drawings, every number from the dataset.
  The test's dates are the constants in the `testday` calc. Write with `ArtifactData set` and
  `if_version`. Then read `data/users/me` doc `check`: it may say `checking`, or be absent until
  the owner opens the page.

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
  2. Re-check live with `entry.py SYM --setup level --level X --side long --cutoff 23:55`
     (stocks: `--cutoff 15:55`).
  3. Only a TRIGGERED close in the last 15 minutes is an idea (§3). A wick-only touch is a watch:
     the desk on 2026-10-31 followed one with `entry.py ... --watch --interval 60 --max-minutes
     115` and checked the close when it came.
- **The watcher** is a shell loop that writes its process id to `$OUT/watcher.pid`. Start one only
  if none is running, and check the file, **not `pgrep -f`**. `pgrep -f 'alerts.py check --json'`
  also matches the shell that runs the check, so it always says a watcher is running.

  ```bash
  P=$OUT/watcher.pid; [ -f $P ] && kill -0 "$(cat $P)" 2>/dev/null && echo "watcher running" || echo "no watcher"
  ```

  Launch it with the Bash tool's `run_in_background: true` and **`timeout: 7200000`**; the
  default 30-minute limit would kill it. 115 loops of about 60 s fit in the 2-hour maximum.
  Restart it once after a fire (for the remaining alerts) or after its window ends; the 2-hourly
  crypto run also restarts it. Never restart it after this session has stood down (§6).

  ```bash
  OUT=<this session's scratchpad>/desk; mkdir -p $OUT; P=$OUT/watcher.pid; echo $$ > $P
  cd /home/user/projects/day-trader && for i in $(seq 1 115); do
    out=$(python3 skills/alerts/scripts/alerts.py check --json 2>/dev/null)
    n=$(printf '%s' "$out" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('fired') or []))" 2>/dev/null || echo 0)
    if [ "${n:-0}" -gt 0 ] 2>/dev/null; then echo "ALERT FIRED $(date -u +%H:%M) UTC"; printf '%s' "$out"; rm -f $P; exit 0; fi
    sleep 60; done; rm -f $P; echo "watcher window ended, no alert"
  ```

## 6. Scheduled jobs, and taking them over after a session transfer

Scheduled tasks fire into the session that created them. The bot has two:

| Name | Schedule | Prompt |
|---|---|---|
| `Paper bot: crypto every 2 hours` | cron `10 */2 * * *` UTC (00:10, 02:10, ...), no end date | Restore if needed → sync both accounts → crypto scan (re-create missing stock scans) → alerts → ideas into the shadow, passes into the desk (§3) → refresh and back up both → the daily results line after 00:00 UTC → the two-week verdict after 2026-11-14 12:00 UTC → restart the watcher |
| `Paper bot: US stock sweep (weekdays 09:50 ET)` | cron `CRON_TZ=America/New_York 50 9 * * 1-5` | Restore if needed → sync → pinned original pack's ORB sweep → rank by `pct_over`, liquidity → shadow batch, then the desk (§3) → refresh and back up both |

- The crypto run is at :10, so on weekdays it is done well before the 09:50 ET sweep (14:50 UTC in
  winter). Runs starting between 6 and 9 AM Pacific can start late; the sweep is still valid a
  few minutes late.
- The owner's account also has an unrelated task, `PR review digest`. Leave it alone; it stays
  enabled.
- The new session must run in **Auto mode** (the mode dropdown next to the prompt box). In other
  modes every Bash and MCP call of an unattended run waits for an approval nobody gives.
  `get_session` with no args shows `permission_mode`.

**A session transfer goes in this order:**

1. **The owner tells the old session to stand down, right after a crypto run has finished**
   (about :20-:30 past an even UTC hour, once that run has replied). The new session then has
   until the next :10 run to re-create the jobs, so no run falls between the two sessions. On a
   weekday, also keep clear of 09:30-10:30 ET (the stock sweep). On 2026-10-31 the old jobs went
   off at 14:07 and the new ones came on at 14:12, so the 14:10 run was lost; a manual re-scan
   covered it. The old session then:
   - updates and disables both of its tasks by id (`update_trigger enabled: false`). Paused tasks
     keep their prompts;
   - stops every background task it started (the watcher, any `entry.py --watch`);
   - runs a last sync, refresh and backup of both accounts with `--event "Old session stood
     down"`. It writes the same line into `$SHADOW/desk_log.jsonl` (the one-liner in step 6)
     before that account's backup;
   - takes no more desk actions.
2. **The new session checks the stand-down.** These must hold:
   - `get_trigger` on both old ids shows `enabled: false`;
   - both backups' last `desk_log` event is `Old session stood down`.

   Until both hold: no restore, no jobs, no watcher. Scans and the news check can go ahead. Tell
   the owner.
3. **Restore** both accounts from those same backup files (§1.4).
4. **Re-create the jobs:** `get_trigger <old id>` → `derived_state.prompt`, copied verbatim. Then
   `create_trigger` with the same name, the cron above and `initiation: human_request`. **Check
   the copies by machine, not by eye.** Paste the old prompts' JSON strings (as `get_trigger`
   prints them) into `$OUT/jobs/old_prompts.json` as `{"crypto": "...", "stock": "..."}`, the new
   tasks' stored prompts into `$OUT/jobs/new_prompts.json` the same way, and compare them in
   Python: they must be equal (since the 3:1 rule of 2026-11-01: 5,642 and 3,706 characters).
5. **Verify** with `list_triggers` (`enabled: true` lists only the live tasks): of the desk's
   tasks, only the new pair is enabled, with `persistent_session_id` = this session
   (`get_session` with no args gives it). The `PR review digest` stays enabled.
6. **Write "New session took over" into both logs before the next :10 run.** The restored logs
   end with "Old session stood down", and the job prompts skip a run whose last `desk_log` event
   says that. The next transfer's check also reads both logs' last line, so a stale line left in
   the shadow's log would pass that check before the old session has really stood down.
   `desk_snapshot.py --event` writes only the desk's log, so write the shadow's first:

   ```bash
   DAYTRADER_HOME=$SHADOW python3 -c "import sys; sys.path.insert(0, '$TOOLS'); import desk_snapshot as ds; ds.append_log('New session took over', 'The shadow account moved to the new session with the desk.')"
   ```

   Then sync, refresh with `--event "New session took over"` and back up both accounts (§4). Read
   both backups back (`ArtifactData get`): each must end with that line. The Binance check (§11)
   can run meanwhile, but don't let it hold this step up: a deep web check took 31 minutes on
   2026-10-31.
7. Ask the owner whether to archive the old session. `archive_session` also kills its container.

**Rollback:** if the new session can't take over, the owner tells the old session to re-enable its
two tasks (`update_trigger enabled: true`) and restart its watcher.

## 7. Settings and limits

| Setting | Desk (`$DESK`) | Shadow (`$SHADOW`) | Change with |
|---|---|---|---|
| Paper account | $25,000 | $250,000 | `risk.py config --account N` |
| Risk per trade | **1% ($250)** (owner, 2026-11-01) | 0.1% ($250) | `risk.py config --risk-pct X` |
| Max position | **100% ($25,000)**, no borrowing | 10% ($25,000) | `risk.py config --max-position-pct X` |
| Target | **3R** (owner's 3:1 rule) | 3R | `exit_plan.py --rr 3`; the shadow: `paper_autotrade.py --rr 3` |
| Min reward:risk warning | 1.0 | 1.0 | `risk.py config --min-rr X` |
| Max trades a day | **20** (owner) | 500 | `budget.py set --max-trades N` |
| Daily max loss | **$1,250** (owner: 5 full losses) | $25,000 | `budget.py set --daily-max-loss X` |
| Max open risk | **$1,250** (owner) | $25,000 | `budget.py set --max-open-risk X` |
| Tilt guard | **20** losses in a row (owner) | 1,000 | `budget.py set --max-losses N` |
| Crypto exchange | Binance (default) | Binance | `risk.py config --crypto-exchange binance\|coinbase`, or `$DAYTRADER_CRYPTO_EXCHANGE` |
| Crypto paper fee | 0.1% per fill (follows the exchange; Coinbase 0.6%) | same | `risk.py config --crypto-fee-pct X` |
| Ticket life | 15 minutes | 15 minutes | `ticket_ttl_minutes` in `settings.json` |
| SEC contact | not set | not set | `$DAYTRADER_CONTACT` or `contact_email` in `settings.json`. **Ask the owner first**: it would also ride in the private backup |

The shadow's account is ten times larger at a tenth of the risk percentage. That way every trade
is sized exactly like the desk's ($250 risk, $25,000 cap), and the shadow can hold many positions at
once. Its limits are set high so they never stop an idea.

The position cap matters as much as the risk: a position's size is the smaller of risk ÷ stop
distance and the cap. On 2026-10-31 the old 25% cap ($6,250) left a BTC trade with a 0.19% stop
only about $12 at risk instead of $50; with the whole account as the cap, a stop of 1% or wider
gets the full $250, and tighter stops still get less (no borrowing).

Environment variables:

| Variable | Effect |
|---|---|
| `DAYTRADER_HOME` | state folder (the account) |
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
python3 -m unittest discover -s tests        # the full suite, about 2 minutes, offline
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
- **Public repo:** never put the owner's email, the dashboard URL, task ids, the old session's id,
  ledgers or keys in files. `.gitignore` blocks stray desk files (JSON at the root and in
  `day-trader/`, ledgers, alerts, logs, plans, `trader/`, PID files). The `Claude-Session` trailer
  in commit messages is expected. Never rewrite history.

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `data error: ... blocked by proxy ... may need to be allow-listed` (exit 2) | The environment's network policy. The owner adds the host under Network access → Allowed domains |
| Yahoo `HTTP 429` | Yahoo rate-limits shared cloud IPs for a while. Retry later; crypto is unaffected |
| SEC `HTTP 403` | No contact email in the User-Agent. Ask the owner about `DAYTRADER_CONTACT`; news and the brief skip SEC meanwhile |
| `api.binance.com` HTTP 451 | Binance blocks US IPs on its main API. The pack uses `data-api.binance.vision`, which works |
| `entry.py` says EXPIRED for a crypto setup | No `--cutoff`: it stopped looking at 02:00 UTC. Re-run with `--cutoff 23:55` (§3) |
| `budget.py check --symbol` says "no saved plan", or `ticket` asks for `--qty` | The plan wasn't copied to `$DESK/plans/` (§3) |
| A crypto plan "not found" | BTCUSDT and BTC-USDT share one plan file. **BTC-USD is a separate plan**, so use the `-USDT` spelling throughout |
| A stock ticket is only a PREVIEW | Market closed or past 15:55 ET. `paper_autotrade.py` skips it |
| `desk_restore.py` says "already holds a desk" | That account has newer local state. Keep it and back it up; restore with `--force` only if it is a stray file (say a setting written before the restore) |
| `desk_backup.py` says "never restored" | The container was recycled and the account wasn't restored yet. Restore first (§1.4) |
| Dashboard tables went empty | A refresh without that input (§4), or the dataset `update` was skipped or its `if_version` was stale |
| An open order still shows after its time | Sync wasn't run before the snapshot (§4) |
| An alert "fired" but nothing happened | It was a touch (wick) or happened hours ago. Re-check with `entry.py --setup level --cutoff 23:55` (§5) |
| The watcher never starts | A `pgrep -f` guard matches its own shell. Use the PID-file check (§5) |
| The disk fills up in a long-running session | The download cache keeps a new set of Binance daily candles every UTC day (about 46 MB a day, never pruned); the stock charts (about 86 MB) are overwritten in place. It is only a cache: `find /root/.day-trader/cache -type f -mtime +2 -delete`. A new session starts with an empty one |

## 10. The two-week test and the verdict

- **Dates:** Saturday 2026-10-31 to Saturday 2026-11-14. The bot keeps running afterwards until the
  owner says stop.
- **The question:** is the bot profitable, and does the desk's filter add money? The desk trades
  only passes; the shadow trades every idea with the default plan. Both use the same risk per
  trade, and both are judged net of paper fees. **The rules changed on day 2** (2026-11-01, the
  owner's 3:1 rule: 3R targets, $250 a trade, a +0.2R bar); trades before that used 2R and $50.
  Report the two periods separately.
- **Each day** (the first crypto run after 00:00 UTC): an Activity line `Day <n> results` with both
  accounts' all-time P&L, closed trades and win rate.
- **The verdict** (the first crypto run after 2026-11-14 12:00 UTC, once):
  - `analyst.py review` for both accounts (`DAYTRADER_HOME=$SHADOW` for the shadow): win rate,
    average R, profit factor, expectancy, max drawdown, by market, setup and hour;
  - desk vs shadow: the filter's value is the desk's P&L per idea minus the shadow's;
  - a plain verdict: profitable or not, and why, with the sample size. A few dozen trades is a
    first read, not proof.

  The verdict goes to Activity (`--event "Two-week verdict"`) and to a push notification, and the
  full text goes in the reply.

## 11. The Binance hack: checking the status

Binance was hacked on 2026-10-29/30 and suspended withdrawals
([HISTORY](HISTORY.md#2026-10-31-afternoon-the-first-session-transfer)). Paper trading is
unaffected: the bot only reads Binance's public prices. Real money waits until withdrawals have
reopened and the reimbursement is independently confirmed. Binance's own public feeds answer the
first question in seconds, with no account and no keys:

```bash
D=$OUT/dl-binance; rm -rf "$D"; mkdir -p "$D"     # downloads are data: their own folder, read with python3 -I
curl -sS --max-time 30 -o "$D/coins.json" "https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll"
python3 -I -c "import json,sys; d=json.load(open(sys.argv[1]))['data']; n=[x for c in d for x in c.get('networkList') or []]; print(len(n), 'networks:', sum(bool(x.get('withdrawEnable')) for x in n), 'with withdrawals on,', sum(bool(x.get('depositEnable')) for x in n), 'with deposits on')" "$D/coins.json"
curl -sS --max-time 30 -o "$D/news.json" "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=49&pageNo=1&pageSize=5"
python3 -I -c "import json,sys,datetime as t; [print(t.datetime.fromtimestamp(a['releaseDate']/1000, t.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'), a['title']) for c in json.load(open(sys.argv[1]))['data']['catalogs'] for a in c['articles']]" "$D/news.json"
```

- The first feed is the live data behind Binance's deposit and withdrawal page. On 2026-10-31 at
  14:47 UTC: 1,447 networks, 0 with withdrawals on, 1,048 with deposits on. The second lists
  Binance's latest announcements, where its "Recovery Update" posts appear.
- Both are Binance website endpoints, not a documented API, and may change. If one fails, use
  `news.py BTC-USDT --since 24h`, then WebSearch in **extended** mode. Standard mode missed the
  story on 2026-10-31, and search results lagged Binance's own posts by 15 minutes or more.
- "Reimbursement confirmed" needs more than Binance's word: its zero-knowledge proof of reserves
  (due 2026-11-03) or an independent check (an auditor, a regulator, on-chain analysts).
  "Pledged" and "90% replaced" are Binance's own figures.
- Tell the owner when withdrawals reopen and when the reimbursement is independently confirmed.

## 12. Watch Binance live

When the owner asks what is happening on Binance right now, listen to its public live stream:

```bash
python3 $TOOLS/binance_live.py                                       # 15 s of BTC-USDT and ETH-USDT
python3 $TOOLS/binance_live.py --symbols SOL-USDT,ZAMA-USDT --seconds 30 --json
```

- Per pair it shows the trades, the dollars traded and how much of that was buyers lifting the
  ask, the price range, the last price and the biggest trade. For the first pair it also shows the
  forming 1-minute candle and the best bid and ask with the size on the top five levels.
- It reads `data-stream.binance.vision`, the public market-data WebSocket: no account, no keys,
  nothing that can place an order. From this server the main stream (`stream.binance.com`), the
  main API and the Spot Testnet answer 451 (restricted location).
- The dashboard can't show it live: claude.ai pages block every outside connection, WebSockets
  included. A live reading reaches the owner in chat, or on the dashboard at the next refresh.
- Exit codes as everywhere: 0 ok, 1 bad input, 2 the stream is unreachable.
