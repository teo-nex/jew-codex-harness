#!/usr/bin/env python3
"""Lean Codex worker with a small plugin surface for questions and bounded code."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import tomllib

from pi_worker import fingerprint, make_patch, run_checks, now, write_json
from native_fallback import NATIVE_WORKER_MODELS

AUTO_MODEL = "jev/auto"


def capability_skill_paths(capabilities, home=None):
    home = Path.home() if home is None else Path(home)
    variants = {
        "computer": ("openai-bundled/computer-use", "computer-use"),
        "pdf": ("openai-primary-runtime/pdf", "pdf"),
        "documents": ("openai-primary-runtime/documents", "documents"),
    }
    paths = []
    for capability in capabilities:
        if capability not in variants:
            continue
        family, skill = variants[capability]
        root = home / ".codex/plugins/cache" / family
        found = sorted(root.glob("*/skills/" + skill + "/SKILL.md"), reverse=True)
        if found:
            paths.append(str(found[0]))
    return paths


def capability_instructions(paths):
    blocks = []
    total = 0
    for name in paths:
        path = Path(name)
        data = path.read_text(encoding="utf-8")
        total += len(data)
        if len(data) > 50000 or total > 80000:
            raise ValueError("Capability skill too large for worker context")
        blocks.append("Selected capability skill " + path.parent.name + ":\n" + data)
    return "\n\n".join(blocks)


def focused_context_hint(task):
    cwd = Path(task["cwd"])
    tests = []
    for argv in task.get("acceptance_commands", []):
        for word in argv:
            if word.startswith("test_") and "/" not in word and "\\" not in word:
                candidate = word if word.endswith(".py") else word + ".py"
                if (cwd / candidate).is_file() and candidate not in tests:
                    tests.append(candidate)
    git = any((parent / ".git").exists() for parent in (cwd, *cwd.parents))
    return ("\nRelevant test files: " + json.dumps(tests)
            + ("\nThis workspace has no Git metadata; do not run git commands." if not git else "")
            + "\nRead only the named owned and test files in one bounded read call, then make the smallest patch. "
              "Do not list the workspace, inspect Codex hooks, or search agent configuration. "
              "If a tool is denied, stop and report its exact blocker.")


def command(task):
    question_only = task.get("question_only") is True
    read_only = task.get("read_only") is True
    if question_only and task.get("allowed_files"):
        raise ValueError("One-shot question cannot own files")
    if read_only and task.get("allowed_files"):
        raise ValueError("Read-only worker cannot own writable files")
    if not question_only and not read_only and not task.get("allowed_files"):
        raise ValueError("Coding worker needs explicit file ownership")
    model = task["worker_model"]
    effort = task["worker_effort"]
    if model not in (*NATIVE_WORKER_MODELS, AUTO_MODEL) or effort not in ("low", "medium"):
        raise ValueError("Lean worker model or effort unsupported")
    config = tomllib.loads((Path.home() / ".codex/config.toml").read_text())
    capabilities = set(task.get("capabilities", []))
    if "browser" in capabilities:
        raise ValueError("CanvasTTY browser workers require the native backend")
    enabled_mcp = set()
    enabled_plugins = set()
    if "computer" in capabilities:
        enabled_mcp.update(("node_repl", "computer-use"))
        enabled_plugins.add("computer-use@openai-bundled")
    if "pdf" in capabilities:
        enabled_plugins.add("pdf@openai-primary-runtime")
    if "documents" in capabilities:
        enabled_plugins.add("documents@openai-primary-runtime")
    flags = []
    for name in config.get("mcp_servers", {}):
        if name not in enabled_mcp:
            flags.extend(["-c", f"mcp_servers.{name}.enabled=false"])
    for name in config.get("plugins", {}):
        if name not in enabled_plugins:
            flags.extend(["-c", f"plugins.{name}.enabled=false"])
    flags.extend(["-c", "apps._default.enabled=false", "-c", "features.memories=false",
                  "-c", f"features.hooks={'true' if model == AUTO_MODEL else 'false'}",
                  "-c", "agents.enabled=false",
                  "-c", f'model_reasoning_effort="{effort}"', "-c", 'approval_policy="never"',
                  "-c", 'sandbox_mode="danger-full-access"'])
    skill_paths = capability_skill_paths(capabilities)
    if len(skill_paths) != len(capabilities):
        raise RuntimeError("Requested capability skill is not installed")
    skill_prefix = capability_instructions(skill_paths)
    manifest_prefix = ("JEV_WORKER_MANIFEST_V1: " + str(task["manifest_path"]) + "\n"
                       if task.get("manifest_path") else "")
    if question_only:
        prompt = manifest_prefix + task["goal"] + "\nAnswer briefly. Do not use tools or edit files."
    elif read_only:
        prompt = (manifest_prefix + (skill_prefix + "\n\n" if skill_prefix else "")
            + "Selected capability skills above are already loaded; do not read those files again.\n"
            + "This is a read-only task in an empty isolated folder. Do not write files, delegate, or inspect hook internals.\n"
            + "Goal: " + task["goal"] + "\nMandatory instructions: " + task["mandatory_instructions"]
            + "\nReport only the observed result or the exact blocker.")
    else:
        prompt = (manifest_prefix + (skill_prefix + "\n\n" if skill_prefix else "")
            + "Selected capability skills above are already loaded; do not read those files again.\n"
            + "Complete this bounded task directly; applicable AGENTS.md is already supplied by Codex.\nGoal: " + task["goal"]
            + "\nWorking directory: " + task["cwd"]
            + "\nAllowed files: " + json.dumps(task["allowed_files"])
            + "\nRequired checks: " + json.dumps(task["acceptance_commands"])
            + focused_context_hint(task)
            + "\nMandatory instructions: " + task["mandatory_instructions"]
            + "\nDo not edit files outside ownership or create subagents. For edits, use one valid apply_patch envelope: *** Begin Patch, *** Update File: owned path, an @@ hunk, context lines starting with a space, removed lines starting -, added lines starting +, *** End Patch. Never use <<<<, ==== or >>>> merge markers. The harness runs listed acceptance commands after your turn; do not run them yourself. After one denied tool call, report the denial rather than inspecting hook internals or trying command variants. Report completion briefly.")
    return ["codex", "exec", "--json", "--skip-git-repo-check",
            "-m", model, "-C", task["cwd"], *flags, prompt]


def resume_command(task, thread_id):
    initial = command(task)
    config_flags = initial[initial.index("-C") + 2:-1]
    manifest = ("JEV_WORKER_MANIFEST_V1: " + str(task["manifest_path"]) + "\n"
                if task.get("manifest_path") else "")
    feedback = (manifest + "Goal: " + str(task.get("goal") or "")[:800] + "\n"
                "The acceptance check failed and no file change or non-read tool action occurred. "
                "Retry once in this same thread. Use a complete *** Begin Patch / *** Update File / @@ hunk "
                "with -/+ lines / *** End Patch envelope; never use merge markers. "
                "Do not run acceptance commands yourself, delegate, or inspect hook internals. "
                "Finish after the intended edit; the harness will verify it.")
    return ["codex", "exec", "resume", "--json", "--skip-git-repo-check", "-m",
            task["worker_model"], *config_flags, thread_id, feedback]


def safe_read_command(item):
    command = item.get("command")
    if isinstance(command, list) and len(command) >= 3 and command[-2] == "-lc":
        command = command[-1]
    if not isinstance(command, str) or re.search(r"[`$;&|<>\n]", command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    if Path(words[0]).name in ("sh", "bash", "zsh") and len(words) == 3 and words[1] == "-lc":
        command = words[2]
        if re.search(r"[`$;&|<>\n]", command):
            return False
        try:
            words = shlex.split(command)
        except ValueError:
            return False
        if not words:
            return False
    base = Path(words[0]).name
    if base == "pwd" and len(words) == 1:
        return True
    if base in ("cat", "head", "tail", "stat", "od") and len(words) >= 2:
        return True
    if base == "rg" and not any(word.startswith(("--pre", "--pager", "--search-zip")) for word in words[1:]):
        return True
    return False


def summarize_events(stdout):
    thread_id = None
    answer = ""
    usage_rows = []
    completed = False
    side_effects = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
        if event.get("type") == "turn.completed":
            completed = True
            if isinstance(event.get("usage"), dict):
                usage_rows.append(event["usage"])
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            answer = item.get("text", "")
        if event.get("type") in ("item.started", "item.completed"):
            kind = item.get("type")
            if kind == "command_execution" and not safe_read_command(item):
                side_effects = True
            elif kind in ("file_change", "mcp_tool_call", "web_search_call", "computer_call"):
                side_effects = True
    usage = ({key: sum(row.get(key, 0) for row in usage_rows if isinstance(row.get(key), int))
              for key in set().union(*(row.keys() for row in usage_rows))}
             if usage_rows else None)
    return {"thread_id": thread_id, "answer": answer, "usage": usage,
            "completed": completed, "side_effects": side_effects}


def recovery_eligible(task, first, returncode, baseline_files, current_files, checks, elapsed):
    """Retry once only when the failed turn exposed no command or file change."""
    return (
        task.get("worker_model") == AUTO_MODEL and not task.get("capabilities")
        and not task.get("question_only") and not task.get("read_only")
        and bool(task.get("acceptance_commands"))
        and returncode == 0 and first["completed"] and first["thread_id"]
        and not first["side_effects"] and current_files == baseline_files
        and any(row.get("exit_code") != 0 for row in checks)
        and task["budget"].get("max_seconds", 180) - elapsed >= 30
    )


def run(task_dir):
    task_dir = Path(task_dir).resolve(strict=True)
    task = json.loads((task_dir / "task.json").read_text())
    argv = command(task)
    print("Jev lean worker | " + task["worker_model"] + ":" + task["worker_effort"], flush=True)
    started = time.monotonic()
    max_seconds = task["budget"].get("max_seconds", 180)
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, cwd=task["cwd"], timeout=max_seconds)
    except subprocess.TimeoutExpired:
        write_json(task_dir / "exit.json", {"state": "failed", "reason": "quick_timeout", "ended_at": now()})
        return 1
    except KeyboardInterrupt:
        write_json(task_dir / "exit.json", {"state": "cancelled", "ended_at": now()})
        return 130

    first = summarize_events(result.stdout)
    all_stdout = result.stdout
    all_stderr = result.stderr
    answer = first["answer"]
    usage = first["usage"]
    token_ceiling = task["budget"].get("max_input_tokens", 500000)
    under_budget = not (isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int)
                        and usage["input_tokens"] >= token_ceiling)
    completed = first["completed"]
    attempts = 1
    recovery_timeout = False
    baseline_path = task_dir / "baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {"files": []}
    checks = run_checks(task, task_dir) if result.returncode == 0 and completed and not task.get("question_only") else []
    current = fingerprint(task)
    should_retry = (under_budget and not (task_dir / "cancel.request").exists() and recovery_eligible(
        task, first, result.returncode, baseline["files"], current["files"], checks,
        time.monotonic() - started))
    if should_retry:
        attempts = 2
        try:
            second = subprocess.run(resume_command(task, first["thread_id"]),
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                cwd=task["cwd"], timeout=max_seconds - (time.monotonic() - started))
            all_stdout += second.stdout
            all_stderr += second.stderr
            followup = summarize_events(second.stdout)
            result = second
            completed = followup["completed"] and followup["thread_id"] == first["thread_id"]
            answer = followup["answer"]
            if usage and followup["usage"]:
                usage = {key: usage.get(key, 0) + followup["usage"].get(key, 0)
                         for key in usage.keys() | followup["usage"].keys()}
            else:
                usage = usage or followup["usage"]
            under_budget = not (isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int)
                                and usage["input_tokens"] >= token_ceiling)
            checks = run_checks(task, task_dir) if result.returncode == 0 and completed else []
            current = fingerprint(task)
        except subprocess.TimeoutExpired:
            recovery_timeout = True
            completed = False

    events = task_dir / "quick-events.jsonl"
    with events.open("x") as output:
        os.chmod(events, 0o600)
        output.write(all_stdout)
    errors = task_dir / "quick-stderr.log"
    with errors.open("x") as output:
        os.chmod(errors, 0o600)
        output.write(all_stderr)

    response = task_dir / "quick-answer.txt"
    with response.open("x") as output:
        os.chmod(response, 0o600)
        output.write(answer)
    before = {item["path"]: item for item in baseline["files"]}
    after = {item["path"]: item for item in current["files"]}
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    patch = make_patch(task_dir, changed) if changed else None
    unexpected_files = ((task.get("question_only") or task.get("read_only"))
                        and any(Path(task["cwd"]).iterdir()))
    checks_passed = all(row.get("exit_code") == 0 for row in checks)
    state = ("turn_finished_needs_review" if under_budget and result.returncode == 0 and completed and answer
             and checks_passed and not unexpected_files else "failed")
    write_json(task_dir / "exit.json", {"state": state, "provider": "codex", "model": task["worker_model"],
        "effort": task["worker_effort"], "cli_exit_code": result.returncode, "completed": completed,
        "reason": "input_token_budget_exceeded" if not under_budget else None,
        "input_token_budget": token_ceiling,
        "attempts": attempts, "same_thread_recovery": bool(should_retry),
        "recovery_timeout": recovery_timeout,
        "result_file": str(response), "events_file": str(events), "usage": usage,
        "usage_source": "codex_exec_json" if usage else "unknown", "usage_coverage": {"known": usage is not None},
        "checks": checks, "changed_files": changed, "patch": patch,
        "unexpected_workspace_files": bool(unexpected_files), **current, "ended_at": now()})
    print(answer[:500] if state == "turn_finished_needs_review" else "Quick worker failed", flush=True)
    return 0 if state == "turn_finished_needs_review" else 1


if __name__ == "__main__":
    try:
        sys.exit(run(sys.argv[1]))
    except BaseException as error:
        path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
        if path and path.is_dir() and not (path / "exit.json").exists():
            write_json(path / "exit.json", {"state": "failed", "reason": type(error).__name__, "ended_at": now()})
        print("Quick worker error: " + type(error).__name__, file=sys.stderr)
        raise
