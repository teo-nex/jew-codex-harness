#!/usr/bin/env python3
"""Exercise real Node configuration in an isolated Codex profile, without OAuth."""

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile


def main():
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="jev-config-smoke-") as directory:
        home = Path(directory) / "codex"
        state = home / "codex-router"
        state.mkdir(parents=True)
        env = {**os.environ, "CODEX_HOME": str(home), "CODEX_ROUTER_STATE_DIR": str(state)}
        steps = [
            ("provider_add", ["node", str(repo / "router/src/providers.mjs"), "generic", "add", "jev",
                              "--name", "Jev Router", "--base-url", "http://127.0.0.1:4330/v1",
                              "--adapter", "openai-responses", "--allow-private"]),
            ("provider_enable", ["node", str(repo / "router/src/providers.mjs"), "generic", "enable", "jev"]),
            ("model", ["node", str(repo / "server/configure-model.mjs")]),
            ("auth", ["node", str(repo / "server/configure-auth.mjs")]),
            ("catalog", ["node", str(repo / "router/src/refresh-catalog.mjs")]),
            ("picker", ["node", str(repo / "router/src/control.mjs"), "picker", "set", "jev/auto", "show"]),
        ]
        completed = []
        for name, command in steps:
            result = subprocess.run(command, cwd=repo, env=env, capture_output=True, timeout=45)
            if result.returncode:
                detail = (result.stderr or result.stdout).decode(errors="replace").strip()[-1200:]
                raise RuntimeError(f"offline configuration step {name} exited {result.returncode}: {detail}")
            completed.append(name)
        models = json.loads((state / "user-models.json").read_text())
        if sum(model.get("slug") == "jev/auto" for model in models.get("models", [])) != 1:
            raise RuntimeError("jev/auto model was not registered exactly once")
        key = state / "generic-provider-credentials/jev.key"
        if not key.is_file() or not key.read_text().strip():
            raise RuntimeError("local Jev transport key is absent")
        if os.name != "nt" and stat.S_IMODE(key.stat().st_mode) != 0o600:
            raise RuntimeError("local Jev transport key is not private")
        print(json.dumps({"ok": True, "steps": completed, "isolated": True}))


if __name__ == "__main__":
    main()
