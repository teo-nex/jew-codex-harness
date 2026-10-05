from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from harness import process
from harness.process import run_bounded


class BoundedProcessTests(unittest.TestCase):
    def test_stdin_is_closed_and_both_outputs_are_captured(self):
        result = run_bounded([sys.executable, "-c", "import sys; "
                              "assert sys.stdin.read() == ''; print('stdout'); "
                              "print('stderr', file=sys.stderr)"], timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "stdout")
        self.assertEqual(result.stderr.strip(), "stderr")

    def test_timeout_stops_descendant_before_it_can_write(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "late-write"
            ready = Path(root) / "ready"
            child = "import time; from pathlib import Path; time.sleep(3); Path(" + repr(str(marker)) + ").touch()"
            parent = ("import subprocess, sys, time; from pathlib import Path; "
                      "subprocess.Popen([sys.executable, '-c', " + repr(child) + "]); "
                      "Path(" + repr(str(ready)) + ").touch(); time.sleep(30)")
            start = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded([sys.executable, "-c", parent], timeout=1)
            self.assertLess(time.monotonic() - start, 9)
            self.assertTrue(ready.is_file(), "positive control: descendant was spawned")
            time.sleep(3)
            self.assertFalse(marker.exists())

    def test_finished_launcher_does_not_wait_for_descendant_pipe_eof(self):
        # File capture must return even while an inherited-handle child runs.
        start = time.monotonic()
        result = run_bounded([sys.executable, "-c", "import subprocess, sys; "
                              "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)']); "
                              "print('done')"], timeout=1)
        self.assertEqual(result.returncode, 0)
        self.assertIn("done", result.stdout)
        self.assertLess(time.monotonic() - start, 1.5)
        time.sleep(2.1)

    def test_nonzero_status_is_not_treated_as_success(self):
        result = run_bounded([sys.executable, "-c", "raise SystemExit(7)"], timeout=5)
        self.assertEqual(result.returncode, 7)

    def test_oversized_capture_fails_without_echoing_output(self):
        with mock.patch.object(process, "OUTPUT_LIMIT", 64):
            with self.assertRaisesRegex(OSError, "capture limit") as error:
                run_bounded([sys.executable, "-c", "print('private-output-' * 20)"], timeout=5)
        self.assertNotIn("private-output", str(error.exception))
