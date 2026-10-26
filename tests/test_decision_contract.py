"""Offline response compatibility and decision-cache regression tests."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from integrations.jev import client
from server.decision_contract import compatible_model


class DecisionContractTests(unittest.TestCase):
    def test_v1_identities_and_latest_are_compatible(self):
        for name in ("jev-latest", "jev-1.13.0", "jev-1.14", "typesafe/jev-1.14.0-20261026"):
            self.assertTrue(compatible_model(name), name)
        for name in (None, {}, "jev-2.0", "gpt-6-luna", "other/jev-1.14", "jev-1.14 trailing"):
            self.assertFalse(compatible_model(name), name)

    def test_new_v1_response_and_invalid_cache_are_replaced_atomically(self):
        questions = {"task": {"type": "choice", "criteria": {"yes": "Matches"}}}
        response = {"model": "jev-1.14.0", "answers": {
            "task": {"type": "choice", "choice": "yes", "confidence": 0.99}}}
        transport = mock.Mock()
        transport.open.side_effect = lambda *_a, **_kw: io.BytesIO(json.dumps(response).encode())
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(client, "key_from_environment", return_value="synthetic"):
            root = Path(tmp)
            cache = root / "cache"
            result = client.judgment(root, "fixture", questions, cache, transport)
            self.assertEqual(result["model"], "jev-1.14.0", result)
            saved = next(cache.glob("jev-*.json"))
            for damaged in ("invalid cache", "[]", '{"model":"jev-1.14.0","answers":{}}'):
                saved.write_text(damaged)
                result = client.judgment(root, "fixture", questions, cache, transport)
                self.assertEqual(result["model"], "jev-1.14.0", result)
            self.assertEqual(json.loads(saved.read_text())["model"], "jev-1.14.0")
            cached = client.judgment(root, "fixture", questions, cache, transport)
            self.assertTrue(cached["cache_hit"])
            self.assertEqual(transport.open.call_count, 4)
            self.assertEqual(len(list(cache.iterdir())), 1)

    def test_cache_write_failure_does_not_discard_a_valid_answer(self):
        response = {"model": "jev-latest", "answers": {
            "task": {"type": "choice", "choice": "simple", "confidence": 0.99}}}
        transport = mock.Mock()
        transport.open.return_value = io.BytesIO(json.dumps(response).encode())
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(client, "key_from_environment", return_value="synthetic"), \
             mock.patch.object(client.tempfile, "NamedTemporaryFile", side_effect=PermissionError):
            result = client.judgment(Path(tmp), "fixture", {"task": client.TASK_QUESTION}, tmp, transport)
        self.assertEqual(result["answers"], response["answers"], result)

    def test_version_compatibility_does_not_relax_typed_answers(self):
        transport = mock.Mock()
        transport.open.return_value = io.BytesIO(b'{"model":"jev-1.14.0","answers":{}}')
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(client, "key_from_environment", return_value="synthetic"):
            result = client.judgment(Path(tmp), "fixture", {"task": client.TASK_QUESTION}, tmp, transport)
        self.assertEqual(result["unavailable"], "ValueError")
