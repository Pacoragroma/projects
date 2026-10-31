---
name: news
description: >-
  Day-trading desk: track breaking news that could move a stock. Pulls Yahoo Finance headlines
  plus SEC 8-K/6-K filings for one or more tickers, merges and de-duplicates them, tags each one
  (EARNINGS, GUIDANCE, DEAL, ANALYST, FDA, LEGAL, OFFERING, INSIDER, EXEC, MACRO), flags HIGH
  impact items and lists them newest first with time ET, source and link. Repeat runs with
  --new-only show only headlines you have not seen yet. Use when the user asks "any news on
  TSLA?", "why is NVDA moving?", "what's the catalyst?", "check headlines before I trade",
  "did AAPL file an 8-K?", "earnings news for my watchlist", "anything new since last time?",
  or wants to keep an eye on breaking news for a ticker. Crypto pairs (BTC-USDT, ETH-USDT, BTC-USD)
  work too: Yahoo headlines for the coin (SEC does not apply), times in UTC.
---
# News

Breaking headlines that could move your tickers, in one list: Yahoo Finance news (RSS and search
feeds) plus SEC EDGAR 8-K / 8-K/A / 6-K filings. Copies of the same story are merged, every item is
tagged by topic and flagged HIGH when it is a market-moving topic or less than an hour old. This is
the desk's "why is it moving / is there a catalyst?" check before and during a trade.

## Run

> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder (`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.

```bash
python3 scripts/news.py TSLA                        # last 24h, newest 20
python3 scripts/news.py TSLA NVDA AMD --since 90m   # several tickers (or TSLA,NVDA,AMD)
python3 scripts/news.py TSLA --since 09:30          # since the open today (ET)
python3 scripts/news.py TSLA --since 2026-10-29     # since midnight ET on that date
python3 scripts/news.py TSLA --since 3d --limit 50  # Monday morning: cover the weekend
python3 scripts/news.py TSLA --tags EARNINGS,GUIDANCE,DEAL
python3 scripts/news.py TSLA --tags HIGH            # only high-impact items
python3 scripts/news.py TSLA NVDA --new-only        # only what you have not been shown yet
python3 scripts/news.py TSLA --json                 # machine-readable, for chaining
python3 scripts/news.py BTC-USDT ETH-USDT --since 12h  # crypto: Yahoo news for the coin, in UTC
```

* `--since`: `90m`, `24h` (default), `2d`, `1w`, `today`, a clock time today (`09:30`), or an ET
  date/time (`2026-10-29`, `2026-10-29T08:00`). A run of crypto pairs only reads (and prints)
  times in UTC, since a crypto day is a UTC day; any run with a stock in it uses ET.
* `--tags`: comma-separated, matches any. Valid tags: EARNINGS, GUIDANCE, DEAL, ANALYST, FDA,
  LEGAL, OFFERING, INSIDER, EXEC, MACRO, plus `HIGH`.
* `--new-only` is how you track news. Each `--new-only` run (text or `--json`) remembers what it
  showed in `news_seen.json` in `$DAYTRADER_HOME` (default `~/.day-trader`), keyed per ticker. The
  next run shows only fresh items. Delete that file to start over. Plain runs never read or write it.
* Up to 25 tickers per run. Each ticker costs about 3 requests, which run in parallel.

## How it works

* **Sources**
  * Yahoo Finance RSS (`feeds.finance.yahoo.com`).
  * Yahoo Finance search news (`query1.finance.yahoo.com`, falling back to `query2`). Articles
    whose related tickers do not include yours are dropped.
  * SEC EDGAR filings (`www.sec.gov`, `data.sec.gov`). These become headlines like
    `8-K: Results of operations (earnings)`, timed by EDGAR's acceptance time (ET). ETFs and
    indexes have no EDGAR filer, so SEC shows as `n/a` for them, which is not an error.
  * **Crypto pairs** (`BTC-USDT`, `ETH-USDT`, `BTCUSDT`, `BTC-USD`, ...): the two Yahoo feeds are
    asked for the coin in Yahoo's spelling, `BTC-USD` for `BTC-USDT` or `BTCUSDT` (a `Yahoo symbols`
    line says so; JSON `yahoo_symbols`): news is about the coin, not the exchange that prices it
    (Binance by default, or Coinbase) or the quote currency. Spellings of one coin count as one
    ticker and share the `--new-only` memory. SEC EDGAR does not apply to crypto, so it is not asked
    at all and shows as `n/a` (JSON `not_applicable`) - never as a failure, and never a reason to
    say filings are missing.
* **Dedupe**: copies count as one story when they share a link (ignoring tracking parameters) or a
  normalized headline within 12 hours. A merged story keeps the earliest time, every ticker and
  source it came from, and the real publisher name. SEC filings merge only on the same filing URL.
* **Tags**: keyword and regex rules on the headline and summary, plus 8-K items (2.02 EARNINGS;
  1.01, 2.01, 5.01 DEAL; 3.02 OFFERING; 5.02 EXEC; 1.03 LEGAL). The rules are in `reference.md`.
* **HIGH impact**: tagged EARNINGS, GUIDANCE, DEAL, FDA, OFFERING or LEGAL, **or** published within
  the last 60 minutes.
* **Failures**: one source failing only adds a warning (`Sources:` line, JSON `warnings`). The run
  stops with exit 2 only when every source fails.
* **Limits**: each Yahoo feed returns only its latest ~20 stories, so a long `--since` may not
  reach back that far. No headline does **not** mean no news. Paywalled stories show only a
  headline. Items dated after "now" (or after `$DAYTRADER_NOW` in a replay) are left out.

## Reading the output / how to present it to the user

```
NEWS · TSLA · last 24h
4 headlines, 3 high impact · as of Fri Oct 30 10:15 ET

HIGH  10:02 ET         13m ago      TSLA  Reuters · EARNINGS GUIDANCE
      Tesla beats third-quarter estimates, raises delivery outlook
      https://finance.yahoo.com/news/...
HIGH  Oct 29 16:05 ET  18h 09m ago  TSLA  SEC EDGAR · EARNINGS
      8-K: Results of operations (earnings)
```

* Lead with the HIGH items. For each one give the time (ET; UTC for a crypto-only run - JSON
  `tz`), how long ago, the source, the
  **exact** headline and its link. Do not paraphrase it into something stronger than it says.
* Group by ticker when there are several. Mention when one story covers more than one ticker
  (`TSLA,NVDA`).
* Say what a tag means for a day trader without predicting direction. For example, EARNINGS or
  FDA means gap and volatility risk, OFFERING can mean dilution pressure, and ANALYST moves are
  usually smaller and fade.
* Read the `Sources:` line and any `!` warnings. If a source failed, say the list may be
  incomplete. A `Yahoo symbols` line (crypto) names the coin whose news was read: say so. If SEC EDGAR failed with `HTTP 403` or the warning mentions a contact email /
  `DAYTRADER_CONTACT`, see the SEC rule below.
* With `--new-only`, "No new headlines" means nothing new since the last check. It does not mean
  "no news today". With `--tags`, "No headlines tagged X" means none matched the tag filter; the
  count of other headlines is in brackets.

## Next step on the desk

* Found a catalyst on a scanner hit? Use **entry** to wait for the setup to trigger, then **exit**
  to set the stop and target, and check the odds with **time-machine**.
* EARNINGS, FDA or OFFERING news on a ticker you hold or plan to trade: re-check the plan with
  **exit** and **risk**, because gap risk is higher. **alerts** can watch the key levels.
* **analyst** gives the ticker brief (trend, levels, recent SEC filings) around the headline.

## Rules

* Never invent, embellish or "summarize" headlines that are not in the output. Quote titles as
  given and give the link. If you have not run the script, do not claim there is or is no news.
* Tags are keyword guesses. HIGH means "look at this", not "this will move the stock" and not
  bullish or bearish. Tell the user to read the story before acting.
* Not financial advice. Never say a headline makes a trade a sure thing. Paper trading only.
* Old news is not breaking news: always say how old an item is. SEC filings use EDGAR's acceptance
  time. An item marked `date only` has no known time of day.
* **If data fails** (exit 2, `data error: ...`): tell the user which host failed. The host is in
  the message: `feeds.finance.yahoo.com`, `query1.finance.yahoo.com`, `www.sec.gov` or
  `data.sec.gov`. In sandboxes the host may need to be allow-listed. Never invent prices,
  headlines or odds to fill the gap. Exit 1 means bad input (bad ticker, tag or `--since`); the
  message says what to fix.
* **SEC 403 / contact email:** SEC asks automated clients to send a contact address, and may answer
  `HTTP 403` without one (the message then says `set DAYTRADER_CONTACT=...`). Suggest the user set
  `DAYTRADER_CONTACT` to their own email and re-run, e.g.
  `DAYTRADER_CONTACT=you@example.com python3 scripts/news.py TSLA` (or put `"contact_email"` in
  `settings.json` in `$DAYTRADER_HOME`). Ask for the address; never make one up. `blocked by
  proxy` / `Tunnel connection failed` means the request never reached SEC: the host needs
  allow-listing, and an email will not fix that. Meanwhile the Yahoo headlines still stand; say
  that SEC filings are missing from the list.
