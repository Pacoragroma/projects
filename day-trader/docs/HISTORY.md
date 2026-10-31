# History: what was asked, decided and found

**Short version: the owner sent a promo video of an 11-skill Claude day-trading pack and asked
"can we implement this". We rebuilt it from scratch, then added 24/7 crypto, then switched crypto
prices to Binance at the owner's request, then installed the video's original pack alongside.
First full live run on 2026-10-31: 6,420 charts scanned, 10 candidates checked, no trade worth
taking. Then the handoff docs for a session transfer, checked by two rounds of rehearsal and
audit. Then the owner made it an autonomous paper bot: a two-week test of the desk against a
shadow account that takes every idea.**

## The video (the spec)

A 74-second vertical promo (by @auraguy.ai) for "the Trader skill" v2: 11 Claude skills "like a
little trading desk living inside Claude". The narration, word for word from the burned-in
captions:

> Claude just killed day traders. Now you can plan and test your trades inside Claude, and it's
> completely free. So what is it? Well, it's called the Trader skill, and it's not one skill. It's
> 11, like a little trading desk living inside Claude. One scans thousands of charts for setups.
> Another tracks breaking news that could move those stocks. One watches for the exact moment a
> setup triggers, and another sets your stop and your target before you even get in. But the
> craziest one is the Time Machine skill. And what it does is it takes your trade and simulates the
> rest of the trading day 10,000 times using historical price data. Then, it shows how often you
> would have hit your target, hit your stop, or ended somewhere in between. So you see the honest
> odds, before you risk a single dollar. And nothing goes through without your yes. It starts in
> paper trading, so you can try it without risking real money. It's a simulation, not a guarantee,
> and this isn't financial advice. And setup is super easy. Just paste the link into Claude and
> say, install skill.

Key visuals:

- "SCANNING ≈5,900 US STOCKS": a wall of daily charts, with one standing out as "TSLA ·
  BREAKOUT".
- News card with EARNINGS / DEAL tags ("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of
  Earnings").
- TSLA 5-minute opening range "TRIGGERED 10:15".
- THE PLAN: entry 364.18 / stop 358.32 / target 370.04.
- Time Machine: target first 29.4%, stop first 25.5%, the rest open at the close; "CLOSE TO A
  COIN FLIP"; "RISKED SO FAR: $0".
- "BUY 8 TSLA · WAITING FOR YOUR YES", PAPER vs LIVE, "NOT A GUARANTEE / NOT FINANCIAL ADVICE".

**The owner's own reading (2026-10-31):** "we're trying to look through a big number of charts
to see discrepancies, to see something that we can take advantage of and trade with". The scanner
is the heart of it; the rest of the desk decides whether a standout is actually tradeable.

## Timeline

### 2026-10-30: the rebuild (v1.0)

- The repo was empty and the network blocked every data host, so the pack was written against
  documented formats and synthetic fixtures. The original repo's URL was cut off in the video.
- Built by parallel agents (one per skill), each followed by an independent reviewer that fixed
  only bugs it could first reproduce with a failing test. Then an integration stage (end-to-end
  desk-flow test, README, packaging, CI) and a completeness critic.
- Round two closed the critic's gaps:
  - the "Full desk run" section was put into exit and time-machine SKILL.md;
  - the broker shows a preview ticket out of hours;
  - honest install docs;
  - the same Time Machine odds display everywhere (`display_pct`);
  - a safety bug: a made-up manual close price could book fake profit and loosen the budget.
- CI: `actions/setup-python` has no 3.9 build on current runners, so it was moved to uv.

### 2026-10-31 morning: crypto (v1.1)

- The owner asked to install the pack in Claude Code and paper trade "on one of the central or
  non-central exchanges".
- Owner's choices: a **centralized** exchange, fills in **the pack's own paper ledger** (no
  exchange account or keys).
- Built a market layer: US vs crypto, UTC days, 24/7, fractional sizes, paper fees counted in the
  loss at the stop. Every skill was adapted. US output stayed byte-identical.

### 2026-10-31: Binance instead of Coinbase (v1.2)

- The owner: "can we not do coinbase". The network had opened by then. Binance, OKX, Kraken and
  Coinbase all answered; `api.binance.com` gave 451 (US IP), `data-api.binance.vision` gave 200.
- Owner chose **Binance**. It is now the default price source; Coinbase stays selectable. Fees
  follow the exchange (0.1% Binance).
- First live validation of every parser. `tools/smoke_live.py` caught two shared-library bugs:
  - real coins JUP and SYRUP were dropped as "leveraged tokens";
  - one-letter and Chinese coin names were not read as pairs.

  Both are fixed. Now 17 of 17 live checks pass. Also fixed: one plan per pair whatever the
  spelling, missing stablecoins, parallel Binance downloads (an 18 s cold fetch is now a few
  seconds), and a bad exchange setting no longer breaks stock runs.

### 2026-10-31: the owner's questions and choices

- **"Are you actually using the skill from the video?"** No, our rebuild. The original was then
  found at github.com/Jakeschincariol/day-trader-skill (MIT, Jake Schincariol). Owner chose
  **install the original too**:
  - ours runs crypto and the dashboard;
  - the original's intraday sweep handles stocks from Monday.

  Audited before install: skills only, no hooks or MCP servers, data hosts only. Its 315 tests
  pass.
- **Max trades:** "You're allowed a maximum of 20", then **$1,000 daily max loss, $500 max open
  risk, tilt guard 20**. The old $150 limits would have stopped trading after 3 losses.
- **"Give me a link where I can watch you trade"**: the "Paper Trading Desk" dashboard artifact,
  fed by `tools/desk_snapshot.py`.
- **Next steps chosen:** a crypto re-scan every 2 hours over the weekend, alerts on the trigger
  levels, and the Monday stock sweep at 09:50 ET.
- **Binance hack, and "are the customer funds safe?"** As reported on Oct 30-31:
  - about $6.3B was stolen on Oct 29-30 (Chainalysis counts $5.9B), the largest crypto theft on
    record, linked by researchers to North Korea's TraderTraitor group;
  - Binance says users are safe and pledged to cover the loss from its SAFU reserve and its own
    funds: more than $6.34B set aside, about 80% already held in the affected tokens;
  - about $3B is frozen on-chain but not returned, withdrawals were suspended, and nobody has
    confirmed the reimbursement is complete;
  - in 2019 SAFU covered a 7,000 BTC hack without touching user balances.

  Recommendation given: fine for paper; wait for confirmed reimbursement and reopened
  withdrawals before real money. The handoff check confirmed the core facts from live headlines,
  but could not re-verify Chainalysis's count, the 80% and the $3B, so treat those as reported.

  Sources:
  - [CNBC](https://www.cnbc.com/2026/10/29/circle-tether-freeze-funds-impacted-by-potential-binance-security-issue.html)
  - [IBTimes](https://www.ibtimes.co.uk/binance-crypto-breach-north-korea-tradertraitor-1829097)
  - [CryptoTicker](https://cryptoticker.io/en/binance-safu-fund-full-reimbursement/)
  - [ABC News](https://abcnews.com/Business/binance-estimates-security-breach-impact-63-billion/story?id=137698510)
  - [Forbes](https://www.forbes.com/sites/antoniopequenoiv/2026/10/30/binance-says-63-billion-impacted-in-record-crypto-hack-suspends-withdrawals/)
  - [Onmanorama](https://www.onmanorama.com/news/business/2026/10/31/6-billion-binance-hack-what-failed-who-pays-and-what-customers-should-check.html)
  - [PYMNTS](https://www.pymnts.com/cybersecurity/2026/binance-hack-tests-pledge-cover-customer-losses/)
- **CLAUDE.md** arrived via the owner's PR #1: work style rules and "this repo is public".

### 2026-10-31: the first full scan and candidate check

- **Crypto** (Binance, 08:50 UTC):
  - 513 USDT pairs scanned (508 after the fixes), 79 liquid enough, only 2 setups: unusual
    volume on ZAMA-USDT and ZRO-USDT, both thin and about 12% daily ATR.
  - BTC sat about 9% under its 20-day high (77.3k vs 84.9k), ETH about 13% under: a quiet weekend
    pullback.
- **US stocks** (Friday's close):
  - 5,907 scanned, 1,998 passed the filters, 645 setups, led by earnings gaps.
  - Down: INTC −28.6%, RDDT −29.2%, CACC −19.3%. Up: REGN +13.3%, GPN +13.0%, MEOH +14.0%.
- **The original pack's daily scan:**
  - a 5,942-symbol universe, 1,590 liquid charts and 470 hits;
  - the tags: 256 20-day breakouts, 115 bull flags, 92 oversold, 40 MA20 reclaims and 9 oversold
    bounces (a hit can carry several);
  - both scanners flag INTC, RDDT, REGN, GPN and MEOH. The original calls INTC and RDDT oversold (a
    long idea); ours calls them breakdowns (a short).
- **10 candidates through the whole desk** (agents, live data, throwaway homes, a skeptic per
  proposed trade). **All skipped:**

  | Candidate | Why it was skipped |
  |---|---|
  | ZAMA | Coin flip after fees; its ORB long already faded |
  | ZRO | Stop first in 68% of replays; fees made it −0.26R |
  | BTC | Today's long triggers had failed; the next trigger tested −0.43R after fees |
  | ETH | Same pattern as BTC, −0.33R after fees |
  | CACC | The short is under the short-sale restriction Monday, and its edge was +0.07R at best |
  | MEOH | The edge was only time-of-day drift, and the stop sat inside opening noise |
  | INTC | Short below Friday's low hit its stop first in 66% of replays |
  | RDDT | 71% stop first from the open |
  | REGN | 68% stop first from the open |
  | GPN | Moved on an unconfirmed takeover report, and its trigger spike was rejected |

  Then 3 alerts were armed on the levels that would make BTC, ETH and ZAMA worth re-checking.

### 2026-10-31: the 10:41 UTC re-scan

- The first scheduled crypto run: 508 pairs, 78 liquid, 2 setups. ZAMA-USDT scored 59 but is
  short-only (spot can't be shorted), and QNT-USDT scored 22. Nothing to take.
- No alert fired: BTC was 0.19% under its trigger. The dashboard and the backup were updated.

### 2026-10-31: the session handoff

- The owner: "put out all MD files needed for a transfer of the session", plus a prompt to paste
  into the new session. Written: `HANDOFF.md` and the three docs in `day-trader/docs/`. The
  private details (dashboard link, session and task ids) go only in the owner's prompt.
- Checked two ways:
  - a cold-start rehearsal: a fresh agent followed only the docs, in throwaway homes;
  - an independent audit of every number, flag and link.

  The docs were then fixed:

  | Found | Fix |
  |---|---|
  | The pass-to-ticket path failed: the throwaway home's plan never reached the real desk, so the budget check and the ticket exited 1 | `risk.py size --save`, then copy the plan to `~/.day-trader/plans/`. Verified live in throwaway homes, and fixed in both stored task prompts |
  | The alert watcher would die after 30 minutes (the default background limit) | Start it with `timeout: 7200000` |
  | The old session would keep acting after the handoff | Wait until it is idle, disable its tasks by id, then tell it "Handoff done" and ask the owner about archiving it |
  | Relative output paths would put desk files (with the ledger) in the public repo | Absolute scratchpad paths everywhere, plus a `.gitignore` backstop |
  | No way to build the backup | `tools/desk_backup.py` and `tools/desk_restore.py`. The restore refuses to roll back a newer desk |
  | Task prompts were only summarized | Copy them verbatim with `get_trigger`. The stored prompts were updated |
  | After a container recycle the crypto job would empty the stock tables | It re-creates missing stock scans first |
  | Tickets could expire unseen (15 minutes) | A push notification for every ticket |

- Smaller fixes:
  - U-USDT, a stablecoin, had passed the filter;
  - `desk_snapshot.py` silently skipped a missing input, and now exits 1;
  - ORB hits keep their order (they carry no volume);
  - the original pack's audited commit is recorded;
  - BTC-USD is documented as a separate plan from BTC-USDT;
  - several numbers in the docs were corrected.

  The stablecoin fix is the only change to how the skills behave. It shipped as v1.2.1.

### 2026-10-31 afternoon: the BTC trigger

- 11:49 UTC: the BTC-USDT alert fired on a wick to 77,679.58. No 5-minute bar closed above
  77,654.44, so it was a watch, not a trade.
- The desk tested a long at the level with three stops: 1.5 × ATR, a structure stop below the
  3-hour swing, and 4 × ATR. All leaned against: −2.00R, −0.63R and −0.93R after fees, and
  negative even before fees.
- 12:40 UTC: a real close above the level (77,674, 1.9 × volume). The same three stops tested
  worse: −2.42R, −0.83R and −1.10R after fees. Only 17-20% of past days hit the target first at
  that hour. Skipped.
- The 12:40 crypto re-scan: 507 pairs, 3 setups, none a liquid long of 60+.

### 2026-10-31 afternoon: the autonomous paper bot

- **The owner:** "give me a link where I can see what exactly you're saying. If you're going to
  trade something, you don't need my permission. You trade on your own with paper money. Run the
  bot for however long we need to see the results later, if we're profitable in the long or short
  term, or how it exactly works."
- **Pushed back on one point first:** the desk had approved none of 13 ideas, so a desk-only bot
  might show no results at all.
- **Owner's choices:**
  - **desk + shadow account:** the desk trades only passes; a shadow account trades every idea the
    desk checks, with the default plan, so the filter's value can be measured;
  - **two weeks**, verdict on Saturday 2026-11-14; the bot keeps running afterwards until the
    owner says stop.
- **Built:**
  - `tools/paper_autotrade.py`: ticket and confirm under the standing order, through the skills'
    own CLIs (budget, previews and the 10-second confirm guard still apply);
  - all-time results, the shadow rows and an equity series in `desk_snapshot.py`;
  - a "Two-week paper test" section on the dashboard: desk vs shadow, and an equity chart;
  - the shadow account: $250,000 at 0.02% risk, so each trade is sized like the desk's ($50 risk,
    $6,250 cap);
  - the jobs now run with no end date: crypto every 2 hours at :10 UTC, a US stock sweep every
    weekday at 09:50 ET.
- **A second verification round of the handoff** (a cold-start rehearsal, a rehearsal of both
  jobs, a consistency audit, a red team). Before it started, the old session found a bug in its own
  fix: the watcher guard `pgrep -f` matched the shell running the check, so it always said a watcher was running and none
  would ever start. Now a PID file. The round kept 21 findings, 6 of them major:

  | Found | Fix |
  |---|---|
  | The new session restored while the old one could still act, and "idle" can't be read reliably from outside | The old session stands down first: disables its tasks, stops its background tasks, and writes final backups ending in "Old session stood down". The new session checks that before restoring |
  | `entry.py` stops looking at 02:00 UTC for crypto without `--cutoff` | Always `--cutoff 23:55` (crypto) or `--cutoff 15:55` (stocks) |
  | A new session not in Auto mode stalls every unattended run on permission prompts | The handoff prompt and RUNBOOK say Auto mode |
  | `$THROWAWAY_HOME` was never defined, and `mktemp` homes are forgotten between shell calls | Fixed paths `$OUT/homes/<SYM>-<stop>` |
  | After a recycled container, an owner message could write before the restore, and the next backup would replace the real one | Restore first, every turn. The restore refuses any existing desk file; the backup refuses a home without its log |
  | A bare "yes" could confirm the wrong ticket | Moot: there is no per-ticket yes any more |

  Smaller fixes:
  - sync before every snapshot, so dead tickets disappear;
  - ORB hits ranked by `pct_over`;
  - the original pack runs from a clone pinned to the audited commit;
  - Reg SHO Rule 201 described correctly;
  - `.gitignore` covers stray desk files;
  - a Yahoo 429 or an SEC 403 doesn't block a takeover;
  - `PR review digest` stays enabled;
  - the news check uses extended web search.

## Decisions that shaped the design

- **Paper only, always.** No live mode exists, and nothing calls an order endpoint. "Nothing
  goes through without your yes" is enforced in code (confirm `--yes`, ticket expiry, budget
  re-check) and in every broker rule. The owner's standing order (2026-10-31) lifts the
  per-ticket yes for this paper deployment only; the code's guards still run.
- **Honest odds over action.** The Time Machine judges crypto after fees. Candidates are skipped
  unless the edge survives fees, noise and a skeptic.
- **Stdlib only**, so it runs in the Claude apps' sandbox, on 3.9 through 3.13, with no installs.
- **Vendored shared library** so single skills work standalone; `sync_common` keeps the copies
  identical.

## Backlog and ideas (not started)

- An intraday opening-range sweep in our own scanner, porting the original's `scan.py orb` (MIT,
  with credit), so crypto and stocks both get a "what's breaking out right now" view.
- A dashboard auto-refresh path: today it updates only when Claude runs a desk step.
- A separate `DAYTRADER_HOME` per market if the owner trades crypto and stocks heavily (cleaner
  daily limits).
- An optional Alpaca paper account for the original pack's broker (owner's call; needs paper API
  keys stored in the environment's settings, never pasted in chat).
- SEC contact email for EDGAR (owner's call).
- A generic stablecoin guard (price about 1 and a tiny ATR), so the next new stablecoin is dropped
  without a list update.
- `paper_autotrade.py`: confirm each ticket as soon as it is 11 seconds old, instead of after the
  whole batch. A very slow batch (many open positions, Yahoo back-off) could otherwise let its first
  tickets expire. Low risk at today's batch sizes (up to 10).
- `paper_autotrade.py --plan saved`: refuse a plan not updated today, as a guard against trading a
  stale plan when the copy from the throwaway home was skipped.
- `desk_snapshot.py`: show crypto positions' unrealized P&L net of fees, and include crypto fees and
  working orders in open risk, as `budget.py status` does.
