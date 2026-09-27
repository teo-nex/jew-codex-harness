"""Conservative, per-user installer for a fresh Codex profile.

The existing profile is deliberately outside this installer's ownership. A
later migration tool can adopt it after checking live sessions and provider
state; a fresh install must never silently become a migration.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

from .agent_config import managed_section, merge_agents, remove_agents, section
from .browser_config import DEFAULT_HELPER, merge_browser_config


class InstallError(RuntimeError):
    pass


def platform_module(name: str | None = None):
    name = name or {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(sys.platform)
    if name not in ("macos", "linux", "windows"):
        raise InstallError("Unsupported OS; requires macOS, Linux or Windows")
    return __import__(f"harness.platforms.{name}", fromlist=["plan_service"])


def protected_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise InstallError(f"{label} must be a regular file")
    if os.name == "nt":
        # st_mode does not describe Windows ACLs. Reject read access granted to
        # any principal other than this user, SYSTEM or Administrators.
        script = (
            "$ErrorActionPreference='Stop'; "
            "try { $acl=[System.IO.File]::GetAccessControl($env:JEV_ACL_CHECK_PATH); "
            "$self = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
            "$allowed = @($self, 'S-1-5-18', 'S-1-5-32-544'); "
            "$rules=$acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]); "
            "foreach ($ace in $rules) { "
            "if ($ace.AccessControlType -ne 'Allow') { continue }; "
            "if ($ace.IdentityReference.Value -notin $allowed) { exit 3 } }; exit 0 } "
            "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 2 }"
        )
        env = {**os.environ, "JEV_ACL_CHECK_PATH": str(path)}
        checked = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 env=env, capture_output=True, check=False)
        if checked.returncode:
            raise InstallError(f"{label} has unverified or shared Windows ACL")
    elif path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise InstallError(f"{label} must have mode 0600 or stricter")
    return path


def ladder_file(path: Path) -> Path:
    path = protected_file(path, "ladder config")
    try:
        from server.provider_ladder import validate_config
        validate_config(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"Invalid provider ladder config: {exc}") from exc
    return path


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _hook_entries(repo: Path) -> dict:
    script = repo / "integrations/jev/global/codex_hook.py"
    import shlex
    command = shlex.join([sys.executable, str(script)]) if os.name != "nt" else subprocess.list2cmdline([sys.executable, str(script)])
    return {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command + " prompt", "timeout": 3}]}],
        "PreToolUse": [{"hooks": [{"type": "command", "command": command + " pre_tool", "timeout": 8}]}],
        "PostToolUse": [{"matcher": "^(Bash|apply_patch|mcp__canvastty_browser__(browser_observe|browser_read_page))$",
                         "hooks": [{"type": "command", "command": command + " post_tool", "timeout": 3}]}],
        "PreCompact": [{"matcher": "^(auto|manual)$",
                        "hooks": [{"type": "command", "command": command + " pre_compact", "timeout": 8}]}],
        "SessionStart": [{"matcher": "^compact$",
                          "hooks": [{"type": "command", "command": command + " session_start",
                                     "timeout": 3, "additionalContextLimit": 1000}]}],
    }


def merge_hooks(existing: dict, repo: Path) -> dict:
    if not isinstance(existing, dict) or not isinstance(existing.get("hooks", {}), dict):
        raise InstallError("hooks.json must contain an object with a hooks object")
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault("hooks", {})
    for event, entries in _hook_entries(repo).items():
        rows = hooks.setdefault(event, [])
        if not isinstance(rows, list):
            raise InstallError(f"hooks.json {event} must be an array")
        for entry in entries:
            command = entry["hooks"][0]["command"]
            if not any(command == hook.get("command") for row in rows if isinstance(row, dict)
                       for hook in row.get("hooks", []) if isinstance(hook, dict)):
                rows.append(entry)
    return result


def merge_config(existing: str, repo: Path) -> str:
    try:
        config = tomllib.loads(existing)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError("Existing config.toml is invalid TOML") from exc
    current = config.get("mcp_servers", {}).get("jev-workers")
    if current is not None:
        expected = {"command": sys.executable, "args": [str(repo / "integrations/workers/mcp.py")]}
        if current != expected:
            raise InstallError("Existing jev-workers MCP differs; migration requires review")
        return existing
    section = "\n[mcp_servers.jev-workers]\ncommand = " + _toml_string(sys.executable)
    section += "\nargs = [" + _toml_string(str(repo / "integrations/workers/mcp.py")) + "]\n"
    return existing.rstrip("\n") + "\n" + section


def ladder_mode(ladder: Path | None, omni: Path | None) -> str:
    if ladder is None and omni is None:
        return "native"
    if ladder is None or omni is None:
        raise InstallError("Supply both ladder config and OmniRoute auth, or neither for native-only routing")
    return "active"


def _service_env(mode: str, omni: Path | None, decision_key: Path, port: int,
                 jev_provider: str) -> dict[str, str]:
    values = {"JEV_LISTEN_PORT": str(port), "JEV_LADDER_MODE": mode,
              "JEV_DECISION_PROVIDER": jev_provider}
    values["TYPESAFE_API_KEY_FILE" if jev_provider == "typesafe" else "JEV_DECISION_KEY_FILE"] = str(decision_key)
    if mode == "active":
        values["JEV_OMNIROUTE_AUTH_FILE"] = str(omni)
    return values


def _input_paths(ctx: dict) -> list[str | None]:
    return [str(ctx[key]) if ctx[key] is not None else None for key in ("ladder", "omni", "typesafe")]


def _input_hashes(ctx: dict) -> list[str | None]:
    return [_digest(ctx[key]) if ctx[key] is not None else None for key in ("ladder", "omni", "typesafe")]


def _browser_helper() -> Path:
    value = os.environ.get("CANVASTTY_MCP_HELPER")
    return Path(value) if value else DEFAULT_HELPER


def _browser_supported(os_name: str | None) -> bool:
    return (os_name or sys.platform) in ("macos", "darwin") and _browser_helper().is_file()


def inputs(repo: Path, codex_home: Path, ladder: Path | None, omni: Path | None,
           typesafe: Path | None, port: int, os_name: str | None = None,
           jev_provider: str = "typesafe") -> dict:
    repo = repo.resolve()
    codex_home = codex_home.expanduser().resolve()
    if codex_home.is_relative_to(repo):
        raise InstallError("Codex profile must be outside the source repository")
    mode = ladder_mode(ladder, omni)
    if jev_provider not in ("typesafe", "openrouter"):
        raise InstallError("Jev decision provider must be typesafe or openrouter")
    if typesafe is None:
        raise InstallError("Provide protected file path: --jev-key-file")
    ladder = ladder_file(ladder) if mode == "active" else None
    omni = protected_file(omni, "OmniRoute auth file") if mode == "active" else None
    typesafe = protected_file(typesafe, "Jev decision key file")
    if any(path is not None and path.is_relative_to(repo) for path in (ladder, omni, typesafe)):
        raise InstallError("Provider configuration and credentials must be outside the source repository")
    if not 1024 <= port <= 65535:
        raise InstallError("--port must be between 1024 and 65535")
    state = codex_home / "jev-harness"
    module = platform_module(os_name)
    plan = module.plan_service(repo, codex_home, state, _service_env(mode, omni, typesafe, port, jev_provider))
    return {"repo": repo, "codex_home": codex_home, "state": state, "ladder": ladder,
            "omni": omni, "typesafe": typesafe, "ladder_mode": mode,
            "jev_provider": jev_provider,
            "service": plan, "platform": module, "port": port}


def doctor(repo: Path, codex_home: Path, ladder: Path | None, omni: Path | None,
           typesafe: Path | None, port: int, os_name: str | None = None,
           jev_provider: str = "typesafe") -> dict:
    issues = []
    if sys.version_info < (3, 11):
        issues.append("Python 3.11+ required")
    if shutil.which("codex") is None:
        issues.append("Codex CLI required and must be on PATH")
    node = shutil.which("node")
    if node is None or shutil.which("npm") is None:
        issues.append("Node.js 22.19+ and npm required")
    else:
        try:
            version = subprocess.run([node, "--version"], capture_output=True,
                                     text=True, timeout=3, check=True).stdout.strip()
            match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
            if not match or tuple(map(int, match.groups()[:2])) < (22, 19):
                issues.append("Node.js 22.19+ required")
        except (OSError, subprocess.SubprocessError):
            issues.append("Cannot verify Node.js version")
    if not (repo / "server/jev_server.py").is_file() or not (repo / "router/bin/install").is_file():
        issues.append("Embedded router/server missing")
    profile = codex_home.expanduser().resolve()
    if profile.is_relative_to(repo.resolve()):
        issues.append("Codex profile must be outside the source repository")
    if profile.exists() and any(profile.iterdir()):
        issues.append("Existing Codex profile requires reviewed migration; use a fresh --codex-home")
    try:
        mode = ladder_mode(ladder, omni)
    except InstallError as exc:
        issues.append(str(exc))
        mode = None
    if jev_provider not in ("typesafe", "openrouter"):
        issues.append("Jev decision provider must be typesafe or openrouter")
    for label, path in (("ladder config", ladder), ("OmniRoute auth", omni), ("Jev decision key", typesafe)):
        if path is None:
            if label == "Jev decision key" or mode == "active":
                issues.append(f"Set protected {label} path")
        else:
            try:
                ladder_file(path) if label == "ladder config" else protected_file(path, label)
            except (OSError, InstallError) as exc:
                issues.append(str(exc))
            if path.expanduser().resolve().is_relative_to(repo.resolve()):
                issues.append(f"{label} must be outside the source repository")
    if not 1024 <= port <= 65535 or not _port_free(port):
        issues.append("Jev loopback port invalid or occupied")
    # A router edge may already be installed independently; this installer has
    # no authority to replace it, even for a fresh Codex profile.
    if not _port_free(4202):
        issues.append("Router edge port 4202 occupied; stop/migrate it separately")
    router_definition = _router_service_definition(os_name)
    if router_definition is not None and (router_definition.exists() or router_definition.is_symlink()):
        issues.append("Router service definition already exists; stop/migrate it separately")
    try:
        module = platform_module(os_name)
        if not issues:
            ctx = inputs(repo, codex_home, ladder, omni, typesafe, port, os_name, jev_provider)
            status = module.service_status(ctx["service"])
            if status["installed"] or status["active"]:
                issues.append("Jev service identity already exists; migration requires review")
    except (OSError, ValueError, InstallError) as exc:
        issues.append(str(exc))
    return {"ready": not issues, "issues": issues, "profile": str(profile),
            "platform": os_name or sys.platform}


def prepare(repo: Path, codex_home: Path, os_name: str | None = None) -> dict:
    """Prepare dependencies in an isolated temporary profile, never live state."""
    import tempfile
    platform_module(os_name)
    if not (repo / "router/bin/install").is_file():
        raise InstallError("Embedded router installer missing")
    with tempfile.TemporaryDirectory(prefix="jev-prepare-") as scratch:
        env = os.environ.copy()
        env["CODEX_HOME"] = scratch
        env["CODEX_ROUTER_STATE_DIR"] = str(Path(scratch) / "router")
        if (os_name or sys.platform) in ("windows", "win32"):
            command = ["powershell", "-NoProfile", "-File", str(repo / "router/install.ps1"),
                       "-CheckoutInstall", "-PrepareOnly"]
        else:
            command = [str(repo / "router/bin/install"), "--prepare-only"]
        subprocess.run(command, cwd=repo / "router", env=env, check=True)
    return {"prepared": True, "profile_untouched": str(codex_home)}


def _atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise InstallError(f"Refusing symlink at managed path: {path}")
    if os.name != "nt":
        path.parent.chmod(0o700)
    tmp = path.with_name(path.name + ".jev-new")
    with tmp.open("xb") as stream:
        stream.write(data)
    if os.name != "nt":
        tmp.chmod(mode)
    tmp.replace(path)


def _digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"Owned file is missing or is a symlink: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inventory(home: Path, definition: Path) -> dict[str, str]:
    """Strict full-profile checkpoint only while installation is incomplete."""
    result = {}
    for path in home.rglob("*"):
        # Codex creates disposable tmp/arg0 symlinks during router setup.
        # They are runtime scratch, not installation-owned profile state.
        if path.relative_to(home).parts[0] == "tmp":
            continue
        if path.is_symlink():
            raise InstallError(f"Symlink in managed profile: {path}")
        if path.is_file() and path.name not in {"journal.json", "manifest.json"}:
            result[str(path.relative_to(home))] = _digest(path)
    if definition.is_file():
        result["@service-definition"] = _digest(definition)
    return result


_MANAGED_FILES = ("config.toml", "hooks.json", "AGENTS.md", "jev-global/ui.mjs",
                  "skills/jev-worker-orchestration/SKILL.md", "jev-harness/ladder-config.json")
_MUTABLE_HOST_FILES = {"config.toml", "hooks.json", "AGENTS.md"}
_ROLLBACK_PHASES = {"complete", "service_removed", "router_disabled", "router_removed"}
_LIVE_INSTALL_PHASES = {"manifest_written", "service_installed"}


def _owned_payload(home: Path, repo: Path, relative: str) -> object:
    path = home / relative
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"Owned file is missing or is a symlink: {path}")
    if relative == "config.toml":
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        servers = config.get("mcp_servers", {})
        manifest = json.loads((home / "jev-harness/manifest.json").read_text(encoding="utf-8"))
        names = ["jev-workers"] + (["jev-browser"] if manifest.get("browser_enabled") else [])
        return {name: servers.get(name) for name in names}
    if relative == "hooks.json":
        hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
        return {event: [row for row in entries if row in hooks.get(event, [])]
                for event, entries in _hook_entries(repo).items()}
    return managed_section(path.read_text(encoding="utf-8"))


def _owned_fingerprint(home: Path, repo: Path, relative: str) -> str:
    try:
        payload = _owned_payload(home, repo, relative)
    except (ValueError, UnicodeError, OSError, TypeError) as exc:
        raise InstallError(f"Owned {relative} changed since installation") from exc
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "jev-owned-v2:" + hashlib.sha256(data).hexdigest()


def _expected_owned_fingerprint(home: Path, repo: Path, relative: str) -> str:
    if relative == "config.toml":
        config = tomllib.loads((home / relative).read_text(encoding="utf-8"))
        current = config.get("mcp_servers", {})
        manifest = json.loads((home / "jev-harness/manifest.json").read_text(encoding="utf-8"))
        expected = {"jev-workers": {"command": sys.executable,
                                    "args": [str(repo / "integrations/workers/mcp.py")]}}
        if manifest.get("browser_enabled"):
            from .browser_config import IDENTITY_ENV
            node = current.get("jev-browser", {}).get("command")
            if not node:
                raise InstallError("Owned browser MCP missing")
            expected["jev-browser"] = {"command": node,
                "args": [str(repo / "integrations/jev/browser_broker/mcp.mjs")],
                "enabled": True, "required": False, "env_vars": list(IDENTITY_ENV)}
        payload = expected
    elif relative == "hooks.json":
        payload = _hook_entries(repo)
    else:
        payload = section(repo, home)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "jev-owned-v2:" + hashlib.sha256(data).hexdigest()


def _managed_inventory(home: Path, definition: Path) -> dict[str, str]:
    """Hash immutable files and only the Jev-owned pieces of host files."""
    result = {}
    manifest = json.loads((home / "jev-harness/manifest.json").read_text(encoding="utf-8"))
    repo = Path(manifest["repo"])
    for relative in _MANAGED_FILES:
        path = home / relative
        if path.exists() or path.is_symlink():
            result[relative] = (_owned_fingerprint(home, repo, relative)
                                if relative in _MUTABLE_HOST_FILES else _digest(path))
    if definition.exists() or definition.is_symlink():
        result["@service-definition"] = _digest(definition)
    return result


def _expected_managed(journal: dict, home: Path | None = None) -> dict[str, str]:
    """Read legacy full-profile journals without adopting their runtime files."""
    owned = journal.get("owned")
    if not isinstance(owned, dict):
        raise InstallError("Installation journal has no file inventory")
    expected = {key: value for key, value in owned.items()
                if key in _MANAGED_FILES or key == "@service-definition"}
    if home is not None:
        repo = Path(journal["repo"])
        for relative in _MUTABLE_HOST_FILES & expected.keys():
            if not expected[relative].startswith("jev-owned-v2:"):
                # Older complete journals recorded whole host files. Adopt
                # only their exact Jev entries; all unrelated edits stay free.
                if _owned_fingerprint(home, repo, relative) != _expected_owned_fingerprint(home, repo, relative):
                    raise InstallError(f"Owned {relative} changed since installation")
                expected[relative] = _owned_fingerprint(home, repo, relative)
    return expected


def _router_service_definition(os_name: str | None) -> Path | None:
    name = os_name or sys.platform
    if name in {"macos", "darwin"}:
        return Path.home() / "Library/LaunchAgents/io.github.codex-router.plist"
    if name == "linux":
        return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user/codex-router.service"
    # Windows Task Scheduler identity cannot be proven by a file hash alone.
    return None


def _journal(state: Path, payload: dict, expected: dict | None = None) -> None:
    path = state / "journal.json"
    if expected is None:
        if path.exists():
            raise InstallError("Installation journal already exists")
    elif path.is_symlink() or not path.is_file() or json.loads(path.read_text()) != expected:
        raise InstallError("Installation journal changed concurrently")
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    if expected is None:
        _atomic(path, data)
    else:
        if os.name != "nt":
            state.chmod(0o700)
        tmp = path.with_name("journal.json.jev-new")
        with tmp.open("xb") as stream:
            stream.write(data)
        if os.name != "nt":
            tmp.chmod(0o600)
        tmp.replace(path)


def _read_journal(home: Path) -> dict:
    path = home / "jev-harness/journal.json"
    if not path.is_file() or path.is_symlink():
        raise InstallError("No owned installation journal; refusing rollback")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("version") != 1 or data.get("codex_home") != str(home)
            or data.get("repo") is None or data.get("service_id") is None
            or data.get("phase") not in {"started", "router_started", "router_ready", "provider_added",
                                         "provider_enabled", "model_configured", "auth_configured",
                                         "chatgpt_deferred", "catalog_refreshed", "picker_configured",
                                         "config_written", "hooks_written", "ladder_written",
                                         "ui_written", "skill_written", "agents_written",
                                         "service_installed", "manifest_written", "complete",
                                         "service_removed", "router_disabled", "router_removed", "rolled_back"}):
        raise InstallError("Installation journal ownership mismatch")
    return data


def _check_checkpoint(home: Path, plan: dict, journal: dict) -> None:
    """Refuse an interrupted install if any managed bytes changed since its checkpoint."""
    inventory = (_managed_inventory if journal["phase"] in _ROLLBACK_PHASES | _LIVE_INSTALL_PHASES
                 else _inventory)
    expected = (_expected_managed(journal, home) if journal["phase"] in _ROLLBACK_PHASES
                else journal.get("owned"))
    if inventory(home, Path(plan["definition_path"])) != expected:
        raise InstallError("Profile changed since installation checkpoint; exact review required")
    definition = journal.get("router_definition_path")
    if definition and _digest(Path(definition)) != journal.get("router_definition_sha256"):
        raise InstallError("Embedded router service changed since installation checkpoint")


def _review_oauth_change(home: Path, plan: dict, journal: dict, expected_sha256: str) -> None:
    """Allow only a specifically reviewed Codex OAuth file change after auth setup."""
    if journal["phase"] not in {"auth_configured", "chatgpt_deferred"} or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise InstallError("OAuth review requires an exact post-auth checkpoint and SHA-256")
    definition = journal.get("router_definition_path")
    if definition and _digest(Path(definition)) != journal.get("router_definition_sha256"):
        raise InstallError("Embedded router service changed since installation checkpoint")
    current = _inventory(home, Path(plan["definition_path"]))
    previous = journal.get("owned", {})
    changed = {name for name in current.keys() | previous.keys() if current.get(name) != previous.get(name)}
    if changed != {"auth.json"} or current.get("auth.json") != expected_sha256:
        raise InstallError("OAuth review did not match the sole auth.json change; exact review required")
    _checkpoint(home / "jev-harness", home, plan, journal, journal["phase"])


def _check_router_provenance(home: Path, repo: Path) -> None:
    path = home / "codex-router/install-manifest.json"
    if not path.exists():
        return  # A mocked bootstrap has no manifest; byte inventory still applies.
    if path.is_symlink() or not path.is_file():
        raise InstallError("Embedded router provenance is not a regular file")
    try:
        current = json.loads(path.read_text(encoding="utf-8"))["current"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InstallError("Embedded router provenance cannot be verified") from exc
    if current.get("sourceRoot") != str(repo / "router") or current.get("target") != "codex":
        raise InstallError("Embedded router provenance differs; refusing continuation")


def _checkpoint(state: Path, home: Path, plan: dict, journal: dict, phase: str) -> None:
    previous = journal.copy()
    journal["phase"] = phase
    journal["owned"] = (_managed_inventory if phase in _LIVE_INSTALL_PHASES else _inventory)(
        home, Path(plan["definition_path"]))
    definition = journal.get("router_definition_path")
    if definition:
        journal["router_definition_sha256"] = _digest(Path(definition))
    _journal(state, journal, previous)


def _continue_install(ctx: dict, journal: dict, os_name: str | None) -> dict:
    repo, home, state, plan = (ctx[key] for key in ("repo", "codex_home", "state", "service"))
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    env["CODEX_ROUTER_STATE_DIR"] = str(home / "codex-router")
    port = ctx["port"]
    provider = ["node", str(repo / "router/src/providers.mjs"), "generic"]

    def run(command):
        subprocess.run(command, cwd=repo, env=env, check=True)

    def write_config():
        path = home / "config.toml"
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        merged = merge_config(original, repo)
        if _browser_supported(os_name):
            merged = merge_browser_config(merged, repo, shutil.which("node") or "node", _browser_helper())
        if merged != original:
            _atomic(path, merged.encode())

    def write_hooks():
        path = home / "hooks.json"
        original = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        merged = merge_hooks(original, repo)
        _atomic(path, (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode())

    def write_agents():
        path = home / "AGENTS.md"
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        _atomic(path, merge_agents(original, repo, home).encode())

    def install_service():
        status = ctx["platform"].service_status(plan)
        if status["installed"] or status["active"]:
            raise InstallError("Service appeared before its installation checkpoint; exact review required")
        ctx["platform"].install_service(plan, dry_run=False)

    def write_manifest():
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        manifest = {"version": 1, "repo": str(repo), "codex_home": str(home),
                    "service_id": plan["service_id"], "port": port,
                    "model_after": config.get("model"),
                    "ladder_mode": ctx["ladder_mode"],
                    "jev_provider": ctx["jev_provider"],
                    "browser_enabled": _browser_supported(os_name),
                    "native_session_sharing": "pending_explicit_opt_in"}
        _atomic(state / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())

    steps = [
        ("provider_added", lambda: run(provider + ["add", "jev", "--name", "Jev Router", "--base-url",
                                         f"http://127.0.0.1:{port}/v1", "--adapter", "openai-responses",
                                         "--allow-private"])),
        ("provider_enabled", lambda: run(provider + ["enable", "jev"])),
        ("model_configured", lambda: run(["node", str(repo / "server/configure-model.mjs")])),
        ("auth_configured", lambda: run(["node", str(repo / "server/configure-auth.mjs")])),
        # Router bootstrap uses --no-discovery. Native session sharing cannot
        # be enabled in that mode; leave OAuth and credential discovery to an
        # explicit owner action after installation.
        ("chatgpt_deferred", lambda: None),
        ("catalog_refreshed", lambda: run(["node", str(repo / "router/src/refresh-catalog.mjs")])),
        ("picker_configured", lambda: run(["node", str(repo / "router/src/control.mjs"), "picker", "set", "jev/auto", "show"])),
        ("config_written", write_config),
        ("hooks_written", write_hooks),
        ("ladder_written", lambda: _atomic(state / "ladder-config.json", ctx["ladder"].read_bytes())
         if ctx["ladder_mode"] == "active" else None),
        ("ui_written", lambda: _atomic(home / "jev-global/ui.mjs", (repo / "integrations/jev/global/jev_ui.mjs").read_bytes())),
        ("skill_written", lambda: _atomic(home / "skills/jev-worker-orchestration/SKILL.md",
                                           (repo / "integrations/jev-worker-orchestration-SKILL.md").read_bytes())),
        ("agents_written", write_agents),
        ("manifest_written", write_manifest),
        ("service_installed", install_service),
    ]
    phases = ["router_ready"] + [name for name, _ in steps]
    if journal["phase"] not in phases:
        raise InstallError("Router bootstrap is ambiguous; exact review required before continuation")
    start = phases.index(journal["phase"])
    for name, action in steps[start:]:
        _check_checkpoint(home, plan, journal)
        action()
        _checkpoint(state, home, plan, journal, name)
    _check_checkpoint(home, plan, journal)
    previous = journal.copy()
    journal["phase"] = "complete"
    journal["manifest_sha256"] = _digest(state / "manifest.json")
    journal["owned"] = _managed_inventory(home, Path(plan["definition_path"]))
    _journal(state, journal, previous)
    return {"installed": True, "codex_home": str(home), "service": plan["service_id"],
            "model_selection": "preserved"}


def install(repo: Path, codex_home: Path, ladder: Path, omni: Path, typesafe: Path,
            port: int, dry_run: bool = True, os_name: str | None = None,
            reviewed_auth_sha256: str | None = None,
            jev_provider: str = "typesafe") -> dict:
    home_check = codex_home.expanduser().resolve()
    journal_path = home_check / "jev-harness/journal.json"
    if not dry_run and journal_path.exists():
        saved = _read_journal(home_check)
        ctx_saved = inputs(repo, codex_home, ladder, omni, typesafe, port, os_name, jev_provider)
        service = ctx_saved["service"]
        if (saved["repo"] != str(ctx_saved["repo"]) or saved["port"] != port
                or saved.get("jev_provider", "typesafe") != jev_provider
                or saved["service_id"] != service["service_id"]
                or saved["definition_path"] != service["definition_path"]
                or saved.get("input_paths") != _input_paths(ctx_saved)):
            raise InstallError("Existing installation identity differs; exact review required")
        if saved.get("input_hashes") and saved["input_hashes"] != _input_hashes(ctx_saved):
            raise InstallError("Installation inputs changed since checkpoint; exact review required")
        if reviewed_auth_sha256 is not None:
            _review_oauth_change(home_check, service, saved, reviewed_auth_sha256)
        _check_checkpoint(home_check, service, saved)
        if saved["phase"] not in {"started", "router_started"}:
            _check_router_provenance(home_check, ctx_saved["repo"])
        status = ctx_saved["platform"].service_status(service)
        if (status["installed"] or status["active"]) and not status.get("owned"):
            raise InstallError("Installed service ownership changed; refusing continuation")
        if saved["phase"] not in {"service_installed", "complete"} \
                and (status["installed"] or status["active"]):
            raise InstallError("Service appeared before its installation checkpoint; exact review required")
        if saved["phase"] == "service_installed" \
                and not status["installed"]:
            raise InstallError("Owned service disappeared after its installation checkpoint")
        router_definition = _router_service_definition(os_name)
        if (router_definition is not None and saved["phase"] not in {"started", "router_started", "complete"}
                and not saved.get("router_definition_path")
                and (router_definition.exists() or router_definition.is_symlink())):
            raise InstallError("Untracked router service definition appeared; exact review required")
        if saved["phase"] == "complete":
            if _digest(home_check / "jev-harness/manifest.json") != saved.get("manifest_sha256"):
                raise InstallError("Installed manifest changed; refusing reinstall")
            return {"installed": True, "already_installed": True,
                    "codex_home": str(home_check), "service": saved["service_id"],
                    "model_selection": "preserved"}
        if saved["phase"] not in {"started", "router_started", "router_ready",
                                  "provider_added", "provider_enabled", "model_configured",
                                  "auth_configured", "chatgpt_deferred", "catalog_refreshed",
                                  "picker_configured", "config_written", "hooks_written",
                                  "ladder_written", "ui_written", "skill_written",
                                  "agents_written", "service_installed", "manifest_written"}:
            raise InstallError("Existing journal cannot be continued; exact review required")
        return _continue_install(ctx_saved, saved, os_name)
    report = doctor(repo, codex_home, ladder, omni, typesafe, port, os_name, jev_provider)
    if not report["ready"]:
        raise InstallError("Preflight failed: " + "; ".join(report["issues"]))
    ctx = inputs(repo, codex_home, ladder, omni, typesafe, port, os_name, jev_provider)
    home, state = ctx["codex_home"], ctx["state"]
    plan = ctx["service"]
    if dry_run:
        ctx["platform"].install_service(plan, dry_run=True)
        owned = ["config.toml", "hooks.json", "AGENTS.md", "jev-global/ui.mjs",
                 "skills/jev-worker-orchestration/SKILL.md", "jev-harness/manifest.json"]
        if ctx["ladder_mode"] == "active":
            owned.append("jev-harness/ladder-config.json")
        return {"dry_run": True, "codex_home": str(home), "service": plan["service_id"],
                "owned_paths": [str(home / name) for name in owned]
                + [plan["definition_path"], str(home / "codex-router")],
                "optional_browser_mcp": _browser_supported(os_name),
                "ladder_mode": ctx["ladder_mode"],
                "jev_provider": ctx["jev_provider"],
                "model_selection": "preserved"}
    # Journal before the first profile or router mutation. A failed router
    # command may have left partially owned state; rollback will refuse to
    # guess at its identity until the router-ready phase is recorded.
    if home.exists() and any(home.iterdir()):
        raise InstallError("Profile changed after preflight; refusing install")
    if not _port_free(port) or not _port_free(4202):
        raise InstallError("Router port changed after preflight; refusing install")
    router_definition = _router_service_definition(os_name)
    if router_definition is not None and (router_definition.exists() or router_definition.is_symlink()):
        raise InstallError("Router service definition appeared after preflight; refusing install")
    if ctx["platform"].service_status(plan)["installed"] or ctx["platform"].service_status(plan)["active"]:
        raise InstallError("Service changed after preflight; refusing install")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt" and (os_name or sys.platform) in ("windows", "win32"):
        # Protect the new profile before writing the journal or any provider
        # metadata. The platform adapter applies the same ACL to its state dir.
        ctx["platform"]._private_state_dir(home, create=False)
    journal = {"version": 1, "phase": "started", "repo": str(ctx["repo"]),
               "codex_home": str(home), "service_id": plan["service_id"],
               "definition_path": plan["definition_path"], "port": port,
               "jev_provider": jev_provider,
               "input_paths": _input_paths(ctx),
               "input_hashes": _input_hashes(ctx),
               "owned": {}, "router_owned": False}
    _journal(state, journal)
    previous = journal.copy()
    journal["phase"] = "router_started"
    _journal(state, journal, previous)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    env["CODEX_ROUTER_STATE_DIR"] = str(home / "codex-router")
    if (os_name or sys.platform) in ("windows", "win32"):
        command = ["powershell", "-NoProfile", "-File", str(repo / "router/install.ps1"),
                   "-CheckoutInstall", "-Target", "codex", "-NoProvider", "-NoDiscovery", "-NoTray"]
    else:
        command = [str(repo / "router/install.sh"), "--target", "codex", "--no-provider", "--no-discovery", "--no-tray"]
    subprocess.run(command, cwd=repo / "router", env=env, check=True)
    previous = journal.copy()
    journal["phase"] = "router_ready"
    journal["router_owned"] = True
    journal["owned"] = _inventory(home, Path(plan["definition_path"]))
    journal["before_overlay"] = journal["owned"].copy()
    if router_definition is not None and router_definition.is_file():
        journal["router_definition_path"] = str(router_definition)
        journal["router_definition_sha256"] = _digest(router_definition)
    _journal(state, journal, previous)
    return _continue_install(ctx, journal, os_name)


def resume(repo: Path, codex_home: Path, ladder: Path, omni: Path, typesafe: Path,
           port: int, os_name: str | None = None,
           reviewed_auth_sha256: str | None = None,
           jev_provider: str = "typesafe") -> dict:
    """Continue a proven partial install; never start a new one implicitly."""
    home = codex_home.expanduser().resolve()
    if not (home / "jev-harness/journal.json").is_file():
        raise InstallError("No installation journal to resume")
    return install(repo, home, ladder, omni, typesafe, port, False, os_name,
                   reviewed_auth_sha256=reviewed_auth_sha256,
                   jev_provider=jev_provider)


def verify(codex_home: Path) -> dict:
    home = codex_home.expanduser().resolve()
    manifest_path = home / "jev-harness/manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise InstallError("No owned installation manifest at this Codex profile")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("codex_home") != str(home):
        raise InstallError("Installation manifest does not belong to this profile")
    mode = manifest.get("ladder_mode", "active")
    if mode not in ("active", "native"):
        raise InstallError("Installation manifest has an unsupported ladder mode")
    jev_provider = manifest.get("jev_provider", "typesafe")
    if jev_provider not in ("typesafe", "openrouter"):
        raise InstallError("Installation manifest has an unsupported Jev provider")
    repo = Path(manifest.get("repo", ""))
    journal = None
    try:
        journal = _read_journal(home)
    except (OSError, ValueError, InstallError):
        pass
    manifest_owned = bool(journal and journal["phase"] == "complete"
                          and manifest.get("version") == 1
                          and journal["repo"] == str(repo)
                          and manifest.get("service_id") == journal["service_id"]
                          and _digest(manifest_path) == journal.get("manifest_sha256"))
    config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    hooks = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
    mcp = config.get("mcp_servers", {})
    worker_config = mcp.get("jev-workers") == {
        "command": sys.executable, "args": [str(repo / "integrations/workers/mcp.py")]}
    browser_config = True
    if manifest.get("browser_enabled"):
        from .browser_config import IDENTITY_ENV
        browser = mcp.get("jev-browser", {})
        browser_config = (browser.get("args") == [str(repo / "integrations/jev/browser_broker/mcp.mjs")]
                          and browser.get("enabled") is True and browser.get("required") is False
                          and browser.get("env_vars") == list(IDENTITY_ENV)
                          and bool(browser.get("command")))
    installed_hooks = hooks.get("hooks", {})
    hook_config = all(any(row == expected for row in installed_hooks.get(event, []))
                      for event, entries in _hook_entries(repo).items() for expected in entries)
    model_changed_since_install = config.get("model") != manifest.get("model_after")
    # A manual model change is owned by the user, not installation damage.
    model_preserved = True
    owned_files = False
    service_identity = False
    service_status = {"installed": False, "owned": False, "active": False}
    if manifest_owned and repo.is_dir():
        try:
            module = platform_module()
            plan = module.plan_service(repo, home, home / "jev-harness",
                                       {"JEV_LISTEN_PORT": str(manifest["port"]), "JEV_LADDER_MODE": mode,
                                        "JEV_DECISION_PROVIDER": jev_provider})
            service_identity = (plan["service_id"] == journal["service_id"]
                                and plan["definition_path"] == journal["definition_path"])
            owned_files = (service_identity and _managed_inventory(home, Path(plan["definition_path"]))
                           == _expected_managed(journal, home))
            if service_identity:
                service_status = module.service_status(plan)
        except (OSError, ValueError, KeyError, InstallError, RuntimeError, subprocess.SubprocessError):
            pass
    files = owned_files
    client = manifest_owned and owned_files and worker_config and browser_config and hook_config
    service_owned_active = (owned_files and service_identity and service_status.get("installed") is True
                            and service_status.get("owned") is True and service_status.get("active") is True)
    health_endpoint = False
    port = manifest.get("port")
    if service_owned_active and isinstance(port, int) and 1024 <= port <= 65535:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as response:
                health = json.loads(response.read(4096))
                health_endpoint = (response.status == 200 and health.get("ok") is True
                                   and health.get("ladder_active") is (mode == "active")
                                   and health.get("decision_provider", "typesafe") == jev_provider
                                   and health.get("auth_configured") is True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    service_health = service_owned_active and health_endpoint
    return {"files": files, "client_config": client, "model_preserved": model_preserved,
            "model_changed_since_install": model_changed_since_install,
            "service_health": service_health, "jev_auto_response": "unverified",
            "manifest_owned": manifest_owned, "owned_files": owned_files,
            "worker_mcp_config": worker_config, "browser_mcp_config": browser_config,
            "hook_config": hook_config, "service_identity": service_identity,
            "service_installed_owned_active": service_owned_active,
            "health_endpoint": health_endpoint,
            "ladder_mode": mode,
            "jev_provider": jev_provider,
            "action": "Check platform service and send a real jev/auto request from a new Codex session"}


def rollback(codex_home: Path, os_name: str | None = None) -> dict:
    home = codex_home.expanduser().resolve()
    journal = _read_journal(home)
    manifest_path = home / "jev-harness/manifest.json"
    if journal["phase"] == "rolled_back":
        return {"service_removed": True, "router_removed": bool(journal.get("router_removed")),
                "already_rolled_back": True, "profile_retained": str(home)}
    if journal["phase"] not in {"complete", "service_removed", "router_disabled", "router_removed"}:
        raise InstallError("Partial install is not proven complete; retain profile and inspect journal before cleanup")
    if manifest_path.is_symlink() or not manifest_path.is_file() or _digest(manifest_path) != journal.get("manifest_sha256"):
        raise InstallError("Manifest changed; refusing rollback")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("codex_home") != str(home) or manifest.get("version") != 1
            or manifest.get("repo") != journal["repo"]
            or manifest.get("service_id") != journal["service_id"]):
        raise InstallError("Manifest ownership mismatch")
    repo = Path(journal["repo"])
    if not repo.is_dir():
        raise InstallError("Pinned repository is missing; refusing service changes")
    module = platform_module(os_name)
    mode = manifest.get("ladder_mode", "active")
    if mode not in ("active", "native"):
        raise InstallError("Manifest ladder mode changed; refusing rollback")
    jev_provider = manifest.get("jev_provider", "typesafe")
    if jev_provider not in ("typesafe", "openrouter"):
        raise InstallError("Manifest Jev provider changed; refusing rollback")
    plan = module.plan_service(repo, home, home / "jev-harness",
                               {"JEV_LADDER_MODE": mode, "JEV_DECISION_PROVIDER": jev_provider})
    if plan["service_id"] != journal["service_id"] or plan["definition_path"] != journal["definition_path"]:
        raise InstallError("Service identity changed; refusing rollback")
    expected = _expected_managed(journal, home)
    if _managed_inventory(home, Path(plan["definition_path"])) != expected:
        raise InstallError("Profile or service definition changed since install; refusing rollback")
    agents_path = home / "AGENTS.md"
    restored_agents = None
    if expected.get("AGENTS.md"):
        restored_agents = remove_agents(agents_path.read_text(encoding="utf-8"), repo, home)
    status = module.service_status(plan)
    if status["installed"] and not status.get("owned"):
        raise InstallError("Service ownership changed; refusing rollback")
    if status["active"] and not status.get("owned"):
        raise InstallError("Active service ownership cannot be proven; refusing rollback")
    router_definition = journal.get("router_definition_path")
    router_proven = False
    if router_definition and journal["phase"] not in {"router_removed"}:
        path = Path(router_definition)
        expected_path = _router_service_definition(os_name)
        if expected_path is None or path != expected_path or _digest(path) != journal.get("router_definition_sha256"):
            raise InstallError("Embedded router service changed; refusing rollback")
        router_proven = True
    if journal["phase"] == "complete":
        module.remove_service(plan, dry_run=False)
        previous = journal.copy()
        journal["phase"] = "service_removed"
        journal["owned"] = _managed_inventory(home, Path(plan["definition_path"]))
        _journal(home / "jev-harness", journal, previous)
    router_removed = journal["phase"] == "router_removed"
    if router_proven and journal["phase"] == "service_removed":
        env = {**os.environ, "CODEX_HOME": str(home),
               "CODEX_ROUTER_STATE_DIR": str(home / "codex-router")}
        subprocess.run(["node", str(repo / "router/src/config-manager.mjs"), "disable"],
                       cwd=repo / "router", env=env, check=True)
        previous = journal.copy()
        journal["phase"] = "router_disabled"
        journal["owned"] = _managed_inventory(home, Path(plan["definition_path"]))
        _journal(home / "jev-harness", journal, previous)
    if router_proven and journal["phase"] == "router_disabled":
        env = {**os.environ, "CODEX_HOME": str(home),
               "CODEX_ROUTER_STATE_DIR": str(home / "codex-router")}
        subprocess.run(["node", str(repo / "router/src/service.mjs"), "uninstall"],
                       cwd=repo / "router", env=env, check=True)
        router_removed = True
        previous = journal.copy()
        journal["phase"] = "router_removed"
        journal["owned"] = _managed_inventory(home, Path(plan["definition_path"]))
        _journal(home / "jev-harness", journal, previous)
    # Remove only immutable harness artifacts whose bytes still match the
    # journal. Native router state and mutable client config remain available
    # for inspection; the supported disable above detaches its managed block.
    removed = []
    if restored_agents is not None:
        if restored_agents:
            _atomic(agents_path, restored_agents.encode())
        else:
            agents_path.unlink()
        removed.append("AGENTS.md managed section")
    hooks_path = home / "hooks.json"
    if expected.get("hooks.json"):
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        for event, entries in _hook_entries(repo).items():
            rows = hooks.get("hooks", {}).get(event, [])
            hooks["hooks"][event] = [row for row in rows if row not in entries]
            if not hooks["hooks"][event]:
                del hooks["hooks"][event]
        if hooks.get("hooks") or any(key != "hooks" for key in hooks):
            _atomic(hooks_path, (json.dumps(hooks, ensure_ascii=False, indent=2) + "\n").encode())
        elif journal.get("before_overlay", {}).get("hooks.json") is None:
            hooks_path.unlink()
        else:
            _atomic(hooks_path, (json.dumps(hooks, ensure_ascii=False, indent=2) + "\n").encode())
        removed.append("hooks.json managed rows")
    for relative in ("jev-global/ui.mjs",
                     "skills/jev-worker-orchestration/SKILL.md", "jev-harness/ladder-config.json"):
        path = home / relative
        if expected.get(relative) and path.is_file() and _digest(path) == expected[relative]:
            path.unlink()
            removed.append(relative)
    previous = journal.copy()
    journal["phase"] = "rolled_back"
    journal["router_removed"] = router_removed
    journal["owned"] = _managed_inventory(home, Path(plan["definition_path"]))
    _journal(home / "jev-harness", journal, previous)
    return {"service_removed": True, "router_removed": router_removed,
            "removed_files": removed, "profile_retained": str(home),
            "action": "Router state and client config retained for review; no blanket profile deletion"}
