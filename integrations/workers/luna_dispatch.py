#!/usr/bin/env python3
"""Jev-routed native Luna workers, coordinated by the current Codex session."""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
STATE = Path(os.environ.get("JEV_WORKER_STATE_DIR") or
             (str(Path(os.environ["JEV_STATE_DIR"]) / "workers") if os.environ.get("JEV_STATE_DIR")
              else str(CODEX_HOME / "jev-harness/workers")))
CONTROL = ROOT / "integrations/canvastty"
sys.path.insert(0, str(CONTROL))
from control import Bridge  # noqa: E402 - checks the real CanvasTTY process and token
sys.path.insert(0, str(ROOT))
from integrations.jev.routing import LUNA, LUNA_PREVIOUS, TERRA_PREVIOUS, ORCHESTRATOR  # noqa: E402
from pi_worker import fingerprint, snapshot_baseline, write_json, process_start, now  # noqa: E402
from native_fallback import start_native_worker, monitor_answer, cancel_native, view_for  # noqa: E402


def task_path(task_id):
    uuid.UUID(task_id)
    return STATE / task_id


def require_provider_ladder():
    """Do not start a child when the global Jev ladder is unavailable."""
    router_state = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "codex-router"
    if any((router_state / name).exists() for name in ("jev-router.off", "jev-router.shadow")):
        raise RuntimeError("Jev provider ladder is disabled")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    port = 4319
    try:
        manifest = json.loads((Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
                               / "jev-harness/manifest.json").read_text())
        candidate = manifest.get("port")
        if isinstance(candidate, int) and 1024 <= candidate <= 65535:
            port = candidate
    except (OSError, ValueError):
        pass
    with opener.open(f"http://127.0.0.1:{port}/health", timeout=3) as response:
        health = json.load(response)
    if (health.get("ok") is not True or health.get("ladder_active") is not True
            or health.get("auth_configured") is not True):
        raise RuntimeError("Jev provider ladder is unavailable")


def quick_view(bridge, binding):
    view = bridge.call("/g2/api/terminal?id=" + binding["session_id"])
    session = view["session"]
    if (session.get("id") != binding["session_id"] or session.get("provider") != "terminal"
            or session.get("startedAt") != binding["started_at"]
            or str(Path(session.get("cwd", "")).resolve()) != binding["cwd"]):
        raise RuntimeError("Quick terminal binding changed")
    return view


def start_quick(directory, task, bridge):
    if (directory / "quick-create-request.json").exists():
        raise RuntimeError("Quick terminal creation already requested")
    before = bridge.call("/api/sessions")
    write_json(directory / "quick-create-request.json", {"before_ids": [s["id"] for s in before["sessions"]],
        "provider": "terminal", "at": now()})
    session = bridge.call("/g2/api/create", {"provider": "terminal", "cwd": task["cwd"],
        "title": ("Jev Quick " if task.get("question_only") else "Jev Luna Code ") + task["task_id"][:8]})["session"]
    uuid.UUID(session["id"])
    if session.get("provider") != "terminal" or session.get("startedAt") is None or str(Path(session.get("cwd", "")).resolve()) != task["cwd"]:
        raise RuntimeError("Unexpected quick terminal identity")
    binding = {"session_id": session["id"], "started_at": session["startedAt"],
        "cwd": task["cwd"], "provider": "terminal"}
    write_json(directory / "quick-terminal.json", binding)
    command = shlex.join([sys.executable, "-u", str(Path(__file__).with_name("quick_worker.py")), str(directory)])
    request_id = str(uuid.uuid4())
    write_json(directory / "quick-delivery-request.json", {"request_id": request_id,
        "session_id": session["id"], "command_sha256": hashlib.sha256(command.encode()).hexdigest()})
    payload = {"sessionId": session["id"], "requestId": request_id, "action": "text", "text": command}
    for attempt in range(3):
        try:
            quick_view(bridge, binding)
            receipt = bridge.call("/g2/api/control", payload)
            break
        except RuntimeError as error:
            if "HTTP 409" not in str(error) or attempt == 2:
                raise
            time.sleep(0.5)
    write_json(directory / "quick-delivery-receipt.json", receipt)
    return {"state": "lean_running", "route": "auto" if task.get("worker_model") == "jev/auto"
            else "quick" if task.get("question_only") else "luna",
            "worker_backend": "lean", "provider": "codex", "model": task["worker_model"],
            "effort": task["worker_effort"], "session_id": session["id"], "started_at": now()}


def monitor_quick(directory, task, bridge):
    binding = json.loads((directory / "quick-terminal.json").read_text())
    deadline = time.monotonic() + task["budget"].get("max_seconds", 180) + 30
    while time.monotonic() < deadline:
        if (directory / "exit.json").exists():
            value = json.loads((directory / "exit.json").read_text())
            if value.get("state") not in ("quick_running", "lean_running"):
                return value
        if (directory / "cancel.request").exists():
            quick_view(bridge, binding)
            receipt = bridge.call("/api/interrupt", {"sessionId": binding["session_id"]})
            write_json(directory / "quick-cancel-receipt.json", receipt)
            return {"state": "cancelled", "session_id": binding["session_id"], "ended_at": now()}
        view = quick_view(bridge, binding)
        if view["session"].get("status") in ("failed", "done"):
            return {"state": "failed", "reason": "quick_terminal_stopped", "ended_at": now()}
        time.sleep(0.5)
    quick_view(bridge, binding)
    receipt = bridge.call("/api/interrupt", {"sessionId": binding["session_id"]})
    write_json(directory / "quick-timeout-interrupt.json", receipt)
    return {"state": "blocked", "reason": "quick_timeout_interrupt_sent", "ended_at": now()}


def validate_task(value):
    if not isinstance(value, dict):
        raise ValueError("Task must be an object")
    request_id = value.get("request_id")
    goal = value.get("goal")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
        raise ValueError("Invalid request_id")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 50000:
        raise ValueError("Invalid goal")
    cwd = Path(value["cwd"]).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("cwd must be a directory")
    question_only = value.get("question_only") is True
    read_only = value.get("read_only") is True
    files = value.get("allowed_files")
    if not isinstance(files, list) or (not files and not question_only and not read_only) or any(not isinstance(f, str) or not f for f in files):
        raise ValueError("allowed_files must be nonempty")
    if read_only and (files or any(cwd.iterdir())):
        raise ValueError("read_only worker requires empty ownership and isolated cwd")
    if len(set(files)) != len(files):
        raise ValueError("Duplicate allowed_files")
    for name in files:
        if not (cwd / name).resolve().is_relative_to(cwd):
            raise ValueError("allowed_file escapes cwd")
    commands = value.get("acceptance_commands", [])
    if not isinstance(commands, list) or any(
        not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command)
        for command in commands
    ):
        raise ValueError("acceptance_commands must be argv arrays")
    if read_only and commands:
        raise ValueError("read_only worker cannot run acceptance commands")
    instructions = value.get("mandatory_instructions", "")
    if not isinstance(instructions, str):
        raise ValueError("mandatory_instructions must be text")
    capabilities = value.get("capabilities", [])
    if (not isinstance(capabilities, list)
            or any(not isinstance(item, str) or item not in ("browser", "computer", "pdf", "documents")
                   for item in capabilities)
            or len(set(capabilities)) != len(capabilities)):
        raise ValueError("Unsupported worker capability")
    budget = dict(value.get("budget", {})) if isinstance(value.get("budget", {}), dict) else value.get("budget")
    if not isinstance(budget, dict) or not 60 <= budget.get("max_seconds", 3600) <= 86400:
        raise ValueError("Invalid budget")
    if not 1 <= budget.get("check_timeout_seconds", 120) <= 3600:
        raise ValueError("Invalid check timeout")
    token_limit = budget.get("max_input_tokens", 120000 if "browser" in capabilities else 500000)
    if isinstance(token_limit, bool) or not isinstance(token_limit, int) or not 1000 <= token_limit <= 10000000:
        raise ValueError("Invalid input token budget")
    budget["max_input_tokens"] = token_limit
    pin = value.get("requested_model")
    if pin is not None and pin not in (LUNA, LUNA_PREVIOUS, TERRA_PREVIOUS, ORCHESTRATOR):
        raise ValueError("Unsupported model pin")
    candidates = value.get("context_candidates", [])
    if not isinstance(candidates, list) or len(candidates) > 12 or any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str)
        or not isinstance(item.get("excerpt"), str) or len(item["excerpt"].encode()) > 12000
        for item in candidates
    ):
        raise ValueError("Invalid context_candidates")
    return {"request_id": request_id, "goal": goal, "cwd": str(cwd), "allowed_files": files,
            "acceptance_commands": commands, "mandatory_instructions": instructions,
            "capabilities": capabilities,
            "budget": budget, "requested_model": pin, "deterministic": value.get("deterministic") is True,
            "question_only": question_only, "read_only": read_only,
            "context_candidates": candidates}


def ask(value):
    """Route one self-contained question without a coding workspace."""
    if not isinstance(value, dict):
        raise ValueError("Question must be an object")
    question = value.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError("Invalid one-shot question")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = STATE / "quick-workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    return submit({"request_id": value.get("request_id"), "goal": question,
        "cwd": str(workspace), "allowed_files": [], "acceptance_commands": [],
        "mandatory_instructions": "Answer this one question briefly. Do not change files or delegate.",
        "question_only": True, "requested_model": value.get("requested_model"),
        "budget": {"max_seconds": 180, "check_timeout_seconds": 15}})


def submit(value):
    task = validate_task(value)
    if task.get("requested_model") is not None:
        raise ValueError("Subagents use jev/auto; explicit model pins bypass the ladder")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    digest = hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    index = STATE / "request-index.json"
    with (STATE / ".index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        mapping = json.loads(index.read_text()) if index.exists() else {}
        previous = mapping.get(task["request_id"])
        if previous:
            if previous["digest"] != digest:
                raise ValueError("request_id reused with changed task")
            return status(previous["task_id"])
        require_provider_ladder()
        task_id = str(uuid.uuid4())
        task["task_id"] = task_id
        directory = task_path(task_id)
        directory.mkdir(mode=0o700)
        task["manifest_path"] = str(directory / "task.json")
        # Jev chooses model and depth on each Codex call. The worker never
        # pins Luna/Gemini/Gonka itself, so quotas advance inside one thread.
        decision = {"route": "auto", "model": "jev/auto", "effort": "medium",
                    "reason": "provider_ladder",
                    "worker_backend": "native" if "browser" in task["capabilities"] else "lean",
                    "jev": None}
        if decision["route"] in ("luna", "quick", "auto"):
            task["worker_model"] = decision["model"]
            task["worker_effort"] = decision["effort"]
        write_json(directory / "task.json", task)
        write_json(directory / "route.json", decision)
        state = "queued" if decision["route"] in ("luna", "quick", "auto") else (
            "needs_clarification" if decision["reason"] == "needs_clarification" else "orchestrator")
        write_json(directory / "journal.json", {"task_id": task_id, "request_id": task["request_id"],
            "input_digest": digest, "state": state, "provider": "codex",
            "model": decision["model"], "effort": decision.get("effort"), "created_at": now()})
        if state == "queued":
            write_json(directory / "baseline.json", fingerprint(task))
            snapshot_baseline(task, directory)
        mapping[task["request_id"]] = {"task_id": task_id, "digest": digest}
        write_json(index, mapping)
    if state == "queued":
        with (directory / "dispatcher.log").open("x") as log:
            os.chmod(log.name, 0o600)
            process = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "run", task_id],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        write_json(directory / "dispatcher.json", {"pid": process.pid, "process_start": process_start(process.pid)})
    return status(task_id)


def run(task_id):
    directory = task_path(task_id)
    task = json.loads((directory / "task.json").read_text())
    route = json.loads((directory / "route.json").read_text())
    lock_name = ".serial-quick-worker.lock" if route["route"] == "quick" else ".serial-native-worker.lock"
    with (STATE / lock_name).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (directory / "cancel.request").exists():
            if (directory / "quick-terminal.json").exists():
                write_json(directory / "exit.json", monitor_quick(directory, task, Bridge()))
            elif (directory / "native-terminal.json").exists():
                write_json(directory / "exit.json", cancel_native(Bridge(), directory))
            else:
                write_json(directory / "exit.json", {"state": "cancelled", "ended_at": now()})
            return
        if (directory / "exit.json").exists():
            existing = json.loads((directory / "exit.json").read_text())
            if existing.get("state") not in ("native_running", "quick_running", "lean_running"):
                return
        try:
            if route.get("worker_backend") == "lean" or route["route"] == "quick":
                if (directory / "quick-delivery-request.json").exists() and (directory / "quick-terminal.json").exists():
                    result = monitor_quick(directory, task, Bridge())
                elif (directory / "quick-create-request.json").exists():
                    result = {"state": "blocked_ambiguous", "reason": "quick_create_or_delivery_unconfirmed", "ended_at": now()}
                else:
                    write_json(directory / "journal.json", {"task_id": task_id, "request_id": task["request_id"],
                        "state": "lean_starting", "provider": "codex", "model": route["model"],
                        "effort": route["effort"], "started_at": now()})
                    bridge = Bridge()
                    started = start_quick(directory, task, bridge)
                    write_json(directory / "exit.json", started)
                    result = monitor_quick(directory, task, bridge)
            elif (directory / "native-delivery-request.json").exists() and (directory / "native-terminal.json").exists():
                result = monitor_answer(Bridge(), directory, task, task["budget"].get("max_seconds", 3600))
            elif (directory / "native-create-request.json").exists():
                result = {"state": "blocked_ambiguous", "reason": "native_create_or_delivery_unconfirmed", "ended_at": now()}
            else:
                if route.get("route") not in ("luna", "quick", "auto"):
                    return
                journal = json.loads((directory / "journal.json").read_text())
                journal.update(state="native_starting", started_at=now())
                write_json(directory / "journal.json", journal)
                bridge = Bridge()
                started = start_native_worker(directory, task, bridge)
                write_json(directory / "exit.json", started)
                result = monitor_answer(bridge, directory, task, task["budget"].get("max_seconds", 3600))
            write_json(directory / "exit.json", result)
        except Exception as error:
            write_json(directory / "exit.json", {"state": "blocked", "reason": type(error).__name__,
                "detail": str(error)[:500], "ambiguous_create": (directory / "native-create-request.json").exists()
                and not (directory / "native-terminal.json").exists(), "ended_at": now()})


def status(task_id):
    directory = task_path(task_id)
    task = json.loads((directory / "task.json").read_text())
    journal = json.loads((directory / "journal.json").read_text())
    route = json.loads((directory / "route.json").read_text())
    result = {"task_id": task_id, "request_id": task["request_id"], "state": journal["state"],
              "route": route["route"], "worker_backend": route.get("worker_backend"),
              "model": route["model"], "effort": route.get("effort"),
              "reason": route["reason"], "confidence": route.get("confidence")}
    if (directory / "native-terminal.json").exists():
        binding = json.loads((directory / "native-terminal.json").read_text())
        result["native_session_id"] = binding["session_id"]
        try:
            view_for(Bridge(), binding)
            result["terminal_verified"] = True
        except Exception:
            result["terminal_verified"] = False
    if (directory / "quick-terminal.json").exists():
        binding = json.loads((directory / "quick-terminal.json").read_text())
        result["session_id"] = binding["session_id"]
        try:
            quick_view(Bridge(), binding)
            result["terminal_verified"] = True
        except Exception:
            result["terminal_verified"] = False
    if (directory / "exit.json").exists():
        result.update(json.loads((directory / "exit.json").read_text()))
    elif (directory / "dispatcher.json").exists():
        dispatcher = json.loads((directory / "dispatcher.json").read_text())
        if process_start(dispatcher["pid"]) != dispatcher["process_start"]:
            result["state"] = "lost_monitor"
            result["recoverable_with_run"] = True
    return result


def collect(task_id):
    directory = task_path(task_id)
    result = status(task_id)
    lean = result.get("worker_backend") == "lean" or (directory / "quick-answer.txt").exists()
    answer = directory / "quick-answer.txt" if lean else directory / "native-answer.json"
    full = (answer.read_text() if lean else json.loads(answer.read_text()).get("answer", "")) if answer.exists() else ""
    checks = [{key: row.get(key) for key in ("argv", "exit_code", "timed_out", "log_sha256") if key in row}
              for row in result.get("checks", [])[:20]]
    jev = json.loads((directory / "route.json").read_text()).get("jev")
    jev_usage = jev.get("usage") if isinstance(jev, dict) and not jev.get("cache_hit") else {"input_tokens": 0, "output_tokens": 0}
    return {key: result.get(key) for key in ("task_id", "request_id", "state", "route", "worker_backend", "model",
            "effort", "reason", "confidence", "session_id", "native_session_id", "terminal_verified",
            "turn_context_verified") if key in result} | {
            "summary": full[:2400], "summary_truncated": len(full) > 2400,
            "checks": checks, "changed_files": result.get("changed_files", [])[:200],
            "revision_sha256": result.get("revision_sha256"), "patch": result.get("patch"),
            "usage": result.get("usage"), "usage_source": result.get("usage_source", "unknown"),
            "jev_usage": jev_usage, "artifact_dir": str(directory),
            "result_file": str(answer) if answer.exists() else None}


def wait_result(task_id, wait_seconds=50):
    """Wait in code, then return one bounded result to the coordinator."""
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 50:
        raise ValueError("wait_seconds must be between 0 and 50")
    active = {"queued", "quick_starting", "quick_running", "lean_starting", "lean_running",
              "native_starting", "native_running"}
    deadline = time.monotonic() + wait_seconds
    while wait_seconds and status(task_id)["state"] in active and time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    return collect(task_id)


def execute(value):
    """Route, start if needed, and wait for the worker without model polling."""
    seconds = value.get("wait_seconds", 50)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 50:
        raise ValueError("wait_seconds must be between 0 and 50")
    result = submit(value)
    return wait_result(result["task_id"], seconds)


def cancel(task_id):
    directory = task_path(task_id)
    if not directory.is_dir():
        raise FileNotFoundError("Unknown task")
    current = status(task_id)
    active = {"queued", "quick_starting", "quick_running", "lean_starting", "lean_running",
              "native_starting", "native_running", "lost_monitor"}
    if current["state"] not in active:
        return {**current, "cancel_requested": False}
    marker = directory / "cancel.request"
    marker.touch(mode=0o600, exist_ok=True)
    result = status(task_id)
    if result["state"] in ("queued", "native_starting", "native_running", "quick_starting", "quick_running",
                           "lean_starting", "lean_running"):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = status(task_id)
            if result["state"] not in ("queued", "native_starting", "native_running", "quick_starting", "quick_running",
                                       "lean_starting", "lean_running"):
                break
            time.sleep(0.25)
    if result["state"] in ("queued", "native_starting", "native_running", "lost_monitor") and (directory / "native-terminal.json").exists():
        result = cancel_native(Bridge(), directory)
        write_json(directory / "exit.json", result)
    if result["state"] in ("queued", "quick_starting", "quick_running", "lean_starting", "lean_running",
                           "lost_monitor") and (directory / "quick-terminal.json").exists():
        result = monitor_quick(directory, json.loads((directory / "task.json").read_text()), Bridge())
        write_json(directory / "exit.json", result)
    return {**status(task_id), "cancel_requested": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("submit").add_argument("task_json", help="Task JSON file, or - for stdin")
    sub.add_parser("execute").add_argument("task_json", help="Task JSON file, or - for stdin")
    sub.add_parser("ask").add_argument("question_json", help="One-shot question JSON file, or - for stdin")
    for action in ("status", "collect", "cancel", "run"):
        sub.add_parser(action).add_argument("task_id")
    args = parser.parse_args()
    if args.action == "submit":
        return submit(json.load(sys.stdin) if args.task_json == "-" else json.loads(Path(args.task_json).read_text()))
    if args.action == "execute":
        return execute(json.load(sys.stdin) if args.task_json == "-" else json.loads(Path(args.task_json).read_text()))
    if args.action == "ask":
        return ask(json.load(sys.stdin) if args.question_json == "-" else json.loads(Path(args.question_json).read_text()))
    if args.action == "run":
        run(args.task_id)
        return {"task_id": args.task_id}
    return globals()[args.action](args.task_id)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "detail": str(error)[:500]}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
