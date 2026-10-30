---
name: pelosi
description: >-
  Day-trading desk: tracks congressional stock trades from the Periodic Transaction Reports (PTRs)
  House members file with the House Clerk. Lists recent filings, reads the trades inside them
  (member, district, owner, ticker, buy/sell, amount range, trade and filing dates, disclosure lag),
  shows which members traded a ticker, or one member's trades. Use when the user asks "what has
  Pelosi been buying?", "Nancy Pelosi's latest trades", "congress trades this week", "did anyone
  in Congress buy NVDA?", "which politicians traded Tesla?", "latest House stock disclosures",
  "STOCK Act filings", "track congressional trades", or "what is in filing 20026590?". House only
  (the Senate's eFD site needs an interactive session); trades are disclosed up to 45 days late,
  so they are old news, not a signal. Free, no API key. Not financial advice.
---
# Pelosi (congressional trades)

Reads the House Clerk's public disclosure index and the PTR PDFs it points to, and turns each
electronically filed report into rows of trades. It answers "who traded what, when, how big (a
range), and how late did they tell us". It never fills gaps: paper filings are listed with a link.

## Run
> Scripts live in this skill's `scripts/` folder. Run them from the skill's folder (`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.

```bash
python3 scripts/pelosi.py recent                          # PTRs filed in the last 30 days (list only, fast)
python3 scripts/pelosi.py recent --days 7 --trades        # ... and the trades inside the newest 20
python3 scripts/pelosi.py recent --member pelosi          # one member's recent trades (--trades is implied)
python3 scripts/pelosi.py recent --member CA11 --no-trades --limit 0
python3 scripts/pelosi.py ticker NVDA                     # which members traded NVDA (filed in the last 365 days)
python3 scripts/pelosi.py ticker NVDA --days 90 --max-filings 100 --limit 0
python3 scripts/pelosi.py member "Nancy Pelosi" --days 365
python3 scripts/pelosi.py filing 20026590                 # one PTR by DocID (add --year 2025 for older ones)
python3 scripts/pelosi.py ticker NVDA --json              # --json on any command, for chaining
```

* `--days N` is about the **filing** date (when the report became public), not the trade date.
* `--member` / `member NAME` match a last name, first + last, "Last, First", or a district (`CA11`,
  `AK-AL` at large). Accents are optional (`sanchez` finds Sánchez).
* `recent`: `--limit` = newest N filings (default 20, 0 = all); `--max-filings` caps PDF downloads
  (default 25). `ticker`/`member`: `--limit` = trades shown (default 50, 0 = all), `--max-filings`
  = PDFs downloaded this run (200 / 50).
* Speed: the index is one small download (cached 6 h). Each PDF is a separate download with a polite
  0.5 s pause, so `ticker` over a full year (a few thousand filings) takes several runs of a few
  minutes each. Tell the user before a long scan. Start with `--days 30` or `--days 90` for a quick
  answer. Parsed filings are cached (`$DAYTRADER_HOME/cache/ptr/<DocID>.json`), so each rerun only
  downloads filings it has not read yet and goes further back.

Exit codes: **0** ok · **1** bad input (bad flags, unknown DocID) · **2** data source down ("data error: ...").

## How it works
* **Index**: `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip` (an XML +
  TXT list of every filing). `FilingType P` = Periodic Transaction Report. Windows that cross
  January also load the previous year's index.
* **Filings**: `.../public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf`. Electronic filings are text PDFs. Text
  comes from pypdf if installed, else a built-in stdlib extractor (both are tried and the better
  parse wins). Each row is anchored on its `type · trade date · notification date · amount` and the
  asset text around it, so wrapped cells and page breaks are handled. **Paper filings** (scanned
  images, DocIDs usually starting with 8 or 9) have no text and are listed as "paper filing - open
  the PDF". Never guess what is in them.
* **Fields**: owner (self / spouse / joint / dependent child), asset name, ticker (when the filer
  gave one), asset type (`[ST]` stock, `[OP]` option, `[GS]` government bond ... see
  `reference.md`), BUY / SELL / PARTIAL SALE / EXCHANGE, amount **range**, trade and notification
  dates, description (option strike/expiry are pulled out when stated).
* **Disclosure lag** = filing date − trade date. The STOCK Act allows 30 days from notification and
  at most 45 days from the trade. Later ones are marked `*`. Lag is often weeks.
* **Limits**: House only (no Senate). Amounts are ranges, not exact sizes. A capped `ticker` scan
  covers only the newest filings (the output says how far back it got). Assets without a ticker in
  the filing (bonds, funds, private stock) cannot be matched by `ticker`. A PDF the parser cannot
  read is listed, not dropped. Amended filings appear as separate filings.

## Reading the output / how to present it to the user
```
WHO TRADED NVDA · House PTRs filed 2025-10-30 to 2026-10-30 (last 365 days)
Searched 6 of 7 filings (6 downloaded); 1 paper filing (scanned, no text) could not be read.
member                buys  sells  trades  first       last        total (sum of ranges)
Nancy Pelosi (CA-11)     2      0       2  2025-11-21  2026-09-16  $1,500,002 - $6,000,000
TRADES (newest first)
traded      member                owner   ticker  asset               type  amount                 filed       lag(d)
2026-09-16  Nancy Pelosi (CA-11)  spouse  NVDA    NVIDIA Corporation  BUY   $500,001 - $1,000,000  2026-10-28  42
```
* Lead with the answer (who traded, buy or sell, when), then the amount **as a range**, then the
  lag ("disclosed 42 days after the trade"). Say who owned it: a spouse's account is not the
  member's own trade.
* Always state the coverage line: how many filings were searched, how far back, and any paper or
  unreadable filings (give their PDF links). With "not searched yet", offer to rerun.
* Present option trades with their strike and expiry when listed, and say that an option bet is
  not the same as buying shares.
* JSON: `trades[]` items have `member, district, owner, owner_label, ticker, asset, asset_type,
  asset_label, type (P|S|S (partial)|E), action, transaction_date, notification_date, amount,
  amount_min, amount_max (null = open-ended), filed, lag_days, late, doc_id, url, description?,
  option{kind, strike, expires, contracts}?`. Also `scan{in_window, searched, cached, downloaded,
  paper, unparsed, failed, not_searched, oldest_searched}`, `lag{median, max, late}`, `unread[]`,
  `warnings[]`. `ticker` adds `members[]` (per-member buys/sells/date range/sum of ranges). `member`
  adds `tickers[]`. Full schema in `reference.md`.

## Next step on the desk
* A ticker that interests the user → the **analyst** skill (`analyst.py brief NVDA`) for today's
  trend, levels and SEC filings, and the **news** skill for fresh headlines. That is what can move
  the stock **today**. A weeks-old congressional trade cannot be a day-trading entry by itself.
* If they still want a trade idea, it goes through the normal desk: entry → exit plan →
  time-machine → risk → budget → broker (paper ticket that waits for their yes).

## Rules
* Not financial advice. Never present a congressional trade as a reason to buy or sell. Say it is
  disclosed late (often weeks), in ranges, and may be a spouse's or a managed account.
* Report only what the script printed. Never invent trades, tickers, amounts or dates, never turn a
  range into a point estimate, and never assume what a paper filing contains.
* Do not speculate about motives or imply wrongdoing. Stick to the disclosed facts and their dates.
* Coverage is part of the answer: if filings were not searched, were paper, or failed, say so. "No
  trades found" only covers the filings that were actually read.
* For long scans (`ticker` over many months), warn about the time and offer a shorter `--days` first.
* If the script exits with code 2 ("data error: ..."), tell the user which host failed (it is in the
  message, `disclosures-clerk.house.gov`). In sandboxes the host may need to be allow-listed. Never
  invent prices, headlines, trades or odds to fill the gap. Exit 1 is bad input: fix the arguments.
