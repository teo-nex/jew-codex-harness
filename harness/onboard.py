"""Interactive first install; credentials never enter argv or printed reports."""

from __future__ import annotations

import getpass
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys

from . import core
from . import live_verify


def _offline_smoke(repo: Path) -> None:
    result = subprocess.run([sys.executable, str(repo / "scripts/smoke_config_offline.py")],
                            cwd=repo, capture_output=True, timeout=180, check=False)
    if result.returncode:
        raise core.InstallError("Isolated router configuration smoke check failed")


def _answer(read, question: str, default: str) -> str:
    value = read(f"{question} [{default}]: ").strip()
    return value or default


def _new_key_path(profile: Path, provider: str, root: Path | None = None) -> Path:
    base = root or Path.home() / ".config/jev-codex-harness/keys"
    # Check the owned key subtree; macOS may legitimately expose /var as a
    # symlink to /private/var above a temporary test root.
    for path in (base, base.parent, base.parent.parent):
        if path.is_symlink():
            raise core.InstallError("Decision key path contains a symlink")
    identity = hashlib.sha256(str(profile).encode()).hexdigest()[:20]
    directory = base / identity
    if directory.is_symlink():
        raise core.InstallError("Decision key directory is a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        from .platforms.windows import _private_state_dir
        _private_state_dir(directory, create=False)
    else:
        directory.chmod(0o700)
    return directory / (provider + ".key")


def _launch_command(home: Path) -> str:
    if os.name == "nt":
        return "$env:CODEX_HOME = '" + str(home).replace("'", "''") + "'; codex"
    return "CODEX_HOME=" + shlex.quote(str(home)) + " codex"


def _report_command(repo: Path, home: Path) -> str:
    script = repo / "integrations/jev/report_api_cost.py"
    if os.name == "nt":
        return "$env:CODEX_HOME = '" + str(home).replace("'", "''") + "'; py -3 '" + str(script).replace("'", "''") + "' --summary"
    return "CODEX_HOME=" + shlex.quote(str(home)) + " python3 " + shlex.quote(str(script)) + " --summary"


def run(repo: Path, codex_home: Path, port: int, *, default_provider: str = "typesafe",
        ladder: Path | None = None, omni: Path | None = None, key_file: Path | None = None,
        read=input, secret=getpass.getpass, key_root: Path | None = None) -> dict:
    if default_provider not in ("typesafe", "openrouter"):
        raise core.InstallError("Jev provider must be typesafe or openrouter")
    default_home = codex_home.expanduser().resolve()
    if default_home.exists() and any(default_home.iterdir()):
        default_home = Path.home() / ".codex-jev"
    home = Path(_answer(read, "Fresh Codex profile path", str(default_home))).expanduser().resolve()
    selected = _answer(read, "Jev key provider (typesafe/openrouter)", default_provider).lower()
    if selected not in ("typesafe", "openrouter"):
        raise core.InstallError("Choose typesafe or openrouter for Jev")
    external_default = "yes" if ladder is not None and omni is not None else "no"
    external = _answer(read, "Use an existing OmniRoute gateway? (yes/no)", external_default).lower()
    if external not in ("yes", "no"):
        raise core.InstallError("Choose yes or no for OmniRoute")
    if external == "yes":
        ladder = ladder or Path(_answer(read, "Protected ladder config path", ""))
        omni = omni or Path(_answer(read, "Protected OmniRoute auth path", ""))
        if str(ladder) == "." or str(omni) == ".":
            raise core.InstallError("Both OmniRoute paths are required")
    else:
        ladder = omni = None

    # Refuse structural conflicts before collecting or writing a credential.
    if key_file is None:
        preliminary = core.doctor(repo, home, ladder, omni, None, port,
                                  jev_provider=selected)
        blockers = [issue for issue in preliminary["issues"]
                    if issue != "Set protected Jev decision key path"]
        if blockers:
            return {"ready": False, "issues": blockers, "key_written": False,
                    "codex_home": str(home)}
        key_text = secret(f"{selected} Jev API key (hidden): ").strip()
        if (not key_text or len(key_text) > 4096
                or any(ch.isspace() or ord(ch) < 0x20 for ch in key_text)):
            raise core.InstallError("A non-empty single-line Jev key is required")
        key_file = _new_key_path(home, selected, key_root)
        if key_file.exists() or key_file.is_symlink():
            raise core.InstallError("Decision key file exists; use --jev-key-file to reuse it")
        core._atomic(key_file, (key_text + "\n").encode())
        key_text = ""
        created_key = key_file
    else:
        created_key = None

    try:
        report = core.doctor(repo, home, ladder, omni, key_file, port,
                             jev_provider=selected)
        if not report["ready"]:
            return {"ready": False, "issues": report["issues"], "key_written": False,
                    "codex_home": str(home)}
        core.prepare(repo, home)
        _offline_smoke(repo)
        dry_run = core.install(repo, home, ladder, omni, key_file, port,
                               dry_run=True, jev_provider=selected)
        installed = core.install(repo, home, ladder, omni, key_file, port,
                                 dry_run=False, jev_provider=selected)
        verified = core.verify(home)
        local_ready = all(verified.get(key) is True for key in
                          ("files", "client_config", "model_preserved", "service_health"))
        decision = (live_verify.verify_decision(home) if local_ready else
                    {"ok": False, "status": "not_run", "reason": "local verification failed"})
        return {"installed": installed.get("installed") is True,
                "ready": False, "status": ("hook_trust_pending" if decision["ok"] else
                                           "jev_decision_failed" if local_ready else "local_verification_failed"),
                "codex_home": str(home), "jev_provider": selected,
                "ladder_mode": "active" if external == "yes" else "native",
                "launch_command": _launch_command(home),
                "report_command": _report_command(repo, home),
                "key_file": str(key_file), "dry_run": dry_run,
                "local_verify": verified, "jev_decision": decision,
                "next_steps": ["Review and trust Jev hooks with /hooks in a fresh Codex session",
                               "Run verify --live --manual-model YOUR_MODEL after hook trust",
                               "After observed calls, run report_command to see the API-equivalent table",
                               "Complete Browser/Computer/Compact capability checks in the real client"]}
    finally:
        # A failed preflight should not leave a newly supplied secret behind.
        # Once installation has a journal, retain it for an exact resume.
        if (created_key is not None and not (home / "jev-harness/journal.json").exists()
                and created_key.is_file() and not created_key.is_symlink()):
            created_key.unlink()
