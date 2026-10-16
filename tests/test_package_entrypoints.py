"""Installer imports must work without test discovery modifying sys.path."""
import subprocess
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class PackageEntrypointTests(unittest.TestCase):
    def test_package_import_and_installer_help_in_clean_processes(self):
        cases = [
            (["-c", "from server.provider_ladder import validate_config; "
                    "from server.routing_policy import JevDecisionError; "
                    "from server.jev_errors import JevDecisionError as Error; "
                    "assert JevDecisionError is Error; print('package imports ok')"],
             "package imports ok"),
            (["-m", "harness.cli", "--help"], "usage:"),
        ]
        for arguments, expected in cases:
            with self.subTest(entrypoint=arguments[0]):
                result = subprocess.run([sys.executable, "-B", "-E", *arguments],
                    cwd=REPO, capture_output=True, text=True, check=False, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertNotIn("Traceback", result.stderr)
