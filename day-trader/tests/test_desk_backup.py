import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT  # noqa: E402


def tool(name, *args, home):
    env = dict(os.environ, DAYTRADER_HOME=str(home))
    return subprocess.run([sys.executable, str(ROOT / "tools" / name), *map(str, args)],
                          capture_output=True, text=True, env=env)


BACKUP = {"saved_at": "2026-10-31T12:00:00Z", "settings": {"daily_max_loss": 1000.0},
          "ledger": {"tickets": [], "trades": []}, "alerts": {"alerts": [{"id": "ALR-1"}]},
          "desk_log": [{"at": "2026-10-31T09:00:00Z", "event": "Desk opened", "detail": ""}],
          "plans": {"BTC-USDT.json": {"symbol": "BTC-USDT"}}, "candidates": [], "note": ""}


class RestoreGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backup = self.root / "backup.json"
        self.backup.write_text(json.dumps({"id": "desk_state", "data": BACKUP, "version": 5}))

    def test_restore_refuses_any_existing_desk_file(self):
        for name, content in (("ledger.json", {"tickets": [], "trades": []}), ("alerts.json", {"alerts": []}),
                              ("desk_log.jsonl", None), ("plans/SOL-USDT.json", {"symbol": "SOL-USDT"})):
            with self.subTest(name=name):
                home = self.root / name.replace("/", "_")
                (home / "plans").mkdir(parents=True)
                path = home / name
                path.write_text(json.dumps(content) if content is not None else '{"at": "x", "event": "e", "detail": ""}\n')
                r = tool("desk_restore.py", self.backup, home=home)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("already holds a desk", r.stderr)
                self.assertTrue(path.exists())
                self.assertFalse((home / "settings.json").exists())   # nothing was written

    def test_restore_into_an_empty_home_and_force(self):
        home = self.root / "fresh"
        (home / "plans").mkdir(parents=True)   # an empty plans/ folder is not a desk
        (home / "cache").mkdir()
        r = tool("desk_restore.py", self.backup, home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads((home / "settings.json").read_text())["daily_max_loss"], 1000.0)
        r = tool("desk_restore.py", self.backup, "--force", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)


class BackupGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def test_backup_refuses_a_home_that_was_never_restored(self):
        # a recycled container where only a limit was changed: settings.json alone, no log
        (self.home / "settings.json").write_text(json.dumps({"daily_max_loss": 800.0}))
        r = tool("desk_backup.py", "--out", self.home / "b.json", home=self.home)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("never restored", r.stderr)
        self.assertFalse((self.home / "b.json").exists())

    def test_backup_with_a_log(self):
        (self.home / "settings.json").write_text(json.dumps({"daily_max_loss": 800.0}))
        (self.home / "desk_log.jsonl").write_text(json.dumps({"at": "x", "event": "Desk opened", "detail": ""}) + "\n")
        r = tool("desk_backup.py", "--out", self.home / "b.json", home=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads((self.home / "b.json").read_text())["desk_log"][0]["event"], "Desk opened")


if __name__ == "__main__":
    unittest.main()
