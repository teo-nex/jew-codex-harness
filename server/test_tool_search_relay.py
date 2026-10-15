"""External Responses must preserve Codex's client-executed tool discovery."""

import copy
import io
import json
import unittest
from unittest import mock

import jev_server as jev

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


class NonStreamDiscoveryTests(unittest.TestCase):
    def test_nonstream_json_and_sse_restore_only_external_client_discovery(self):
        for transport in ("application/json", "text/event-stream"):
            for external, discovery in ((True, True), (True, False), (False, True)):
                with self.subTest(transport=transport, external=external, discovery=discovery):
                    call = {"id": "fc_fixture", "type": "function_call", "name": "tool_search",
                            "call_id": "call_fixture", "arguments": '{"query":"fixture"}'}
                    ordinary = {**call, "id": "fc_other", "name": "ordinary_tool", "call_id": "call_other"}
                    response = {"id": "resp_fixture", "status": "completed", "output": [call, ordinary]}
                    raw = (json.dumps(response) if transport == "application/json" else
                           event("response.completed", response=response)).encode()
                    payload = {"model": "fixture/model", "stream": False, "input": "fixture",
                               "tools": [{"type": "tool_search", "execution": "client"}] if discovery else
                                        [{"type": "function", "name": "tool_search"}]}
                    original = copy.deepcopy(payload)
                    upstream = mock.Mock(spec=["status", "headers", "getheader", "read"],
                                         status=200, headers={})
                    upstream.getheader.return_value = transport
                    upstream.read.return_value = raw
                    connection = mock.Mock()
                    connection.getresponse.return_value = upstream
                    handler = jev.Handler.__new__(jev.Handler)
                    handler._attempts = []; handler._response_started = False
                    handler.wfile = io.BytesIO(); handler.headers = {}
                    handler.send_response = lambda *args: None
                    handler.send_header = lambda *args: None
                    handler.end_headers = lambda: None
                    route = {"host": "fixture", "port": 1, "key": "synthetic"} if external else None
                    with mock.patch.object(jev.http.client, "HTTPConnection", return_value=connection), \
                         mock.patch.object(jev, "codex_client_version", return_value="1.2.3"), \
                         mock.patch.object(jev, "caller_secret", return_value="synthetic"):
                        result = handler._forward(payload, "/v1/responses", False, False, "",
                                                  "fixture/model", external=route)
                    self.assertEqual(result[0], 200)
                    returned = json.loads(handler.wfile.getvalue())
                    item = returned["output"][0]
                    if external and discovery:
                        self.assertEqual(item["type"], "tool_search_call")
                        self.assertEqual(item["execution"], "client")
                        self.assertEqual(item["arguments"], {"query": "fixture"})
                        self.assertEqual(item["status"], "completed")
                        self.assertEqual(item["id"], "tsc_fc_fixture")
                        self.assertEqual(item["call_id"], call["call_id"])
                        self.assertNotIn("name", item)
                    else:
                        self.assertEqual(item, call)
                    self.assertEqual(returned["output"][1], ordinary)
                    self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()
