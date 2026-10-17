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
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = load_module("worker_authorization_hook", "integrations/jev/global/codex_hook.py")
QUICK = load_module("worker_authorization_quick", "integrations/workers/quick_worker.py")
NATIVE = load_module("worker_authorization_native", "integrations/workers/native_fallback.py")


class WorkerAuthorizationContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
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

    def test_tampered_manifest_fails_before_provider(self):
        self.task["goal"] = "tampered"
        self.write_manifest()
        with mock.patch.object(HOOK, "ask_jev") as provider:
            result = HOOK.pre_tool(self.event(
                "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch\n"))
        provider.assert_not_called()
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_existing_out_of_scope_readonly_and_symlink_guards(self):
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
        self.task["read_only"] = False
        outside = self.root / "outside.py"
        outside.write_text("old\n", encoding="utf-8")
        (self.cwd / "link.py").symlink_to(outside)
        self.task["allowed_files"] = ["link.py"]
        self.write_manifest()
        os.environ["JEV_WORKER_MANIFEST_SHA256"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        with mock.patch.object(HOOK, "ask_jev") as provider:
            result = HOOK.pre_tool(self.event(
                "*** Begin Patch\n*** Update File: link.py\n@@\n-old\n+new\n*** End Patch\n"))
            provider.assert_not_called()
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_initial_resume_and_native_packets_preserve_authority_and_v4a(self):
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
