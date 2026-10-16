"""Model-independent idle compaction for a privately owned Codex app-server.

Run ``python -m harness.idle_compact run -- [Codex TUI arguments]`` or use
``app-server`` as a stdio app-server launcher in a compatible client.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hmac
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid


DEFAULT_IDLE_SECONDS = 30 * 60
MAX_MESSAGE = 32 * 1024 * 1024
REQUIRED_NOTIFICATIONS = {"thread/started", "thread/status/changed", "turn/started",
                          "turn/completed", "item/started", "item/completed",
                          "serverRequest/resolved", "process/exited"}


class RPCRejected(RuntimeError):
    """The native server explicitly rejected an internal request."""


@dataclass
class ThreadState:
    status: str = "unknown"
    idle_since: float | None = None
    armed: bool = False
    eligible: bool = False


class Controller:
    """One client, one private backend, and one serialized compaction gate."""

    def __init__(self, upstream, downstream, *, idle_seconds=DEFAULT_IDLE_SECONDS,
                 timeout_seconds=600, clock=time.monotonic, report=None):
        self.upstream, self.downstream = upstream, downstream
        self.idle_seconds, self.timeout_seconds = idle_seconds, timeout_seconds
        self.clock = clock
        self.report = report or (lambda **event: None)
        self.threads: dict[str, ThreadState] = {}
        self.requests = {}
        self.internal = {}
        self.approvals = set()
        self.processes = set()
        self.initialized = False
        self.notifications_available = True
        self.gated = False
        self.queued = []
        self.queued_bytes = 0
        self.compacting = None
        self.lifecycle = None
        self.closed = False

    def busy(self):
        return bool(self.requests or self.approvals or self.processes or
                    any(s.status != "idle" for s in self.threads.values()))

    def reset_idle(self):
        for state in self.threads.values():
            if state.status == "idle" and state.armed:
                state.idle_since = self.clock()

    async def client(self, message):
        method = message.get("method")
        # Approval replies and an explicit user interrupt must never be trapped
        # behind our gate. New turns wait until the compact lifecycle finishes.
        if self.gated and method and method != "turn/interrupt":
            size = len(json.dumps(message).encode("utf-8"))
            if len(self.queued) >= 256 or self.queued_bytes + size > MAX_MESSAGE:
                raise RuntimeError("idle_compact_client_queue_full")
            self.queued.append(message)
            self.queued_bytes += size
            return
        if method:
            if isinstance(message.get("id"), str) and message["id"].startswith("jev-idle-"):
                raise ValueError("Client request ID uses the controller's reserved namespace")
            self.reset_idle()
            if "id" in message:
                self.requests[message["id"]] = message
            if method == "process/spawn":
                self.processes.add(message.get("params", {}).get("processHandle"))
            if method == "initialize":
                message = {**message, "params": {**message.get("params", {})}}
                caps = message["params"].get("capabilities") or {}
                if REQUIRED_NOTIFICATIONS.intersection(caps.get("optOutNotificationMethods") or []):
                    self.notifications_available = False
                    self.report(event="idle_timer_disabled_missing_notifications")
                # Used only for the background-terminal safety preflight.
                message["params"]["capabilities"] = {**caps, "experimentalApi": True}
        elif "id" in message:
            self.reset_idle()
        await self.upstream(message)

    async def server(self, message):
        method, params = message.get("method"), message.get("params") or {}
        if not method and message.get("id") in self.internal:
            future = self.internal.pop(message["id"])
            if not future.done():
                if "error" in message:
                    future.set_exception(RPCRejected("idle_compact_rpc_rejected"))
                else:
                    future.set_result(message.get("result", {}))
            return
        if method and "id" in message:
            self.approvals.add(message["id"])
            self.reset_idle()
        if not method and "id" in message:
            request = self.requests.pop(message["id"], {})
            name = request.get("method")
            if name == "process/spawn" and "error" in message:
                self.processes.discard(request.get("params", {}).get("processHandle"))
            result = message.get("result") or {}
            if name == "initialize" and "error" not in message:
                self.initialized = True
            if name in ("thread/start", "thread/resume", "thread/fork"):
                thread = result.get("thread")
                if thread:
                    self.observe(thread, armed=bool(thread.get("turns")), eligible=True)
        if method == "thread/started":
            self.observe(params.get("thread") or {})
        tid = params.get("threadId")
        if tid and method == "thread/status/changed":
            state = self.threads.setdefault(tid, ThreadState())
            state.status = (params.get("status") or {}).get("type", "unknown")
            if state.status != "idle":
                state.idle_since = None
            elif state.armed and state.idle_since is None:
                state.idle_since = self.clock()
        if tid and method in ("thread/closed", "thread/archived", "thread/deleted"):
            self.threads.pop(tid, None)
        if tid and method == "turn/started":
            state = self.threads.setdefault(tid, ThreadState())
            state.status, state.idle_since = "active", None
            if tid == self.compacting:
                self.lifecycle["turn_id"] = (params.get("turn") or {}).get("id")
        if tid and method == "turn/completed":
            state = self.threads.setdefault(tid, ThreadState())
            completed = (params.get("turn") or {}).get("status") == "completed"
            if tid == self.compacting:
                if (params.get("turn") or {}).get("id") == self.lifecycle.get("turn_id"):
                    self.lifecycle["turn"] = completed
                    self.lifecycle["failed"] |= not completed
                    self.check_lifecycle()
            else:
                state.armed = completed and state.eligible
                state.idle_since = self.clock() if state.armed else None
        if self.compacting and tid == self.compacting and method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            if (item.get("type") == "contextCompaction" and params.get("turnId")
                    == self.lifecycle.get("turn_id") and params.get("turnId")):
                if method == "item/started":
                    self.lifecycle["item_id"] = item.get("id")
                elif item.get("id") == self.lifecycle.get("item_id") and item.get("id"):
                    self.lifecycle["item"] = True
                self.check_lifecycle()
        if method == "serverRequest/resolved":
            self.approvals.discard(params.get("requestId"))
        if method == "process/exited":
            self.processes.discard(params.get("processHandle"))
            self.reset_idle()
        await self.downstream(message)

    def observe(self, thread, *, armed=False, eligible=False):
        if not thread.get("id"):
            return
        tid = thread["id"]
        state = self.threads.setdefault(tid, ThreadState())
        state.status = (thread.get("status") or {}).get("type", "unknown")
        state.eligible |= eligible
        state.armed |= armed
        if state.status == "idle" and state.armed and state.idle_since is None:
            state.idle_since = self.clock()

    def check_lifecycle(self):
        life = self.lifecycle
        if life and (life["failed"] or (life["item"] and life["turn"])):
            life["done"].set()

    async def rpc(self, method, params=None):
        identifier = "jev-idle-" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.internal[identifier] = future
        try:
            await self.upstream({"id": identifier, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, min(15, self.timeout_seconds))
        finally:
            self.internal.pop(identifier, None)

    async def tick(self):
        if self.closed or self.gated or not self.initialized or not self.notifications_available:
            return
        if self.busy():
            self.reset_idle()
            return
        candidate = next((tid for tid, s in self.threads.items()
                          if s.eligible and s.armed and s.idle_since is not None
                          and self.clock() - s.idle_since >= self.idle_seconds), None)
        if candidate is None:
            return
        self.gated = True
        try:
            loaded = await self.rpc("thread/loaded/list")
            ids = loaded.get("data")
            if not isinstance(ids, list) or loaded.get("nextCursor"):
                self.reset_idle()
                return
            if candidate not in ids:
                self.threads.pop(candidate, None)
                return
            # Unknown loaded threads may be nested workers. Refuse to assume
            # they are idle or to resume/subscribe/change their settings.
            if any(tid not in self.threads for tid in ids):
                self.reset_idle()
                return
            for tid in ids:
                info = await self.rpc("thread/read", {"threadId": tid, "includeTurns": False})
                if (info.get("thread", {}).get("status") or {}).get("type") != "idle":
                    self.reset_idle()
                    return
                terminals = await self.rpc("thread/backgroundTerminals/list", {"threadId": tid})
                if (not isinstance(terminals.get("data"), list) or terminals["data"]
                        or terminals.get("nextCursor")):
                    self.reset_idle()
                    return
            if self.queued or self.busy():
                self.reset_idle()
                return
            state = self.threads[candidate]
            # Consume this idle period even if the provider rejects compaction.
            state.armed, state.idle_since = False, None
            self.compacting = candidate
            self.lifecycle = {"item": False, "turn": False, "failed": False,
                              "done": asyncio.Event()}
            self.report(event="compact_started", thread_id=candidate)
            await self.rpc("thread/compact/start", {"threadId": candidate})
            await asyncio.wait_for(self.lifecycle["done"].wait(), self.timeout_seconds)
            if self.lifecycle["failed"]:
                self.report(event="compact_failed", thread_id=candidate)
            else:
                self.report(event="compact_completed", thread_id=candidate)
        except asyncio.TimeoutError:
            if not self.compacting:
                state = self.threads.get(candidate)
                if state:
                    state.armed, state.idle_since = False, None
                self.report(event="compact_preflight_unavailable", thread_id=candidate)
                return
            # An ambiguous in-flight compact cannot safely release queued turns.
            # Close only this owned transport, never interrupt another session.
            self.closed = True
            self.report(event="compact_timeout_unverified", thread_id=candidate)
            raise RuntimeError("idle_compact_lifecycle_timeout") from None
        except RPCRejected:
            if self.compacting:
                if self.lifecycle.get("turn_id"):
                    self.closed = True
                    raise RuntimeError("idle_compact_rejected_after_turn_started") from None
                self.report(event="compact_rejected", thread_id=candidate)
            else:
                self.report(event="compact_preflight_unavailable", thread_id=candidate)
            state = self.threads.get(candidate)
            if state:
                state.armed, state.idle_since = False, None
        finally:
            self.compacting, self.lifecycle, self.gated = None, None, False
            if not self.closed:
                queued, self.queued = self.queued, []
                self.queued_bytes = 0
                for message in queued:
                    await self.client(message)


async def terminate(process):
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def emit(**event):
    print(json.dumps({"component": "idle_compact", **event}), file=sys.stderr, flush=True)


async def relay(receive, send, *, executable, codex_args=(), idle_seconds=DEFAULT_IDLE_SECONDS,
                timeout_seconds=600, report=emit):
    if any(arg == "--stdio" or arg.startswith("--listen") for arg in codex_args):
        raise ValueError("The controller requires its own private stdio app-server")
    backend = await asyncio.create_subprocess_exec(
        executable, "app-server", *codex_args, "--listen", "stdio://",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, limit=MAX_MESSAGE)
    async def upstream(message):
        backend.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await backend.stdin.drain()
    controller = Controller(upstream, send, idle_seconds=idle_seconds,
                            timeout_seconds=timeout_seconds, report=report)
    async def client_loop():
        while (message := await receive()) is not None:
            await controller.client(message)
    async def server_loop():
        while line := await backend.stdout.readline():
            await controller.server(json.loads(line))
    async def timer_loop():
        while True:
            await asyncio.sleep(min(1, idle_seconds))
            await controller.tick()
    tasks = [asyncio.create_task(f()) for f in (client_loop, server_loop, timer_loop)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        controller.closed = True
        for future in controller.internal.values():
            future.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await terminate(backend)


async def stdio(args, executable):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=256)
    def reader():
        try:
            for line in sys.stdin.buffer:
                future = asyncio.run_coroutine_threadsafe(queue.put(line), loop)
                future.result()
        finally:
            with suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)
    threading.Thread(target=reader, daemon=True).start()
    async def receive():
        raw = await queue.get()
        if raw is not None and len(raw) > MAX_MESSAGE:
            raise ValueError("Client message too large")
        return json.loads(raw) if raw is not None else None
    async def send(message):
        sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    await relay(receive, send, executable=executable, codex_args=args.codex_args,
                idle_seconds=args.idle_seconds, timeout_seconds=args.compact_timeout_seconds)
    return 0


async def terminal(args, executable):
    try:
        from websockets.asyncio.server import serve
        from websockets.exceptions import ConnectionClosed
    except ImportError:
        raise ValueError("CLI mode requires: python -m pip install -r requirements-idle-compact.txt") from None
    server_args = terminal_server_args(args.codex_args)
    token, connected = secrets.token_urlsafe(32), False
    async def authenticate(connection, request):
        value = request.headers.get("Authorization", "")
        if request.headers.get("Origin") or not hmac.compare_digest(value, "Bearer " + token):
            return connection.respond(401, "Unauthorized\n")
        if connected:
            return connection.respond(409, "A client is already connected\n")
    async def handler(connection):
        nonlocal connected
        if connected:
            await connection.close(1008, "One client only")
            return
        connected = True
        async def receive():
            try:
                return json.loads(await connection.recv())
            except ConnectionClosed:
                return None
        async def send(message):
            await connection.send(json.dumps(message, ensure_ascii=False))
        try:
            await relay(receive, send, executable=executable, idle_seconds=args.idle_seconds,
                        timeout_seconds=args.compact_timeout_seconds, codex_args=server_args)
        except Exception:
            emit(event="managed_transport_failed")
            await connection.close(1011, "Managed session transport failed; resume the saved session")
    async with serve(handler, "127.0.0.1", 0, process_request=authenticate,
                     max_size=MAX_MESSAGE, compression=None) as server:
        port = server.sockets[0].getsockname()[1]
        cli = await asyncio.create_subprocess_exec(
            executable, "--remote", f"ws://127.0.0.1:{port}",
            "--remote-auth-token-env", "JEV_IDLE_COMPACT_TOKEN", *args.codex_args,
            env={**os.environ, "JEV_IDLE_COMPACT_TOKEN": token})
        try:
            return await cli.wait()
        finally:
            await terminate(cli)


def terminal_server_args(arguments):
    """Apply shared config overrides to the backend as well as the TUI."""
    result = []
    arguments = iter(arguments)
    for arg in arguments:
        if arg == "--":
            break
        if arg == "--no-daemon" or arg.startswith("--remote"):
            raise ValueError("The managed CLI owns its --remote transport")
        if arg in ("-p", "--profile", "--oss", "--local-provider") or arg.startswith(("--profile=", "--local-provider=")):
            raise ValueError("Managed CLI requires CODEX_HOME/config.toml instead of --profile/--oss/--local-provider")
        if arg.startswith("-p") and len(arg) > 2:
            raise ValueError("Managed CLI requires CODEX_HOME/config.toml instead of -p")
        if arg in ("-c", "--config", "--enable", "--disable"):
            value = next(arguments, None)
            if value is None:
                raise ValueError(f"Missing value after {arg}")
            result.extend((arg, value))
        elif arg.startswith(("--config=", "--enable=", "--disable=")) or (arg.startswith("-c") and len(arg) > 2):
            result.append(arg)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("app-server", "run"))
    parser.add_argument("--idle-seconds", type=float, default=DEFAULT_IDLE_SECONDS)
    parser.add_argument("--compact-timeout-seconds", type=float, default=600)
    parser.add_argument("--codex", default="codex")
    raw = list(sys.argv[1:] if argv is None else argv)
    split = raw.index("--") if "--" in raw else len(raw)
    args = parser.parse_args(raw[:split])
    args.codex_args = raw[split + 1:]
    for value in (args.idle_seconds, args.compact_timeout_seconds):
        if not math.isfinite(value) or value <= 0:
            parser.error("timeouts must be finite positive seconds")
    executable = shutil.which(args.codex)
    if not executable:
        parser.error("Codex CLI not found")
    try:
        return asyncio.run((terminal if args.mode == "run" else stdio)(args, executable))
    except KeyboardInterrupt:
        return 130
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (RuntimeError, OSError):
        emit(event="controller_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
