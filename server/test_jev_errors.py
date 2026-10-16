"""Jev diagnostics expose only enum metadata, including adversarial failures."""
import json
import subprocess
import unittest

from jev_errors import (JevError, JevDecisionError, decode_response, diagnostic,
                        error_diagnostic, error_name, helper_error)
from routing_policy import _validated_choice

PRIVATE = "synthetic-private-key-and-prompt"


class JevErrorTests(unittest.TestCase):
    def test_response_parse_and_shape_failures(self):
        for raw, stage, reason in [
            (PRIVATE, "response_parse", "invalid_json"),
            (b"\xff", "response_parse", "invalid_encoding"),
            ("[]", "response_validation", "invalid_response_type"),
            ("null", "response_validation", "invalid_response_type"),
            (json.dumps({"body": PRIVATE}), "response_validation", "missing_answers"),
            ('{"answers":[]}', "response_validation", "missing_answers"),
        ]:
            with self.subTest(reason=reason, raw=type(raw).__name__):
                with self.assertRaises(JevError) as caught:
                    decode_response(raw)
                self.assertEqual(error_diagnostic(caught.exception, "transport"), {
                    "jev_error_stage": stage, "jev_error_reason": reason})
                self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertEqual(decode_response(b'{"answers":{}}'), {"answers": {}})

    def test_every_choice_validation_failure_has_safe_reason_and_field(self):
        for name in ("astra_policy", "model", "effort", "lease"):
            for value, reason in [
                (None, "missing_decision"),
                ({"choice": PRIVATE}, "unknown_choice"),
                ({"choice": "a", "probabilities": {PRIVATE: 1}}, "incomplete_distribution"),
                ({"choice": "a", "probabilities": {"a": PRIVATE, "b": 0}}, "invalid_probabilities"),
                ({"choice": "a", "probabilities": {"a": .1, "b": .9}}, "inconsistent_distribution"),
            ]:
                with self.subTest(field=name, reason=reason):
                    with self.assertRaises(JevDecisionError) as caught:
                        _validated_choice({name: value}, name, ["a", "b"])
                    self.assertEqual(error_diagnostic(caught.exception, "transport"), {
                        "jev_error_stage": "decision_validation", "jev_error_reason": reason,
                        "jev_error_field": name})
                    self.assertEqual(error_name(caught.exception), "ValueError")
                    self.assertNotIn(PRIVATE, str(caught.exception))

    def test_helper_stderr_accepts_only_bounded_enum_payloads(self):
        for stderr in [PRIVATE, json.dumps({"stage": PRIVATE, "reason": PRIVATE}),
                       json.dumps({"stage": "transport", "reason": "http_error", "body": PRIVATE}),
                       "[1]", "null", "x" * 1025, None]:
            with self.subTest(stderr_type=type(stderr).__name__):
                error = helper_error(stderr)
                self.assertEqual(error.details, {
                    "jev_error_stage": "transport", "jev_error_reason": "helper_failed"})
                self.assertNotIn(PRIVATE, str(error))
        error = helper_error('{"stage":"transport","reason":"http_error","http_status":401}')
        self.assertEqual(error.details["jev_error_http_status"], 401)
        for status in (True, "401 " + PRIVATE, 999):
            error = helper_error(json.dumps({"stage": "transport", "reason": "http_error", "http_status": status}))
            self.assertNotIn("jev_error_http_status", error.details)
            self.assertNotIn(PRIVATE, str(error))

    def test_unknown_exceptions_never_serialize_message_or_class_name(self):
        class Unprintable(Exception):
            def __str__(self):
                raise AssertionError("must not stringify exceptions")
        cases = [
            (type(PRIVATE, (Unprintable,), {})(), "transport", "unexpected_error", "Exception"),
            (subprocess.TimeoutExpired(PRIVATE, 4, output=PRIVATE, stderr=PRIVATE), "transport", "timeout", "TimeoutExpired"),
            (json.JSONDecodeError(PRIVATE, PRIVATE, 0), "response_parse", "invalid_json", "JSONDecodeError"),
            (UnicodeDecodeError("utf8", b"\xff", 0, 1, PRIVATE), "response_parse", "invalid_encoding", "UnicodeDecodeError"),
            (ValueError(PRIVATE), "route_application", "invalid_route", "ValueError"),
            (OSError(PRIVATE), "transport", "connection_failed", "OSError"),
        ]
        for error, stage, reason, name in cases:
            with self.subTest(reason=reason):
                safe = error_diagnostic(error, stage if stage == "route_application" else "transport")
                self.assertEqual(safe, {"jev_error_stage": stage, "jev_error_reason": reason})
                self.assertEqual(error_name(error), name)
                self.assertNotIn(PRIVATE, json.dumps(safe) + error_name(error))
        self.assertEqual(diagnostic(PRIVATE, PRIVATE, PRIVATE, PRIVATE), {
            "jev_error_stage": "transport", "jev_error_reason": "unexpected_error"})
        error = JevError("transport", "timeout")
        error.details.update(jev_error_reason=PRIVATE, jev_error_field=PRIVATE, extra=PRIVATE)
        self.assertNotIn(PRIVATE, json.dumps(error_diagnostic(error, "transport")))
