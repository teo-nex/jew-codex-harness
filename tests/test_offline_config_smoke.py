"""Offline preparation must not start a host service or accept missing outputs."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from scripts import smoke_config_offline as smoke


class OfflineConfigurationSmokeTests(unittest.TestCase):
    def test_unused_provider_is_staged_before_explicit_model_publication(self):
        seen = []

        def configure(command, *, cwd, env, capture_output, timeout):
            script = Path(command[1]).name
            seen.append((script, list(command[2:])))
            home = Path(env["CODEX_HOME"])
            state = Path(env["CODEX_ROUTER_STATE_DIR"])
            self.assertEqual(state, home / "codex-router")
            self.assertTrue(capture_output)
            self.assertEqual(timeout, 45)
            if script == "providers.mjs":
                self.assertIn("--no-apply", command)
            elif script == "configure-model.mjs":
                (state / "user-models.json").write_text(
                    json.dumps({"models": [{"slug": "jev/auto"}]}), encoding="utf-8"
                )
            elif script == "configure-auth.mjs":
                key = state / "generic-provider-credentials/jev.key"
                key.parent.mkdir(parents=True)
                key.write_text("synthetic-local-transport-key", encoding="utf-8")
                key.chmod(0o600)
            return subprocess.CompletedProcess(command, 0, stdout=b"{}", stderr=b"")

        output = io.StringIO()
        with mock.patch.object(smoke.subprocess, "run", side_effect=configure), contextlib.redirect_stdout(output):
            smoke.main()
        self.assertEqual([name for name, _ in seen], [
            "providers.mjs", "providers.mjs", "configure-model.mjs",
            "configure-auth.mjs", "refresh-catalog.mjs", "control.mjs",
        ])
        self.assertEqual(seen[0][1][:3], ["generic", "add", "jev"])
        self.assertEqual(seen[1][1][:3], ["generic", "enable", "jev"])
        self.assertEqual(json.loads(output.getvalue()), {
            "ok": True,
            "steps": ["provider_add", "provider_enable", "model", "auth", "catalog", "picker"],
            "isolated": True,
        })

    def test_successful_commands_without_output_are_not_a_successful_smoke(self):
        result = subprocess.CompletedProcess([], 0, stdout=b"{}", stderr=b"")
        with mock.patch.object(smoke.subprocess, "run", return_value=result), self.assertRaises(FileNotFoundError):
            smoke.main()

    def test_failed_staging_command_still_fails_the_smoke(self):
        result = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"synthetic-configuration-failure")
        with mock.patch.object(smoke.subprocess, "run", return_value=result), self.assertRaisesRegex(
            RuntimeError, "provider_add exited 1"
        ):
            smoke.main()


if __name__ == "__main__":
    unittest.main()
