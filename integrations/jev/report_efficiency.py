#!/usr/bin/env python3
"""Aggregate Jev routing and worker usage without exposing prompts or keys."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path


def codex_home():
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


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


def default_paths():
    home = codex_home()
    return (home / "codex-router/jev-router-live.jsonl", home / "jev-global/usage.jsonl",
            worker_state_dir())


ROUTING_LOG, HOOK_LOG, WORKERS = default_paths()  # Compatibility for existing imports.


def read_jsonl(path):
    for source in (path.with_name(path.name + ".1"), path):
        try:
            lines = source.open(encoding="utf-8")
        except OSError:
            continue
        with lines:
            for line in lines:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value


def stage_for(model):
    model = str(model or "")
    if model.startswith("codex/gpt-"):
        return "plus"
    if model.startswith("antigravity/claude-opus-"):
        return "opus"
    if model.startswith("antigravity/"):
        return "gemini"
    if model.startswith("wally/"):
        return "wally"
    if "glm-5.3-flash" in model:
        return "glm"
    if "deepseek-v4-flash-0731" in model:
        return "deepseek"
    if model == "gpt-6-luna":
        return "main"
    return "other"


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * fraction))]


def load_private_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def report(hours=24, *, routing_log=None, hook_log=None, workers=None, now=None):
    if not 0 < hours <= 24 * 90:
        raise ValueError("hours must be between 0 and 2160")
    now = now or datetime.now(timezone.utc)
    cutoff_utc = now - timedelta(hours=hours)
    cutoff_local = cutoff_utc.astimezone().replace(tzinfo=None)
    stages = defaultdict(Counter)
    jev = Counter()
    hook = Counter()
    skills = Counter()
    ladder_calls = 0
    completed_calls = 0
    native_reference_calls = 0
    latencies = []
    routing_default, hook_default, worker_default = default_paths()
    routing_log = Path(routing_log) if routing_log is not None else routing_default
    hook_log = Path(hook_log) if hook_log is not None else hook_default
    workers = Path(workers) if workers is not None else worker_default
    for row in read_jsonl(Path(routing_log)):
        try:
            at = datetime.fromisoformat(str(row.get("at")))
        except ValueError:
            continue
        if at.replace(tzinfo=None) < cutoff_local:
            continue
        if "ladder_stage" not in row:
            native_reference_calls += 1
            continue
        ladder_calls += 1
        if row.get("status") == 200 and row.get("ladder_stage") != "failed":
            completed_calls += 1
        duration = row.get("total_ms")
        if isinstance(duration, (int, float)) and duration >= 0:
            latencies.append(duration)
        usage = row.get("jev_usage")
        if isinstance(usage, dict):
            jev["known_calls"] += 1
            for key in ("input_tokens", "output_tokens"):
                amount = usage.get(key)
                if isinstance(amount, int) and amount >= 0:
                    jev[key] += amount
        elif row.get("decision_source") == "lease":
            jev["reused_without_call"] += 1
        else:
            jev["unknown_calls"] += 1
        for attempt in row.get("attempts") or []:
            if not isinstance(attempt, dict):
                continue
            stage = stage_for(attempt.get("model"))
            target = stages[stage]
            target["attempts"] += 1
            if attempt.get("http_status") == 200 and attempt.get("terminal_type") == "response.completed":
                target["completed"] += 1
            else:
                target["refused_or_incomplete"] += 1
            status = attempt.get("http_status")
            if status in (400, 401, 403, 429, 500, 502, 503, 504):
                target["http_" + str(status)] += 1
            counts = attempt.get("usage")
            if not isinstance(counts, dict):
                target["usage_unknown"] += 1
                continue
            target["usage_known"] += 1
            for key in ("input_tokens", "cached_input_tokens", "output_tokens",
                        "cache_write_input_tokens", "reasoning_tokens"):
                amount = counts.get(key)
                if isinstance(amount, int) and amount >= 0:
                    target[key] += amount
    for row in read_jsonl(Path(hook_log)):
        at = row.get("at")
        if not isinstance(at, (int, float)) or datetime.fromtimestamp(at, timezone.utc) < cutoff_utc:
            continue
        if row.get("kind") == "tool_review":
            hook["reviews"] += 1
            hook[str(row.get("decision") or "unavailable")] += 1
            if isinstance(row.get("allowed"), bool):
                hook["actually_allowed" if row["allowed"] else "actually_denied"] += 1
            else:
                hook["outcome_unknown"] += 1
            for key in ("input_tokens", "output_tokens"):
                amount = row.get(key)
                if isinstance(amount, int) and amount >= 0:
                    hook[key] += amount
        if row.get("kind") == "skill_review":
            skills["reviews"] += 1
            if row.get("suggested") is True:
                skills["suggested"] += 1
            for key in ("input_tokens", "output_tokens"):
                amount = row.get(key)
                if isinstance(amount, int) and amount >= 0:
                    skills[key] += amount
    worker_counts = Counter()
    durations = []
    reviewed_usage = Counter()
    accepted_durations = []
    task_results = []
    if Path(workers).is_dir():
        for directory in Path(workers).iterdir():
            if not directory.is_dir():
                continue
            task = load_private_json(directory / "task.json")
            exit_data = load_private_json(directory / "exit.json")
            if task.get("worker_model") != "jev/auto" or not exit_data:
                continue
            try:
                ended = datetime.fromisoformat(exit_data["ended_at"])
            except (KeyError, ValueError):
                continue
            if ended.astimezone(timezone.utc) < cutoff_utc:
                continue
            group = "smoke" if "-smoke-" in str(task.get("request_id", "")) else "other"
            worker_counts[group + "_" + str(exit_data.get("state") or "unknown")] += 1
            worker_counts["all"] += 1
            acceptance = load_private_json(directory / "acceptance.json")
            if acceptance.get("accepted") is True or acceptance.get("functional_pass") is True:
                worker_counts["functional_pass"] += 1
            if acceptance.get("reason") == "input_token_budget_exceeded":
                worker_counts["budget_exceeded"] += 1
            if acceptance.get("accepted") is True:
                worker_counts["accepted"] += 1
                usage = exit_data.get("usage")
                if isinstance(usage, dict):
                    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                        amount = usage.get(key)
                        if isinstance(amount, int) and amount >= 0:
                            reviewed_usage[key] += amount
            elif acceptance.get("accepted") is False:
                worker_counts["rejected"] += 1
            else:
                worker_counts["unreviewed"] += 1
            if acceptance.get("accepted") in (True, False):
                usage_row = exit_data.get("usage") if isinstance(exit_data.get("usage"), dict) else {}
                task_results.append({"task_hash": hashlib.sha256(directory.name.encode()).hexdigest()[:12],
                                     "accepted": acceptance["accepted"],
                                     "backend": "native" if (directory / "native-terminal.json").exists() else "lean",
                                     "input_tokens": usage_row.get("input_tokens")})
            if exit_data.get("same_thread_recovery"):
                worker_counts["same_thread_recovery"] += 1
            journal = load_private_json(directory / "journal.json")
            try:
                started = datetime.fromisoformat(journal["started_at"])
                seconds = (ended - started).total_seconds()
                durations.append(seconds)
                if acceptance.get("accepted") is True:
                    accepted_durations.append(seconds)
            except (KeyError, ValueError):
                pass
    stage_data = {name: dict(counts) for name, counts in sorted(stages.items())}
    for value in stage_data.values():
        inp = value.get("input_tokens", 0)
        value["cache_read_percent"] = round(100 * value.get("cached_input_tokens", 0) / inp, 1) if inp else None
    return {"window_hours": hours, "ladder_calls": ladder_calls,
            "completed_calls": completed_calls, "native_reference_calls": native_reference_calls,
            "stages": stage_data, "jev": dict(jev), "tool_guard": dict(hook),
            "skill_selection": dict(skills),
            "workers": dict(worker_counts), "latency_ms_p50": percentile(latencies, .5),
            "latency_ms_p95": percentile(latencies, .95),
            "worker_seconds_p50": percentile(durations, .5),
            "accepted_worker_input_tokens_per_task": (round(reviewed_usage["input_tokens"] / worker_counts["accepted"])
                if worker_counts["accepted"] and reviewed_usage["input_tokens"] else None),
            "accepted_worker_cached_input_percent": (round(100 * reviewed_usage["cached_input_tokens"]
                / reviewed_usage["input_tokens"], 1) if reviewed_usage["input_tokens"] else None),
            "accepted_worker_seconds_p50": percentile(accepted_durations, .5),
            "reviewed_tasks": task_results[-50:],
            "savings_estimate": None,
            "interpretation": "Observed routing and usage only; no matched-task baseline or causal savings proof."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    value = report(args.hours)
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.output.write_text(data)
        args.output.chmod(0o600)
        print(args.output)
    else:
        print(data, end="")


if __name__ == "__main__":
    main()
