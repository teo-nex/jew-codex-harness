import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import smoke_reasoning_live as smoke


def completed(output=None):
    return {"status": "completed", "output": output or [{"type": "message", "content": [
        {"type": "output_text", "text": '{"result":4183}'}]}]}


class ReasoningSmokeTests(unittest.TestCase):
    def test_http_errors_do_not_echo_body_and_close_connection(self):
        connection = Mock()
        connection.getresponse.return_value.status = 401
        with patch.object(smoke.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(ValueError, "^gateway_http_401$"):
                smoke.request("synthetic", 20128, "GET", "/v1/models")
        connection.close.assert_called_once()

    def test_stream_deadline_cannot_be_extended_by_chunks(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read1.return_value = b"data"
        with patch.object(smoke.http.client, "HTTPConnection", return_value=connection), \
             patch.object(smoke.time, "monotonic", side_effect=[0, 0.5, 2]):
            with self.assertRaises(TimeoutError):
                smoke.request("synthetic", 20128, "GET", "/v1/models", timeout=1)
        self.assertEqual(response.read1.call_count, 1)
        connection.close.assert_called_once()

    def test_auth_formats_and_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.txt"
            for content in ('synthetic', '{"omniroute":{"key":"synthetic"}}', '{"key":"synthetic"}'):
                path.write_text(content)
                path.chmod(0o600)
                with patch.object(smoke, "protected_file", return_value=path):
                    self.assertEqual(smoke.load_auth_key(path), "synthetic")
            for content in ('', '{}', '{"key":false}', 'bad\nkey', '{"key":{}}'):
                path.write_text(content)
                with patch.object(smoke, "protected_file", return_value=path), self.assertRaises(ValueError):
                    smoke.load_auth_key(path)

    def test_reject_empty_failed_incomplete(self):
        for result in ({}, {"status": "completed", "output": []},
                       {"status": "failed", "output": [1]},
                       {"status": "incomplete", "output": [1]}):
            with self.subTest(result=result), self.assertRaises(ValueError):
                smoke.decode_response(json.dumps(result).encode())

    def test_sse_rejects_truncation_and_late_error(self):
        raw = b'data: ' + json.dumps({"type": "response.completed", "response": completed()}).encode() + b'\n\n'
        self.assertEqual(smoke.decode_response(raw)["status"], "completed")
        for bad in (b'data: {"type":"response.created"}\n\n', raw + b'data: {"type":"error"}\n\n'):
            with self.assertRaises(ValueError):
                smoke.decode_response(bad)

    def test_exact_output_required(self):
        smoke.validate_math(completed())
        for text in ('{"result":42}', 'done', '{"result":4183,"extra":1}'):
            with self.assertRaises(ValueError):
                smoke.validate_math(completed([{"type":"message","content":[{"type":"output_text","text":text}]}]))

    def test_real_shaping_and_no_fallback(self):
        profiles = {"test/model": {"supported_efforts": ["low", "high"]}}
        with patch.object(smoke, "request", return_value=json.dumps(completed()).encode()) as request:
            result = smoke.run_probe("synthetic", 20128, "test/model", "max", profiles)
        self.assertEqual(result["effective_effort"], "high")
        self.assertEqual(result["provider_control"], "NOT_VERIFIED")
        payload = request.call_args.args[4]
        self.assertEqual(payload["model"], "test/model")
        self.assertEqual(payload["reasoning"]["effort"], "high")
        self.assertNotIn("requested_effort", payload)
        self.assertEqual(request.call_count, 1)

    def test_two_turn_tool_loop(self):
        call = {"type":"function_call", "name":"multiply_numbers", "call_id":"synthetic_call",
                "arguments":'{"a":47,"b":89}'}
        with patch.object(smoke, "request", side_effect=[json.dumps(completed([call])).encode(),
                                                        json.dumps(completed()).encode()]) as request:
            result = smoke.run_probe("synthetic", 20128, "test/model", "high", {}, tool_loop=True)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[4]["input"][-1]["type"], "function_call_output")

    def test_tool_loop_requires_correct_call(self):
        with patch.object(smoke, "request", return_value=json.dumps(completed()).encode()), self.assertRaises(ValueError):
            smoke.run_probe("synthetic", 20128, "test/model", "high", {}, tool_loop=True)
