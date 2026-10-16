"""Only the native MCP is offered to CanvasTTY; standalone workers retain their tools elsewhere."""
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

CONTEXT = {"CANVASTTY_RUNTIME_PROVIDER": "codex", "CANVASTTY_RUNTIME_ADDRESS": "/fixture/runtime.sock",
           "CANVASTTY_RUNTIME_TERMINAL_SESSION_ID": "12345678-1234-4567-8901-123456789abc",
           "CANVASTTY_RUNTIME_CAPABILITY": "fixture-capability"}


class WorkerMCPOwnership(unittest.TestCase):
    def setUp(self):
        self.dispatch = types.ModuleType("portable")
        for name in ("submit", "status", "collect", "cancel", "execute"):
            setattr(self.dispatch, name, mock.Mock(return_value={"state": "finished"}))
        path = Path(__file__).resolve().parents[1] / "integrations/workers/mcp.py"
        spec = importlib.util.spec_from_file_location("worker_mcp_ownership", path)
        self.module = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(sys.modules, {"portable": self.dispatch}):
            spec.loader.exec_module(self.module)
        self.enterContext(mock.patch.dict(os.environ, {key: "" for key in CONTEXT}))

    def test_native_context_hides_standalone_tools_and_never_starts_a_worker(self):
        with mock.patch.dict(os.environ, CONTEXT):
            self.assertEqual(self.module.handle({"method": "tools/list"}), {"tools": []})
            result = self.module.handle({"method": "tools/call", "params": {"name": "execute", "arguments": {}}})
        self.assertTrue(result["isError"])
        self.assertIn("canvastty_agents", result["content"][0]["text"])
        self.dispatch.execute.assert_not_called()
        self.assertNotIn(CONTEXT["CANVASTTY_RUNTIME_CAPABILITY"], str(result))

    def test_standalone_and_partial_native_context_keep_the_original_tools(self):
        for env in ({}, {**CONTEXT, "CANVASTTY_RUNTIME_ADDRESS": ""},
                    {**CONTEXT, "CANVASTTY_RUNTIME_TERMINAL_SESSION_ID": "invalid"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertEqual(self.module.handle({"method": "tools/list"})["tools"], self.module.TOOLS)
                result = self.module.handle({"method": "tools/call", "params": {"name": "execute", "arguments": {}}})
                self.assertFalse(result["isError"])
