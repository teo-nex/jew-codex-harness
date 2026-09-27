import json
import hashlib
from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import live_verify


class LiveVerifyTests(unittest.TestCase):
    def test_accepts_completed_turn_with_exact_marker(self):
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": live_verify.MARKER}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        ]
        ok, reason = live_verify._parse_events("\n".join(map(json.dumps, events)))
        self.assertTrue(ok)
        self.assertIn("expected marker", reason)

    def test_rejects_wrong_or_incomplete_response_without_echoing_it(self):
        raw = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "SECRET RAW"}})
        ok, reason = live_verify._parse_events(raw)
        self.assertFalse(ok)
        self.assertNotIn("SECRET RAW", reason)

    def test_runs_one_ephemeral_readonly_call_in_empty_temp_cwd(self):
        with tempfile.TemporaryDirectory() as home:
            thread = "synthetic-thread"
            completed = mock.Mock(returncode=0, stdout="\n".join([
                json.dumps({"type": "thread.started", "thread_id": thread}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": live_verify.MARKER}}),
                json.dumps({"type": "turn.completed"}),
            ]), stderr="credential must not escape")

            def run(command, *, cwd, env, **kwargs):
                self.assertEqual(command[1:5], ["exec", "-m", "jev/auto", "--ephemeral"])
                self.assertIn("--json", command)
                self.assertIn("read-only", command)
                self.assertIn("features.multi_agent=false", command)
                self.assertEqual(Path(env["CODEX_HOME"]), Path(home).resolve())
                self.assertEqual(cwd, command[command.index("-C") + 1])
                self.assertFalse(list(Path(cwd).iterdir()))
                self.assertLessEqual(kwargs["timeout"], 120)
                session = Path(home) / "jev-global/sessions" / (hashlib.sha256(thread.encode()).hexdigest()[:24] + ".json")
                session.parent.mkdir(parents=True)
                session.write_text(json.dumps({"active_goal": live_verify.PROMPT}))
                return completed

            with mock.patch.object(live_verify.shutil, "which", return_value="/usr/bin/codex"), \
                 mock.patch.object(live_verify.subprocess, "run", side_effect=run) as run_mock:
                result = live_verify.verify_live(Path(home))
            self.assertTrue(result["ok"], result)
            self.assertEqual(run_mock.call_count, 1)
            self.assertNotIn("credential", str(result))

    def test_live_response_without_loaded_hook_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as home:
            events = "\n".join(map(json.dumps, [
                {"type": "thread.started", "thread_id": "missing-hook"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": live_verify.MARKER}},
                {"type": "turn.completed"},
            ]))
            with mock.patch.object(live_verify.shutil, "which", return_value="codex"), \
                 mock.patch.object(live_verify.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=events)):
                result = live_verify.verify_live(Path(home))
        self.assertFalse(result["ok"])
        self.assertIn("hook did not load", result["reason"])

    def test_decision_smoke_requires_typed_response_through_loopback(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            manifest = root / "jev-harness/manifest.json"
            manifest.parent.mkdir()
            manifest.write_text('{"port": 4319}')
            key = root / "codex-router/generic-provider-credentials/jev.key"
            key.parent.mkdir(parents=True)
            key.write_text("fixture-local-key")
            key.chmod(0o600)
            response = mock.MagicMock(status=200)
            response.read.return_value = json.dumps({"model": "jev-1.13.0", "answers": {
                "marker": {"type": "choice", "choice": "present"}}}).encode()
            with mock.patch.object(live_verify.urllib.request, "build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value = response
                result = live_verify.verify_decision(root)
            self.assertTrue(result["ok"], result)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:4319/ask")
            self.assertNotIn("fixture-local-key", str(result))

    def test_manual_model_write_requires_matching_pretool_hook_receipt(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            thread = "manual-synthetic-thread"
            session = hashlib.sha256(thread.encode()).hexdigest()[:24]
            events = json.dumps({"type": "thread.started", "thread_id": thread}) + "\n"

            def run(command, *, cwd, env, **kwargs):
                self.assertIn("workspace-write", command)
                self.assertIn('approval_policy="never"', command)
                (Path(cwd) / "proof.txt").write_bytes((live_verify.MANUAL_MARKER + "\n").encode())
                state = root / "jev-global"
                state.mkdir()
                (state / "usage.jsonl").write_text(json.dumps({
                    "kind": "pre_tool_seen", "session": session,
                    "guard_mode": "manual", "model": "gpt-6-luna"}) + "\n")
                return mock.Mock(returncode=0, stdout=events)

            with mock.patch.object(live_verify.shutil, "which", return_value="codex"), \
                 mock.patch.object(live_verify.subprocess, "run", side_effect=run):
                result = live_verify.verify_manual(root, "gpt-6-luna")
            self.assertTrue(result["ok"], result)

    def test_manual_model_rejects_auto_alias(self):
        self.assertFalse(live_verify.verify_manual(Path("/tmp/profile"), "jev/auto")["ok"])

    def test_cost_telemetry_prices_native_route_and_flags_unknown_usage(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            log = root / "codex-router/jev-router-live.jsonl"
            log.parent.mkdir()
            row = {"at": datetime.now().astimezone().isoformat(), "gate": "jev",
                   "attempts": [{"model": "gpt-6-luna", "http_status": 200,
                                  "terminal_type": "response.completed", "speed": "default",
                                  "usage": {"input_tokens": 1000, "cached_input_tokens": 0,
                                            "output_tokens": 100}}]}
            log.write_text(json.dumps(row) + "\n")
            result = live_verify.verify_cost_telemetry(root)
            self.assertTrue(result["ok"])
            self.assertEqual(result["priced_attempts"], 1)
            row["attempts"][0]["usage"] = None
            log.write_text(json.dumps(row) + "\n")
            missing = live_verify.verify_cost_telemetry(root)
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["usage_unknown"], 1)

    def test_timeout_is_safe_and_clear(self):
        with mock.patch.object(live_verify.shutil, "which", return_value="codex"), \
             mock.patch.object(live_verify.subprocess, "run", side_effect=live_verify.subprocess.TimeoutExpired("codex", 120)):
            result = live_verify.verify_live(Path("/tmp/profile"))
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["reason"])


if __name__ == "__main__":
    unittest.main()
