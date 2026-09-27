#!/usr/bin/env python3
"""Create and control native Codex terminals through CanvasTTY's loopback API. No GUI."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import sys
import uuid

from control import Bridge
from runner import save_new


def seq(view):
    match = re.match(r"^(\d+)-", view.get("revision", ""))
    return int(match.group(1)) if match else None


def validate_view(view, pinned):
    session = view["session"]
    if (session["id"] != pinned["session_id"] or session["provider"] != "codex"
            or str(Path(session["cwd"]).resolve()) != pinned["cwd"]
            or pinned.get("started_at") is None or session.get("startedAt") != pinned["started_at"]):
        raise RuntimeError("Native identity/provider/cwd/start generation mismatch; rebind after restart")
    return session


def latest_request(worker):
    records = sorted(worker.glob("native-turn-*.request.json"), key=lambda p: p.stat().st_mtime_ns)
    if not records:
        return None, None
    path = records[-1]
    return path, json.loads(path.read_text())


def result_path(request_path):
    return Path(str(request_path).replace(".request.json", ".result.json"))


def summarize(worker, view, pinned):
    session = validate_view(view, pinned)
    state = session["status"]
    request_path, request = latest_request(worker)
    if request_path:
        if result_path(request_path).exists():
            state = "turn_finished_needs_review" if state == "idle" else state
        elif (state == "idle" and seq(view) is not None
              and seq(view) > request["answer_sequence_before"]):
            state = "answer_ready"
        elif state == "idle":
            state = "awaiting_delivery_or_result"
    return {
        "session_id": session["id"], "title": session["title"], "provider": session["provider"],
        "cwd": session["cwd"], "terminal_status": session["status"], "state": state,
        "answer_revision": view.get("revision"), "answer": view.get("body", "")[:2200],
        "interaction": view.get("interaction"),
        "request_file": str(request_path) if request_path else None,
    }


def ensure_composer(view, *, bootstrap=False):
    allowed = ("idle", "unavailable") if bootstrap else ("idle",)
    if view["session"]["status"] not in allowed or view.get("interaction"):
        raise RuntimeError("Terminal is busy, failed, or showing an interaction; inspect it first")
    if "Do you trust the contents" in view.get("body", ""):
        raise RuntimeError("Review trust for the exact launch folder before entering a task")
    if seq(view) is None:
        raise RuntimeError("No authoritative answer sequence; inspect the API screen")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create", help="Create a native provider=codex session via the supported API")
    create.add_argument("worker", type=Path)
    bind = sub.add_parser("bind", help="Bind an explicitly identified native Codex terminal")
    bind.add_argument("worker", type=Path)
    bind.add_argument("session_id")
    bind.add_argument("--cwd", type=Path, required=True)
    for command in ("status", "screen", "permissions", "yolo", "collect", "interrupt"):
        sub.add_parser(command).add_argument("worker", type=Path)
    send = sub.add_parser("send")
    send.add_argument("worker", type=Path)
    send.add_argument("prompt", type=Path)
    send.add_argument("--preflight", action="store_true",
                      help="Allow initial unavailable hook status after inspecting the native composer")
    args = parser.parse_args()
    worker = args.worker.resolve(strict=True)
    bridge = Bridge()
    if args.action == "create":
        if (worker / "native-binding.json").exists():
            raise RuntimeError("Worker already has a native terminal binding")
        before = bridge.call("/api/sessions")
        save_new(worker / "native-create-request.json", {
            "provider": "codex", "before_ids": [s["id"] for s in before["sessions"]],
            "requested_at": datetime.now(timezone.utc).isoformat(),
        })
        # This endpoint is non-idempotent: an ambiguous response requires inspection, not retry.
        session = bridge.call("/g2/api/create", {"provider": "codex", "profile": "yolo", "cwd": str(worker), "title": "Codex Orchestrated"})["session"]
        if session["provider"] != "codex":
            raise RuntimeError("CanvasTTY returned an unexpected provider")
        pinned = {"session_id": session["id"], "cwd": str(Path(session["cwd"]).resolve()),
                  "worker": str(worker), "mode": "native-codex-api", "started_at": session["startedAt"]}
        save_new(worker / "native-binding.json", pinned)
        return {**pinned, "note": "Native Electron API; explicit YOLO profile; cwd is the worker directory"}
    if args.action == "bind":
        uuid.UUID(args.session_id)
        pinned = {"session_id": args.session_id, "cwd": str(args.cwd.resolve(strict=True)),
                  "worker": str(worker), "mode": "native-codex-bound"}
        view = bridge.call("/g2/api/terminal?id=" + args.session_id)
        pinned["started_at"] = view["session"].get("startedAt")
        session = validate_view(view, pinned)
        if session["profile"] not in ("normal", "yolo") or session["status"] in ("failed", "done"):
            raise RuntimeError("Bind a live normal/yolo native Codex")
        save_new(worker / "native-binding.json", pinned)
        return pinned
    pinned = json.loads((worker / "native-binding.json").read_text())
    uuid.UUID(pinned["session_id"])
    if pinned["worker"] != str(worker):
        raise RuntimeError("Binding directory mismatch")
    sid = pinned["session_id"]
    if args.action not in ("status", "screen"):
        lock = (worker / ".native-control.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    view = bridge.call("/g2/api/terminal?id=" + sid)
    validate_view(view, pinned)
    summary = summarize(worker, view, pinned)
    if args.action == "status":
        return summary
    if args.action == "screen":
        return bridge.call("/api/screen?sessionId=" + sid)
    if args.action == "interrupt":
        # Ctrl-C goes only to this bound PTY. Keep all windows and records.
        receipt = bridge.call("/api/interrupt", {"sessionId": sid})
        save_new(worker / ("native-interrupt-" + uuid.uuid4().hex + ".json"), receipt)
        return {**receipt, "state": "interrupt_sent_not_yet_confirmed"}
    if args.action == "collect":
        if summary["state"] != "answer_ready":
            raise RuntimeError("No new completed answer; idle alone is insufficient")
        request_path, request = latest_request(worker)
        output = result_path(request_path)
        save_new(output, {"request_id": request["request_id"], "session_id": sid,
                          "answer_revision": view["revision"], "answer": view["body"],
                          "collected_at": datetime.now(timezone.utc).isoformat()})
        return {"state": "turn_finished_needs_review", "result_file": str(output), "answer": view["body"]}
    if args.action == "yolo":
        menu = view.get("interaction")
        if not menu or "Model Permissions" not in menu["title"]:
            raise RuntimeError("Open /permissions and refresh before selecting Full Access")
        indices = [i for i, option in enumerate(menu["options"])
                   if re.match(r"^Full Access(?:\s|\(|$)", option["label"])]
        if len(indices) != 1:
            raise RuntimeError("Full Access option is missing or ambiguous")
        receipt = bridge.call("/g2/api/control", {"sessionId": sid, "requestId": str(uuid.uuid4()),
            "action": "choose", "menuId": menu["id"], "index": indices[0]})
        save_new(worker / ("native-yolo-" + uuid.uuid4().hex + ".json"), receipt)
        return {**receipt, "next": "Verify YOLO / Full Access in the API screen and danger-full-access / never in the exact turn_context"}
    if args.action == "permissions":
        ensure_composer(view, bootstrap=True)
        return bridge.call("/g2/api/control", {"sessionId": sid, "requestId": str(uuid.uuid4()),
                           "action": "text", "text": "/permissions"})
    ensure_composer(view, bootstrap=args.preflight)
    request_path, _ = latest_request(worker)
    if request_path and not result_path(request_path).exists():
        raise RuntimeError("Previous native turn is uncollected; never send over pending delivery")
    prompt = args.prompt.resolve(strict=True)
    if not prompt.is_relative_to(worker):
        raise RuntimeError("Prompt must belong to this worker artifact directory")
    message = prompt.read_text()
    if not message.strip() or len(message) > 8000:
        raise RuntimeError("Prompt must contain 1..8000 characters; use a short file bootstrap if needed")
    request_id = str(uuid.uuid4())
    prefix = worker / ("native-turn-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + uuid.uuid4().hex[:6])
    save_new(Path(str(prefix) + ".request.json"), {
        "request_id": request_id, "session_id": sid, "prompt": str(prompt),
        "answer_sequence_before": seq(view), "submitted_at": datetime.now(timezone.utc).isoformat(),
    })
    receipt = bridge.call("/g2/api/control", {"sessionId": sid, "requestId": request_id,
                                           "action": "text", "text": message})
    save_new(Path(str(prefix) + ".receipt.json"), receipt)
    return receipt


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
