"""Run with ``python -m harness.cli`` from the repository root."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import core
from . import live_verify
from . import onboard


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(description="Install Jev Codex Harness into a fresh Codex profile")
    app.add_argument("--codex-home", type=Path,
                     default=Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex")
    app.add_argument("--ladder-config", type=Path, default=Path(os.environ["JEV_LADDER_CONFIG"]) if os.environ.get("JEV_LADDER_CONFIG") else None)
    app.add_argument("--omniroute-auth-file", type=Path, default=Path(os.environ["JEV_OMNIROUTE_AUTH_FILE"]) if os.environ.get("JEV_OMNIROUTE_AUTH_FILE") else None)
    app.add_argument("--jev-provider", choices=("typesafe", "openrouter"),
                     default=os.environ.get("JEV_DECISION_PROVIDER", "typesafe"))
    app.add_argument("--jev-key-file", "--typesafe-key-file", dest="typesafe_key_file",
                     type=Path)
    app.add_argument("--port", type=int, default=4319)
    sub = app.add_subparsers(dest="command", required=True)
    for command in ("doctor", "prepare", "install", "resume", "verify", "rollback", "onboard"):
        child = sub.add_parser(command)
        if command == "install":
            child.add_argument("--dry-run", action="store_true")
        if command == "resume":
            child.add_argument("--reviewed-auth-sha256")
        if command == "verify":
            child.add_argument("--live", action="store_true",
                               help="make one bounded synthetic request through jev/auto")
            child.add_argument("--manual-model", help="also prove ordinary Tool Use under a manually selected model")
    return app


def main(argv: list[str] | None = None, repo: Path | None = None) -> int:
    args = parser().parse_args(argv)
    if args.typesafe_key_file is None:
        key_name = "JEV_DECISION_KEY_FILE" if args.jev_provider == "openrouter" else "TYPESAFE_API_KEY_FILE"
        if os.environ.get(key_name):
            args.typesafe_key_file = Path(os.environ[key_name])
    repo = repo or Path(__file__).resolve().parents[1]
    try:
        if args.command == "onboard":
            if not sys.stdin.isatty():
                raise core.InstallError("onboard needs an interactive terminal; use install with a protected --jev-key-file for automation")
            result = onboard.run(repo, args.codex_home, args.port,
                                 default_provider=args.jev_provider,
                                 ladder=args.ladder_config, omni=args.omniroute_auth_file,
                                 key_file=args.typesafe_key_file)
            code = 0 if (result.get("ready") is True or
                         (result.get("installed") is True and result.get("status") == "hook_trust_pending")) else 2
        elif args.command == "doctor":
            result = core.doctor(repo, args.codex_home, args.ladder_config,
                                 args.omniroute_auth_file, args.typesafe_key_file, args.port,
                                 jev_provider=args.jev_provider)
            code = 0 if result["ready"] else 2
        elif args.command == "prepare":
            result = core.prepare(repo, args.codex_home)
            code = 0
        elif args.command == "install":
            result = core.install(repo, args.codex_home, args.ladder_config,
                                  args.omniroute_auth_file, args.typesafe_key_file,
                                  args.port, dry_run=args.dry_run,
                                  jev_provider=args.jev_provider)
            code = 0
        elif args.command == "resume":
            result = core.resume(repo, args.codex_home, args.ladder_config,
                                 args.omniroute_auth_file, args.typesafe_key_file,
                                 args.port, reviewed_auth_sha256=args.reviewed_auth_sha256,
                                 jev_provider=args.jev_provider)
            code = 0
        elif args.command == "verify":
            result = core.verify(args.codex_home)
            code = 0 if all(result.get(k) is True for k in ("files", "client_config", "model_preserved", "service_health")) else 2
            if args.live:
                result["jev_decision"] = (live_verify.verify_decision(args.codex_home) if code == 0 else
                                          {"ok": False, "status": "not_run", "reason": "local verification failed"})
                result["live"] = (live_verify.verify_live(args.codex_home)
                                  if result["jev_decision"].get("ok") is True else
                                  {"ok": False, "status": "not_run", "reason": "Jev decision check failed"})
                result["cost_telemetry"] = (live_verify.verify_cost_telemetry(args.codex_home)
                                            if result["live"].get("ok") is True else
                                            {"ok": False, "status": "not_run", "reason": "live response failed"})
                if args.manual_model:
                    result["manual_model"] = (live_verify.verify_manual(args.codex_home, args.manual_model)
                                              if code == 0 else
                                              {"ok": False, "status": "not_run", "reason": "local verification failed"})
                code = 0 if (result["live"].get("ok") is True
                             and result["cost_telemetry"].get("ok") is True
                             and (not args.manual_model or result["manual_model"].get("ok") is True)) else 2
        else:
            result = core.rollback(args.codex_home)
            code = 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (core.InstallError, OSError, ValueError) as exc:
        # File path labels may be shown; credential contents never are.
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except subprocess.SubprocessError as exc:
        print(json.dumps({"ok": False, "error": "Installation command failed",
                          "type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
