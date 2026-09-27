#!/usr/bin/env python3
"""Owned CanvasTTY terminal creation, input and read-only monitoring. No deletion."""
import argparse
import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import sys
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid

from runner import save_new


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Bridge redirects forbidden")


class Bridge:
    def __init__(self):
        root = Path(os.environ.get("CANVASTTY_G2_ROOT", str(Path.home() / "Library/Application Support/canvastty/native-orchestration")))
        descriptor = json.loads((root / "connection.json").read_text())
        parsed = urllib.parse.urlsplit(descriptor["address"])
        if (descriptor.get("service") != "canvastty-g2" or parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise RuntimeError("Unexpected bridge identity/address")
        if descriptor.get("backend") != "electron-terminal-manager" or not isinstance(descriptor.get("pid"), int):
            raise RuntimeError("Not a native CanvasTTY API descriptor; standalone PTY bridges are not supported")
        pid = descriptor["pid"]
        listener = subprocess.run(["/usr/sbin/lsof", "-nP", "-t", "-iTCP:" + str(parsed.port), "-sTCP:LISTEN"], capture_output=True, text=True, check=False)
        if str(pid) not in listener.stdout.split():
            raise RuntimeError("CanvasTTY API listener does not match its descriptor PID; start the prepared CanvasTTY build")
        command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, check=False).stdout.strip()
        if ".app/Contents/MacOS/" not in command or Path(command).name not in ("CanvasTTY", "Electron"):
            raise RuntimeError("API listener is not the native CanvasTTY Electron process")
        token_path = Path(descriptor["authTokenFile"]).resolve(strict=True)
        if token_path.parent != root.resolve() or token_path.name != "token":
            raise RuntimeError("Unexpected bridge token location")
        self.token = token_path.read_text().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", self.token):
            raise RuntimeError("Invalid bridge token format")
        self.address = descriptor["address"].rstrip("/")
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        info = self.call("/api/info")
        if info.get("backend") != "electron-terminal-manager" or info.get("pid") != pid:
            raise RuntimeError("Native CanvasTTY API identity check failed")

    def call(self, path, data=None):
        allowed = ("/api/info", "/api/sessions", "/g2/api/create", "/g2/api/control", "/api/interrupt")
        if path not in allowed and not path.startswith(("/g2/api/terminal?id=", "/api/screen?sessionId=")):
            raise RuntimeError("Endpoint not allowed")
        request = urllib.request.Request(self.address + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.client.open(request, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Bridge HTTP {error.code}; inspect current terminal before retry") from None


def binding(worker):
    worker = Path(worker).resolve(strict=True)
    value = json.loads((worker / "terminal.json").read_text())
    uuid.UUID(value["session_id"])
    if value["worker"] != str(worker):
        raise RuntimeError("Worker binding path mismatch")
    return value


def status(worker):
    worker = Path(worker).resolve(strict=True)
    pinned = binding(worker)
    records = sorted(worker.glob("*.queued.json"), key=lambda path: (
        json.loads(path.read_text()).get("sequence", 0), path.stat().st_mtime_ns, path.name,
    ))
    result = {"worker": worker.name, "session_id": pinned["session_id"], "state": "created"}
    if not records:
        return result
    base = Path(str(records[-1])[:-len(".queued.json")])
    started = base.with_suffix(".started.json")
    ended = base.with_suffix(".exit.json")
    result.update(invocation=base.name, state="queued")
    if started.exists():
        info = json.loads(started.read_text())
        result.update(runner_pid=info["runner_pid"], state="running")
        try:
            os.kill(info["runner_pid"], 0)  # Liveness only, never a signal/interrupt.
        except ProcessLookupError:
            result["state"] = "lost_process"
    events = base.with_suffix(".events.jsonl")
    last_message = ""
    if events.exists():
        result["last_event_at"] = datetime.fromtimestamp(events.stat().st_mtime, timezone.utc).isoformat()
        with events.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                result["last_event"] = event.get("type")
                if event.get("type") == "thread.started":
                    result["thread_id"] = event.get("thread_id")
                if event.get("type") in ("error", "turn.failed"):
                    result["agent_error"] = True
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    last_message = item.get("text", "")
        result["last_message"] = last_message[:1600]
        reported = re.search(r"(?m)^RESULT:\s*([a-z_]+)", last_message)
        if reported:
            result["reported_result"] = reported.group(1)
    if ended.exists():
        end = json.loads(ended.read_text())
        result.update(end)
        result["state"] = "turn_finished_needs_review" if end["exit_code"] == 0 and not result.get("agent_error") else "failed"
        if base.with_suffix(".result.md").exists():
            result["result_file"] = str(base.with_suffix(".result.md"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("sessions")
    for action in ("create", "inspect", "interrupt"):
        sub.add_parser(action).add_argument("worker", type=Path)
    start = sub.add_parser("start")
    start.add_argument("worker", type=Path)
    start.add_argument("prompt", type=Path)
    start.add_argument("--resume")
    start.add_argument("--retry-failed", action="store_true",
                       help="Explicit retry only after inspection and confirmed child shutdown")
    start.add_argument("--review-github-writes", action="store_true",
                       help="Compatibility flag; worker execution uses the requested YOLO / never mode")
    sub.add_parser("status").add_argument("workers", nargs="+", type=Path)
    args = parser.parse_args()
    if args.action == "status":
        return [status(worker) for worker in args.workers]
    bridge = Bridge()
    if args.action == "sessions":
        info = bridge.call("/api/info")
        if info.get("service") != "canvastty-g2":
            raise RuntimeError("Unexpected service")
        return {"version": info.get("version"), **bridge.call("/api/sessions")}
    worker = args.worker.resolve(strict=True)
    if args.action == "start":
        # Serialize admission + delivery for this worker, never across unrelated workers.
        lock = (worker / ".control.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.action == "create":
        if (worker / "terminal.json").exists():
            raise FileExistsError("File exists: terminal.json; this worker already owns a terminal")
        before = bridge.call("/api/sessions")
        # Exclusive marker: never silently retry the non-idempotent create endpoint.
        save_new(worker / "creation-request.json", {"before_ids": [s["id"] for s in before["sessions"]]})
        session = bridge.call("/g2/api/create", {"provider": "terminal"})["session"]
        uuid.UUID(session["id"])
        value = {"worker": str(worker), "session_id": session["id"], "provider": session["provider"],
                 "created_at": datetime.now(timezone.utc).isoformat()}
        save_new(worker / "terminal.json", value)
        return value
    pinned = binding(worker)
    session_id = pinned["session_id"]
    if args.action == "inspect":
        return {"presentation": bridge.call("/g2/api/terminal?id=" + session_id),
                "screen": bridge.call("/api/screen?sessionId=" + session_id)}
    if args.action == "interrupt":
        return bridge.call("/api/interrupt", {"sessionId": session_id})
    current = status(worker)
    retry = (args.retry_failed and current["state"] == "failed" and current.get("child_stopped") is True)
    if current["state"] not in ("created", "turn_finished_needs_review") and not retry:
        raise RuntimeError(f"Cannot send over state {current['state']}; inspect first")
    prompt = args.prompt.resolve(strict=True)
    if not prompt.is_relative_to(worker):
        raise RuntimeError("Prompt must be in the worker directory")
    if args.resume:
        uuid.UUID(args.resume)
        if args.resume != current.get("thread_id"):
            raise RuntimeError("Resume must match the exact previous worker thread")
    elif current["state"] != "created" and not (retry and not current.get("thread_id")):
        raise RuntimeError("Use explicit --resume after the first turn")
    view = bridge.call("/g2/api/terminal?id=" + session_id)
    if view.get("interaction") or view["session"]["provider"] != "terminal":
        raise RuntimeError("Unexpected terminal provider or interaction")
    # The orchestrator must inspect the current shell before this write; no blind screen heuristics.
    sequence = max((json.loads(p.read_text()).get("sequence", 0)
                    for p in worker.glob("*.queued.json")), default=0) + 1
    invocation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + uuid.uuid4().hex[:6]
    request_id = str(uuid.uuid4())
    save_new(worker / (invocation + ".queued.json"), {
        "session_id": session_id, "request_id": request_id, "prompt": str(prompt), "resume": args.resume,
        "review_github_writes": args.review_github_writes,
        "sequence": sequence, "previous_invocation": current.get("invocation"),
    })
    command = shlex.join([sys.executable, "-u", str(Path(__file__).with_name("runner.py")), str(worker), invocation])
    receipt = bridge.call("/g2/api/control", {
        "sessionId": session_id, "requestId": request_id, "action": "text", "text": command,
    })
    save_new(worker / (invocation + ".receipt.json"), receipt)
    return {"worker": worker.name, "invocation": invocation, **receipt}


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
