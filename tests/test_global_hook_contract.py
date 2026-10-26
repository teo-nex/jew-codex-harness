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
CANVASTTY_RUNTIME = {
    "CANVASTTY_RUNTIME_PROVIDER": "codex",
    "CANVASTTY_RUNTIME_ADDRESS": "http://127.0.0.1:9",
    "CANVASTTY_RUNTIME_TERMINAL_SESSION_ID": "00000000-0000-4000-8000-000000000001",
    "CANVASTTY_RUNTIME_CAPABILITY": "synthetic-not-a-credential",
}


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


class GlobalHookContractTests(unittest.TestCase):
    def setUp(self):
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

    def test_manual_model_does_not_call_jev_for_ordinary_tool_use(self):
        event = {"session_id": self.session, "model": "gpt-6.1-sol", "tool_name": "Bash",
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

    def test_javascript_helper_text_does_not_authorize_an_entire_program(self):
        for code in ("// safeBrowserClick\n42", "const text = 'safeBrowserClick';",
                     "await safeBrowserClick(tab, 'button'); await otherAction();"):
            with self.subTest(code=code):
                event = {"session_id": self.session, "model": "jev/auto",
                         "tool_name": "mcp__node_repl__js", "tool_input": {"code": code}}
                with mock.patch.object(self.hook, "ask_jev", return_value=self.decision()) as ask:
                    result = self.hook.pre_tool(event)
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
                ask.assert_called_once()

    def test_shell_read_aliases_do_not_spend_a_semantic_decision(self):
        for name, arguments in (("Bash", {"command": "cat fixture.txt"}),
                                ("exec_command", {"cmd": "cat fixture.txt"}),
                                ("shell_command", {"command": "cat fixture.txt"})):
            with self.subTest(name=name):
                event = {"session_id": self.session, "model": "jev/auto",
                         "tool_name": name, "tool_input": arguments}
                with mock.patch.object(self.hook, "ask_jev") as ask:
                    result = self.hook.pre_tool(event)
                self.assertIsNone(result)
                ask.assert_not_called()

    def test_metric_rotation_keeps_only_current_and_one_private_backup(self):
        with mock.patch.object(self.hook, "METRIC_MAX_BYTES", 120):
            for index in range(10):
                self.hook.log_metric("fixture", counter=index, note="x" * 20)
        files = list(self.state.glob("usage.jsonl*"))
        self.assertEqual(len(files), 2)
        for path in files:
            self.assertLessEqual(path.stat().st_size, 120)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_canvastty_environment_does_not_bypass_jev_auto_review(self):
        event = {"session_id": self.session, "model": "jev/auto", "tool_name": "Bash",
                 "tool_input": {"command": "touch synthetic-output.txt"}}
        for capability in ("", "synthetic-not-a-credential"):
            for answer in (self.decision(), None):
                with self.subTest(capability_present=bool(capability), unavailable=answer is None):
                    with mock.patch.dict(os.environ, {
                        **CANVASTTY_RUNTIME, "CANVASTTY_RUNTIME_CAPABILITY": capability,
                        "JEV_WORKER_MANIFEST": "",
                    }), mock.patch.object(self.hook, "ask_jev", return_value=answer) as ask:
                        result = self.hook.pre_tool(event)
                    self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
                    ask.assert_called_once()

    def test_canvastty_environment_preserves_manual_model_tool_use(self):
        event = {"session_id": self.session, "model": "gpt-6.1-sol", "tool_name": "Bash",
                 "tool_input": {"command": "touch synthetic-output.txt"}}
        with mock.patch.dict(os.environ, {**CANVASTTY_RUNTIME,
                                         "JEV_WORKER_MANIFEST": ""}), \
                mock.patch.object(self.hook, "ask_jev") as ask:
            self.assertIsNone(self.hook.pre_tool(event))
        ask.assert_not_called()

    def test_canvastty_environment_does_not_skip_invalid_worker_binding(self):
        for model in ("jev/auto", "gpt-6.1-sol"):
            with self.subTest(model=model):
                event = {"session_id": self.session, "model": model, "tool_name": "Bash",
                         "tool_input": {"command": "touch synthetic-output.txt"}}
                with mock.patch.dict(os.environ, {
                    **CANVASTTY_RUNTIME,
                    "JEV_WORKER_MANIFEST": str(self.state / "missing-manifest.json"),
                }), mock.patch.object(self.hook, "ask_jev") as ask:
                    result = self.hook.pre_tool(event)
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
                ask.assert_not_called()

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
        for model in ("gpt-6.1-sol", "jev/auto"):
            for tool in ("mcp__computer-use__click", "mcp__browser__click"):
                with self.subTest(model=model, tool=tool):
                    event = {"session_id": self.session, "model": model, "tool_name": tool,
                             "tool_input": {"target": "synthetic button"}}
                    result = self.hook.pre_tool(event)
                    self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_browser_broker_rejects_url_outside_goal_in_both_modes(self):
        for model in ("gpt-6.1-sol", "jev/auto"):
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
