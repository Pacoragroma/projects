#!/usr/bin/env python3
"""Copy shared/trader_common.py into every skills/*/scripts/ folder.

Each skill ships its own copy so it still works when installed on its own.
Run with --check to fail (exit 1) if any copy is out of date, e.g. in CI.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ROOT / "shared" / "trader_common.py"


def targets():
    return sorted(p / "scripts" / "trader_common.py" for p in (ROOT / "skills").iterdir() if p.is_dir())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="only report stale copies")
    args = ap.parse_args()
    source = CANONICAL.read_bytes()
    stale = [t for t in targets() if not t.exists() or t.read_bytes() != source]
    if args.check:
        for t in stale:
            print(f"stale: {t.relative_to(ROOT)}")
        return 1 if stale else 0
    for t in stale:
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(source)
        print(f"updated {t.relative_to(ROOT)}")
    if not stale:
        print("all copies up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
