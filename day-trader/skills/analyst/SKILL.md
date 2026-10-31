---
name: analyst
description: >-
  Day-trading desk: pre-trade ticker brief and post-trade review of the paper journal. The brief
  shows trend (20/50-day SMAs, 20-day slope), key levels (prior day high/low/close, 20-day and
  6-month high/low, VWAP), daily ATR in $ and %, average dollar volume, relative volume vs the
  same time of day, the gap, and SEC context: shares outstanding, recent 8-K items, 10-Q/10-K,
  possible dilution and insider Form 4s. Crypto pairs (BTC-USD): Coinbase data, UTC days, no SEC.
  The review grades closed paper trades: win rate, average win/loss in $ and R, profit factor,
  expectancy, max drawdown, and breakdowns by setup, side, entry hour, exit, market and Time
  Machine verdict, with takeaways. Use for "brief me on TSLA", "key levels for NVDA?", "is AAPL
  trending?", "is volume heavy today?", "any recent filings or dilution?", "review my trades",
  "how am I doing overall / this month?", "what's my win rate?", "which setups work for me?".
  Free Yahoo Finance, Coinbase and SEC EDGAR data, no API key. Not financial advice.
---
# Analyst

Two jobs on the desk. Before a trade, **brief** gives one compact read of a ticker: is it
trending, where are the levels that matter, how much does it normally move, is today's volume
unusual, did it gap, and is anything in its recent SEC filings (earnings, offerings, insider
filings) that could move it. After trades, **review** reads the paper journal the broker skill
keeps and shows what is and is not working, by setup, side, time of day and whether the Time
Machine said the odds leaned your way.

## Run
> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder (`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.

```bash
python3 scripts/analyst.py brief TSLA                    # Yahoo daily + 5m bars and SEC EDGAR
python3 scripts/analyst.py brief TSLA --no-sec           # skip SEC (faster, Yahoo only)
python3 scripts/analyst.py brief NVDA --filing-days 90 --json
python3 scripts/analyst.py review                        # closed paper trades, last 30 days
python3 scripts/analyst.py review --days 90 --symbol TSLA
python3 scripts/analyst.py review --days 3650            # "overall": the whole journal (max 10 years)
python3 scripts/analyst.py review --json
python3 scripts/analyst.py brief BTC-USD                 # crypto pair: Coinbase, UTC day, no SEC
python3 scripts/analyst.py review --market crypto        # only crypto trades (or --market us)
```

* `brief SYMBOL`: one ticker per run. `--filing-days N` (1-365, default 30) sets the SEC window.
* `review`: `--days N` = today and the previous N-1 calendar days, by close date (default 30).
  `--symbol` keeps one ticker, `--market us|crypto` one market. "This month" = `--days` set to
  today's day of the month (e.g. `--days 30` on Oct 30); "overall" = `--days 3650`.
* `--json` on either command gives the machine-readable result for chaining. Exit codes: 0 ok,
  1 bad input or a broken state file, 2 a data source is unreachable.

## How it works
**brief.** Yahoo daily bars (6 months) are required. Yahoo 5-minute bars (1 month) and SEC
EDGAR are optional. If they fail, the brief still prints and says what is missing.
* **Baselines:** SMAs, ATR, 20-day/6-month high/low and average volume use only *completed
  sessions before today*. Today's partial bar never moves them. "Prior day" means the last
  full session before today's date.
* **Session:** during and after market hours, the session line is today's: price, change,
  range and VWAP, from 5-minute bars up to now. After 16:00 ET it uses the official daily
  bar. Pre-market and on weekends it shows the last completed session. Pre-market trading is
  not included.
* **Trend:**
  * UPTREND: price > 20-day SMA > 50-day SMA, and the 20-day linear-regression slope of closes
    is rising (more than +0.05%/day).
  * DOWNTREND: the mirror image.
  * SIDEWAYS: slope flat. MIXED: anything else. UNKNOWN: under 20 daily bars.
* **ATR(14):** Wilder's ATR of daily bars, in $ and % of price. The session's range is shown as
  a % of ATR (how much of a normal day's move is used up).
* **Relative volume:** today's volume through the last completed 5-minute bar vs the average
  of up to 20 prior sessions through the same clock time. Half days that closed earlier are
  excluded. Without 5-minute bars it falls back to today's volume vs the 20-day average daily
  volume, flagged "partial day" during market hours because it reads low early on. Before
  09:35 ET there is no reading.
* **Gap:** the session's open vs the prior close, in % and ATRs, with a filled / not filled flag
  (and the 5-minute bar that filled it). Flat means under ±0.25%.
* **SEC:**
  * company name, CIK and industry from the submissions API;
  * shares outstanding from XBRL `dei:EntityCommonStockSharesOutstanding`: the latest
    cover-page value, with several share classes summed. Market cap ≈ shares × price (assumes
    every class trades at this price; none from a 20-F count, as an ADR's ADS may be several shares);
  * filings in the window: 8-K/6-K items by name (2.02 = earnings), 10-Q/10-K/20-F, possible
    dilution (S-1, S-3, F-1, F-3, 424B*, 8-K item 3.02), 13D/13G holders, and counts of
    Form 4 (insider trades) and Form 144 (planned insider sales).
* **Crypto pairs** (`BTC-USD`, `ETH-USD`, ...): Coinbase Exchange candles (daily 6 months, 5-minute
  21 days), no API key. Crypto trades 24/7, so "today" and "prior day" are UTC days (levels read
  `prior UTC day high/low/close` and `VWAP (UTC day)`), the header says `24/7 (UTC day)`, relative
  volume compares against the same UTC clock time on up to 20 prior days, there is no gap (a UTC
  day opens where the last one closed), liquidity is in coins, and SEC is `n/a` - not asked, not
  a failure.

**review.** Reads closed trades from `ledger.json` (the broker skill writes it) in
`$DAYTRADER_HOME`.
* **Classifying trades:** win = P&L > 0, loss < 0, scratch = 0. R = P&L / initial risk
  (|entry - stop| × shares). Crypto P&L and R are net of the paper fees the broker charged
  (`Crypto paper fees $X` line).
* **Headline numbers:**
  * profit factor = gross wins / gross losses;
  * expectancy = average R per trade;
  * max drawdown = the largest peak-to-trough fall of cumulative closed P&L;
  * break-even win rate = 1 / (1 + average win R / |average loss R|), compared with the win rate
    of decided trades (`win_rate_ex_scratches_pct`; scratches neither win nor lose).
* **Breakdowns:** setup, side, entry hour (ET), entry window (09:30-10:30, 10:30-14:00,
  14:00-16:00; crypto trades in UTC: hours and the quarters 00:00-06:00 ... 18:00-24:00, in their own
  `entry hour UTC` / `entry window UTC` tables), exit reason (target, stop, time stop, manual), symbol, and the Time Machine
  verdict carried on the ticket (leaned your way, coin flip, leaned against you, not checked).
  The Time Machine check compares its average predicted target-first % with how often trades
  actually exited at the target.
* **Takeaways:** fixed rules that name a bucket only when it has 3 or more trades, and that
  always state the sample size.
* **Both markets:** each trade's dates and hours are read in its own clock (a crypto close date is
  a UTC date). With stock and crypto trades in the window, the review adds a per-market line each,
  a `market` table and a by-market takeaway (JSON `markets`, `by_market`).

Limits: Yahoo data can be delayed, and exchange holidays are not modelled. SEC "recent filings"
holds a company's last ~1,000 filings, so for very heavy filers the window can be cut short (the
brief says so). Many 424B2 filings from banks are structured notes, not stock dilution. Ten or
twenty paper trades cannot show an edge.

## Reading the output / how to present it to the user
* **Brief.** Lead with the header and the LAST line. Then give one sentence each on trend,
  volatility (ATR %), volume (relative volume) and the gap. Then the 2-3 nearest levels above
  and below LAST in the KEY LEVELS ladder. The `>` row marks the last price, and the ATR column
  says how far each level is in normal daily moves. Finish with anything notable from SEC: an
  earnings 8-K, a "possible dilution" filing, a Form 4 cluster.
* Relay every `!` warning (JSON `warnings`): stale data, a halt, no bars in market hours, or a
  replay caveat. `-` lines (`notes`) say which data was missing or how a number was computed.
* If the SEC line says `unavailable - HTTP 403 ...` or mentions `DAYTRADER_CONTACT`, see the SEC
  rule below; the Yahoo part of the brief still stands.
* "Possible dilution" means a registration or prospectus was filed, not that shares were sold.
  Say "worth checking", not "they are diluting".
* **Brief JSON keys:**
  * price data: `symbol, name, as_of, market (open/closed/...), market_type (us|crypto), tz, price,
    session{...}, prior_day{...}`;
  * analysis: `trend{label, sma20, sma50, slope_20d_pct_per_day, ...}`,
    `levels[{name, price, from_last_pct, from_last_atr}]`,
    `volatility{atr14, atr14_pct}`, `liquidity{avg_dollar_volume_20d, avg_volume_20d}`,
    `relative_volume{value, method: time_of_day|daily, ...}`,
    `gap{gap_pct, gap_atr, direction, filled, filled_at}`;
  * SEC: `sec{status, cik, name, sic, shares{shares, as_of, classes, market_cap_approx},
    filings{notable[], insider{form4, form144, ...}, other{}}, errors[]}`;
  * messages and provenance: `sources, notes, warnings`.
* **Review.** Give net P&L and expectancy (R) first, then the win rate next to the break-even
  win rate. Then the 2-3 most useful takeaways in your own words, always with the trade count
  ("over 5 trades"). Never turn a small-sample bucket into a rule ("always trade ORB longs").
* **"How am I doing?"** Review is the trend over days and weeks, from closed trades only. For
  *today* (P&L, open risk, loss budget left, trades left, loss streak, OPEN / CAUTION / STOP
  TRADING) use the **budget** skill's `budget.py status` card. For a plain "how am I doing?",
  give today's budget status first, then this review for the trend.
* **Review JSON keys:** the headline numbers `count, win_rate_pct, net_pnl, expectancy_r,
  profit_factor, max_drawdown{usd, peak_at, trough_at}`, the breakdowns `by_setup, by_side,
  by_hour, by_window, by_exit, by_time_machine, by_symbol, by_market` (each a list of bucket
  stats), plus `time_machine_check`, `crypto_fees`, `markets` (per-market stats when both exist),
  `takeaways[]` and `trades[]`.

## Next step on the desk
* After a **brief**: the **entry** skill watches for the trigger (an ORB, a VWAP reclaim, or a
  break of a ladder level such as the prior day high), **exit** sets the stop and target before
  getting in, and **time-machine** gives the honest odds for those levels. The **news** skill
  covers headlines the brief does not read.
* After a **review**: tighten the plan with **exit** and **time-machine**, and adjust size and
  daily limits with **risk** and **budget**. Run time-machine with `--save` before each ticket,
  so the next review can show whether its verdict helped.

## Rules
* Not financial advice. The brief describes recent data. It is not a prediction or a
  recommendation. Never say a level "will hold" or a stock "will" break out.
* Report only what the script printed: never invent prices, levels, filings or statistics.
  If a section is missing (e.g. SEC unavailable), say so instead of filling it in.
* Paper trading only. This skill reads data and the paper journal. It places nothing, and
  nothing here can place a real order. Orders go through the broker skill, only after the
  user's explicit yes.
* Be honest about sample size in reviews. Under ~30 trades, patterns are hints, and the script
  says so. Do not present a takeaway as proven.
* If the script exits with code 2 ("data error: ..."), tell the user which host failed. It is
  in the message: `query1`/`query2.finance.yahoo.com` (crypto: `api.exchange.coinbase.com`) for bars, and `www.sec.gov` /
  `data.sec.gov` failures show in the SEC section of an otherwise working brief. In sandboxes the host
  may need to be allow-listed. Never invent prices, headlines, or odds to fill the gap. Use
  `--no-sec` if only SEC is blocked. Exit code 1 means bad input or a broken state file: show
  the error.
* **SEC 403 / contact email:** SEC asks automated clients to send a contact address, and may answer
  `HTTP 403` without one (the SEC line then says `set DAYTRADER_CONTACT=...`). Suggest the user set
  `DAYTRADER_CONTACT` to their own email and re-run, e.g.
  `DAYTRADER_CONTACT=you@example.com python3 scripts/analyst.py brief TSLA` (or put
  `"contact_email"` in `settings.json` in `$DAYTRADER_HOME`). Ask for the address; never make one
  up. `blocked by proxy` / `Tunnel connection failed` means the request never reached SEC: the
  host needs allow-listing, and an email will not fix that.
