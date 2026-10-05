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

    def test_servers_stop_before_directory_cleanup_and_global_restoration(self):
        original_directory = recovery.tempfile.TemporaryDirectory
        original_close = recovery.close_servers
        stopped = []

        def close(servers, threads):
            if servers:
                self.assertEqual(jev.STATE, str(root[0] / "state"))
                original_close(servers, threads)
                stopped.append(True)

        root = []
        class Directory:
            def __init__(self, **kwargs):
                self.directory = original_directory(**kwargs)
            def __enter__(self):
                value = self.directory.__enter__()
                root.append(recovery.Path(value))
                return value
            def __exit__(inner, *args):
                self.assertTrue(stopped, "servers must stop before their files are removed")
                return inner.directory.__exit__(*args)

        with mock.patch.object(recovery.tempfile, "TemporaryDirectory", Directory), \
             mock.patch.object(recovery, "close_servers", side_effect=close), \
             mock.patch.object(recovery, "run_bounded", return_value=mock.Mock(returncode=1, stdout="")):
            report = recovery.run("fixture-codex")
        self.assertFalse(report["ok"])
        self.assertEqual(stopped, [True])

    def test_partial_server_startup_closes_without_waiting_for_unstarted_server(self):
        server = mock.Mock()
        with mock.patch.object(recovery, "FixtureServer", side_effect=[server, OSError(13, "private detail")]):
            report = recovery.run("fixture-codex")
        self.assertEqual(report["stage"], "setup")
        self.assertEqual(report["error_errno"], 13)
        self.assertNotIn("private", str(report))
        server.stop.assert_not_called()
        server.server_close.assert_called_once()
