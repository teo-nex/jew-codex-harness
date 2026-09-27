"""A provider quota refusal retries the same canonical turn before any output."""
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import jev_server as jev
from provider_ladder import Ladder


CONFIG = {"plus_connection_id": "plus-id", "gemini_connection_ids": ["gemini-one", "gemini-two"],
          "gemini_model": "antigravity/gemini", "glm_model": "gonka/glm",
          "deepseek_model": "gonka/deepseek", "main_model": "gpt-6-luna"}


class LadderForwardTests(unittest.TestCase):
    def test_wire_summary_contains_only_event_types(self):
        raw = (b'event: response.output_text.delta\n'
               b'data: {"type":"response.output_text.delta","delta":"PRIVATE_SYNTHETIC_TEXT"}\n\n'
               b'data:[DONE]\n\n')
        summary = jev.sse_event_summary(raw)
        self.assertEqual(summary["counts"]["response.output_text.delta"], 1)
        self.assertEqual(summary["tail"][-1], "[DONE]")
        self.assertNotIn("PRIVATE_SYNTHETIC_TEXT", json.dumps(summary))

    def test_exposed_stream_failure_is_logged_without_same_call_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(CONFIG, Path(directory) / "state.json")
            handler = self.handler()
            handler._attempts = []
            calls = []
            def forward(payload, *args, **kwargs):
                calls.append(payload["model"])
                handler._attempts.append({"model": payload["model"], "status": 200,
                                          "completion": "response.failed",
                                          "terminal_type": "response.failed", "usage": None})
                return 200, "sse", "text/event-stream", False, None, None
            handler._forward = forward
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="synthetic"), \
                 mock.patch.object(jev, "log_line") as logged:
                handler._serve_ladder({"model": "jev/auto", "input": "same turn",
                                       "prompt_cache_key": "thread"}, "scope", jev.LUNA,
                                      "low", None, {"step_type": "user_turn"}, None,
                                      True, False)
            self.assertEqual(calls, ["codex/gpt-5.6-luna-low"])
            self.assertEqual(handler.wfile.getvalue(), b"")
            self.assertEqual(logged.call_args.args[0]["ladder_stage"], "stream_incomplete")

    def test_post_terminal_error_without_space_stays_unwritten(self):
        body = (b'event: response.created\n'
                b'data: {"type":"response.created","response":{"id":"r"}}\n\n'
                b'event: response.completed\n'
                b'data: {"type":"response.completed","response":{"id":"r","status":"completed","output":[]}}\n\n'
                b'event:error\n'
                b'data:{"type":"error","error":{"message":"synthetic"}}')
        self.assertEqual(jev.external_sse_issue(body), "post_terminal_event")
        class Response:
            status = 200
            def getheader(self, name):
                return "text/event-stream" if name == "Content-Type" else None
            def read(self, *args):
                return body
        class Connection:
            def __init__(self, *args, **kwargs):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        handler = self.handler()
        handler._attempts = []
        with mock.patch.object(jev.http.client, "HTTPConnection", Connection):
            status, _, _, _, unwritten, _ = handler._forward(
                {"model": "jev/auto", "input": "same turn"}, "/v1/responses", True,
                False, "", "codex/gpt-5.6-sol-high", external={"host": "127.0.0.1",
                  "port": 20128, "key": "synthetic", "account": "plus-id"})
        self.assertEqual(status, 502)
        self.assertIsNotNone(unwritten)
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_nonstream_external_error_before_completion_stays_unwritten(self):
        body = (b'event: error\n'
                b'data: {"type":"error","error":{"message":"temporary"}}\n\n'
                b'event: response.completed\n'
                b'data: {"type":"response.completed","response":{"id":"r","status":"completed","output":[]}}\n\n')
        class Response:
            status = 200
            headers = {}
            def getheader(self, name):
                return "text/event-stream" if name == "Content-Type" else None
            def read(self, *args):
                return body
        class Connection:
            def __init__(self, *args, **kwargs):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        handler = self.handler()
        handler._attempts = []
        with mock.patch.object(jev.http.client, "HTTPConnection", Connection):
            status, _, _, _, unwritten, _ = handler._forward(
                {"model": "jev/auto", "input": "same turn"}, "/v1/responses", False,
                False, "", "antigravity/gemini", external={"host": "127.0.0.1",
                  "port": 20128, "key": "synthetic", "account": "gemini-one"})
        self.assertEqual(status, 502)
        self.assertIsNotNone(unwritten)
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_error_then_completed_stays_unwritten_and_retries_on_gemini(self):
        def stream(*events):
            return b"".join(
                ("event: " + event["type"] + "\n" +
                 "data: " + json.dumps(event) + "\n\n").encode()
                for event in events
            )

        created = {"type": "response.created", "response": {"id": "resp_one", "status": "in_progress"}}
        completed = {"type": "response.completed", "response": {
            "id": "resp_one", "status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}]}]}}
        bad = stream(created, {"type": "error", "error": {"message": "upstream failed"}}, completed)
        good = stream(created, completed)
        bodies = iter((bad, good))
        calls = []

        class Response:
            status = 200
            def __init__(self, body):
                self.body = body
            def getheader(self, name):
                return "text/event-stream" if name == "Content-Type" else None
            def read(self, limit):
                return self.body

        class Connection:
            def __init__(self, host, port, timeout):
                pass
            def request(self, method, path, body, headers):
                calls.append(headers.get("X-OmniRoute-Connection"))
            def getresponse(self):
                return Response(next(bodies))
            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(CONFIG, Path(directory) / "state.json")
            handler = self.handler()
            handler._attempts = []
            written_status = []
            handler.send_response = written_status.append
            payload = {"model": "jev/auto", "input": "same turn", "prompt_cache_key": "thread"}
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="secret"), \
                 mock.patch.object(jev.http.client, "HTTPConnection", Connection), \
                 mock.patch.object(jev, "remember_cache_model"), \
                 mock.patch.object(jev, "remember_route_lease"), \
                 mock.patch.object(jev, "log_line"):
                handler._serve_ladder(payload, "scope", jev.LUNA, "low", None,
                                      {"step_type": "user_turn"}, None, True, False)
            self.assertEqual(calls, ["plus-id", "gemini-one"])
            self.assertEqual(written_status, [200])
            self.assertEqual(handler._attempts[0]["completion"], "invalid_external_stream:failure_event")
            self.assertEqual(handler._attempts[1]["terminal_type"], "response.completed")
            self.assertNotIn(b"upstream failed", handler.wfile.getvalue())
            self.assertIn(b"done", handler.wfile.getvalue())

    def test_gonka_bad_requests_still_reach_wally_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"scope": {"stage": "glm", "gemini_index": 0}}))
            ladder = Ladder({**CONFIG, "wally_model": "wally/glm-5.3-flash"}, path)
            handler = self.handler()
            calls = []
            def forward(payload, *args, **kwargs):
                calls.append(payload["model"])
                if payload["model"] != "wally/glm-5.3-flash":
                    handler._attempts.append({"model": payload["model"], "http_status": 400,
                                              "terminal_type": None, "usage": None})
                    return 400, "json", "application/json", False, b'{"error":"bad request"}', None
                handler._attempts.append({"model": payload["model"], "http_status": 200,
                                          "terminal_type": "response.completed", "usage": {"input_tokens": 10}})
                return 200, "sse", "text/event-stream", False, None, None
            handler._forward = forward
            payload = {"model": "jev/auto", "input": "Same canonical user turn", "prompt_cache_key": "scope"}
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="secret"), \
                 mock.patch.object(jev, "remember_cache_model"), \
                 mock.patch.object(jev, "remember_route_lease"), \
                 mock.patch.object(jev, "log_line"):
                handler._serve_ladder(payload, "scope", jev.LUNA, "low", None,
                                      {"step_type": "user_turn"}, None, True, False)
            self.assertEqual(calls, ["gonka/glm", "gonka/deepseek", "wally/glm-5.3-flash"])
            self.assertEqual(ladder.route("scope", jev.LUNA, "low")["stage"], "wally")

    def test_deepseek_failure_replays_same_turn_on_wally_before_main(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"scope": {"stage": "deepseek", "gemini_index": 0}}))
            ladder = Ladder({**CONFIG, "wally_model": "wally/glm-5.3-flash"}, path)
            handler = self.handler()
            calls = []
            def forward(payload, *args, **kwargs):
                calls.append((payload["model"], payload["input"], kwargs.get("validate_gonka")))
                if len(calls) == 1:
                    handler._attempts.append({"model": payload["model"], "http_status": 503,
                                              "terminal_type": None, "usage": None})
                    return 503, "json", "application/json", False, b'{"error":"unavailable"}', None
                handler._attempts.append({"model": payload["model"], "http_status": 200,
                                          "terminal_type": "response.completed", "usage": {"input_tokens": 10}})
                return 200, "sse", "text/event-stream", False, None, None
            handler._forward = forward
            payload = {"model": "jev/auto", "input": "Same canonical user turn", "prompt_cache_key": "scope"}
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="secret"), \
                 mock.patch.object(jev, "remember_cache_model"), \
                 mock.patch.object(jev, "remember_route_lease"), \
                 mock.patch.object(jev, "log_line"):
                handler._serve_ladder(payload, "scope", jev.LUNA, "low", None,
                                      {"step_type": "user_turn"}, None, True, False)
            self.assertEqual(calls, [("gonka/deepseek", "Same canonical user turn", True),
                                     ("wally/glm-5.3-flash", "Same canonical user turn", True)])
            self.assertEqual(ladder.route("scope", jev.LUNA, "low")["stage"], "wally")

    def handler(self):
        handler = jev.Handler.__new__(jev.Handler)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        return handler

    def test_plus_quota_replays_on_first_gemini_without_changing_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(CONFIG, Path(directory) / "state.json")
            handler = self.handler()
            calls = []
            def forward(payload, *args, **kwargs):
                calls.append({"model": payload["model"], "input": payload["input"],
                              "account": (kwargs.get("external") or {}).get("account")})
                if len(calls) == 1:
                    handler._attempts.append({"model": payload["model"], "status": 429,
                                              "terminal_type": None, "usage": None})
                    return 429, "json", "application/json", True, b'{"error":{"code":"usage_limit_reached"}}', None
                handler._attempts.append({"model": payload["model"], "status": 200,
                                          "terminal_type": "response.completed", "usage": {"input_tokens": 10}})
                return 200, "sse", "text/event-stream", False, None, None
            handler._forward = forward
            payload = {"model": "jev/auto", "input": "The exact same user request",
                       "reasoning": {"effort": "high"}, "prompt_cache_key": "thread-one"}
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="secret"), \
                 mock.patch.object(jev, "remember_cache_model"), \
                 mock.patch.object(jev, "remember_route_lease"), \
                 mock.patch.object(jev, "log_line"):
                handler._serve_ladder(payload, "scope-one", jev.LUNA, "low", None,
                                      {"step_type": "user_turn"}, None, True, False)
            self.assertEqual([row["model"] for row in calls],
                             ["codex/gpt-5.6-luna-low", "antigravity/gemini"])
            self.assertEqual([row["account"] for row in calls], ["plus-id", "gemini-one"])
            self.assertEqual(calls[0]["input"], calls[1]["input"])
            self.assertEqual(ladder.route("scope-one", jev.SOL, "high")["account"], "gemini-one")

    def test_gemini_account_failure_advances_only_one_account(self):
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(CONFIG, Path(directory) / "state.json")
            plus = ladder.route("scope", jev.LUNA, "low")
            ladder.advance("scope", plus, "quota")
            first = ladder.route("scope", jev.LUNA, "low")
            self.assertTrue(ladder.advance("scope", first, "unavailable"))
            self.assertEqual(ladder.route("scope", jev.LUNA, "low")["account"], "gemini-two")

    def test_nonquota_bad_request_uses_main_for_call_without_dropping_gemini_affinity(self):
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(CONFIG, Path(directory) / "state.json")
            ladder.advance("scope", ladder.route("scope", jev.LUNA, "low"), "quota")
            handler = self.handler()
            calls = []
            def forward(payload, *args, **kwargs):
                account = (kwargs.get("external") or {}).get("account")
                calls.append((payload["model"], account))
                if account == "gemini-one":
                    handler._attempts.append({"model": payload["model"], "http_status": 400,
                                              "terminal_type": None, "usage": None})
                    return 400, "json", "application/json", False, b'{"error":{"message":"invalid request"}}', None
                handler._attempts.append({"model": payload["model"], "http_status": 200,
                                          "terminal_type": "response.completed", "usage": {"input_tokens": 10}})
                return 200, "sse", "text/event-stream", False, None, None
            handler._forward = forward
            payload = {"model": "jev/auto", "input": "Use browser tool", "prompt_cache_key": "thread-one"}
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="secret"), \
                 mock.patch.object(jev, "remember_cache_model"), \
                 mock.patch.object(jev, "remember_route_lease"), \
                 mock.patch.object(jev, "log_line"):
                handler._serve_ladder(payload, "scope", jev.LUNA, "low", None,
                                      {"step_type": "tool"}, None, True, False)
            self.assertEqual(calls, [("antigravity/gemini", "gemini-one"), ("gpt-6-luna", None)])
            self.assertEqual(ladder.route("scope", jev.LUNA, "low")["account"], "gemini-one")

    def test_tool_search_history_skips_incompatible_external_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder(CONFIG, Path(directory) / "state.json")
            handler = self.handler()
            calls = []
            def forward(payload, *args, **kwargs):
                calls.append((payload["model"], kwargs.get("external")))
                handler._attempts.append({"model": payload["model"], "http_status": 200,
                                          "terminal_type": "response.completed", "usage": {"input_tokens": 10}})
                return 200, "sse", "text/event-stream", False, None, None
            handler._forward = forward
            payload = {"model": "jev/auto", "input": [{"role": "user", "content": "Read page"},
                       {"type": "tool_search_output", "execution": "client", "tools": []}],
                       "prompt_cache_key": "thread-one"}
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="secret"), \
                 mock.patch.object(jev, "remember_cache_model"), \
                 mock.patch.object(jev, "remember_route_lease"), \
                 mock.patch.object(jev, "log_line"):
                handler._serve_ladder(payload, "scope", jev.LUNA, "low", None,
                                      {"step_type": "tool_step"}, None, True, False)
            self.assertEqual(calls, [("gpt-6-luna", None)])
            self.assertEqual(ladder.route("scope", jev.LUNA, "low")["stage"], "plus")


if __name__ == "__main__":
    unittest.main()
