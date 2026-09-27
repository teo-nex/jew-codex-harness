import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.platforms import windows


class WindowsServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "server").mkdir()
        (self.root / "server/jev_server.py").touch()
        self.plan = windows.plan_service(self.root, self.root / "codex",
                                          self.root / "state", {"JEV_LISTEN_PORT": "54323"})

    def test_task_and_wrapper_contain_no_secret_values(self):
        task = windows._render(self.plan)
        self.assertFalse(task.startswith("<?xml"))
        wrapper = windows._wrapper(self.plan)
        self.assertTrue(windows._owned_xml(task))
        self.assertIn("run-router.py", task)
        self.assertIn("JEV_LADDER_CONFIG", wrapper)
        self.assertNotIn("TYPESAFE_API_KEY\"", wrapper)

    def test_task_binds_current_user_and_exact_action(self):
        task = windows._render(self.plan)
        self.assertIn(self.plan["owner_sid"], task)
        self.assertTrue(windows._owned_xml(task, self.plan))
        altered = dict(self.plan, command=["C:\\foreign\\python.exe", self.plan["command"][1]])
        self.assertFalse(windows._owned_xml(task, altered))

    def test_router_state_dir_is_bound_to_selected_profile(self):
        with self.assertRaisesRegex(ValueError, "fixed by the selected Codex profile"):
            windows.plan_service(self.root, self.root / "codex", self.root / "state",
                                 {"CODEX_ROUTER_STATE_DIR": str(self.root / "other")})

    def test_dry_run_does_not_create_files(self):
        with patch.object(windows, "service_status", return_value={
            "installed": False, "owned": False, "active": False,
            "port_occupied": False, "service_id": windows.SERVICE_ID}):
            self.assertTrue(windows.install_service(self.plan)["dry_run"])
        self.assertFalse(Path(self.plan["definition_path"]).exists())

    def test_foreign_task_is_refused(self):
        with patch.object(windows, "service_status", return_value={
            "installed": True, "owned": False, "active": False,
            "port_occupied": False, "service_id": windows.SERVICE_ID}):
            with self.assertRaises(FileExistsError):
                windows.install_service(self.plan)

    def test_port_conflict_is_refused(self):
        with patch.object(windows, "service_status", return_value={
            "installed": False, "owned": False, "active": False,
            "port_occupied": True, "service_id": windows.SERVICE_ID}):
            with self.assertRaises(RuntimeError):
                windows.install_service(self.plan)

    def test_repeated_dry_run_is_idempotent(self):
        with patch.object(windows, "service_status", return_value={
                "installed": False, "owned": False, "active": False,
                "port_occupied": False, "service_id": windows.SERVICE_ID}):
            first = windows.install_service(self.plan)
            second = windows.install_service(self.plan)
        self.assertEqual(first["action"], second["action"])
        self.assertFalse(Path(self.plan["state_dir"]).exists())

    def test_native_wrapper_has_no_external_ladder_paths(self):
        plan = windows.plan_service(self.root, self.root / "codex", self.root / "state",
                                    {"JEV_LISTEN_PORT": "54323", "JEV_LADDER_MODE": "native"})
        wrapper = windows._wrapper(plan)
        self.assertIn("JEV_LADDER_MODE", wrapper)
        self.assertNotIn("JEV_LADDER_CONFIG", wrapper)
        self.assertNotIn("JEV_OMNIROUTE_AUTH_FILE", wrapper)

    def test_openrouter_wrapper_uses_generic_key_path(self):
        plan = windows.plan_service(self.root, self.root / "codex", self.root / "state",
                                    {"JEV_DECISION_PROVIDER": "openrouter",
                                     "JEV_DECISION_KEY_FILE": str(self.root / "jev.key")})
        wrapper = windows._wrapper(plan)
        self.assertIn("JEV_DECISION_KEY_FILE", wrapper)
        self.assertNotIn("TYPESAFE_API_KEY_FILE", wrapper)

    def test_foreign_local_artifact_is_not_overwritten(self):
        Path(self.plan["wrapper_path"]).parent.mkdir(parents=True)
        Path(self.plan["wrapper_path"]).write_text("print('foreign')")
        with patch.object(windows, "service_status", return_value={
                "installed": False, "owned": False, "active": False,
                "port_occupied": False, "service_id": windows.SERVICE_ID}), patch.object(
                windows, "_private_state_dir"):
            with self.assertRaises(FileExistsError):
                windows.install_service(self.plan, dry_run=False)

    def test_state_acl_is_applied_to_the_exact_directory(self):
        state = Path(self.plan["state_dir"])
        state.mkdir()
        with patch.object(windows.subprocess, "run", return_value=type(
                "Result", (), {"returncode": 0})()) as run:
            windows._private_state_dir(state)
        self.assertEqual(run.call_args.kwargs["env"]["JEV_STATE_PATH"], str(state))
        self.assertIn("SetAccessRuleProtection", run.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()
