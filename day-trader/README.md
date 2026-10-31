# Day Trader — 11 Claude skills that work like a small trading desk

One skill scans thousands of charts for setups, one tracks breaking news, one watches a setup bar
by bar for the entry, one sets the stop and target before you get in, and the **Time Machine**
replays the rest of the session 10,000 times from the stock's own recent bars to show the honest
odds of hitting your target first, your stop first, or neither. Risk sizes the trade, Budget
enforces daily limits, and the Broker is a **paper-only** order ticket that waits for your "yes".
The desk trades US stocks in market hours and crypto pairs such as `BTC-USD` around the clock, on
Coinbase Exchange's public prices (see [Crypto](#crypto-247-coinbase-prices)).

* **Free and MIT-licensed. No signup, no API key.**
* Plain Python 3.9+ scripts, standard library only (the `pelosi` skill uses `pypdf` if it happens
  to be installed and has a built-in fallback).
* Free public data: Yahoo Finance charts, Nasdaq Trader symbol lists, SEC EDGAR, and House Clerk
  financial disclosures for stocks; Coinbase Exchange's public market data (candles, ticker,
  product list) for crypto, with no exchange account and no API key.
* **Paper trading only.** Nothing in this pack can place a real order. Crypto fills land in the
  same local paper ledger as stock fills.

## The skills

Commands are shown relative to each skill's folder (`skills/<skill>/`); Claude runs them for you.
Every command takes `--json`, which is how the skills hand results to each other.

| Skill | What it does | Main command |
|---|---|---|
| `scanner` | Scans ~5,900 US stocks, every Coinbase USD crypto pair (`--market crypto`) or a watchlist for daily setups: breakouts, breakdowns, gaps, unusual volume, pullbacks, NR7 coils | `python3 scripts/scan.py --symbols TSLA,NVDA,AMD` |
| `news` | Breaking headlines and SEC 8-K/6-K filings per ticker (crypto pairs: headlines; SEC is n/a), tagged (EARNINGS, DEAL, FDA, ...) with HIGH-impact flags | `python3 scripts/news.py TSLA` |
| `entry` | Watches 5-minute bars and says the moment an ORB, VWAP reclaim, level break or prior-day high/low break triggers | `python3 scripts/entry.py TSLA --save` |
| `exit` | "THE PLAN" before you enter: stop (ATR, structure, percent or yours), target, R:R, time stop | `python3 scripts/exit_plan.py TSLA --entry 364.18 --save` |
| `time-machine` | Replays the rest of the session 10,000 times from the symbol's past ~59 sessions (crypto: UTC days, to the 23:55 UTC time stop, fees in R), plus every past day at the same clock time | `python3 scripts/time_machine.py TSLA --entry 364.18 --stop 358.32 --target 370.04` |
| `risk` | Position size from your risk budget (crypto: fractional, paper fees counted), with buying-power, R:R and daily-budget checks | `python3 scripts/risk.py size --entry 364.18 --stop 358.32` |
| `budget` | Daily guard-rails: max loss, max open risk, max trades, tilt guard; OK/BLOCKED before every ticket | `python3 scripts/budget.py check --risk 46.88` |
| `broker` | Paper order ticket that waits for your explicit yes; paper fills (crypto: 24/7, paper fees), stop/target/time-stop exits, history | `python3 scripts/broker.py ticket --symbol TSLA --qty 8 --entry 364.18 --stop 358.32 --target 370.04` |
| `alerts` | Price alerts: above/below a level, % moves, VWAP crosses, or straight from a saved plan | `python3 scripts/alerts.py add TSLA --above 370.04` |
| `analyst` | Pre-trade ticker brief (trend, levels, ATR, relative volume, SEC filings; crypto: UTC days) and a review of your paper journal | `python3 scripts/analyst.py brief TSLA` |
| `pelosi` | Congressional stock trades from House Periodic Transaction Reports, with the disclosure lag | `python3 scripts/pelosi.py recent --member pelosi` |

## The desk flow: "Plan a trade on TSLA and test it"

Ask Claude that, and the skills hand the trade along through a shared plan file
(`plans/TSLA.json`) and the paper ledger:

**Scanner → News → Entry → Exit → Time Machine → Risk → Budget → Broker**

These are the same steps written out, run from `day-trader/`:

```bash
python3 skills/scanner/scripts/scan.py --symbols TSLA                          # any setup on the daily chart?
python3 skills/news/scripts/news.py TSLA                                        # anything about to move it?
python3 skills/entry/scripts/entry.py TSLA --save                               # ORB(15) trigger -> entry + stop
python3 skills/exit/scripts/exit_plan.py TSLA --method manual --rr 1 --save     # keep that stop, add a 1R target
python3 skills/time-machine/scripts/time_machine.py TSLA --save                 # honest odds for those levels
python3 skills/risk/scripts/risk.py size --symbol TSLA --save                   # how many shares
python3 skills/budget/scripts/budget.py check --symbol TSLA                     # exit 0 = OK, 3 = BLOCKED
python3 skills/broker/scripts/broker.py ticket --symbol TSLA                    # paper ticket: waits for your yes
```

On the pack's synthetic test data (made-up bars from `tests/test_desk_flow.py`, not real TSLA
prices), run at 10:16–10:20 ET, these are the key lines each step prints, top to bottom: scanner,
news, entry, exit, time machine, risk, budget, broker:

```text
TSLA  breakout 52: trading 0.6% above the 20-day high 362.16 on 2.3x volume (pace-adjusted)
HIGH  09:05 ET  1h 11m ago  TSLA  Yahoo Finance · EARNINGS
      Tesla beats third-quarter earnings estimates as deliveries rise
TSLA 5m · ORB(15) · TRIGGERED 10:15 ET · LONG @ 364.18 (OR 358.32–362.40)
THE PLAN — TSLA LONG · manual stop
ENTRY 364.18 / STOP 358.32 / TARGET 370.04 / R:R 1.0
TIME MACHINE · TSLA · LONG from 10:15 ET · 10,000 replays from TSLA's past 59 sessions
HIT TARGET FIRST    31.6%  ±0.5         median 2h 50m
HIT STOP FIRST      31.6%  ±0.5         median 3h 10m
OPEN AT CLOSE       36.8%  (avg -0.05R)
VERDICT: CLOSE TO A COIN FLIP
BUY 8 TSLA @ 364.18 · STOP 358.32 · TARGET 370.04 · PAPER
BUDGET CHECK · TSLA · new risk $46.88 -> OK
ORDER TICKET · TKT-20261030-001
BUY 8 TSLA · LIMIT 364.18 · TARGET 370.04 · STOP 358.32 · PAPER · REAL MONEY: OFF
WAITING FOR YOUR YES — reply yes to confirm ticket TKT-20261030-001
```

Every line above is real output: `tests/test_desk_flow.py` runs the eight commands exactly as
written here on that data and checks each line, and `python3 tests/test_desk_flow.py
--print-morning` prints the full output of every step.

Only after you reply **yes**, in a later message, does Claude run
`python3 skills/broker/scripts/broker.py confirm TKT-... --yes`. After that, `broker.py sync` applies
the stop, the target and the 15:55 ET time stop from 5-minute bars, `budget.py status` shows what
you have risked so far, and `analyst.py review` shows what is and isn't working.
`tests/test_desk_flow.py` runs this whole chain end to end, offline, through both endings (target
hit and stop hit).

**Outside market hours a stock ticket is only a preview** (crypto never closes; see
[Crypto](#crypto-247-coinbase-prices)). On a weekend, before about 09:15 ET (with
the default 15-minute ticket life, a ticket made earlier would expire before the 09:30 open) or
after the 15:55 ET time stop, `broker.py ticket` still runs every check (levels, size, notional,
budget) and prints the whole card, but headed `ORDER TICKET · PREVIEW (not a ticket - nothing to
confirm)` and ending with:

```text
MARKET CLOSED — preview only. Re-run ticket after 09:30 ET to get a ticket you can confirm.
```

After the close or on a weekend that line says `after 09:30 ET on the next trading day`; between
the time stop and the close it starts `PAST THE 15:55 ET TIME STOP`. A preview exits 0 (JSON
`"status": "preview"`, `"preview": true`), has no ticket id and saves nothing, so there is nothing
to say yes to, and Claude does not ask for one. A budget block still exits 3. Re-run `ticket`
during the session to get a ticket you can confirm.

## Crypto (24/7, Coinbase prices)

The same desk paper-trades crypto pairs: Coinbase product ids such as `BTC-USD`, `ETH-USD` or
`SOL-USD`. Prices are Coinbase Exchange's public market data (candles, ticker and product list from
`api.exchange.coinbase.com`), with no exchange account and no API key, and the fills go into the
pack's own paper ledger next to your stock trades. Ask Claude "Paper trade BTC" or "Plan a trade
on BTC-USD and test it". Each skill sees the crypto symbol and switches itself to the crypto
market. This is what changes:

* **A session is a UTC day.** Crypto trades 24/7, weekends included, so times print in UTC and
  "today" is the UTC day, 00:00–24:00. VWAP, the prior day's high/low, the budget's daily limits
  and the ticket and trade ids (`TKT-20261031-001`) all follow the UTC day. A crypto ticket can be
  confirmed at any hour. Only after the time stop is it a preview, until 00:00 UTC.
* **The time stop is 23:55 UTC** by default (`exit_plan.py --time-stop` takes 00:05–23:59 UTC).
  The Time Machine replays from the pair's past 59 UTC days up to that time stop, and 10,000
  replays of a whole day take about a second.
* **There is no opening bell.** The ORB takes its range from 00:00 UTC unless you anchor it with
  `--or-start HH:MM`, for example where you start watching:
  `python3 skills/entry/scripts/entry.py BTC-USD --or-start 12:00 --save`. The default cutoff is
  two hours after the start, so later in the day pass `--or-start` or `--cutoff 23:55`.
* **Sizes are fractional.** Risk sizes in fractions of a coin, floored to the product's size step
  (its `base_increment`, 0.00000001 BTC), as in `BUY 0.01448645 BTC-USD`. Prices use the product's
  tick (`quote_increment`) instead of the US cent rule.
* **Paper fees are charged.** Every crypto paper fill pays the `crypto_fee_pct` setting of its
  notional: 0.6% per fill by default, an estimate of Coinbase's entry-tier taker fee. Sizing and
  the budget count both fills' fees in the loss at the stop, so the default $50 risk budget covers
  the price risk plus the fees. The plan, Time Machine, risk and ticket cards show the fees in R,
  and the ledger's `pnl` and `r_multiple` are net of them (`gross_pnl` and `fees` are stored too).
  At 0.6% a round trip costs about 1.2% of the price, so a tight intraday stop is mostly fees,
  and the cards warn when that happens. Set your own fee with
  `python3 skills/risk/scripts/risk.py config --crypto-fee-pct 0.4` (0 turns paper fees off, 5 is
  the maximum). It is stored in `settings.json`.
* **The scanner** scans every online Coinbase USD pair with
  `python3 skills/scanner/scripts/scan.py --market crypto` (stablecoins are left out), or just the
  pairs you list with `--symbols BTC-USD,ETH-USD`. It reads one candle per UTC day. Its crypto
  defaults are a minimum price of $0 and $5M of 20-day average dollar volume. Gap setups are
  skipped, because a 24/7 market has no opening gap, and relative volume is paced evenly across
  the UTC day.
* **News** reads Yahoo's feeds for the pair. SEC EDGAR does not apply to crypto, so it is never
  asked and shows as `n/a`, which is not a failure.
* **Commands without a symbol** (`broker.py sync`, `positions`, `tickets` and `history`,
  `budget.py status`, `analyst.py review`) take `--market crypto` to put "today" on the UTC day.
  `DAYTRADER_MARKET=crypto` makes that the default.

### A crypto desk run

These are the desk steps for `BTC-USD`, plus what happens after your yes, run from `day-trader/`:

```bash
python3 skills/scanner/scripts/scan.py --symbols BTC-USD                        # any setup on the daily chart?
python3 skills/news/scripts/news.py BTC-USD                                     # Yahoo headlines; SEC is n/a
python3 skills/entry/scripts/entry.py BTC-USD --save                            # ORB(15) from 00:00 UTC
python3 skills/exit/scripts/exit_plan.py BTC-USD --method manual --rr 1 --save  # keep that stop, 1R target
python3 skills/time-machine/scripts/time_machine.py BTC-USD --save              # odds to 23:55 UTC, after fees
python3 skills/risk/scripts/risk.py size --symbol BTC-USD --save                # fractional size, fees in the loss
python3 skills/budget/scripts/budget.py check --symbol BTC-USD                  # the UTC day's limits
python3 skills/broker/scripts/broker.py ticket --symbol BTC-USD                 # paper ticket: waits for your yes
# only after your yes, in a later message:
python3 skills/broker/scripts/broker.py confirm TKT-20261031-001 --yes          # paper fill at Coinbase's last price
python3 skills/broker/scripts/broker.py sync --market crypto                    # stop, target, time stop
python3 skills/budget/scripts/budget.py status --market crypto                  # what the UTC day has risked
python3 skills/analyst/scripts/analyst.py review --market crypto                # the journal, net of fees
```

On the pack's synthetic crypto test data (made-up candles from `tests/test_crypto_desk_flow.py`,
not real BTC prices), run on Saturday 2026-10-31 between 00:41 and 01:33 UTC, while the US stock
market is closed, these are the key lines each step prints, top to bottom:

```text
BTC-USD  breakout 39: trading 0.6% above the 20-day high 100,600.00 on 2.0x volume (pace-adjusted)
Sources: Yahoo RSS ok · Yahoo search ok · SEC EDGAR n/a
BTC-USD 5m · ORB(15) · TRIGGERED 00:40 UTC · LONG @ 101,250.00 (OR 99,000.00–101,200.00)
THE PLAN — BTC-USD LONG · manual stop
ENTRY 101,250.00 / STOP 99,000.00 / TARGET 103,500.00 / R:R 1.0
  Time stop       23:55  UTC - flat by then, win or lose (23h 13m left)
TIME MACHINE · BTC-USD · LONG from 00:40 UTC · 10,000 replays from BTC-USD's past 59 days (UTC)
HIT TARGET FIRST    47.6%  ±0.5         median 10h 35m
HIT STOP FIRST      27.8%  ±0.5         median 9h 05m
OPEN AT TIME STOP   24.6%  (avg +0.03R)
Fees (paper, 0.6% per fill): round trip ≈ 0.54R at these levels · expected value after fees -0.34R
VERDICT: ODDS LEAN AGAINST YOU
BUY 0.01448645 BTC-USD @ 101,250.00 · STOP 99,000.00 · TARGET 103,500.00 · PAPER · Coinbase prices
  $ at risk     $50.00 ($32.59 + $17.41 fees at the stop) of a $50.00 budget (0.20% of the account)
BUDGET CHECK · BTC-USD · new risk $50.00 -> OK
ORDER TICKET · TKT-20261031-001
BUY 0.01448645 BTC-USD · LIMIT 101,250.00 · TARGET 103,500.00 · STOP 99,000.00 · PAPER · REAL MONEY: OFF · Coinbase prices
  Fees      0.6% per fill (paper) · $17.41 if the stop fills -> loss at the stop $50.00 · $17.80 at the target
WAITING FOR YOUR YES — reply yes to confirm ticket TKT-20261031-001
FILLED (paper) · BUY 0.01448645 BTC-USD @ 101,250.00 · TRADE-20261031-001 · PAPER · REAL MONEY: OFF · Coinbase prices
  TRADE-20261031-001 BTC-USD LONG 0.01448645: TARGET @ 103,500.00 in the 01:20 UTC bar · +$14.79 +0.45R (net of $17.80 fees)
RISKED SO FAR · Sat 2026-10-31 · 01:32 UTC (market open)
  realized P&L  +$14.79  (1 closed: 1 W / 0 L)
JOURNAL REVIEW · crypto only · last 30 days (2026-10-02 to 2026-10-31) · 1 closed paper trade
Crypto paper fees $17.80 - crypto P&L and R are net of them
```

The fees do real work here. Before fees the Time Machine's odds would lean your way, but after
0.6% per fill they lean against you, and the 1R target nets +0.45R ($14.79 after $17.80 of
paper fees). The cards say so before you say yes.

Every line above is real output: `tests/test_crypto_desk_flow.py` runs these twelve commands
exactly as written here on that data and checks each line, and
`python3 tests/test_crypto_desk_flow.py --print-run` prints the full output of every step. The
same test also plays the night where the stop is hit and a run twelve hours later that anchors
the range with `--or-start 12:00`.

### Honest limits for crypto

* **Coinbase public data only.** Prices come from Coinbase Exchange's public candles, ticker and
  product list. There is no exchange account, no API key and no order routing, so nothing here can
  reach a Coinbase account or real money. No other exchange, centralized or decentralized, is
  supported, and Coinbase's price can differ from other venues.
* **Paper fills on 5-minute candles.** A ticket confirmed at a marketable limit fills at Coinbase's
  last trade price. A working limit, the stop, the target and the time stop fill from 5-minute
  candles. There is no order book and no slippage model beyond the bar. When the stop and the
  target are both inside one candle, the trade counts as stopped out, and a fast market can fill
  far worse than any of this.
* **Fees are an estimate.** 0.6% per fill approximates Coinbase's entry-tier taker fee. Your real
  tier, maker fees, the spread and the Coinbase app's own pricing all differ, so set
  `crypto_fee_pct` to match yours.
* **Spot only.** There is no margin, funding or borrow model. Leverage stays 1x unless you pass
  it, and a short is only simulated, since Coinbase spot cannot be shorted.
* **One ledger, two clocks.** Crypto and stock paper trades share one ledger by default, and the
  daily budget dates every trade on the clock of the market you are checking. A BTC trade at
  01:00 UTC on Saturday counts toward Friday in the US view and Saturday in the crypto view. A
  real crypto exchange and a stock broker are separate accounts anyway, so if you trade both,
  give crypto its own state folder and its own limits:
  `DAYTRADER_HOME=~/.day-trader-crypto DAYTRADER_MARKET=crypto`.
* **UTC days are a convention.** Crypto never closes, so "today", the 23:55 UTC time stop and the
  00:00 UTC range anchor are this pack's conventions, not exchange sessions.
* **Parsers were developed against synthetic data.** Like the stock sources, the Coinbase parsers
  have been tested offline only. Where the network is open, `python3 tools/smoke_live.py` checks
  Coinbase's candles, ticker and product list through the skills' own code.

## Safety

* **Paper only.** There is no live mode, no broker connection and no `--live` flag. Nothing here
  can place a real order or move real money. Crypto uses Coinbase's public price data only: there
  is no exchange account, no API key and no order routing.
* **No trade until you say yes.** The broker opens a paper position only through
  `confirm <ticket> --yes`. Claude is told to show the ticket and wait for your explicit yes in a
  later message. "Looks good?" or "maybe" is not a yes, and a yes counts only for that ticket.
  Outside market hours you get a preview, which has no ticket id and cannot be confirmed.
* **Daily limits are enforced.** A ticket that would break your loss budget, open-risk limit,
  trade count or tilt guard is refused.
* **Not financial advice.** Scanner hits, news tags and entry triggers are prompts to do your own
  research, not signals.
* **Simulations are not guarantees.** The Time Machine recycles roughly 59 past sessions. Its odds
  carry real uncertainty and assume fills exactly at your levels. Past behavior does not predict
  the future.

## Install

The pack is 11 skill folders under `skills/`. Each one ships its own copy of `trader_common.py`,
so any single skill also works on its own.

**Claude Code, as a plugin (recommended).** Type these two commands into Claude Code yourself.
They are slash commands, which Claude cannot run for you:

```text
/plugin marketplace add Pacoragroma/projects
/plugin install day-trader@pacoragroma-projects
```

Plugin skills are listed under the plugin's name (for example `day-trader:news`), so they don't
collide with skills you already have.

**Claude Code, manual copy.** Personal skills live in `~/.claude/skills/<name>/`, and several
folder names here are generic: `news`, `risk`, `budget`, `alerts`, `entry`, `exit`, `broker`,
`analyst` and `scanner` may already be taken by skills of your own. Check with
`ls ~/.claude/skills`, then copy without overwriting anything:

```bash
git clone https://github.com/Pacoragroma/projects.git
mkdir -p ~/.claude/skills
for d in projects/day-trader/skills/*; do
  n=$(basename "$d")
  if [ -e ~/.claude/skills/"$n" ]; then echo "skipped $n: ~/.claude/skills/$n already exists"
  else cp -rn "$d" ~/.claude/skills/; fi
done
```

`cp -n` (no-clobber) never overwrites an existing file. On its own, though, `cp -rn` would still
copy files into an existing folder of the same name and mix them into your skill, so the loop
skips such folders entirely. A skipped skill is not installed: use the plugin instead, or move
your own skill out of the way first. (Recent GNU `cp` warns that `-n` is non-portable; that is
harmless here.) If the new skills don't show up, restart Claude Code.

**Claude apps (claude.ai, Claude Desktop).** Skills run in Claude's code-execution environment, so
code execution has to be on. Build one zip per skill, then upload each zip in Claude's settings,
under Skills (only the ones you want, if you like):

```bash
git clone https://github.com/Pacoragroma/projects.git
cd projects/day-trader && python3 tools/package_skills.py     # writes dist/<skill>.zip (11 zips)
```

Then read [Running in Claude's apps / sandboxes](#running-in-claudes-apps--sandboxes): the data
hosts may need to be allowed, and your paper ledger may not survive to the next conversation.

**"Here's the link, install it."** You can paste the repository link into a chat and ask Claude
to install the skills. This is what actually happens:

* **In Claude Code,** Claude can clone the repository and copy the skill folders into
  `~/.claude/skills` the way the manual copy above does, skipping any folder that already exists
  and telling you which ones it skipped. Or it tells you the two `/plugin` commands to type.
* **In the Claude apps,** Claude cannot add skills to your account. If its sandbox can reach
  GitHub, it can clone the repository, build the 11 zips with `tools/package_skills.py` and give
  them to you as files. You then upload each one in Claude's settings, under Skills.

## Running in Claude's apps / sandboxes

In the Claude apps the scripts run in Claude's code-execution sandbox, and two things work
differently there. The same goes for other sandboxes, such as cloud coding environments.

* **Outbound network may be blocked by default.** Then every data skill stops with a one-line
  error that names the host, for example `data error: TSLA: blocked by proxy for
  https://query1.finance.yahoo.com/... (Tunnel connection failed: 403 Forbidden) - access denied;
  if you are in a sandbox, query1.finance.yahoo.com may need to be allow-listed`. In the settings
  for code execution / network access, allow network egress to the hosts in the
  [Network access](#network-access) table. A skill needs only its own hosts: `pelosi`, for
  example, needs only `disclosures-clerk.house.gov`.
* **State may not persist between conversations.** Everything the pack remembers lives in
  `$DAYTRADER_HOME` (default `~/.day-trader`): the paper `ledger.json` (positions, trade history,
  and what the daily budget counts), `settings.json` (account size, risk and budget limits),
  `plans/` and `alerts.json`. In a sandbox that folder can be gone in the next conversation.
  Before you finish, ask Claude to give you `ledger.json` and `settings.json` as files to
  download; next time, upload them and ask Claude to put them back in `~/.day-trader/`. Where
  your environment has a folder that persists, set `DAYTRADER_HOME` to it instead.

## Network access

The scripts call these hosts over HTTPS and nothing else. In a sandbox, these hosts need to be
allowed (see [Running in Claude's apps / sandboxes](#running-in-claudes-apps--sandboxes)). When a
host a command needs is unreachable, the script prints a one-line `data error: ...` naming it and
exits with code 2. Optional sources, such as one of the news feeds, only add a warning. Nothing
makes up prices or headlines to fill the gap. `python3 tools/smoke_live.py` checks every host
below in one go (see [Development](#development)).

| Host | Used by | For |
|---|---|---|
| `query1.finance.yahoo.com`, `query2.finance.yahoo.com` | scanner, news, entry, exit, time-machine, risk, budget, broker, alerts, analyst | chart bars and last price (`/v8/finance/chart`), news search (`/v1/finance/search`) |
| `feeds.finance.yahoo.com` | news | headline RSS feed |
| `www.nasdaqtrader.com` | scanner | the US symbol lists (universe scans only, not `--symbols`) |
| `www.sec.gov` | news, analyst | ticker to CIK map (`/files/company_tickers.json`) |
| `data.sec.gov` | news, analyst | filings (`/submissions`) and shares outstanding (`/api/xbrl`) |
| `disclosures-clerk.house.gov` | pelosi | Periodic Transaction Report index and PDFs |
| `api.exchange.coinbase.com` | scanner, entry, exit, time-machine, risk, budget, broker, alerts, analyst (crypto pairs such as `BTC-USD`) | Coinbase Exchange public market data, no account or API key: candles (`/products/<id>/candles`), last trade and 24 h stats (`/ticker`, `/stats`), and the product list with each pair's size step and price tick (`/products`) |

`risk` only needs Yahoo when no entry price is given. `budget` only needs it for `status --mark`.
For crypto the same goes for Coinbase, and `risk`, `entry`, `exit` and `broker` also read the
product list, best effort, for the pair's size step and price tick. `news` never calls Coinbase:
for a crypto pair it reads the two Yahoo feeds.

## Configuration

State lives in `$DAYTRADER_HOME` (default `~/.day-trader`): `settings.json`, `plans/`, the paper
`ledger.json`, `alerts.json`, `news_seen.json` and a download `cache/`.

```bash
python3 skills/risk/scripts/risk.py config --account 25000 --risk-pct 0.2 --max-position-pct 25 --min-rr 1
python3 skills/budget/scripts/budget.py set --daily-max-loss 150 --max-open-risk 150 --max-trades 5 --max-losses 3
python3 skills/risk/scripts/risk.py config        # show the current settings
```

The values above are the defaults. The paper fee per crypto fill is
`python3 skills/risk/scripts/risk.py config --crypto-fee-pct 0.6` (the `crypto_fee_pct` setting, in
percent of notional; US stocks pay no paper fee). Two settings have no command:
`ticket_ttl_minutes` (15) and `contact_email`. To change them, edit `settings.json`.

| Environment variable | Effect |
|---|---|
| `DAYTRADER_HOME` | where state is kept (default `~/.day-trader`) |
| `DAYTRADER_CONTACT` | your email for the User-Agent that SEC EDGAR asks automated clients to send (or set `contact_email`) |
| `DAYTRADER_NOW` | pretend it is this US/Eastern time, for replays and demos, e.g. `DAYTRADER_NOW=2026-10-29T10:15` (crypto commands read it as UTC; a trailing `Z` or an offset is accepted). Intraday (5-minute) bars that start after it are ignored; the bar in progress at that time is used as the data has it, so with data fetched later it already shows that bar's later prices (e.g. the entry skill's `since trigger` high/low). A daily bar can't be cut at a time of day, so if the data was fetched later, that day's daily bar includes the later prices: the scanner then prints `WARNING: replay ...`, and the analyst brief builds today from 5-minute bars and warns when it has to fall back to the daily bar |
| `DAYTRADER_MARKET` | `us` (the default) or `crypto`: the market of commands that take no symbol (`broker.py sync`, `budget.py status`, `analyst.py review`, ...) when `--market` is not given |

## Development

```bash
cd day-trader
python3 -m unittest discover -s tests       # the full offline test suite (no network)
python3 tools/smoke_live.py                 # live check of every data source (needs the network)
python3 tools/sync_common.py                # after editing shared/trader_common.py: refresh every copy
python3 tools/sync_common.py --check        # fail if any skill's copy is stale (CI)
python3 tools/package_skills.py --check     # validate every SKILL.md frontmatter (CI)
python3 tests/test_desk_flow.py --print-morning   # the desk-flow sample run, in full
```

`shared/trader_common.py` is the one canonical shared library; never edit the vendored copies in
`skills/*/scripts/`. Tests use synthetic data. CLI tests read canned responses from
`$DAYTRADER_FIXTURES` (see `tests/helpers.py`), so the suite never touches the network. CI runs
the suite on Python 3.9 and 3.12.

`tools/smoke_live.py` is the live counterpart, for a machine where the network is open (it is not
part of CI). It makes one real request per data source through the skills' own code: Yahoo
5-minute and daily bars and the quote, the Nasdaq symbol lists, the news skill's RSS and search
parsers, SEC filings, the House index plus one recent electronic PTR (skipped if none was
filed in the last 60 days), and Coinbase's 5-minute and daily candles, ticker and product list
(Coinbase pages its candles 300 at a time, so the candle checks make a few requests). Each check asserts the parse is sane (bar spacing, prices above 0,
recent timestamps, plausible counts), not just that the host answered. It prints a PASS/FAIL
table with the host for each check and exits 0 only if every check that ran passed; one failure
never stops the others. `--json` gives the same as JSON, `--symbol NVDA` changes the ticker,
`--crypto-symbol ETH-USD` the crypto pair (default `BTC-USD`), and `--only yahoo-5m,coinbase-5m`
runs a subset. Behind a proxy that blocks a host, that row fails
within seconds with the allow-list message. If SEC answers HTTP 403, set `DAYTRADER_CONTACT` first.

## Limitations

* **Parsers were developed against synthetic data.** The pack was built where none of the data
  hosts could be reached, so every parser (Yahoo charts, search and RSS, the Nasdaq lists, SEC
  EDGAR, the House index and PTR PDFs, Coinbase candles, ticker and products) has been tested only
  on synthetic payloads modelled on each documented format. Before relying on it, run `tools/smoke_live.py` first, where the
  network is open: it checks each live source through the skills' own parsers.
* **Unofficial Yahoo endpoints.** Yahoo Finance has no official free API. Its chart and search
  endpoints can change, rate-limit or block without notice.
* **About 60 days of 5-minute history.** That is all Yahoo keeps, so the Time Machine learns from
  about 59 sessions and `entry --date` can replay only that far back.
* **No holiday calendar.** The market-open check for stocks is 09:30–16:00 ET on weekdays. It does not know
  exchange holidays or half days. Some skills read early closes from Yahoo's trading-period data,
  but on a holiday they mostly just see no new bars.
* **House only, and late.** `pelosi` reads House filings only, because the Senate's eFD site needs
  an interactive session. Amounts are disclosed as ranges, and members have up to 45 days to
  report a trade, so it is old news by the time it is public.
* **A full-market scan takes minutes.** It makes about 5,900 requests. Use `--symbols` or
  `--limit` for quick runs.
* **5-minute resolution.** Paper fills and exits are simulated on 5-minute bars. When a stop and a
  target are both hit in one bar, the trade counts as stopped out. There is no slippage model
  beyond gaps.
* **Crypto has its own limits.** Coinbase public data only, estimated fees, spot only, and UTC
  days as a convention: see [Honest limits for crypto](#honest-limits-for-crypto).

## License

MIT. See [LICENSE](LICENSE).
