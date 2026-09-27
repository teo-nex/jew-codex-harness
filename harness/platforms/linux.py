"""Per-user systemd adapter; no root privileges or credential values."""

import os
import socket
import subprocess
import sys
from pathlib import Path


SERVICE_ID = "jev-codex-harness-router.service"
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
              "MODEL_ROUTER_TARGET": "codex",
              "MODEL_ROUTER_STATE_DIR": str(codex_home / "codex-router"),
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
            if key in ("CODEX_HOME", "CODEX_ROUTER_STATE_DIR"):
                expected = values[key]
                if Path(value).resolve() != Path(expected).resolve():
                    raise ValueError(f"{key} is fixed by the selected Codex profile")
                continue
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
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not config_home.is_absolute():
        config_home = Path.home() / ".config"
    definition = config_home / "systemd/user" / SERVICE_ID
    return {"platform": "linux", "service_id": SERVICE_ID, "marker": MARKER,
            "definition_path": str(definition), "repo": str(repo),
            "command": [sys.executable, str(server)], "env_paths": values,
            "port": int(values["JEV_LISTEN_PORT"]), "state_dir": str(state_dir)}


def _owned(path):
    try:
        st = path.lstat()
        return (path.is_file() and not path.is_symlink() and
                (not hasattr(os, "getuid") or st.st_uid == os.getuid()) and
                path.read_text(encoding="utf-8").startswith("# " + MARKER + "\n"))
    except (OSError, UnicodeError):
        return False


def _private_state_dir(path: Path, create: bool = False):
    if path.is_symlink():
        raise RuntimeError("router state directory must not be a symlink")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise RuntimeError("router state directory is unavailable")
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise RuntimeError("router state directory is not owned by the current user")
    os.chmod(path, 0o700)


def _active():
    result = subprocess.run(["systemctl", "--user", "is-active", "--quiet", SERVICE_ID],
                            capture_output=True, check=False)
    return result.returncode == 0


def service_status(plan: dict) -> dict:
    path = Path(plan["definition_path"])
    installed = path.exists() or path.is_symlink()
    return {"installed": installed, "owned": _owned(path) if installed else False,
            "active": _active(), "port_occupied": _port_occupied(int(plan["port"])),
            "service_id": SERVICE_ID}


def _render(plan):
    # systemd uses UTF-8 unit files and C-style quoting, not JSON escaping.
    # In particular, JSON's \\uXXXX escapes are not decoded by systemd, and
    # percent specifiers must be doubled to preserve literal path characters.
    env_lines = "\n".join("Environment=" + _systemd_quote(k + "=" + v)
                          for k, v in sorted(plan["env_paths"].items()))
    command = " ".join(_systemd_quote(v) for v in plan["command"])
    return (f"# {MARKER}\n[Unit]\nDescription=Jev Codex Harness router\n"
            "After=network-online.target\n\n[Service]\nType=simple\n"
            f"WorkingDirectory={_systemd_quote(plan['repo'])[1:-1]}\nExecStart={command}\n{env_lines}\n"
            "Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n")


def _systemd_quote(value: str) -> str:
    """Quote a value for a systemd unit directive using systemd C escapes."""
    if "\0" in value:
        raise ValueError("systemd unit values cannot contain NUL")
    escaped = []
    for char in value:
        if char == "%":
            escaped.append("%%")
        elif char == "\\":
            escaped.append("\\\\")
        elif char == '"':
            escaped.append('\\"')
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif char == "\t":
            escaped.append("\\t")
        elif ord(char) < 0x20 or ord(char) == 0x7f:
            escaped.extend(f"\\x{byte:02x}" for byte in char.encode("utf-8"))
        else:
            # Keep ordinary Unicode as UTF-8 text; systemd does not decode JSON
            # Unicode escapes such as \\u0416.
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def install_service(plan: dict, dry_run: bool = True) -> dict:
    status = service_status(plan)
    if status["installed"] and not status["owned"]:
        raise FileExistsError("foreign systemd unit at " + plan["definition_path"])
    if status["active"] and not status["owned"]:
        raise RuntimeError("foreign service identity is active")
    if status["port_occupied"] and not status["active"]:
        raise RuntimeError("loopback listener already occupied")
    if status["active"]:
        return {"action": "already_running", "dry_run": dry_run, **status}
    if not dry_run:
        path = Path(plan["definition_path"])
        if path.is_symlink():
            raise RuntimeError("systemd unit path must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)
        _private_state_dir(Path(plan["state_dir"]), create=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(_render(plan), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_ID], check=True)
    return {"action": "install_and_start", "dry_run": dry_run, **status}


def remove_service(plan: dict, dry_run: bool = True) -> dict:
    status = service_status(plan)
    if status["installed"] and not status["owned"]:
        raise FileExistsError("foreign systemd unit at " + plan["definition_path"])
    if status["active"] and not status["owned"]:
        raise RuntimeError("foreign service identity is active")
    if not dry_run and status["installed"]:
        if Path(plan["definition_path"]).is_symlink():
            raise RuntimeError("systemd unit path must not be a symlink")
        if status["active"]:
            subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE_ID], check=True)
        else:
            subprocess.run(["systemctl", "--user", "disable", SERVICE_ID], check=True)
        Path(plan["definition_path"]).unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return {"action": "remove" if status["installed"] else "absent", "dry_run": dry_run, **status}
