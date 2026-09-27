#!/usr/bin/env python3
"""One pi RPC turn inside an owned, visible CanvasTTY terminal."""
import hashlib
import difflib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

from launcher import prepare, MODEL


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temp.open("x") as output:
        os.chmod(temp, 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
    os.replace(temp, path)


def process_start(pid):
    return subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True).stdout.strip()


def classify_model_error(detail):
    detail = str(detail)
    if re.search(r"\b400\b", detail) and re.search(r"location is not supported|unsupported (?:user )?location|region.*not supported", detail, re.I):
        return "egress_blocked", None
    if re.search(r"\b(401|403)\b", detail):
        return "authentication_or_permission_error", None
    if re.search(r"\b429\b", detail):
        match = re.search(r"retry[- ]after\s*[:=]?\s*(\d{1,3})", detail, re.I)
        retry_after = int(match.group(1)) if match else None
        return ("quota_error" if re.search(r"quota|exhaust", detail, re.I) else "rate_limited"), retry_after
    return "model_error", None


def usage_summary(rows, assistant_count):
    keys = ("input", "output", "cacheRead", "cacheWrite")
    valid = [row for row in rows if isinstance(row, dict) and any(
        isinstance(row.get(key), (int, float)) and row.get(key, 0) > 0 for key in keys)]
    usage = {key: sum(row.get(key, 0) for row in valid if isinstance(row.get(key), (int, float))) for key in keys} if valid else None
    return usage, {
        "assistant_messages": assistant_count,
        "usage_objects_seen": len(rows),
        "valid_nonzero_usage_objects": len(valid),
        "coverage": "complete" if assistant_count > 0 and len(valid) == assistant_count else "partial_or_unknown",
    }


def may_run_checks(settled, accepted, errors, returncode, terminated_by_runner):
    return settled and accepted and not errors and (returncode == 0 or (terminated_by_runner and returncode in (-15, 143)))


def fingerprint(task):
    cwd = Path(task["cwd"]).resolve(strict=True)
    files = []
    for name in task["allowed_files"]:
        p = (cwd / name).resolve()
        if not p.is_relative_to(cwd):
            raise ValueError("File left task cwd")
        candidates = [p] if not p.is_dir() else sorted(p.rglob("*"))
        if not candidates:
            files.append({"path": str(p), "empty_directory": True})
        for candidate in candidates:
            if candidate.is_symlink():
                files.append({"path": str(candidate), "symlink": True})
            elif candidate.is_file():
                files.append({"path": str(candidate), "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()})
            elif not candidate.exists():
                files.append({"path": str(candidate), "missing": True})
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"files": files, "revision_sha256": digest}


def snapshot_baseline(task, task_dir):
    """Private text snapshots make a reviewable patch without depending on git."""
    base = task_dir / "baseline-files"
    base.mkdir(mode=0o700, exist_ok=True)
    manifest = {}
    for item in fingerprint(task)["files"]:
        path = Path(item["path"])
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_000_000:
            continue
        try:
            data = path.read_bytes()
            data.decode("utf-8")
        except (UnicodeError, OSError):
            continue
        name = hashlib.sha256(str(path).encode()).hexdigest() + ".txt"
        with (base / name).open("xb") as output:
            os.chmod(base / name, 0o600)
            output.write(data)
        manifest[str(path)] = name
    write_json(task_dir / "baseline-manifest.json", manifest)


def make_patch(task_dir, changed_files):
    manifest_path = task_dir / "baseline-manifest.json"
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text())
    parts = []
    for name in changed_files:
        path = Path(name)
        try:
            old = (task_dir / "baseline-files" / manifest[name]).read_text().splitlines(keepends=True) if name in manifest else []
            new = path.read_text().splitlines(keepends=True) if path.is_file() and path.stat().st_size <= 1_000_000 else []
        except (UnicodeError, OSError):
            continue
        parts.extend(difflib.unified_diff(old, new, fromfile="a/" + name, tofile="b/" + name))
    patch = task_dir / "changes.patch"
    patch.write_text("".join(parts))
    patch.chmod(0o600)
    return {"patch_file": str(patch), "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}


def run_checks(task, task_dir):
    checks = []
    for index, argv in enumerate(task["acceptance_commands"]):
        started = time.monotonic()
        try:
            result = subprocess.run(argv, cwd=task["cwd"], capture_output=True, timeout=task["budget"].get("check_timeout_seconds", 120))
            log_path = task_dir / ("check-" + str(index) + ".log")
            with log_path.open("wb") as output:
                os.chmod(log_path, 0o600)
                output.write(b"STDOUT\n" + result.stdout + b"\nSTDERR\n" + result.stderr)
            item = {"argv": argv, "exit_code": result.returncode, "duration_seconds": round(time.monotonic()-started, 3),
                    "log_file": str(log_path), "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest()}
        except subprocess.TimeoutExpired:
            item = {"argv": argv, "exit_code": None, "timed_out": True}
        checks.append(item)
    return checks


def main(task_dir):
    task_dir = Path(task_dir).resolve(strict=True)
    task = json.loads((task_dir / "task.json").read_text())
    if (task_dir / "cancel.request").exists():
        write_json(task_dir / "exit.json", {"state": "cancelled", "ended_at": now()})
        return 130
    argv, env = prepare(task_dir, task)
    write_json(task_dir / "runner.json", {"pid": os.getpid(), "process_start": process_start(os.getpid()), "started_at": now()})
    print("Jev Gemini worker " + task["task_id"] + " | " + MODEL, flush=True)
    usage_rows = []
    assistant_count = 0
    tool_calls = 0
    errors = []
    final_text = ""
    started = time.monotonic()
    settled = False
    accepted = False
    retry_after = None
    retry_count = 0
    terminated_by_runner = False
    prompt_id = task["task_id"]
    with (task_dir / "events.jsonl").open("x") as log, (task_dir / "pi-stderr.log").open("x") as stderr:
        os.chmod(log.name, 0o600)
        os.chmod(stderr.name, 0o600)
        child = subprocess.Popen(argv, cwd=task["cwd"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=stderr, text=True, bufsize=1)
        write_json(task_dir / "pi-process.json", {"pid": child.pid, "process_start": process_start(child.pid)})
        prompt = "Goal: " + task["goal"] + "\nAllowed files: " + json.dumps(task["allowed_files"]) + "\nAcceptance commands: " + json.dumps(task["acceptance_commands"]) + "\nDo not change files outside ownership. Report changed files and remaining issues."
        selection = task_dir / "context-selection.json"
        if selection.exists():
            excerpts = json.loads(selection.read_text()).get("kept", [])
            if excerpts:
                prompt += "\nRelevant supplied context:\n" + "\n".join("[" + c["id"] + "] " + c["excerpt"] for c in excerpts)
        shortlist = task_dir / "shortlist-selection.json"
        if shortlist.exists():
            candidates = json.loads(shortlist.read_text()).get("kept", [])
            if candidates:
                prompt += "\nJev-selected shortlist candidates (verify before use):\n" + "\n".join("[" + c["id"] + "] " + c["excerpt"] for c in candidates)
        child.stdin.write(json.dumps({"id": prompt_id, "type": "prompt", "message": prompt}) + "\n")
        child.stdin.flush()
        deadline = started + task["budget"].get("max_seconds", 3600)
        # select keeps cancellation and time budget effective even when pi is silent.
        import select
        pending = b""
        try:
            while time.monotonic() < deadline:
                if (task_dir / "cancel.request").exists():
                    child.stdin.write(json.dumps({"type": "abort"}) + "\n")
                    child.stdin.flush()
                    errors.append("cancel_requested")
                    break
                if b"\n" not in pending:
                    ready, _, _ = select.select([child.stdout.fileno()], [], [], 0.5)
                    if not ready:
                        if child.poll() is not None:
                            break
                        continue
                    chunk = os.read(child.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    pending += chunk
                    if b"\n" not in pending:
                        continue
                raw, pending = pending.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace") + "\n"
                log.write(line)
                log.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    errors.append("invalid_rpc_json")
                    continue
                kind = event.get("type")
                if kind == "response" and event.get("id") == prompt_id:
                    accepted = event.get("success") is True
                    if not accepted:
                        errors.append("prompt_rejected")
                elif kind == "tool_execution_start":
                    tool_calls += 1
                    print("tool " + str(event.get("toolName", "unknown")), flush=True)
                elif kind == "message_update":
                    delta = event.get("assistantMessageEvent", {})
                    if delta.get("type") == "text_delta":
                        print(delta.get("delta", ""), end="", flush=True)
                elif kind == "message_end":
                    message = event.get("message", {})
                    if message.get("role") == "assistant":
                        assistant_count += 1
                        if isinstance(message.get("usage"), dict):
                            usage_rows.append(message["usage"])
                        final_text = "".join(c.get("text", "") for c in message.get("content", []) if c.get("type") == "text")
                        if message.get("stopReason") == "error" or message.get("errorMessage"):
                            detail = str(message.get("errorMessage", ""))
                            reason, retry_after = classify_model_error(detail)
                            errors.append(reason)
                elif kind == "agent_settled":
                    if ("rate_limited" in errors and retry_after is not None and retry_after <= 30
                            and retry_count == 0 and tool_calls == 0 and time.monotonic() + retry_after < deadline
                            and not (task_dir / "cancel.request").exists()):
                        print("Transient 429; waiting Retry-After " + str(retry_after) + "s", flush=True)
                        time.sleep(retry_after)
                        errors.remove("rate_limited")
                        retry_count += 1
                        prompt_id = task["task_id"] + "-retry"
                        child.stdin.write(json.dumps({"id": prompt_id, "type": "prompt", "message": prompt}) + "\n")
                        child.stdin.flush()
                        continue
                    settled = True
                    break
            else:
                errors.append("budget_timeout")
        finally:
            if child.poll() is None:
                terminated_by_runner = True
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
    (task_dir / "result.md").write_text(final_text)
    (task_dir / "result.md").chmod(0o600)
    checks = run_checks(task, task_dir) if may_run_checks(settled, accepted, errors, child.returncode, terminated_by_runner) else []
    fp = fingerprint(task)
    baseline = json.loads((task_dir / "baseline.json").read_text()) if (task_dir / "baseline.json").exists() else {"files": []}
    before = {item["path"]: item for item in baseline["files"]}
    after = {item["path"]: item for item in fp["files"]}
    changed_files = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    patch = make_patch(task_dir, changed_files)
    usage, coverage = usage_summary(usage_rows, assistant_count)
    # A successful RPC run is still an unreviewed worker claim; checks are independent evidence.
    state = "turn_finished_needs_review" if settled and accepted and not errors else "failed"
    if "cancel_requested" in errors:
        state = "cancelled"
    result = {"state": state, "provider": "terminal", "model": MODEL, "account_known": None,
              "accepted": accepted, "settled": settled, "pi_exit_code": child.returncode,
              "controlled_pi_shutdown": terminated_by_runner, "errors": errors,
              "checks": checks, **fp, "result_file": str(task_dir / "result.md"),
              "baseline_file": str(task_dir / "baseline.json"), "changed_files": changed_files,
              "patch": patch,
              "events_file": str(task_dir / "events.jsonl"), "tool_calls": tool_calls, "attempts": retry_count + 1,
              "usage": usage, "usage_source": "pi_assistant" if usage else "unknown",
              "usage_coverage": coverage,
              "duration_seconds": round(time.monotonic()-started, 3),
              "ended_at": now()}
    write_json(task_dir / "exit.json", result)
    print("worker state: " + state, flush=True)
    return 0 if state == "turn_finished_needs_review" else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except BaseException as error:
        path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
        if path and path.is_dir() and not (path / "exit.json").exists():
            write_json(path / "exit.json", {"state": "cancelled" if (path / "cancel.request").exists() else "failed",
                "runner_error": type(error).__name__, "ended_at": now()})
        print("runner failed: " + type(error).__name__, file=sys.stderr)
        raise
