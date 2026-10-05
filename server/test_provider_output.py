"""A bad Gonka response cannot reach Codex before provider fallback."""

import io
import json
import unittest
from unittest import mock

import jev_server as jev
from provider_output import normalize_sparse_output_indices, validate_gonka_response


REQUEST = {"model": "gonka/glm", "stream": True, "input": "fixture",
           "tools": [{"type": "function", "name": "exec_command"}]}


def answer(text):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}]}


def tool_call():
    return {"type": "function_call", "name": "exec_command", "call_id": "call_1",
            "arguments": '{"cmd":"pwd"}'}


def stream(response):
    events = [
        {"type": "response.created", "response": {"id": "resp_test", "status": "in_progress"}},
        {"type": "response.output_item.done", "output_index": 0,
         "item": response["output"][0]},
        {"type": "response.completed", "response": {"id": "resp_test", **response}},
    ]
    return ("".join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n'
                    for event in events) + "data: [DONE]\n\n").encode()


class GonkaOutputTests(unittest.TestCase):
    def test_valid_tool_call_and_text(self):
        self.assertIsNone(validate_gonka_response(
            {"status": "completed", "output": [tool_call()]}, REQUEST))
        self.assertIsNone(validate_gonka_response(answer("The answer is seven."), REQUEST))

    def test_leaked_tool_call_or_duplicate_answer_is_rejected(self):
        self.assertEqual(validate_gonka_response(
            {"status": "completed", "output": [tool_call(), *answer("I ran it.")["output"]]},
            REQUEST), "text_alongside_tool_call")
        self.assertEqual(validate_gonka_response(answer("<tool_call>exec_command</tool_call>"),
                                                 REQUEST), "tool_call_leaked_as_text")
        self.assertEqual(validate_gonka_response(answer(("This is a long repeated answer with enough words to be detected correctly. " * 2 + "\n\n") * 3),
                                                 REQUEST), "repeated_final_answer")
        self.assertEqual(validate_gonka_response({"status": "completed", "output": [
            {**tool_call(), "name": "unlisted_tool"}]}, REQUEST), "invalid_function_call")
        self.assertEqual(validate_gonka_response({"status": "completed", "output": [
            tool_call(), {**tool_call(), "call_id": "call_2"}]}, REQUEST),
            "duplicate_tool_action")

    def _forward(self, raw, ctype="text/event-stream"):
        response = mock.Mock(spec=["status", "headers", "getheader", "read"], status=200, headers={})
        response.getheader.return_value = ctype
        response.read.return_value = raw
        connection = mock.Mock()
        connection.getresponse.return_value = response
        handler = jev.Handler.__new__(jev.Handler)
        handler.wfile = io.BytesIO()
        handler._response_started = False
        sent = []
        handler.send_response = lambda status: sent.append(status)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler._attempts = []
        with mock.patch.object(jev.http.client, "HTTPConnection", return_value=connection):
            result = handler._forward(REQUEST.copy(), "/v1/responses", True, False,
                                      "", REQUEST["model"], external={"host": "127.0.0.1",
                                      "port": 20128, "key": "fixture"}, validate_gonka=True)
        return result, sent, handler.wfile.getvalue(), handler._attempts[0]

    def test_invalid_stream_is_held_back_for_fallback(self):
        bad = {"status": "completed", "output": [tool_call(), *answer("Invented tool result.")["output"]]}
        result, sent, body, attempt = self._forward(stream(bad))
        self.assertEqual(result[0], 502)
        self.assertEqual(sent, [])
        self.assertEqual(body, b"")
        self.assertIn("invalid_response", attempt["completion"])

    def test_reasoning_only_completed_stream_is_held_back(self):
        # Wally can claim completion after emitting only a reasoning item.
        bad = {"status": "completed", "output": [{"type": "reasoning", "id": "reason_1"}]}
        result, sent, body, attempt = self._forward(stream(bad))
        self.assertEqual(result[0], 502)
        self.assertEqual(sent, [])
        self.assertEqual(body, b"")
        self.assertIn("invalid_response", attempt["completion"])

    def test_clean_stream_reaches_codex_once(self):
        result, sent, body, attempt = self._forward(stream({"status": "completed", "output": [tool_call()]}))
        self.assertEqual(result[0], 200)
        self.assertEqual(sent, [200])
        self.assertEqual(body.count(b"event: response.completed"), 1)
        self.assertEqual(attempt["terminal_type"], "response.completed")

    def test_non_stream_200_is_held_back(self):
        result, sent, body, _ = self._forward(json.dumps(answer("text")).encode(), "application/json")
        self.assertEqual(result[0], 502)
        self.assertEqual(sent, [])
        self.assertEqual(body, b"")

    def test_ambiguous_sparse_stream_is_held_back(self):
        result, sent, body, attempt = self._forward(SparseIndexTests().fixture(final_type="message"))
        self.assertEqual(result[0], 502)
        self.assertEqual(sent, [])
        self.assertEqual(body, b"")
        self.assertEqual(attempt["completion"], "sparse_index_ambiguous")


class SparseIndexTests(unittest.TestCase):
    def fixture(self, index=2, final_type="function_call"):
        reasoning = {"type": "reasoning", "id": "reason_1"}
        tool = {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                "name": "exec_command", "arguments": '{"cmd":"pwd"}'}
        events = [
            {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
            {"type": "response.output_item.added", "output_index": 0, "item": reasoning},
            {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
            {"type": "response.output_item.added", "output_index": index, "item": tool},
            {"type": "response.function_call_arguments.delta", "output_index": index,
             "item_id": "fc_1", "delta": '{"cmd":"pwd"}'},
            {"type": "response.function_call_arguments.done", "output_index": index,
             "item_id": "fc_1", "arguments": '{"cmd":"pwd"}'},
            {"type": "response.output_item.done", "output_index": index, "item": tool},
            {"type": "response.completed", "response": {"id": "resp_1", "status": "completed",
             "output": [reasoning, {**tool, "type": final_type}]}},
        ]
        return ("".join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n'
                        for event in events)).encode()

    def test_sparse_indices_become_dense_without_dropping_tool(self):
        normalized, changed = normalize_sparse_output_indices(self.fixture())
        self.assertTrue(changed)
        events = [json.loads(line[6:]) for line in normalized.decode().splitlines()
                  if line.startswith("data: ")]
        added = [event for event in events if event["type"] == "response.output_item.added"]
        self.assertEqual([event["output_index"] for event in added], [0, 1])
        self.assertEqual([event["output_index"] for event in events
                          if event["type"] == "response.function_call_arguments.done"], [1])
        self.assertEqual(added[-1]["item"]["call_id"], "call_1")
        self.assertEqual(events[-1]["response"]["output"][-1]["call_id"], "call_1")

    def test_ambiguous_final_item_refuses_rewrite(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            normalize_sparse_output_indices(self.fixture(final_type="message"))

    def test_contiguous_stream_is_byte_exact(self):
        raw = self.fixture(index=1)
        normalized, changed = normalize_sparse_output_indices(raw)
        self.assertFalse(changed)
        self.assertEqual(normalized, raw)

    def test_missing_index_is_left_for_standard_adapter_to_fill(self):
        item = {"type": "message", "id": "msg_1", "role": "assistant",
                "content": [{"type": "output_text", "text": "safe"}]}
        events = [
            {"type": "response.output_item.added", "item": item},
            {"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "output": [item]}},
        ]
        raw = "".join(f'data: {json.dumps(event)}\n\n' for event in events).encode()
        normalized, changed = normalize_sparse_output_indices(raw)
        self.assertFalse(changed)
        self.assertEqual(normalized, raw)


if __name__ == "__main__":
    unittest.main()
