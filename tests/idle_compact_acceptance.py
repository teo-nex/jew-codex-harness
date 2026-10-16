"""Native Codex lifecycle against a synthetic loopback provider, no credentials."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.idle_compact import relay


def response_stream(text, index, model):
    item = {"id": f"msg_{index}", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}
    response = {"id": f"resp_{index}", "object": "response", "created_at": 1,
                "status": "completed", "model": model, "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
              {"type": "response.output_item.added", "output_index": 0, "item": item},
              {"type": "response.output_item.done", "output_index": 0, "item": item},
              {"type": "response.completed", "response": response}]
    return b"".join(("event: " + e["type"] + "\ndata: " + json.dumps({**e, "sequence_number": i})
                     + "\n\n").encode() for i, e in enumerate(events))


async def exercise(executable, work, model, requests=None):
    incoming = asyncio.Queue()
    pending, notifications, reports = {}, [], []
    compact_done, turn_done = asyncio.Event(), asyncio.Event()
    async def send(message):
        if "method" in message:
            notifications.append(message)
            if message["method"] == "turn/completed":
                turn_done.set()
        elif message.get("id") in pending:
            pending.pop(message["id"]).set_result(message)
    def report(**event):
        reports.append(event)
        if event["event"] in ("compact_completed", "compact_failed", "compact_preflight_unavailable"):
            compact_done.set()
    task = asyncio.create_task(relay(incoming.get, send, executable=executable,
                                    idle_seconds=1, timeout_seconds=15, report=report))
    async def rpc(method, params):
        identifier = len(notifications) + len(pending) + 1
        future = asyncio.get_running_loop().create_future()
        pending[identifier] = future
        await incoming.put({"id": identifier, "method": method, "params": params})
        done, _ = await asyncio.wait([future, task], timeout=20, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            task.result()
            raise RuntimeError("Native transport ended unexpectedly")
        if future not in done:
            raise TimeoutError(method)
        message = future.result()
        if "error" in message:
            raise RuntimeError(message["error"])
        return message.get("result", {})
    try:
        await rpc("initialize", {"clientInfo": {"name": "idle-fixture", "version": "1"}})
        await incoming.put({"method": "initialized"})
        result = await rpc("thread/start", {"cwd": str(work), "model": model,
                                           "approvalPolicy": "never", "sandbox": "read-only"})
        tid = result["thread"]["id"]
        await rpc("turn/start", {"threadId": tid, "input": [{"type": "text", "text": "Remember IDLE_FACT_42"}]})
        await asyncio.wait_for(turn_done.wait(), 20)
        await asyncio.wait_for(compact_done.wait(), 25)
        if reports[-1]["event"] != "compact_completed":
            raise AssertionError(reports)
        turn_done.clear()
        await rpc("turn/start", {"threadId": tid, "input": [{"type": "text", "text": "Return the remembered fact"}]})
        await asyncio.wait_for(turn_done.wait(), 20)
        # Ack isn't acceptance: inspect the native item and lifecycle, then
        # prove the same thread can perform its next normal turn.
        compact_items = [n for n in notifications if n["method"] == "item/completed"
                         and n["params"]["item"]["type"] == "contextCompaction"]
        assert len(compact_items) == 1, compact_items
        assert compact_items[0]["params"]["threadId"] == tid
        completed = [n for n in notifications if n["method"] == "turn/completed"]
        assert len(completed) == 3, completed
        assert all(n["params"]["turn"]["status"] == "completed" for n in completed)
        return {"model": model, "ok": True, "native_compact_items": len(compact_items),
                "completed_turns": len(completed)}
    except Exception:
        print(json.dumps({"reports": reports, "events": notifications[-15:]}, default=str), file=sys.stderr)
        raise
    finally:
        await incoming.put(None)
        try:
            await asyncio.wait_for(task, 10)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def exercise_tui(executable, work, model, requests):
    """Drive the actual terminal client, not a replacement websocket client."""
    import fcntl
    import pty
    import struct
    import termios
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    os.set_blocking(master, False)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "harness.idle_compact", "run", "--idle-seconds", "2",
        "--codex", executable, "--", "-C", str(work), "-m", model, "--no-alt-screen",
        "Remember IDLE_FACT_42", cwd=ROOT, stdin=slave, stdout=slave, stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"})
    os.close(slave)
    output = bytearray()
    returned = False
    return_offset = None
    try:
        deadline = asyncio.get_running_loop().time() + 40
        while asyncio.get_running_loop().time() < deadline:
            try:
                chunk = os.read(master, 65536)
                output.extend(chunk)
                if b"\x1b[6n" in chunk:
                    os.write(master, b"\x1b[1;1R")
                if b'"event": "compact_completed"' in output and not returned:
                    return_offset = len(output)
                    # A burst containing text+Enter may be treated as a paste
                    # by the real TUI. Submit Enter as a separate key event.
                    os.write(master, b"Return the remembered fact")
                    await asyncio.sleep(0.2)
                    os.write(master, b"\r")
                    returned = True
                if returned and b"IDLE_FACT_42" in output[return_offset:]:
                    return {"model": model, "ok": True, "native_tui": True}
            except BlockingIOError:
                pass
            except OSError:
                break
            if process.returncode is not None:
                break
            await asyncio.sleep(0.05)
        raise AssertionError(output.decode(errors="replace")[-12000:])
    finally:
        # The real TUI consumes Ctrl-C; the wrapper then reaps its own backend.
        if process.returncode is None:
            os.write(master, b"\x03")
            await asyncio.sleep(0.1)
            os.write(master, b"\x03")
        try:
            await asyncio.wait_for(process.wait(), 10)
        except asyncio.TimeoutError:
            process.terminate()
            await process.wait()
        os.close(master)


def run_case(executable, template, model, exercise_case=exercise):
    requests = []
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            data = response_stream("IDLE_FACT_42", len(requests), model)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="idle-native-", dir=ROOT.parent) as temporary:
            root = Path(temporary).resolve()
            home, work = root / "profile", root / "work"
            home.mkdir(); work.mkdir()
            catalog = home / "models.json"
            catalog.write_text(json.dumps({"models": [{**template, "slug": model, "display_name": model,
                                                       "use_responses_lite": False}]}), encoding="utf-8")
            (home / "config.toml").write_text(
                f'model = {json.dumps(model)}\nmodel_provider = "fixture"\n'
                f'model_catalog_json = {json.dumps(str(catalog))}\n'
                '[model_providers.fixture]\nname = "Synthetic idle fixture"\nwire_api = "responses"\n'
                f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                'env_key = "JEV_IDLE_FIXTURE_KEY"\nrequires_openai_auth = false\n'
                f'[projects.{json.dumps(str(work))}]\ntrust_level = "trusted"\n', encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(home), "JEV_IDLE_FIXTURE_KEY": "synthetic"}):
                result = asyncio.run(exercise_case(executable, work, model, requests))
            if exercise_case is exercise:
                assert len(requests) == 3, len(requests)
            # The interactive client may additionally request a session title.
            # Inspect the actual follow-up prompt, not an incidental title call.
            followups = [r for r in requests if "Return the remembered fact" in json.dumps(r.get("input"))]
            assert followups, "No provider input for the actual follow-up turn"
            assert "IDLE_FACT_42" in json.dumps(followups[0].get("input")), "Fact absent after compaction"
            return {**result, "provider_requests": len(requests), "fact_carried": True}
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    executable = shutil.which("codex")
    if not executable:
        raise SystemExit("Codex CLI required")
    bundled = json.loads(subprocess.check_output([executable, "debug", "models", "--bundled"], encoding="utf-8"))
    template = next(m for m in bundled["models"] if m.get("apply_patch_tool_type") == "freeform")
    template.pop("tool_mode", None)
    exercise_case = exercise_tui if "--tui" in sys.argv else exercise
    results = [run_case(executable, template, model, exercise_case) for model in ("manual-fixture", "jev/auto")]
    print(json.dumps({"ok": True, "cases": results}, indent=2))
