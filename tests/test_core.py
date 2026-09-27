import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import core
from harness.agent_config import merge_agents


REPO = Path(__file__).resolve().parents[1]
LADDER = {"plus_connection_id": "plus", "gemini_connection_ids": ["g1"],
          "gemini_model": "antigravity/gemini", "glm_model": "gonkagate/glm",
          "deepseek_model": "gonkagate/deepseek", "main_model": "gpt-6-luna"}


class InstallCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        macos = core.platform_module("macos")
        platform_patch = mock.patch.object(core, "platform_module", return_value=macos)
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        self.root = Path(self.temp.name)
        if os.name == "nt":
            from harness.platforms.windows import _private_state_dir
            _private_state_dir(self.root, create=False)
            # These tests exercise the macOS adapter's installer state machine
            # on every host. ACL behavior is covered by the Windows adapter;
            # a mocked subprocess in this fixture cannot validate real ACLs.
            acl_patch = mock.patch.object(core, "protected_file", side_effect=lambda path, _label: Path(path).resolve(strict=True))
            acl_patch.start()
            self.addCleanup(acl_patch.stop)
        self.home = self.root / "codex"
        self.ladder = self.root / "ladder.json"
        self.omni = self.root / "omni.json"
        self.key = self.root / "typesafe.key"
        self.ladder.write_text(json.dumps(LADDER))
        self.omni.write_text("{}")
        self.key.write_text("synthetic")
        for path in (self.ladder, self.omni, self.key):
            path.chmod(0o600)

    def _doctor(self):
        with mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.shutil, "which", return_value="/fake/node"), \
             mock.patch.object(core.subprocess, "run", return_value=mock.Mock(stdout="v22.22.3")), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}):
            return core.doctor(REPO, self.home, self.ladder, self.omni, self.key, 4321, "macos")

    def test_doctor_accepts_clean_profile(self):
        self.assertTrue(self._doctor()["ready"])

    def test_prepare_overrides_inherited_router_identity(self):
        inherited = {"MODEL_ROUTER_STATE_DIR": str(self.root / "foreign-state"),
                     "MODEL_ROUTER_TARGET": "claude"}
        with mock.patch.dict(os.environ, inherited), mock.patch.object(core.subprocess, "run") as run:
            core.prepare(REPO, self.home, "macos")
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["MODEL_ROUTER_STATE_DIR"], env["CODEX_ROUTER_STATE_DIR"])
        self.assertEqual(env["MODEL_ROUTER_TARGET"], "codex")
        self.assertTrue(Path(env["MODEL_ROUTER_STATE_DIR"]).is_relative_to(Path(env["CODEX_HOME"])))

    def test_service_environment_selects_the_profile_on_every_platform(self):
        from harness.platforms import linux, macos, windows
        from server.local_runtime import resolve_runtime_paths
        inherited = {"MODEL_ROUTER_STATE_DIR": str(self.root / "foreign-state"),
                     "MODEL_ROUTER_TARGET": "claude"}
        for module in (linux, macos, windows):
            with self.subTest(platform=module.__name__):
                env = {**inherited, **module._environment(self.home, self.home / "jev-harness", {})}
                self.assertEqual(resolve_runtime_paths(env)[2], str(self.home / "codex-router"))
                self.assertEqual(env["MODEL_ROUTER_TARGET"], "codex")

    def test_doctor_accepts_native_mode_without_external_gateway(self):
        with mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.shutil, "which", return_value="/fake/node"), \
             mock.patch.object(core.subprocess, "run", return_value=mock.Mock(stdout="v22.22.3")), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}):
            report = core.doctor(REPO, self.home, None, None, self.key, 4321, "macos")
        self.assertTrue(report["ready"])

    def test_doctor_rejects_half_configured_external_gateway(self):
        with mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.shutil, "which", return_value="/fake/node"), \
             mock.patch.object(core.subprocess, "run", return_value=mock.Mock(stdout="v22.22.3")):
            report = core.doctor(REPO, self.home, self.ladder, None, self.key, 4321, "macos")
        self.assertFalse(report["ready"])
        self.assertIn("both ladder config and OmniRoute auth", " ".join(report["issues"]))

    def test_doctor_refuses_existing_profile(self):
        self.home.mkdir()
        (self.home / "config.toml").write_text('model = "gpt-6-sol"\n')
        self.assertFalse(self._doctor()["ready"])
        self.assertIn("reviewed migration", " ".join(self._doctor()["issues"]))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits do not validate Windows ACLs")
    def test_doctor_never_displays_secret(self):
        self.key.chmod(0o644)
        report = self._doctor()
        self.assertFalse(report["ready"])
        self.assertNotIn("synthetic", json.dumps(report))

    def test_merge_config_preserves_model_and_other_mcp(self):
        source = 'model = "gpt-6-sol"\n[mcp_servers.other]\ncommand = "other"\n'
        result = core.merge_config(source, REPO)
        self.assertIn('model = "gpt-6-sol"', result)
        self.assertIn('[mcp_servers.other]', result)
        self.assertEqual(core.merge_config(result, REPO), result)

    def test_merge_config_refuses_foreign_jev_worker(self):
        with self.assertRaisesRegex(core.InstallError, "migration"):
            core.merge_config('[mcp_servers.jev-workers]\ncommand = "foreign"\n', REPO)

    def test_merge_hooks_preserves_unrelated_and_is_idempotent(self):
        original = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "other"}]}]}}
        once = core.merge_hooks(original, REPO)
        twice = core.merge_hooks(once, REPO)
        self.assertEqual(once, twice)
        self.assertEqual(once["hooks"]["PreToolUse"][0], original["hooks"]["PreToolUse"][0])

    def test_dry_run_does_not_touch_profile(self):
        with mock.patch.object(core, "doctor", return_value={"ready": True, "issues": []}), \
             mock.patch("harness.platforms.macos.install_service", return_value={"action": "install"}) as service:
            result = core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, True, "macos")
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.home.exists())
        service.assert_called_once()
        self.assertTrue(service.call_args.kwargs["dry_run"])

    def test_invalid_ladder_refused_before_writes(self):
        self.ladder.write_text("{}")
        self.assertFalse(self._doctor()["ready"])
        self.assertFalse(self.home.exists())

    def test_credentials_and_profile_inside_source_are_refused(self):
        with self.assertRaisesRegex(core.InstallError, "outside the source"):
            core.inputs(REPO, REPO / "generated-profile", self.ladder, self.omni, self.key, 4321, "macos")
        with mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.shutil, "which", return_value="/fake/node"), \
             mock.patch.object(core.subprocess, "run", return_value=mock.Mock(stdout="v22.22.3")):
            report = core.doctor(REPO, self.home, REPO / "config/ladder.example.json",
                                 self.omni, self.key, 4321, "macos")
        self.assertFalse(report["ready"])
        self.assertIn("outside the source repository", " ".join(report["issues"]))

    def test_fresh_install_records_owned_files_and_preserves_model(self):
        with mock.patch.dict(os.environ, {"MODEL_ROUTER_STATE_DIR": str(self.root / "foreign"),
                                         "MODEL_ROUTER_TARGET": "claude"}), \
             mock.patch.object(core, "doctor", return_value={"ready": True, "issues": []}), \
             mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run") as process, \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service", return_value={"action": "install"}) as service:
            result = core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
        self.assertTrue(result["installed"])
        self.assertTrue((self.home / "jev-harness/manifest.json").is_file())
        journal = json.loads((self.home / "jev-harness/journal.json").read_text())
        self.assertEqual(journal["phase"], "complete")
        self.assertEqual(journal["owned"]["hooks.json"], core._owned_fingerprint(self.home, REPO, "hooks.json"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.home / "jev-harness").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.home / "jev-harness/journal.json").stat().st_mode), 0o600)
        self.assertTrue((self.home / "jev-global/ui.mjs").is_file())
        self.assertIn("safeBrowserClick", (self.home / "AGENTS.md").read_text())
        self.assertEqual(json.loads((self.home / "jev-harness/ladder-config.json").read_text()), LADDER)
        self.assertIn("jev-workers", (self.home / "config.toml").read_text())
        self.assertIsNone(json.loads((self.home / "jev-harness/manifest.json").read_text())["model_after"])
        self.assertEqual(json.loads((self.home / "jev-harness/manifest.json").read_text())["native_session_sharing"],
                         "pending_explicit_opt_in")
        self.assertTrue(service.call_args.kwargs["dry_run"] is False)
        self.assertTrue(any("configure-auth.mjs" in " ".join(call.args[0]) for call in process.call_args_list))
        self.assertFalse(any("chatgpt-session.mjs" in " ".join(call.args[0]) for call in process.call_args_list))
        self.assertFalse(any("synthetic" in str(call) for call in process.call_args_list))
        for call in process.call_args_list:
            self.assertEqual(Path(call.kwargs["env"]["MODEL_ROUTER_STATE_DIR"]).resolve(),
                             (self.home / "codex-router").resolve())
            self.assertEqual(call.kwargs["env"]["MODEL_ROUTER_TARGET"], "codex")

    def test_service_runtime_write_does_not_invalidate_owned_checkpoint(self):
        def start_service(_plan, *, dry_run):
            self.assertFalse(dry_run)
            runtime = self.home / "codex-router/runtime-metrics.json"
            runtime.parent.mkdir(parents=True, exist_ok=True)
            runtime.write_text('{"started": true}')

        with mock.patch.object(core, "doctor", return_value={"ready": True, "issues": []}), \
             mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run"), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service", side_effect=start_service):
            result = core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
        self.assertTrue(result["installed"])
        self.assertEqual(json.loads((self.home / "jev-harness/journal.json").read_text())["phase"], "complete")

    def test_native_install_omits_external_credentials_and_ladder_file(self):
        with mock.patch.object(core, "doctor", return_value={"ready": True, "issues": []}), \
             mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run"), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service", return_value={"action": "install"}) as service:
            result = core.install(REPO, self.home, None, None, self.key, 4321, False, "macos")
        manifest = json.loads((self.home / "jev-harness/manifest.json").read_text())
        journal = json.loads((self.home / "jev-harness/journal.json").read_text())
        self.assertTrue(result["installed"])
        self.assertEqual(manifest["ladder_mode"], "native")
        self.assertEqual(journal["input_paths"][:2], [None, None])
        self.assertFalse((self.home / "jev-harness/ladder-config.json").exists())
        env = service.call_args.args[0]["env_paths"]
        self.assertEqual(env["JEV_LADDER_MODE"], "native")
        self.assertNotIn("JEV_OMNIROUTE_AUTH_FILE", env)

    def test_openrouter_jev_key_stays_on_its_own_provider_path(self):
        with mock.patch.object(core, "doctor", return_value={"ready": True, "issues": []}), \
             mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run"), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service", return_value={"action": "install"}) as service:
            core.install(REPO, self.home, None, None, self.key, 4321, False, "macos",
                         jev_provider="openrouter")
        manifest = json.loads((self.home / "jev-harness/manifest.json").read_text())
        env = service.call_args.args[0]["env_paths"]
        self.assertEqual(manifest["jev_provider"], "openrouter")
        self.assertEqual(env["JEV_DECISION_PROVIDER"], "openrouter")
        self.assertEqual(env["JEV_DECISION_KEY_FILE"], str(self.key.resolve()))
        self.assertNotIn("TYPESAFE_API_KEY_FILE", env)

    def test_native_verify_accepts_healthy_service_without_ladder(self):
        with mock.patch.object(core, "doctor", return_value={"ready": True, "issues": []}), \
             mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run"), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service", return_value={"action": "install"}):
            core.install(REPO, self.home, None, None, self.key, 4321, False, "macos")
        health = mock.Mock(status=200)
        health.read.return_value = b'{"ok":true,"ladder_active":false,"auth_configured":true}'
        health.__enter__ = mock.Mock(return_value=health)
        health.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(core, "platform_module", return_value=core.platform_module("macos")), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "owned": True, "active": True}), \
             mock.patch.object(core.urllib.request, "urlopen", return_value=health):
            report = core.verify(self.home)
        self.assertEqual(report["ladder_mode"], "native")
        self.assertTrue(report["service_health"])

    def _installed(self, fail_command=None):
        def process(command, **kwargs):
            if fail_command and any(fail_command in str(part) for part in command):
                raise core.subprocess.CalledProcessError(1, command)
            return mock.Mock(returncode=0, stdout="")
        with mock.patch.object(core, "doctor", return_value={"ready": True, "issues": []}), \
             mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run", side_effect=process), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service", return_value={"action": "install"}):
            return core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")

    def test_partial_failure_keeps_journal_and_refuses_ambiguous_rollback(self):
        with self.assertRaises(core.subprocess.CalledProcessError):
            self._installed("configure-auth.mjs")
        journal = json.loads((self.home / "jev-harness/journal.json").read_text())
        self.assertEqual(journal["phase"], "model_configured")
        with self.assertRaisesRegex(core.InstallError, "Partial install"):
            core.rollback(self.home, "macos")
        self.assertTrue((self.home / "jev-harness/journal.json").exists())

    def test_resume_after_auth_failure_skips_completed_provider_steps(self):
        with self.assertRaises(core.subprocess.CalledProcessError):
            self._installed("configure-auth.mjs")
        with mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run") as process, \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service"):
            result = core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
        self.assertTrue(result["installed"])
        commands = [" ".join(map(str, call.args[0])) for call in process.call_args_list]
        self.assertTrue(any("configure-auth.mjs" in command for command in commands))
        self.assertFalse(any("generic add" in command or "configure-model.mjs" in command for command in commands))
        self.assertEqual(json.loads((self.home / "jev-harness/journal.json").read_text())["phase"], "complete")

    def test_resume_after_catalog_failure_preserves_checkpoint(self):
        with self.assertRaises(core.subprocess.CalledProcessError):
            self._installed("refresh-catalog.mjs")
        journal = json.loads((self.home / "jev-harness/journal.json").read_text())
        self.assertEqual(journal["phase"], "chatgpt_deferred")
        self._installed()
        self.assertEqual(json.loads((self.home / "jev-harness/journal.json").read_text())["phase"], "complete")

    def test_reviewed_oauth_change_can_resume_without_adopting_other_edits(self):
        with self.assertRaises(core.subprocess.CalledProcessError):
            self._installed("refresh-catalog.mjs")
        auth = self.home / "auth.json"
        auth.write_text('{"synthetic": "private"}')
        auth.chmod(0o600)
        with self.assertRaisesRegex(core.InstallError, "changed since installation checkpoint"):
            self._installed()
        with mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run"), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service"):
            result = core.resume(REPO, self.home, self.ladder, self.omni, self.key, 4321,
                                 "macos", reviewed_auth_sha256=core._digest(auth))
        self.assertTrue(result["installed"])

    def test_oauth_review_refuses_any_second_changed_file(self):
        with self.assertRaises(core.subprocess.CalledProcessError):
            self._installed("refresh-catalog.mjs")
        auth = self.home / "auth.json"
        auth.write_text('{"synthetic": "private"}')
        (self.home / "config.toml").write_text('model = "foreign"\n')
        with mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}):
            with self.assertRaisesRegex(core.InstallError, "sole auth.json change"):
                core.resume(REPO, self.home, self.ladder, self.omni, self.key, 4321,
                            "macos", reviewed_auth_sha256=core._digest(auth))

    def test_resume_after_service_failure_does_not_repeat_router(self):
        with mock.patch.object(core, "doctor", return_value={"ready": True, "issues": []}), \
             mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run"), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service", side_effect=RuntimeError("synthetic service failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic service failure"):
                core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
        self.assertEqual(json.loads((self.home / "jev-harness/journal.json").read_text())["phase"], "manifest_written")
        with mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run") as process, \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service") as service:
            self.assertTrue(core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")["installed"])
        self.assertFalse(any("router/install.sh" in " ".join(map(str, call.args[0]))
                             for call in process.call_args_list))
        service.assert_called_once()

    def test_resume_refuses_foreign_profile_edit_before_any_action(self):
        with self.assertRaises(core.subprocess.CalledProcessError):
            self._installed("configure-auth.mjs")
        (self.home / "config.toml").write_text('model = "foreign"\n')
        with mock.patch.object(core.subprocess, "run") as process:
            with self.assertRaisesRegex(core.InstallError, "changed since installation checkpoint"):
                core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
        process.assert_not_called()

    def test_resume_refuses_foreign_router_provenance(self):
        with self.assertRaises(core.subprocess.CalledProcessError):
            self._installed("configure-auth.mjs")
        manifest = self.home / "codex-router/install-manifest.json"
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text(json.dumps({"version": 1, "current": {"sourceRoot": "/foreign", "target": "codex"}}))
        journal_path = self.home / "jev-harness/journal.json"
        journal = json.loads(journal_path.read_text())
        journal["owned"] = core._inventory(self.home, Path(journal["definition_path"]))
        journal_path.write_text(json.dumps(journal))
        with mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}):
            with self.assertRaisesRegex(core.InstallError, "provenance differs"):
                core.resume(REPO, self.home, self.ladder, self.omni, self.key, 4321, "macos")

    def test_resume_refuses_ambiguous_router_bootstrap(self):
        with self.assertRaises(core.subprocess.CalledProcessError):
            self._installed("install.sh")
        with mock.patch.object(core.subprocess, "run") as process, \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}):
            with self.assertRaisesRegex(core.InstallError, "ambiguous"):
                core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
        self.assertFalse(any("router/install.sh" in " ".join(map(str, call.args[0]))
                             for call in process.call_args_list))

    def test_failed_bootstrap_journal_precedes_side_effect(self):
        with self.assertRaises(core.subprocess.CalledProcessError):
            self._installed("install.sh")
        journal = json.loads((self.home / "jev-harness/journal.json").read_text())
        self.assertEqual(journal["phase"], "router_started")
        self.assertFalse(journal["router_owned"])

    def test_rollback_refuses_concurrent_edit_and_foreign_service(self):
        self._installed()
        (self.home / "hooks.json").write_text("user changed this")
        with mock.patch("harness.platforms.macos.remove_service") as remove:
            with self.assertRaisesRegex(core.InstallError, "changed since install"):
                core.rollback(self.home, "macos")
            remove.assert_not_called()
        self.assertTrue((self.home / "hooks.json").exists())

    def test_reinstall_identical_profile_is_noop(self):
        self._installed()
        (self.home / "sessions").mkdir()
        (self.home / "sessions/new.jsonl").write_text("runtime session")
        (self.home / "auth.json").write_text("runtime auth")
        (self.home / "jev-harness/router.out.log").write_text("runtime log")
        with mock.patch.object(core, "doctor") as doctor, \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}), \
             mock.patch.object(core.subprocess, "run") as process:
            result = core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
        self.assertTrue(result["already_installed"])
        doctor.assert_not_called()
        process.assert_not_called()

    def test_rollback_ignores_unrelated_runtime_churn(self):
        self._installed()
        (self.home / "sessions").mkdir()
        (self.home / "sessions/new.jsonl").write_text("runtime session")
        (self.home / "auth.json").write_text("runtime auth")
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}), \
             mock.patch("harness.platforms.macos.remove_service"):
            self.assertTrue(core.rollback(self.home, "macos")["service_removed"])
        self.assertTrue((self.home / "sessions/new.jsonl").exists())
        self.assertTrue((self.home / "auth.json").exists())

    def test_manual_model_and_unrelated_host_edits_survive_reinstall_and_rollback(self):
        self._installed()
        config_path = self.home / "config.toml"
        config_path.write_text('model = "gpt-6-sol"\n[unrelated]\nmode = "custom"\n' + config_path.read_text())
        hooks_path = self.home / "hooks.json"
        hooks = json.loads(hooks_path.read_text())
        foreign = {"hooks": [{"type": "command", "command": "foreign-read"}]}
        hooks["hooks"]["PreToolUse"].insert(0, foreign)
        hooks_path.write_text(json.dumps(hooks))
        agents_path = self.home / "AGENTS.md"
        agents_path.write_text("User preface\n" + agents_path.read_text() + "User footer\n")
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}):
            report = core.verify(self.home)
            self.assertTrue(report["files"])
            self.assertTrue(report["client_config"])
            self.assertTrue(report["model_preserved"])
            self.assertTrue(report["model_changed_since_install"])
            self.assertTrue(core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")["already_installed"])
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}), \
             mock.patch("harness.platforms.macos.remove_service"):
            core.rollback(self.home, "macos")
        self.assertIn('model = "gpt-6-sol"', config_path.read_text())
        self.assertIn('[unrelated]', config_path.read_text())
        self.assertIn(foreign, json.loads(hooks_path.read_text())["hooks"]["PreToolUse"])
        self.assertEqual(agents_path.read_text(), "User preface\nUser footer\n")

    def test_managed_host_entries_cannot_be_changed_or_removed(self):
        cases = (
            ("config.toml", lambda path: path.write_text(path.read_text().replace(
                core._toml_string(str(REPO / "integrations/workers/mcp.py")),
                core._toml_string("/foreign/mcp.py")))),
            ("hooks.json", lambda path: path.write_text(path.read_text().replace(" pre_tool", " foreign_tool"))),
            ("AGENTS.md", lambda path: path.write_text(path.read_text().replace("safeBrowserClick", "rawClick"))),
        )
        for relative, mutate in cases:
            with self.subTest(relative=relative):
                if self.home.exists():
                    import shutil
                    shutil.rmtree(self.home)
                self._installed()
                mutate(self.home / relative)
                with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}):
                    with self.assertRaisesRegex(core.InstallError, "changed since installation checkpoint"):
                        core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
                with mock.patch("harness.platforms.macos.remove_service") as remove:
                    with self.assertRaises(core.InstallError):
                        core.rollback(self.home, "macos")
                    remove.assert_not_called()

    def test_canvas_browser_is_configured_only_with_local_helper(self):
        helper = self.root / "mcp-helper.mjs"
        helper.write_text("// local test helper")
        with mock.patch.object(core, "_browser_helper", return_value=helper):
            self._installed()
        config = core.tomllib.loads((self.home / "config.toml").read_text())
        self.assertIn("jev-browser", config["mcp_servers"])
        manifest = json.loads((self.home / "jev-harness/manifest.json").read_text())
        self.assertTrue(manifest["browser_enabled"])

    def test_reinstall_refuses_foreign_service(self):
        self._installed()
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": False}), \
             mock.patch.object(core.subprocess, "run") as process:
            with self.assertRaisesRegex(core.InstallError, "service ownership"):
                core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
        process.assert_not_called()

    def test_rollback_removes_only_unchanged_harness_files(self):
        self._installed()
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}), \
             mock.patch("harness.platforms.macos.remove_service") as remove, \
             mock.patch.object(core.subprocess, "run") as process:
            result = core.rollback(self.home, "macos")
        remove.assert_called_once()
        process.assert_not_called()  # No proven router service identity.
        self.assertTrue(result["service_removed"])
        self.assertFalse(result["router_removed"])
        self.assertFalse((self.home / "hooks.json").exists())
        self.assertFalse((self.home / "AGENTS.md").exists())
        self.assertTrue((self.home / "config.toml").exists())
        self.assertTrue((self.home / "jev-harness/journal.json").exists())
        with mock.patch("harness.platforms.macos.remove_service") as repeat:
            self.assertTrue(core.rollback(self.home, "macos")["already_rolled_back"])
            repeat.assert_not_called()

    def test_rollback_preserves_preexisting_agent_instructions(self):
        original_agents = "# Router instructions\n\nKeep this exact line.  \n"
        def process(command, **kwargs):
            if any("router/install.sh" in str(part).replace("\\", "/") for part in command):
                (self.home / "AGENTS.md").write_text(original_agents)
            return mock.Mock(returncode=0, stdout="")
        with mock.patch.object(core, "doctor", return_value={"ready": True, "issues": []}), \
             mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=None), \
             mock.patch.object(core.subprocess, "run", side_effect=process), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service"):
            core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}), \
             mock.patch("harness.platforms.macos.remove_service"):
            core.rollback(self.home, "macos")
        self.assertEqual((self.home / "AGENTS.md").read_text(), original_agents)

    def test_native_router_uninstall_requires_exact_definition(self):
        definition = self.root / "router.plist"
        def process(command, **kwargs):
            rendered = " ".join(map(str, command)).replace("\\", "/")
            if "router/install.sh" in rendered:
                definition.write_text("owned router definition")
            if "src/service.mjs uninstall" in rendered:
                definition.unlink()
            return mock.Mock(returncode=0, stdout="")
        with mock.patch.object(core, "doctor", return_value={"ready": True, "issues": []}), \
             mock.patch.object(core, "_port_free", return_value=True), \
             mock.patch.object(core, "_router_service_definition", return_value=definition), \
             mock.patch.object(core.subprocess, "run", side_effect=process), \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False}), \
             mock.patch("harness.platforms.macos.install_service"):
            core.install(REPO, self.home, self.ladder, self.omni, self.key, 4321, False, "macos")
        with mock.patch.object(core, "_router_service_definition", return_value=definition), \
             mock.patch.object(core.subprocess, "run", side_effect=process) as native, \
             mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}), \
             mock.patch("harness.platforms.macos.remove_service"):
            definition.write_text("foreign edit")
            with self.assertRaisesRegex(core.InstallError, "Embedded router service changed"):
                core.rollback(self.home, "macos")
            native.assert_not_called()
            definition.write_text("owned router definition")
            result = core.rollback(self.home, "macos")
        self.assertTrue(result["router_removed"])
        self.assertFalse(definition.exists())

    def test_verify_reports_health_separately(self):
        self.home.mkdir()
        (self.home / "jev-harness").mkdir()
        (self.home / "jev-global").mkdir()
        (self.home / "config.toml").write_text(core.merge_config("", REPO))
        (self.home / "hooks.json").write_text(json.dumps(core.merge_hooks({}, REPO)))
        (self.home / "jev-harness/ladder-config.json").write_text(json.dumps(LADDER))
        (self.home / "jev-global/ui.mjs").write_text("// test")
        (self.home / "AGENTS.md").write_text(merge_agents("", REPO, self.home))
        (self.home / "jev-harness/manifest.json").write_text(json.dumps({
            "version": 1, "codex_home": str(self.home.resolve()), "model_after": None, "port": 4321}))
        with mock.patch.object(core.urllib.request, "urlopen", side_effect=OSError("offline")):
            result = core.verify(self.home)
        self.assertFalse(result["files"])
        self.assertFalse(result["client_config"])
        self.assertFalse(result["manifest_owned"])
        self.assertFalse(result["service_health"])
        self.assertEqual(result["jev_auto_response"], "unverified")

    def test_verify_rejects_foreign_hook_and_foreign_service_despite_http_ok(self):
        self._installed()
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"ok":true,"ladder_active":true,"auth_configured":true}'
        response.__enter__.return_value = response
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": False}), \
             mock.patch.object(core.urllib.request, "urlopen", return_value=response) as http:
            result = core.verify(self.home)
        self.assertTrue(result["files"])
        self.assertTrue(result["client_config"])
        self.assertFalse(result["service_installed_owned_active"])
        self.assertFalse(result["service_health"])
        http.assert_not_called()
        hooks_path = self.home / "hooks.json"
        hooks = json.loads(hooks_path.read_text())
        hooks["hooks"]["PreToolUse"] = [{"hooks": [{"type": "command", "command": "foreign"}]}]
        hooks_path.write_text(json.dumps(hooks))
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}), \
             mock.patch.object(core.urllib.request, "urlopen", return_value=response) as http:
            result = core.verify(self.home)
        self.assertFalse(result["files"])
        self.assertFalse(result["hook_config"])
        self.assertFalse(result["client_config"])
        self.assertFalse(result["service_health"])
        http.assert_not_called()

    def test_verify_requires_exact_worker_mcp_and_installed_service(self):
        self._installed()
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": False, "active": False, "owned": False}):
            result = core.verify(self.home)
        self.assertTrue(result["files"])
        self.assertTrue(result["hook_config"])
        self.assertFalse(result["service_health"])
        config_path = self.home / "config.toml"
        config_path.write_text(config_path.read_text().replace(
            core._toml_string(str(REPO / "integrations/workers/mcp.py")),
            core._toml_string("/foreign/mcp.py")))
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}):
            result = core.verify(self.home)
        self.assertFalse(result["worker_mcp_config"])
        self.assertFalse(result["client_config"])

    def test_verify_owned_active_service_and_manifest_hash(self):
        self._installed()
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"ok":true,"ladder_active":true,"auth_configured":true}'
        response.__enter__.return_value = response
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}), \
             mock.patch.object(core.urllib.request, "urlopen", return_value=response):
            result = core.verify(self.home)
        self.assertTrue(result["files"])
        self.assertTrue(result["client_config"])
        self.assertTrue(result["service_health"])
        manifest_path = self.home / "jev-harness/manifest.json"
        manifest_path.write_text(manifest_path.read_text() + " ")
        with mock.patch("harness.platforms.macos.service_status", return_value={"installed": True, "active": True, "owned": True}), \
             mock.patch.object(core.urllib.request, "urlopen", return_value=response) as http:
            result = core.verify(self.home)
        self.assertFalse(result["manifest_owned"])
        self.assertFalse(result["client_config"])
        self.assertFalse(result["service_health"])
        http.assert_not_called()

    def test_rollback_without_manifest_refuses_delete(self):
        self.home.mkdir()
        (self.home / "owned-by-user").write_text("retain")
        with self.assertRaisesRegex(core.InstallError, "No owned installation journal"):
            core.rollback(self.home, "macos")
        self.assertTrue((self.home / "owned-by-user").exists())


if __name__ == "__main__":
    unittest.main()
