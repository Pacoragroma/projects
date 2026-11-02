#!/usr/bin/env python3
"""Pre-market study helpers: the candidates from both scanners, and the agents' parts merged. PAPER ONLY.

The weekday pre-market study (RUNBOOK §2a) has helper agents read the charts and the news of the
names both scanners flagged. This tool does the two mechanical steps around them:

  candidates   our scanner's JSON (--us) and the original pack's daily watchlist (--original) ->
               one list of liquid candidates in groups: our hits with a score of --min-score or
               more and --min-dollar-volume or more a day (the --max-ours most traded of them),
               then the original's most traded hits up to --max names in all, --group-size a
               group. Writes --out (a JSON object with "candidates" and "groups") and prints the groups.
  merge        the chart agents' parts (--charts, globs), the news agents' files (--news, globs)
               and the macro agent's file (--macro) merged by symbol into the study (--out):
               {"date", "macro", "symbols": [{"symbol", "scan", "chart", "news"}], "missing"}.
               A file that is missing or unreadable is reported, never fatal.

  python3 tools/premarket.py candidates --us $OUT/scan/us.json \\
      --original /home/user/original-desk/trader/watchlist.json --out $OUT/premarket/candidates-<date>.json --json
  python3 tools/premarket.py merge --date <date> --candidates $OUT/premarket/candidates-<date>.json \\
      --charts "$OUT/premarket/parts/charts-<date>-*.json" --news "$OUT/sweep/news-<date>-premarket-*.json" \\
      --macro $OUT/premarket/parts/macro-<date>.json --out $OUT/premarket/study-<date>.json --json

Exit codes: 0 done, 1 bad input (an unreadable scan file, no candidates).
"""
import argparse
import glob
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(ROOT / "shared"))
import trader_common as tc  # noqa: E402

DEFAULT_MAX, DEFAULT_MAX_OURS, DEFAULT_GROUP, DEFAULT_MIN_SCORE, DEFAULT_MIN_DV = 50, 30, 10, 60, 20e6
SCAN_FIELDS = ("score", "bias", "last", "change_pct", "rvol", "atr_pct", "dollar_volume_20d", "high_20d", "low_20d", "rsi14")


def load_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{what} {path}: {e}") from None


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------

def candidates(us, original, max_names, max_ours, group_size, min_score, min_dv):
    ours = []
    for r in (us.get("results") or []) if isinstance(us, dict) else []:
        sym = str(r.get("symbol") or "").upper()
        dv = r.get("dollar_volume_20d") or 0
        if sym and (r.get("score") or 0) >= min_score and dv >= min_dv:
            row = {"symbol": sym, "from": "ours", "setups": [s.get("setup") for s in r.get("setups") or [] if isinstance(s, dict)]}
            row.update({k: r.get(k) for k in SCAN_FIELDS})
            ours.append(row)
    ours.sort(key=lambda r: (-(r.get("dollar_volume_20d") or 0), r["symbol"]))   # the most traded first
    rows = {r["symbol"]: r for r in ours[:max_ours]}
    hits = (original.get("hits") or []) if isinstance(original, dict) else (original if isinstance(original, list) else [])
    for h in hits:                                   # the original pack lists the most traded first
        sym = str(h.get("symbol") or "").upper()
        if not sym:
            continue
        if sym in rows:
            rows[sym]["from"] = "both"
            rows[sym]["original_tags"] = h.get("setups")
        elif len(rows) < max_names:
            rows[sym] = {"symbol": sym, "from": "original", "original_tags": h.get("setups"), "last": h.get("price"),
                         "rsi14": h.get("rsi"), "dollar_volume_20d": None}
    out = list(rows.values())[:max_names]            # ours by dollar volume, then the original's order
    groups = [[r["symbol"] for r in out[i:i + group_size]] for i in range(0, len(out), group_size)]
    return {"candidates": out, "groups": groups,
            "counts": {"ours": len(ours), "ours_kept": min(len(ours), max_ours), "original_hits": len(hits), "kept": len(out)},
            "filters": {"min_score": min_score, "min_dollar_volume": min_dv, "max": max_names, "max_ours": max_ours,
                        "group_size": group_size}}


def render_candidates(doc):
    c = doc["counts"]
    lines = [f"PRE-MARKET CANDIDATES · {c['kept']} names in {len(doc['groups'])} group(s) · ours {c['ours_kept']} of "
             f"{c['ours']} (score {doc['filters']['min_score']}+, ${tc.fmt_big(doc['filters']['min_dollar_volume'])}+ a day) · "
             f"the original's hits {c['original_hits']} · paper only"]
    for i, g in enumerate(doc["groups"]):
        lines.append(f"  group {i}: {' '.join(g)}")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def read_parts(patterns, what):
    """{symbol: row} from every readable part file, plus the problems."""
    rows, files, problems = {}, [], []
    for pat in patterns:
        for f in (sorted(glob.glob(pat)) or [pat]):
            try:
                doc = load_json(f, what)
                syms = doc.get("symbols") if isinstance(doc, dict) else doc
                if not isinstance(syms, list):
                    raise ValueError(f"{what} {f}: no symbols list")
                n = 0
                for r in syms:
                    if isinstance(r, dict) and r.get("symbol"):
                        rows[str(r["symbol"]).strip().upper()] = r
                        n += 1
                files.append({"file": f, "symbols": n})
            except ValueError as e:
                problems.append(str(e))
    return rows, files, problems


def merge(date, cands, charts, news, macro):
    symbols = []
    for c in cands.get("candidates") or []:
        sym = c["symbol"]
        symbols.append({"symbol": sym, "scan": c, "chart": charts.get(sym), "news": news.get(sym)})
    return {"date": date, "generated_for": "the 09:50 ET opening-range sweep", "macro": macro, "symbols": symbols,
            "missing": {"chart": [s["symbol"] for s in symbols if not s["chart"]],
                        "news": [s["symbol"] for s in symbols if not s["news"]]},
            "red_flags": [f"{s['symbol']}: {s['news'].get('red_flag_reason')}" for s in symbols if s["news"] and s["news"].get("red_flag")],
            "good": [s["symbol"] for s in symbols if (s["chart"] or {}).get("orb_follow_through") == "good"],
            "poor": [s["symbol"] for s in symbols if (s["chart"] or {}).get("orb_follow_through") == "poor"]}


def render_merge(out):
    m = out["missing"]
    lines = [f"PRE-MARKET STUDY · {out['date']} · {len(out['symbols'])} symbols · charts missing {len(m['chart'])} · "
             f"news missing {len(m['news'])} · macro {'yes' if out['macro'] else 'MISSING'} · paper only",
             f"  good: {' '.join(out['good']) or 'none'}", f"  poor: {' '.join(out['poor']) or 'none'}",
             f"  red flags: {'; '.join(out['red_flags']) or 'none'}"]
    if m["chart"]:
        lines.append(f"  no chart row: {' '.join(m['chart'])}")
    if m["news"]:
        lines.append(f"  no news row: {' '.join(m['news'])}")
    for p in out.get("problems") or []:
        lines.append(f"  problem: {p}")
    lines.append(f"  written: {out['out']}")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = tc.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidates", help="the liquid names of both scanners, in groups")
    c.add_argument("--us", required=True, metavar="FILE", help="our scanner's --json output (the US daily scan)")
    c.add_argument("--original", required=True, metavar="FILE", help="the original pack's trader/watchlist.json")
    c.add_argument("--out", required=True, metavar="FILE", help="where the candidates JSON goes")
    c.add_argument("--max", type=int, default=DEFAULT_MAX, metavar="N", help=f"at most this many names (default {DEFAULT_MAX})")
    c.add_argument("--max-ours", type=int, default=DEFAULT_MAX_OURS, metavar="N",
                   help=f"at most this many of our own hits, so the original's hits keep room (default {DEFAULT_MAX_OURS})")
    c.add_argument("--group-size", type=int, default=DEFAULT_GROUP, metavar="N", help=f"names per agent (default {DEFAULT_GROUP})")
    c.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, metavar="S", help=f"our hits need this score (default {DEFAULT_MIN_SCORE:g})")
    c.add_argument("--min-dollar-volume", type=float, default=DEFAULT_MIN_DV, metavar="USD", help="our hits need this 20-day dollar volume (default $20M)")
    tc.add_json_flag(c)
    m = sub.add_parser("merge", help="the agents' parts into the study JSON")
    m.add_argument("--date", required=True, metavar="YYYY-MM-DD")
    m.add_argument("--candidates", required=True, metavar="FILE", help="the candidates JSON from `candidates`")
    m.add_argument("--charts", nargs="*", default=[], metavar="GLOB", help="the chart agents' part files")
    m.add_argument("--news", nargs="*", default=[], metavar="GLOB", help="the news agents' files")
    m.add_argument("--macro", metavar="FILE", help="the macro agent's file")
    m.add_argument("--out", required=True, metavar="FILE", help="where the study JSON goes")
    tc.add_json_flag(m)
    return p


def main():
    args = build_parser().parse_args()
    as_json = getattr(args, "json", False)
    try:
        if args.cmd == "candidates":
            if args.max < 1 or args.max_ours < 0 or args.group_size < 1:
                raise ValueError("--max and --group-size must be at least 1, --max-ours 0 or more")
            doc = candidates(load_json(args.us, "our scan"), load_json(args.original, "the original's watchlist"),
                             args.max, args.max_ours, args.group_size, args.min_score, args.min_dollar_volume)
            if not doc["candidates"]:
                raise ValueError("no candidates: both scans are empty or nothing is liquid enough")
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(doc, indent=1), encoding="utf-8")
            doc["out"] = args.out
            tc.emit(doc, as_json, render_candidates)
            return 0
        cands = load_json(args.candidates, "candidates")
        if not isinstance(cands, dict) or not isinstance(cands.get("candidates"), list):
            raise ValueError(f"candidates {args.candidates}: not this tool's candidates JSON")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    charts, chart_files, problems = read_parts(args.charts, "chart part")
    news, news_files, more = read_parts(args.news, "news file")
    problems += more
    macro = None
    if args.macro:
        try:
            macro = load_json(args.macro, "macro file")
        except ValueError as e:
            problems.append(str(e))
    out = merge(args.date, cands, charts, news, macro)
    out.update({"problems": problems, "chart_files": chart_files, "news_files": news_files, "out": args.out})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({k: v for k, v in out.items() if k not in ("problems", "chart_files", "news_files", "out")},
                                         indent=1), encoding="utf-8")
    tc.emit(out, as_json, render_merge)
    return 0


if __name__ == "__main__":
    tc.run(main)
