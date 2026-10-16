"""Allowlisted Jev failure metadata; never serialize exception/provider text."""

import json
import subprocess


REASONS = {
    "transport": {"timeout", "http_error", "connection_failed", "helper_failed", "redirect_refused"},
    "response_parse": {"invalid_json", "invalid_encoding"},
    "response_validation": {"invalid_response_type", "missing_answers", "unexpected_model"},
    "decision_validation": {"missing_decision", "unknown_choice", "incomplete_distribution",
                            "invalid_probabilities", "inconsistent_distribution"},
    "route_application": {"invalid_route"},
}
FIELDS = {"astra_policy", "model", "effort", "lease"}


def diagnostic(stage, reason, field=None, http_status=None):
    stage = stage if isinstance(stage, str) and stage in REASONS else "transport"
    reason = reason if isinstance(reason, str) and reason in REASONS[stage] else "unexpected_error"
    result = {"jev_error_stage": stage, "jev_error_reason": reason}
    if isinstance(field, str) and field in FIELDS:
        result["jev_error_field"] = field
    if type(http_status) is int and 100 <= http_status <= 599:
        result["jev_error_http_status"] = http_status
    return result


class JevError(RuntimeError):
    def __init__(self, stage, reason, *, http_status=None):
        self.details = diagnostic(stage, reason, http_status=http_status)
        message = (f"provider HTTP {http_status}" if self.details.get("jev_error_http_status")
                   else self.details["jev_error_stage"] + ": " + self.details["jev_error_reason"])
        super().__init__(message)


class JevDecisionError(ValueError):
    def __init__(self, reason, field):
        self.details = diagnostic("decision_validation", reason, field)
        super().__init__(self.details["jev_error_reason"])


def error_name(error):
    # Never copy arbitrary class names: dynamically named exception classes can
    # themselves contain private data. Preserve the legacy gate vocabulary.
    for cls in (json.JSONDecodeError, UnicodeDecodeError, subprocess.TimeoutExpired,
                TimeoutError, ValueError, TypeError, RuntimeError, OSError):
        if isinstance(error, cls):
            return cls.__name__
    return "Exception"


def error_diagnostic(error, stage):
    if isinstance(error, (JevError, JevDecisionError)):
        d = error.details
        return diagnostic(d.get("jev_error_stage"), d.get("jev_error_reason"),
                          d.get("jev_error_field"), d.get("jev_error_http_status"))
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        stage = "response_parse"
    reason = ("timeout" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired))
              else "invalid_json" if isinstance(error, json.JSONDecodeError)
              else "invalid_encoding" if isinstance(error, UnicodeDecodeError)
              else "connection_failed" if isinstance(error, OSError)
              else "invalid_route" if stage == "route_application" and isinstance(error, ValueError)
              else "unexpected_error")
    return diagnostic(stage, reason)


def helper_error(stderr):
    # Only our helper's structured enum payload is accepted. Arbitrary stderr,
    # extra fields, upstream bodies and exception strings are discarded.
    try:
        if not isinstance(stderr, str) or len(stderr) > 1024:
            raise ValueError()
        value = json.loads(stderr)
        if not isinstance(value, dict) or set(value) - {"stage", "reason", "http_status"}:
            raise ValueError()
        safe = diagnostic(value.get("stage"), value.get("reason"), http_status=value.get("http_status"))
        if safe["jev_error_reason"] == "unexpected_error":
            raise ValueError()
        return JevError(safe["jev_error_stage"], safe["jev_error_reason"],
                        http_status=safe.get("jev_error_http_status"))
    except (ValueError, TypeError):
        return JevError("transport", "helper_failed")


def decode_response(raw):
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except UnicodeDecodeError:
        raise JevError("response_parse", "invalid_encoding") from None
    except (ValueError, TypeError):
        raise JevError("response_parse", "invalid_json") from None
    if not isinstance(value, dict):
        raise JevError("response_validation", "invalid_response_type")
    if not isinstance(value.get("answers"), dict):
        raise JevError("response_validation", "missing_answers")
    return value
