"""External Responses must preserve Codex's client-executed tool discovery."""

import json
import unittest

from jev_server import SummaryMarker


def event(kind, **fields):
    value = {"type": kind, **fields}
    return f"event: {kind}\ndata: {json.dumps(value)}\n\n"


class ToolSearchRelayTests(unittest.TestCase):
    def test_function_call_becomes_native_tool_search_call(self):
        call = {"id": "fc_search", "type": "function_call", "name": "tool_search",
                "call_id": "call_search", "arguments": '{"query":"jev-workers status"}'}
        raw = (
            event("response.created", response={"id": "resp_search", "status": "in_progress"})
            + event("response.output_item.added", output_index=0,
                    item={**call, "arguments": ""})
            + event("response.function_call_arguments.delta", output_index=0,
                    item_id="fc_search", delta='{"query":')
            + event("response.function_call_arguments.done", output_index=0,
                    item_id="fc_search", arguments=call["arguments"])
            + event("response.output_item.done", output_index=0, item=call.copy())
            + event("response.completed", response={"id": "resp_search", "status": "completed",
                                                    "output": [call.copy()]})
            + "data: [DONE]\n\n"
        ).encode()
        marker = SummaryMarker("", tool_search=True)
        output = marker.feed(raw[:47]) + marker.feed(raw[47:]) + marker.flush()
        events = []
        for line in output.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                events.append(json.loads(line[6:]))
        names = [item["type"] for item in events]
        self.assertNotIn("response.function_call_arguments.delta", names)
        self.assertNotIn("response.function_call_arguments.done", names)
        added = next(item["item"] for item in events if item["type"] == "response.output_item.added")
        done = next(item["item"] for item in events if item["type"] == "response.output_item.done")
        terminal = next(item["response"]["output"][0] for item in events
                        if item["type"] == "response.completed")
        for item in (added, done, terminal):
            self.assertEqual(item["type"], "tool_search_call")
            self.assertEqual(item["execution"], "client")
            self.assertEqual(item["call_id"], "call_search")
            self.assertTrue(item["id"].startswith("tsc"))
            self.assertNotIn("name", item)
        self.assertEqual(done["arguments"], {"query": "jev-workers status"})
        self.assertEqual(terminal["arguments"], done["arguments"])
        self.assertEqual(marker.terminal_type, "response.completed")


if __name__ == "__main__":
    unittest.main()
