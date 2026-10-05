"""Interactive first install; credentials never enter argv or printed reports."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from . import core
from . import live_verify
from . import catalog
from server.provider_ladder import NATIVE_MODELS, validate_config


def _provider_sequence(read, models=None) -> dict:
    try:
        count = int(_answer(read, "Number of providers in fallback order (1-32)", "1"))
    except ValueError as exc:
        raise core.InstallError("Provider count must be an integer") from exc
    if not 1 <= count <= 32:
        raise core.InstallError("Provider count must be between 1 and 32")
    providers, profiles = [], {}
    for index in range(count):
        prefix = f"Provider {index + 1}"
        identity = _answer(read, prefix + " name", f"provider-{index + 1}")
        transport = _answer(read, prefix + " transport (omniroute/native)", "omniroute").lower()
        mode = _answer(read, prefix + " model selection (fixed/jev)",
                       "jev" if transport == "native" else "fixed").lower()
        provider = {"id": identity, "transport": transport}
        def destination(question, default=""):
            if transport == "omniroute" and models is not None:
                return catalog.choose(read, models, question)
            return _answer(read, question, default)
        if mode == "fixed":
            provider["model"] = destination(prefix + " exact model ID")
        elif mode == "jev":
            provider["models"] = {
                model: destination(prefix + " destination for " + model,
                               model if transport == "native" else "")
                for model in NATIVE_MODELS
            }
        else:
            raise core.InstallError("Model selection must be fixed or jev")
        if transport == "omniroute":
            accounts = _answer(read, prefix + " connection IDs in order (comma-separated, optional)", "")
            if accounts:
                provider["connection_ids"] = [item.strip() for item in accounts.split(",")]
            models = [provider["model"]] if mode == "fixed" else provider["models"].values()
            for model in dict.fromkeys(models):
                if model in profiles:
                    continue
                levels = _answer(read, model + " reasoning levels (unknown/unsupported/comma-separated levels)", "unknown")
                if levels == "unsupported":
                    profiles[model] = {"supported": False}
                elif levels != "unknown":
                    profiles[model] = {"supported_efforts": [item.strip() for item in levels.split(",")]}
        providers.append(provider)
    try:
        return validate_config({"version": 2, "providers": providers, "reasoning_profiles": profiles})
    except ValueError as exc:
        raise core.InstallError(str(exc)) from exc


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
    home = Path(_answer(read, "Fresh Codex profile path", str(default_home))).expanduser().resolve()
    selected = _answer(read, "Jev key provider (typesafe/openrouter)", default_provider).lower()
    if selected not in ("typesafe", "openrouter"):
        raise core.InstallError("Choose typesafe or openrouter for Jev")
    external_default = "yes" if ladder is not None and omni is not None else "no"
    external = _answer(read, "Use an existing OmniRoute gateway? (yes/no)", external_default).lower()
    if external not in ("yes", "no"):
        raise core.InstallError("Choose yes or no for OmniRoute")
    generated_config = None
    if external == "yes":
        omni = omni or Path(_answer(read, "Protected OmniRoute auth path", ""))
        if ladder is None:
            sequence_mode = _answer(read, "Provider sequence (create/file)", "create").lower()
            if sequence_mode == "create":
                try:
                    available = catalog.fetch(omni)
                except core.InstallError:
                    available = None
                    print("Catalog unavailable; enter exact model IDs (availability unverified).", file=sys.stderr)
                generated_config = _provider_sequence(read, available)
                for warning in catalog.warnings(generated_config, available or []):
                    print(json.dumps(warning), file=sys.stderr)
            elif sequence_mode == "file":
                ladder = Path(_answer(read, "Protected provider config path", ""))
            else:
                raise core.InstallError("Choose create or file for the provider sequence")
        if str(omni) == "." or (generated_config is None and str(ladder) == "."):
            raise core.InstallError("Both OmniRoute paths are required")
        if generated_config is not None:
            core.protected_file(omni, "OmniRoute auth file")
    else:
        ladder = omni = None

    # Refuse structural conflicts before collecting or writing a credential.
    preliminary = core.doctor(repo, home, ladder, omni if ladder is not None else None, key_file, port,
                              jev_provider=selected)
    blockers = [issue for issue in preliminary["issues"]
                if issue != "Set protected Jev decision key path"]
    if blockers:
        return {"ready": False, "issues": blockers, "key_written": False,
                "codex_home": str(home)}
    if key_file is None:
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

    created_ladder = None
    try:
        if generated_config is not None:
            ladder = _new_key_path(home, selected, key_root).with_name("providers.json")
            if ladder.exists() or ladder.is_symlink():
                raise core.InstallError("Provider config exists; use --ladder-config to reuse it")
            core._atomic(ladder, (json.dumps(generated_config, indent=2) + "\n").encode())
            created_ladder = ladder
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
                "ladder_config": str(ladder) if ladder is not None else None,
                "provider_order": ([p["id"] for p in generated_config["providers"]]
                                   if generated_config is not None else None),
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
        if not (home / "jev-harness/journal.json").exists():
            for created in (created_key, created_ladder):
                if created is not None and created.is_file() and not created.is_symlink():
                    created.unlink()
