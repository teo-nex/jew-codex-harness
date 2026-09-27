import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from harness import cli


class CLITests(unittest.TestCase):
    def test_doctor_exit_code_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with mock.patch.object(cli.core, "doctor", return_value={"ready": False, "issues": ["missing"]}), redirect_stdout(output):
                code = cli.main(["--codex-home", directory, "doctor"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["issues"], ["missing"])

    def test_openrouter_never_inherits_ambient_typesafe_key_path(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY_FILE": "/private/typesafe.key"}, clear=True), \
             mock.patch.object(cli.core, "doctor", return_value={"ready": False, "issues": []}) as doctor, \
             redirect_stdout(io.StringIO()):
            cli.main(["--jev-provider", "openrouter", "doctor"])
        self.assertIsNone(doctor.call_args.args[4])
        self.assertEqual(doctor.call_args.kwargs["jev_provider"], "openrouter")

    def test_onboard_requires_a_terminal(self):
        output = io.StringIO()
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False), \
             redirect_stderr(output):
            code = cli.main(["onboard"])
        self.assertEqual(code, 2)
        self.assertIn("interactive terminal", json.loads(output.getvalue())["error"])

    def test_onboard_reports_installed_with_hook_trust_still_pending(self):
        output = io.StringIO()
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
             mock.patch.object(cli.onboard, "run", return_value={
                 "installed": True, "ready": False, "status": "hook_trust_pending"}), \
             redirect_stdout(output):
            code = cli.main(["onboard"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())["ready"])

    def test_install_dry_run_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with mock.patch.object(cli.core, "install", return_value={"dry_run": True}) as install, redirect_stdout(output):
                code = cli.main(["--codex-home", directory, "install", "--dry-run"])
            self.assertEqual(code, 0)
            self.assertTrue(install.call_args.kwargs["dry_run"])

    def test_resume_forwards_reviewed_auth_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            digest = "a" * 64
            output = io.StringIO()
            with mock.patch.object(cli.core, "resume", return_value={"installed": True}) as resume, redirect_stdout(output):
                code = cli.main(["--codex-home", directory, "resume", "--reviewed-auth-sha256", digest])
            self.assertEqual(code, 0)
            self.assertEqual(resume.call_args.kwargs["reviewed_auth_sha256"], digest)
            self.assertTrue(json.loads(output.getvalue())["installed"])

    def test_error_is_nonzero_and_json(self):
        output = io.StringIO()
        with mock.patch.object(cli.core, "rollback", side_effect=cli.core.InstallError("not owned")), redirect_stderr(output):
            code = cli.main(["rollback"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"], "not owned")

    def test_plain_verify_does_not_make_provider_call(self):
        result = {"files": True, "client_config": True, "model_preserved": True, "service_health": True}
        output = io.StringIO()
        with mock.patch.object(cli.core, "verify", return_value=result), \
             mock.patch.object(cli.live_verify, "verify_live") as live, redirect_stdout(output):
            code = cli.main(["--codex-home", "/tmp/profile", "verify"])
        self.assertEqual(code, 0)
        live.assert_not_called()

    def test_live_verify_reports_separate_failure_and_nonzero(self):
        core_result = {"files": True, "client_config": True, "model_preserved": True, "service_health": True}
        output = io.StringIO()
        with mock.patch.object(cli.core, "verify", return_value=core_result), \
             mock.patch.object(cli.live_verify, "verify_decision", return_value={"ok": True, "status": "passed"}), \
             mock.patch.object(cli.live_verify, "verify_live", return_value={
                 "ok": False, "status": "failed", "reason": "synthetic failure"}) as live, \
             redirect_stdout(output):
            code = cli.main(["--codex-home", "/tmp/profile", "verify", "--live"])
        self.assertEqual(code, 2)
        live.assert_called_once_with(Path("/tmp/profile"))
        self.assertEqual(json.loads(output.getvalue())["live"]["status"], "failed")

    def test_manual_model_failure_is_reported_separately(self):
        core_result = {"files": True, "client_config": True,
                       "model_preserved": True, "service_health": True}
        output = io.StringIO()
        with mock.patch.object(cli.core, "verify", return_value=core_result), \
             mock.patch.object(cli.live_verify, "verify_decision", return_value={"ok": True}), \
             mock.patch.object(cli.live_verify, "verify_live", return_value={"ok": True}), \
             mock.patch.object(cli.live_verify, "verify_cost_telemetry", return_value={"ok": True}), \
             mock.patch.object(cli.live_verify, "verify_manual", return_value={"ok": False, "reason": "fixture failed"}) as manual, \
             redirect_stdout(output):
            code = cli.main(["verify", "--live", "--manual-model", "gpt-6-luna"])
        self.assertEqual(code, 2)
        manual.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())["manual_model"]["reason"], "fixture failed")

    def test_live_verify_fails_when_cost_usage_cannot_be_priced(self):
        core_result = {"files": True, "client_config": True,
                       "model_preserved": True, "service_health": True}
        output = io.StringIO()
        with mock.patch.object(cli.core, "verify", return_value=core_result), \
             mock.patch.object(cli.live_verify, "verify_decision", return_value={"ok": True}), \
             mock.patch.object(cli.live_verify, "verify_live", return_value={"ok": True}), \
             mock.patch.object(cli.live_verify, "verify_cost_telemetry", return_value={
                 "ok": False, "status": "unknown", "priced_attempts": 0}), redirect_stdout(output):
            code = cli.main(["verify", "--live"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["cost_telemetry"]["priced_attempts"], 0)

    def test_live_verify_does_not_call_provider_after_local_failure(self):
        core_result = {"files": True, "client_config": False,
                       "model_preserved": True, "service_health": True}
        output = io.StringIO()
        with mock.patch.object(cli.core, "verify", return_value=core_result), \
             mock.patch.object(cli.live_verify, "verify_decision") as decision, \
             mock.patch.object(cli.live_verify, "verify_live") as live, redirect_stdout(output):
            code = cli.main(["--codex-home", "/tmp/profile", "verify", "--live"])
        self.assertEqual(code, 2)
        decision.assert_not_called()
        live.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["live"]["status"], "not_run")


if __name__ == "__main__":
    unittest.main()
