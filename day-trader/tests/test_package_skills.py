"""Tests for tools/package_skills.py (per-skill upload zips + SKILL.md frontmatter checks)."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, SHARED, SKILLS, tc  # noqa: E402

TOOL = ROOT / "tools" / "package_skills.py"
_spec = importlib.util.spec_from_file_location("day_trader_package_skills", TOOL)
pkg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkg)  # type: ignore[union-attr]

ALL_SKILLS = sorted(p.name for p in SKILLS.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def run_tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, timeout=60)


class FrontmatterTests(unittest.TestCase):
    def test_folded_description(self):
        fm = pkg.parse_frontmatter("---\nname: risk\ndescription: >-\n  Day-trading desk: size\n"
                                   "  the trade.\n\n  Second paragraph.\n---\n# Risk\n")
        self.assertEqual(fm["name"], "risk")
        self.assertEqual(fm["description"], "Day-trading desk: size the trade.\nSecond paragraph.")

    def test_plain_quoted_and_literal_values(self):
        fm = pkg.parse_frontmatter("---\nname: 'news'\ndescription: \"Day-trading desk: news\"\n"
                                   "other: |\n  line one\n  line two\n---\n")
        self.assertEqual((fm["name"], fm["description"]), ("news", "Day-trading desk: news"))
        self.assertEqual(fm["other"], "line one\nline two\n")
        fm = pkg.parse_frontmatter("---\nname: exit\ndescription: plain text that\n  continues here\n---\n")
        self.assertEqual(fm["description"], "plain text that continues here")

    def test_malformed_frontmatter_is_an_error(self):
        for text in ("# no frontmatter\n", "---\nname: x\n", "---\njust words\n---\n",
                     "---\nname: a\nname: b\n---\n"):
            with self.assertRaises(ValueError, msg=text):
                pkg.parse_frontmatter(text)

    def test_check_rules(self):
        ok = {"name": "time-machine", "description": "Day-trading desk: replay the session."}
        self.assertEqual(pkg.check_frontmatter("time-machine", ok), [])
        cases = {
            "does not match its folder": ("scanner", ok),
            "lowercase letters": ("Time_Machine", dict(ok, name="Time_Machine")),
            "characters (max 64)": ("a" * 65, dict(ok, name="a" * 65)),
            "reserved word": ("claude-trader", dict(ok, name="claude-trader")),
            "missing 'name'": ("x", {"description": "d"}),
            "missing 'description'": ("risk", {"name": "risk", "description": "  "}),
            "description is 1025 characters": ("risk", {"name": "risk", "description": "x" * 1025}),
            "XML tag": ("risk", {"name": "risk", "description": "use <b>this</b>"}),
        }
        for want, (folder, fields) in cases.items():
            problems = pkg.check_frontmatter(folder, fields)
            self.assertTrue(any(want in p for p in problems), (want, problems))
        self.assertEqual(pkg.check_frontmatter("risk", {"name": "risk", "description": "x" * 1024}), [])
        self.assertEqual(pkg.check_frontmatter("a1-b2", {"name": "a1-b2", "description": "price < 5"}), [])


class RepoSkillsTests(unittest.TestCase):
    def test_every_skill_in_the_pack_passes_the_check(self):
        for skill in pkg.skill_dirs():
            self.assertEqual(pkg.check_skill(skill), [], skill.name)
        self.assertEqual(len(ALL_SKILLS), 11)

    def test_check_cli(self):
        p = run_tool("--check")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("11 skill(s) OK", p.stdout)
        p = run_tool("--check", "no-such-skill")
        self.assertEqual(p.returncode, 1)
        self.assertIn("unknown skill", p.stderr)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "dist"

    def test_builds_one_zip_per_skill_with_a_single_top_folder(self):
        p = run_tool("--out", str(self.out))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(z.stem for z in self.out.glob("*.zip")), ALL_SKILLS)
        shared = SHARED.joinpath("trader_common.py").read_bytes()
        for name in ALL_SKILLS:
            src = SKILLS / name
            with zipfile.ZipFile(self.out / f"{name}.zip") as zf:
                self.assertIsNone(zf.testzip())
                names = zf.namelist()
                self.assertTrue(all(n.startswith(f"{name}/") for n in names), names)
                self.assertIn(f"{name}/SKILL.md", names)
                self.assertIn(f"{name}/LICENSE", names)
                self.assertEqual(f"{name}/reference.md" in names, (src / "reference.md").is_file())
                self.assertEqual(zf.read(f"{name}/scripts/trader_common.py"), shared)
                self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")], names)
                scripts = sorted(f"{name}/scripts/{p.name}" for p in (src / "scripts").glob("*.py"))
                self.assertEqual(sorted(n for n in names if n.endswith(".py")), scripts)
                self.assertEqual(zf.read(f"{name}/SKILL.md"), (src / "SKILL.md").read_bytes())

    def test_zips_are_reproducible(self):
        a, b = self.out / "a", self.out / "b"
        pkg.build_zip(SKILLS / "risk", a)
        pkg.build_zip(SKILLS / "risk", b)
        self.assertEqual((a / "risk.zip").read_bytes(), (b / "risk.zip").read_bytes())

    def test_unzipped_skill_runs_on_its_own(self):
        p = run_tool("risk", "--out", str(self.out))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([z.name for z in self.out.glob("*.zip")], ["risk.zip"])
        unpacked = Path(self.tmp.name) / "installed"
        with zipfile.ZipFile(self.out / "risk.zip") as zf:
            zf.extractall(unpacked)
        script = unpacked / "risk" / "scripts" / "risk.py"
        self.assertTrue(os.access(script, os.R_OK))
        env = dict(os.environ, DAYTRADER_HOME=str(Path(self.tmp.name) / "home"))
        r = subprocess.run([sys.executable, "-I", str(script), "size", "--entry", "364.18", "--stop",
                            "358.32", "--json"], capture_output=True, text=True, env=env, timeout=60,
                           cwd=self.tmp.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["shares"], 8)

    def test_a_bad_skill_blocks_the_build(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "Bad_Skill"
            (bad / "scripts").mkdir(parents=True)
            (bad / "SKILL.md").write_text("---\nname: bad-skill\ndescription: x\n---\n", encoding="utf-8")
            problems = pkg.check_skill(bad)
        self.assertTrue(any("does not match its folder" in p for p in problems), problems)
        self.assertTrue(any("trader_common.py is missing" in p for p in problems), problems)


class PackReadmeTests(unittest.TestCase):
    """Keep README.md honest as the skills change."""

    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_every_skill_is_in_the_table_and_every_script_exists(self):
        for name in ALL_SKILLS:
            self.assertIn(f"| `{name}` |", self.readme)
        mains = {"scan.py": "scanner", "news.py": "news", "entry.py": "entry", "exit_plan.py": "exit",
                 "time_machine.py": "time-machine", "risk.py": "risk", "budget.py": "budget",
                 "broker.py": "broker", "alerts.py": "alerts", "analyst.py": "analyst", "pelosi.py": "pelosi"}
        for path in re.findall(r"python3 (skills/[\w-]+/scripts/\w+\.py|tools/\w+\.py)", self.readme):
            self.assertTrue((ROOT / path).is_file(), path)
        for script in re.findall(r"python3 scripts/(\w+\.py)", self.readme):
            self.assertTrue((SKILLS / mains[script] / "scripts" / script).is_file(), script)

    def test_every_host_the_code_calls_is_listed(self):
        hosts = set(tc.YAHOO_HOSTS)
        for p in [SHARED / "trader_common.py"] + sorted(SKILLS.glob("*/scripts/*.py")):
            if p.name == "trader_common.py" and p.parent != SHARED:
                continue
            hosts.update(h for h in re.findall(r"https://([a-z0-9.-]+\.[a-z]{2,})", p.read_text(encoding="utf-8")))
        self.assertTrue(hosts)
        for h in sorted(hosts):
            self.assertIn(f"`{h}`", self.readme, f"{h} is used by the code but not in README's network table")

    def test_replay_row_says_what_a_replay_cannot_cut(self):
        # $DAYTRADER_NOW drops bars that start later, but the bar in progress and the day's daily bar
        # keep whatever the data has (a replay of data fetched later can show those later prices).
        row = next(ln for ln in self.readme.splitlines() if ln.startswith("| `DAYTRADER_NOW` |"))
        for phrase in ("bars that start after it are ignored", "the bar in progress at that time is used as the data has it",
                       "`since trigger`", "daily bar can't be cut", "WARNING: replay"):
            self.assertIn(phrase, row)


if __name__ == "__main__":
    unittest.main()
