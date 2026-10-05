"""Explicit opt-in live acceptance; synthetic recovery is a separate evidence arm."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from . import core, live_verify, explain
from .process import run_bounded


def recovery(repo):
    try:
        result = run_bounded([sys.executable, str(repo / "server/recovery_acceptance.py")], timeout=150)
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or report.get("ok") is not (result.returncode == 0):
            raise ValueError("invalid recovery report")
        return report
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"ok": False, "status": "failed", "reason": "controlled recovery runner unavailable"}


def run(repo, home, *, live=False, manual_model=None, gateway_model=None, auth=None, offline_recovery=False):
    report = {"live": {"status": "not_run", "reason": "requires explicit --live"},
              "controlled_recovery": {"status": "not_run", "reason": "requires --offline-recovery"},
              "reasoning_enforcement": "unknown", "backend_identity": "unverified"}
    if offline_recovery:
        report["controlled_recovery"] = recovery(repo)
    if not live:
        return report
    # Validate all inputs before spending the first model request.
    if not manual_model or not gateway_model or auth is None:
        raise core.InstallError("Live acceptance requires --manual-model, --gateway-model and protected --omniroute-auth-file")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", manual_model)
            or manual_model.casefold().split(":", 1)[0] == "jev/auto"
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", gateway_model)):
        raise core.InstallError("Invalid acceptance model IDs")
    from scripts import smoke_reasoning_live as smoke
    key = smoke.load_auth_key(core.protected_file(auth, "OmniRoute auth"))
    profile = Path(home).expanduser().resolve()
    local = core.verify(profile)
    if not all(local.get(field) is True for field in ("files", "client_config", "model_preserved", "service_health")):
        report["live"] = {"ok": False, "status": "blocked", "reason": "owned local verification failed"}
        return report
    config = profile / "jev-harness/ladder-config.json"
    profiles = json.loads(config.read_text()).get("reasoning_profiles", {}) if config.is_file() else {}
    port = int(os.environ.get("JEV_OMNIROUTE_PORT", "20128"))
    decision = live_verify.verify_decision(profile)
    client = live_verify.verify_live(profile) if decision.get("ok") else {"ok": False, "status": "not_run"}
    evidence = explain.latest(profile, client.get("cache_scope")) if client.get("cache_scope") else {"found": False}
    route_accepted = (evidence.get("found") is True and evidence.get("status") == 200 and
                      any(a.get("terminal_type") == "response.completed" for a in evidence.get("attempts", [])))
    tool = live_verify.verify_manual(profile, manual_model) if client.get("ok") else {"ok": False, "status": "not_run"}
    probes = []
    if client.get("ok"):
        for effort in ("low", "high"):
            for tools in (False, True):
                try:
                    outcome = smoke.run_probe(key, port, gateway_model, effort, profiles, tool_loop=tools, timeout=60)
                    probes.append({**outcome, "api_accepted": True, "reasoning_sent": outcome.get("effective_effort"),
                                   "reasoning_enforcement": "unknown"})
                except (OSError, ValueError, RuntimeError):
                    probes.append({"model": gateway_model, "requested_effort": effort,
                                   "probe": "tool_loop" if tools else "json_math", "status": "FAIL",
                                   "api_accepted": "unknown", "reasoning_enforcement": "unknown"})
                    # No wrong-answer retry, and no hidden fallback to another model.
    passed = (decision.get("ok") is True and client.get("ok") is True and route_accepted
              and tool.get("ok") is True and len(probes) == 4 and all(p.get("status") == "PASS" for p in probes))
    report["live"] = {"ok": passed, "status": "passed" if passed else "failed", "decision": decision,
                      "fresh_client_response": client, "route_evidence": evidence, "route_accepted": route_accepted,
                      "fresh_client_tools": tool, "reasoning_probes": probes,
                      "max_direct_gateway_requests": 6}
    return report
