#!/usr/bin/env python3
"""Run one YOLO Codex turn inside an owned CanvasTTY shell. No cleanup."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def save_new(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worker", type=Path)
    parser.add_argument("invocation")
    args = parser.parse_args()
    worker = args.worker.resolve(strict=True)
    if not args.invocation.isalnum():
        parser.error("Invalid invocation")
    base = worker / args.invocation
    request = json.loads(base.with_suffix(".queued.json").read_text())
    binding = json.loads((worker / "terminal.json").read_text())
    if request["session_id"] != binding["session_id"]:
        raise SystemExit("Terminal binding mismatch")
    prompt = Path(request["prompt"]).resolve(strict=True)
    if not prompt.is_relative_to(worker):
        raise SystemExit("Prompt must be inside the owned worker directory")
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("Codex executable unavailable")
    approval = "never"
    command = [codex, "exec",
               "--json", "--color", "never", "-C", str(worker), "--skip-git-repo-check"]
    if request.get("resume"):
        command += ["resume", "--dangerously-bypass-approvals-and-sandbox", request["resume"]]
    else:
        command += ["--dangerously-bypass-approvals-and-sandbox"]
    command += ["-"]
    save_new(base.with_suffix(".started.json"), {
        "runner_pid": os.getpid(), "started_at": now(), "worker": str(worker),
        "session_id": binding["session_id"], "argv": command,
    })
    print(f"CanvasTTY Codex worker: {worker.name}", flush=True)
    print("YOLO / Full Access | approval: never | no deletion / no system changes", flush=True)
    result = ""
    process = None
    try:
        with base.with_suffix(".events.jsonl").open("x", encoding="utf-8") as events, \
             base.with_suffix(".stderr.log").open("x", encoding="utf-8") as errors:
            os.chmod(events.name, 0o600)
            os.chmod(errors.name, 0o600)
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=errors, cwd=worker, text=True)
            save_new(base.with_suffix(".process.json"), {"codex_pid": process.pid})
            process.stdin.write(prompt.read_text(encoding="utf-8"))
            process.stdin.close()
            for line in process.stdout:
                events.write(line)
                events.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type", "unknown")
                item = event.get("item", {})
                if kind == "thread.started":
                    print("THREAD: " + event.get("thread_id", "unknown"), flush=True)
                elif kind == "item.completed" and item.get("type") == "agent_message":
                    result = item.get("text", "")
                    print(result, flush=True)
                elif kind in ("item.started", "item.completed"):
                    print(f"{now()[11:19]} {kind}: {item.get('type', 'item')}", flush=True)
                elif kind in ("turn.started", "turn.completed", "turn.failed", "error"):
                    print(f"{now()[11:19]} {kind}", flush=True)
            returncode = process.wait()
        with base.with_suffix(".result.md").open("x", encoding="utf-8") as output:
            os.chmod(output.name, 0o600)
            output.write(result + "\n")
        save_new(base.with_suffix(".exit.json"), {
            "exit_code": returncode, "child_exit_code": returncode,
            "child_stopped": True, "ended_at": now(),
        })
        print(f"CODEX_EXIT: {returncode}; result: {base.with_suffix('.result.md')}", flush=True)
        return returncode
    except BaseException as error:
        # A failed runner must not leave its own child working unnoticed.
        # Popen retains ownership of this unreaped child; never select processes by name.
        child_code = None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            child_code = process.wait()
        if not base.with_suffix(".exit.json").exists():
            save_new(base.with_suffix(".exit.json"), {
                "exit_code": 130 if isinstance(error, KeyboardInterrupt) else 1,
                "child_exit_code": child_code, "child_stopped": True,
                "ended_at": now(), "runner_error": type(error).__name__,
            })
        raise


if __name__ == "__main__":
    sys.exit(main())
