"""Sanitized last-request diagnostics, never a raw-log export."""

import json
from pathlib import Path


FIELDS = ("at", "policy_version", "selected_model", "native", "base_tier", "model",
          "effort", "requested_effort", "effective_effort", "reasoning_status",
          "reasoning_source", "decision_source", "gate", "astra_policy", "step",
          "ladder_stage", "status", "jev_ms", "total_ms", "project_policy", "budget_exhausted")
ATTEMPT_FIELDS = ("model", "response_model", "selected_model", "effort", "requested_effort",
                  "effective_effort", "reasoning_status", "reasoning_source", "status",
                  "http_status", "terminal_type", "completion", "failure_class", "fallback_reason",
                  "connect_ms", "headers_ms", "first_token_ms", "total_ms", "timeout_phase")


def _safe(row, fields):
    return {key: value for key in fields if (value := row.get(key)) is None
            or type(value) in (str, int, float, bool)}


def latest(home, scope=None):
    path = Path(home).expanduser() / "codex-router/jev-router-live.jsonl"
    if path.is_symlink():
        raise ValueError("Refusing symlinked routing log")
    if not path.exists():
        return {"found": False, "status": "no_request_evidence"}
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - 1024 * 1024))
        data = stream.read()
    rows = data.splitlines()
    if size > 1024 * 1024:
        rows = rows[1:]
    for raw in reversed(rows):
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict) or (scope is not None and row.get("cache_scope") != scope):
            continue
        if "model" not in row and "attempts" not in row:
            continue
        report = _safe(row, FIELDS)
        report["attempts"] = [_safe(attempt, ATTEMPT_FIELDS) for attempt in row.get("attempts", [])
                              if isinstance(attempt, dict)]
        report["found"] = True
        report["reasoning_enforcement"] = "unknown"
        report["backend_identity"] = "unverified"
        report["observed_model"] = next((attempt.get("response_model") for attempt in reversed(report["attempts"])
                                         if attempt.get("response_model")), "unknown")
        return report
    return {"found": False, "status": "no_matching_request_evidence"}
