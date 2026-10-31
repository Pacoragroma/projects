# Session handoff: start here

**Short version: this repo holds `day-trader`, our own MIT rebuild of the 11-skill "Day Trader" pack
from the owner's promo video, extended to trade crypto 24/7 on Binance prices. It is installed in
Claude Code next to the video's original pack. A paper desk is live: $25,000 of paper money, no
trades yet, a dashboard the owner watches, three price alerts and two scheduled jobs. Nothing here
can place a real order.**

State as of **Saturday 2026-10-31, 11:15 UTC**, version 1.2.1, the latest commit on
`claude/day-trader-skills`.

Read in this order:

1. [`CLAUDE.md`](CLAUDE.md): how to work with the owner (short version first, bullets, push back,
   one recommendation). **This repo is public: everything committed here is published.**
2. This file: where things are, what is running, what to do first.
3. [`day-trader/docs/RUNBOOK.md`](day-trader/docs/RUNBOOK.md): the commands that operate the desk,
   the dashboard, the backups and the scheduled jobs.
4. [`day-trader/docs/ARCHITECTURE.md`](day-trader/docs/ARCHITECTURE.md): how the code is built.
5. [`day-trader/docs/HISTORY.md`](day-trader/docs/HISTORY.md): what the owner asked for, what was
   decided and found, in order.

The owner's handoff prompt carries what is deliberately kept out of the repo's files: the
dashboard's link, the old session's id and the old scheduled tasks' ids.

## What exists

| Thing | Where | Notes |
|---|---|---|
| Our skill pack, 11 skills, v1.2.1 | `day-trader/` on branch `claude/day-trader-skills` (the repo's default branch) | Stdlib Python 3.9+, 945 offline tests, CI on 3.9 and 3.12 |
| The video's original pack, v2.0.0 | github.com/Jakeschincariol/day-trader-skill (MIT, Jake Schincariol) | US stocks only. Installed as a plugin, not vendored here. Audited at commit `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd` |
| Installed plugins (Claude Code, user scope) | `day-trader@pacoragroma-projects` 1.2.1 and `day-trader-skill@day-trader-skill` 2.0.0 | A fresh container may need them reinstalled ([RUNBOOK §1](day-trader/docs/RUNBOOK.md#1-set-up-a-fresh-container)) |
| The paper desk's state | `~/.day-trader/` in the container: `settings.json`, `alerts.json`, `desk_log.jsonl`, later `ledger.json` and `plans/` | Lost when a container is recycled, so it is backed up (next row) |
| Backup of that state | The dashboard's private store: collection `data/users/me`, doc `desk_state` | Built by `day-trader/tools/desk_backup.py`. Restore with `day-trader/tools/desk_restore.py`, **only if `~/.day-trader/settings.json` is missing** |
| The live dashboard | A claude.ai artifact named **"Paper Trading Desk"** (Dashboard type) | Fed by `day-trader/tools/desk_snapshot.py`. Private to the owner |
| The original pack's workspace | `/home/user/original-desk/trader/` in the container (`universe.txt`, `watchlist.json`) | Rebuilt by running its scanner; not in the repo |

## The desk right now

- **Money:** $25,000 paper account. No tickets, no positions, no closed trades. Status OPEN FOR
  TRADING.
- **Limits (owner's choice):** $50 risk per trade (0.2%), max 20 trades a day, $1,000 daily max
  loss, $500 max open risk, tilt guard at 20 losses in a row, max position 25% of the account.
- **Crypto prices:** Binance public data (`data-api.binance.vision`), no account and no keys.
  Paper fee 0.1% per fill. Coinbase stays selectable.
- **Alerts** (`once`): BTC-USDT above 77,654.44, ETH-USDT above 2,262.49, ZAMA-USDT above 0.0699.
  These are the levels where a long would be worth re-checking. They fire on a touch (a 5-minute
  bar's high), so re-check live before acting
  ([RUNBOOK §5](day-trader/docs/RUNBOOK.md#5-alerts-and-the-watcher)). A watcher loop checks them
  every minute in the old container. It does not survive a container change.
- **Last results:**
  - 6,420 charts scanned on 2026-10-31: 513 Binance pairs and 5,907 US stocks.
  - The original pack's daily scan: 470 hits among 1,590 liquid stocks.
  - 10 candidates went through the whole desk, and **all 10 were skipped**: no edge after fees and
    noise. Details are in
    [HISTORY](day-trader/docs/HISTORY.md#2026-10-31-the-first-full-scan-and-candidate-check).
  - The 10:41 UTC re-scan found nothing new: 2 setups, neither a long worth taking, and no alert
    fired.
- **News to know:** Binance was hacked on Oct 29-30 (about $6.3B, the largest crypto theft on
  record). Binance pledges to cover users from its SAFU reserve and its own funds. Completion is
  not confirmed, and withdrawals were suspended. Paper trading is unaffected. Real money should
  wait. Sources are in [HISTORY](day-trader/docs/HISTORY.md#2026-10-31-the-owners-questions-and-choices).

## What is scheduled

Both scheduled tasks were created by the **old** session and fire **into the old session**. A new
session takes them over:
- it copies their prompts with `get_trigger` and re-creates them, so they fire into the new session;
- it disables the old ones **by id** (the copies share their names);
- it tells the old session to stand down.

See [RUNBOOK §6](day-trader/docs/RUNBOOK.md#6-scheduled-jobs-and-taking-them-over-after-a-session-transfer).
The owner's account also has an unrelated task, `PR review digest`. Leave it alone.

| Name | When | What |
|---|---|---|
| `Crypto desk re-scan (weekend)` | Every 2 hours at :40 UTC, until Monday 2026-11-02 14:00 UTC (then it disables itself) | Re-scan Binance (and re-create missing stock scans), check alerts, run the desk on anything new, refresh the dashboard, back up state, restart the watcher |
| `Monday stock sweep at the open` | Once, Monday 2026-11-02 14:50 UTC (09:50 ET: US clocks go back on Nov 1) | The original pack's intraday opening-range sweep, our desk on its hits, paper tickets with a push notification for the owner's yes |

## First steps in a new session

This must be a claude.ai cloud session: scheduled tasks and session messages don't exist in a
local CLI.

1. **Sync and read.** `git fetch origin && git checkout claude/day-trader-skills && git pull
   --ff-only`, then read `CLAUDE.md`, this file and the RUNBOOK.
2. **Set up the container** ([RUNBOOK §1](day-trader/docs/RUNBOOK.md#1-set-up-a-fresh-container)):
   - network check;
   - both plugins, checking the original's audited commit;
   - the original pack's universe.
3. **Wait until the old session is idle** (`get_session`, `get_trigger`). A run in flight writes a
   newer backup.
4. **Restore `~/.day-trader` from the backup only if `settings.json` is missing.** Otherwise keep
   the local state: it is newer.
5. **Take over the jobs** ([RUNBOOK §6](day-trader/docs/RUNBOOK.md#6-scheduled-jobs-and-taking-them-over-after-a-session-transfer)):
   - copy both prompts with `get_trigger`;
   - re-create them with `create_trigger` (cron `40 */2 * * *`) and `send_later`;
   - disable the old two by id and verify with `list_triggers`;
   - then `send_message` the old session "Handoff done".
6. **Start the alert watcher**, unless one is running: Bash `run_in_background: true`,
   `timeout: 7200000` ([RUNBOOK §5](day-trader/docs/RUNBOOK.md#5-alerts-and-the-watcher)).
7. **Re-run the three scans** to absolute paths in the scratchpad
   ([RUNBOOK §2](day-trader/docs/RUNBOOK.md#2-scan-for-setups)): crypto, US daily, and the original
   pack's daily. A dashboard refresh replaces everything, so without the scan files its scanner
   tables go empty.
8. **Re-check the Binance news** (`news.py BTC-USDT --since 24h`, plus a web search): have
   withdrawals reopened, and is reimbursement confirmed?
9. **Refresh the dashboard and back up** ([RUNBOOK §4](day-trader/docs/RUNBOOK.md#4-the-dashboard)):
   - the backed-up candidates;
   - an Activity entry "New session took over".
10. **Tell the owner** in a few lines, in the `CLAUDE.md` style:
    - what is live;
    - the Binance status;
    - that they need to be on the app **Monday 14:50-15:45 UTC** (tickets expire after 15 minutes);
    - ask whether to archive the old session.

## Rules that must keep holding

- **Paper only.** No live mode, no broker connection, no exchange keys. The original pack's broker
  can talk to an Alpaca **paper** account, but only if the owner sets up Alpaca's MCP server with
  paper keys. That has not been done.
- **No trade without the owner's yes.** Create tickets, send a push notification, show them, wait.
  Run `broker.py confirm ... --yes` only after the owner's explicit yes, in a later message, for
  that ticket id. Tickets expire after 15 minutes.
- **Don't force trades.** A skip is a valid desk outcome. Pass rules are in
  [RUNBOOK §3](day-trader/docs/RUNBOOK.md#3-take-a-candidate-through-the-desk).
- **Public repo.** Never put the owner's email, the dashboard link, scheduled-task ids, ledgers or
  keys in the repo's files. Write desk outputs to the scratchpad, never inside the repo. Session
  links in commit trailers are expected. Commits go to `claude/day-trader-skills`; run the checks
  first ([RUNBOOK §8](day-trader/docs/RUNBOOK.md#8-change-the-code-and-release)).
- **SEC EDGAR** wants a contact email in the User-Agent (`DAYTRADER_CONTACT`, or `contact_email`
  in `settings.json`). Ask the owner before using theirs. Without it SEC sometimes answers 403,
  and news and the analyst brief simply skip SEC.

## Open items

- **Monday's sweep** is the first real stock-trading moment. The owner needs to be around from
  14:50 UTC to say yes to any ticket. Extra sweeps at 10:05 and 10:20 ET are to be offered then.
- **Binance customer funds:** the owner asked whether they are safe. Each crypto run checks the
  news. Tell the owner when reimbursement is confirmed and withdrawals have reopened. Until then,
  paper only.
- The original pack's broker plus an Alpaca paper account is optional. Offered, not chosen.
- Our scanner has no intraday opening-range sweep. The original pack's `scan.py orb` covers it.
  Porting it into our scanner (MIT, with credit) was offered as an alternative and not chosen.
- Crypto and stocks share one paper ledger. Daily-limit days follow the clock of the market being
  checked: ET for stocks, UTC for crypto. This is documented in the README. A separate
  `DAYTRADER_HOME` per market is the clean fix if the owner trades both heavily.
- Backlog ideas are in [HISTORY](day-trader/docs/HISTORY.md#backlog-and-ideas-not-started).
