import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest
from unittest import mock

import jev_server as jev
from request_budget import RequestBudget, AttemptClock, BudgetExceeded, validate_budget


class BudgetTests(unittest.TestCase):
    def test_validation_and_attempt_limit(self):
        for value in ({"total_seconds": float("nan")}, {"max_attempts": True},
                      {"idle_seconds": 0}, {"unknown": 1}):
            with self.assertRaises(ValueError):
                validate_budget(value)
        budget = RequestBudget({"max_attempts": 1})
        budget.claim()
        with self.assertRaises(BudgetExceeded) as caught:
            budget.claim()
        self.assertEqual(caught.exception.phase, "max_attempts")

    def _network_timeout(self, mode):
        class SlowProvider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for index in range(100):
                        event = ({"type": "response.output_text.delta", "delta": "x"}
                                 if mode != "first_token" else {"type": "response.in_progress"})
                        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                        self.wfile.flush()
                        time.sleep(0.5 if mode == "idle" else 0.02)
                except (OSError, BrokenPipeError):
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowProvider)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        handler = object.__new__(jev.Handler)
        handler._response_started = False
        handler._attempts = []
        handler.wfile = io.BytesIO()
        handler.send_response = lambda *_: None
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        # Keep non-target deadlines apart so runner scheduling cannot change
        # which limit the fixture is meant to exercise.
        limits = {"first_token_seconds": 2, "idle_seconds": 2, "total_seconds": 2}
        limits[mode + "_seconds"] = 0.3 if mode == "total" else 0.2
        handler._budget = RequestBudget(limits)
        started = time.monotonic()
        try:
            result = handler._forward({"model": "fixture", "input": "synthetic"}, "/v1/responses",
                                      True, False, "", "fixture", external={"host": "127.0.0.1",
                                          "port": server.server_port, "key": "fixture", "account": None})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(result[0], 504)
        self.assertIsNotNone(result[4])
        self.assertEqual(handler._attempts[-1]["timeout_phase"], mode)
        self.assertEqual(handler.wfile.getvalue(), b"")
        self.assertLess(time.monotonic() - started, 3)

    def test_first_token_not_reset_by_lifecycle_heartbeats(self):
        self._network_timeout("first_token")

    def test_stream_idle_is_bounded(self):
        self._network_timeout("idle")

    def test_total_deadline_stops_continuously_active_stream(self):
        self._network_timeout("total")

    def test_progress_and_stale_timer_never_extend_total_deadline(self):
        with mock.patch("request_budget.time.monotonic", return_value=0) as now, \
             mock.patch.object(AttemptClock, "_arm"):
            budget = RequestBudget({"total_seconds": 1, "idle_seconds": 0.5})
            clock = AttemptClock(budget)
            sock = mock.Mock()
            clock.connected(sock)
            now.return_value = 0.4
            clock.progress(token=True)
            now.return_value = 0.8
            clock.progress(token=True)
            clock.before_read()
            self.assertAlmostEqual(sock.settimeout.call_args.args[0], 0.2)
            self.assertEqual(budget.deadline, 1)
            clock._interrupt()
            sock.shutdown.assert_not_called()
            now.return_value = 1
            clock._interrupt()
            self.assertEqual(clock.expired, "total")
            sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
            with self.assertRaises(BudgetExceeded) as caught:
                clock.timeout()
            self.assertEqual(caught.exception.phase, "total")
            clock.close()

    def test_connect_timeout_and_exhausted_budget_do_not_send(self):
        handler = object.__new__(jev.Handler)
        handler._response_started = False
        handler._attempts = []
        handler._budget = RequestBudget({"max_attempts": 1})
        conn = mock.Mock()
        conn.connect.side_effect = socket.timeout()
        with mock.patch.object(jev.http.client, "HTTPConnection", return_value=conn):
            result = handler._forward({}, "/v1/responses", True, False, "", "fixture")
            second = handler._forward({}, "/v1/responses", True, False, "", "fixture")
        self.assertEqual(result[0], 504)
        self.assertEqual(handler._attempts[0]["timeout_phase"], "connect")
        self.assertEqual(second[0], 504)
        self.assertEqual(handler._attempts[1]["timeout_phase"], "max_attempts")
        conn.request.assert_not_called()

    def test_socket_timeout_reports_the_limit_that_bounded_the_read(self):
        with mock.patch("request_budget.time.monotonic", return_value=0) as now, \
             mock.patch.object(AttemptClock, "_arm"):
            clock = AttemptClock(RequestBudget({"total_seconds": 0.3, "idle_seconds": 2}))
            sock = mock.Mock()
            clock.connected(sock)
            clock.progress(token=True)
            clock.before_read()
            self.assertAlmostEqual(sock.settimeout.call_args.args[0], 0.3)
            # Windows socket timeouts can fire just before monotonic reaches
            # the deadline. Attribute the error to the deadline actually set.
            now.return_value = 0.299
            self.assertEqual(clock.failure_phase(socket.timeout()), "total")
            self.assertEqual(clock.failure_phase(BudgetExceeded("idle")), "idle")
            clock.close()
