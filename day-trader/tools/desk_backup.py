#!/usr/bin/env python3
"""Build a backup of the paper desk's state, to keep somewhere that outlives the container.

Reads $DAYTRADER_HOME (default ~/.day-trader): settings.json, ledger.json, alerts.json,
plans/*.json and desk_log.jsonl. Writes one JSON object:
{saved_at, settings, ledger, alerts, desk_log, plans, candidates, note}. `candidates` (the last
desk-checked candidates a dashboard shows) comes from --candidates, else from --previous (the
last backup), else []. tools/desk_restore.py reverses it. It refuses a home without desk_log.jsonl:
that is a desk never restored or set up (say a recycled container where only a setting was
written), whose backup would replace the real one. Paper only; nothing leaves this machine.

  python3 tools/desk_backup.py --out /tmp/desk_state.json --candidates /tmp/candidates.json
  python3 tools/desk_backup.py --out /tmp/desk_state.json --previous /tmp/old/desk_state.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

NOTE = ("Backup of a day-trader paper desk ($DAYTRADER_HOME). Restore with tools/desk_restore.py. "
        "candidates = the last desk-checked candidates (for tools/desk_snapshot.py --candidates). Paper only.")


def desk_home() -> Path:
    return Path(os.environ.get("DAYTRADER_HOME") or (Path.home() / ".day-trader")).expanduser()


def _json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _unwrap(doc):
    """A backup as ArtifactData saves it may sit under "data"."""
    return doc.get("data", doc) if isinstance(doc, dict) and "settings" not in doc else doc


def build(home: Path, candidates=None, previous=None) -> dict:
    log = home / "desk_log.jsonl"
    plans = home / "plans"
    if candidates is None:
        candidates = (previous or {}).get("candidates") or []
    return {
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "settings": _json(home / "settings.json", {}),
        "ledger": _json(home / "ledger.json", {"tickets": [], "trades": []}),
        "alerts": _json(home / "alerts.json", None),
        "desk_log": [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        if log.exists() else [],
        "plans": {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(plans.glob("*.json"))}
        if plans.exists() else {},
        "candidates": candidates,
        "note": NOTE,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="where to write the backup JSON")
    ap.add_argument("--candidates", help="JSON list of the last desk-checked candidates")
    ap.add_argument("--previous", help="the last backup, to carry its candidates over")
    args = ap.parse_args()
    for flag in ("candidates", "previous"):
        path = getattr(args, flag)
        if path and not Path(path).exists():
            print(f"error: --{flag} {path} does not exist", file=sys.stderr)
            return 1
    home = desk_home()
    if not (home / "settings.json").exists() and not (home / "ledger.json").exists():
        print(f"error: {home} holds no desk (no settings.json or ledger.json) - nothing to back up", file=sys.stderr)
        return 1
    if not (home / "desk_log.jsonl").exists():   # a desk that was restored or set up always has its log
        print(f"error: {home} has no desk_log.jsonl, so it was never restored or set up here (a recycled "
              "container?). Restore it first (tools/desk_restore.py): this backup would replace the real one. "
              "For a brand-new desk, write its first log line first.", file=sys.stderr)
        return 1
    candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8")) if args.candidates else None
    previous = _unwrap(json.loads(Path(args.previous).read_text(encoding="utf-8"))) if args.previous else None
    state = build(home, candidates, previous)
    Path(args.out).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"out": args.out, "saved_at": state["saved_at"], "trades": len(state["ledger"].get("trades", [])),
                      "tickets": len(state["ledger"].get("tickets", [])), "plans": len(state["plans"]),
                      "desk_log": len(state["desk_log"]), "candidates": len(state["candidates"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
