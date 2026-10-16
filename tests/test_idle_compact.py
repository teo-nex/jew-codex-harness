import asyncio
import unittest

from harness.idle_compact import Controller, ThreadState, DEFAULT_IDLE_SECONDS


class IdleCompactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 0
        self.up, self.down, self.events = [], [], []
        self.background = []
        self.preflight_action = None
        self.hold = False
        async def upstream(message):
            self.up.append(message)
            method = message.get("method")
            if not str(message.get("id", "")).startswith("jev-idle-"):
                return
            result = {}
            if method == "thread/loaded/list":
                result = {"data": list(self.c.threads)}
            elif method == "thread/read":
                result = {"thread": {"status": {"type": "idle"}}}
            elif method == "thread/backgroundTerminals/list":
                result = {"data": self.background}
                if self.preflight_action:
                    await self.preflight_action()
            await self.c.server({"id": message["id"], "result": result})
            if method == "thread/compact/start" and not self.hold:
                await self.finish()
        async def downstream(message):
            self.down.append(message)
        self.c = Controller(upstream, downstream, clock=lambda: self.now,
                            timeout_seconds=0.2, report=lambda **e: self.events.append(e))
        self.c.initialized = True
        self.c.threads["t"] = ThreadState("idle", 0, True, True)

    async def notify(self, method, **params):
        await self.c.server({"method": method, "params": {"threadId": "t", **params}})

    async def finish(self):
        await self.notify("turn/started", turn={"id": "compact", "status": "inProgress"})
        for method in ("item/started", "item/completed"):
            await self.notify(method, turnId="compact", item={"id": "item", "type": "contextCompaction"})
        await self.notify("turn/completed", turn={"id": "compact", "status": "completed"})
        await self.notify("thread/status/changed", status={"type": "idle"})

    def compacts(self):
        return [m for m in self.up if m.get("method") == "thread/compact/start"]

    async def test_boundary_and_once_per_idle_period(self):
        self.assertEqual(DEFAULT_IDLE_SECONDS, 1800)
        self.now = 1799
        await self.c.tick()
        self.assertFalse(self.compacts())
        self.now = 1800
        await self.c.tick()
        self.assertEqual(len(self.compacts()), 1)
        self.assertEqual(self.events[-1]["event"], "compact_completed")
        self.assertFalse(any(str(m.get("id", "")).startswith("jev-idle-") for m in self.down))
        self.now = 9000
        await self.c.tick()
        self.assertEqual(len(self.compacts()), 1)

    async def test_new_successful_work_rearms(self):
        self.now = 1800
        await self.c.tick()
        await self.notify("turn/started", turn={"id": "user"})
        await self.notify("turn/completed", turn={"id": "user", "status": "completed"})
        await self.notify("thread/status/changed", status={"type": "idle"})
        self.now = 3600
        await self.c.tick()
        self.assertEqual(len(self.compacts()), 2)

    async def test_active_worker_and_approval_defer(self):
        self.c.threads["worker"] = ThreadState("active")
        self.now = 1800
        await self.c.tick()
        self.assertFalse(self.compacts())
        self.c.threads.pop("worker")
        self.c.approvals.add(9)
        await self.c.client({"id": 9, "result": {"decision": "accept"}})
        self.assertIn(9, self.c.approvals)
        await self.c.tick()
        await self.notify("serverRequest/resolved", requestId=9)
        self.now = 3599
        await self.c.tick()
        self.assertFalse(self.compacts())
        self.now = 3600
        await self.c.tick()
        self.assertEqual(len(self.compacts()), 1)

    async def test_background_process_resets_idle(self):
        self.now = 1800
        self.background = [{"processId": "p"}]
        await self.c.tick()
        self.background = []
        self.now = 3599
        await self.c.tick()
        self.assertFalse(self.compacts())
        self.now = 3600
        await self.c.tick()
        self.assertTrue(self.compacts())

    async def test_nested_worker_is_observed_but_never_compacted(self):
        await self.c.server({"method": "thread/started", "params": {
            "thread": {"id": "worker", "status": {"type": "idle"}}}})
        await self.c.server({"method": "turn/completed", "params": {
            "threadId": "worker", "turn": {"id": "w", "status": "completed"}}})
        self.c.threads["t"].armed = False
        self.now = 9000
        await self.c.tick()
        self.assertFalse(self.compacts())
        self.assertFalse(self.c.threads["worker"].eligible)

    async def test_user_return_during_preflight_cancels(self):
        async def user():
            await self.c.client({"id": 4, "method": "turn/start", "params": {"threadId": "t"}})
        self.preflight_action = user
        self.now = 1800
        await self.c.tick()
        self.assertFalse(self.compacts())
        self.assertEqual(self.up[-1]["id"], 4)

    async def test_user_return_during_compact_waits_for_verified_completion(self):
        self.hold, self.now = True, 1800
        task = asyncio.create_task(self.c.tick())
        for _ in range(20):
            await asyncio.sleep(0)
            if self.compacts():
                break
        await self.c.client({"id": 4, "method": "turn/start"})
        self.assertFalse(any(m.get("id") == 4 for m in self.up))
        # A response acknowledgement alone must not release the new turn.
        self.assertFalse(task.done())
        await self.finish()
        await task
        self.assertEqual(self.up[-1]["id"], 4)

    async def test_timeout_does_not_release_queued_turn(self):
        self.hold, self.now = True, 1800
        task = asyncio.create_task(self.c.tick())
        for _ in range(20):
            await asyncio.sleep(0)
            if self.compacts():
                break
        await self.c.client({"id": 4, "method": "turn/start"})
        with self.assertRaises(RuntimeError):
            await task
        self.assertTrue(self.c.closed)
        self.assertFalse(any(m.get("id") == 4 for m in self.up))

    async def test_new_arrival_cannot_overtake_existing_queue_during_drain(self):
        self.hold, self.now = True, 1800
        task = asyncio.create_task(self.c.tick())
        for _ in range(20):
            await asyncio.sleep(0)
            if self.compacts():
                break
        for identifier in (10, 11):
            await self.c.client({"id": identifier, "method": "thread/read"})
        original = self.c.upstream
        async def interleave(message):
            await original(message)
            if message.get("id") == 10:
                await self.c.client({"id": 12, "method": "thread/read"})
        self.c.upstream = interleave
        await self.finish()
        await task
        self.assertEqual([m["id"] for m in self.up if m.get("id") in (10, 11, 12)], [10, 11, 12])
        self.assertEqual(self.c.queued_bytes, 0)

    async def test_no_compact_for_empty_session_or_failed_work(self):
        self.c.threads["t"].armed = False
        self.now = 9000
        await self.c.tick()
        self.assertFalse(self.compacts())
        await self.notify("turn/completed", turn={"id": "fail", "status": "failed"})
        await self.c.tick()
        self.assertFalse(self.compacts())

    async def test_model_config_and_tools_pass_through_unchanged(self):
        for model in ("jev/auto", "gpt-6.1-sol"):
            msg = {"id": model, "method": "thread/start", "params": {"model": model,
                   "sandbox": "workspace-write", "approvalPolicy": "on-request"}}
            await self.c.client(msg)
            self.assertEqual(self.up[-1], msg)

    async def test_explicit_process_defers_until_exit(self):
        await self.c.client({"id": 3, "method": "process/spawn", "params": {"processHandle": "p"}})
        await self.c.server({"id": 3, "result": {}})
        self.now = 1800
        await self.c.tick()
        self.assertFalse(self.compacts())
        await self.notify("process/exited", processHandle="p")
        self.assertFalse(self.c.processes)
        self.now = 3600
        await self.c.tick()
        self.assertTrue(self.compacts())

    async def test_initialize_preserves_client_fields_and_enables_safety_api(self):
        message = {"id": 5, "method": "initialize", "params": {
            "clientInfo": {"name": "client", "version": "1"},
            "capabilities": {"optOutNotificationMethods": ["irrelevant/event"]}}}
        await self.c.client(message)
        self.assertTrue(self.up[-1]["params"]["capabilities"]["experimentalApi"])
        self.assertNotIn("experimentalApi", message["params"]["capabilities"])
        self.assertEqual(self.up[-1]["params"]["clientInfo"], message["params"]["clientInfo"])

    async def test_unknown_loaded_worker_and_pagination_defer(self):
        original = self.c.upstream
        for loaded in ({"data": ["t", "unknown"]}, {"data": ["t"], "nextCursor": "next"}):
            async def intercept(message):
                if message.get("method") == "thread/loaded/list":
                    await self.c.server({"id": message["id"], "result": loaded})
                else:
                    await original(message)
            self.c.upstream = intercept
            self.now += 1800
            await self.c.tick()
            self.assertFalse(self.compacts())

    async def test_client_opt_out_disables_timer_without_overriding_preference(self):
        await self.c.client({"id": 5, "method": "initialize", "params": {
            "capabilities": {"optOutNotificationMethods": ["item/completed"]}}})
        await self.c.server({"id": 5, "result": {}})
        self.now = 1800
        await self.c.tick()
        self.assertFalse(self.compacts())
        self.assertFalse(self.c.notifications_available)
        self.assertEqual(self.up[0]["params"]["capabilities"]["optOutNotificationMethods"], ["item/completed"])

    async def test_preflight_rejection_does_not_close_or_retry(self):
        async def reject(message):
            await self.c.server({"id": message["id"], "error": {"code": -32601}})
        self.c.upstream = reject
        self.now = 1800
        await self.c.tick()
        self.assertFalse(self.c.closed)
        self.assertFalse(self.c.threads["t"].armed)
        self.assertEqual(self.events[-1]["event"], "compact_preflight_unavailable")

    async def test_late_internal_response_never_leaks_into_client_protocol(self):
        await self.c.server({"id": "jev-idle-expired", "result": {"data": []}})
        self.assertFalse(self.down)

    async def test_unrelated_turn_cannot_release_compaction_gate(self):
        self.hold, self.now = True, 1800
        task = asyncio.create_task(self.c.tick())
        for _ in range(20):
            await asyncio.sleep(0)
            if self.compacts():
                break
        await self.notify("turn/started", turn={"id": "compact"})
        await self.notify("item/started", turnId="compact", item={"id": "c", "type": "contextCompaction"})
        await self.notify("item/completed", turnId="stale", item={"id": "c", "type": "contextCompaction"})
        await self.notify("turn/completed", turn={"id": "stale", "status": "completed"})
        self.assertFalse(self.c.lifecycle["done"].is_set())
        await self.finish()
        await task


if __name__ == "__main__":
    unittest.main()
