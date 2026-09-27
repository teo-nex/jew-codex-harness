#!/usr/bin/env python3
"""Independent worker acceptance from files/checks or actual MCP result."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import uuid

def codex_home():
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def state_dir():
    configured = os.environ.get("JEV_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif os.sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return (base / "jev-codex-harness").resolve()


def worker_state_dir():
    configured = os.environ.get("JEV_WORKER_STATE_DIR") or os.environ.get("JEV_PORTABLE_WORKER_STATE")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.environ.get("JEV_STATE_DIR"):
        return state_dir() / "workers"
    return codex_home() / "jev-harness/workers"


WORKERS = worker_state_dir()  # Compatibility for existing imports.


def load(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def browser_proof(directory, task, sessions_root=None):
    evidence = load(directory / "native-runtime-evidence.json")
    source = Path(str(evidence.get("source") or ""))
    sessions = Path(sessions_root) if sessions_root else codex_home() / "sessions"
    if (evidence.get("verified") is not True or not source.is_file()
            or not source.resolve().is_relative_to(sessions.resolve())):
        return False, "native_rollout_unverified", None
    if any(Path(task["cwd"]).iterdir()):
        return False, "read_only_workspace_changed", None
    results = []
    raw_browser_calls = 0
    with source.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get("payload", {})
            if row.get("type") == "event_msg" and payload.get("type") == "task_complete":
                break  # Later manual turns are outside this worker's recorded outcome.
            if row.get("type") != "event_msg" or payload.get("type") != "item_completed":
                continue
            item = payload.get("item", {})
            if item.get("type") != "McpToolCall":
                continue
            if item.get("server") == "canvastty_browser":
                raw_browser_calls += 1
            if (item.get("server") == "jev-browser" and item.get("tool") == "browser_task"
                    and item.get("status") == "completed"):
                for block in (item.get("result") or {}).get("content") or []:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        try:
                            value = json.loads(block["text"])
                        except ValueError:
                            continue
                        if isinstance(value, dict):
                            results.append(value)
    if raw_browser_calls:
        return False, "raw_browser_tool_used", None
    verified = [value for value in results if value.get("status") in ("verified", "verified_with_navigation")
                and (value.get("page") or {}).get("marker_found") is True]
    if not verified:
        return False, "browser_marker_not_verified", None
    value = verified[-1]
    return True, "browser_mcp_marker_verified", {"method": value["status"],
        "click_verified": value.get("click_verified"), "rollout_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


def coding_proof(directory, task, exit_data):
    checks = exit_data.get("checks") or []
    changed = exit_data.get("changed_files") or []
    allowed = set(task.get("allowed_files") or [])
    if not checks or any(not isinstance(row, dict) or row.get("exit_code") != 0 for row in checks):
        return False, "checks_missing_or_failed", None
    if not changed:
        return False, "owned_file_change_missing_or_outside_scope", None
    cwd = Path(task["cwd"]).resolve()
    changed_paths = []
    for name in changed:
        path = Path(name).resolve() if Path(name).is_absolute() else (cwd / name).resolve()
        if not path.is_relative_to(cwd):
            return False, "changed_file_escaped_workspace", None
        if str(path.relative_to(cwd)) not in allowed:
            return False, "owned_file_change_missing_or_outside_scope", None
        changed_paths.append(path)
    files = {}
    for row in exit_data.get("files") or []:
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            path = Path(row["path"])
            files[path.resolve() if path.is_absolute() else (cwd / path).resolve()] = row
    for path in changed_paths:
        record = files.get(path)
        if not path.is_file() or not record or record.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
            return False, "file_hash_changed_after_worker", None
    return True, "owned_files_and_checks_verified", {"changed_files": [str(path.relative_to(cwd)) for path in changed_paths],
        "checks_passed": len(checks)}


def verify(task_id, *, workers=None, write=True):
    uuid.UUID(task_id)
    directory = (Path(workers) if workers is not None else worker_state_dir()) / task_id
    task, exit_data = load(directory / "task.json"), load(directory / "exit.json")
    if (task and exit_data.get("state") == "failed"
            and exit_data.get("reason") == "input_token_budget_exceeded" and task.get("allowed_files")):
        functional_pass, _, proof = coding_proof(directory, task, exit_data)
        result = {"accepted": False, "functional_pass": functional_pass,
                  "reason": "input_token_budget_exceeded", "proof": proof}
    elif not task or not exit_data or exit_data.get("state") != "turn_finished_needs_review":
        result = {"accepted": False, "reason": "worker_not_completed"}
    elif "browser" in task.get("capabilities", []):
        accepted, reason, proof = browser_proof(directory, task)
        result = {"accepted": accepted, "reason": reason, "proof": proof}
    elif task.get("allowed_files"):
        accepted, reason, proof = coding_proof(directory, task, exit_data)
        result = {"accepted": accepted, "reason": reason, "proof": proof}
    else:
        result = {"accepted": False, "reason": "no_independent_acceptance_rule"}
    result.update({"task_id": task_id, "verified_at": datetime.now(timezone.utc).isoformat(),
                   "usage": exit_data.get("usage") if isinstance(exit_data.get("usage"), dict) else None})
    if write:
        target = directory / "acceptance.json"
        tmp = target.with_name(target.name + ".tmp-" + str(os.getpid()))
        with tmp.open("x") as output:
            os.chmod(tmp, 0o600)
            json.dump(result, output, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    args = parser.parse_args()
    result = verify(args.task_id)
    print(json.dumps({key: result.get(key) for key in ("task_id", "accepted", "reason")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
