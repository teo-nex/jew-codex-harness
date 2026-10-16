"""Write authorization must survive platform, model and tool-name differences."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import uuid

from test_global_hook_contract import load_hook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/workers"))
from ownership import owned_paths, patch_allowed, readonly_command


class WorkerOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.state = self.root / "state"
        self.directory = self.state / str(uuid.uuid4())
        self.directory.mkdir(parents=True)
        self.task = {"task_id": self.directory.name, "cwd": str(self.cwd),
                     "allowed_files": ["owned.txt"], "goal": "Create owned.txt with the answer", "read_only": False}
        self.manifest = self.directory / "task.json"
        self.manifest.write_text(json.dumps(self.task), encoding="utf-8")
        self.env = {"JEV_WORKER_MANIFEST": str(self.manifest),
                    "JEV_WORKER_STATE_DIR": str(self.state),
                    "JEV_WORKER_MANIFEST_SHA256": hashlib.sha256(self.manifest.read_bytes()).hexdigest()}
        self.hook = load_hook(self.root / "profile", self.root / "hook-state")

    def patch(self, name="owned.txt", operation="Add File"):
        return f"*** Begin Patch\n*** {operation}: {name}\n+answer\n*** End Patch"

    def event(self, tool="apply_patch", value=None, model="jev/auto"):
        return {"session_id": "worker-session", "cwd": str(self.cwd), "model": model,
                "tool_name": tool, "tool_input": value or {"command": self.patch()}}

    def test_patch_paths_and_move_destination_must_be_owned(self):
        self.assertTrue(patch_allowed(self.task, self.patch()))
        for name in ("other.txt", "../escape.txt", str(self.root / "outside.txt"), "."):
            with self.subTest(name=name):
                self.assertFalse(patch_allowed(self.task, self.patch(name)))
        move = "*** Begin Patch\n*** Update File: owned.txt\n*** Move to: other.txt\n@@\n-old\n+new\n*** End Patch"
        self.assertFalse(patch_allowed(self.task, move))
        self.assertFalse(patch_allowed({**self.task, "read_only": True}, self.patch()))
        for names in (["."], ["../work/owned.txt"], ["owned.txt", "owned.txt"]):
            with self.assertRaises(ValueError):
                owned_paths(self.cwd, names)

    def test_link_escapes_and_hardlinks_are_denied(self):
        target = self.root / "outside.txt"
        target.write_text("preserve", encoding="utf-8")
        link = self.cwd / "owned.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Host cannot create symlinks")
        self.assertFalse(patch_allowed(self.task, self.patch()))
        link.unlink()
        os.link(target, link)
        self.assertFalse(patch_allowed(self.task, self.patch()))
        self.assertEqual(target.read_text(encoding="utf-8"), "preserve")

    def test_shell_write_and_interpreter_bypasses_are_denied(self):
        for command in ("cat owned.txt", "rg -n answer .", "Get-Content -LiteralPath 'owned.txt' -Raw"):
            self.assertTrue(readonly_command(command), command)
        for command in ("cat owned.txt > other.txt", "python -c 'print(1)'", "bash -c 'cat owned.txt'",
                        "rg --pre python answer .", "rg --hostname-bin python answer .", "cat a; touch b",
                        "Get-Content a | Set-Content b", "Get-Content $(evil)", "codex exec task"):
            self.assertFalse(readonly_command(command), command)

    def test_jev_authorizes_owned_patch_for_auto_and_concrete_model(self):
        for model in ("jev/auto", "gpt-6-sol"):
            with self.subTest(model=model), mock.patch.dict(os.environ, self.env), \
                 mock.patch.object(self.hook, "ask_jev", return_value={"answers": {
                     "authorization": {"choice": "allow", "confidence": 0.99}}}) as ask:
                self.assertIsNone(self.hook.pre_tool(self.event(model=model)))
                ask.assert_called_once()
        receipts = [json.loads(line) for line in (self.directory / "guard.jsonl").read_text().splitlines()]
        self.assertEqual(len(receipts), 2)
        self.assertTrue(all(row["allowed"] for row in receipts))

    def test_deterministic_denial_precedes_jev(self):
        events = [self.event(value={"command": self.patch("other.txt")}),
                  self.event("exec_command", {"cmd": "touch other.txt"}),
                  self.event("Bash", {"command": "python -c 'pass'"}),
                  self.event("mcp__files__write", {"path": "owned.txt"}),
                  self.event("spawn_agent", {"message": "work"})]
        with mock.patch.dict(os.environ, self.env), mock.patch.object(self.hook, "ask_jev") as ask:
            for event in events:
                with self.subTest(tool=event["tool_name"]):
                    self.assertEqual(self.hook.pre_tool(event)["hookSpecificOutput"]["permissionDecision"], "deny")
            ask.assert_not_called()

    def test_jev_denial_and_unavailable_decision_block_patch(self):
        for decision in (None, {"answers": {"authorization": {"choice": "deny", "confidence": 1}}}):
            with mock.patch.dict(os.environ, self.env), mock.patch.object(self.hook, "ask_jev", return_value=decision):
                self.assertEqual(self.hook.pre_tool(self.event())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_tampered_manifest_fails_closed_even_for_read_tool(self):
        self.manifest.write_text('{}', encoding="utf-8")
        with mock.patch.dict(os.environ, self.env), mock.patch.object(self.hook, "ask_jev") as ask:
            result = self.hook.pre_tool(self.event("exec_command", {"cmd": "cat owned.txt"}))
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
            ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()
