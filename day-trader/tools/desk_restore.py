#!/usr/bin/env python3
"""Restore a paper desk from a backup made by tools/desk_backup.py.

Writes settings.json, ledger.json, alerts.json, plans/*.json and desk_log.jsonl into
$DAYTRADER_HOME (default ~/.day-trader). It refuses to touch a home that already holds any desk
file (settings.json, ledger.json, alerts.json, desk_log.jsonl or a plan) unless --force, so a newer
local state is never rolled back by, or mixed with, an older backup. --candidates-out also writes the backup's candidates list (for tools/desk_snapshot.py).

  python3 tools/desk_restore.py /tmp/backup/data/users/me/desk_state.json --candidates-out /tmp/candidates.json
"""
import argparse
import json
import os
import sys
from pathlib import Path


DESK_FILES = ("settings.json", "ledger.json", "alerts.json", "desk_log.jsonl")


def desk_home() -> Path:
    return Path(os.environ.get("DAYTRADER_HOME") or (Path.home() / ".day-trader")).expanduser()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("backup", help="the backup JSON (as saved by ArtifactData get ... out_dir, or desk_backup.py)")
    ap.add_argument("--candidates-out", help="also write the backup's candidates list here")
    ap.add_argument("--force", action="store_true", help="overwrite a desk that already exists here")
    args = ap.parse_args()
    src = Path(args.backup)
    if not src.exists():
        print(f"error: {src} does not exist", file=sys.stderr)
        return 1
    doc = json.loads(src.read_text(encoding="utf-8"))
    state = doc.get("data", doc) if isinstance(doc, dict) and "settings" not in doc else doc
    if not isinstance(state, dict) or "settings" not in state:
        print(f"error: {src} is not a desk backup (no settings)", file=sys.stderr)
        return 1
    home = desk_home()
    present = [n for n in DESK_FILES if (home / n).exists()]
    if (home / "plans").is_dir() and any((home / "plans").glob("*.json")):
        present.append("plans/")
    if present and not args.force:   # any desk file: a newer local state must never be rolled back or mixed
        print(f"error: {home} already holds a desk ({', '.join(present)}); a restore would overwrite it. "
              "Keep it and back it up, merge by hand, or pass --force", file=sys.stderr)
        return 1
    (home / "plans").mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(state.get("settings") or {}), encoding="utf-8")
    (home / "ledger.json").write_text(json.dumps(state.get("ledger") or {"tickets": [], "trades": []}), encoding="utf-8")
    if state.get("alerts"):
        (home / "alerts.json").write_text(json.dumps(state["alerts"]), encoding="utf-8")
    for name, plan in (state.get("plans") or {}).items():
        (home / "plans" / Path(name).name).write_text(json.dumps(plan), encoding="utf-8")
    (home / "desk_log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in state.get("desk_log") or []),
                                         encoding="utf-8")
    if args.candidates_out:
        Path(args.candidates_out).write_text(json.dumps(state.get("candidates") or []), encoding="utf-8")
    print(json.dumps({"restored_to": str(home), "saved_at": state.get("saved_at"),
                      "files": sorted(p.name for p in home.iterdir()),
                      "candidates": len(state.get("candidates") or [])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
