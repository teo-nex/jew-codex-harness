import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harness.idle_compact import main, terminal, terminal_server_args, terminate, owned_process_options


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


class ProcessOwnershipTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(os.name == "nt", "POSIX process group fixture")
    async def test_native_child_of_launcher_cannot_outlive_controller(self):
        child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(10)"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(12)"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", parent, child, stdout=subprocess.PIPE, **owned_process_options())
        denied = False
        try:
            self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b"ready\n")
            try:
                await terminate(process, process_tree=True)
            except PermissionError:
                denied = True
                # A restrictive outer host can deny signals to descendants.
                # Do not retry via another API; these fixtures expire by design.
                await asyncio.wait_for(process.stdout.read(), 15)
                await process.wait()
                self.skipTest("Host denies owned descendant process-group signals")
            # The deliberately SIGTERM-resistant descendant holds stdout open
            # until it is killed too; parent exit alone cannot satisfy this.
            self.assertEqual(await asyncio.wait_for(process.stdout.read(), 3), b"")
        finally:
            if not denied:
                await terminate(process, process_tree=True)

    async def test_windows_cleanup_targets_only_owned_pid_and_descendants(self):
        commands = []
        class Process:
            pid, returncode = 12345, None
            async def wait(self):
                self.returncode = 0
                return 0
        async def launch(*command, **kwargs):
            commands.append(command)
            return Process()
        with patch("harness.idle_compact.os.name", "nt"), patch("asyncio.create_subprocess_exec", launch):
            await terminate(Process(), process_tree=True)
        self.assertEqual(commands[0][1:], ("/PID", "12345", "/T", "/F"))


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
            pid = 12345
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
        with (patch("harness.idle_compact.relay", fake_relay),
              patch("asyncio.create_subprocess_exec", launch),
              patch("harness.idle_compact.os.killpg", create=True)):
            self.assertEqual(await terminal(args, "codex"), 0)
        self.assertEqual(observed[0]["codex_args"], args.codex_args)


if __name__ == "__main__":
    unittest.main()
