"""Per-user launchd adapter. The generated job never contains credential values."""

import os
import plistlib
import socket
import stat
import subprocess
import sys
from pathlib import Path


SERVICE_ID = "ai.jev-codex-harness.router"
MARKER = "jev-codex-harness:v1"
PATH_ENV = ("CODEX_HOME", "CODEX_ROUTER_STATE_DIR", "JEV_LADDER_CONFIG",
            "JEV_LADDER_STATE", "JEV_OMNIROUTE_AUTH_FILE", "TYPESAFE_API_KEY_FILE",
            "JEV_DECISION_KEY_FILE")


def _port_occupied(port):
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _environment(codex_home, state_dir, env):
    provider = env.get("JEV_DECISION_PROVIDER", "typesafe")
    if provider not in ("typesafe", "openrouter"):
        raise ValueError("unsupported Jev decision provider")
    if (provider == "typesafe" and "JEV_DECISION_KEY_FILE" in env
            or provider == "openrouter" and "TYPESAFE_API_KEY_FILE" in env):
        raise ValueError("decision key path does not match provider")
    mode = env.get("JEV_LADDER_MODE", "active")
    if mode not in ("active", "native"):
        raise ValueError("unsupported ladder mode")
    if mode == "native" and any(key in env for key in ("JEV_LADDER_CONFIG", "JEV_LADDER_STATE", "JEV_OMNIROUTE_AUTH_FILE")):
        raise ValueError("native mode must not configure an external provider ladder")
    values = {"CODEX_HOME": str(codex_home),
              "CODEX_ROUTER_STATE_DIR": str(codex_home / "codex-router"),
              "JEV_LISTEN_PORT": str(env.get("JEV_LISTEN_PORT", "4321")),
              "JEV_LADDER_MODE": mode, "JEV_DECISION_PROVIDER": provider}
    if mode == "active":
        values.update(JEV_LADDER_CONFIG=str(state_dir / "ladder-config.json"),
                      JEV_LADDER_STATE=str(state_dir / "ladder-state.json"))
    port = int(values["JEV_LISTEN_PORT"])
    if not 1024 <= port <= 65535:
        raise ValueError("JEV_LISTEN_PORT must be an unprivileged TCP port")
    for key in PATH_ENV:
        if key in env:
            value = str(env[key])
            if not Path(value).is_absolute():
                raise ValueError(f"{key} must be an absolute path")
            values[key] = value
    forbidden = set(env) - set(PATH_ENV) - {"JEV_LISTEN_PORT", "JEV_LADDER_MODE", "JEV_DECISION_PROVIDER"}
    if forbidden:
        raise ValueError("unsupported service environment keys: " + ", ".join(sorted(forbidden)))
    return values


def plan_service(repo: Path, codex_home: Path, state_dir: Path, env: dict[str, str]) -> dict:
    repo, codex_home, state_dir = (Path(p).expanduser().resolve() for p in
                                    (repo, codex_home, state_dir))
    values = _environment(codex_home, state_dir, env)
    server = repo / "server/jev_server.py"
    if not server.is_file():
        raise FileNotFoundError(server)
    definition = Path.home() / "Library/LaunchAgents" / f"{SERVICE_ID}.plist"
    return {"platform": "macos", "service_id": SERVICE_ID, "marker": MARKER,
            "definition_path": str(definition), "repo": str(repo),
            "command": [sys.executable, str(server)], "env_paths": values,
            "port": int(values["JEV_LISTEN_PORT"]), "state_dir": str(state_dir)}


def _owned(path):
    if not path.exists() or path.is_symlink():
        return False
    try:
        data = plistlib.loads(path.read_bytes())
        return data.get("JevHarnessOwner") == MARKER and data.get("Label") == SERVICE_ID
    except (OSError, ValueError, TypeError):
        return False


def _active(plan):
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{SERVICE_ID}"],
                            capture_output=True, text=True, check=False)
    return result.returncode == 0


def service_status(plan: dict) -> dict:
    path = Path(plan["definition_path"])
    installed = path.exists()
    owner = _owned(path) if installed else False
    active = _active(plan)
    return {"installed": installed, "owned": owner, "active": active,
            "port_occupied": _port_occupied(int(plan["port"])),
            "service_id": SERVICE_ID}


def install_service(plan: dict, dry_run: bool = True) -> dict:
    status = service_status(plan)
    if status["installed"] and not status["owned"]:
        raise FileExistsError("foreign launchd job at " + plan["definition_path"])
    if status["active"] and not status["owned"]:
        raise RuntimeError("foreign service identity already active")
    if status["port_occupied"] and not status["active"]:
        raise RuntimeError("loopback listener already occupied")
    if status["active"]:
        return {"action": "already_running", "dry_run": dry_run, **status}
    state_dir = Path(plan["state_dir"])
    if state_dir.is_symlink() or (state_dir.exists() and
            (not state_dir.is_dir() or stat.S_IMODE(state_dir.stat().st_mode) & 0o077)):
        raise PermissionError("Jev state directory must be private and must not be a symlink")
    path = Path(plan["definition_path"])
    payload = {"Label": SERVICE_ID, "JevHarnessOwner": MARKER,
               "ProgramArguments": plan["command"], "WorkingDirectory": plan["repo"],
               "EnvironmentVariables": plan["env_paths"], "RunAtLoad": True,
               "KeepAlive": True, "Umask": 0o077,
               "StandardOutPath": str(state_dir / "router.out.log"),
               "StandardErrorPath": str(state_dir / "router.err.log")}
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(plistlib.dumps(payload))
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    return {"action": "install_and_start", "dry_run": dry_run, **status}


def remove_service(plan: dict, dry_run: bool = True) -> dict:
    status = service_status(plan)
    if status["installed"] and not status["owned"]:
        raise FileExistsError("foreign launchd job at " + plan["definition_path"])
    if status["active"] and not status["owned"]:
        raise RuntimeError("foreign service identity already active")
    if not dry_run and status["installed"]:
        if status["active"]:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_ID}"], check=True)
        Path(plan["definition_path"]).unlink()
    return {"action": "remove" if status["installed"] else "absent", "dry_run": dry_run, **status}
