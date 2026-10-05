import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from harness import acceptance, core
from scripts import smoke_reasoning_live as smoke


class AcceptanceTests(unittest.TestCase):
    def test_default_does_not_make_requests_or_start_client(self):
        with mock.patch.object(acceptance.subprocess, "run") as process, \
             mock.patch.object(acceptance.live_verify, "verify_live") as live:
            report = acceptance.run(Path("."), Path("missing"))
        process.assert_not_called()
        live.assert_not_called()
        self.assertEqual(report["live"]["status"], "not_run")

    def test_bad_live_inputs_do_not_spend_a_request(self):
        with mock.patch.object(acceptance.live_verify, "verify_decision") as request:
            with self.assertRaises(core.InstallError):
                acceptance.run(Path("."), Path("missing"), live=True)
        request.assert_not_called()

    def test_live_report_requires_fresh_matching_route_and_separates_enforcement(self):
        with tempfile.TemporaryDirectory() as root:
            profile = Path(root)
            auth = profile / "fixture-auth.json"
            auth.write_text('{"omniroute":{"key":"fixture"}}')
            auth.chmod(0o600)
            state = profile / "jev-harness"
            state.mkdir()
            (state / "ladder-config.json").write_text('{"reasoning_profiles":{}}')
            evidence = {"found": True, "status": 200, "attempts": [{"terminal_type": "response.completed"}]}
            probe = {"status": "PASS", "effective_effort": "low", "provider_control": "NOT_VERIFIED"}
            with mock.patch.object(core, "protected_file", return_value=auth), \
                 mock.patch.object(smoke, "protected_file", return_value=auth), \
                 mock.patch.object(core, "verify", return_value={k: True for k in
                     ("files", "client_config", "model_preserved", "service_health")}), \
                 mock.patch.object(acceptance.live_verify, "verify_decision", return_value={"ok": True}), \
                 mock.patch.object(acceptance.live_verify, "verify_live", return_value={"ok": True, "cache_scope": "fresh"}), \
                 mock.patch.object(acceptance.live_verify, "verify_manual", return_value={"ok": True}), \
                 mock.patch.object(acceptance.explain, "latest", return_value=evidence) as explain, \
                 mock.patch.object(smoke, "run_probe", return_value=probe) as request:
                report = acceptance.run(profile, profile, live=True, manual_model="gpt-6-sol",
                                        gateway_model="fixture/model", auth=auth)
                self.assertTrue(report["live"]["ok"])
                self.assertEqual(request.call_count, 4)
                self.assertEqual([call.args[3] for call in request.call_args_list], ["low", "low", "high", "high"])
                self.assertEqual(report["reasoning_enforcement"], "unknown")
                self.assertNotIn('"key"', json.dumps(report))
                explain.assert_called_with(profile.resolve(), "fresh")
                evidence["found"] = False
                self.assertFalse(acceptance.run(profile, profile, live=True, manual_model="gpt-6-sol",
                                               gateway_model="fixture/model", auth=auth)["live"]["ok"])

    def test_unprotected_auth_is_rejected_before_first_model_request(self):
        with mock.patch.object(core, "protected_file", side_effect=core.InstallError("shared auth")), \
             mock.patch.object(acceptance.live_verify, "verify_decision") as request:
            with self.assertRaisesRegex(core.InstallError, "shared auth"):
                acceptance.run(Path("."), Path("missing"), live=True,
                               manual_model="gpt-6-sol", gateway_model="fixture/model", auth=Path("fixture"))
        request.assert_not_called()

    def test_recovery_runner_rejects_bad_report_and_does_not_echo_stderr(self):
        with mock.patch.object(acceptance.subprocess, "run", return_value=mock.Mock(
                returncode=0, stdout='{"ok":false}', stderr="secret")):
            report = acceptance.recovery(Path("."))
        self.assertFalse(report["ok"])
        self.assertNotIn("secret", str(report))
