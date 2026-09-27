import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKERS = ROOT / "integrations" / "workers"
sys.path.insert(0, str(WORKERS))
import portable


class PortableWorkersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cwd = self.base / "workspace"
        self.cwd.mkdir()
        self.state = self.base / "private-state"
        patcher = mock.patch.dict(os.environ, {"JEV_PORTABLE_WORKER_STATE": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = {"request_id": "portable-1", "goal": "Answer one question", "cwd": str(self.cwd),
                     "allowed_files": [], "read_only": True}

    def test_mutable_and_unsafe_ownership_fail_closed(self):
        with mock.patch.object(portable.shutil, "which", return_value="/fake/codex"):
            for delta in ({"read_only": False, "allowed_files": ["answer.txt"]},
                          {"acceptance_commands": [["python", "test.py"]]},
                          {"capabilities": ["browser"]},
                          {"requested_model": "gpt-6-luna"}):
                with self.subTest(delta=delta), self.assertRaises((RuntimeError, ValueError)):
                    portable.submit({**self.task, **delta})
            with self.assertRaises(ValueError):
                portable.submit({**self.task, "allowed_files": ["../escape"]})
        self.assertFalse(self.state.exists() and any(self.state.glob("request-*")))

    def test_submit_idempotent_and_private_state(self):
        with mock.patch.object(portable.shutil, "which", return_value="/fake/codex"), \
             mock.patch.object(portable.subprocess, "Popen") as process:
            first = portable.submit(self.task)
            second = portable.submit(self.task)
            self.assertEqual(first["task_id"], second["task_id"])
            process.assert_called_once()
            self.assertEqual(first["worker_backend"], "portable_codex_cli")
            with self.assertRaises(ValueError):
                portable.submit({**self.task, "goal": "Different"})
        if os.name != "nt":
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.state / first["task_id"] / "task.json").stat().st_mode & 0o777, 0o600)

    def test_runner_uses_read_only_codex_and_suppresses_events(self):
        with mock.patch.object(portable.shutil, "which", return_value="/fake/codex"), \
             mock.patch.object(portable.subprocess, "Popen") as launch:
            task_id = portable.submit(self.task)["task_id"]
            launch.reset_mock()
            process = launch.return_value
            process.poll.return_value = 0
            process.returncode = 0
            answer = self.state / task_id / "answer.txt"
            answer.write_text("safe answer", encoding="utf-8")
            portable._run(task_id)
            args, kwargs = launch.call_args
            self.assertIn("--sandbox", args[0])
            self.assertIn("read-only", args[0])
            self.assertIn("jev/auto", args[0])
            self.assertIn("mcp_servers.jev-workers.enabled=false", args[0])
            self.assertIs(kwargs["stdout"], portable.subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], portable.subprocess.DEVNULL)
            self.assertEqual(portable.collect(task_id)["summary"], "safe answer")

    def test_mcp_loads_without_macos_dependencies_and_dispatches(self):
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.dict(sys.modules, {"luna_dispatch": None}):
            spec = importlib.util.spec_from_file_location("portable_mcp_test", WORKERS / "mcp.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with mock.patch.object(module, "submit", return_value={"task_id": "test", "state": "queued"}):
                answer = module.handle({"method": "tools/call", "params": {"name": "submit", "arguments": self.task}})
            self.assertFalse(answer["isError"])
            self.assertEqual(json.loads(answer["content"][0]["text"])["task_id"], "test")
            self.assertEqual(len(module.handle({"method": "tools/list"})["tools"]), 5)


if __name__ == "__main__":
    unittest.main()
