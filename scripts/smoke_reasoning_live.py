#!/usr/bin/env python3
"""Opt-in synthetic reasoning probes; no services, fallback or credential writes."""
import argparse
import http.client
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
import jev_server as jev
from harness.core import protected_file
from reasoning_effort import resolve_model_reasoning_effort, validate_reasoning_profiles

MODELS = ("antigravity/gemini-3.8-flash-tiered",
          "gonkagate/deepseek-ai/deepseek-v4-flash-0731")
LIMIT = 4 * 1024 * 1024


def load_auth_key(path):
    text = protected_file(Path(path), "gateway auth").read_text().strip()
    if text.startswith("{"):
        value = json.loads(text)
        nested = value.get("omniroute", {})
        key = nested.get("key") if isinstance(nested, dict) else None
        key = key or value.get("key") or value.get("token")
    else:
        key = text
    if not isinstance(key, str) or not key.strip() or any(c.isspace() for c in key):
        raise ValueError("invalid gateway key format")
    return key


def request(key, port, method, path, payload=None, timeout=90):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    deadline = time.monotonic() + timeout
    try:
        body = None if payload is None else json.dumps(payload).encode()
        conn.request(method, path, body=body, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json"})
        sock = conn.sock
        response = conn.getresponse()
        if response.status != 200:
            # Remote error bodies can contain request headers.
            raise ValueError("gateway_http_" + str(response.status))
        raw = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("smoke request deadline")
            sock.settimeout(remaining)
            chunk = response.read1(min(65536, LIMIT + 1 - len(raw)))
            if not chunk:
                return bytes(raw)
            raw.extend(chunk)
            if len(raw) > LIMIT:
                raise ValueError("response exceeds smoke size limit")
            if response.isclosed() is True:
                return bytes(raw)
    finally:
        conn.close()


def decode_response(raw):
    if raw.lstrip().startswith((b"event:", b"data:", b":")):
        issue = jev.external_sse_issue(raw)
        if issue:
            raise ValueError("invalid_stream_" + issue)
        result = jev.assemble_sse(raw)
    else:
        result = json.loads(raw)
    if not isinstance(result, dict) or result.get("status") != "completed" or result.get("error"):
        raise ValueError("response_not_completed")
    if not isinstance(result.get("output"), list) or not result["output"]:
        raise ValueError("empty_output")
    return result


def output_text(response):
    return "".join(part.get("text", "") for item in response["output"]
                   if item.get("type") == "message"
                   for part in item.get("content", []) if part.get("type") == "output_text").strip()


def validate_math(response):
    value = json.loads(output_text(response))
    if value != {"result": 4183} or type(value.get("result")) is not int:
        raise ValueError("incorrect_math_output")


def prepare_payload(model, effort, profiles, items, tools=None):
    resolved = resolve_model_reasoning_effort(model, effort, profiles)
    payload = {"input": items, "max_output_tokens": 2048}
    jev.apply_route_payload(payload, model, resolved["effective_effort"], None)
    if tools:
        payload["tools"] = tools
    return payload, resolved


def run_probe(key, port, model, effort, profiles, *, tool_loop=False, timeout=90):
    items = [{"role": "user", "content": (
        'Use multiply_numbers for 47 times 89, then return ONLY JSON {"result": <integer>}.'
        if tool_loop else 'Compute 47 times 89. Return ONLY JSON {"result": <integer>}.')}]
    tools = [{"type": "function", "name": "multiply_numbers", "description": "Multiply integers",
              "parameters": {"type": "object", "properties": {
                  "a": {"type": "integer"}, "b": {"type": "integer"}},
                  "required": ["a", "b"], "additionalProperties": False}}] if tool_loop else None
    payload, resolved = prepare_payload(model, effort, profiles, items, tools)
    start = time.monotonic()
    response = decode_response(request(key, port, "POST", "/v1/responses", payload, timeout))
    if tool_loop:
        calls = [item for item in response["output"] if item.get("type") == "function_call"]
        if len(calls) != 1 or calls[0].get("name") != "multiply_numbers":
            raise ValueError("expected_one_tool_call")
        call = calls[0]
        arguments = json.loads(call.get("arguments", ""))
        if arguments != {"a": 47, "b": 89} or not call.get("call_id"):
            raise ValueError("incorrect_tool_arguments")
        # Synthetic tool only: never execute a model-supplied command.
        items = items + response["output"] + [{"type": "function_call_output",
            "call_id": call["call_id"], "output": json.dumps({"result": 4183})}]
        payload, resolved = prepare_payload(model, effort, profiles, items, tools)
        response = decode_response(request(key, port, "POST", "/v1/responses", payload, timeout))
    validate_math(response)
    return {"model": model, **resolved, "probe": "tool_loop" if tool_loop else "json_math",
            "reasoning_status": resolved["status"],
            "status": "PASS", "response_status": response["status"],
            "elapsed_ms": round((time.monotonic() - start) * 1000),
            "provider_control": "NOT_VERIFIED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable bounded model requests")
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--port", type=int, default=20128)
    parser.add_argument("--timeout", type=int, choices=range(1, 121), default=90)
    parser.add_argument("--model", action="append", help="Exact gateway model ID; default: Gemini and DeepSeek")
    parser.add_argument("--tools", action="store_true", help="Also verify a two-request tool loop")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; no network requests made")
    try:
        key = load_auth_key(args.auth_file)
        profiles = validate_reasoning_profiles(json.loads(args.profiles.read_text()))
        catalog = json.loads(request(key, args.port, "GET", "/v1/models", timeout=args.timeout))
        available = {row["id"] for row in catalog.get("data", []) if isinstance(row, dict) and "id" in row}
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        return 1
    passed = True
    for model in dict.fromkeys(args.model or MODELS):
        if model not in available:
            print(json.dumps({"model": model, "status": "BLOCKED_MODEL_UNAVAILABLE"}))
            passed = False
            continue
        cases = [("low", False), ("high", False)] + ([("high", True)] if args.tools else [])
        for effort, tool_loop in cases:
            try:
                result = run_probe(key, args.port, model, effort, profiles,
                                   tool_loop=tool_loop, timeout=args.timeout)
            except Exception as exc:
                result = {"model": model, "requested_effort": effort,
                          "probe": "tool_loop" if tool_loop else "json_math",
                          "status": "FAIL", "error_type": type(exc).__name__}
                passed = False
            print(json.dumps(result), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
