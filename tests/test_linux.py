import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.platforms import linux


class LinuxServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "server").mkdir()
        (self.root / "server/jev_server.py").touch()
        self.plan = linux.plan_service(self.root, self.root / "codex",
                                        self.root / "state", {"JEV_LISTEN_PORT": "54322"})
        self.plan["definition_path"] = str(self.root / "unit.service")

    def test_unit_has_owner_marker_and_path_only(self):
        unit = linux._render(self.plan)
        self.assertTrue(unit.startswith("# " + linux.MARKER))
        self.assertIn("JEV_LADDER_CONFIG=", unit)
        self.assertNotIn("TYPESAFE_API_KEY=", unit)

    def test_systemd_quotes_paths_with_spaces_using_its_own_syntax(self):
        self.plan["repo"] = "/tmp/agent repo's"
        self.plan["command"] = ["/tmp/python env/bin/python", "/tmp/agent repo/server.py"]
        unit = linux._render(self.plan)
        self.assertIn('WorkingDirectory="/tmp/agent repo\'s"', unit)
        self.assertIn('ExecStart="/tmp/python env/bin/python" "/tmp/agent repo/server.py"', unit)
        self.assertNotIn("'", unit.split("ExecStart=", 1)[1].splitlines()[0])

    def test_systemd_keeps_unicode_and_escapes_literal_percent_and_quotes(self):
        self.plan["repo"] = '/tmp/агент 100% "один"\\проект'
        self.plan["command"] = ["/usr/bin/python3", self.plan["repo"] + "/jev_server.py"]
        self.plan["env_paths"]["JEV_LADDER_CONFIG"] = self.plan["repo"] + "/ladder.json"
        unit = linux._render(self.plan)
        self.assertIn('WorkingDirectory="/tmp/агент 100%% \\"один\\"\\\\проект"', unit)
        self.assertIn('ExecStart="/usr/bin/python3" "/tmp/агент 100%% \\"один\\"\\\\проект/jev_server.py"', unit)
        self.assertIn('Environment="JEV_LADDER_CONFIG=/tmp/агент 100%% \\"один\\"\\\\проект/ladder.json"', unit)
        self.assertNotIn("\\u0430", unit)

    def test_systemd_rejects_nul_in_values(self):
        self.plan["repo"] = "/tmp/bad\0path"
        with self.assertRaisesRegex(ValueError, "cannot contain NUL"):
            linux._render(self.plan)

    def test_router_state_dir_is_bound_to_selected_profile(self):
        with self.assertRaisesRegex(ValueError, "fixed by the selected Codex profile"):
            linux.plan_service(self.root, self.root / "codex", self.root / "state",
                               {"CODEX_ROUTER_STATE_DIR": str(self.root / "other")})

    def test_dry_run_does_not_create_unit(self):
        with patch.object(linux, "_active", return_value=False), patch.object(
                linux, "_port_occupied", return_value=False):
            self.assertTrue(linux.install_service(self.plan)["dry_run"])
        self.assertFalse(Path(self.plan["definition_path"]).exists())

    def test_native_unit_has_no_external_ladder_paths(self):
        plan = linux.plan_service(self.root, self.root / "codex", self.root / "state",
                                  {"JEV_LISTEN_PORT": "54322", "JEV_LADDER_MODE": "native"})
        unit = linux._render(plan)
        self.assertIn("JEV_LADDER_MODE=native", unit)
        self.assertNotIn("JEV_LADDER_CONFIG", unit)
        self.assertNotIn("JEV_OMNIROUTE_AUTH_FILE", unit)

    def test_openrouter_unit_does_not_load_typesafe_key(self):
        plan = linux.plan_service(self.root, self.root / "codex", self.root / "state",
                                  {"JEV_DECISION_PROVIDER": "openrouter",
                                   "JEV_DECISION_KEY_FILE": str(self.root / "jev.key")})
        unit = linux._render(plan)
        self.assertIn("JEV_DECISION_PROVIDER=openrouter", unit)
        self.assertIn("JEV_DECISION_KEY_FILE=", unit)
        self.assertNotIn("TYPESAFE_API_KEY_FILE=", unit)

    def test_foreign_unit_is_refused(self):
        Path(self.plan["definition_path"]).write_text("[Service]\nExecStart=/bin/true\n")
        with patch.object(linux, "_active", return_value=False), patch.object(
                linux, "_port_occupied", return_value=False):
            with self.assertRaises(FileExistsError):
                linux.install_service(self.plan)

    def test_foreign_active_service_is_refused(self):
        with patch.object(linux, "_active", return_value=True), patch.object(
                linux, "_port_occupied", return_value=False):
            with self.assertRaises(RuntimeError):
                linux.install_service(self.plan)

    def test_busy_port_is_refused_even_when_service_inactive(self):
        with patch.object(linux, "_active", return_value=False), patch.object(
                linux, "_port_occupied", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "listener already occupied"):
                linux.install_service(self.plan)

    def test_dry_run_is_idempotent_and_does_not_create_state(self):
        with patch.object(linux, "_active", return_value=False), patch.object(
                linux, "_port_occupied", return_value=False):
            first = linux.install_service(self.plan)
            second = linux.install_service(self.plan)
        self.assertEqual(first["action"], second["action"])
        self.assertFalse(Path(self.plan["state_dir"]).exists())

    def test_state_directory_is_private_and_symlinks_are_rejected(self):
        state = Path(self.plan["state_dir"])
        state.mkdir()
        state.chmod(0o755)
        linux._private_state_dir(state)
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        link = self.root / "state-link"
        link.symlink_to(state, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
            linux._private_state_dir(link)


if __name__ == "__main__":
    unittest.main()
