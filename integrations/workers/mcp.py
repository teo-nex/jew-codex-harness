#!/usr/bin/env python3
"""Owned Codex workers: one MCP creation path, jev/auto provider ladder."""
import json
import math
import sys
import time

if sys.platform == "darwin":
    from luna_dispatch import submit, status, collect, cancel, execute
else:
    # CanvasTTY and fcntl are macOS-only. Never import them on portable hosts.
    from portable import submit, status, collect, cancel, execute

TASK_SCHEMA = {"type": "object", "required": ["request_id", "goal", "cwd", "allowed_files"],
    "properties": {"request_id": {"type": "string"}, "goal": {"type": "string"},
        "cwd": {"type": "string"}, "allowed_files": {"type": "array", "items": {"type": "string"}},
        "acceptance_commands": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        "mandatory_instructions": {"type": "string"}, "budget": {"type": "object"},
        "read_only": {"type": "boolean", "description": "Use an empty isolated cwd and make no file changes."},
        "capabilities": {"type": "array", "uniqueItems": True,
                         "items": {"type": "string", "enum": ["browser", "computer", "pdf", "documents"]}}}}
WAIT = {"type": "number", "minimum": 0, "maximum": 50}
TOOLS = [
    {"name": "submit", "description": "Start one owned Codex worker on jev/auto. Jev applies Plus, sticky Gemini, Opus, Gonka GLM, Gonka DeepSeek, Wally GLM, then Luna 6.",
     "inputSchema": TASK_SCHEMA},
    {"name": "execute", "description": "Start an owned jev/auto worker and wait up to 50 seconds in code; use collect for longer tasks.",
     "inputSchema": {**TASK_SCHEMA, "properties": {**TASK_SCHEMA["properties"], "wait_seconds": WAIT}}},
    *({"name": name, "description": name.capitalize() + " an owned Jev worker by task_id.",
       "inputSchema": {"type": "object", "required": ["task_id"],
                       "properties": {"task_id": {"type": "string"}, "wait_seconds": WAIT}}}
      for name in ("status", "collect", "cancel")),
]


def validate_wait(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 50:
        raise ValueError("wait_seconds must be between 0 and 50")
    return seconds


def wait_for(task_id, seconds, get_status, active):
    seconds = validate_wait(seconds)
    deadline = time.monotonic() + seconds
    current = get_status(task_id)
    while seconds and current["state"] in active and time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        current = get_status(task_id)
    return current


def handle(message):
    if not isinstance(message, dict):
        raise ValueError("JSON-RPC message must be an object")
    method = message.get("method")
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "jev-workers", "version": "0.3.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = message.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name not in {tool["name"] for tool in TOOLS}:
            raise ValueError("Unknown or retired worker tool")
        try:
            if name in ("status", "collect"):
                active = {"queued", "running", "quick_starting", "quick_running", "lean_starting", "lean_running",
                          "native_starting", "native_selecting_model", "native_running"}
                wait_for(arguments["task_id"], arguments.get("wait_seconds", 0), status, active)
            if name == "submit":
                output = submit(arguments)
            elif name == "execute":
                output = execute(arguments)
            else:
                output = {"status": status, "collect": collect, "cancel": cancel}[name](arguments["task_id"])
            return {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}], "isError": False}
        except Exception as error:
            return {"content": [{"type": "text", "text": json.dumps({"error": type(error).__name__, "detail": str(error)[:500]}, ensure_ascii=False)}], "isError": True}
    raise ValueError("Unknown method")


def main():
    for line in sys.stdin:
        message = None
        try:
            message = json.loads(line)
            if "id" not in message:
                continue
            result = {"jsonrpc": "2.0", "id": message["id"], "result": handle(message)}
        except json.JSONDecodeError:
            result = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        except Exception as error:
            result = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                      "error": {"code": -32603, "message": type(error).__name__ + ": " + str(error)[:500]}}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
