"""Buffered validation must not cause header timeouts, replay loops or orphan work."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import jev_server as jev
from provider_ladder import Ladder
from request_budget import RequestBudget, ExhaustedRequests
from validated_stream import ValidatedStream


def frame(kind, **fields):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


def events(raw):
    return [json.loads(line[5:]) for line in raw.decode().splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


@contextlib.contextmanager
def serving(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class ValidatedStreamTests(unittest.TestCase):
    def test_envelope_is_stable_across_fallback_and_sequence_is_monotonic(self):
        h = object.__new__(jev.Handler)
        h.wfile = io.BytesIO()
        h.send_response = lambda *_: None
        h.send_header = lambda *_: None
        h.end_headers = lambda: None
        relay = ValidatedStream(h, "gateway/model")
        relay.finish(frame("response.created", response={"id": "provider-response"}) +
                     frame("response.output_text.delta", delta="exact result") +
                     frame("response.completed", response={"id": "different-final-id", "status": "completed", "output": []}))
        result = events(h.wfile.getvalue())
        self.assertEqual([v["type"] for v in result], ["response.created", "response.output_text.delta", "response.completed"])
        self.assertEqual([v["sequence_number"] for v in result], [0, 1, 2])
        self.assertEqual(result[0]["response"]["id"], result[-1]["response"]["id"])
        self.assertEqual(result[1]["delta"], "exact result")

    def test_buffered_external_failure_can_fall_back_to_native_without_second_headers(self):
        from recovery_acceptance import response_stream
        bad = frame("error", error={"message": "synthetic failure"})
        bodies = iter([bad, response_stream()])
        class Response:
            status = 200
            def __init__(self): self.body = next(bodies)
            def getheader(self, _): return "text/event-stream"
            def read(self, *_): return self.body
        class Connection:
            def __init__(self, *a, **kw): pass
            def request(self, *a, **kw): pass
            def getresponse(self): return Response()
            def close(self): pass
        h = object.__new__(jev.Handler)
        h.wfile = io.BytesIO()
        h._response_started = False
        sent = []
        h.send_response = sent.append
        h.send_header = lambda *_: None
        h.end_headers = lambda: None
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder({"providers": [{"id": "external", "model": "gateway/model"},
                                            {"id": "native", "transport": "native"}]}, Path(directory) / "state.json")
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="fixture"), \
                 mock.patch.object(jev, "caller_secret", return_value="fixture"), \
                 mock.patch.object(jev.http.client, "HTTPConnection", Connection), \
                 mock.patch.object(jev, "log_line"):
                h._serve_ladder({"model": "jev/auto", "input": "fixture", "stream": True}, "scope", jev.SOL61,
                                "high", None, {"step_type": "user_turn"}, None, True, False)
        result = events(h.wfile.getvalue())
        self.assertEqual(sent, [200])
        self.assertEqual([e["type"] for e in result].count("response.created"), 1)
        self.assertEqual(result[-1]["type"], "response.completed")
        self.assertEqual(result[0]["response"]["id"], result[-1]["response"]["id"])
        self.assertEqual(len(h._attempts), 2)
        self.assertNotIn(b"synthetic failure", h.wfile.getvalue())

    def exercise(self, mode):
        generated = threading.Event()
        stopped = threading.Event()
        finished = threading.Event()
        attempts = []
        class Provider(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_): pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(frame("response.created", response={"id": "upstream", "status": "in_progress"}))
                    self.wfile.flush()
                    for _ in range(8 if mode == "complete" else 200):
                        self.wfile.write(frame("response.reasoning_text.delta", delta="thinking"))
                        self.wfile.flush()
                        generated.set()
                        time.sleep(0.8 if mode == "disconnect_idle" else 0.04)
                    self.wfile.write(frame("response.completed", response={"id": "upstream", "status": "completed", "output": []}))
                    self.wfile.flush()
                except OSError:
                    stopped.set()
        with tempfile.TemporaryDirectory() as directory, serving(Provider) as provider_port:
            ladder = Ladder({"providers": [{"id": "fixture", "model": "gateway/model"}]}, Path(directory) / "state.json")
            class Relay(jev.Handler):
                def log_message(self, *_): pass
                def _post(self):
                    payload = self._body(jev.RESPONSE_MAX_BYTES)
                    self._budget = RequestBudget({"total_seconds": 0.22 if mode in ("budget", "cli_budget") else 3,
                                                  "first_token_seconds": 2, "idle_seconds": 2})
                    if not self._admit_replay(payload):
                        return
                    self._policy_ladder = ladder
                    try:
                        self._serve_ladder(payload, "isolated-scope", jev.SOL61, "high", None,
                                           {"step_type": "user_turn"}, None, True, False)
                    finally:
                        attempts.extend(self._attempts)
                        finished.set()
            with mock.patch.object(jev, "local_secret", return_value="fixture"), \
                 mock.patch.object(jev, "_omniroute_key", return_value="fixture"), \
                 mock.patch.object(jev, "log_line"), \
                 mock.patch.object(jev, "_exhausted_requests", ExhaustedRequests()), \
                 mock.patch.dict(jev.os.environ, {"JEV_OMNIROUTE_PORT": str(provider_port)}), serving(Relay) as relay_port:
                if mode == "cli_budget":
                    home = Path(directory) / "profile"
                    home.mkdir()
                    (home / "config.toml").write_text(
                        'model="fixture"\nmodel_provider="fixture"\n'
                        '[model_providers.fixture]\nname="Fixture"\nwire_api="responses"\n'
                        f'base_url="http://127.0.0.1:{relay_port}/v1"\n'
                        'env_key="FIXTURE_TOKEN"\nrequires_openai_auth=false\n')
                    env = {k: v for k, v in os.environ.items() if k.upper() in
                           ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TMP", "TEMP", "TMPDIR")}
                    env.update({"CODEX_HOME": str(home), "FIXTURE_TOKEN": "fixture"})
                    result = subprocess.run([shutil.which("codex"), "exec", "--ephemeral", "--json",
                                             "--sandbox", "read-only", "--skip-git-repo-check",
                                             "-c", 'approval_policy="never"', "No tools. Reply with a brief acknowledgement."],
                                            cwd=directory, env=env, capture_output=True, text=True, timeout=20)
                    self.assertTrue(finished.wait(1), (result.stdout + result.stderr)[-2500:])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("budget exhausted", result.stdout + result.stderr)
                    self.assertEqual(len(attempts), 1, "a real Codex client must not reconnect/replay this terminal failure")
                    return
                client = http.client.HTTPConnection("127.0.0.1", relay_port, timeout=0.2)
                started = time.monotonic()
                client.request("POST", "/v1/responses", body=json.dumps({"model": "jev/auto", "input": "fixture", "stream": True}),
                               headers={"Authorization": "Bearer fixture", "Content-Type": "application/json"})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertLess(time.monotonic() - started, 0.2, "headers arrive before the provider finishes")
                # The lifecycle event must also arrive early: LiteLLM buffers
                # the first real SSE event, so comments/headers alone do not fix this.
                prologue = b""
                while not prologue.endswith(b"\n\n"):
                    prologue += response.read(1)
                self.assertEqual(events(prologue)[0]["type"], "response.created")
                self.assertTrue(generated.wait(1))
                if mode.startswith("disconnect"):
                    # Drop the TCP connection, as a canceled downstream proxy does.
                    response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                    response.close()
                    client.close()
                    self.assertTrue(finished.wait(1.5), "the canceled request must not run until its total deadline")
                    self.assertTrue(stopped.wait(2.5), "disconnect reaches the upstream provider")
                    self.assertEqual(attempts[-1]["completion"], "client_disconnected")
                else:
                    response.fp.raw._sock.settimeout(2)
                    body = prologue + response.read()
                    self.assertTrue(finished.wait(1))
                    result = events(body)
                    if mode == "complete":
                        self.assertEqual(result[-1]["type"], "response.completed")
                        self.assertEqual(attempts[-1]["status"], 200)
                        self.assertGreater(attempts[-1]["total_ms"], 200)
                    else:
                        self.assertEqual(result[-1]["type"], "response.failed")
                        self.assertEqual(result[-1]["response"]["error"]["code"], "request_budget_exhausted")
                        self.assertEqual(len(attempts), 1, "exhaustion never replays the provider")
                        self.assertEqual(attempts[-1]["failure_class"], "budget_exhausted")
                        self.assertNotIn("response.reasoning_text.delta", [e["type"] for e in result])
                    client.close()

    def test_active_generation_outlives_downstream_header_wait(self):
        self.exercise("complete")

    def test_budget_exhaustion_is_terminal_without_exposing_actions(self):
        self.exercise("budget")

    def test_cancel_interrupts_buffered_upstream_generation(self):
        self.exercise("disconnect")

    def test_cancel_interrupts_silent_upstream_read(self):
        self.exercise("disconnect_idle")

    def test_exhausted_replays_are_private_bounded_and_do_not_block_changed_requests(self):
        guard = ExhaustedRequests(ttl=2, capacity=2)
        first = guard.key({"input": "private fixture", "model": "a"})
        changed = guard.key({"input": "private fixture", "model": "b"})
        with mock.patch("request_budget.time.monotonic", return_value=10) as now:
            guard.remember(first)
            self.assertTrue(guard.contains(first))
            self.assertFalse(guard.contains(changed))
            self.assertNotIn("private fixture", repr(guard.entries))
            now.return_value = 13
            self.assertFalse(guard.contains(first))
            for i in range(3):
                guard.remember(guard.key({"input": str(i)}))
            self.assertEqual(len(guard.entries), 2)
            self.assertFalse(guard.contains(guard.key({"input": "0"})))

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI not installed")
    def test_real_codex_does_not_retry_terminal_budget_failure(self):
        self.exercise("cli_budget")

    def test_unstreamed_budget_exhaustion_is_not_a_retryable_http_status(self):
        h = object.__new__(jev.Handler)
        h.wfile = io.BytesIO()
        statuses = []
        h.send_response = statuses.append
        h.send_header = lambda *_: None
        h.end_headers = lambda: None
        with tempfile.TemporaryDirectory() as directory:
            ladder = Ladder({"providers": [{"id": "fixture", "model": "gateway/model"}]}, Path(directory) / "state.json")
            def forward(*args, **kwargs):
                h._attempts.append({"model": "gateway/model", "status": 504, "timeout_phase": "total"})
                return 504, "json", "application/json", False, b'{"error":{"message":"budget"}}', None
            h._forward = forward
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="fixture"), \
                 mock.patch.object(jev, "remember_route_lease") as remember, mock.patch.object(jev, "log_line"):
                h._serve_ladder({"model": "jev/auto", "input": "fixture"}, "scope", jev.SOL61,
                                "high", None, {"step_type": "user_turn"}, None, False, False)
            self.assertEqual(statuses, [400])
            self.assertEqual(json.loads(h.wfile.getvalue())["error"]["code"], "request_budget_exhausted")
            self.assertNotEqual(remember.call_args.args[4], 200)
