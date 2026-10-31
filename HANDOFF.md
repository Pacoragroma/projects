# Session handoff: start here

**Short version: this repo holds `day-trader`, our own MIT rebuild of the 11-skill "Day Trader" pack
from the owner's promo video, extended to trade crypto 24/7 on Binance prices. It runs as an
autonomous paper bot in a two-week test (2026-10-31 to 2026-11-14). The desk trades only ideas
that pass its rules. A shadow account trades every idea, so the two can be compared. The owner's
standing order: paper trades need no per-ticket yes. Nothing here can place a real order.**

State as of **Saturday 2026-10-31, about 14:00 UTC**, version 1.2.1, the latest commit on
`claude/day-trader-skills`.

Read in this order:

1. [`CLAUDE.md`](CLAUDE.md): how to work with the owner (short version first, bullets, push back,
   one recommendation). **This repo is public: everything committed here is published.**
2. This file: where things are, what is running, what to do first.
3. [`day-trader/docs/RUNBOOK.md`](day-trader/docs/RUNBOOK.md): the commands that run the bot,
   the dashboard, the backups and the scheduled jobs, and how a session transfer goes.
4. [`day-trader/docs/ARCHITECTURE.md`](day-trader/docs/ARCHITECTURE.md): how the code is built.
5. [`day-trader/docs/HISTORY.md`](day-trader/docs/HISTORY.md): what the owner asked for, what was
   decided and found, in order.

The owner's handoff prompt carries what is deliberately kept out of the repo's files: the
dashboard's link, the old session's id and the scheduled tasks' ids.

## What exists

| Thing | Where | Notes |
|---|---|---|
| Our skill pack, 11 skills, v1.2.1 | `day-trader/` on branch `claude/day-trader-skills` (the repo's default branch) | Stdlib Python 3.9+, 975 offline tests, CI on 3.9 and 3.12 |
| The bot's tools | `day-trader/tools/` | `paper_autotrade.py` (ticket and confirm under the standing order), `desk_snapshot.py` (the dashboard data), `desk_backup.py`, `desk_restore.py`, `smoke_live.py` |
| The video's original pack, v2.0.0 | github.com/Jakeschincariol/day-trader-skill (MIT, Jake Schincariol) | US stocks only; its ORB sweep feeds the stock runs. The jobs run a clone pinned to the audited commit `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd` at `/home/user/day-trader-skill/` |
| **The desk** (main paper account) | `/root/.day-trader/` in the container | Trades only the ideas that pass the desk's rules |
| **The shadow account** | `/root/.day-trader-shadow/` in the container | Trades every idea the desk checks, with the default plan. Same $50 risk and $6,250 cap per trade |
| Backups of both | The dashboard's private store: collection `data/users/me`, docs `desk_state` and `shadow_state` | Lost containers are restored from these ([RUNBOOK §1.4](day-trader/docs/RUNBOOK.md#1-set-up-a-fresh-container)) |
| The live dashboard | A claude.ai artifact named **"Paper Trading Desk"** (Dashboard type) | The two-week test (desk vs shadow, equity chart), scans, candidates, trades, Activity. Private to the owner |
| The original pack's workspace | `/home/user/original-desk/trader/` in the container (`universe.txt`, `watchlist.json`) | Rebuilt by running its scanner; not in the repo |

## The bot right now

- **Money:**
  - the desk: $25,000 paper;
  - the shadow: $250,000 paper at 0.02% risk, so every trade is sized like the desk's;
  - no trades yet in either.
- **Limits (owner's choice, the desk):** $50 risk per trade (0.2%), max 20 trades a day, $1,000
  daily max loss, $500 max open risk, tilt guard at 20 losses in a row, max position 25% of the
  account. The shadow's limits are set high so they never stop an idea.
- **Crypto prices:** Binance public data (`data-api.binance.vision`), no account and no keys.
  Paper fee 0.1% per fill. Coinbase stays selectable.
- **Alerts** (`once`): ETH-USDT above 2,262.49 and ZAMA-USDT above 0.0699 are armed.
  - BTC-USDT above 77,654.44 fired at 11:49 UTC on a wick and is spent.
  - A real 5-minute close above it came at 12:40 UTC. The desk tested it with three stops and
    skipped it: the odds leaned against every time (best −0.83R after fees).
  - Alerts fire on a touch, so re-check live before acting
    ([RUNBOOK §5](day-trader/docs/RUNBOOK.md#5-alerts-and-the-watcher)).
- **Results so far:**
  - 13 ideas checked on 2026-10-31, **none traded**: none had an edge after fees and noise.
  - The shadow account started at about 13:00 UTC, so its first trades come with the next live
    idea.
  - Details are in
    [HISTORY](day-trader/docs/HISTORY.md#2026-10-31-the-first-full-scan-and-candidate-check).
- **News to know:** Binance was hacked on Oct 29-30 (about $6.3B, the largest crypto theft on
  record). Binance pledges to cover users from its SAFU reserve and its own funds. Completion is
  not confirmed, and withdrawals were suspended. Paper trading is unaffected. Real money should
  wait. Sources are in [HISTORY](day-trader/docs/HISTORY.md#2026-10-31-the-owners-questions-and-choices).

## What is scheduled

Two scheduled tasks run the bot
([RUNBOOK §6](day-trader/docs/RUNBOOK.md#6-scheduled-jobs-and-taking-them-over-after-a-session-transfer)).
They fire into the session that created them. The owner's account also has an unrelated
`PR review digest` task: leave it alone.

| Name | When | What |
|---|---|---|
| `Paper bot: crypto every 2 hours` | Every 2 hours at :10 UTC, no end date | Sync both accounts, scan Binance, check alerts, ideas into the shadow and passes into the desk, refresh the dashboard, back up both, a daily results line, the verdict on 2026-11-14 |
| `Paper bot: US stock sweep (weekdays 09:50 ET)` | Mon-Fri 09:50 ET (14:50 UTC in winter) | The original pack's opening-range sweep, the top liquid hits into the shadow, passes into the desk, refresh and back up |

## A session transfer, in order

The full steps are in
[RUNBOOK §6](day-trader/docs/RUNBOOK.md#6-scheduled-jobs-and-taking-them-over-after-a-session-transfer).
This must be a claude.ai cloud session in **Auto mode**: scheduled runs happen with nobody
watching, and in other modes they stall on permission prompts.

1. **The owner tells the old session to stand down.** It disables its two tasks, stops its
   background tasks, and writes final backups of both accounts whose last log line is "Old session
   stood down".
2. **The new session syncs and reads.** `git fetch origin && git checkout claude/day-trader-skills
   && git pull --ff-only`, then `CLAUDE.md`, this file and the RUNBOOK.
3. **It sets up the container** ([RUNBOOK §1](day-trader/docs/RUNBOOK.md#1-set-up-a-fresh-container)):
   the network check, the pinned original pack and its universe.
4. **It checks the stand-down:** both old tasks disabled, and both backups end with "Old session
   stood down". Until then: no restore, no jobs, no watcher.
5. **It restores both accounts** (only where `settings.json` is missing).
6. **It re-creates the two jobs** from the old prompts (`get_trigger`, then `create_trigger`).
   Then it checks `list_triggers`.
7. **It starts the alert watcher:** a PID-file check first, then Bash `run_in_background: true`,
   `timeout: 7200000`.
8. **It re-runs the scans** ([RUNBOOK §2](day-trader/docs/RUNBOOK.md#2-scan-for-setups)): crypto and
   US daily into the scratchpad, the original pack's daily in `/home/user/original-desk`.
9. **It re-checks the Binance news:** `news.py BTC-USDT --since 24h`, then WebSearch in
   extended mode (standard mode missed it on 2026-10-31).
10. **It syncs, refreshes the dashboard and backs up both accounts**
    ([RUNBOOK §4](day-trader/docs/RUNBOOK.md#4-the-dashboard-and-the-backups)), with an Activity
    entry "New session took over".
11. **It tells the owner** in a few lines, in the `CLAUDE.md` style:
    - what is live;
    - both accounts' results;
    - the Binance status;
    - what runs next.

    Then it asks whether to archive the old session.

## Rules that must keep holding

- **Paper only.** No live mode, no broker connection, no exchange keys. The original pack's broker
  can talk to an Alpaca **paper** account, but only if the owner sets up Alpaca's MCP server with
  paper keys. That has not been done.
- **The standing order (2026-10-31):** paper trades need no per-ticket yes. Claude confirms the
  desk's passing tickets itself with `paper_autotrade.py`, and the shadow trades every idea the
  desk checks. Everything else still holds: the pass rules, the budget limits, paper only.
- **Restore first, every turn:** before any desk command, if an account's `settings.json` is
  missing, the container was recycled. Restore it before doing anything else.
- **Don't force trades.** The desk's rules decide; a skip is a valid outcome. The ideas that count
  are fixed for the test ([RUNBOOK §3](day-trader/docs/RUNBOOK.md#3-ideas-the-shadow-takes-each-one-the-desk-only-the-ones-that-pass)).
- **Public repo.** Never put the owner's email, the dashboard link, scheduled-task ids, the old
  session's id, ledgers or keys in the repo's files. Write desk outputs to the scratchpad, never
  inside the repo. The `Claude-Session` trailer in commit messages is expected. Never rewrite
  history. Commits go to `claude/day-trader-skills`; run the checks first
  ([RUNBOOK §8](day-trader/docs/RUNBOOK.md#8-change-the-code-and-release)).
- **SEC EDGAR** wants a contact email in the User-Agent (`DAYTRADER_CONTACT`, or `contact_email`
  in `settings.json`). Ask the owner before using theirs. Without it SEC sometimes answers 403,
  and news and the analyst brief simply skip SEC.

## Open items

- **The two-week verdict** is due on Saturday 2026-11-14, from the first crypto run after 12:00 UTC
  ([RUNBOOK §10](day-trader/docs/RUNBOOK.md#10-the-two-week-test-and-the-verdict)). The question:
  is the bot profitable, and does the desk's filter add money?
- **Binance customer funds:** the owner asked whether they are safe. Each crypto run checks the
  news. Tell the owner when reimbursement is confirmed and withdrawals have reopened. Until then,
  paper only.
- The original pack's broker plus an Alpaca paper account is optional. Offered, not chosen.
- Our scanner has no intraday opening-range sweep. The original pack's `scan.py orb` covers it.
  Porting it into our scanner (MIT, with credit) was offered as an alternative and not chosen.
- Backlog ideas are in [HISTORY](day-trader/docs/HISTORY.md#backlog-and-ideas-not-started).
