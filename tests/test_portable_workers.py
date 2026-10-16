import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKERS = ROOT / "integrations" / "workers"
sys.path.insert(0, str(WORKERS))
import portable
from harness.core import _hook_entries
REAL_POPEN = subprocess.Popen


class PortableWorkersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.cwd = self.base / "workspace"
        self.cwd.mkdir()
        self.state = self.base / "private-state"
        self.home = self.base / "profile"
        self.home.mkdir()
        (self.home / "config.toml").write_text('[mcp_servers.jev-workers]\ncommand = "python"\n', encoding="utf-8")
        (self.home / "hooks.json").write_text(json.dumps({"hooks": _hook_entries(ROOT)}), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"JEV_PORTABLE_WORKER_STATE": str(self.state),
                                               "JEV_WORKER_STATE_DIR": str(self.state),
                                               "CODEX_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = {"request_id": "portable-1", "goal": "Answer one question", "cwd": str(self.cwd),
                     "allowed_files": [], "read_only": True}

    def test_invalid_ownership_and_capabilities_fail_closed(self):
        with mock.patch.object(portable.shutil, "which", return_value="/fake/codex"):
            for delta in ({"read_only": False, "allowed_files": []},
                          {"read_only": True, "allowed_files": ["answer.txt"]},
                          {"acceptance_commands": [["python", "test.py"]]},
                          {"capabilities": ["browser"]},
                          {"requested_model": "gpt-6-luna"}):
                with self.subTest(delta=delta), self.assertRaises((RuntimeError, ValueError)):
                    portable.submit({**self.task, **delta})
            with self.assertRaises(ValueError):
                portable.submit({**self.task, "allowed_files": ["../escape"]})
        self.assertFalse(self.state.exists() and any(self.state.glob("request-*")))

    def test_coding_task_requires_packaged_hook_and_supports_owned_files(self):
        task = {**self.task, "read_only": False, "allowed_files": ["answer.txt"],
                "acceptance_commands": [[sys.executable, "-c", "pass"]]}
        with mock.patch.object(portable.shutil, "which", return_value="/fake/codex"), \
             mock.patch.object(portable.subprocess, "Popen"):
            result = portable.submit(task)
        saved = portable._read(self.state / result["task_id"] / "task.json")
        self.assertEqual(saved["allowed_files"], ["answer.txt"])
        (self.home / "hooks.json").write_text('{}', encoding="utf-8")
        with mock.patch.object(portable.shutil, "which", return_value="/fake/codex"):
            with self.assertRaisesRegex(RuntimeError, "PreToolUse"):
                portable.submit({**task, "request_id": "missing-hook"})

    def test_mutable_runner_reports_real_changes_checks_and_guard(self):
        content = "ok \u041f\u0440\u0438\u0432\u0435\u0442"
        task = {**self.task, "read_only": False, "allowed_files": ["answer.txt"],
                "acceptance_commands": [[sys.executable, "-c", "from pathlib import Path; assert Path('answer.txt').read_text(encoding='utf-8') == " + repr(content)]]}
        with mock.patch.object(portable.shutil, "which", return_value="/fake/codex"), \
             mock.patch.object(portable.subprocess, "Popen"):
            task_id = portable.submit(task)["task_id"]
        directory = self.state / task_id

        def launch(command, **kwargs):
            self.assertIn("workspace-write", command)
            self.assertIn("features.hooks=true", command)
            self.assertIn("features.plugins=false", command)
            self.assertIn("features.apps=false", command)
            self.assertIn("features.skill_mcp_dependency_install=false", command)
            self.assertEqual(kwargs["env"]["JEV_WORKER_MANIFEST"], str(directory / "task.json"))
            (self.cwd / "answer.txt").write_text(content, encoding="utf-8")
            (directory / "answer.txt").write_text("Done", encoding="utf-8")
            (directory / "guard.jsonl").write_text(json.dumps({"task_id": task_id, "allowed": True}) + "\n", encoding="utf-8")
            child = mock.Mock(returncode=0)
            child.poll.return_value = 0
            return child

        with mock.patch.object(portable.subprocess, "Popen", side_effect=launch), \
             mock.patch.object(portable, "authorize_checks", return_value=True):
            # run_checks must launch a real Python check, not the model stub.
            real_run_checks = portable.run_checks
            def checks(task, directory):
                with mock.patch.object(portable.subprocess, "Popen", REAL_POPEN):
                    return real_run_checks(task, directory)
            with mock.patch.object(portable, "run_checks", side_effect=checks):
                portable._run(task_id)
        result = portable.collect(task_id)
        self.assertEqual(result["state"], "completed", result)
        self.assertTrue(result["guard_observed"])
        self.assertEqual(result["changed_files"], [str(self.cwd / "answer.txt")])
        self.assertEqual(result["checks"][0]["exit_code"], 0)
        self.assertTrue(Path(result["patch"]["patch_file"]).is_file())
        self.assertIn(content, Path(result["patch"]["patch_file"]).read_text(encoding="utf-8"))

    def test_mutable_success_without_guard_is_rejected(self):
        with mock.patch.object(portable.shutil, "which", return_value="/fake/codex"), \
             mock.patch.object(portable.subprocess, "Popen") as launch:
            task_id = portable.submit({**self.task, "read_only": False, "allowed_files": ["answer.txt"]})["task_id"]
            launch.return_value.poll.return_value = 0
            launch.return_value.returncode = 0
            (self.state / task_id / "answer.txt").write_text("Claimed success", encoding="utf-8")
            portable._run(task_id)
        self.assertEqual(portable.status(task_id)["reason"], "worker_guard_not_observed")

    def test_windows_sandbox_default_does_not_replace_configured_mode(self):
        for configured in (None, "elevated", "unelevated", "mxc"):
            with self.subTest(configured=configured):
                config = "" if configured is None else '[windows]\nsandbox = "' + configured + '"\n'
                (self.home / "config.toml").write_text(config, encoding="utf-8")
                with mock.patch.object(portable.shutil, "which", return_value="/fake/codex"), \
                     mock.patch.object(portable.subprocess, "Popen") as launch:
                    task_id = portable.submit({**self.task, "request_id": str(configured)})["task_id"]
                    launch.return_value.poll.return_value = 0
                    launch.return_value.returncode = 0
                    with mock.patch.object(portable.sys, "platform", "win32"):
                        portable._run(task_id)
                    command = launch.call_args.args[0]
                self.assertEqual('windows.sandbox="unelevated"' in command, configured is None)

    def test_failed_tools_denied_checks_and_failing_checks_cannot_complete(self):
        for case, expected in (("tool", "worker_tool_failed"), ("denied", "acceptance_denied_by_jev"),
                               ("check", "acceptance_failed")):
            with self.subTest(case=case):
                task = {**self.task, "request_id": case, "read_only": False, "allowed_files": [case + ".txt"],
                        "acceptance_commands": [[sys.executable, "-c", "raise SystemExit(7)"]]}
                with mock.patch.object(portable.shutil, "which", return_value="/fake/codex"), \
                     mock.patch.object(portable.subprocess, "Popen"):
                    task_id = portable.submit(task)["task_id"]
                directory = self.state / task_id
                def launch(command, **kwargs):
                    (self.cwd / (case + ".txt")).write_text("changed", encoding="utf-8")
                    (directory / "answer.txt").write_text("Claimed completion", encoding="utf-8")
                    (directory / "guard.jsonl").write_text(json.dumps({"task_id": task_id, "allowed": True}) + "\n", encoding="utf-8")
                    if case == "tool":
                        kwargs["stdout"].write(b'{"type":"item.completed","item":{"type":"file_change","status":"failed"}}\n')
                        kwargs["stdout"].flush()
                    child = mock.Mock(returncode=0)
                    child.poll.return_value = 0
                    return child
                with mock.patch.object(portable.subprocess, "Popen", side_effect=launch), \
                     mock.patch.object(portable, "authorize_checks", return_value=case != "denied"), \
                     mock.patch.object(portable, "run_checks", return_value=[{"exit_code": 7}]) as checks:
                    portable._run(task_id)
                result = portable.collect(task_id)
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["reason"], expected)
                self.assertEqual(checks.call_count, 1 if case == "check" else 0)

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
            self.assertIn('mcp_servers."jev-workers".enabled=false', args[0])
            self.assertEqual(Path(kwargs["stdout"].name).name, "events.jsonl")
            self.assertIs(kwargs["stderr"], portable.subprocess.DEVNULL)
            self.assertEqual(portable.collect(task_id)["summary"], "safe answer")

    def test_mcp_loads_without_macos_dependencies_and_dispatches(self):
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.dict(os.environ, {"CANVASTTY_AGENT_ID": ""}), \
             mock.patch.dict(sys.modules, {"luna_dispatch": None}):
            os.environ.pop("JEV_WORKER_BACKEND", None)
            spec = importlib.util.spec_from_file_location("portable_mcp_test", WORKERS / "mcp.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with mock.patch.object(module, "submit", return_value={"task_id": "test", "state": "queued"}):
                answer = module.handle({"method": "tools/call", "params": {"name": "submit", "arguments": self.task}})
            self.assertFalse(answer["isError"])
            self.assertEqual(json.loads(answer["content"][0]["text"])["task_id"], "test")
            self.assertEqual(len(module.handle({"method": "tools/list"})["tools"]), 5)

    def test_mcp_preserves_canvastty_backend_inside_its_isolation(self):
        native = mock.Mock()
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.dict(os.environ, {"CANVASTTY_AGENT_ID": "fixture"}), \
             mock.patch.dict(sys.modules, {"luna_dispatch": native}):
            os.environ.pop("JEV_WORKER_BACKEND", None)
            spec = importlib.util.spec_from_file_location("canvastty_mcp_test", WORKERS / "mcp.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.BACKEND, "canvastty")
            self.assertIs(module.submit, native.submit)

    def test_timeout_covers_child_that_never_reads_large_prompt(self):
        with mock.patch.object(portable.shutil, "which", return_value=sys.executable), \
             mock.patch.object(portable.subprocess, "Popen"):
            task_id = portable.submit({**self.task, "mandatory_instructions": "x" * 2000000})["task_id"]
        path = self.state / task_id / "task.json"
        task = portable._read(path)
        task["budget"]["max_seconds"] = 0.1
        portable._write(path, task)
        popen = portable.subprocess.Popen
        children = []

        def launch(command, **kwargs):
            child = popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            return child

        with mock.patch.object(portable.subprocess, "Popen", side_effect=launch):
            runner = threading.Thread(target=portable._run, args=(task_id,), daemon=True)
            runner.start()
            runner.join(timeout=3)
            bounded = not runner.is_alive()
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
            runner.join(timeout=3)
        self.assertTrue(bounded, "writing stdin bypassed the worker deadline")
        self.assertEqual(portable.status(task_id)["reason"], "worker_timeout")


if __name__ == "__main__":
    unittest.main()
