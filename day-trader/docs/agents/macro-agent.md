# Macro agent: one per pre-market study

The pre-market study (RUNBOOK §2a) spawns one of these with the Agent tool (`general-purpose`,
`run_in_background: false`), in the same tool block as the chart and news agents. Fill the
placeholders, then pass everything below the line as the prompt:

| Placeholder | Value |
|---|---|
| `{{DATE}}` | today, `YYYY-MM-DD` |
| `{{OUT}}` | the session's scratchpad desk folder (`$OUT`) |
| `{{SECTORS}}` | the candidates' symbols grouped by sector, one line, or `see the candidates` |

It writes `{{OUT}}/premarket/parts/macro-{{DATE}}.json`, which `tools/premarket.py merge` reads.

---

Paper-trading research only: you place no orders. Today is {{DATE}}; the US regular session opens
09:30 ET and the desk's opening-range sweep runs at 09:50 ET. Build the pre-market picture with
WebSearch (mode "extended") and WebFetch, with a budget of 8 minutes, and source every claim (a URL
you actually read); write "unknown" where you found nothing. Futures and prices can also be read live:
`curl -sS "https://query1.finance.yahoo.com/v8/finance/chart/ES=F?interval=5m&range=1d"` (and NQ=F,
YM=F, RTY=F, BTC-USD) gives `regularMarketPrice` and `chartPreviousClose` in its meta.

Cover:
1. Futures (ES, NQ, YM, RTY) and the overnight move; the dollar, yields, oil, gold, bitcoin.
2. Today's US economic calendar with times in ET (PMIs, ISM, jobs data, Treasury auctions, Fed
   speakers), and this week's main events; flag anything that lands between 09:50 and 11:00 ET.
3. Notable earnings today (before the open and after the close) and this week among large caps and
   in the candidates' sectors: {{SECTORS}}.
4. Sector moves to expect at the open, from overnight news and the prior session.
5. Crypto-linked stocks (exchanges, brokers, miners, treasuries) and the crypto tape overnight.
6. Any market-structure notice for today: exchange outages, data-feed issues, holidays, early closes.
7. Risks: what could reverse an opening-range breakout this morning.

Write ONE file with the Write tool, `{{OUT}}/premarket/parts/macro-{{DATE}}.json`:

```json
{"date": "{{DATE}}", "futures": "...", "overnight": "...", "calendar": ["HH:MM ET ..."], "events": ["..."],
 "earnings_today": ["before the open: ...", "after the close: ..."], "sector_notes": ["..."],
 "crypto_stock_notes": "...", "market_structure_notes": "...", "risks": ["..."], "sources": ["https://..."]}
```

Then return one line: futures, the one event that matters most for the 09:50 sweep, and the file's
path. Nothing else.
