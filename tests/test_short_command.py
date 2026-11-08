"""Tests for the short command: 'afl' installed next to 'audezeforlinux', 'db', and no command."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from maxwell2 import system

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CLI = os.path.join(REPO, "maxwell_ctl.py")


class InstallShortCommandTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        self.source = os.path.join(self.tmp.name, "audezeforlinux")
        open(self.source, "w").close()
        path = mock.patch.dict(os.environ, {"PATH": self.bin})
        path.start()
        self.addCleanup(path.stop)

    def test_adds_afl_and_is_idempotent(self):
        self.assertIn("✔", system.install_short_command(self.bin, self.source))
        link = os.path.join(self.bin, "afl")
        self.assertEqual(os.path.realpath(link), os.path.realpath(self.source))
        self.assertIn("✔", system.install_short_command(self.bin, self.source))  # already there: fine

    def test_never_replaces_someone_elses_afl(self):
        foreign = os.path.join(self.bin, "afl")
        with open(foreign, "w") as f:
            f.write("#!/bin/sh\n")
        self.assertIn("not added", system.install_short_command(self.bin, self.source))
        with open(foreign) as f:
            self.assertEqual(f.read(), "#!/bin/sh\n")

    def test_skips_when_another_program_on_path_is_called_afl(self):
        elsewhere = os.path.join(self.tmp.name, "usr-bin")
        os.makedirs(elsewhere)
        other = os.path.join(elsewhere, "afl")
        with open(other, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(other, 0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{elsewhere}"}):
            self.assertIn("another program", system.install_short_command(self.bin, self.source))
        self.assertFalse(os.path.lexists(os.path.join(self.bin, "afl")))


class CommandLineTests(unittest.TestCase):

    def run_cli(self, *args):
        return subprocess.run([sys.executable, CLI, *args], cwd=REPO, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)

    def test_db_is_an_alias_for_dashboard(self):
        out = self.run_cli("db", "--help")
        self.assertEqual(out.returncode, 0)
        self.assertIn("usage:", out.stdout)

    def test_no_command_outside_a_terminal_prints_help(self):
        out = self.run_cli()
        self.assertEqual(out.returncode, 2)
        self.assertIn("opens the dashboard", out.stdout)


if __name__ == "__main__":
    unittest.main()
