#!/usr/bin/env python3
"""Desk decide: the numbers (desk_check.py) plus the news (the news agents) -> the desk's passes. PAPER ONLY.

Reads desk_check.py's --json output and the news agents' files (docs/agents/news-agent.md), and
gives every idea one decision by the runbook's rules, nothing else:
  pass   passes_numbers in the check, and its news row says red_flag false
  skip   the numbers failed, or the news row carries a red flag (its reason and sources are kept)
  hold   the numbers passed but no news row covers the symbol (an agent that ran out of time or
         failed): read news.py yourself, apply the written definition (RUNBOOK §3), and run this
         again with --mark SYM=ok or --mark SYM=flag:"why"
For each pass it copies the best stop's plan file into --desk's plans/ (the desk then trades it with
paper_autotrade.py --plan saved) and writes --passes as paper_autotrade.py's --ideas list. With
--candidates it sets each idea's dashboard row: decision and reason. It places no order.

  python3 tools/desk_decide.py --check $OUT/check-<date>.json --news $OUT/sweep/news-<date>-*.json \\
      --desk ~/.day-trader --passes $OUT/passes-<date>.json --candidates $OUT/candidates.json --json
  python3 tools/paper_autotrade.py --home ~/.day-trader --standing-order "..." --plan saved --ideas $OUT/passes-<date>.json

Exit codes: 0 decided (holds are in the output), 1 bad input (an unreadable check file, a bad --mark).
"""
import argparse
import glob
import json
import shutil
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(ROOT / "shared"))
import trader_common as tc  # noqa: E402

sys.path.insert(0, str(TOOLS))
import desk_check as dc  # noqa: E402  (its row shapes and its candidate-file merge)

PASS, SKIP, HOLD = "pass", "skip", "hold"


def load_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{what} {path}: {e}") from None


def load_news(patterns):
    """{symbol: news row} from every readable file, plus what went wrong with the rest."""
    rows, files, problems = {}, [], []
    for pat in patterns:
        matches = sorted(glob.glob(pat)) or [pat]
        for f in matches:
            try:
                doc = load_json(f, "news file")
                syms = doc.get("symbols") if isinstance(doc, dict) else doc
                if not isinstance(syms, list):
                    raise ValueError("no symbols list")
                n = 0
                for r in syms:
                    if isinstance(r, dict) and r.get("symbol"):
                        rows[str(r["symbol"]).strip().upper()] = r
                        n += 1
                files.append({"file": f, "symbols": n})
            except ValueError as e:
                problems.append(str(e))
    return rows, files, problems


def parse_marks(marks):
    """--mark SYM=ok | SYM=flag:why -> news rows written by the main session itself."""
    rows = {}
    for m in marks or []:
        sym, _, rest = m.partition("=")
        sym = sym.strip().upper()
        if not sym or not rest:
            raise ValueError(f"--mark {m!r}: use SYM=ok or SYM=flag:why")
        if rest.strip().lower() == "ok":
            rows[sym] = {"symbol": sym, "red_flag": False, "red_flag_reason": "none", "sources": [], "marked_by": "the desk"}
        elif rest.lower().startswith("flag:") and rest[5:].strip():
            rows[sym] = {"symbol": sym, "red_flag": True, "red_flag_reason": rest[5:].strip(), "sources": [], "marked_by": "the desk"}
        else:
            raise ValueError(f"--mark {m!r}: use SYM=ok or SYM=flag:why")
    return rows


def best_stop(idea):
    return next((s for s in idea.get("stops") or [] if s.get("name") == idea.get("best")), None)


def decide(idea, news):
    sym = idea["symbol"]
    row = news.get(sym)
    numbers = f"best stop {idea.get('best')}: {best_stop(idea).get('expected_r'):+.2f}R expected" if idea.get("passes_numbers") and best_stop(idea) else None
    if not idea.get("passes_numbers"):
        return SKIP, "; ".join(idea.get("reasons") or ["the numbers failed"]), row
    if row is None:
        return HOLD, f"{numbers}; no news row: read news.py and --mark {sym}=ok or {sym}=flag:why", None
    if row.get("red_flag"):
        src = ", ".join(str(s) for s in (row.get("sources") or [])[:3])
        return SKIP, f"red flag: {row.get('red_flag_reason') or 'unstated'}" + (f" ({src})" if src else ""), row
    return PASS, f"{numbers}; news: no red flag" + (f" ({row.get('catalyst')})" if row.get("catalyst") else ""), row


def copy_plan(idea, desk):
    src = (best_stop(idea) or {}).get("plan_file")
    if not src or not Path(src).is_file():
        return None, f"no plan file for the best stop ({src})"
    dest = desk / "plans" / Path(src).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return str(dest), None


def render(out):
    lines = [f"DESK DECIDE · {len(out['decisions'])} idea(s) · pass {len(out['passes'])} · skip {len(out['skips'])} · "
             f"hold {len(out['holds'])} · paper only, no orders"]
    for d in out["decisions"]:
        lines.append(f"  {d['decision']:<4} {d['symbol']:<10} {d['reason']}" + (f" -> {d['copied_to']}" if d.get("copied_to") else ""))
    if out["news_problems"]:
        lines.append("news files: " + "; ".join(out["news_problems"]))
    if out["passes"]:
        lines.append(f"Trade the passes with: paper_autotrade.py --plan saved --ideas {out['passes_file'] or '<--passes FILE>'}")
    if out["holds"]:
        lines.append("Holds need the desk's own news read and a --mark before they can pass.")
    lines.append(tc.DISCLAIMER)
    return "\n".join(lines)


def build_parser():
    p = tc.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", required=True, metavar="FILE", help="desk_check.py --json output")
    p.add_argument("--news", nargs="*", default=[], metavar="FILE", help="news agents' JSON files (globs allowed)")
    p.add_argument("--mark", action="append", metavar="SYM=ok|SYM=flag:why", help="the desk's own news verdict for a symbol")
    p.add_argument("--desk", metavar="DIR", help="copy each pass's best plan into DIR/plans/")
    p.add_argument("--passes", metavar="FILE", help="write the passes as paper_autotrade.py's --ideas list")
    p.add_argument("--candidates", metavar="FILE", help="set each idea's dashboard row (decision, reason)")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return p


def main():
    args = build_parser().parse_args()
    try:
        check = load_json(args.check, "check file")
        ideas = check.get("ideas") if isinstance(check, dict) else None
        if not isinstance(ideas, list):
            raise ValueError(f"check file {args.check}: not desk_check.py output (no ideas list)")
        news, files, problems = load_news(args.news)
        news.update(parse_marks(args.mark))
        desk = None
        if args.desk:
            desk = Path(args.desk).expanduser()
            if not (desk / "settings.json").is_file():
                raise ValueError(f"--desk {args.desk} holds no settings.json")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    decisions, cand_rows = [], []
    for idea in ideas:
        decision, reason, row = decide(idea, news)
        d = {"symbol": idea["symbol"], "side": idea.get("side"), "setup": idea.get("setup"), "decision": decision,
             "reason": reason, "news": row, "best": idea.get("best"), "copied_to": None}
        if decision == PASS and desk is not None:
            d["copied_to"], err = copy_plan(idea, desk)
            if err:
                d["decision"], d["reason"] = HOLD, f"{reason}; {err}"
        decisions.append(d)
        cand = dc.candidate_row(idea)
        cand["decision"] = {PASS: "pass", SKIP: "skip", HOLD: "pending news"}[d["decision"]]
        cand["reason"] = d["reason"]
        cand_rows.append(cand)
    passes = [d for d in decisions if d["decision"] == PASS]
    out = {"check": args.check, "news_files": files, "news_problems": problems, "decisions": decisions,
           "passes": [d["symbol"] for d in passes], "skips": [d["symbol"] for d in decisions if d["decision"] == SKIP],
           "holds": [d["symbol"] for d in decisions if d["decision"] == HOLD], "passes_file": args.passes,
           "mode": "paper", "real_money": False}
    if args.passes:
        Path(args.passes).parent.mkdir(parents=True, exist_ok=True)
        Path(args.passes).write_text(json.dumps([{"symbol": d["symbol"], "side": d["side"], "setup": d["setup"],
                                                   "note": d["reason"]} for d in passes], indent=1), encoding="utf-8")
    if args.candidates:
        dc.merge_candidates(args.candidates, cand_rows)
        out["candidates"] = args.candidates
    tc.emit(out, args.json, render)
    return 0


if __name__ == "__main__":
    tc.run(main)
