"""Offline checks for nonstream validation and policy-bound emergency routing."""

from contextlib import ExitStack
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jev_server as jev
from provider_ladder import Ladder


def handler():
    value = object.__new__(jev.Handler)
    value.wfile = io.BytesIO()
    value._response_started = False
    value._validated_stream = None
    value._attempts = []
    value.statuses = []
    value.send_response = value.statuses.append
    value.send_header = lambda *_: None
    value.end_headers = lambda: None
    return value


GOOD = {"id": "fixture", "status": "completed", "output": [
    {"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "fixture answer"}]}]}


class NonstreamValidationTests(unittest.TestCase):
    def run_ladder(self, responses):
        calls = []
        pending = iter(responses)

        class Response:
            status = 200
            headers = {}

            def __init__(self, body, content_type):
                self.body, self.content_type = body, content_type

            def getheader(self, name):
                return self.content_type if name == "Content-Type" else None

            def read(self, *_):
                return self.body

        class Connection:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, _method, _path, body, headers):
                calls.append(json.loads(body)["model"])

            def getresponse(self):
                return Response(*next(pending))

            def close(self):
                pass

        h = handler()
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            ladder = Ladder({"providers": [{"id": "first", "model": "vendor/first"},
                                            {"id": "second", "model": "vendor/second"}]},
                            Path(root) / "state.json")
            stack.enter_context(mock.patch.object(jev.http.client, "HTTPConnection", Connection))
            stack.enter_context(mock.patch.object(jev, "_provider_ladder", return_value=ladder))
            stack.enter_context(mock.patch.object(jev, "_omniroute_key", return_value="synthetic"))
            for name in ("log_line", "remember_cache_model", "remember_route_lease"):
                stack.enter_context(mock.patch.object(jev, name))
            h._serve_ladder({"model": "auto", "input": "fixture"}, "scope", jev.LUNA,
                            "low", None, {"step_type": "user_turn"}, None, False, False)
        return h, calls

    def test_invalid_json_and_failed_responses_retry_before_exposure(self):
        invalid = [b"not-json", b"[]", b"{}",
                   json.dumps({**GOOD, "status": "failed"}).encode(),
                   json.dumps({**GOOD, "status": "incomplete"}).encode(),
                   json.dumps({**GOOD, "output": "invalid"}).encode(),
                   json.dumps({**GOOD, "error": {"message": "private fixture"}}).encode()]
        for body in invalid:
            with self.subTest(body=body):
                h, calls = self.run_ladder([(body, "application/json"),
                                            (json.dumps(GOOD).encode(), "application/json")])
                self.assertEqual(calls, ["vendor/first", "vendor/second"])
                self.assertEqual(h.statuses, [200])
                self.assertEqual(json.loads(h.wfile.getvalue()), GOOD)

    def test_all_invalid_responses_return_safe_failure_not_http_200(self):
        h, calls = self.run_ladder([(b"private invalid body", "application/json")] * 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(h.statuses, [502])
        self.assertNotIn(b"private invalid body", h.wfile.getvalue())

    def test_post_terminal_sse_error_is_not_hidden_by_assembly(self):
        body = ("data: " + json.dumps({"type": "response.completed", "response": GOOD})
                + '\n\ndata: {"type":"response.failed","response":{"status":"failed"}}\n\n').encode()
        h, calls = self.run_ladder([(body, "text/event-stream"),
                                    (json.dumps(GOOD).encode(), "application/json")])
        self.assertEqual(len(calls), 2)
        self.assertEqual(h.statuses, [200])
        self.assertEqual(json.loads(h.wfile.getvalue()), GOOD)

    def test_malformed_sse_terminal_and_output_retry_before_signing(self):
        for response in ([], {**GOOD, "output": "invalid"}):
            with self.subTest(response=response):
                body = ("data: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode()
                h, calls = self.run_ladder([(body, "text/event-stream"),
                                            (json.dumps(GOOD).encode(), "application/json")])
                self.assertEqual(len(calls), 2)
                self.assertEqual(h.statuses, [200])
                self.assertEqual(json.loads(h.wfile.getvalue()), GOOD)

    def test_valid_sse_completion_is_assembled_without_retry(self):
        body = ("data: " + json.dumps({"type": "response.completed", "response": GOOD}) + "\n\ndata: [DONE]\n\n").encode()
        h, calls = self.run_ladder([(body, "text/event-stream")])
        self.assertEqual(calls, ["vendor/first"])
        self.assertEqual(h.statuses, [200])
        self.assertEqual(json.loads(h.wfile.getvalue()), GOOD)


class OffPolicyTests(unittest.TestCase):
    def run_off(self, policy, *, effort=None, native=True, quota=False, final=False,
                shadow=False, cap=False):
        payload = {"model": "auto", "input": "fixture", "prompt_cache_key": "off-fixture"}
        if effort:
            payload["reasoning"] = {"effort": effort}
        if final:
            payload["metadata"] = {"jev_phase": "final_review"}
        scope = jev.cache_scope(payload, "fixture")
        config = {"providers": [{"id": "free", "model": "vendor/free", "billing": "free"}],
                  "project_policies": {"/fixture": policy},
                  "project_scopes": {scope: "/fixture"}}
        if native:
            config["providers"].append({"id": "native", "transport": "native"})
        h = handler()
        h.path = "/v1/responses"
        h._body = lambda *_: copy.deepcopy(payload)
        calls = []

        def forward(body, *_args, **kwargs):
            calls.append((body["model"], kwargs.get("external"), kwargs.get("exact_route")))
            h._attempts.append({"model": body["model"], "status": 429 if quota else 200,
                                "terminal_type": None if quota else "response.completed"})
            return (429 if quota else 200), "json", "application/json", quota, (b'{}' if quota else None), None

        h._forward = forward
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            ladder = Ladder(config, Path(root) / "state.json")
            # Off must not depend on healthy sticky state.
            ladder.path.write_text("invalid sticky state")
            for name in ("OFF_PATH", "SHADOW_PATH", "DEBUG_PATH", "SIGNATURE_PATH", "DRY_STATE_PATH"):
                path = Path(root) / name
                if name == "OFF_PATH" or (shadow and name == "SHADOW_PATH"):
                    path.touch()
                stack.enter_context(mock.patch.object(jev, name, str(path)))
            stack.enter_context(mock.patch.dict(jev.os.environ,
                {"JEV_LADDER_MODE": "active", "JEV_DISABLE_AUTO_ASTRA": "1" if cap else "0"}))
            stack.enter_context(mock.patch.object(jev, "_provider_ladder", return_value=ladder))
            classifier = stack.enter_context(mock.patch.object(jev, "call_jev_routed"))
            discovery = stack.enter_context(mock.patch.object(jev, "fallback_candidates"))
            for name in ("log_line", "remember_cache_model", "remember_route_lease", "mark_native_dry"):
                stack.enter_context(mock.patch.object(jev, name))
            stack.enter_context(mock.patch.object(jev, "native_dry", return_value=None))
            h._post()
            classifier.assert_not_called()
            discovery.assert_not_called()
        return h, calls

    def test_off_uses_native_without_reading_provider_state(self):
        for policy in ({"native_only": True}, {"no_paid_fallback": True},
                       {"allowed_providers": ["free"]}):
            for effort in (None, "high"):
                with self.subTest(policy=policy, effort=effort):
                    h, calls = self.run_off(policy, effort=effort)
                    self.assertEqual(calls, [(jev.SOL, None, True)])
                    self.assertNotIn(503, h.statuses)

    def test_off_fails_closed_without_configured_native_route(self):
        h, calls = self.run_off({"no_paid_fallback": True}, native=False)
        self.assertEqual(calls, [])
        self.assertEqual(h.statuses, [503])

    def test_off_quota_cannot_discover_external_fallbacks(self):
        h, calls = self.run_off({"no_paid_fallback": True}, quota=True)
        self.assertEqual(calls, [(jev.SOL, None, True)])
        self.assertEqual(h.statuses, [429])

    def test_off_final_review_stays_astra_under_cap_and_shadow(self):
        _h, calls = self.run_off({"require_astra_final": True}, final=True, cap=True, shadow=True)
        self.assertEqual(calls, [(jev.ASTRA, None, True)])
