"""Conservative Responses checks before exposing a Gonka fallback turn."""

import json
import re


TOOL_TEXT = re.compile(
    r"<\s*(?:tool_call|function_call|\|?tool)[^>]*>|"
    r"\[im_start\]\s*(?:assistant\s+to=|tool)|"
    r"\b(?:functions\.[a-z_][\w.]*|recipient=functions\.)\s*\(",
    re.IGNORECASE,
)


def normalize_sparse_output_indices(raw):
    """Map a provider's sparse streamed output indices onto its final array.

    The full external SSE is still buffered. Rewrite only if every streamed
    added item matches the final output item at that position; otherwise fail
    before exposing a possible tool call. IDs, call IDs and arguments are kept.
    """
    parts = re.split(r"(\r?\n\r?\n)", raw.decode("utf-8"))
    records = []
    added = []
    final = None
    for position in range(0, len(parts), 2):
        lines = parts[position].splitlines(keepends=True)
        data_lines = [i for i, line in enumerate(lines) if line.startswith("data:")]
        if len(data_lines) != 1:
            records.append((position, lines, None, None))
            continue
        index = data_lines[0]
        try:
            value = json.loads(lines[index][5:].strip())
        except ValueError:
            records.append((position, lines, None, None))
            continue
        if not isinstance(value, dict):
            records.append((position, lines, None, None))
            continue
        records.append((position, lines, index, value))
        if value.get("type") == "response.output_item.added":
            added.append(value)
        elif value.get("type") == "response.completed":
            response = value.get("response")
            final = response.get("output") if isinstance(response, dict) else None
    if not added:
        return raw, False
    originals = []
    for event in added:
        top = event.get("output_index")
        item_index = event.get("item", {}).get("output_index") if isinstance(event.get("item"), dict) else None
        if top is not None and (not isinstance(top, int) or isinstance(top, bool) or top < 0):
            raise ValueError("sparse_index_ambiguous")
        if item_index is not None and (not isinstance(item_index, int) or isinstance(item_index, bool) or item_index < 0):
            raise ValueError("sparse_index_ambiguous")
        originals.append(top if top is not None else item_index)
    if all(index is None or index == position for position, index in enumerate(originals)):
        return raw, False
    if (not isinstance(final, list) or len(final) != len(added)
            or any(index is None for index in originals)
            or len(set(originals)) != len(originals)):
        raise ValueError("sparse_index_ambiguous")
    mapping = dict(zip(originals, range(len(originals))))
    for event, item in zip(added, final):
        streamed = event.get("item")
        if not isinstance(streamed, dict) or not isinstance(item, dict):
            raise ValueError("sparse_index_ambiguous")
        if streamed.get("type") != item.get("type"):
            raise ValueError("sparse_index_ambiguous")
        for key in ("id", "call_id"):
            if (streamed.get(key) is not None and item.get(key) is not None
                    and streamed[key] != item[key]):
                raise ValueError("sparse_index_ambiguous")
    for position, lines, data_line, value in records:
        if value is None:
            continue
        changed = False
        for obj in (value, value.get("item") if isinstance(value.get("item"), dict) else None):
            if isinstance(obj, dict) and isinstance(obj.get("output_index"), int):
                old = obj["output_index"]
                if old not in mapping:
                    raise ValueError("sparse_index_ambiguous")
                obj["output_index"] = mapping[old]
                changed = True
        response = value.get("response")
        if isinstance(response, dict) and isinstance(response.get("output"), list):
            for item in response["output"]:
                if isinstance(item, dict) and isinstance(item.get("output_index"), int):
                    old = item["output_index"]
                    if old not in mapping:
                        raise ValueError("sparse_index_ambiguous")
                    item["output_index"] = mapping[old]
                    changed = True
        if changed:
            original = lines[data_line]
            newline = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else ""
            prefix = "data: " if original.startswith("data: ") else "data:"
            lines[data_line] = prefix + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + newline
            parts[position] = "".join(lines)
    return "".join(parts).encode("utf-8"), True


def _text(item):
    return "\n".join(
        part.get("text", "") for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "output_text"
        and isinstance(part.get("text"), str)
    )


def _repetitive(text):
    # Provider loops have repeated entire final paragraphs. Keep this check
    # narrow so ordinary repeated identifiers or code are not rejected.
    paragraphs = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n", text)]
    counts = {}
    for part in paragraphs:
        if len(part) >= 80:
            counts[part] = counts.get(part, 0) + 1
            if counts[part] >= 3:
                return True
    return False


def validate_gonka_response(response, request):
    """Return a reason on malformed output, or None when it is safe to relay.

    This validates format and obvious tool-call leakage, not semantic quality.
    The model must still be independently evaluated on real coding tasks.
    """
    if not isinstance(response, dict) or response.get("status") != "completed":
        return "missing_completed_response"
    output = response.get("output")
    if not isinstance(output, list) or not output:
        return "empty_output"
    tool_names = {
        tool.get("name") for tool in request.get("tools", [])
        if isinstance(tool, dict) and tool.get("type") == "function"
    }
    if any(isinstance(tool, dict) and tool.get("type") == "tool_search"
           for tool in request.get("tools", [])):
        tool_names.add("tool_search")
    calls = []
    call_arguments = []
    messages = []
    for item in output:
        if not isinstance(item, dict):
            return "invalid_output_item"
        kind = item.get("type")
        if kind == "function_call":
            if (not isinstance(item.get("call_id"), str) or not item["call_id"]
                    or item.get("name") not in tool_names
                    or not isinstance(item.get("arguments"), str)):
                return "invalid_function_call"
            calls.append(item["call_id"])
            call_arguments.append((item["name"], item["arguments"]))
        elif kind == "message":
            if item.get("role", "assistant") != "assistant":
                return "invalid_message_role"
            messages.append(_text(item))
        elif kind != "reasoning":
            return "unknown_output_item"
    if len(calls) != len(set(calls)):
        return "duplicate_call_id"
    if len(call_arguments) != len(set(call_arguments)):
        return "duplicate_tool_action"
    text = "\n".join(messages)
    if calls and text.strip():
        return "text_alongside_tool_call"
    if TOOL_TEXT.search(text):
        return "tool_call_leaked_as_text"
    if _repetitive(text):
        return "repeated_final_answer"
    if not calls and not text.strip():
        return "no_answer_or_tool_call"
    return None
