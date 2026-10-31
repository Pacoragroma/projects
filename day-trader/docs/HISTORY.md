# History: what was asked, decided and found

**Short version: the owner sent a promo video of an 11-skill Claude day-trading pack and asked
"can we implement this". We rebuilt it from scratch, then added 24/7 crypto, then switched crypto
prices to Binance at the owner's request, then installed the video's original pack alongside.
First full live run on 2026-10-31: 6,420 charts scanned, 10 candidates checked, no trade worth
taking.**

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
- **Binance hack:** about $6.3B stolen on Oct 29-30 (Chainalysis: $5.9B), the largest crypto theft
  on record, linked by researchers to North Korea's TraderTraitor. Binance says users are safe and
  pledged to cover the loss from SAFU and its own funds: more than $6.34B set aside, about 80%
  already held in the affected tokens. About $3B is frozen on-chain. Withdrawals were suspended,
  and completion is not confirmed. Recommendation given: fine for paper; wait for confirmed
  reimbursement and reopened withdrawals before real money.
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
- **The original pack's daily scan:** 5,942 charts, 1,590 liquid, 470 setups (256 breakouts, 115
  bull flags, 92 oversold). The two scanners agree on INTC, RDDT, REGN and GPN.
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

## Decisions that shaped the design

- **Paper only, always.** No live mode exists, and nothing calls an order endpoint. "Nothing
  goes through without your yes" is enforced in code (confirm `--yes`, ticket expiry, budget
  re-check) and in every broker rule.
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
