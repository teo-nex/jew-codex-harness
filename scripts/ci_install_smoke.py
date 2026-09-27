"""Exercise the real Codex installer on a disposable GitHub-hosted runner.

No provider request is made. A synthetic Jev decision key only satisfies the
installer's protected-file preflight; it cannot authenticate to a provider.
The service and router still use their real platform adapters.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
REQUIRED_VERIFY = ("files", "client_config", "model_preserved", "service_health")


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _private_synthetic_key(path: Path) -> None:
    path.write_text("ci-synthetic-key-no-provider-access\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
        return
    # Windows st_mode cannot validate ACLs. Grant only this runner identity;
    # harness.core.protected_file independently checks the resulting ACL.
    script = (
        "$ErrorActionPreference = 'Stop'; $p = $env:JEV_CI_KEY_PATH; "
        "$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User; "
        "try { $acl = [System.Security.AccessControl.FileSecurity]::new(); "
        "$acl.SetAccessRuleProtection($true, $false); "
        "$rule = [System.Security.AccessControl.FileSystemAccessRule]::new("
        "$sid, [System.Security.AccessControl.FileSystemRights]::FullControl, "
        "[System.Security.AccessControl.InheritanceFlags]::None, "
        "[System.Security.AccessControl.PropagationFlags]::None, "
        "[System.Security.AccessControl.AccessControlType]::Allow); "
        "$acl.AddAccessRule($rule); [System.IO.File]::SetAccessControl($p, $acl) } "
        "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "JEV_CI_KEY_PATH": str(path)},
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError("Windows synthetic key ACL failed: " + result.stderr.strip()[-500:])


def _result_object(output: str) -> dict:
    """CLI dependency steps may print first; use the final JSON object."""
    decoder = json.JSONDecoder()
    for line in reversed(output.splitlines()):
        if not line.startswith("{"):
            continue
        start = output.rfind(line)
        try:
            value, end = decoder.raw_decode(output[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not output[start + end :].strip():
            return value
    raise RuntimeError("installer did not emit a final JSON result")


def _run_cli(command: str, profile: Path, key: Path, port: int, env: dict[str, str],
             timeout: int = 1200, extra: tuple[str, ...] = ()) -> dict:
    argv = [sys.executable, "-m", "harness.cli", "--codex-home", str(profile),
            "--jev-provider", "typesafe", "--jev-key-file", str(key),
            "--port", str(port), command, *extra]
    completed = subprocess.run(argv, cwd=REPO, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=timeout, check=False)
    if completed.returncode and command != "verify":
        # This job has only a synthetic key. Keep logs bounded for CI review.
        tail = completed.stdout[-1800:].strip()
        raise RuntimeError(f"{command} exited {completed.returncode}: {tail}")
    return _result_object(completed.stdout)


def _check(name: str, result: dict, expected: dict[str, object]) -> None:
    missing = {key: value for key, value in expected.items() if result.get(key) != value}
    if missing:
        raise RuntimeError(f"{name} returned unexpected fields: {missing}; result={result}")
    print(json.dumps({"stage": name, "status": "passed"}), flush=True)


def _service_diagnostic(profile: Path) -> None:
    # CI uses only a synthetic key; keep failure details bounded and never
    # dump configuration files or credential material.
    if sys.platform == "linux":
        for command in (
            ["systemctl", "--user", "show", "jev-codex-harness-router.service",
             "--property=ActiveState,SubState,ExecMainStatus,NRestarts"],
            ["journalctl", "--user", "-u", "jev-codex-harness-router.service",
             "-n", "12", "--no-pager", "-o", "cat"],
        ):
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
                print(json.dumps({"diagnostic": command[0],
                                  "output": (result.stdout or result.stderr)[-1500:]}),
                      file=sys.stderr, flush=True)
            except (OSError, subprocess.TimeoutExpired):
                pass
    elif os.name == "nt":
        log = profile / "codex-router/router.log"
        if log.is_file():
            print(json.dumps({"diagnostic": "router_log_tail",
                              "output": log.read_text(encoding="utf-8", errors="replace")[-1800:]}),
                  file=sys.stderr, flush=True)


def main() -> int:
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        print("CI_INSTALL_SMOKE_REQUIRES_GITHUB_HOSTED_RUNNER", file=sys.stderr)
        return 2
    if os.environ.get("RUNNER_ENVIRONMENT", "").lower() != "github-hosted":
        print("CI_INSTALL_SMOKE_REFUSES_SELF_HOSTED_RUNNER", file=sys.stderr)
        return 2

    stage = "setup"
    with tempfile.TemporaryDirectory(prefix="jev-install-ci-") as scratch:
        root = Path(scratch)
        home = root / "home"
        home.mkdir(mode=0o700)
        profile = root / "codex-profile"
        key = root / "synthetic-jev-key"
        _private_synthetic_key(key)
        port = _free_port()
        env = os.environ.copy()
        env["CODEX_HOME"] = str(profile)
        env["CODEX_ROUTER_STATE_DIR"] = str(profile / "codex-router")
        if sys.platform == "darwin":
            env["HOME"] = str(home)
        for name, dirname in (("XDG_CONFIG_HOME", "config"),
                              ("XDG_DATA_HOME", "data"),
                              ("XDG_CACHE_HOME", "cache"),
                              ("XDG_STATE_HOME", "state")):
            env[name] = str(root / dirname)
        if sys.platform == "linux":
            # The user systemd manager resolves units under the runner's real
            # home; an isolated XDG_CONFIG_HOME would hide the installed unit.
            env["XDG_CONFIG_HOME"] = str(Path.home() / ".config")

        installed = False
        failed = False
        try:
            stage = "doctor"
            _check(stage, _run_cli(stage, profile, key, port, env), {"ready": True})
            stage = "prepare"
            _check(stage, _run_cli(stage, profile, key, port, env), {"prepared": True})
            stage = "install-dry-run"
            _check(stage, _run_cli("install", profile, key, port, env,
                                   extra=("--dry-run",)), {"dry_run": True})
            stage = "install"
            result = _run_cli(stage, profile, key, port, env)
            _check(stage, result, {"installed": True})
            installed = True
            stage = "verify"
            for attempt in range(20):
                try:
                    result = _run_cli(stage, profile, key, port, env, timeout=20)
                except RuntimeError as exc:
                    result = {"verify_error": str(exc)[-1000:]}
                if all(result.get(field) is True for field in REQUIRED_VERIFY):
                    break
                if attempt < 19:
                    time.sleep(1)
            _check(stage, result, {field: True for field in REQUIRED_VERIFY})
            # A successful offline install proves files, client wiring, service
            # ownership and loopback health. It does not prove a model response.
            print(json.dumps({"result": "offline_install_verified",
                              "live_provider": "not_tested",
                              "codex_oauth": "not_tested",
                              "visible_worker_windows": "not_tested"}), flush=True)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            failed = True
            print(json.dumps({"stage": stage, "status": "failed",
                              "error": str(exc)[:2000]}), file=sys.stderr, flush=True)
            _service_diagnostic(profile)
        finally:
            if installed:
                try:
                    stage = "rollback"
                    result = _run_cli(stage, profile, key, port, env, timeout=180)
                    _check(stage, result, {"service_removed": True})
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    failed = True
                    print(json.dumps({"stage": "rollback", "status": "failed",
                                      "error": str(exc)[:2000]}), file=sys.stderr, flush=True)
            if failed:
                journal_path = profile / "jev-harness" / "journal.json"
                phase = "not_created"
                if journal_path.is_file():
                    try:
                        saved = json.loads(journal_path.read_text(encoding="utf-8"))
                        phase = saved.get("phase", "invalid")
                        if "owned" in saved:
                            sys.path.insert(0, str(REPO))
                            from harness.core import _inventory
                            try:
                                actual = _inventory(profile, Path(saved["definition_path"]))
                                before = saved["owned"]
                                changed = sorted(name for name in actual.keys() | before.keys()
                                                 if actual.get(name) != before.get(name))
                                print(json.dumps({"diagnostic": "checkpoint_paths",
                                                  "changed": changed[:20],
                                                  "total_changed": len(changed)}),
                                      file=sys.stderr, flush=True)
                            except (OSError, RuntimeError, ValueError) as diagnostic_error:
                                print(json.dumps({"diagnostic": "checkpoint_inventory_error",
                                                  "error": str(diagnostic_error)[:300]}),
                                      file=sys.stderr, flush=True)
                    except (OSError, ValueError):
                        phase = "unreadable"
                print(json.dumps({"diagnostic": "installer_checkpoint",
                                  "phase": phase,
                                  "manifest_exists": (profile / "jev-harness" / "manifest.json").is_file()}),
                      file=sys.stderr, flush=True)
            # On a partial install rollback is intentionally forbidden by the
            # product. The entire GitHub-hosted VM is disposed after this job.
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
