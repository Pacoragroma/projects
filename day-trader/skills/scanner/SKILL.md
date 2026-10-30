---
name: scanner
description: >-
  Day-trading desk: scan the US stock market (about 5,900 common stocks, optionally ETFs) or a
  watchlist for daily-chart setups (20-day breakouts and breakdowns on volume, gap ups and gap
  downs of 3% or more, unusual volume, pullbacks to the 20-day average in an uptrend, NR7 coils)
  and rank them by a 0-100 score. Use when the user asks "what's moving", "find me setups",
  "scan the market", "any breakouts today", "what's gapping", "stocks with unusual volume",
  "screen my watchlist", "which of TSLA, NVDA, AMD look good", or wants trade ideas to start
  the day. Free Yahoo Finance and Nasdaq Trader data, no API key. Ideas to research, not advice.
---
# Scanner

The first desk on the floor: it looks at thousands of daily charts and hands back a short,
ranked list of stocks that are doing something worth a closer look today: breaking out, breaking
down, gapping, trading heavy volume, pulling back in an uptrend, or coiling. Each hit comes with a
plain-English reason and the key price level, so the next skills (news, entry, exit, time-machine)
can take over.

## Run
> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder (`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.

```bash
python3 scripts/scan.py --symbols TSLA,NVDA,AMD,AAPL          # a watchlist: seconds
python3 scripts/scan.py --limit 500 --top 15                  # first 500 names of the universe
python3 scripts/scan.py --setups breakout,gap_up --top 20     # whole market: a few minutes
python3 scripts/scan.py --setups gap_up,gap_down,unusual_volume --min-dollar-volume 50M
python3 scripts/scan.py --setups pullback --min-price 20 --include-etfs --top 10
python3 scripts/scan.py --symbols TSLA,NVDA --json            # machine-readable, for chaining
```

Flags: `--symbols A,B` (only these), `--limit N` (first N of the universe), `--setups` (any of
`breakout, breakdown, gap_up, gap_down, unusual_volume, pullback, nr7`), `--top N` (default 25,
0 = all), `--min-price` (default 5), `--min-dollar-volume` (20-day average close x volume, default
20M; accepts `20M`, `500K`, `1.5B`, `20,000,000`), `--include-etfs`, `--workers` (parallel downloads, default 8),
`--quiet` (no progress lines on stderr), `--json`.

**Speed:** a full-market scan is ~5,900 Yahoo requests and takes a few minutes (progress prints to
stderr every 250 symbols). If the user wants something quick, or names tickers, use `--symbols` or
`--limit` first, and tell them before starting a full scan that it will take a few minutes.
Re-running within ~2 minutes (market open) or ~15 minutes (closed) reuses the download cache.

## How it works
1. Universe: Nasdaq Trader's symbol lists, minus ETFs (unless `--include-etfs`), warrants,
   units, rights, preferreds and notes. Or just `--symbols`.
2. For each symbol: ~6 months of daily bars (`1d`, `6mo`). Skipped (and counted): download
   failures, unknown symbols, fewer than 60 bars, no bar in the last 6 days (halted/delisted).
3. Liquidity filter: last price >= `--min-price`, and average close x volume of the 20 bars
   *before* today >= `--min-dollar-volume` (one spike day cannot qualify a thin stock).
4. Setups on "today" = the latest bar (today's bar in progress while the market is open):
   - `breakout`: close above the highest high of the prior 20 bars, volume >= 1.5x average.
   - `breakdown`: close below the lowest low of the prior 20 bars, volume >= 1.5x average.
   - `gap_up` / `gap_down`: open >= +3% / <= -3% vs the prior close.
   - `unusual_volume`: volume >= 3x the 20-day average.
   - `pullback`: 20-SMA above the 50-SMA by at least 0.25 ATR (a real uptrend, not a flat
     line), low within 1 ATR(14) of the 20-SMA, close above the 20-SMA, RSI(14) 40-60.
   - `nr7`: the last *completed* day's range is the narrowest of 7 and inside the prior day
     (during the session that is yesterday's bar; the levels are the range to break today).
5. **Relative volume (rvol)** = volume / average volume of the prior 20 bars. While today's bar is still
   forming, the average is scaled by a typical U-shaped intraday volume curve (about 11.5% of a
   day's volume by 10:00, 52% by 13:30, 92% by 15:59; early closes from Yahoo's trading period
   compress it). So mid-session rvol is a *projection*: noisy in the first 30 minutes, and blind
   to unusual days. The first 5 minutes are floored at 3% so a few trades cannot read as 50x.
6. **Score (0-100)** per setup = 40 x V + 40 x M + 20 x T, each part between 0 and 1:
   - V = ln(rvol) / ln(5): 1x -> 0, 2x -> 0.43, 3x -> 0.68, 5x or more -> 1.
   - M = (close - prior close) / ATR(14) / 2 in the setup's direction: a 2-ATR move -> 1, a move
     against the setup -> 0 (`unusual_volume` uses the absolute move). ATR is as of yesterday, so
     today's own range does not shrink today's move.
   - T = trend alignment in the setup's direction, in ATRs, each half capped at 1:
     0.5 x (price - 50-SMA) / ATR + 0.5 x (20-SMA - 50-SMA) / ATR (signs flipped for shorts).
   - `nr7` instead: V = volume dry-up (1 - rvol) / 0.5, M = tightness 1 - range / ATR, T = the
     stronger trend in either direction, and the total is x 0.6 (max 60): a coil has not moved
     yet, so it ranks below an active breakout, gap or volume spike of similar quality.
   A symbol's score is its best setup's score; ties are broken by rvol.

Limits: daily bars only (no pre-market: before 09:30 ET the scan shows the last completed day;
today's gaps appear after the open). Early closes come from Yahoo's trading period when it
reports one; exchange holidays are otherwise not modelled. Yahoo data can be
delayed or wrong for thinly traded names. A score ranks how strongly a chart fits a pattern; it is
not a probability of profit.

## Reading the output / how to present it to the user
- Header: time (ET) and whether today's bar is still forming. Mid-session, say "rvol is projected".
- Table: rank, symbol, setups (best first), score, last price, % change vs prior close, rvol,
  and the key level (20-day high for breakouts, 20-day low for breakdowns, prior close = gap-fill
  level for gaps, 20-SMA for pullbacks, the NR7 high/low range).
- "Top setups": one line per setup with the reason in words - quote these rather than inventing
  your own narrative.
- Footer: symbols scanned, skipped (with reasons), filtered by liquidity, matched. Mention skips
  if they are a large share. With `--symbols`, it also lists names with no setup ("No matching
  setup (...)" when `--setups` narrowed the search) and why any were filtered or skipped.
- Present 5-10 names, best first, with setup, level and one-line reason. Group long vs short ideas
  (`bias` in JSON: `long`, `short`, or `either` for NR7). Always add that these are candidates
  to research, not recommendations.
- JSON (`--json`): `results[]` has `symbol, score, bias, setups[] {setup, score, bias, detail,
  levels, key_level, components}, last, change_pct, gap_pct, rvol, partial_day, atr14, atr_pct,
  sma20, sma50, rsi14, high_20d, low_20d, dollar_volume_20d, key_level {name, price}`; top level
  has `scanned, skipped, skipped_by_reason, filtered, matched, incomplete, warning, filters`.

## Next step on the desk
- **news** skill: check each candidate for a catalyst (earnings, deal, FDA, offering) before
  anything else - a gap with no news behaves differently from one with news.
- **analyst** skill (`analyst.py brief SYM`): a fuller brief on the one or two the user likes.
- **entry** skill: watch the 5-minute chart for the trigger, e.g. the opening-range breakout
  (`entry.py SYM --setup orb`) or the scanner's key level (`entry.py SYM --setup level --level
  <key level> --side long`).
- Then **exit** (stop/target), **time-machine** (honest odds), **risk** (size), **budget**, and the
  paper **broker**.

## Rules
- Not financial advice. Scanner hits are ideas to research, never "buy this". Never promise a
  breakout will follow through; a high score is not a high probability.
- Never invent tickers, prices, levels or volumes. Only report what the script printed.
- If a full scan stopped early (`WARNING: stopped early`, `incomplete: true` in JSON), say the
  list is partial and why.
- Paper trading only - this skill places nothing; orders go through the broker skill and only
  after the user's explicit yes.
- If the script exits with code 2 ("data error: ..."), tell the user which host failed (it is in
  the message, e.g. `query1.finance.yahoo.com` or `www.nasdaqtrader.com`) - in sandboxes the
  host may need to be allow-listed. Never invent prices, headlines, or odds to fill the gap.
  Exit code 1 means bad input (e.g. an unknown setup name, or no `--symbols` ticker that Yahoo
  knows; a single unknown ticker in a list is just reported under "Skipped").
