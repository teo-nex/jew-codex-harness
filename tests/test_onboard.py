import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from harness import onboard


class OnboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        if os.name == "nt":
            from harness.platforms.windows import _private_state_dir
            _private_state_dir(self.root, create=False)
        self.home = self.root / "fresh-codex"
        self.key_root = self.root / "private-keys"

    def _doctor(self, repo, home, ladder, omni, key, port, **kwargs):
        return ({"ready": True, "issues": []} if key is not None else
                {"ready": False, "issues": ["Set protected Jev decision key path"]})

    def _install(self, repo, home, ladder, omni, key, port, *, dry_run, **kwargs):
        if dry_run:
            return {"dry_run": True}
        state = home / "jev-harness"
        state.mkdir(parents=True)
        (state / "journal.json").write_text("{}")
        return {"installed": True}

    def test_openrouter_native_key_is_hidden_and_written_privately(self):
        answers = iter(("", "openrouter", "no"))
        with mock.patch.object(onboard.core, "doctor", side_effect=self._doctor), \
             mock.patch.object(onboard.core, "prepare"), \
             mock.patch.object(onboard, "_offline_smoke"), \
             mock.patch.object(onboard.core, "install", side_effect=self._install) as install, \
             mock.patch.object(onboard.live_verify, "verify_decision", return_value={"ok": True, "status": "passed"}), \
             mock.patch.object(onboard.core, "verify", return_value={"files": True, "client_config": True,
                                                                     "model_preserved": True, "service_health": True}):
            result = onboard.run(self.root, self.home, 4319, read=lambda _: next(answers),
                                 secret=lambda _: "synthetic-secret", key_root=self.key_root)
        path = Path(result["key_file"])
        self.assertTrue(result["installed"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "hook_trust_pending")
        self.assertEqual(result["jev_provider"], "openrouter")
        self.assertEqual(result["ladder_mode"], "native")
        self.assertIn("CODEX_HOME", result["launch_command"])
        self.assertIn("--summary", result["report_command"])
        self.assertEqual(path.read_text(), "synthetic-secret\n")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertEqual(install.call_args.kwargs["jev_provider"], "openrouter")

    def test_preflight_blocker_prevents_secret_prompt(self):
        answers = iter(("", "typesafe", "no"))
        with mock.patch.object(onboard.core, "doctor", return_value={
                "ready": False, "issues": ["Set protected Jev decision key path", "Router port occupied"]}), \
             mock.patch.object(onboard.core, "prepare") as prepare:
            result = onboard.run(self.root, self.home, 4319, read=lambda _: next(answers),
                                 secret=lambda _: self.fail("secret must not be requested"), key_root=self.key_root)
        self.assertFalse(result["ready"])
        self.assertFalse(result["key_written"])
        self.assertFalse(self.key_root.exists())
        prepare.assert_not_called()

    def test_external_ladder_choice_requires_both_protected_paths(self):
        ladder = self.root / "ladder.json"
        omni = self.root / "omni.json"
        ladder.write_text("{}")
        omni.write_text("{}")
        answers = iter(("", "typesafe", "yes"))
        with mock.patch.object(onboard.core, "doctor", side_effect=self._doctor), \
             mock.patch.object(onboard.core, "prepare"), \
             mock.patch.object(onboard, "_offline_smoke"), \
             mock.patch.object(onboard.core, "install", side_effect=self._install) as install, \
             mock.patch.object(onboard.live_verify, "verify_decision", return_value={"ok": True, "status": "passed"}), \
             mock.patch.object(onboard.core, "verify", return_value={"files": True, "client_config": True,
                                                                     "model_preserved": True, "service_health": True}):
            result = onboard.run(self.root, self.home, 4319, ladder=ladder, omni=omni,
                                 read=lambda _: next(answers), secret=lambda _: "fixture-key",
                                 key_root=self.key_root)
        self.assertEqual(result["ladder_mode"], "active")
        self.assertEqual(install.call_args.args[2:4], (ladder, omni))

    @unittest.skipIf(os.name == "nt", "symlink creation is not guaranteed for Windows CI users")
    def test_key_store_rejects_symlinked_parent(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "linked-keys"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(onboard.core.InstallError, "symlink"):
            onboard._new_key_path(self.home, "typesafe", link / "inside")

    def test_wizard_creates_private_custom_sequence_used_by_installer(self):
        omni = self.root / "omni.json"
        omni.write_text("{}")
        omni.chmod(0o600)
        answers = iter(("", "typesafe", "yes", "create", "2",
                        "wally", "", "", "wally/glm", "", "low,high",
                        "native", "native", "", "", "", "", ""))
        with mock.patch.object(onboard.core, "doctor", side_effect=self._doctor), \
             mock.patch.object(onboard.core, "prepare"), \
             mock.patch.object(onboard, "_offline_smoke"), \
             mock.patch.object(onboard.core, "install", side_effect=self._install) as install, \
             mock.patch.object(onboard.live_verify, "verify_decision", return_value={"ok": True}), \
             mock.patch.object(onboard.core, "verify", return_value={"files": True, "client_config": True,
                                                                     "model_preserved": True, "service_health": True}):
            result = onboard.run(self.root, self.home, 4319, omni=omni,
                                 read=lambda _: next(answers), secret=lambda _: "synthetic",
                                 key_root=self.key_root)
        path = Path(result["ladder_config"])
        config = json.loads(path.read_text())
        self.assertEqual(result["provider_order"], ["wally", "native"])
        self.assertEqual(install.call_args.args[2], path)
        self.assertEqual(config["providers"][0]["model"], "wally/glm")
        self.assertEqual(config["reasoning_profiles"]["wally/glm"]["supported_efforts"], ["low", "high"])
        self.assertEqual(config["providers"][1]["models"]["gpt-6-astra"], "gpt-6-astra")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_preparation_removes_new_key_and_provider_config(self):
        omni = self.root / "omni.json"
        omni.write_text("{}")
        omni.chmod(0o600)
        answers = iter(("", "typesafe", "yes", "create", "1",
                        "custom", "", "", "vendor/model", "", "unknown"))
        with mock.patch.object(onboard.core, "doctor", side_effect=self._doctor), \
             mock.patch.object(onboard.core, "prepare", side_effect=onboard.core.InstallError("synthetic failure")), \
             self.assertRaisesRegex(onboard.core.InstallError, "synthetic failure"):
            onboard.run(self.root, self.home, 4319, omni=omni, read=lambda _: next(answers),
                        secret=lambda _: "synthetic", key_root=self.key_root)
        self.assertEqual(list(self.key_root.rglob("*.key")), [])
        self.assertEqual(list(self.key_root.rglob("providers.json")), [])

    def test_existing_intended_profile_is_not_replaced_by_another_default(self):
        self.home.mkdir()
        (self.home / "config.toml").write_text('model = "manual"')
        answers = iter(("", "typesafe", "no"))
        with mock.patch.object(onboard.core, "doctor", return_value={"ready": False,
              "issues": ["Existing Codex profile requires reviewed migration"]}) as doctor:
            result = onboard.run(self.root, self.home, 4319, read=lambda _: next(answers),
                                 secret=lambda _: self.fail("must not ask for a key"))
        self.assertEqual(Path(result["codex_home"]), self.home.resolve())
        self.assertEqual(doctor.call_args.args[1], self.home.resolve())

    def test_invalid_custom_sequence_does_not_collect_secret(self):
        omni = self.root / "omni.json"
        omni.write_text("{}")
        omni.chmod(0o600)
        answers = iter(("", "typesafe", "yes", "create", "1", "custom", "", "", "", "", "unknown"))
        with self.assertRaises(onboard.core.InstallError):
            onboard.run(self.root, self.home, 4319, omni=omni, read=lambda _: next(answers),
                        secret=lambda _: self.fail("must not ask for a key"), key_root=self.key_root)
        self.assertFalse(self.key_root.exists())


if __name__ == "__main__":
    unittest.main()
