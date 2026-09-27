"""Native Codex fallback after proved all-account Gemini quota exhaustion."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import tomllib
import uuid
from codex_runtime import defaults_snapshot, restore_picker_defaults, read_native_evidence

from pi_worker import fingerprint, make_patch, run_checks, usage_summary, write_json, now

ACTIVE_NATIVE_STATES = {"native_starting", "native_selecting_model", "native_running", "native_cancelling"}

def view_for(bridge, binding):
    value = bridge.call("/g2/api/terminal?id=" + binding["session_id"])
    session = value["session"]
    if (session.get("id") != binding["session_id"] or session.get("provider") != "codex" or
        session.get("startedAt") != binding["started_at"] or str(Path(session.get("cwd", "")).resolve()) != binding["cwd"]):
        raise RuntimeError("Native fallback binding changed")
    return value

def send(bridge, binding, action, **kwargs):
    payload = {"sessionId": binding["session_id"], "requestId": str(uuid.uuid4()), "action": action, **kwargs}
    for attempt in range(3):
        try:
            return bridge.call("/g2/api/control", payload)
        except RuntimeError as error:
            # A definite HTTP409 means no delivery, unlike a transport timeout.
            if "HTTP 409" not in str(error) or action != "text" or attempt == 2:
                raise
            view = view_for(bridge, binding)
            if view.get("interaction") or view["session"].get("status") not in ("idle", "unavailable"):
                raise
            time.sleep(0.5)


def menu_matches(menu, pattern):
    return [(i, option) for i, option in enumerate(menu.get("options", [])) if re.search(pattern, option.get("label", ""), re.I)]

def wait_startup_ready(bridge, binding, seconds=30, stable_seconds=1.0):
    deadline = time.monotonic() + seconds
    trust_receipt = None
    handled_trust = set()
    handled_plain_trust = False
    ready_since = None
    while time.monotonic() < deadline:
        view = view_for(bridge, binding)
        menu = view.get("interaction")
        if menu:
            if menu["id"] in handled_trust:
                time.sleep(0.25)
                continue
            if "Release notes" in menu.get("title", ""):
                choices = menu_matches(menu, r"^Skip$")
                if len(choices) != 1:
                    raise RuntimeError("Exact optional update skip missing")
                send(bridge, binding, "choose", menuId=menu["id"], index=choices[0][0])
                handled_trust.add(menu["id"])
                time.sleep(0.25)
                continue
            if "trust" not in menu.get("title", "").lower() and "Do you trust the contents" not in view.get("body", ""):
                raise RuntimeError("Unexpected native startup interaction")
            choices = menu_matches(menu, r"^Yes,? continue(?:\s|$)")
            if len(choices) != 1:
                raise RuntimeError("Exact trust continuation option missing")
            index, _ = choices[0]
            trust_receipt = send(bridge, binding, "choose", menuId=menu["id"], index=index)
            handled_trust.add(menu["id"])
            time.sleep(0.25)
            continue
        screen = bridge.call("/api/screen?sessionId=" + binding["session_id"]).get("text", "")
        if "Hooks need review" in screen:
            raise RuntimeError("Native Codex hooks need review before worker delivery")
        if ("Trust this folder?" in screen and "1. Trust and continue" in screen
                and "2. Quit" in screen):
            if handled_plain_trust:
                time.sleep(0.25)
                continue
            # The terminal is bound to our own isolated cwd. This TUI version
            # shows trust before the bridge exposes a structured interaction.
            trust_receipt = send(bridge, binding, "text", text="1")
            handled_plain_trust = True
            time.sleep(0.25)
            continue
        ready = (view["session"].get("status") in ("idle", "unavailable")
                 and "Ask Codex" in screen and not view.get("interaction"))
        if ready:
            ready_since = ready_since or time.monotonic()
            if time.monotonic() - ready_since >= stable_seconds:
                return view, trust_receipt
        else:
            ready_since = None
        if view["session"].get("status") in ("failed", "done"):
            raise RuntimeError("Native startup failed")
        time.sleep(0.25)
    raise RuntimeError("Native startup readiness timeout")

def wait_menu(bridge, binding, title_pattern, previous_id=None, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        view = view_for(bridge, binding)
        menu = view.get("interaction")
        if menu and menu.get("id") != previous_id and re.search(title_pattern, menu.get("title", ""), re.I):
            return menu
        time.sleep(0.25)
    raise RuntimeError("Fresh native menu unavailable: " + title_pattern)

def wait_composer(bridge, binding, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        view = view_for(bridge, binding)
        if (not view.get("interaction") and view["session"].get("status") in ("idle", "unavailable")
                and sequence(view.get("revision")) is not None):
            screen = bridge.call("/api/screen?sessionId=" + binding["session_id"]).get("text", "")
            if "Ask Codex" in screen or view["session"].get("status") == "idle":
                time.sleep(0.25)
                return view
        time.sleep(0.25)
    raise RuntimeError("Native composer did not become ready")

NATIVE_WORKER_MODELS = {"gpt-6-luna", "gpt-5.6-luna", "gpt-5.6-terra"}


def choose_model_effort(bridge, binding, model="gpt-6-luna", effort="medium"):
    if model not in NATIVE_WORKER_MODELS or effort not in ("low", "medium"):
        raise ValueError("Unsupported native worker model or effort")
    wait_startup_ready(bridge, binding)
    send(bridge, binding, "text", text="/model")
    model_menu = wait_menu(bridge, binding, r"Select Model")
    models = menu_matches(model_menu, r"^" + re.escape(model) + r"(?:\s|$)")
    if len(models) != 1:
        raise RuntimeError(model + " unavailable or ambiguous")
    model_index, model_option = models[0]
    model_receipt = send(bridge, binding, "choose", menuId=model_menu["id"], index=model_index)
    effort_menu = wait_menu(bridge, binding, r"Select Reasoning (?:Level|Effort)", previous_id=model_menu["id"])
    efforts = menu_matches(effort_menu, r"^" + effort.capitalize() + r"(?:\s|$)")
    if len(efforts) != 1:
        raise RuntimeError(effort + " reasoning unavailable or ambiguous")
    effort_index, effort_option = efforts[0]
    effort_receipt = send(bridge, binding, "choose", menuId=effort_menu["id"], index=effort_index)
    wait_composer(bridge, binding)
    return {"model_menu_id": model_menu["id"], "model_index": model_index, "model_label": model_option["label"],
            "model_receipt": model_receipt, "effort_menu_id": effort_menu["id"], "effort_index": effort_index,
            "effort_label": effort_option["label"], "effort_receipt": effort_receipt}


def choose_luna_medium(bridge, binding):
    """Compatibility for the original Luna fallback path."""
    return choose_model_effort(bridge, binding)

def sequence(revision):
    match = re.match(r"^(\d+)-", str(revision or ""))
    return int(match.group(1)) if match else None


def delivery_prompt(packet, task, packet_file):
    """Expose the authorized goal to Jev while keeping large packets bounded."""
    if len(packet.encode("utf-8")) <= 7000:
        return packet
    return ("Goal: " + task["goal"][:2400] + "\nRead the remaining task packet at "
            + str(packet_file) + ". Report actual changes and checks.")

def extract_turn_context(view):
    candidates = [view.get("turn_context"), view.get("turnContext"), view.get("session", {}).get("turn_context"), view.get("session", {}).get("turnContext")]
    return next((item for item in candidates if isinstance(item, dict)), None)

def validate_turn_context(context):
    if not isinstance(context, dict):
        return False, "missing"
    text = json.dumps(context, sort_keys=True).lower()
    ok = ("gpt-6-luna" in text and "medium" in text and any(token in text for token in
          ('"auth_scope": "primary"', '"authscope": "primary"', '"auth_mode": "primary"', '"authmode": "primary"')))
    return ok, "verified" if ok else "mismatch"

def cancel_native(bridge, directory, seconds=10):
    directory = Path(directory)
    binding = json.loads((directory / "native-terminal.json").read_text())
    view_for(bridge, binding)
    receipt = bridge.call("/api/interrupt", {"sessionId": binding["session_id"]})
    write_json(directory / "native-cancel-receipt.json", receipt)
    deadline = time.monotonic() + seconds
    stopped = False
    while time.monotonic() < deadline:
        if view_for(bridge, binding)["session"].get("status") != "working":
            stopped = True
            break
        time.sleep(0.25)
    return {"state": "cancelled" if stopped else "cancel_pending", "native_session_id": binding["session_id"],
            "cancel_confirmed_stopped": stopped, "ended_at": now()}


def unsupported_call_count(rollout_path):
    """Count tool names that Codex explicitly refused in this worker thread."""
    count = 0
    with Path(rollout_path).open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get("payload", {})
            if (row.get("type") == "response_item" and payload.get("type") == "function_call_output"
                    and str(payload.get("output") or "").startswith("unsupported call: ")):
                count += 1
    return count

def monitor_answer(bridge, directory, task, max_seconds):
    directory = Path(directory)
    binding = json.loads((directory / "native-terminal.json").read_text())
    target_model = task.get("worker_model", "gpt-6-luna")
    target_effort = task.get("worker_effort", "medium")
    delivery = json.loads((directory / "native-delivery-request.json").read_text())
    baseline_sequence = sequence(delivery.get("revision_before"))
    deadline = time.monotonic() + max_seconds
    next_tool_guard = time.monotonic() + 3
    while time.monotonic() < deadline:
        if (directory / "cancel.request").exists():
            return cancel_native(bridge, directory)
        view = view_for(bridge, binding)
        if ("browser" in task.get("capabilities", []) and view["session"].get("status") == "working"
                and time.monotonic() >= next_tool_guard):
            next_tool_guard = time.monotonic() + 3
            try:
                live_evidence = read_native_evidence(binding, expected_model=target_model,
                    expected_effort=None if target_model == "jev/auto" else target_effort)
                used = (live_evidence.get("usage") or {}).get("input_tokens")
                ceiling = task.get("budget", {}).get("max_input_tokens", 120000)
                if isinstance(used, int) and used >= ceiling:
                    stopped = cancel_native(bridge, directory, seconds=30)
                    return {**stopped, "state": "blocked" if stopped["cancel_confirmed_stopped"] else "cancel_pending",
                            "reason": "input_token_budget_exceeded", "input_tokens_at_least": used,
                            "input_token_budget": ceiling}
                source = live_evidence.get("source")
                if source and unsupported_call_count(source) >= 3:
                    stopped = cancel_native(bridge, directory, seconds=30)
                    return {**stopped, "state": "blocked" if stopped["cancel_confirmed_stopped"] else "cancel_pending",
                            "reason": "repeated_unsupported_browser_tool",
                            "unsupported_calls_at_least": 3}
            except (OSError, ValueError, sqlite3.Error):
                pass
        current_sequence = sequence(view.get("revision"))
        if (baseline_sequence is not None and current_sequence is not None and current_sequence > baseline_sequence
                and view["session"].get("status") == "idle" and not view.get("interaction")):
            evidence = read_native_evidence(binding, expected_model=target_model,
                                            expected_effort=None if target_model == "jev/auto" else target_effort)
            write_json(directory / "native-runtime-evidence.json", evidence)
            context = evidence.get("context")
            context_ok = evidence.get("verified") is True
            context_status = "verified" if context_ok else evidence.get("reason", "missing")
            if not context_ok:
                return {"state": "blocked", "reason": context_status, "turn_context_verified": False,
                        "native_session_id": binding["session_id"], "ended_at": now()}
            write_json(directory / "native-answer.json", {"answer": view.get("body", ""), "answer_revision": view.get("revision"),
                "turn_context": context, "collected_at": now()})
            checks = run_checks(task, directory)
            current = fingerprint(task)
            if task.get("read_only") and any(Path(task["cwd"]).iterdir()):
                return {"state": "failed", "reason": "read_only_workspace_changed",
                        "native_session_id": binding["session_id"], "ended_at": now()}
            baseline = json.loads((directory / "baseline.json").read_text())
            before = {item["path"]: item for item in baseline["files"]}; after = {item["path"]: item for item in current["files"]}
            changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
            patch = make_patch(directory, changed)
            usage = evidence.get("usage")
            ceiling = task.get("budget", {}).get("max_input_tokens", 120000)
            if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int) and usage["input_tokens"] >= ceiling:
                return {"state": "failed", "reason": "input_token_budget_exceeded",
                        "input_tokens_at_least": usage["input_tokens"], "input_token_budget": ceiling,
                        "native_session_id": binding["session_id"], "turn_context_verified": context_ok,
                        "usage": usage, "ended_at": now()}
            coverage = {"source": "native_cumulative_usage", "known": usage is not None}
            return {"state": "turn_finished_needs_review", "provider": "codex", "model": target_model, "effort": target_effort,
                    "native_session_id": binding["session_id"], "terminal_verified": True, "turn_context_verified": context_ok,
                    "turn_context_status": context_status, "answer_file": str(directory / "native-answer.json"),
                    "answer_revision": view.get("revision"), "checks": checks, **current, "changed_files": changed, "patch": patch,
                    "usage": usage, "usage_source": "native_turn_context" if usage else "unknown", "usage_coverage": coverage, "ended_at": now()}
        if view["session"].get("status") in ("failed", "done"):
            return {"state": "blocked", "reason": "native_terminal_stopped_before_new_answer", "native_session_id": binding["session_id"], "ended_at": now()}
        time.sleep(1)
    receipt = bridge.call("/api/interrupt", {"sessionId": binding["session_id"]})
    write_json(directory / "native-timeout-interrupt.json", receipt)
    return {"state": "blocked", "reason": "native_timeout_interrupt_sent", "native_session_id": binding["session_id"], "turn_context_verified": False, "ended_at": now()}

def start_native_worker(directory, task, bridge, prior_attempt_file=None, context_items=None):
    directory = Path(directory)
    target_model = task.get("worker_model", "gpt-6-luna")
    target_effort = task.get("worker_effort", "medium")
    if target_model not in (NATIVE_WORKER_MODELS | {"jev/auto"}) or target_effort not in ("low", "medium"):
        raise ValueError("Unsupported native worker model or effort")
    if (directory / "native-create-request.json").exists():
        raise RuntimeError("Native create already requested; recovery must inspect binding")
    preface = ("This is a read-only task. Do not change files or inspect unrelated app data."
               if task.get("read_only") else "Inspect current files first and do not repeat completed side effects.")
    packet = ("JEV_WORKER_MANIFEST_V1: " + str(task.get("manifest_path") or directory / "task.json") + "\n"
              "You are a worker. Do not delegate, spawn agents, or call jev-workers. Execute the task directly.\n"
              + preface + "\nGoal: " + task["goal"] +
              "\nWorking directory: " + task["cwd"] + "\nAllowed files: " + json.dumps(task["allowed_files"]) +
              "\nAcceptance commands: " + json.dumps(task["acceptance_commands"]) + "\nMandatory instructions: " + task["mandatory_instructions"] +
              "\nVerify actual files and checks; do not trust prior worker claims.")
    capabilities = set(task.get("capabilities", []))
    if "browser" in capabilities:
        packet += ("\nCanvasTTY Browser Use is provided by MCP jev-browser. "
                   "Call mcp__jev_browser__browser_task once with the goal, exact URL, "
                   "and action read or open_link; for open_link include link_text, "
                   "expected_url and expected_text. Discover only this broker tool if deferred. "
                   "Its output is a compact verified result; report unverified outcomes as blockers. "
                   "Do not use raw canvastty_browser tools, OpenAI Browser plugin, shell, or "
                   "node_repl for this browser task.")
    other_capabilities = capabilities - {"browser"}
    if other_capabilities:
        from quick_worker import capability_skill_paths
        paths = capability_skill_paths(other_capabilities)
        if len(paths) != len(other_capabilities):
            raise RuntimeError("Requested capability skill is not installed")
        packet += "\nExact selected SKILL.md paths: " + json.dumps(paths)
    if task.get("question_only"):
        packet += "\nAnswer this one question briefly. Do not edit files or run tools unless needed to verify an uncertain fact."
    if prior_attempt_file is not None:
        packet += "\nPrevious attempt record: " + str(prior_attempt_file)
    if context_items:
        excerpts = [item["excerpt"] for item in context_items]
        encoded = json.dumps(excerpts, ensure_ascii=False)
        if len(encoded.encode("utf-8")) <= 8000:
            packet += "\nRelevant context: " + encoded
    packet_file = directory / "native-packet.txt"; packet_file.write_text(packet); packet_file.chmod(0o600)
    before = bridge.call("/api/sessions")
    write_json(directory / "native-create-request.json", {"provider": "codex", "at": now(), "before_ids": [s["id"] for s in before["sessions"]]})
    session = bridge.call("/g2/api/create", {"provider": "codex", "profile": "yolo", "cwd": task["cwd"], "title": "Jev Luna " + task["task_id"][:8]})["session"]
    uuid.UUID(session["id"])
    if session.get("provider") != "codex" or session.get("profile") != "yolo" or not session.get("startedAt") or str(Path(session.get("cwd", "")).resolve()) != task["cwd"]:
        raise RuntimeError("Unexpected native Codex identity")
    binding = {"session_id": session["id"], "started_at": session["startedAt"], "provider": "codex", "cwd": task["cwd"], "title": session.get("title")}
    write_json(directory / "native-terminal.json", binding); write_json(directory / "native-state.json", {"state": "native_starting", "at": now()})
    _, trust_receipt = wait_startup_ready(bridge, binding)
    if trust_receipt: write_json(directory / "native-trust-receipt.json", trust_receipt)
    write_json(directory / "native-state.json", {"state": "native_selecting_model", "at": now()})
    if target_model == "jev/auto":
        config = tomllib.loads((Path.home() / ".codex/config.toml").read_text())
        if config.get("model") != target_model:
            raise RuntimeError("Global Codex default is not jev/auto")
        write_json(directory / "native-model-selection.json",
                   {"source": "inherited_global_default", "model": target_model})
    else:
        snapshot = defaults_snapshot()
        write_json(directory / "native-defaults-before.json", snapshot)
        try:
            selected = choose_model_effort(bridge, binding, target_model, target_effort)
            write_json(directory / "native-model-selection.json", selected)
        finally:
            write_json(directory / "native-defaults-restored.json", restore_picker_defaults(
                snapshot, selected_model=target_model, selected_effort=target_effort))
    view = wait_composer(bridge, binding); revision_before = view.get("revision")
    # The global Jev PreToolUse hook sees the submitted user prompt. Include
    # short packets directly so its decision has the actual goal and target.
    # A path-only bootstrap would make even an explicitly requested browser
    # navigation look unauthorized to Jev.
    prompt = delivery_prompt(packet, task, packet_file)
    write_json(directory / "native-delivery-request.json", {"prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "revision_before": revision_before, "at": now()})
    write_json(directory / "native-delivery-receipt.json", send(bridge, binding, "text", text=prompt))
    write_json(directory / "native-state.json", {"state": "native_running", "at": now()})
    return {"state": "native_running", "provider": "codex", "model": target_model, "effort": target_effort,
        "native_session_id": session["id"], "packet_file": str(packet_file), "model_selection_file": str(directory / "native-model-selection.json"),
        "turn_context_verified": False, "started_at": now()}


def start_fallback(directory, task, bridge):
    """Compatibility entry point for the parked Gemini experiment."""
    return start_native_worker(directory, task, bridge, Path(directory) / "gemini-exit.json")
