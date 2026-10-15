"""Offline behavior contracts for the packaged global Codex hook."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "integrations/jev/global/codex_hook.py"


def load_hook(codex_home, state_dir):
    with mock.patch.dict(os.environ, {
        "CODEX_HOME": str(codex_home),
        "JEV_GLOBAL_STATE_DIR": str(state_dir),
        "JEV_WORKER_STATE_DIR": str(state_dir / "workers"),
        "JEV_GLOBAL_ASK_URL": "http://127.0.0.1:9/ask",
    }, clear=False):
        spec = importlib.util.spec_from_file_location("test_packaged_codex_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


CANVASTTY_ENV = {
    "CANVASTTY_RUNTIME_PROVIDER": "codex",
    "CANVASTTY_RUNTIME_ADDRESS": "/synthetic/runtime.sock",
    "CANVASTTY_RUNTIME_TERMINAL_SESSION_ID": "12345678-1234-4567-8901-123456789abc",
    "CANVASTTY_RUNTIME_CAPABILITY": "synthetic-capability-not-a-real-secret",
}


class GlobalHookContractTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {key: "" for key in CANVASTTY_ENV})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "private-state"
        self.hook = load_hook(root / "codex-profile", self.state)
        self.session = "synthetic-session"
        self.hook.update_state(self.session, lambda saved: saved.update(
            active_goal="Inspect the synthetic fixture and preserve its contents."))

    @staticmethod
    def decision(choice="deny", confidence=0.99):
        return {"answers": {"authorization": {"choice": choice, "confidence": confidence}}}

    def test_native_launch_context_requires_all_fields_and_codex_identity(self):
        with mock.patch.dict(os.environ, CANVASTTY_ENV):
            self.assertTrue(self.hook.canvastty_session())
            for key in CANVASTTY_ENV:
                with self.subTest(missing=key), mock.patch.dict(os.environ, {key: " "}):
                    self.assertFalse(self.hook.canvastty_session())
            for key, value in [("CANVASTTY_RUNTIME_PROVIDER", "other-provider"),
                               ("CANVASTTY_RUNTIME_TERMINAL_SESSION_ID", "invalid")]:
                with self.subTest(field=key), mock.patch.dict(os.environ, {key: value}):
                    self.assertFalse(self.hook.canvastty_session())

    def test_native_tool_use_returns_no_verdict_and_preserves_arguments(self):
        import copy
        tools = [("Bash", {"command": "interpreter --evaluate synthetic-program"}),
                 ("apply_patch", {"command": "synthetic patch"}),
                 ("mcp__canvastty_agents__spawn_agent", {"provider": "codex", "model": "jev/auto",
                     "profile": "auto", "cwd": "/workspace/project", "prompt": "Inspect the fixture.",
                     "launchOptions": {"account-plugin": {"account": "selected-account"}}}),
                 ("mcp__canvastty_agents__send_to_agent", {"sessionId": "owned-agent"}),
                 ("mcp__browser__click", {"target": "synthetic button"}),
                 ("mcp__other_service__write", {"path": "synthetic-target"}),
                 ("new_tool", {"value": "synthetic-value"})]
        for model in ["jev/auto", "manual-model", None]:
            for name, value in tools:
                with self.subTest(model=model, tool=name), \
                     mock.patch.dict(os.environ, CANVASTTY_ENV), \
                     mock.patch.object(self.hook, "ask_jev") as ask:
                    event = {"session_id": "uncaptured-session", "model": model,
                             "tool_name": name, "tool_input": value}
                    original = copy.deepcopy(event)
                    self.assertIsNone(self.hook.pre_tool(event))
                    self.assertEqual(event, original)
                    ask.assert_not_called()
        rows = [json.loads(line) for line in (self.state / "usage.jsonl").read_text().splitlines()]
        receipts = [row for row in rows if row["kind"] == "native_tool_use"]
        self.assertEqual(len(receipts), len(tools) * 3)
        self.assertTrue(all(row["policy"] == "canvastty" for row in receipts))
        serialized = json.dumps(receipts)
        for value in CANVASTTY_ENV.values():
            self.assertNotIn(value, serialized)
        self.assertNotIn("synthetic-program", serialized)
        self.assertNotIn("permissionDecision", serialized)

    def test_partial_launch_context_and_tool_arguments_do_not_disable_standalone_guard(self):
        event = {"session_id": self.session, "model": "jev/auto", "tool_name": "Bash",
                 "tool_input": {"command": "touch synthetic-output.txt", **CANVASTTY_ENV}}
        for missing in CANVASTTY_ENV:
            with self.subTest(missing=missing), \
                 mock.patch.dict(os.environ, {**CANVASTTY_ENV, missing: ""}), \
                 mock.patch.object(self.hook, "ask_jev", return_value=self.decision()) as ask:
                result = self.hook.pre_tool(event)
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
                ask.assert_called_once()

    def test_native_prompt_describes_owner_without_extra_model_calls_or_auxiliary_guide(self):
        with mock.patch.dict(os.environ, CANVASTTY_ENV), \
             mock.patch.object(self.hook, "ask_jev") as ask, \
             mock.patch.object(self.hook, "skill_hint") as hint:
            result = self.hook.prompt({"session_id": self.session, "model": "jev/auto",
                                       "prompt": "Inspect the synthetic fixture"})
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CanvasTTY owns all Tool Use permissions", context)
        self.assertIn("Jev only selects the model", context)
        self.assertIn("discover missing tools once", context)
        self.assertIn("no duplicate get_agent_result", context)
        self.assertIn("Do not read the standalone", context)
        self.assertNotIn(self.hook.OMNIROUTE_TEXT_HELPER, context)
        self.assertNotIn(self.hook.OMNIROUTE_GUIDE, context)
        ask.assert_not_called()
        hint.assert_not_called()

    def test_owner_transition_replaces_previous_tool_review_notice(self):
        self.hook.update_state(self.session, lambda saved: saved.update(
            permission_owner="jev", tool_mode="jev_auto"))
        with mock.patch.dict(os.environ, CANVASTTY_ENV):
            result = self.hook.prompt({"session_id": self.session, "model": "jev/auto",
                                       "prompt": "Continue"})
        self.assertIn("CanvasTTY owns", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook._read(self.hook.state_file(self.session))["permission_owner"], "canvastty")

    def test_existing_native_session_receives_updated_workflow_once(self):
        self.hook.update_state(self.session, lambda saved: saved.update(permission_owner="canvastty", tool_mode="jev_auto"))
        with mock.patch.dict(os.environ, CANVASTTY_ENV):
            first = self.hook.prompt({"session_id": self.session, "model": "jev/auto", "prompt": "Continue"})
            second = self.hook.prompt({"session_id": self.session, "model": "jev/auto", "prompt": "Continue"})
        self.assertIn("discover missing tools once", first["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(second)

    def test_native_prompt_does_not_consume_the_standalone_auxiliary_notice(self):
        with mock.patch.dict(os.environ, CANVASTTY_ENV):
            self.hook.prompt({"session_id": self.session, "model": "jev/auto",
                              "prompt": "Continue"})
        self.assertNotIn("omniroute_guide_version", self.hook._read(self.hook.state_file(self.session)))
        manifest = self.hook.CODEX_HOME / "jev-harness/manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"ladder_mode": "active"}))
        result = self.hook.prompt({"session_id": self.session, "model": "jev/auto", "prompt": "Continue"})
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.hook.OMNIROUTE_GUIDE, context)
        self.assertNotIn("CanvasTTY owns", context)

    def test_native_compaction_keeps_bounded_local_carry_without_a_model_judge(self):
        recent = ["Changed synthetic file", "Unresolved synthetic failure"]
        self.hook.update_state(self.session, lambda saved: saved.update(recent=recent))
        with mock.patch.dict(os.environ, CANVASTTY_ENV), \
             mock.patch.object(self.hook, "ask_jev") as ask:
            self.assertIsNone(self.hook.pre_compact({"session_id": self.session, "trigger": "test"}))
        carry = self.hook._read(self.hook.state_file(self.session))["carry"]
        self.assertLessEqual(len(carry), 900)
        for item in recent:
            self.assertIn(item, carry)
        ask.assert_not_called()

    def test_manual_model_does_not_call_jev_for_ordinary_tool_use(self):
        event = {"session_id": self.session, "model": "gpt-6-sol", "tool_name": "Bash",
                 "tool_input": {"command": "touch synthetic-output.txt"}}
        with mock.patch.object(self.hook, "ask_jev") as ask:
            result = self.hook.pre_tool(event)
        self.assertIsNone(result)
        ask.assert_not_called()

    def test_jev_auto_keeps_ordinary_tool_use_review(self):
        event = {"session_id": self.session, "model": "jev/auto", "tool_name": "Bash",
                 "tool_input": {"command": "touch synthetic-output.txt"}}
        with mock.patch.object(self.hook, "ask_jev", return_value=self.decision()) as ask:
            result = self.hook.pre_tool(event)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        ask.assert_called_once()

    def test_other_spawn_paths_and_nested_cli_remain_blocked(self):
        tools = (("spawn_agent", {}), ("functions.spawn_agent", {}),
                 ("mcp__other_agents__spawn_agent", {}),
                 ("mcp__canvastty_agents__spawn_agent", {}),
                 ("mcp__canvastty_agents_extra__spawn_agent", {}),
                 ("Bash", {"command": "codex exec 'inspect the fixture'"}))
        for name, value in tools:
            with self.subTest(name=name), mock.patch.object(self.hook, "ask_jev") as ask:
                result = self.hook.pre_tool({"session_id": self.session, "model": "jev/auto",
                                             "tool_name": name, "tool_input": value})
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
                ask.assert_not_called()

    def test_tool_namespace_without_native_launch_context_keeps_default_review(self):
        event = {"session_id": self.session, "model": "jev/auto",
                 "tool_name": "mcp__canvastty_agents__send_to_agent", "tool_input": {}}
        with mock.patch.object(self.hook, "ask_jev", return_value=self.decision()) as ask:
            result = self.hook.pre_tool(event)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        ask.assert_called_once()

    def test_named_single_file_patch_uses_bounded_scope_review(self):
        cwd = Path(self.tmp.name) / "workspace"
        cwd.mkdir()
        self.hook.update_state(self.session, lambda saved: saved.update(
            active_goal="Read seed.txt, then create proof.txt in this workspace with its bytes."))
        event = {"session_id": self.session, "model": "jev/auto", "cwd": str(cwd),
                 "tool_name": "apply_patch", "tool_input": {"command":
                     "*** Begin Patch\n*** Add File: proof.txt\n+JEV_TOOL_OK\n*** End Patch"}}
        with mock.patch.object(self.hook, "ask_jev", return_value=self.decision("allow", 0.85)) as ask:
            self.assertIsNone(self.hook.pre_tool(event))
        state = ask.call_args.args[0]
        self.assertEqual(ask.call_args.kwargs["timeout"], 5)
        self.assertEqual(state["patch_operations"], [{"operation": "add", "path": "proof.txt",
                                                        "inside_working_directory": True}])
        self.assertNotIn(str(cwd), json.dumps(state))
        self.assertNotIn("JEV_TOOL_OK", json.dumps(state))

    def test_patch_scope_does_not_lower_threshold_for_other_targets_or_deletion(self):
        cwd = Path(self.tmp.name) / "workspace"
        cwd.mkdir()
        self.hook.update_state(self.session, lambda saved: saved.update(
            active_goal="Create only proof.txt. Do not delete or modify any other file."))
        for operation, target in (("Add File", "other.txt"),
                                  ("Delete File", "proof.txt"),
                                  ("Add File", "../outside.txt"),
                                  ("Add File", "nested/proof.txt")):
            with self.subTest(operation=operation, target=target):
                patch = f"*** Begin Patch\n*** {operation}: {target}\n+test\n*** End Patch"
                event = {"session_id": self.session, "model": "jev/auto", "cwd": str(cwd),
                         "tool_name": "apply_patch", "tool_input": {"command": patch}}
                with mock.patch.object(self.hook, "ask_jev",
                                       return_value=self.decision("allow", 0.85)) as ask:
                    result = self.hook.pre_tool(event)
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertNotIn("patch_operations", ask.call_args.args[0])
        patch = ("*** Begin Patch\n*** Add File: proof.txt\n+ok\n"
                 "*** Add File: other.txt\n+extra\n*** End Patch")
        event = {"session_id": self.session, "model": "jev/auto", "cwd": str(cwd),
                 "tool_name": "apply_patch", "tool_input": {"command": patch}}
        with mock.patch.object(self.hook, "ask_jev",
                               return_value=self.decision("allow", 0.85)) as ask:
            result = self.hook.pre_tool(event)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("patch_operations", ask.call_args.args[0])

    def test_browser_and_computer_ui_writes_stay_guarded_for_both_modes(self):
        for model in ("gpt-6-sol", "jev/auto"):
            for tool in ("mcp__computer-use__click", "mcp__browser__click"):
                with self.subTest(model=model, tool=tool):
                    event = {"session_id": self.session, "model": model, "tool_name": tool,
                             "tool_input": {"target": "synthetic button"}}
                    result = self.hook.pre_tool(event)
                    self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_browser_broker_rejects_url_outside_goal_in_both_modes(self):
        for model in ("gpt-6-sol", "jev/auto"):
            with self.subTest(model=model):
                event = {"session_id": self.session, "model": model,
                         "tool_name": "mcp__jev_browser__browser_task",
                         "tool_input": {"action": "navigate", "url": "https://outside.example"}}
                result = self.hook.pre_tool(event)
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_compaction_carries_bounded_goal_with_recent_intent(self):
        long_goal = "Old context. " * 240 + "Keep the synthetic acceptance marker JEV_GOAL_TAIL."
        self.hook.update_state(self.session, lambda saved: saved.update(active_goal=long_goal))
        with mock.patch.object(self.hook, "ask_jev") as ask:
            self.hook.pre_compact({"session_id": self.session, "trigger": "test"})
        saved = self.hook._read(self.hook.state_file(self.session))
        carry = saved["carry"]
        self.assertLessEqual(len(carry), 900)
        self.assertIn("JEV_GOAL_TAIL", carry)
        ask.assert_not_called()
        self.assertEqual(saved["compactions"], 1)

    def test_native_mode_does_not_recommend_unconfigured_omniroute(self):
        manifest = self.hook.CODEX_HOME / "jev-harness/manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"ladder_mode":"native"}')
        with mock.patch.object(self.hook, "skill_hint", return_value=None):
            result = self.hook.prompt({"session_id": self.session, "model": "jev/auto",
                                       "prompt": "Inspect the synthetic fixture"})
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Active model is jev/auto", context)
        self.assertNotIn("OmniRoute", context)


if __name__ == "__main__":
    unittest.main()
