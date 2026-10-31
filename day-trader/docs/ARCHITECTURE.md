# Architecture

**Short version: 11 small CLI skills on one shared standard-library module. Each skill folder
ships its own byte-identical copy of that module, so any skill also works installed alone. State
is plain JSON files in `$DAYTRADER_HOME`. Data comes from free public endpoints only. The pack is
paper-only by construction.**

## Layout

```
.claude-plugin/marketplace.json      # marketplace "pacoragroma-projects" -> plugin ./day-trader
CLAUDE.md                            # how to work with the owner (read first)
HANDOFF.md                           # session handoff: state, what runs, first steps
day-trader/
  .claude-plugin/plugin.json         # plugin "day-trader", version 1.2.1
  README.md                          # user-facing docs (install, desk flow, crypto, limits)
  docs/                              # RUNBOOK.md, ARCHITECTURE.md (this), HISTORY.md
  shared/trader_common.py            # THE shared library (canonical copy, ~1,700 lines)
  skills/<skill>/SKILL.md            # instructions Claude reads (frontmatter name + description <= 1024 chars)
  skills/<skill>/scripts/*.py        # the CLI(s) + a vendored copy of trader_common.py
  skills/<skill>/reference.md        # longer method notes (broker, news, pelosi, time-machine)
  tests/                             # unittest, offline; helpers.py has fixtures and fakes
  tools/sync_common.py               # copy shared/trader_common.py into every skill (--check for CI)
  tools/package_skills.py            # build dist/<skill>.zip for the Claude apps; --check validates SKILL.md
  tools/smoke_live.py                # 17 live checks across the 9 data hosts, through the skills' own parsers
  tools/desk_snapshot.py             # the desk (and the shadow account) as one JSON file for the dashboard
  tools/desk_backup.py               # one account's state as one JSON (the dashboard's private backup)
  tools/desk_restore.py              # a backup back into $DAYTRADER_HOME; refuses to overwrite any desk file
  tools/paper_autotrade.py           # ticket and confirm paper trades under the owner's standing order
.github/workflows/day-trader.yml     # CI: sync check, tests, SKILL.md check on Python 3.9 and 3.12 (via uv)
.gitignore                           # also blocks stray *.json at the root and in day-trader/ (desk outputs carry the ledger)
```

**Rule:** edit only `shared/trader_common.py`, then run `tools/sync_common.py`. A test fails if
any copy differs.

## The shared library (`trader_common`, imported as `tc`)

| Area | What it gives the skills |
|---|---|
| State | `home()`, `load_json/save_json` (atomic), `load_settings/save_settings` (validated; `None` un-sets a key) |
| HTTP | `http_get` (retries 429/5xx, fails fast on proxy denials with an allow-list hint), `cached_get/get_json` (disk cache in `home()/cache`), offline fixtures via `$DAYTRADER_FIXTURES` |
| Markets and time | `tc.US` (09:30-16:00 ET, weekdays, Yahoo) and `tc.CRYPTO` (UTC days, always open, 23:55 time stop, fractional sizes). The **active market** (`use_market_for(symbol)`, `market_scope(...)`) decides what `now_et()`, `today_et()`, `et_hhmm()`, `in_regular_session()`, `is_market_open()` and `group_sessions()` mean. The names say "et", but in crypto mode they are UTC |
| Bars and quotes | `fetch_bars(symbol, interval, range)` and `quote(symbol)` route stocks to Yahoo and crypto to `crypto_bars/crypto_quote`, i.e. the configured exchange |
| Crypto exchanges | `CRYPTO_EXCHANGES` = binance (default; `data-api.binance.vision`, 1,000 klines per request, 4 requests at a time, 0.1% paper fee) and coinbase (`api.exchange.coinbase.com`, 300 candles per request, 0.6% fee). `crypto_exchange()` reads `$DAYTRADER_CRYPTO_EXCHANGE`, then the `crypto_exchange` setting. `product_info` gives size step and price tick; `crypto_symbols` gives the scan universe (stablecoins and real leveraged tokens dropped) |
| Symbols | `is_crypto_symbol` (BTC-USDT, BTCUSDT, BTC-USD, one-letter and non-Latin coin names), `crypto_pair`, `canonical_symbol` (one spelling per pair), `binance_symbol` (BTC-USD is priced from BTCUSDT: Binance's USD pairs are thin), `us_symbols()` (Nasdaq Trader lists, about 5,900 common stocks) |
| SEC | `sec_cik`, `sec_recent_filings` (User-Agent carries `DAYTRADER_CONTACT` if set) |
| Plans | `save_plan` (merges, validates the merged levels), `load_plan`, keyed by canonical symbol |
| Ledger and budget | `load_ledger/save_ledger`, `day_summary`, `budget_check(new_risk)` (daily max loss, max open risk, trades per day, tilt guard), `trade_initial_risk` (0 once the stop is past entry) |
| Sizing | `position_size(entry, stop, ..., step, fee)`: floors to the size step; for crypto the fees of both fills count toward the loss, so a stopped-out trade stays within the risk budget |
| CLI | `make_parser` (usage errors exit 1), `run(main)` (exit 1 bad input/state, 2 data source down), `emit`, `table`, `fmt_price/fmt_money/fmt_pct/fmt_qty` |

## The 11 skills

| Skill | Script | Job | Writes |
|---|---|---|---|
| scanner | `scan.py` | Rank daily-chart setups across the market or a watchlist: breakout, breakdown, gap up/down (stocks), unusual volume, pullback, NR7. Score = 40·volume + 40·move/ATR + 20·trend (NR7 weighted 0.6) | nothing |
| news | `news.py` | Yahoo RSS and search plus SEC 8-K/6-K (stocks), tagged EARNINGS/DEAL/FDA/...; `--new-only` remembers what was shown | `news_seen.json` |
| entry | `entry.py` | Walk 5-minute bars to the moment a setup triggers: ORB (`--or-start` for crypto), VWAP reclaim, level, PDH/PDL; only completed bars | plan `entry_signal` |
| exit | `exit_plan.py` | Stop (ATR, structure, percent, manual), target at R:R, time stop, fees in R | plan levels + `exit_plan` |
| time-machine | `time_machine.py` | 10,000 replays of the rest of the session from the symbol's past ~59 sessions; reports target first / stop first / open, expected R (after fees for crypto), a history check and a verdict | plan `time_machine` (+ `display_pct`) |
| risk | `risk.py` | Position size, warnings, settings (`config`) | plan `sizing`, settings |
| budget | `budget.py` | Daily guard-rails: `status`, `check` (exit 3 = blocked), `set`, `history` | settings |
| broker | `broker.py` | Paper tickets that wait for a yes; `confirm --yes`, `sync`, `close`, `cancel`, `positions`, `tickets`, `history` | `ledger.json` |
| alerts | `alerts.py` | Price, % move and VWAP alerts; `check` (fired = list), `watch` | `alerts.json` |
| analyst | `analyst.py` | `brief` (trend, levels, ATR, rel. volume, SEC) and `review` (journal stats by setup, side, hour, market, verdict) | nothing |
| pelosi | `pelosi.py` (+ `pelosi_pdf.py`, `pelosi_ptr.py`) | House PTR filings: `recent`, `ticker`, `member`, `filing`; disclosure lag | cache |

The desk flow ("plan a trade on SYM and test it") is written in the identical "Full desk run"
section of `exit/SKILL.md` and `time-machine/SKILL.md`: scanner → news → entry → exit →
time-machine → risk → budget → broker, then stop and wait for the yes.

## State files (`$DAYTRADER_HOME`, default `~/.day-trader`)

- `settings.json`: only the keys that differ from `DEFAULT_SETTINGS` in `trader_common.py`.
- `plans/<SYMBOL>.json`: `symbol, side, entry, stop, target, setup`, plus sections written by
  entry (`entry_signal`), exit (`exit_plan`), time-machine (`time_machine`, with `display_pct` so
  every skill prints the same rounded odds, and `fees` for crypto) and risk (`sizing`). A section
  computed for other levels is cleared when the levels change.
- `ledger.json`: `{tickets: [...], trades: [...]}`. Tickets have `id TKT-YYYYMMDD-NNN, symbol,
  side, qty, entry, stop, target, status (pending|working|confirmed|cancelled|expired), created_at,
  expires_at, market, exchange`. Trades have `id TRADE-..., entry_price, exit_price, exit_reason,
  pnl (net of fees), gross_pnl, fees, r_multiple, opened_at, closed_at, market, exchange`.
- `alerts.json`, `news_seen.json`, `desk_log.jsonl` (written by `tools/desk_snapshot.py --event`),
  `cache/` (closed candle windows are cached for 7 days).
- **Two accounts, one code base.** The bot keeps two homes: the desk (`/root/.day-trader`) and the
  shadow account (`/root/.day-trader-shadow`). Every tool works on whichever home
  `$DAYTRADER_HOME` names. `desk_snapshot.py --shadow-home` reads the second one for the
  dashboard without switching the active home.
- Outside the container: `tools/desk_backup.py` packs one account's settings, ledger, alerts,
  plans, desk log and last candidates into one JSON. It is kept in the dashboard's private store:
  `desk_state` for the desk, `shadow_state` for the shadow. `tools/desk_restore.py` writes it
  back.
  - The restore refuses a home that holds any desk file, so an older backup never rolls back or
    mixes with newer local state.
  - The backup refuses a home without `desk_log.jsonl`: that one was never restored (a recycled
    container), and its backup would replace the real one.

## Safety by construction

- The only network call is `tc.http_get`: GET, no body, to the documented data hosts. There are no
  API keys, no account endpoints and no order endpoints. `smoke_live` and the tests check the host
  list.
- `broker.py confirm` needs `--yes`. A yes within seconds of the ticket is refused. Tickets expire
  (15 minutes). Out of hours or past the time stop you only get a preview with no id. The budget
  is checked again at confirm. A manual close `--price` must be a price that traded since the fill
  (5-minute bars, within half a tick), so a made-up price can't book fake profit. With no bars
  available, the check is skipped with a note.
- SKILL.md rules tell Claude: show the ticket, stop, and confirm only after the owner's explicit
  yes in a later message, for that ticket id. That is the product's default.
- **This deployment runs under the owner's standing order (2026-10-31):** paper trades need no
  per-ticket yes. `tools/paper_autotrade.py` does the confirming. It calls the skills' own CLIs as
  subprocesses, so the budget check, previews, expiry and the 10-second minimum between ticket and
  confirm all still apply. It needs `--standing-order` to run, and it writes a desk-log line for
  every trade it confirms.

## Tests

- `tests/helpers.py`: `TempHomeTestCase` (isolated home; network guard; market reset; crypto
  exchange pinned to coinbase for older tests), synthetic bars, `fake_bars`, `fake_http`,
  `write_fixture`, `write_coinbase_*`, `write_binance_*`, and `run_cli` for subprocess tests.
- End-to-end desk flows: `test_desk_flow.py` (TSLA, US), `test_crypto_desk_flow.py` (BTC-USD on
  Coinbase), `test_binance_desk_flow.py` (BTC-USDT on Binance). The README's sample output is
  checked against real runs of these.
- 975 tests, all offline, about 2 minutes. `tools/smoke_live.py` is the live counterpart: 17
  checks across 9 hosts, all passing on 2026-10-31.
- `test_desk_snapshot.py` covers the dashboard data: snapshot rows, all-time results, the shadow
  rows, equity, stale tickets, ORB order, a missing input exits 1, and the backup → restore round
  trip. `test_desk_backup.py` covers the restore and backup guards. `test_paper_autotrade.py`
  covers the auto-trader: default and saved plans, skips, the 10-second confirm, the desk log.

## The video's original pack (installed alongside, not part of this repo)

`github.com/Jakeschincariol/day-trader-skill` v2.0.0 (MIT). Skills `trader-scanner`,
`trader-news`, `trader-entry`, `trader-exit`, `trader-timemachine`, `trader-risk`,
`trader-budget`, `trader-broker`, `trader-alerts`, `trader-analyst`, `trader-pelosi`. US stocks
only, with state in `./trader/` of the working folder. Differences that matter:

- **Scanner:** a daily scan, plus an **intraday opening-range sweep** (`scan.py orb`) across
  thousands of charts using Yahoo's batch "spark" endpoint. Ours has no intraday sweep.
- **Broker:** sends approved orders to an **Alpaca paper account** through Alpaca's MCP server
  (three gates: risk PASS, the owner's typed yes, Claude's Allow-once card). Ours keeps a local
  paper ledger.
- Its own 315 tests pass in this environment. Its only subprocess call is `pdftotext` (Pelosi).
