# Pelosi skill reference

Loaded on demand. SKILL.md has the commands and the rules.

## Sources

| What | URL | Notes |
|---|---|---|
| Disclosure index | `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip` | `{YEAR}FD.xml` + `{YEAR}FD.txt` (tab-separated), every filing of that year. Cached 6 h; earlier years 7 days, except last year's during January (late-December filings are still being posted). |
| One PTR | `https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf` | `{YEAR}` is the index year the filing is listed in. |

Index record fields: `Prefix, Last, First, Suffix, FilingType, StateDst, Year, FilingDate (M/D/YYYY), DocID`.
`FilingType P` = Periodic Transaction Report (other types are annual reports, amendments,
extensions, candidate reports and are ignored). `StateDst` is `CA11`, shown as `CA-11` (`00` = at large, `AK-AL`).

The Senate publishes on `efdsearch.senate.gov`, which requires accepting terms in an interactive
browser session, so it is not covered.

## Codes in a PTR row

Owner: blank = the member · `SP` spouse · `JT` joint · `DC` dependent child.

Transaction type: `P` purchase (BUY) · `S` sale (SELL) · `S (partial)` partial sale · `E` exchange.

Amount ranges (the only size information disclosed): `$1,001 - $15,000`, `$15,001 - $50,000`,
`$50,001 - $100,000`, `$100,001 - $250,000`, `$250,001 - $500,000`, `$500,001 - $1,000,000`,
`$1,000,001 - $5,000,000`, `$5,000,001 - $25,000,000`, `$25,000,001 - $50,000,000`,
`Over $50,000,000`, and `Spouse/DC Over $1,000,000` (a spouse/child asset above $1M).
`amount_max` is `null` for the open-ended ones. Sums of ranges are sums of the bounds, nothing more.

Label lines under each row: `F S` Filing Status (New / Amended) · `S O` Subholding Of (the account) ·
`D` Description (free text; option strike/expiry often here) · `L` Location · `C` Comments.
They print as initials because the heading font has no Unicode mapping for its lowercase glyphs.

Asset type codes (in brackets after the asset):

| Code | Meaning | Code | Meaning |
|---|---|---|---|
| ST | Stocks (incl. ADRs) | OP | Options |
| EF | Exchange-traded funds | ET | Exchange-traded notes |
| MF | Mutual funds | GS | Government securities / agency debt |
| CS | Corporate securities (bonds, notes) | AB | Asset-backed securities |
| CT | Cryptocurrency | PS | Stock (not publicly traded) |
| RS | Restricted stock units | SA | Stock appreciation rights |
| RE | REITs | RP | Real property |
| HE / HN | Hedge & private-equity funds | OI / OL | Ownership interests |
| BA | Bank / money-market accounts, CDs | BK | Brokerage accounts |
| IR / IH | IRAs | 4K | 401(k) & other retirement |
| 5C / 5F / 5P | 529 plans | DB / PE | Pensions |
| VA / FN | Variable / fixed annuities | VI / WU | Insurance |
| FU | Futures | FE | Foreign exchange |
| PM | Precious metals | CO | Collectibles |
| MA | Managed accounts | TR | Trusts |
| DO | Debts owed to the filer | OT | Other |

Unknown codes are passed through (`asset_label` = the code in lowercase).

## How a row is parsed

1. Text comes from pypdf (if importable) and from `pelosi_pdf.py` (stdlib: object streams,
   Flate/ASCIIHex/ASCII85, Type0 + ToUnicode and simple fonts, Form XObjects, positioned text
   rebuilt into visual lines). Both run and the parse with more rows wins.
2. `pelosi_ptr.py` drops page furniture (Clerk header, `Filing ID #`, table headings, `Name:` /
   `Status:` / `State/District:`), marks section ends (asset-type footnote, IPO question,
   certification, signature), then finds every `type · date · date · amount` core.
3. The asset is the text before the core (anchored on `[XX]`, walking up over wrapped name lines
   but not over description prose); asset text that wrapped past the dates or into the amount cell
   is re-attached. Label lines after the core belong to that row.
4. Ticker = the last `(SYMBOL)` before the asset code, when it looks like a ticker (words such as
   `(ADR)` or `(LLC)` are not; real symbols like `CL`, `SE`, `AG` are). Filings
   without one (bonds, funds, private stock) have `ticker: null` and cannot be found by `ticker`.

Status per filing: `parsed` (rows found) · `paper` (pages decoded fine but have no text layer: a
scanned paper filing) · `unparsed` (text but no rows, or a PDF that is damaged, encrypted, or draws
text the extractors cannot decode - never called `paper`) · `error` (download failed, cut short, or
not a PDF; retried next run) · `not_searched` (over the `--max-filings` cap; next run continues).
`parsed`, `paper` and `unparsed` are cached, except an `unparsed` PDF that no extractor could open
(retried next run, e.g. after installing pypdf).

## Caching and limits

* Parsed filings: `$DAYTRADER_HOME/cache/ptr/<DocID>.json` (a filing never changes; amendments get a
  new DocID). Delete the folder to re-parse everything. `$DAYTRADER_NO_CACHE=1` ignores it.
* Downloads: 0.5 s apart, newest filings first. After 3 failures in a row the run stops downloading
  and says so. If nothing at all could be read, the command exits 2.
* `--max-filings` counts downloads (and failed attempts) in this run. Cached filings are free.
* `$DAYTRADER_PDF_ENGINE=stdlib|pypdf` forces one extractor (default: both).
* Debugging one PDF: `python3 scripts/pelosi_pdf.py file.pdf` prints its text,
  `python3 scripts/pelosi_ptr.py file.txt` prints the parsed rows as JSON.

## JSON shapes

Trade (`trades[]` in every command):
```json
{"owner": "SP", "owner_label": "spouse", "asset": "NVIDIA Corporation - Common Stock",
 "ticker": "NVDA", "asset_type": "OP", "asset_label": "option", "type": "P", "action": "BUY",
 "transaction_date": "2025-11-21", "notification_date": "2025-11-21",
 "amount": "$1,000,001 - $5,000,000", "amount_min": 1000001, "amount_max": 5000000,
 "filing_status": "New", "subholding_of": null, "description": "Purchased 50 call options ...",
 "option": {"kind": "call", "strike": 120.0, "expires": "2026-12-18", "contracts": 50},
 "shares": null, "member": "Nancy Pelosi", "district": "CA-11", "filed": "2025-12-19",
 "doc_id": "20024100", "url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/20024100.pdf",
 "lag_days": 28, "late": false}
```
Keys that do not apply (`subholding_of`, `description`, `location`, `comments`, `option`,
`shares`) are omitted.

* `recent`: `command, as_of, since, days, member_query, filings_total, trades_mode, filings[]`
  (`doc_id, member, prefix, first, last, suffix, state_district, district, filed, year, url,
  paper_likely`, plus `status, trade_count` in trades mode), and in trades mode `trades[],
  total_trades, scan, lag, unread[]`. `did_you_mean[]` when `--member` matched nothing.
* `ticker`: `ticker, members[]` (`member, district, trades, buys, sells, partial_sales,
  exchanges, first_trade, last_trade, amount_min_sum, amount_max_sum, open_ended`), `trades[]`,
  `total_trades, scan, lag, unread[]`.
* `member`: `query, members[{member, district}], filings[], tickers[]` (`ticker, asset, trades,
  buys, sells, exchanges, last_trade`), `trades[], total_trades, scan, lag, unread[]`, `did_you_mean[]`.
* `filing`: `filing{... status, trade_count}, trades[], lag`.
* `scan`: `in_window, searched, cached, downloaded, failed, not_searched, parsed, paper, unparsed,
  oldest_searched` (+ `stopped_after_errors`). `oldest_searched` is how far back the scan is complete:
  the oldest filing read before the first one skipped by the cap (older cached filings are still
  searched but do not extend it). `lag`: `median, max, min, late` (days; `late` =
  trades disclosed more than 45 days after they happened).
* Every command: `as_of, since, days, warnings[], disclaimer`.
