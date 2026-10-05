"""Transport failure classes without storing upstream response bodies."""

import json


def classify_failure(status, quota_hit=False, body=None, attempt=None):
    attempt = attempt or {}
    phase = attempt.get("timeout_phase")
    code = None
    try:
        parsed = json.loads(body) if body else None
        error = parsed.get("error") if isinstance(parsed, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
    except (ValueError, UnicodeError, TypeError):
        pass
    if attempt.get("completion") == "client_disconnected":
        category, seconds, retry = "client_disconnected", 0, False
    elif phase in ("total", "max_attempts"):
        category, seconds, retry = "budget_exhausted", 0, False
    elif status in (400, 413, 422) and not quota_hit:
        category, seconds, retry = "bad_request", 0, False
    elif status in (401, 403):
        category, seconds, retry = "auth", 900, True
    elif status == 404:
        category, seconds, retry = "model_unavailable", 300, True
    elif status == 429 and code in ("rate_limit_exceeded", "rate_limit", "too_many_requests"):
        category, seconds, retry = "rate_limit", 30, True
    elif quota_hit or status in (402, 429):
        category, seconds, retry = "quota", 300, True
    elif phase or status in (408, 504):
        category, seconds, retry = "timeout", 30, True
    elif (status == 200 or str(attempt.get("completion", "")).startswith("invalid")):
        category, seconds, retry = "invalid_response", 30, True
    elif status >= 500 or attempt.get("transport_error"):
        category, seconds, retry = "unavailable", 30, True
    else:
        category, seconds, retry = "bad_request", 0, False
    return {"class": category, "cooldown_seconds": seconds, "retryable": retry}
