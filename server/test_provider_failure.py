import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jev_server as jev
from provider_failure import classify_failure
from provider_ladder import Ladder


CONFIG = {"providers": [{"id": "first", "model": "vendor/a", "connection_ids": ["a1", "a2"]},
                         {"id": "second", "model": "vendor/b"},
                         {"id": "native", "transport": "native"}]}


class FailureTests(unittest.TestCase):
    def test_classes_do_not_echo_bodies_and_bad_request_is_not_retryable(self):
        for status, category in ((400, "bad_request"), (422, "bad_request"), (401, "auth"),
                                 (403, "auth"), (402, "quota"), (429, "quota"),
                                 (404, "model_unavailable"), (503, "unavailable"), (504, "timeout")):
            row = classify_failure(status, body=b'{"error":{"message":"private-body"}}')
            self.assertEqual(row["class"], category)
            self.assertNotIn("private", str(row))
        self.assertFalse(classify_failure(400)["retryable"])
        self.assertEqual(classify_failure(429, body=b'{"error":{"code":"rate_limit"}}')["class"], "rate_limit")
        self.assertFalse(classify_failure(504, attempt={"timeout_phase": "total"})["retryable"])

    def test_account_auth_cooldown_is_shared_but_other_account_and_route_stay_healthy(self):
        with tempfile.TemporaryDirectory() as root:
            ladder = Ladder(CONFIG, Path(root) / "state.json")
            with mock.patch("provider_ladder.time.time", return_value=1000):
                first = ladder.route("one", "gpt-6-sol", "high")
                ladder.advance("one", first, "auth", cooldown_seconds=900)
                other = ladder.route("other", "gpt-6-sol", "low")
                self.assertEqual(other["account"], "a2")
                ladder.advance("other", other, "unavailable", cooldown_seconds=30)
                route = ladder.route("fresh", "gpt-6-sol", "low")
                self.assertEqual(route["stage"], "second")
            with mock.patch("provider_ladder.time.time", return_value=1031):
                self.assertEqual(ladder.route("fresh", "gpt-6-sol", "low")["account"], "a2")
            with mock.patch("provider_ladder.time.time", return_value=1901):
                self.assertEqual(ladder.route("new", "gpt-6-sol", "low")["account"], "a1")

    def test_bad_payload_does_not_exhaust_or_retry_healthy_accounts(self):
        with tempfile.TemporaryDirectory() as root:
            ladder = Ladder(CONFIG, Path(root) / "state.json")
            handler = object.__new__(jev.Handler)
            handler.wfile = io.BytesIO()
            handler.send_response = mock.Mock()
            handler.send_header = mock.Mock()
            handler.end_headers = mock.Mock()
            def forward(*args, **kwargs):
                handler._attempts.append({"status": 400, "completion": "http_error"})
                return 400, "json", "application/json", False, b'{"error":{"message":"bad payload"}}', None
            handler._forward = mock.Mock(side_effect=forward)
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="fixture"), \
                 mock.patch.object(jev, "log_line"):
                handler._serve_ladder({"input": "fixture"}, "scope", "gpt-6-sol", "high",
                                      None, {"step_type": "user_turn"}, None, True, False)
            self.assertEqual(handler._forward.call_count, 1)
            self.assertEqual(ladder.route("scope", "gpt-6-sol", "high")["account"], "a1")
            self.assertEqual(handler._attempts[0]["failure_class"], "bad_request")

    def test_exhausted_thread_can_recover_secondary_before_primary(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"providers": [{"id": "first", "model": "a"}, {"id": "second", "model": "b"}]}
            ladder = Ladder(config, Path(root) / "state.json")
            with mock.patch("provider_ladder.time.time", return_value=1000):
                route = ladder.route("scope", "gpt-6-sol", "high")
                ladder.advance("scope", route, "auth", cooldown_seconds=900)
                route = ladder.route("scope", "gpt-6-sol", "high")
                ladder.advance("scope", route, "timeout", cooldown_seconds=30)
                self.assertEqual(ladder.route("scope", "gpt-6-sol", "high")["stage"], "exhausted")
            with mock.patch("provider_ladder.time.time", return_value=1031):
                self.assertEqual(ladder.route("scope", "gpt-6-sol", "high")["stage"], "second")

    def test_missing_model_skips_all_accounts_not_other_destination(self):
        with tempfile.TemporaryDirectory() as root:
            ladder = Ladder(CONFIG, Path(root) / "state.json")
            with mock.patch("provider_ladder.time.time", return_value=1000):
                route = ladder.route("one", "gpt-6-sol", "high")
                ladder.advance("one", route, "model_unavailable", cooldown_seconds=300)
                self.assertEqual(ladder.route("fresh", "gpt-6-sol", "high")["stage"], "second")
