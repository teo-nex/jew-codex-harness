"""Run the public PowerShell entry point with controlled Python discovery."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
class WindowsLauncherTests(unittest.TestCase):
    def run_launcher(self, commands, *args):
        with tempfile.TemporaryDirectory(prefix="jev launcher ") as directory:
            root = Path(directory)
            for name, body in commands.items():
                (root / (name + ".cmd")).write_text("@echo off\n" + body + "\n", encoding="utf-8")
            env = {**os.environ, "PATH": str(root), "PYLAUNCHER_ALLOW_INSTALL": ""}
            powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            return subprocess.run(
                [str(powershell), "-NoProfile", "-File", str(REPO / "install.ps1"), *args],
                cwd=root, env=env, capture_output=True, text=True, timeout=30,
            )

    def forward_python(self):
        return f'"{sys.executable}" %*\nexit /b %errorlevel%'

    def test_python_on_path_without_py_preserves_arguments_and_exit_code(self):
        result = self.run_launcher({"python": self.forward_python()}, "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Install Jev Codex Harness", result.stdout)
        result = self.run_launcher({"python": self.forward_python()}, "not-a-command")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("invalid choice", result.stderr)

    def test_py_only_and_python3_only(self):
        for name in ("py", "python3"):
            with self.subTest(command=name):
                # py consumes its interpreter selector before forwarding.
                body = self.forward_python() if name != "py" else (
                    f'"{sys.executable}" %2 %3 %4 %5\nexit /b %errorlevel%')
                result = self.run_launcher({name: body}, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Install Jev Codex Harness", result.stdout)

    def test_incompatible_python_does_not_hide_working_python3(self):
        result = self.run_launcher({"python": "exit /b 3", "python3": self.forward_python()}, "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Install Jev Codex Harness", result.stdout)

    def test_old_py_default_falls_back_to_explicit_version(self):
        body = ('if "%1"=="-3" exit /b 3\n'
                f'"{sys.executable}" %2 %3 %4 %5\nexit /b %errorlevel%')
        result = self.run_launcher({"py": body}, "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Install Jev Codex Harness", result.stdout)

    def test_missing_python_has_actionable_error(self):
        result = self.run_launcher({}, "--help")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.11+ is required", result.stderr)
