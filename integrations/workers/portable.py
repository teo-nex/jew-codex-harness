#!/usr/bin/env python3
"""Conservative non-macOS jev-workers backend using the installed Codex CLI.

Only read-only tasks are admitted: Codex's workspace sandbox cannot enforce an
allowed_files list. The worker runs in its own process and never prints CLI events.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


ACTIVE = {"queued", "running"}


def state_root():
    codex_home = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
    configured = os.environ.get("JEV_WORKER_STATE_DIR") or os.environ.get("JEV_PORTABLE_WORKER_STATE")
    if configured is None and os.environ.get("JEV_STATE_DIR"):
        configured = str(Path(os.environ["JEV_STATE_DIR"]) / "workers")
    root = Path(configured or codex_home / "jev-harness/workers").expanduser().resolve()
    repository = Path(__file__).resolve().parents[2]
    if root.is_relative_to(repository):
        raise ValueError("Portable worker state must be outside the repository")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        root.chmod(0o700)
    return root


def _write(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _directory(task_id):
    uuid.UUID(task_id)
    directory = state_root() / task_id
    if not directory.is_dir():
        raise FileNotFoundError("Unknown portable worker task")
    return directory


def _validate(value):
    if not isinstance(value, dict):
        raise ValueError("Task must be an object")
    request_id, goal = value.get("request_id"), value.get("goal")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
        raise ValueError("Invalid request_id")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 50000:
        raise ValueError("Invalid goal")
    cwd_arg = value.get("cwd")
    if not isinstance(cwd_arg, str):
        raise ValueError("cwd must be a directory")
    cwd = Path(cwd_arg).resolve(strict=True)
    if not cwd.is_dir() or cwd == state_root() or state_root().is_relative_to(cwd):
        raise ValueError("cwd must be a directory outside worker state")
    files = value.get("allowed_files")
    if not isinstance(files, list) or any(not isinstance(name, str) or not name for name in files):
        raise ValueError("allowed_files must be an array of paths")
    for name in files:
        if Path(name).is_absolute() or not (cwd / name).resolve().is_relative_to(cwd):
            raise ValueError("allowed_file escapes cwd")
    commands = value.get("acceptance_commands", [])
    if not isinstance(commands, list) or any(
        not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command)
        for command in commands
    ):
        raise ValueError("acceptance_commands must be argv arrays")
    if files or commands or value.get("read_only") is not True:
        raise RuntimeError("Portable workers support read_only tasks only; mutable ownership and acceptance commands require a verified sandbox")
    if value.get("capabilities"):
        raise RuntimeError("Portable worker browser/computer capabilities are not verified")
    if value.get("requested_model") is not None:
        raise ValueError("Portable subagents use jev/auto; model pins bypass the ladder")
    instructions = value.get("mandatory_instructions", "")
    if not isinstance(instructions, str):
        raise ValueError("mandatory_instructions must be text")
    budget = value.get("budget", {})
    if not isinstance(budget, dict):
        raise ValueError("Invalid budget")
    seconds = budget.get("max_seconds", 3600)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 60 <= seconds <= 86400:
        raise ValueError("Invalid max_seconds")
    if shutil.which("codex") is None:
        raise RuntimeError("Codex CLI is required for portable jev-workers")
    return {"request_id": request_id, "goal": goal, "cwd": str(cwd), "allowed_files": [],
            "acceptance_commands": [], "mandatory_instructions": instructions,
            "budget": {"max_seconds": seconds}, "read_only": True}


def submit(value):
    task = _validate(value)
    root = state_root()
    digest = hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    request_path = root / ("request-" + hashlib.sha256(task["request_id"].encode()).hexdigest() + ".json")
    task_id = str(uuid.uuid4())
    directory = root / task_id
    directory.mkdir(mode=0o700)
    task["task_id"] = task_id
    _write(directory / "task.json", task)
    _write(directory / "state.json", {"state": "queued", "created_at": time.time()})
    try:
        fd = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        prior = _read(request_path)
        shutil.rmtree(directory)
        if prior["digest"] != digest:
            raise ValueError("request_id reused with changed task")
        return status(prior["task_id"])
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump({"task_id": task_id, "digest": digest}, output)
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", task_id],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(root), start_new_session=(os.name != "nt"),
                         creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0))
    except Exception:
        _write(directory / "state.json", {"state": "blocked", "reason": "worker_runner_start_failed"})
        raise RuntimeError("Portable worker runner could not start") from None
    return status(task_id)


def _run(task_id):
    directory = _directory(task_id)
    task = _read(directory / "task.json")
    if (directory / "cancel.request").exists():
        _write(directory / "state.json", {"state": "cancelled"})
        return
    prompt = ("You are one read-only worker. Do not edit files, use subagents, or call jev-workers. "
              "Do not reveal credentials or secrets in the answer.\n"
              "Instructions: " + task["mandatory_instructions"] + "\nTask: " + task["goal"])
    answer = directory / "answer.txt"
    command = [shutil.which("codex"), "exec", "--model", "jev/auto", "--sandbox", "read-only",
               "--skip-git-repo-check", "--ephemeral", "--json", "--cd", task["cwd"],
               "-c", "mcp_servers.jev-workers.enabled=false", "--output-last-message", str(answer), "-"]
    _write(directory / "state.json", {"state": "running", "started_at": time.time()})
    process = None
    try:
        # A pipe write can block before the timeout loop if Codex never reads.
        with tempfile.TemporaryFile() as input_file:
            input_file.write(prompt.encode())
            input_file.seek(0)
            process = subprocess.Popen(command, stdin=input_file, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, cwd=task["cwd"])
        deadline = time.monotonic() + task["budget"]["max_seconds"]
        while process.poll() is None:
            if (directory / "cancel.request").exists():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                _write(directory / "state.json", {"state": "cancelled", "ended_at": time.time()})
                return
            if time.monotonic() > deadline:
                process.kill(); process.wait()
                _write(directory / "state.json", {"state": "blocked", "reason": "worker_timeout", "ended_at": time.time()})
                return
            time.sleep(0.25)
        _write(directory / "state.json", {"state": "completed" if process.returncode == 0 and answer.exists() else "failed",
                                         "reason": None if process.returncode == 0 else "codex_exec_failed",
                                         "ended_at": time.time()})
    except Exception:
        _write(directory / "state.json", {"state": "failed", "reason": "worker_runner_error", "ended_at": time.time()})
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


def status(task_id):
    directory = _directory(task_id)
    task = _read(directory / "task.json")
    state = _read(directory / "state.json")
    since = state.get("started_at", state.get("created_at", 0))
    if state["state"] in ACTIVE and time.time() - since > task["budget"]["max_seconds"] + 60:
        state = {"state": "lost_monitor", "reason": "worker_state_expired_without_terminal_receipt"}
    return {"task_id": task_id, "request_id": task["request_id"], "state": state["state"],
            "route": "auto", "worker_backend": "portable_codex_cli", "model": "jev/auto",
            "reason": state.get("reason", "provider_ladder")}


def collect(task_id):
    result = status(task_id)
    directory = _directory(task_id)
    answer = directory / "answer.txt"
    full = answer.read_text(encoding="utf-8") if result["state"] == "completed" and answer.exists() else ""
    return {**result, "summary": full[:2400], "summary_truncated": len(full) > 2400,
            "checks": [], "changed_files": [], "usage": None, "usage_source": "unknown",
            "artifact_dir": str(directory), "result_file": str(answer) if answer.exists() else None}


def execute(value):
    seconds = value.get("wait_seconds", 50)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 50:
        raise ValueError("wait_seconds must be between 0 and 50")
    result = submit(value)
    deadline = time.monotonic() + seconds
    while seconds and status(result["task_id"])["state"] in ACTIVE and time.monotonic() < deadline:
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    return collect(result["task_id"])


def cancel(task_id):
    result = status(task_id)
    if result["state"] in ACTIVE:
        ( _directory(task_id) / "cancel.request" ).touch(mode=0o600, exist_ok=True)
        return {**result, "cancel_requested": True}
    return {**result, "cancel_requested": False}


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "run":
    _run(sys.argv[2])
