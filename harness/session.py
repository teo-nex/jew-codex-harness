"""Profile-bound, explicit native-session setup; never exports credentials."""

import json
from pathlib import Path

from . import core
from .process import run_bounded


def _profile(repo: Path, home: Path) -> tuple[Path, Path]:
    repo, home = repo.resolve(), home.expanduser().resolve()
    manifest_path = home / "jev-harness/manifest.json"
    if (manifest_path.is_symlink() or not manifest_path.is_file()
            or manifest_path.parent.is_symlink() or (home / "codex-router").is_symlink()):
        raise core.InstallError("Session setup requires an owned installation manifest and state")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise core.InstallError("Session setup requires a valid installation manifest")
    journal = core._read_journal(home)
    if (journal["phase"] != "complete" or manifest.get("version") != 1
            or manifest.get("codex_home") != str(home) or manifest.get("repo") != str(repo)
            or journal.get("repo") != str(repo)
            or manifest.get("service_id") != journal.get("service_id")
            or core._digest(manifest_path) != journal.get("manifest_sha256")):
        raise core.InstallError("Session setup profile ownership mismatch; use the installed checkout and profile")
    return repo, home


def manage(repo: Path, home: Path, action: str, *, allow_sharing: bool = False) -> dict:
    if action not in {"status", "enable", "disable"}:
        raise core.InstallError("Unknown session action")
    if action == "enable" and not allow_sharing:
        raise core.InstallError("Session sharing lets other local router clients use this ChatGPT login. "
                                "After owner approval, repeat session enable --allow-sharing")
    repo, home = _profile(repo, home)
    env = core._router_environment(home)
    env["CODEX_ROUTER_SOURCE_ROOT"] = str(repo / "router")
    # A parent shell's auth-path override must not read another profile's login.
    env["MODEL_ROUTER_CODEX_AUTH"] = str(home / "auth.json")

    def run(script, args):
        result = run_bounded(["node", str(repo / "router/src" / script), *args],
                             cwd=repo, env=env, timeout=120)
        if result.returncode:
            # Child errors/publication logs can wrap arbitrary private text.
            raise core.InstallError("Session action failed for the selected profile. Run session status; "
                                    "check login, discovery overrides and client catalog publication")
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise core.InstallError("Session command returned an invalid status") from exc
        if not isinstance(value, dict):
            raise core.InstallError("Session command returned an invalid status")
        return value

    if action == "enable":
        if env.get("CODEX_ROUTER_NO_DISCOVERY") == "1":
            raise core.InstallError("CODEX_ROUTER_NO_DISCOVERY=1 blocks session setup; review the service environment")
        override = env.get("CODEX_ROUTER_NATIVE_SESSION_FALLBACK")
        if override is not None and override != "1":
            raise core.InstallError("CODEX_ROUTER_NATIVE_SESSION_FALLBACK blocks session sharing; review the service environment")
        # Both changes are explicitly authorized by --allow-sharing. If login is
        # absent, discovery stays enabled but no sharing consent is created.
        run("discovery-mode.mjs", ["set", "enabled"])
        if run("chatgpt-session.mjs", ["status", "--json"]).get("session") != "usable":
            raise core.InstallError("No usable ChatGPT login in the selected profile. Sign in with this "
                                    "CODEX_HOME, then repeat session enable --allow-sharing; discovery is enabled")
    status = run("chatgpt-session.mjs", [action, "--json"])
    return {"action": action, **status}
