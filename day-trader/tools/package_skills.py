#!/usr/bin/env python3
"""Build one upload-ready zip per skill: dist/<skill>.zip.

Each zip holds a single top-level folder named after the skill:

    <skill>/SKILL.md
    <skill>/reference.md          (if the skill has one)
    <skill>/LICENSE               (the pack's MIT license)
    <skill>/scripts/...           (its scripts, including its copy of trader_common.py)

__pycache__ folders and compiled files are left out. Zips are deterministic (sorted entries,
fixed timestamps), so rebuilding unchanged skills gives byte-identical files.

Usage (from day-trader/):
    python3 tools/package_skills.py                  # check, then build dist/*.zip
    python3 tools/package_skills.py --check          # only validate every SKILL.md (CI)
    python3 tools/package_skills.py risk broker      # just these skills
    python3 tools/package_skills.py --out /tmp/zips

--check validates each SKILL.md frontmatter: `name` matches the folder, is at most 64
characters of lowercase letters, digits and hyphens, and avoids the reserved words "anthropic"
and "claude"; `description` is present, at most 1024 characters, and has no XML tags. It also
checks that scripts/trader_common.py matches shared/ (run tools/sync_common.py if not).
Exit code 0 = OK, 1 = problems found.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent          # day-trader/
SKILLS = ROOT / "skills"
SHARED = ROOT / "shared" / "trader_common.py"
LICENSE = ROOT / "LICENSE"
DIST = ROOT / "dist"

MAX_NAME = 64
MAX_DESCRIPTION = 1024
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
RESERVED = ("anthropic", "claude")
XML_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")
ZIP_DATE = (1980, 1, 1, 0, 0, 0)                        # fixed: reproducible zips
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".tmp", ".swp")


# ---------------------------------------------------------------------------
# SKILL.md frontmatter (the small YAML subset SKILL.md files use)
# ---------------------------------------------------------------------------

def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    return value


def _block_scalar(style: str, lines: List[str]) -> str:
    """Body of a `>` / `|` block scalar (with -/+ chomping) from its raw, indented lines."""
    indents = [len(x) - len(x.lstrip(" ")) for x in lines if x.strip()]
    cut = min(indents) if indents else 0
    body = [x[cut:] if x.strip() else "" for x in lines]
    if style[0] == "|":
        text = "\n".join(body)
    else:  # folded: adjacent lines join with a space; each blank line is one newline
        text, blanks, started = "", 0, False
        for x in body:
            if not x:
                blanks += 1
                continue
            text += ("\n" * blanks if blanks else (" " if started else "")) + x.strip()
            blanks, started = 0, True
        text += "\n" * blanks
    chomp = style[1:2]
    core = text.rstrip("\n")
    if chomp == "-":
        return core
    if chomp == "+":
        return text + "\n"
    return core + "\n" if core else ""


def parse_frontmatter(text: str) -> Dict[str, str]:
    """{key: value} from the `---` block at the top of a SKILL.md. Raises ValueError."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("must start with a '---' frontmatter block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("frontmatter has no closing '---'") from None
    fields: Dict[str, str] = {}
    i = 1
    while i < end:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.fullmatch(r"([A-Za-z0-9_-]+):(?:\s+(.*?))?\s*", line)
        if not m or line[0].isspace():
            raise ValueError(f"frontmatter line {i + 1}: expected 'key: value', got {line.strip()!r}")
        key, rest = m.group(1), (m.group(2) or "")
        if key in fields:
            raise ValueError(f"frontmatter key {key!r} appears twice")
        i += 1
        block: List[str] = []
        while i < end and (not lines[i].strip() or lines[i][0].isspace()):
            block.append(lines[i])
            i += 1
        if re.fullmatch(r"[>|][-+]?", rest):
            fields[key] = _block_scalar(rest, block)
        else:  # plain or quoted scalar; indented continuation lines fold into it
            parts = [rest] + [x.strip() for x in block if x.strip()]
            fields[key] = _unquote(" ".join(p for p in parts if p).strip())
    return fields


def check_frontmatter(folder: str, fields: Dict[str, str]) -> List[str]:
    """Problems with a skill's frontmatter (empty list = OK)."""
    problems: List[str] = []
    name = (fields.get("name") or "").strip()
    if not name:
        problems.append("missing 'name'")
    else:
        if name != folder:
            problems.append(f"name {name!r} does not match its folder {folder!r}")
        if len(name) > MAX_NAME:
            problems.append(f"name is {len(name)} characters (max {MAX_NAME})")
        if not NAME_RE.fullmatch(name):
            problems.append(f"name {name!r} must be lowercase letters, digits and single hyphens")
        for word in RESERVED:
            if word in name:
                problems.append(f"name {name!r} contains the reserved word {word!r}")
    desc = (fields.get("description") or "").strip()
    if not desc:
        problems.append("missing 'description'")
    else:
        if len(desc) > MAX_DESCRIPTION:
            problems.append(f"description is {len(desc)} characters (max {MAX_DESCRIPTION})")
        tag = XML_TAG_RE.search(desc)
        if tag:
            problems.append(f"description contains an XML tag ({tag.group(0)})")
    return problems


# ---------------------------------------------------------------------------
# Skills on disk
# ---------------------------------------------------------------------------

def skill_dirs(names: Optional[Iterable[str]] = None) -> List[Path]:
    found = sorted(p for p in SKILLS.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))
    if not names:
        return found
    by_name = {p.name: p for p in found}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise ValueError(f"unknown skill(s): {', '.join(unknown)} (have: {', '.join(by_name)})")
    return [by_name[n] for n in names]


def check_skill(skill: Path) -> List[str]:
    """Every problem that would make this skill's zip broken or rejected."""
    md = skill / "SKILL.md"
    if not md.is_file():
        return ["no SKILL.md"]
    try:
        problems = check_frontmatter(skill.name, parse_frontmatter(md.read_text(encoding="utf-8")))
    except (ValueError, UnicodeDecodeError) as e:
        problems = [f"SKILL.md: {e}"]
    scripts = skill / "scripts"
    common = scripts / "trader_common.py"
    if not scripts.is_dir():
        problems.append("no scripts/ folder")
    elif not common.is_file():
        problems.append("scripts/trader_common.py is missing - run tools/sync_common.py")
    elif SHARED.is_file() and common.read_bytes() != SHARED.read_bytes():
        problems.append("scripts/trader_common.py differs from shared/ - run tools/sync_common.py")
    return problems


def package_files(skill: Path) -> List[Tuple[str, Path]]:
    """(name inside the zip, source path), sorted, for one skill."""
    files: List[Tuple[str, Path]] = [(f"{skill.name}/SKILL.md", skill / "SKILL.md")]
    if (skill / "reference.md").is_file():
        files.append((f"{skill.name}/reference.md", skill / "reference.md"))
    if LICENSE.is_file():
        files.append((f"{skill.name}/LICENSE", LICENSE))
    for p in sorted((skill / "scripts").rglob("*")):
        rel = p.relative_to(skill)
        if not p.is_file() or SKIP_DIRS.intersection(rel.parts) or p.name.endswith(SKIP_SUFFIXES):
            continue
        files.append((f"{skill.name}/{rel.as_posix()}", p))
    return sorted(files)


def _info(name: str, mode: int) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=ZIP_DATE)
    zi.create_system = 3                                   # unix, so the mode bits below apply
    zi.external_attr = (mode & 0xFFFF) << 16
    if name.endswith("/"):
        zi.external_attr |= 0x10                           # MS-DOS directory flag
    else:
        zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def build_zip(skill: Path, out_dir: Path) -> Tuple[Path, int]:
    """Write out_dir/<skill>.zip; returns (path, number of files)."""
    files = package_files(skill)
    dirs = sorted({name.rsplit("/", 1)[0] + "/" for name, _ in files})
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{skill.name}.zip"
    tmp = target.with_name(target.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zf:
        for d in dirs:
            zf.writestr(_info(d, 0o40755), b"")
        for name, src in files:
            data = src.read_bytes()
            executable = name.endswith(".py") and data.startswith(b"#!")
            zf.writestr(_info(name, 0o100755 if executable else 0o100644), data)
    tmp.replace(target)
    return target, len(files)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build dist/<skill>.zip for each skill (or --check them).")
    ap.add_argument("skills", nargs="*", help="skill folder names (default: all)")
    ap.add_argument("--check", action="store_true", help="only validate the skills; build nothing")
    ap.add_argument("--out", default=str(DIST), help=f"output folder (default {DIST})")
    args = ap.parse_args(argv)
    try:
        skills = skill_dirs(args.skills)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not skills:
        print(f"error: no skills under {SKILLS}", file=sys.stderr)
        return 1
    bad = 0
    for skill in skills:
        for p in check_skill(skill):
            print(f"{skill.name}: {p}", file=sys.stderr)
            bad += 1
    if bad:
        print(f"{bad} problem(s) found - nothing built" if not args.check else f"{bad} problem(s) found",
              file=sys.stderr)
        return 1
    if args.check:
        print(f"{len(skills)} skill(s) OK")
        return 0
    out = Path(args.out).expanduser()
    for skill in skills:
        path, n = build_zip(skill, out)
        print(f"{path}  ({n} files, {path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
