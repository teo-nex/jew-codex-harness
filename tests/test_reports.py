import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from integrations.workers.portable import state_root as portable_worker_state_root


REPO = Path(__file__).resolve().parents[1]


def load_module(name):
    path = REPO / "integrations/jev" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api_cost = load_module("report_api_cost")
efficiency = load_module("report_efficiency")
verify_worker = load_module("verify_worker")


class PortableReportPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.codex = self.base / "codex-profile"
        self.state = self.base / "private-state"
        self.workers = self.base / "private-workers"

    def test_paths_follow_codex_and_private_state_environment(self):
        with mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.codex),
            "JEV_STATE_DIR": str(self.state),
            "JEV_WORKER_STATE_DIR": str(self.workers),
        }, clear=False):
            self.assertEqual(api_cost.default_paths(), (
                self.codex.resolve() / "codex-router/jev-router-live.jsonl",
                self.state.resolve() / "cost", self.state.resolve() / "cost/gonka-prices.json"))
            self.assertEqual(efficiency.default_paths(), (
                self.codex.resolve() / "codex-router/jev-router-live.jsonl",
                self.codex.resolve() / "jev-global/usage.jsonl", self.workers.resolve()))
            self.assertEqual(verify_worker.worker_state_dir(), self.workers.resolve())
            self.assertEqual(verify_worker.codex_home(), self.codex.resolve())
        self.assertFalse((REPO / ".runtime").exists())

    def test_worker_state_defaults_outside_repository_and_inherits_state_dir(self):
        with mock.patch.dict(os.environ, {"JEV_STATE_DIR": str(self.state)}, clear=True):
            self.assertEqual(efficiency.worker_state_dir(), self.state.resolve() / "workers")
            self.assertEqual(verify_worker.worker_state_dir(), self.state.resolve() / "workers")
            self.assertFalse(efficiency.worker_state_dir().is_relative_to(REPO))

    def test_default_worker_state_matches_portable_backend(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex)}, clear=True):
            expected = self.codex.resolve() / "jev-harness/workers"
            self.assertEqual(efficiency.worker_state_dir(), expected)
            self.assertEqual(verify_worker.worker_state_dir(), expected)
            self.assertEqual(portable_worker_state_root(), expected)

    @unittest.skipUnless(sys.platform == "darwin", "CanvasTTY worker is macOS-only")
    def test_macos_worker_uses_the_same_default_state(self):
        env = {**os.environ, "CODEX_HOME": str(self.codex)}
        for key in ("JEV_STATE_DIR", "JEV_WORKER_STATE_DIR", "JEV_PORTABLE_WORKER_STATE"):
            env.pop(key, None)
        code = ("import sys; sys.path.insert(0, 'integrations/workers'); "
                "from luna_dispatch import STATE; print(STATE)")
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                                capture_output=True, text=True, check=True)
        self.assertEqual(Path(result.stdout.strip()), self.codex / "jev-harness/workers")

    def test_verifier_writes_only_into_selected_private_worker_state(self):
        task_id = "12345678-1234-4234-8234-123456789abc"
        directory = self.workers / task_id
        directory.mkdir(parents=True)
        (directory / "task.json").write_text("{}", encoding="utf-8")
        (directory / "exit.json").write_text(json.dumps({"state": "failed"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"JEV_WORKER_STATE_DIR": str(self.workers)}, clear=False):
            result = verify_worker.verify(task_id)
        self.assertEqual(result["reason"], "worker_not_completed")
        self.assertTrue((directory / "acceptance.json").is_file())
        self.assertFalse((REPO / ".runtime").exists())

    def test_reports_keep_explicit_inputs_and_do_not_create_repo_state(self):
        routing = self.base / "routing.jsonl"
        hook = self.base / "hook.jsonl"
        workers = self.base / "workers"
        routing.write_text("", encoding="utf-8")
        hook.write_text("", encoding="utf-8")
        report = efficiency.report(routing_log=routing, hook_log=hook, workers=workers)
        self.assertEqual(report["ladder_calls"], 0)
        priced = api_cost.report(routing_log=routing, gonka_prices={})
        self.assertEqual(priced["routed_calls"], 0)
        self.assertFalse((REPO / ".runtime").exists())

    def test_api_cost_counts_native_and_ladder_calls_but_not_future_rows(self):
        log = self.base / "routing.jsonl"
        now = datetime(2026, 9, 24, 19, 51, tzinfo=ZoneInfo("Europe/Moscow"))
        attempt = {"model": "gpt-6-luna", "http_status": 200,
                   "terminal_type": "response.completed", "speed": "default",
                   "usage": {"input_tokens": 1000, "cached_input_tokens": 0,
                             "output_tokens": 100}}
        rows = [
            {"at": "2026-09-24T18:00:00+03:00", "gate": "jev", "attempts": [attempt]},
            {"at": "2026-09-24T18:05:00+03:00", "ladder_stage": "main", "attempts": [attempt]},
            {"at": "2026-09-24T20:00:00+03:00", "ladder_stage": "main", "attempts": [attempt]},
            {"at": "2026-09-24T18:10:00+03:00", "kind": "unrelated", "attempts": [attempt]},
        ]
        log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        result = api_cost.report(24, routing_log=log, gonka_prices={}, now=now)
        self.assertEqual(result["routed_calls"], 2)
        self.assertEqual(result["totals"]["attempts"], 2)
        self.assertEqual(result["totals"]["completed"], 2)
        self.assertGreater(float(result["totals"]["routed_low"]), 0)
        self.assertEqual(len(result["source_rows_sha256"]), 64)
        self.assertEqual(result["priced_attempts"], 2)
        summary = api_cost.render_summary(result, report_sha256="synthetic-digest")
        self.assertIn("Фактически выбранные модели", summary)
        self.assertIn("gpt-6-sol", summary)
        self.assertIn("gpt-6-astra", summary)
        self.assertIn("Охват: 2/2", summary)
        self.assertIn("Оценка частичная", summary)  # fixture starts after window start
        self.assertIn("synthetic-digest", summary)

    def test_saved_report_reprints_without_fetching_prices(self):
        saved = self.base / "report.json"
        saved.write_text(json.dumps({"as_of": "2026-09-24T19:51:00+03:00", "window_hours": 24,
            "totals": {"attempts": 0, "usage_unknown": 0, "unpriced": 0},
            "retained_log_covers_window_start": False, "coverage": "no_priced_data",
            "price_stale": False}))
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["report_api_cost", "--from-report", str(saved)]), \
             mock.patch.object(api_cost, "live_gonka_prices") as prices, redirect_stdout(output):
            api_cost.main()
        prices.assert_not_called()
        self.assertIn("Нет оценённых вызовов", output.getvalue())
        self.assertNotIn("$0.00", output.getvalue())
        self.assertNotIn("Оценка частичная", output.getvalue())

    def test_native_only_cost_report_needs_no_gonka_price_request(self):
        log = self.base / "native.jsonl"
        now = datetime(2026, 9, 24, 19, 51, tzinfo=ZoneInfo("Europe/Moscow"))
        log.write_text(json.dumps({"at": "2026-09-24T19:50:00+03:00", "gate": "jev",
                                   "attempts": [{"model": "gpt-6-luna"}]}) + "\n")
        self.assertFalse(api_cost.gonka_used(log, 24, now=now))
        log.write_text(json.dumps({"at": "2026-09-24T19:50:00+03:00", "ladder_stage": "glm",
                                   "attempts": [{"model": "gonkagate/zai-org/glm-5.3-flash"}]}) + "\n")
        self.assertTrue(api_cost.gonka_used(log, 24, now=now))

    def test_provisional_price_is_visible_in_summary(self):
        log = self.base / "wally.jsonl"
        now = datetime(2026, 9, 24, 19, 51, tzinfo=ZoneInfo("Europe/Moscow"))
        log.write_text(json.dumps({"at": "2026-09-24T19:50:00+03:00", "ladder_stage": "wally",
            "attempts": [{"model": "wally/glm-5.3-flash", "http_status": 200,
                           "terminal_type": "response.completed", "speed": "default",
                           "usage": {"input_tokens": 1000, "cached_input_tokens": 0,
                                     "output_tokens": 100}}]}) + "\n")
        result = api_cost.report(24, routing_log=log, gonka_prices={}, now=now)
        self.assertEqual(result["provisional_models"], ["wally/glm-5.3-flash"])
        self.assertEqual(result["coverage"], "partial")
        self.assertIn("Предварительные тарифы", api_cost.render_summary(result))


if __name__ == "__main__":
    unittest.main()
