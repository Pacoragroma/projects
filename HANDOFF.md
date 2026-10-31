# Session handoff: start here

**Short version: this repo holds `day-trader`, our own MIT rebuild of the 11-skill "Day Trader" pack
from the owner's promo video, extended to trade crypto 24/7 on Binance prices. It is installed in
Claude Code next to the video's original pack. A paper desk is live: $25,000 of paper money, no
trades yet, a dashboard the owner watches, three price alerts and two scheduled jobs. Nothing here
can place a real order.**

State as of **Saturday 2026-10-31, 10:30 UTC** (commit `bbdc1a4` + these docs, version 1.2.0).

Read in this order:

1. [`CLAUDE.md`](CLAUDE.md): how to work with the owner (short version first, bullets, push back,
   one recommendation). **This repo is public: everything committed here is published.**
2. This file: where things are, what is running, what to do first.
3. [`day-trader/docs/RUNBOOK.md`](day-trader/docs/RUNBOOK.md): the commands that operate the desk,
   the dashboard, the backups and the scheduled jobs.
4. [`day-trader/docs/ARCHITECTURE.md`](day-trader/docs/ARCHITECTURE.md): how the code is built.
5. [`day-trader/docs/HISTORY.md`](day-trader/docs/HISTORY.md): what the owner asked for, what was
   decided and found, in order.

## What exists

| Thing | Where | Notes |
|---|---|---|
| Our skill pack, 11 skills, v1.2.0 | `day-trader/` on branch `claude/day-trader-skills` (the repo's default branch) | Stdlib Python 3.9+, 942 offline tests, CI on 3.9 and 3.12 |
| The video's original pack, v2.0.0 | github.com/Jakeschincariol/day-trader-skill (MIT, Jake Schincariol) | US stocks only. Installed as a plugin; not vendored here |
| Installed plugins (Claude Code, user scope) | `day-trader@pacoragroma-projects` 1.2.0 and `day-trader-skill@day-trader-skill` 2.0.0 | A fresh container may need them reinstalled ([RUNBOOK](day-trader/docs/RUNBOOK.md#1-set-up-a-fresh-container)) |
| The paper desk's state | `~/.day-trader/` in the container: `settings.json`, `alerts.json`, `desk_log.jsonl`, later `ledger.json` and `plans/` | Lost when a container is recycled, so it is backed up (next row) |
| Backup of that state | The dashboard's private store: collection `data/users/me`, doc `desk_state` | Restore it first in a new container |
| The live dashboard | A claude.ai artifact named **"Paper Trading Desk"** (Dashboard type) | Its URL is in the owner's handoff prompt, or in their claude.ai artifacts. Private to the owner |
| The original pack's workspace | `/home/user/original-desk/trader/` in the container (`universe.txt`, `watchlist.json`) | Rebuilt by running its scanner; not in the repo |

## The desk right now

- **Money:** $25,000 paper account. No tickets, no positions, no closed trades. Status OPEN FOR
  TRADING.
- **Limits (owner's choice):** $50 risk per trade (0.2%), max 20 trades a day, $1,000 daily max
  loss, $500 max open risk, tilt guard at 20 losses in a row, max position 25% of the account.
- **Crypto prices:** Binance public data (`data-api.binance.vision`), no account and no keys.
  Paper fee 0.1% per fill. Coinbase stays selectable.
- **Alerts** (`once`): BTC-USDT above 77,654.44, ETH-USDT above 2,262.49, ZAMA-USDT above 0.0699.
  These are the levels where the desk said a long would be worth re-checking. A watcher loop
  checked them every minute in the old container; it does not survive a container change.
- **Last results:** 6,420 charts scanned on 2026-10-31 (513 Binance pairs, 5,907 US stocks), plus
  the original pack's daily scan (470 setups among 1,590 liquid stocks). 10 candidates went through
  the whole desk, and **all 10 were skipped**: no edge after fees and noise. Details are in
  [HISTORY](day-trader/docs/HISTORY.md#2026-10-31-the-first-full-scan-and-candidate-check).
- **News to know:** Binance was hacked on Oct 29-30 (about $6.3B, the largest crypto theft on
  record). Binance pledges to cover users from SAFU and its own funds, but completion is not
  confirmed and withdrawals were suspended. Paper trading is unaffected. Real money should wait.

## What is scheduled

Both scheduled tasks were created by the **old** session and fire **into the old session**. A new
session should take them over: create its own copies (they then fire into the new session), then
disable the old ones by name. See
[RUNBOOK §6](day-trader/docs/RUNBOOK.md#6-scheduled-jobs-and-taking-them-over).

| Name | When | What |
|---|---|---|
| `Crypto desk re-scan (weekend)` | Every 2 hours at :40 UTC, until Monday 2026-11-02 14:00 UTC (then it disables itself) | Re-scan Binance, check alerts, run the desk on anything new, refresh the dashboard, back up state |
| `Monday stock sweep at the open` | Once, Monday 2026-11-02 14:50 UTC (09:50 ET) | The original pack's intraday opening-range sweep, our desk on its hits, paper tickets for the owner's yes |

## First steps in a new session

1. Read `CLAUDE.md` and this file.
2. Set up the container ([RUNBOOK §1](day-trader/docs/RUNBOOK.md#1-set-up-a-fresh-container)):
   check the network, reinstall the two plugins if missing, and restore `~/.day-trader` from the
   dashboard backup.
3. Take over the scheduled jobs and restart the alert watcher
   ([RUNBOOK §5-6](day-trader/docs/RUNBOOK.md#5-alerts-and-the-watcher)).
4. Re-run the three scans ([RUNBOOK §2](day-trader/docs/RUNBOOK.md#2-scan-for-setups): crypto,
   US daily, the original pack's daily). A dashboard refresh replaces everything, so without the
   scan files its scanner tables would go empty. Then refresh the dashboard
   ([RUNBOOK §4](day-trader/docs/RUNBOOK.md#4-the-dashboard)) with the backed-up candidates and an
   Activity entry "New session took over", and back up the state.
5. Tell the owner in a few lines what is live and what is next, in the `CLAUDE.md` style.

## Rules that must keep holding

- **Paper only.** No live mode, no broker connection, no exchange keys. The original pack's broker
  can talk to an Alpaca **paper** account, but only if the owner sets up Alpaca's MCP server with
  paper keys. That has not been done.
- **No trade without the owner's yes.** Create tickets, show them, wait. Run `broker.py confirm ...
  --yes` only after the owner's explicit yes, in a later message, for that ticket id. Tickets
  expire after 15 minutes.
- **Don't force trades.** A skip is a valid desk outcome. Pass rules are in
  [RUNBOOK §3](day-trader/docs/RUNBOOK.md#3-take-a-candidate-through-the-desk).
- **Public repo.** Never commit the owner's email, dashboard URL, scheduled-task ids, ledger or
  keys. Commits go to `claude/day-trader-skills`; run the checks first
  ([RUNBOOK §8](day-trader/docs/RUNBOOK.md#8-change-the-code-and-release)).
- **SEC EDGAR** wants a contact email in the User-Agent (`DAYTRADER_CONTACT`). Ask the owner
  before using theirs. Without it SEC sometimes answers 403, and news and the analyst brief simply
  skip SEC.

## Open items

- Monday's sweep is the first real stock-trading moment. The owner needs to be around after
  14:50 UTC to say yes to any ticket.
- The original pack's broker plus Alpaca paper account is optional. Offered, not chosen.
- Our scanner has no intraday opening-range sweep. The original pack's `scan.py orb` covers it.
  Porting it into our scanner (MIT, with credit) was offered as an alternative and not chosen.
- Crypto and stocks share one paper ledger. Daily-limit days follow the clock of the market being
  checked: ET for stocks, UTC for crypto. This is documented in the README. A separate
  `DAYTRADER_HOME` per market is the clean fix if the owner trades both heavily.
- Backlog ideas are in [HISTORY](day-trader/docs/HISTORY.md#backlog-and-ideas-not-started).
