import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.platforms import macos


class MacServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "server").mkdir()
        (self.root / "server/jev_server.py").touch()
        self.plan = macos.plan_service(self.root, self.root / "codex",
                                        self.root / "state", {"JEV_LISTEN_PORT": "54321"})
        self.plan["definition_path"] = str(self.root / "job.plist")

    def test_dry_run_does_not_create_service(self):
        with patch.object(macos, "_active", return_value=False), patch.object(
                macos, "_port_occupied", return_value=False):
            result = macos.install_service(self.plan)
        self.assertEqual(result["action"], "install_and_start")
        self.assertFalse(Path(self.plan["definition_path"]).exists())

    def test_foreign_definition_is_refused(self):
        Path(self.plan["definition_path"]).write_bytes(plistlib.dumps({"Label": macos.SERVICE_ID}))
        with patch.object(macos, "_active", return_value=False), patch.object(
                macos, "_port_occupied", return_value=False):
            with self.assertRaises(FileExistsError):
                macos.install_service(self.plan)

    def test_port_conflict_is_refused(self):
        with patch.object(macos, "_active", return_value=False), patch.object(
                macos, "_port_occupied", return_value=True):
            with self.assertRaises(RuntimeError):
                macos.install_service(self.plan)

    def test_shared_state_directory_is_refused(self):
        state = Path(self.plan["state_dir"])
        state.mkdir()
        state.chmod(0o755)
        with patch.object(macos, "_active", return_value=False), patch.object(
                macos, "_port_occupied", return_value=False):
            with self.assertRaises(PermissionError):
                macos.install_service(self.plan)

    def test_secret_value_is_refused(self):
        with self.assertRaises(ValueError):
            macos.plan_service(self.root, self.root / "codex", self.root / "state",
                               {"TYPESAFE_API_KEY": "secret"})

    def test_native_service_has_no_external_ladder(self):
        plan = macos.plan_service(self.root, self.root / "codex", self.root / "state",
                                  {"JEV_LISTEN_PORT": "54321", "JEV_LADDER_MODE": "native"})
        self.assertEqual(plan["env_paths"]["JEV_LADDER_MODE"], "native")
        self.assertNotIn("JEV_LADDER_CONFIG", plan["env_paths"])
        self.assertNotIn("JEV_OMNIROUTE_AUTH_FILE", plan["env_paths"])

    def test_openrouter_service_uses_only_generic_key_path(self):
        plan = macos.plan_service(self.root, self.root / "codex", self.root / "state",
                                  {"JEV_DECISION_PROVIDER": "openrouter",
                                   "JEV_DECISION_KEY_FILE": str(self.root / "jev.key")})
        self.assertIn("JEV_DECISION_KEY_FILE", plan["env_paths"])
        self.assertNotIn("TYPESAFE_API_KEY_FILE", plan["env_paths"])


if __name__ == "__main__":
    unittest.main()
