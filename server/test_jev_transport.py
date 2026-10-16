"""Node transport keeps TypeSafe credentials off argv and returns typed answers."""
import io
import json
import subprocess
import unittest
from unittest import mock

import jev_server as jev


class JevTransportTests(unittest.TestCase):
    def test_node_transport_uses_stdin_and_parses_response(self):
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps({
            "model": "jev-1.13.0", "answers": {"choice": {"choice": "open"}}}), stderr="")
        with mock.patch.object(jev, "_node_binary", return_value="/opt/homebrew/bin/node"), \
             mock.patch.object(jev.subprocess, "run", return_value=response) as run:
            result = jev.call_jev("private-key", {"task": "open"}, {"choice": {"type": "choice"}})
        self.assertEqual(result["answers"]["choice"]["choice"], "open")
        args, kwargs = run.call_args
        self.assertNotIn("private-key", " ".join(args[0]))
        self.assertEqual(json.loads(kwargs["input"])["key"], "private-key")

    def test_node_provider_error_never_echoes_secret(self):
        response = subprocess.CompletedProcess([], 1, stdout="", stderr=json.dumps(
            {"stage": "transport", "reason": "http_error", "http_status": 403}) + "\n")
        with mock.patch.object(jev, "_node_binary", return_value="/opt/homebrew/bin/node"), \
             mock.patch.object(jev.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "provider HTTP 403") as error:
                jev.call_jev("private-key", "state", {"choice": {"type": "choice"}})
        self.assertNotIn("private-key", str(error.exception))
        self.assertEqual(error.exception.details, {
            "jev_error_stage": "transport", "jev_error_reason": "http_error",
            "jev_error_http_status": 403})

    def test_python_http_and_connection_errors_keep_only_safe_metadata(self):
        cases = [
            (jev.urllib.error.HTTPError("https://private.invalid/key", 429,
                "private-body", {}, io.BytesIO(b"private-provider-body")), "http_error", 429),
            (jev.urllib.error.URLError("private-url-and-key"), "connection_failed", None),
            (jev.urllib.error.URLError(TimeoutError("private-timeout")), "timeout", None),
        ]
        for failure, reason, status in cases:
            with self.subTest(reason=reason), \
                 mock.patch.object(jev, "_node_binary", return_value=None), \
                 mock.patch.object(jev.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = failure
                with self.assertRaises(RuntimeError) as caught:
                    jev.call_jev("private-key", {"task": "private-prompt"})
            expected = {"jev_error_stage": "transport", "jev_error_reason": reason}
            if status:
                expected["jev_error_http_status"] = status
            self.assertEqual(caught.exception.details, expected)
            self.assertNotIn("private", str(caught.exception))
            if isinstance(failure, jev.urllib.error.HTTPError):
                self.assertTrue(failure.closed)

    def test_unexpected_openrouter_identity_and_redirect_are_safe(self):
        with mock.patch.dict(jev.os.environ, {"JEV_DECISION_PROVIDER": "openrouter"}), \
             mock.patch.object(jev, "call_jev", return_value={"model": "private-key", "answers": {}}):
            with self.assertRaises(RuntimeError) as caught:
                jev.call_jev_routed("private-key", "private-prompt")
        self.assertEqual(caught.exception.details, {
            "jev_error_stage": "response_validation", "jev_error_reason": "unexpected_model"})
        self.assertNotIn("private", str(caught.exception))
        with self.assertRaises(RuntimeError) as caught:
            jev._NoJevRedirect().redirect_request(None, None, 302, "private", {}, "https://private")
        self.assertEqual(caught.exception.details["jev_error_reason"], "redirect_refused")

    def test_openrouter_uses_decisions_contract_and_normalizes_local_identity(self):
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps({
            "model": "typesafe/jev-1.13-20260917",
            "answers": {"choice": {"type": "choice", "choice": "open", "confidence": 1}},
            "usage": {"input_tokens": 15, "output_tokens": 3}}), stderr="")
        with mock.patch.dict(jev.os.environ, {"JEV_DECISION_PROVIDER": "openrouter"}), \
             mock.patch.object(jev, "_node_binary", return_value="/fake/node"), \
             mock.patch.object(jev.subprocess, "run", return_value=response) as run:
            result = jev.call_jev_routed("private-key", {"task": "open"},
                                         {"choice": {"type": "choice"}})
        envelope = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(envelope["provider"], "openrouter")
        self.assertEqual(envelope["body"]["model"], "typesafe/jev-1.13")
        self.assertNotIn("private-key", " ".join(run.call_args.args[0]))
        self.assertEqual(result["model"], jev.MODEL)
        self.assertEqual(result["answers"]["choice"]["choice"], "open")

    def test_python_fallback_targets_only_openrouter_decisions_api(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": {"choice": {"type": "choice", "choice": "open"}}}).encode()
        with mock.patch.dict(jev.os.environ, {"JEV_DECISION_PROVIDER": "openrouter"}), \
             mock.patch.object(jev, "_node_binary", return_value=None), \
             mock.patch.object(jev.urllib.request, "build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value = response
            jev.call_jev_routed("private-key", "synthetic", {"choice": {"type": "choice"}})
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(json.loads(request.data)["model"], "typesafe/jev-1.13")
        self.assertTrue(any(isinstance(handler, jev._NoJevRedirect) for handler in build.call_args.args))


if __name__ == "__main__":
    unittest.main()
