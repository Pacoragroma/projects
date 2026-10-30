# Day Trader — 11 Claude skills that work like a small trading desk

One skill scans thousands of charts for setups, one tracks breaking news, one watches a setup bar
by bar for the entry, one sets the stop and target before you get in, and the **Time Machine**
replays the rest of the session 10,000 times from the stock's own recent bars to show the honest
odds of hitting your target first, your stop first, or neither. Risk sizes the trade, Budget
enforces daily limits, and the Broker is a **paper-only** order ticket that waits for your "yes".

* **Free and MIT-licensed. No signup, no API key.**
* Plain Python 3.9+ scripts, standard library only (the `pelosi` skill uses `pypdf` if it happens
  to be installed and has a built-in fallback).
* Free public data: Yahoo Finance charts, Nasdaq Trader symbol lists, SEC EDGAR, and House Clerk
  financial disclosures.
* **Paper trading only.** Nothing in this pack can place a real order.

## The skills

Commands are shown relative to each skill's folder (`skills/<skill>/`); Claude runs them for you.
Every command takes `--json`, which is how the skills hand results to each other.

| Skill | What it does | Main command |
|---|---|---|
| `scanner` | Scans ~5,900 US stocks (or a watchlist) for daily setups: breakouts, breakdowns, gaps, unusual volume, pullbacks, NR7 coils | `python3 scripts/scan.py --symbols TSLA,NVDA,AMD` |
| `news` | Breaking headlines and SEC 8-K/6-K filings per ticker, tagged (EARNINGS, DEAL, FDA, ...) with HIGH-impact flags | `python3 scripts/news.py TSLA` |
| `entry` | Watches 5-minute bars and says the moment an ORB, VWAP reclaim, level break or prior-day high/low break triggers | `python3 scripts/entry.py TSLA --save` |
| `exit` | "THE PLAN" before you enter: stop (ATR, structure, percent or yours), target, R:R, time stop | `python3 scripts/exit_plan.py TSLA --entry 364.18 --save` |
| `time-machine` | Replays the rest of the session 10,000 times from the stock's past ~59 sessions, plus every past day at the same clock time | `python3 scripts/time_machine.py TSLA --entry 364.18 --stop 358.32 --target 370.04` |
| `risk` | Position size from your risk budget, with buying-power, R:R and daily-budget checks | `python3 scripts/risk.py size --entry 364.18 --stop 358.32` |
| `budget` | Daily guard-rails: max loss, max open risk, max trades, tilt guard; OK/BLOCKED before every ticket | `python3 scripts/budget.py check --risk 46.88` |
| `broker` | Paper order ticket that waits for your explicit yes; paper fills, stop/target/time-stop exits, history | `python3 scripts/broker.py ticket --symbol TSLA --qty 8 --entry 364.18 --stop 358.32 --target 370.04` |
| `alerts` | Price alerts: above/below a level, % moves, VWAP crosses, or straight from a saved plan | `python3 scripts/alerts.py add TSLA --above 370.04` |
| `analyst` | Pre-trade ticker brief (trend, levels, ATR, relative volume, SEC filings) and a review of your paper journal | `python3 scripts/analyst.py brief TSLA` |
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

On the pack's offline test data (synthetic bars, not a real quote), that morning looks like this:

```text
TSLA 5m · ORB(15) · TRIGGERED 10:15 ET · LONG @ 364.18 (OR 358.32–362.40)
THE PLAN — TSLA LONG · manual stop
ENTRY 364.18 / STOP 358.32 / TARGET 370.04 / R:R 1.0
TIME MACHINE · TSLA · LONG from 10:15 ET · 10,000 replays from TSLA's past 59 sessions
HIT TARGET FIRST    31.6%  ±0.5
HIT STOP FIRST      31.6%  ±0.5
OPEN AT CLOSE       36.8%  (avg -0.05R)
VERDICT: CLOSE TO A COIN FLIP
BUY 8 TSLA @ 364.18 · STOP 358.32 · TARGET 370.04 · PAPER
BUDGET CHECK · TSLA · new risk $46.88 -> OK
BUY 8 TSLA · LIMIT 364.18 · TARGET 370.04 · STOP 358.32 · PAPER · REAL MONEY: OFF
WAITING FOR YOUR YES — reply yes to confirm ticket TKT-20261030-001
```

Only after you reply **yes**, in a later message, does Claude run
`python3 skills/broker/scripts/broker.py confirm TKT-... --yes`. After that, `broker.py sync` applies
the stop, the target and the 15:55 ET time stop from 5-minute bars, `budget.py status` shows what
you have risked so far, and `analyst.py review` shows what is and isn't working.
`tests/test_desk_flow.py` runs this whole chain end to end, offline.

## Safety

* **Paper only.** There is no live mode, no broker connection and no `--live` flag. Nothing here
  can place a real order or move real money.
* **No trade until you say yes.** The broker opens a paper position only through
  `confirm <ticket> --yes`. Claude is told to show the ticket and wait for your explicit yes in a
  later message. "Looks good?" or "maybe" is not a yes, and a yes counts only for that ticket.
* **Daily limits are enforced.** A ticket that would break your loss budget, open-risk limit,
  trade count or tilt guard is refused.
* **Not financial advice.** Scanner hits, news tags and entry triggers are prompts to do your own
  research, not signals.
* **Simulations are not guarantees.** The Time Machine recycles roughly 59 past sessions. Its odds
  carry real uncertainty and assume fills exactly at your levels. Past behavior does not predict
  the future.

## Install

**Claude Code (plugin)**

```text
/plugin marketplace add Pacoragroma/projects
/plugin install day-trader@pacoragroma-projects
```

**Claude Code (manual).** Copy the skill folders into your personal skills folder:

```bash
git clone https://github.com/Pacoragroma/projects.git
mkdir -p ~/.claude/skills && cp -r projects/day-trader/skills/* ~/.claude/skills/
```

**Claude apps (claude.ai, desktop).** Build one zip per skill, then upload each one under
**Settings → Capabilities → Skills**:

```bash
cd projects/day-trader && python3 tools/package_skills.py     # writes dist/<skill>.zip
```

**Or** paste the repository link into a chat with Claude and ask it to install the skills.

Each skill folder ships its own copy of `trader_common.py`, so any single skill works on its own.

## Network access

The scripts call these hosts over HTTPS and nothing else. In a sandbox, these hosts need to be on
the allow-list. When a host a command needs is unreachable, the script prints a one-line
`data error: ...` naming it and exits with code 2. Optional sources, such as one of the news
feeds, only add a warning. Nothing makes up prices or headlines to fill the gap.

| Host | Used by | For |
|---|---|---|
| `query1.finance.yahoo.com`, `query2.finance.yahoo.com` | scanner, news, entry, exit, time-machine, risk, budget, broker, alerts, analyst | chart bars and last price (`/v8/finance/chart`), news search (`/v1/finance/search`) |
| `feeds.finance.yahoo.com` | news | headline RSS feed |
| `www.nasdaqtrader.com` | scanner | the US symbol lists (universe scans only, not `--symbols`) |
| `www.sec.gov` | news, analyst | ticker to CIK map (`/files/company_tickers.json`) |
| `data.sec.gov` | news, analyst | filings (`/submissions`) and shares outstanding (`/api/xbrl`) |
| `disclosures-clerk.house.gov` | pelosi | Periodic Transaction Report index and PDFs |

`risk` only needs Yahoo when no entry price is given. `budget` only needs it for `status --mark`.

## Configuration

State lives in `$DAYTRADER_HOME` (default `~/.day-trader`): `settings.json`, `plans/`, the paper
`ledger.json`, `alerts.json`, `news_seen.json` and a download `cache/`.

```bash
python3 skills/risk/scripts/risk.py config --account 25000 --risk-pct 0.2 --max-position-pct 25 --min-rr 1
python3 skills/budget/scripts/budget.py set --daily-max-loss 150 --max-open-risk 150 --max-trades 5 --max-losses 3
python3 skills/risk/scripts/risk.py config        # show the current settings
```

The values above are the defaults. Two settings have no command: `ticket_ttl_minutes` (15) and
`contact_email`. To change them, edit `settings.json`.

| Environment variable | Effect |
|---|---|
| `DAYTRADER_HOME` | where state is kept (default `~/.day-trader`) |
| `DAYTRADER_CONTACT` | your email for the User-Agent that SEC EDGAR asks automated clients to send (or set `contact_email`) |
| `DAYTRADER_NOW` | pretend it is this US/Eastern time, for replays and demos, e.g. `DAYTRADER_NOW=2026-10-29T10:15`; bars that start after it are ignored |

## Development

```bash
cd day-trader
python3 -m unittest discover -s tests       # the full offline test suite (no network)
python3 tools/sync_common.py                # after editing shared/trader_common.py: refresh every copy
python3 tools/sync_common.py --check        # fail if any skill's copy is stale (CI)
python3 tools/package_skills.py --check     # validate every SKILL.md frontmatter (CI)
```

`shared/trader_common.py` is the one canonical shared library; never edit the vendored copies in
`skills/*/scripts/`. Tests use synthetic data. CLI tests read canned responses from
`$DAYTRADER_FIXTURES` (see `tests/helpers.py`), so the suite never touches the network. CI runs
the suite on Python 3.9 and 3.12.

## Limitations

* **Unofficial Yahoo endpoints.** Yahoo Finance has no official free API. Its chart and search
  endpoints can change, rate-limit or block without notice.
* **About 60 days of 5-minute history.** That is all Yahoo keeps, so the Time Machine learns from
  about 59 sessions and `entry --date` can replay only that far back.
* **No holiday calendar.** The market-open check is 09:30–16:00 ET on weekdays. It does not know
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

## License

MIT. See [LICENSE](LICENSE).
