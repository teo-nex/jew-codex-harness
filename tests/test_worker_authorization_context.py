import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise ImportError("Synthetic test module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    with tempfile.TemporaryDirectory() as bootstrap:
        with mock.patch.dict(os.environ, {
            "CODEX_HOME": str(Path(bootstrap) / "profile"),
            "JEV_GLOBAL_STATE_DIR": str(Path(bootstrap) / "state"),
        }):
            spec.loader.exec_module(module)
    return module


HOOK = load_module("worker_authorization_hook", "integrations/jev/global/codex_hook.py")
QUICK = load_module("worker_authorization_quick", "integrations/workers/quick_worker.py")
NATIVE = load_module("worker_authorization_native", "integrations/workers/native_fallback.py")


class WorkerAuthorizationContextTests(unittest.TestCase):
    def test_terra61_worker_command_preserves_exact_model_and_effort(self):
        self.assertIn("gpt-6.1-terra", NATIVE.NATIVE_WORKER_MODELS)
        profile = self.root / "lean-profile"
        (profile / ".codex").mkdir(parents=True)
        (profile / ".codex/config.toml").write_text("", encoding="utf-8")
        task = {"worker_model": "gpt-6.1-terra", "worker_effort": "medium",
                "question_only": True, "goal": "Return a marker", "cwd": str(self.cwd)}
        with mock.patch.object(QUICK.Path, "home", return_value=profile):
            command = QUICK.command(task)
        self.assertEqual(command[command.index("-m") + 1], "gpt-6.1-terra")
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertIn("agents.enabled=false", command)

    def test_retired_native_worker_models_are_rejected_before_ui_access(self):
        bridge = mock.Mock()
        for model in ("gpt-5.6-luna", "gpt-5.6-terra"):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "Unsupported native worker"):
                NATIVE.choose_model_effort(bridge, {}, model=model)
        bridge.call.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        state = mock.patch.object(HOOK, "STATE", self.root / "hook-state")
        state.start()
        self.addCleanup(state.stop)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        for name in ("a.py", "b.py"):
            (self.cwd / name).write_text("old\n", encoding="utf-8")
        self.state = self.root / "workers"
        self.task_id = str(uuid.uuid4())
        self.directory = self.state / self.task_id
        self.directory.mkdir(parents=True)
        self.goal = "Implement exact authorization. " + ("goal-detail-" * 900)
        self.constraints = "Keep privacy and ownership. " + ("constraint-detail-" * 500)
        self.task = {
            "task_id": self.task_id,
            "request_id": "synthetic-request",
            "goal": self.goal,
            "cwd": str(self.cwd),
            "allowed_files": ["a.py", "b.py", "new.py"],
            "acceptance_commands": [["python", "-m", "unittest", "synthetic"]],
            "mandatory_instructions": self.constraints,
            "read_only": False,
            "manifest_path": str(self.directory / "task.json"),
            "worker_model": "gpt-6-luna",
            "worker_effort": "medium",
        }
        self.manifest = self.directory / "task.json"
        self.write_manifest()
        self.environment = mock.patch.dict(os.environ, {
            "JEV_WORKER_MANIFEST": str(self.manifest),
            "JEV_WORKER_MANIFEST_SHA256": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
            "JEV_WORKER_STATE_DIR": str(self.state),
        }, clear=False)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.task, ensure_ascii=False), encoding="utf-8")

    def rebind_manifest(self):
        self.write_manifest()
        os.environ["JEV_WORKER_MANIFEST_SHA256"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def capture_patch(self, patch):
        with mock.patch.object(HOOK, "ask_jev", return_value=self.decision("allow", 0.95)) as provider:
            self.assertIsNone(HOOK.pre_tool(self.event(patch)))
        provider.assert_called_once()
        return provider.call_args.args[0]

    @staticmethod
    def decision(choice, confidence):
        return {"answers": {"authorization": {"choice": choice, "confidence": confidence}}}

    def event(self, patch):
        return {"session_id": "synthetic-session", "model": "jev/auto",
                "tool_name": "apply_patch", "tool_input": {"command": patch}}

    def test_full_manifest_context_ignores_empty_or_truncated_active_goal(self):
        patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch\n"
        captured = {}

        def authorize(state, questions, timeout=4):
            captured.update(state)
            return self.decision("allow", 0.95)

        with mock.patch.object(HOOK, "ask_jev", side_effect=authorize), \
                mock.patch.object(HOOK, "_read", return_value={
                    "active_goal": "", "goals": ["start\n[middle omitted]\nend"]}):
            self.assertIsNone(HOOK.pre_tool(self.event(patch)))
        self.assertEqual(captured["user_goal"], self.goal)
        self.assertEqual(captured["delegation"]["constraints"], self.constraints)
        self.assertEqual(captured["delegation"]["cwd"], str(self.cwd))
        self.assertEqual(captured["delegation"]["allowed_files"], ["a.py", "b.py", "new.py"])

    def test_large_multi_file_patch_sends_complete_metadata_without_code(self):
        body = "+print('private-code-canary')\n" * 1200
        patch = ("*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n"
                 "*** Update File: b.py\n@@\n-old\n+new\n"
                 "*** Add File: new.py\n" + body + "*** End Patch\n")
        captured = {}

        def authorize(state, questions, timeout=4):
            captured.update(state)
            return self.decision("allow", 0.95)

        with mock.patch.object(HOOK, "ask_jev", side_effect=authorize):
            self.assertIsNone(HOOK.pre_tool(self.event(patch)))
        self.assertEqual(captured["patch_operations"], [
            {"operation": "update", "path": "a.py", "inside_working_directory": True},
            {"operation": "update", "path": "b.py", "inside_working_directory": True},
            {"operation": "add", "path": "new.py", "inside_working_directory": True},
        ])
        self.assertNotIn("private-code-canary", json.dumps(captured))
        self.assertNotIn("proposed_patch", captured)

    def test_missing_optional_instructions_keep_legacy_manifest_compatible(self):
        self.task.pop("mandatory_instructions")
        self.rebind_manifest()
        state = self.capture_patch("*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch\n")
        self.assertEqual(state["delegation"]["constraints"], "")
        self.assertEqual(state["user_goal"], self.goal)

    def test_invalid_instruction_types_fail_before_provider(self):
        patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch\n"
        for invalid in (None, False, 1, [], {}):
            with self.subTest(invalid=invalid):
                self.task["mandatory_instructions"] = invalid
                self.rebind_manifest()
                with mock.patch.object(HOOK, "ask_jev") as provider:
                    result = HOOK.pre_tool(self.event(patch))
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
                provider.assert_not_called()

    def test_nested_and_dot_prefixed_ownership_uses_canonical_paths(self):
        (self.cwd / "nested").mkdir()
        (self.cwd / "nested" / "file.py").write_text("old\n", encoding="utf-8")
        patch = "*** Begin Patch\n*** Update File: nested/file.py\n@@\n-old\n+new\n*** End Patch\n"
        for owned in (["./nested/file.py"], ["nested"]):
            with self.subTest(owned=owned):
                self.task["allowed_files"] = owned
                self.rebind_manifest()
                state = self.capture_patch(patch)
                self.assertEqual(state["patch_operations"], [{
                    "operation": "update", "path": "nested/file.py", "inside_working_directory": True,
                }])

    def test_scope_preserves_literal_leading_space_in_filename(self):
        name = " leading.py"
        (self.cwd / name).write_text("old\n", encoding="utf-8")
        self.task["allowed_files"] = [name]
        self.rebind_manifest()
        state = self.capture_patch(f"*** Begin Patch\n*** Update File: {name}\n@@\n-old\n+new\n*** End Patch\n")
        self.assertEqual(state["patch_operations"][0]["path"], name)
        self.assertEqual(state["delegation"]["allowed_files"], [name])

    def test_threshold_explicit_deny_unavailable_and_allow(self):
        patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch\n"
        for response, expected in (
            (self.decision("allow", 0.89), "Decision=allow"),
            (self.decision("deny", 0.99), "Decision=deny"),
            (None, "Decision=unavailable"),
        ):
            with self.subTest(expected=expected), mock.patch.object(HOOK, "ask_jev", return_value=response):
                result = HOOK.pre_tool(self.event(patch))
                self.assertIn(expected, result["hookSpecificOutput"]["permissionDecisionReason"])
        with mock.patch.object(HOOK, "ask_jev", return_value=self.decision("allow", 0.95)):
            self.assertIsNone(HOOK.pre_tool(self.event(patch)))

    def test_malformed_provider_answers_fail_closed(self):
        patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch\n"
        responses = [[], {"answers": None}, {"answers": {"authorization": None}}]
        responses.extend(self.decision("allow", score) for score in (True, float("inf"), float("nan"), 1.1))
        for response in responses:
            with self.subTest(response=response), mock.patch.object(HOOK, "ask_jev", return_value=response):
                result = HOOK.pre_tool(self.event(patch))
                self.assertIsNotNone(result)
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_tampered_manifest_fails_before_provider(self):
        self.task["goal"] = "tampered"
        self.write_manifest()
        with mock.patch.object(HOOK, "ask_jev") as provider:
            result = HOOK.pre_tool(self.event(
                "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch\n"))
        provider.assert_not_called()
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_existing_out_of_scope_and_readonly_guards(self):
        patches = [
            "*** Begin Patch\n*** Update File: ../outside.py\n@@\n-old\n+new\n*** End Patch\n",
            "*** Begin Patch\n*** Update File: unowned.py\n@@\n-old\n+new\n*** End Patch\n",
        ]
        for patch in patches:
            with self.subTest(patch=patch), mock.patch.object(HOOK, "ask_jev") as provider:
                result = HOOK.pre_tool(self.event(patch))
                provider.assert_not_called()
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.task["read_only"] = True
        self.write_manifest()
        os.environ["JEV_WORKER_MANIFEST_SHA256"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        with mock.patch.object(HOOK, "ask_jev") as provider:
            result = HOOK.pre_tool(self.event(
                "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch\n"))
            provider.assert_not_called()
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_existing_symlink_guard(self):
        outside = self.root / "outside.py"
        outside.write_text("old\n", encoding="utf-8")
        try:
            (self.cwd / "link.py").symlink_to(outside)
        except NotImplementedError:
            self.skipTest("Host does not support symlinks")
        except OSError as error:
            if (error.errno not in {errno.EPERM, errno.EACCES, errno.ENOSYS, getattr(errno, "ENOTSUP", -1)}
                    and getattr(error, "winerror", None) != 1314):
                raise
            self.skipTest("Host lacks symlink capability or privilege")
        self.task["allowed_files"] = ["link.py"]
        self.write_manifest()
        os.environ["JEV_WORKER_MANIFEST_SHA256"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        with mock.patch.object(HOOK, "ask_jev") as provider:
            result = HOOK.pre_tool(self.event(
                "*** Begin Patch\n*** Update File: link.py\n@@\n-old\n+new\n*** End Patch\n"))
            provider.assert_not_called()
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_initial_resume_and_native_packets_preserve_authority_and_v4a(self):
        profile = self.root / ".codex"
        profile.mkdir()
        (profile / "config.toml").write_text("", encoding="utf-8")
        with mock.patch.object(QUICK.tomllib, "loads", return_value={"mcp_servers": {}, "plugins": {}}), \
                mock.patch.object(QUICK.Path, "home", return_value=self.root):
            initial = QUICK.command(self.task)[-1]
            resumed = QUICK.resume_command(self.task, "thread-id")[-1]
        packet = ("JEV_WORKER_MANIFEST_V1: " + str(self.manifest)
                  + "\nGoal: " + self.goal
                  + "\nMandatory instructions: " + self.constraints
                  + "\n" + NATIVE.PATCH_CONTRACT)
        native = NATIVE.delivery_prompt(packet, self.task, self.directory / "native-packet.txt")
        for value in (initial, resumed, native):
            with self.subTest(length=len(value)):
                self.assertIn(self.goal, value)
                self.assertIn(self.constraints, value)
                self.assertIn("*** Add File:", value)
                self.assertIn("*** Update File:", value)
                self.assertIn("bare unified diffs", value)
                self.assertIn("merge-conflict markers", value)


if __name__ == "__main__":
    unittest.main()
