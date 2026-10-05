import json
from pathlib import Path
import tempfile
import unittest
from harness import explain


class ExplainTests(unittest.TestCase):
    def test_latest_scope_and_privacy_with_partial_log(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "codex-router/jev-router-live.jsonl"
            path.parent.mkdir()
            rows = [{"cache_scope": "first", "model": "chosen", "prompt": "secret-prompt",
                     "attempts": [{"model": "sent", "response_model": "reported", "requested_effort": "high",
                                   "effective_effort": "low", "key": "secret-key", "body": "secret-body"}]},
                    {"cache_scope": "last", "model": "last-model", "attempts": []}]
            path.write_text("\n".join(json.dumps(row) for row in rows) + '\n{"partial":')
            self.assertEqual(explain.latest(root)["model"], "last-model")
            report = explain.latest(root, "first")
            self.assertEqual(report["observed_model"], "reported")
            self.assertEqual(report["reasoning_enforcement"], "unknown")
            self.assertNotIn("secret", json.dumps(report))
            self.assertFalse(explain.latest(root, "absent")["found"])

    def test_missing_log_does_not_create_profile(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(explain.latest(Path(root) / "absent")["found"])
            self.assertEqual(list(Path(root).iterdir()), [])
