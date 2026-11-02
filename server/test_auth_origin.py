"""Real loopback Jev handler: local auth and downstream auth are distinct."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
import urllib.error
import urllib.request

import jev_server as j


class AuthOriginTests(unittest.TestCase):
    def test_same_valid_key_reaches_downstream_while_wrong_key_never_does(self):
        receipts = []

        class Downstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                receipts.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                raw = b'{"error":{"type":"native_session_required","message":"No native session"}}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), Downstream),
                   ThreadingHTTPServer(("127.0.0.1", 0), j.Handler)]
        for server in servers:
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with ExitStack() as stack:
                for name, value in [("local_secret", "fixture-local"), ("caller_secret", "fixture-caller"),
                                    ("load_key", ""), ("native_dry", None)]:
                    stack.enter_context(mock.patch.object(j, name, return_value=value))
                stack.enter_context(mock.patch.object(j, "ROUTER", servers[0].server_address))
                stack.enter_context(mock.patch.object(j, "log_line"))
                stack.enter_context(mock.patch.object(j.os.path, "exists", return_value=False))
                stack.enter_context(mock.patch.dict(os.environ, {"JEV_LADDER_MODE": ""}))

                def post(path, key, payload):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{servers[1].server_port}{path}",
                        data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + key,
                        "Content-Type": "application/json"})
                    try:
                        with urllib.request.urlopen(request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as response:
                        try:
                            return response.code, json.load(response)
                        finally:
                            response.close()

                self.assertEqual(post("/ask", "fixture-local", {})[0], 400)
                payload = {"model": "jev/auto", "input": "synthetic fixture", "stream": False}
                status, downstream = post("/v1/responses", "fixture-local", payload)
                self.assertEqual(status, 401)
                self.assertEqual(downstream["error"]["type"], "native_session_required")
                self.assertEqual(receipts, ["/_codex-router/fixture-caller/v1/responses"])
                for path in ("/ask", "/v1/responses"):
                    status, local = post(path, "wrong-fixture", payload)
                    self.assertEqual(status, 401)
                    self.assertEqual(local["error"]["code"], "jev_local_auth_required")
                self.assertEqual(len(receipts), 1)
            if shutil.which("node"):
                repo = Path(__file__).resolve().parents[1]
                code = """import {readFileSync} from 'node:fs';
import {translateGatewayError} from './router/src/error-translation.mjs';
console.log(JSON.stringify(translateGatewayError({status:401,bodyText:readFileSync(0,'utf8'),
providerGeneric:true,modelName:'Jev',providerName:'jev'})));"""
                result = subprocess.run(["node", "--input-type=module", "-e", code], cwd=repo,
                                        input=json.dumps(downstream), text=True, capture_output=True, timeout=15, check=True)
                translated = json.loads(result.stdout)["error"]
                self.assertEqual(translated["code"], "native_session_required")
                self.assertNotIn("rejected the stored credentials", translated["message"])
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
