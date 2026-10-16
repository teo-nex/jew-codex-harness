"""Keep a validated, buffered Responses stream alive without exposing actions."""

import json
import time
import threading
import uuid


class ValidatedStream:
    def __init__(self, handler, model):
        self.handler = handler
        self.lock = threading.RLock()
        self.response_id = "resp_jev_" + uuid.uuid4().hex
        self.sequence = 0
        self.finished = False
        self.last_write = time.monotonic()
        self.response = {"id": self.response_id, "object": "response",
                         "created_at": int(time.time()), "model": model,
                         "status": "in_progress", "output": []}
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Connection", "close")
        handler.end_headers()
        self.event({"type": "response.created", "response": self.response})

    def event(self, value):
        with self.lock:
            value = dict(value)
            response = value.get("response")
            if isinstance(response, dict):
                value["response"] = {**response, "id": self.response_id}
            if "response_id" in value:
                value["response_id"] = self.response_id
            value["sequence_number"] = self.sequence
            self.sequence += 1
            self.write(("event: " + value["type"] + "\ndata: " +
                        json.dumps(value, ensure_ascii=False) + "\n\n").encode())

    def write(self, data):
        with self.lock:
            self.handler.wfile.write(data)
            self.handler.wfile.flush()
            self.last_write = time.monotonic()

    def heartbeat(self):
        with self.lock:
            if not self.finished and time.monotonic() - self.last_write >= 10:
                self.write(b": validating upstream response\n\n")

    def finish(self, data):
        with self.lock:
            # Only fully validated/normalized output reaches this method. The
            # lifecycle envelope is ours, including across safe provider fallback.
            for block in data.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
                lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
                if not lines:
                    continue
                raw = "\n".join(lines)
                if raw == "[DONE]":
                    continue
                value = json.loads(raw)
                if value.get("type") != "response.created":
                    self.event(value)
            self.write(b"data: [DONE]\n\n")
            self.finished = True

    def fail(self, message, code="upstream_response_failed"):
        with self.lock:
            if self.finished:
                return
            self.event({"type": "response.failed", "response": {
                **self.response, "status": "failed",
                "error": {"code": code, "message": message}}})
            self.write(b"data: [DONE]\n\n")
            self.finished = True
