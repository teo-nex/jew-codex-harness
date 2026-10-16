"""Monotonic request deadlines and interruptible upstream socket waits."""

import math
import hashlib
import json
from collections import OrderedDict
import socket
import select
import threading
import time


DEFAULTS = {"connect_seconds": 15, "first_token_seconds": 60, "idle_seconds": 30,
            "total_seconds": 600, "max_attempts": 8}


def validate_budget(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("Invalid request_budget fields")
    result = {**DEFAULTS, **value}
    for key, number in result.items():
        if key == "max_attempts":
            if type(number) is not int or not 1 <= number <= 1024:
                raise ValueError("max_attempts must be an integer from 1 to 1024")
        elif type(number) not in (int, float) or not math.isfinite(number) or not 0.05 <= number <= 900:
            raise ValueError("Request time limits must be finite seconds from 0.05 to 900")
    return result


class BudgetExceeded(TimeoutError):
    def __init__(self, phase):
        self.phase = phase
        super().__init__("request budget exhausted: " + phase)


class ClientDisconnected(ConnectionError):
    pass


class RequestBudget:
    def __init__(self, limits=None, started=None):
        self.limits = validate_budget(limits or {})
        self.started = time.monotonic() if started is None else started
        self.deadline = self.started + self.limits["total_seconds"]
        self.attempts = 0

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def claim(self):
        if not self.remaining():
            raise BudgetExceeded("total")
        if self.attempts >= self.limits["max_attempts"]:
            raise BudgetExceeded("max_attempts")
        self.attempts += 1


class AttemptClock:
    def __init__(self, budget, client_socket=None, on_wait=None):
        self.budget = budget
        self.client_socket = client_socket
        self.on_wait = on_wait
        self.disconnected = False
        self.socket = None
        self.timer = None
        self.phase = "connect"
        self.phase_deadline = time.monotonic() + budget.limits["connect_seconds"]
        self.expired = None
        self.lock = threading.RLock()
        self.closed = False

    def timeout(self):
        if self.disconnected:
            raise ClientDisconnected("downstream client disconnected")
        now = time.monotonic()
        if now >= self.budget.deadline:
            raise BudgetExceeded("total")
        if self.expired or now >= self.phase_deadline:
            raise BudgetExceeded(self.expired or self.phase)
        return max(0.001, min(self.budget.deadline, self.phase_deadline) - now)

    def _check_client(self):
        if self.client_socket is not None:
            try:
                readable, _, _ = select.select([self.client_socket], [], [], 0)
                if readable and not self.client_socket.recv(1, socket.MSG_PEEK):
                    self.disconnected = True
                if not self.disconnected and self.on_wait:
                    self.on_wait()
            except (OSError, ValueError):
                self.disconnected = True

    def _interrupt(self):
        with self.lock:
            if self.closed:
                return
            self._check_client()
            if not self.disconnected and time.monotonic() < min(self.budget.deadline, self.phase_deadline):
                self._arm()
                return
            if not self.disconnected:
                self.expired = "total" if time.monotonic() >= self.budget.deadline else self.phase
            if self.socket is not None:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _arm(self):
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(min(self.timeout(), 0.25) if self.client_socket is not None else self.timeout(), self._interrupt)
        self.timer.daemon = True
        self.timer.start()

    def connected(self, sock):
        with self.lock:
            self.socket = sock
            self.phase = "first_token"
            self.phase_deadline = time.monotonic() + self.budget.limits["first_token_seconds"]
            self._arm()

    def before_read(self):
        with self.lock:
            self._check_client()
            timeout = self.timeout()
            if self.socket is not None:
                self.socket.settimeout(timeout)

    def progress(self, token=False):
        with self.lock:
            self.timeout()
            if token or self.phase == "idle":
                self.phase = "idle"
                self.phase_deadline = time.monotonic() + self.budget.limits["idle_seconds"]
                self._arm()

    def close(self):
        with self.lock:
            self.closed = True
            if self.timer:
                self.timer.cancel()

    def failure_phase(self, exc):
        return (getattr(exc, "phase", None) or
                ("total" if not self.budget.remaining() else self.expired or
                 ("total" if self.budget.deadline <= self.phase_deadline else self.phase)))


class ExhaustedRequests:
    """Bounded, private fingerprints stop proxy/client retries renewing a spent budget.

    A new turn, changed input, model or routing options has a different identity.
    Only exhausted calls are remembered; provider health is never affected.
    """
    def __init__(self, ttl=900, capacity=256):
        self.ttl = ttl
        self.capacity = capacity
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    @staticmethod
    def key(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).digest()

    def contains(self, key):
        now = time.monotonic()
        with self.lock:
            while self.entries and next(iter(self.entries.values())) <= now:
                self.entries.popitem(last=False)
            return key in self.entries

    def remember(self, key):
        if key is None:
            return
        with self.lock:
            self.entries.pop(key, None)
            self.entries[key] = time.monotonic() + self.ttl
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
