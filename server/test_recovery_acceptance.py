import unittest
import subprocess
from unittest import mock

import recovery_acceptance as recovery
import jev_server as jev


class RecoveryAcceptanceTests(unittest.TestCase):
    def test_fixture_stream_uses_valid_actual_transport_contract(self):
        raw = recovery.response_stream()
        self.assertIsNone(jev.external_sse_issue(raw))
        assembled = jev.assemble_sse(raw)
        self.assertEqual(assembled["model"], "fixture/model-b")
        self.assertEqual(assembled["status"], "completed")
        self.assertEqual(assembled["output"][0]["content"][0]["text"], recovery.MARKER)

    def test_no_client_does_not_start_servers(self):
        with mock.patch.object(recovery.shutil, "which", return_value=None), \
             mock.patch.object(recovery, "ThreadingHTTPServer") as server:
            self.assertEqual(recovery.run()["status"], "blocked")
        server.assert_not_called()

    def test_private_windows_environment_is_case_insensitive_and_timeout_is_reported(self):
        def timeout(command, *, cwd, env, **kwargs):
            self.assertEqual(env["SYSTEMROOT"], "fixture-system-root")
            for name in ("HOME", "CODEX_HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                self.assertEqual(env[name], env["CODEX_HOME"])
            self.assertNotIn("PRIVATE_PROVIDER_KEY", env)
            self.assertEqual(kwargs["timeout"], 120)
            self.assertIn('approval_policy="never"', command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with mock.patch.dict(recovery.os.environ, {"SYSTEMROOT": "fixture-system-root",
                                                   "PRIVATE_PROVIDER_KEY": "private"}, clear=True), \
             mock.patch.object(recovery, "run_bounded", side_effect=timeout):
            report = recovery.run("fixture-codex")
        self.assertFalse(report["ok"])
        self.assertEqual(report["provider_requests"], 0)
        self.assertIn("timed out", report["reason"])
        self.assertNotIn("private", str(report))
