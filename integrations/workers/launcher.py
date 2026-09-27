"""Scoped pi 0.85.1 RPC profile, adapted from integrations/antigravity/launch.py."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = "antigravity/gemini-3.8-flash-tiered"


def project_instructions(cwd, mandatory):
    cwd = Path(cwd).resolve(strict=True)
    texts = []
    # Root-to-leaf order matches normal project instruction inheritance.
    for directory in reversed((cwd, *cwd.parents)):
        path = directory / "AGENTS.md"
        if path.is_file():
            texts.append(f"# {path}\n{path.read_text()}")
    if mandatory:
        texts.append("# Task mandatory instructions\n" + mandatory)
    texts.append("# Worker tool boundary\nUse built-in read, grep, find and ls within this project. "
                 "Edit or write only assigned files. Shell tools are blocked. "
                 "The runner executes acceptance commands after the turn.")
    return "\n\n".join(texts)


def prepare(task_dir, task):
    profile = task_dir / "pi-profile"
    profile.mkdir(mode=0o700, exist_ok=True)
    model = next(m for m in json.loads((ROOT / "integrations/antigravity/models.json").read_text())["models"] if m["id"] == MODEL)
    config = {"providers": {"omniroute": {"baseUrl": "http://127.0.0.1:20128/v1", "api": "openai-completions",
        "apiKey": "$OMNIROUTE_API_KEY", "authHeader": True,
        "compat": {"supportsDeveloperRole": False, "supportsStore": False, "supportsReasoningEffort": True, "maxTokensField": "max_tokens"},
        "models": [model]}}}
    (profile / "models.json").write_text(json.dumps(config))
    reserve = max(16384, model["contextWindow"] - 64000)
    (profile / "settings.json").write_text(json.dumps({"defaultProvider": "omniroute", "defaultModel": MODEL,
        "defaultThinkingLevel": "low", "enabledModels": ["omniroute/" + MODEL], "enableSkillCommands": False,
        "quietStartup": True, "packages": [], "extensions": [], "skills": [],
        "compaction": {"enabled": True, "reserveTokens": reserve, "keepRecentTokens": 12000}}))
    for path in profile.iterdir():
        path.chmod(0o600)
    instructions = task_dir / "instructions.txt"
    instructions.write_text(project_instructions(task["cwd"], task["mandatory_instructions"]))
    instructions.chmod(0o600)
    auth_path = os.environ.get("JEV_OMNIROUTE_AUTH_FILE")
    if not auth_path:
        raise FileNotFoundError("Set JEV_OMNIROUTE_AUTH_FILE to a protected file")
    auth = json.loads(Path(auth_path).read_text())
    key = auth["omniroute"]["key"]
    if not isinstance(key, str) or not key:
        raise ValueError("OmniRoute key unavailable")
    env = {k: os.environ[k] for k in ("HOME", "PATH", "TERM", "COLORTERM", "LANG", "LC_ALL", "TMPDIR", "SHELL") if k in os.environ}
    env.update(PI_CODING_AGENT_DIR=str(profile), OMNIROUTE_API_KEY=key, PI_OFFLINE="1", PI_TELEMETRY="0")
    exe = ROOT / "node_modules/.bin/pi"
    if not exe.exists():
        raise FileNotFoundError("Local pi 0.85.1 missing")
    argv = [str(exe), "--mode", "rpc", "--offline", "--no-extensions", "--no-skills",
            "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-approve",
            "--append-system-prompt", str(instructions), "--session-dir", str(task_dir / "pi-sessions"),
            "--provider", "omniroute", "--model", MODEL, "--name", "Jev " + task["task_id"]]
    allowed = []
    cwd = Path(task["cwd"]).resolve(strict=True)
    for name in task["allowed_files"]:
        target = (cwd / name).resolve()
        if not target.is_relative_to(cwd):
            raise ValueError("Allowed file escapes task cwd")
        allowed.append({"path": str(target), "directory": target.is_dir()})
    env["JEV_WORKER_CWD"] = str(cwd)
    env["JEV_WORKER_ALLOWED_FILES"] = json.dumps(allowed)
    argv.extend(["--extension", str(ROOT / "extensions/gemini-worker-guard.ts")])
    context_mode = os.environ.get("JEV_CONTEXT_MODE", "active")
    if context_mode not in ("off", "shadow", "active"):
        raise ValueError("Invalid JEV_CONTEXT_MODE")
    if context_mode != "off":
        argv.extend(["--extension", str(ROOT / "extensions/token-economy.ts")])
        env["JEV_FULL_LOG_DIR"] = str(task_dir / "full-tool-logs")
        env["JEV_TASK_GOAL"] = task["goal"].encode("utf-8")[:2000].decode("utf-8", errors="ignore")
        env["JEV_CONTEXT_MODE"] = context_mode
        env["JEV_CONTEXT_GATE"] = str(ROOT / "integrations/jev/context_gate.py")
        env["JEV_CONTEXT_CACHE_DIR"] = str(task_dir / "jev-context-cache")
        env["JEV_PROJECT_ROOT"] = str(ROOT)
    return argv, env
