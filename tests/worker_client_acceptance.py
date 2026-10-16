"""Real Codex CLI against synthetic loopback tools/Jev; no external model calls."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "integrations/workers")]
import portable
from harness.core import _hook_entries


def stream(item, index):
    response = {"id": f"resp_{index}", "object": "response", "created_at": 1,
                "status": "completed", "model": "jev/auto", "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
              {"type": "response.output_item.added", "output_index": 0, "item": item},
              {"type": "response.output_item.done", "output_index": 0, "item": item},
              {"type": "response.completed", "response": response}]
    return b"".join(("event: " + event["type"] + "\ndata: " +
                     json.dumps({**event, "sequence_number": i}) + "\n\n").encode()
                    for i, event in enumerate(events))


def run_case(kind, executable, catalog):
    requests = []
    reviews = []
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/ask":
                reviews.append(body)
                data = json.dumps({"answers": {name: {"choice": "deny" if kind == "jev_deny" else "allow",
                                                           "confidence": 0.99}
                                               for name in body["questions"]}}).encode()
                content_type = "application/json"
            else:
                requests.append(body)
                index = len(requests)
                if index == 1:
                    name = "outside.txt" if kind == "outside" else "owned.txt"
                    patch = f"*** Begin Patch\n*** Add File: {name}\n+JEV_WORKER_OK\n*** End Patch"
                    flat = [child for t in body["tools"] for child in t.get("tools", [t])]
                    tool = next((t for t in flat if t.get("name") == "apply_patch"), None)
                    if tool is None:
                        self.send_error(400, "apply_patch not exposed: " + json.dumps([
                            {"type": t.get("type"), "name": t.get("name"), "keys": list(t)} for t in body["tools"]]))
                        return
                    item = {"id": "tool_fixture", "call_id": "call_fixture", "name": "apply_patch",
                            "type": "custom_tool_call", "input": patch}
                    if tool["type"] == "function":
                        item = {"id": "tool_fixture", "call_id": "call_fixture", "name": "apply_patch",
                                "type": "function_call", "arguments": json.dumps({"command": patch})}
                else:
                    item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "JEV_WORKER_DONE", "annotations": []}]}
                data = stream(item, index)
                content_type = "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        fixture_root = Path(os.environ.get("JEV_WORKER_FIXTURE_ROOT", str(ROOT.parent)))
        with tempfile.TemporaryDirectory(prefix="jev-worker-client-", dir=fixture_root) as temporary:
            root = Path(temporary).resolve()
            home, cwd, state = root / "profile", root / "work", root / "workers"
            home.mkdir(); cwd.mkdir()
            catalog_path = home / "fixture-models.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            config = ('model = "jev/auto"\nmodel_provider = "fixture"\n'
                      f'model_catalog_json = {json.dumps(str(catalog_path))}\n'
                      '[model_providers.fixture]\nname = "Local fixture"\nwire_api = "responses"\n'
                      f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                      'env_key = "JEV_FIXTURE_TOKEN"\nrequires_openai_auth = false\n'
                      f'[projects.{json.dumps(str(cwd))}]\ntrust_level = "trusted"\n')
            (home / "config.toml").write_text(config, encoding="utf-8")
            (home / "hooks.json").write_text(json.dumps({"hooks": _hook_entries(ROOT)}), encoding="utf-8")
            key = home / "codex-router/generic-provider-credentials/jev.key"
            key.parent.mkdir(parents=True)
            key.write_text("synthetic", encoding="utf-8"); key.chmod(0o600)
            env = {"CODEX_HOME": str(home), "JEV_WORKER_STATE_DIR": str(state),
                   "JEV_GLOBAL_STATE_DIR": str(root / "hook-state"),
                   "JEV_GLOBAL_ASK_URL": f"http://127.0.0.1:{server.server_port}/ask", "JEV_FIXTURE_TOKEN": "synthetic"}
            with mock.patch.dict(os.environ, env):
                with mock.patch.object(portable.subprocess, "Popen"):
                    task_id = portable.submit({"request_id": kind, "goal": "Create owned.txt with JEV_WORKER_OK",
                                               "cwd": str(cwd), "allowed_files": ["owned.txt"],
                                               "budget": {"max_seconds": 60}})["task_id"]
                real_popen = subprocess.Popen
                stderr = root / "client-stderr.log"
                def launch(command, **kwargs):
                    # Only this isolated fixture trusts its generated, reviewed
                    # hooks automatically. Production workers never bypass trust.
                    command = [*command[:-1], "--dangerously-bypass-hook-trust", command[-1]]
                    with stderr.open("wb") as output:
                        return real_popen(command, **{**kwargs, "stderr": output})
                with mock.patch.object(portable.subprocess, "Popen", side_effect=launch):
                    portable._run(task_id)
                result = portable.collect(task_id)
                edited = (cwd / "owned.txt").exists()
                escaped = (cwd / "outside.txt").exists()
                success = (result["state"] == "completed" and edited and not escaped and bool(reviews)
                           if kind == "owned" else result["state"] == "failed" and not edited and not escaped
                           and result.get("reason") == "worker_tool_denied" and len(requests) >= 2
                           and result.get("guard_observed") is True)
                diagnostic = {}
                if not success and kind == "owned":
                    probe = subprocess.run([executable, "sandbox", "--cd", str(cwd),
                                            "--permission-profile", ":workspace", sys.executable, "-c",
                                            "from pathlib import Path; Path('sandbox-probe.txt').write_text('ok')"],
                                           capture_output=True, encoding="utf-8", errors="replace", timeout=30)
                    diagnostic = {"sandbox_probe": {"exit_code": probe.returncode, "stderr": probe.stderr[-3000:]}}
                return {"case": kind, "ok": success, "state": result["state"], "reason": result.get("reason"),
                        "client_requests": len(requests), "jev_reviews": len(reviews),
                        "guard_observed": result.get("guard_observed", False),
                        **diagnostic,
                        **({"client_error": stderr.read_text(encoding="utf-8", errors="replace")[-2000:],
                            "client_events": (state / task_id / "events.jsonl").read_text(encoding="utf-8", errors="replace")[-3000:]} if not success else {})}
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    executable = shutil.which("codex")
    if not executable:
        raise SystemExit("Codex CLI is required")
    bundled = json.loads(subprocess.check_output([executable, "debug", "models", "--bundled"], encoding="utf-8"))
    template = next(model for model in bundled["models"] if model.get("apply_patch_tool_type") == "freeform")
    template.pop("tool_mode", None)
    catalog = {"models": [{**template, "slug": "jev/auto", "display_name": "Synthetic Jev worker",
                           "apply_patch_tool_type": "freeform", "use_responses_lite": False}]}
    results = [run_case(case, executable, catalog) for case in ("owned", "outside", "jev_deny")]
    print(json.dumps({"ok": all(row["ok"] for row in results), "cases": results}, indent=2))
    raise SystemExit(0 if all(row["ok"] for row in results) else 1)
