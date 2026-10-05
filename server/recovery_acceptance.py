"""Fresh real Codex client against owned synthetic loopback faults, never live providers."""

from contextlib import ExitStack
import hashlib
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.process import run_bounded

import jev_server as jev
from provider_ladder import Ladder


MARKER = "JEV_RECOVERY_ACCEPTED"


def response_stream():
    item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": MARKER, "annotations": []}]}
    response = {"id": "resp_fixture", "object": "response", "created_at": 1,
                "status": "completed", "model": "fixture/model-b", "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
              {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
              {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
               "item_id": item["id"], "part": {"type": "output_text", "text": "", "annotations": []}},
              {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
               "item_id": item["id"], "delta": MARKER},
              {"type": "response.output_text.done", "output_index": 0, "content_index": 0,
               "item_id": item["id"], "text": MARKER},
              {"type": "response.output_item.done", "output_index": 0, "item": item},
              {"type": "response.completed", "response": response}]
    return b"".join(("event: " + e["type"] + "\ndata: " + json.dumps({**e, "sequence_number": i}) + "\n\n").encode()
                    for i, e in enumerate(events))


def run(executable=None):
    codex = executable or shutil.which("codex")
    if not codex:
        return {"ok": False, "status": "blocked", "reason": "codex executable unavailable"}
    requests, records, servers, threads = [], [], [], []
    logged = threading.Event()
    def record(row):
        records.append(row)
        logged.set()
    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body.get("model"))
            failing = body.get("model") == "fixture/model-a"
            data = b'{"error":{"message":"synthetic outage"}}' if failing else response_stream()
            self.send_response(503 if failing else 200)
            self.send_header("Content-Type", "application/json" if failing else "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    try:
        with tempfile.TemporaryDirectory(prefix="jev-recovery-acceptance-") as directory, ExitStack() as stack:
            root = Path(directory)
            home, work, state = root / "profile", root / "work", root / "state"
            for path in (home, work, state):
                path.mkdir(mode=0o700)
            gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
            servers.append(gateway)
            adapter = ThreadingHTTPServer(("127.0.0.1", 0), jev.Handler)
            servers.append(adapter)
            for server in servers:
                server.daemon_threads = True
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                threads.append(thread)
            config = {"providers": [{"id": "down", "model": "fixture/model-a"},
                                     {"id": "healthy", "model": "fixture/model-b"}]}
            ladder = Ladder(config, state / "ladder-state.json")
            stack.enter_context(mock.patch.dict(os.environ, {"JEV_LADDER_MODE": "active",
                                                            "JEV_OMNIROUTE_PORT": str(gateway.server_port),
                                                            "JEV_OMNIROUTE_HOST": "127.0.0.1"}))
            for name in ("OFF_PATH", "SHADOW_PATH", "DEBUG_PATH", "SIGNATURE_PATH", "DRY_STATE_PATH", "DRY_MANUAL_PATH"):
                stack.enter_context(mock.patch.object(jev, name, str(state / name)))
            stack.enter_context(mock.patch.object(jev, "STATE", str(state)))
            stack.enter_context(mock.patch.object(jev, "_provider_ladder", return_value=ladder))
            stack.enter_context(mock.patch.object(jev, "_omniroute_key", return_value="fixture"))
            stack.enter_context(mock.patch.object(jev, "local_secret", return_value="fixture"))
            stack.enter_context(mock.patch.object(jev, "load_key", return_value=None))
            stack.enter_context(mock.patch.object(jev, "log_line", side_effect=record))
            content = ('model = "jev/auto"\nmodel_provider = "fixture"\n'
                       '[model_providers.fixture]\nname = "Synthetic recovery"\nwire_api = "responses"\n'
                       f'base_url = "http://127.0.0.1:{adapter.server_port}/v1"\n'
                       'env_key = "JEV_FIXTURE_TOKEN"\nrequires_openai_auth = false\n')
            (home / "config.toml").write_text(content)
            (home / "config.toml").chmod(0o600)
            env = {key: value for key, value in os.environ.items() if key.upper() in
                   ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TMP", "TEMP", "TMPDIR")}
            env.update({"HOME": str(home), "CODEX_HOME": str(home), "JEV_FIXTURE_TOKEN": "fixture",
                        "USERPROFILE": str(home), "APPDATA": str(home), "LOCALAPPDATA": str(home),
                        "CODEX_ROUTER_STATE_DIR": str(state), "MODEL_ROUTER_STATE_DIR": str(state)})
            command = [codex, "exec", "-m", "jev/auto", "--ephemeral", "--json", "--sandbox", "read-only",
                       "--skip-git-repo-check", "-c", "features.multi_agent=false",
                       "-c", 'approval_policy="never"',
                       "-c", "features.multi_agent_v2=false", "-c", "agents.enabled=false", "-C", str(work),
                       "Synthetic connectivity check. No tools. Reply exactly " + MARKER]
            completed = run_bounded(command, cwd=work, env=env, timeout=120)
            logged.wait(2)
            terminal = False
            thread_id = None
            texts = []
            for line in completed.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "turn.completed":
                    terminal = event.get("turn", {}).get("status", "completed") == "completed"
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                item = event.get("item") or {}
                if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                    texts.append(item.get("text", ""))
            attempts = records[-1].get("attempts", []) if records else []
            scope_correlated = bool(isinstance(thread_id, str) and records and
                                    records[-1].get("cache_scope") ==
                                    hashlib.sha256(("prompt:" + thread_id).encode()).hexdigest()[:16])
            ok = (completed.returncode == 0 and terminal and "\n".join(texts).strip() == MARKER
                  and scope_correlated
                  and requests == ["fixture/model-a", "fixture/model-b"]
                  and [a.get("http_status") for a in attempts] == [503, 200]
                  and attempts[-1].get("terminal_type") == "response.completed")
            return {"ok": ok, "status": "passed" if ok else "failed", "fresh_client": True,
                    "client_exit_code": completed.returncode,
                    "provider_requests": len(requests), "attempt_statuses": [a.get("http_status") for a in attempts],
                    "marker_accepted": terminal and "\n".join(texts).strip() == MARKER,
                    "fresh_client_scope_correlated": scope_correlated,
                    "live_provider_proof": False, "reasoning_enforcement": "unknown"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "failed", "reason": "synthetic client timed out after 120 seconds",
                "provider_requests": len(requests), "live_provider_proof": False}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"ok": False, "status": "failed", "reason": "synthetic fresh-client recovery failed"}
    finally:
        for index, server in enumerate(servers):
            if index < len(threads):
                server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(2)


if __name__ == "__main__":
    report = run(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(report))
    raise SystemExit(0 if report["ok"] else 2)
