import asyncio
import importlib.util
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harness.idle_compact import main, terminal, terminal_server_args


class ArgumentTests(unittest.TestCase):
    def test_common_overrides_reach_backend_without_changing_permissions(self):
        args = ["-c", 'model_provider="fixture"', "--enable=test", "--disable", "other",
                "-m", "manual-model", "-s", "workspace-write", "prompt"]
        self.assertEqual(terminal_server_args(args), args[:5])

    def test_transport_and_unsupported_provider_overrides_fail_explicitly(self):
        for argument in ("--remote=ws://host", "--no-daemon", "--oss", "--profile=test", "-ptest"):
            with self.subTest(argument=argument), self.assertRaises(ValueError):
                terminal_server_args([argument])

    def test_options_after_mode_and_codex_separator(self):
        captured = []
        async def fake(args, executable):
            captured.append(args)
            return 0
        with patch("harness.idle_compact.shutil.which", return_value="codex"), patch("harness.idle_compact.terminal", fake):
            self.assertEqual(main(["run", "--idle-seconds", "2", "--", "-m", "manual"]), 0)
        self.assertEqual(captured[0].idle_seconds, 2)
        self.assertEqual(captured[0].codex_args, ["-m", "manual"])


@unittest.skipUnless(importlib.util.find_spec("websockets"), "Optional terminal dependency not installed")
class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_auth_origin_one_client_and_json_relay(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
        observed = []
        async def fake_relay(receive, send, **kwargs):
            observed.append(kwargs)
            message = await receive()
            await send({"id": message["id"], "result": {"ok": True}})
            await receive()
        class FakeCLI:
            returncode = None
            async def wait(self):
                for headers, origin in (({}, None), ({"Authorization": "Bearer wrong"}, None),
                                        (auth, "https://untrusted.example")):
                    with self_test.assertRaises(InvalidStatus):
                        async with connect(url, additional_headers=headers, origin=origin, proxy=None):
                            pass
                async with connect(url, additional_headers=auth, proxy=None) as client:
                    await client.send(json.dumps({"id": 7, "method": "initialize"}))
                    reply = json.loads(await client.recv())
                    self_test.assertEqual(reply, {"id": 7, "result": {"ok": True}})
                    with self_test.assertRaises(InvalidStatus):
                        async with connect(url, additional_headers=auth, proxy=None):
                            pass
                self.returncode = 0
                return 0
        self_test = self
        url, auth = None, None
        async def launch(*command, **kwargs):
            nonlocal url, auth
            self.assertEqual(command[1], "--remote")
            url = command[2]
            self.assertTrue(url.startswith("ws://127.0.0.1:"))
            token = kwargs["env"]["JEV_IDLE_COMPACT_TOKEN"]
            self.assertNotIn(token, " ".join(command))
            auth = {"Authorization": "Bearer " + token}
            return FakeCLI()
        args = SimpleNamespace(codex_args=["-c", 'model="manual"'], idle_seconds=1800, compact_timeout_seconds=600)
        with patch("harness.idle_compact.relay", fake_relay), patch("asyncio.create_subprocess_exec", launch):
            self.assertEqual(await terminal(args, "codex"), 0)
        self.assertEqual(observed[0]["codex_args"], args.codex_args)


if __name__ == "__main__":
    unittest.main()
