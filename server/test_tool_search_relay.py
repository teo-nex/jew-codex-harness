"""External Responses must preserve Codex's client-executed tool discovery."""

import json
import unittest

from jev_server import SummaryMarker, external_tool_search_payload


def event(kind, **fields):
    value = {"type": kind, **fields}
    return f"event: {kind}\ndata: {json.dumps(value)}\n\n"


class ToolSearchPayloadTests(unittest.TestCase):
    def test_external_declaration_changes_without_mutating_canonical_history(self):
        for schema in ({"type": "object", "properties": {"query": {"type": "string"}}},
                       {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}}}):
            with self.subTest(schema=schema):
                payload = {"tools": [{"type": "tool_search", "execution": "client", "description": "Discovery", "parameters": schema},
                                     {"type": "function", "name": "ordinary_tool"}],
                           "input": [{"type": "tool_search_call", "execution": "client", "call_id": "call_a", "arguments": {"query": "fixture"}},
                                     {"type": "tool_search_output", "execution": "client", "call_id": "call_a", "tools": []}],
                           "tool_choice": {"type": "tool_search"}}
                original = json.loads(json.dumps(payload))
                wire = external_tool_search_payload(payload)
                self.assertEqual(payload, original)
                self.assertIs(wire["input"], payload["input"])
                self.assertEqual(wire["tools"][0], {"type": "function", "name": "tool_search", "description": "Discovery", "parameters": schema})
                self.assertIs(wire["tools"][1], payload["tools"][1])
                self.assertEqual(wire["tool_choice"], {"type": "function", "name": "tool_search"})

    def test_unrelated_and_server_executed_tools_are_unchanged(self):
        for tools in (None, [], [{"type": "function", "name": "tool_search"}],
                      [{"type": "tool_search", "execution": "server"}], [{"type": "web_search"}]):
            payload = {"tools": tools, "input": "fixture"}
            self.assertIs(external_tool_search_payload(payload), payload)

    def test_discovery_name_collisions_fail_closed(self):
        native = {"type": "tool_search", "execution": "client"}
        for other in ({"type": "function", "name": "tool_search"},
                      {"type": "function", "function": {"name": "tool_search"}}, native.copy()):
            with self.subTest(other=other):
                payload = {"tools": [native, other]}
                original = json.loads(json.dumps(payload))
                with self.assertRaisesRegex(ValueError, "Ambiguous"):
                    external_tool_search_payload(payload)
                self.assertEqual(payload, original)


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
