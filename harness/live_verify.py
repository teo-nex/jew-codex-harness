"""Optional, bounded provider smoke check for the installed Jev route."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from . import core
from .process import run_bounded


MARKER = "JEV_LIVE_VERIFY_OK"
MANUAL_MARKER = "JEV_MANUAL_TOOL_OK"
TIMEOUT_SECONDS = 120
PROMPT = (
    "This is a synthetic connectivity check. Do not call tools, MCP servers, "
    "or agents; do not inspect or change files. Reply with exactly this marker "
    f"and nothing else: {MARKER}"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("Jev decision redirect refused")


def verify_decision(codex_home: Path) -> dict[str, Any]:
    """Check the configured Jev backend through the owned loopback router."""
    home = codex_home.expanduser().resolve()
    try:
        manifest = json.loads((home / "jev-harness/manifest.json").read_text(encoding="utf-8"))
        port = manifest["port"]
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("invalid port")
        key_path = home / "codex-router/generic-provider-credentials/jev.key"
        if key_path.is_symlink() or not key_path.is_file():
            raise ValueError("local transport key unavailable")
        key_path = core.protected_file(key_path, "local Jev transport key")
        key = key_path.read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError("local transport key empty")
        payload = {"state": {"marker": "JEV_DECISION_VERIFY"},
                   "questions": {"marker": {"type": "choice",
                      "instructions": "Is state.marker the verification marker?",
                      "criteria": {"present": "state.marker is JEV_DECISION_VERIFY",
                                   "absent": "state.marker is a different value"}}}}
        request = urllib.request.Request(f"http://127.0.0.1:{port}/ask",
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=8) as response:
            result = json.load(response)
            if response.status != 200:
                raise ValueError("decision route returned non-200")
        if not isinstance(result, dict):
            raise ValueError("invalid Jev decision envelope")
        answer = result.get("answers", {}).get("marker", {})
        valid = (result.get("model") == "jev-1.13.0" and answer.get("type") == "choice"
                 and answer.get("choice") == "present")
        return {"ok": valid, "status": "passed" if valid else "failed",
                "reason": "typed Jev decision returned" if valid else "invalid typed Jev decision"}
    except (OSError, ValueError, KeyError, TypeError, core.InstallError):
        return {"ok": False, "status": "failed", "reason": "Jev decision backend unavailable"}


def _parse_events(stdout: str) -> tuple[bool, str]:
    """Return success and a safe reason; never return raw provider output."""
    turn_completed = False
    assistant_text: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            turn_completed = event.get("turn", {}).get("status", "completed") == "completed"
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                assistant_text.append(item["text"])
    if not turn_completed:
        return False, "Codex did not report a completed turn"
    if "\n".join(assistant_text).strip() != MARKER:
        return False, "completed turn did not return the expected marker"
    return True, "live route returned the expected marker"


def _thread_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


def _hook_loaded(stdout: str, codex_home: Path) -> bool:
    thread = _thread_id(stdout)
    if not thread:
        return False
    key = hashlib.sha256(thread.encode()).hexdigest()[:24]
    path = codex_home / "jev-global/sessions" / (key + ".json")
    if path.is_symlink():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(value, dict) and MARKER in str(value.get("active_goal") or "")
    except (OSError, ValueError):
        return False


def _manual_hook_seen(thread: str, model: str, codex_home: Path) -> bool:
    session = hashlib.sha256(thread.encode()).hexdigest()[:24]
    state = Path(os.environ.get("JEV_GLOBAL_STATE_DIR", str(codex_home / "jev-global")))
    for path in (state / "usage.jsonl", state / "usage.jsonl.1"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if (row.get("kind") == "pre_tool_seen" and row.get("session") == session
                            and row.get("guard_mode") == "manual" and row.get("model") == model):
                        return True
        except OSError:
            continue
    return False


def verify_manual(codex_home: Path, model: str, executable: str | None = None) -> dict[str, Any]:
    """Prove a selected manual model can make one bounded tool edit under the hook."""
    if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", model)
            or model.casefold().split(":", 1)[0] == "jev/auto"):
        return {"ok": False, "status": "failed", "reason": "a manually selected model is required"}
    codex = executable or shutil.which("codex")
    if not codex:
        return {"ok": False, "status": "failed", "reason": "codex executable not found"}
    try:
        with tempfile.TemporaryDirectory(prefix="jev-manual-verify-") as cwd:
            env = {**os.environ, "CODEX_HOME": str(codex_home.expanduser().resolve())}
            command = [codex, "exec", "-m", model.strip(), "--ephemeral", "--json",
                       "--sandbox", "workspace-write", "--skip-git-repo-check",
                       "-c", "features.multi_agent=false", "-c", "agents.enabled=false",
                       "-c", 'approval_policy="never"',
                       "-C", cwd,
                       f"Create proof.txt containing exactly {MANUAL_MARKER} followed by one newline. "
                       "Use one local tool. Do not inspect other files, agents, or network resources."]
            completed = run_bounded(command, cwd=cwd, env=env, timeout=TIMEOUT_SECONDS)
            if completed.returncode:
                return {"ok": False, "status": "failed", "reason": "manual Codex turn failed"}
            proof = Path(cwd) / "proof.txt"
            if not proof.is_file() or proof.read_bytes() != (MANUAL_MARKER + "\n").encode():
                return {"ok": False, "status": "failed", "reason": "manual model did not write the expected fixture"}
            thread = _thread_id(completed.stdout)
            if not thread or not _manual_hook_seen(thread, model.strip(), codex_home.expanduser().resolve()):
                return {"ok": False, "status": "failed", "reason": "manual tool hook was not observed"}
            return {"ok": True, "status": "passed", "reason": "manual model tool use passed under Jev hook"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "failed", "reason": "manual Codex turn timed out"}
    except OSError:
        return {"ok": False, "status": "failed", "reason": "could not run manual Codex turn"}


def verify_cost_telemetry(codex_home: Path) -> dict[str, Any]:
    """Confirm a Jev-routed call has usage that the public-rate report can price."""
    from integrations.jev import report_api_cost

    log = codex_home.expanduser().resolve() / "codex-router/jev-router-live.jsonl"
    price_snapshot = (report_api_cost.live_gonka_prices() if report_api_cost.gonka_used(log, 1) else
                      {"prices": {}, "mode": "not_needed"})
    result = report_api_cost.report(1, routing_log=log,
                                    gonka_prices=price_snapshot["prices"])
    priced = result["priced_attempts"]
    totals = result["totals"]
    return {"ok": priced > 0, "status": "passed" if priced > 0 else "unknown",
            "routed_calls": result["routed_calls"], "priced_attempts": priced,
            "usage_unknown": totals["usage_unknown"], "unpriced": totals["unpriced"],
            "gonka_pricing": price_snapshot["mode"],
            "reason": ("observed model usage is priceable" if priced > 0 else
                       "no priceable Jev-routed attempt in the last hour")}


def verify_live(codex_home: Path, executable: str | None = None) -> dict[str, Any]:
    """Make one synthetic read-only call through jev/auto, with a 120s cap."""
    codex = executable or shutil.which("codex")
    if not codex:
        return {"ok": False, "status": "failed", "reason": "codex executable not found"}

    try:
        with tempfile.TemporaryDirectory(prefix="jev-live-verify-") as cwd:
            env = os.environ.copy()
            env["CODEX_HOME"] = str(codex_home.expanduser().resolve())
            # The prompt forbids tool use; disable Codex's multi-agent feature as
            # a second guard so the smoke check cannot create nested workers.
            command = [
                codex,
                "exec",
                "-m",
                "jev/auto",
                "--ephemeral",
                "--json",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-c",
                "features.multi_agent=false",
                "-c",
                "features.multi_agent_v2=false",
                "-c",
                "agents.enabled=false",
                "-c",
                "mcp_servers.jev-workers.enabled=false",
                "-C",
                cwd,
                PROMPT,
            ]
            try:
                completed = run_bounded(
                    command,
                    cwd=cwd,
                    env=env,
                    timeout=TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "status": "failed", "reason": "live check timed out after 120 seconds"}
            if completed.returncode != 0:
                return {"ok": False, "status": "failed", "reason": "codex exec returned a nonzero exit code"}
            ok, reason = _parse_events(completed.stdout)
            if ok and not _hook_loaded(completed.stdout, codex_home.expanduser().resolve()):
                return {"ok": False, "status": "failed",
                        "reason": "Codex response completed but Jev prompt hook did not load"}
            thread = _thread_id(completed.stdout)
            return {"ok": ok, "status": "passed" if ok else "failed", "reason": reason,
                    "cache_scope": hashlib.sha256(("prompt:" + thread).encode()).hexdigest()[:16] if thread else None}
    except OSError:
        return {"ok": False, "status": "failed", "reason": "could not start codex exec"}
