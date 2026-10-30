# news: reference

## JSON contract (`news.py ... --json`)

```json
{
  "symbols": ["TSLA"],
  "as_of": "2026-10-30T10:15:00-04:00",
  "since": "2026-10-29T10:15:00-04:00",
  "since_label": "last 24h",
  "new_only": false,
  "tags_filter": [],
  "total": 4,            // items that passed every filter (before --limit)
  "shown": 4,
  "high_impact": 3,
  "items": [{
    "id": "5db15280f57f",                // stable hash of the link (or headline)
    "symbol": "TSLA", "symbols": ["TSLA"],
    "title": "Tesla beats third-quarter estimates, raises delivery outlook",
    "publisher": "Reuters",
    "link": "https://finance.yahoo.com/news/...",
    "published": "2026-10-30T10:02:00-04:00", "published_ts": 1793368920,
    "date_et": "2026-10-30", "time_et": "10:02",   // time_et null = date-only SEC filing
    "age_minutes": 13, "ago": "13m ago",
    "tags": ["EARNINGS", "GUIDANCE"],
    "impact": "HIGH",                    // or "normal"
    "fresh": true,                       // published within the last 60 minutes
    "kind": "headline",                  // or "filing" (then also form, items, accession)
    "sources": ["yahoo-rss", "yahoo-search"],
    "summary": "..."
  }],
  "skipped": {"undated": 0, "older": 1, "future": 0, "seen": 0, "tag_filter": 0},
  "sources": [{"source": "yahoo-rss", "ok": ["TSLA"], "failed": {}, "not_applicable": [], "items": 4}],
  "warnings": [],
  "disclaimer": "..."
}
```

When some sources fail, their warnings go to stderr as `warning: ...`, so stdout stays valid
JSON. Exit codes: 0 ok (even with zero headlines), 1 bad input or a corrupt state file,
2 every source failed.

## Tag rules (case-insensitive; headline and summary are matched separately)

| Tag | Matches (abridged) |
|---|---|
| EARNINGS | earnings, EPS, revenue, quarterly results/profit, first..fourth quarter, Q1-Q4, 3Q26, results of operations, beats/tops/misses ... estimates/expectations/consensus/views, 8-K item 2.02 |
| GUIDANCE | guidance, outlook, forecast, profit warning, raises/cuts/lowers/reaffirms/withdraws ... guidance/outlook/full-year/FY/view |
| DEAL | merger, acquire/acquisition, buyout, takeover, deal (not shopping or trade deals), partnership, partners with, joint venture, tender offer, wins/lands/awarded ... contract, stake (not "at stake"), agrees/in talks to buy, buys/sells ... for $N million/billion (not buybacks or share sales), take-private, 8-K items 1.01/2.01/5.01 |
| ANALYST | upgrade/downgrade (not software/firmware/hardware/system/network upgrades), price target, initiates/resumes coverage, overweight/underweight/outperform/underperform/equal-weight, buy/sell/hold rating, reiterates ... rating |
| FDA | FDA, clinical trial/data/hold, late/mid/early-stage trial, phase 1/2/3 (I/II/III) when near trial/study/data/patients, PDUFA, BLA, breakthrough therapy, fast track, orphan drug, primary endpoint, topline data, drug/therapy/vaccine/device ... approval |
| LEGAL | lawsuit, sues, probe, investigation, SEC/DOJ/FTC/regulators ... charges/fines, charged with, criminal, indictment, DOJ/FTC, antitrust, settlement, settles ... lawsuit/charges, class action, subpoena, fraud, litigation, court/courts/jury, jury or $ verdict, judge rules/blocks, bankruptcy, Chapter 11, 8-K item 1.03 |
| OFFERING | public/secondary/stock/debt/convertible/ATM/registered direct ... offering, $X ... offering, prices/proposed/upsized ... offering, dilution, convertible, ATM, at-the-market, shelf registration, files shelf, private placement, IPO, 424B, Form S-1/S-3, raises $X million, 8-K item 3.02 |
| INSIDER | insider, Form 4, 10b5-1, CEO/CFO/director/founder ... buys/sells ... shares/stock/stake, sells $X (worth of) ... shares |
| EXEC | resigns, steps down, CEO/CFO/chair/president/director ... appointed/named/departs/retires/replaced, names/appoints/hires ... CEO/CFO, succession, new/interim CEO, 8-K item 5.02 |
| MACRO | Fed (case-sensitive, not "fed up"), Federal Reserve, FOMC, Powell, CPI, PPI, PCE, inflation, jobs report, payrolls, jobless claims, unemployment rate, GDP, tariffs, trade war/talks/deal(s), recession, interest rates, rate cut/hike, cuts/holds rates, Treasury/bond yields, export controls, government shutdown, debt ceiling |

Deliberate narrowing compared with a plain keyword list: "approval" counts as FDA only next to
drug/therapy/vaccine/device words, so "shareholders approve" or "board approves split" are not FDA.
"offering" needs a securities word nearby ("new AI offering" and "raises prices for its AI
offering" are not OFFERING). "beats"/"tops"/"misses" need an estimates word nearby, and "Wall
Street" alone is not one ("tops $400 as Wall Street cheers" is not EARNINGS). "settles" needs a
legal word nearby ("oil settles higher" is not LEGAL).

Other everyday phrasings that are left out on purpose:

* DEAL: shopping deals ("best deals", "Black Friday / Prime Day / holiday deals", "deal of the
  day", "a good deal"), "trade deal" (that is MACRO), "customer acquisition" costs.
* GUIDANCE: Microsoft Outlook, the e-mail app.
* LEGAL: "charging" (only "charge(s/d)" counts next to SEC/DOJ/regulators), "courts" as a verb
  ("Tesla courts buyers"; "Courts block ..." and "federal courts" still count), "Wall Street's
  verdict" (only a jury/court/$ verdict or a verdict against/in a case), "court of public
  opinion", "the jury is still out".
* INSIDER: the publishers Business Insider and Insider Monkey.
* EXEC: "CEO ... joins / names / taps / replace" ("CEO Tim Cook joins Trump", "CEO names three
  stocks"). It counts when the role comes after the verb ("names new CFO", "replaces CFO") or as
  "joins Rivian as CEO".
* ANALYST: an "upgrade cycle" (product), besides software/firmware/hardware upgrades.

HIGH impact = any of EARNINGS, GUIDANCE, DEAL, FDA, OFFERING, LEGAL, or published within the
last 60 minutes.

## Source notes

* Yahoo RSS: `https://feeds.finance.yahoo.com/rss/2.0/headline?s={SYM}&region=US&lang=en-US`.
  The RSS 2.0 `<item>`s carry title, link, pubDate (RFC 822) and description (HTML is stripped).
  The publisher comes from `<source>` when present, otherwise from the link host. The item that
  Yahoo returns for an unknown symbol ("RSS feed not found") is ignored. Atom `<entry>` is
  tolerated.
* Yahoo search: `https://query1.finance.yahoo.com/v1/finance/search?q={SYM}&quotesCount=0&newsCount=20`
  returns `news[]` with `title, publisher, link, providerPublishTime (unix), relatedTickers`.
  The nested `content{...}` shape is also accepted.
* SEC: `tc.sec_recent_filings(sym, forms={"8-K","8-K/A","6-K"}, since=<date>)`. The
  submissions JSON is refreshed at most every 60 s (the shared helper's default cache is 15 min).
  EDGAR's `acceptanceDateTime` ends in `Z` but holds Eastern wall-clock time, so it is read as ET.
  Against the real clock, if that reading would be in the future it is retried as UTC.
* A malformed or cut-off response from any source counts as that source failing (a warning that
  names the URL), never as a crash.
