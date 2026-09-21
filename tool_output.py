import json

_large_tool_outputs = {}

_large_tool_output_counter = [0]
_LARGE_TOOL_OUTPUT_THRESHOLD = 3000
_LARGE_TOOL_OUTPUT_MAX_STORED = 50

# A bot's scratch file is already a deliberately curated, information-dense
# deliverable -- not raw noise like a read_file dump -- so a generic head/
# tail preview cuts exactly the itemized findings out of the middle. Exempt
# these from compress_tool_result entirely rather than relying on Athena2
# remembering to call get_full_tool_output every time.
UNCOMPRESSED_TOOL_NAMES = {"read_scratch_file", "read_tool_call_history"}


def compress_tool_result(result):
    """If a tool's result is large enough to meaningfully burn
    context, store the full original and return a compressed version
    instead -- head and tail shown (where the immediately relevant
    content usually is), with a clear note on how much was cut and
    how to retrieve the rest if genuinely needed. Rather than either
    silently truncating with no way back, or always paying the full
    token cost regardless of whether the omitted middle ever
    matters."""
    try:
        serialized = json.dumps(result)
    except (TypeError, ValueError):
        return result
    if len(serialized) <= _LARGE_TOOL_OUTPUT_THRESHOLD:
        return result

    _large_tool_output_counter[0] += 1
    output_id = str(_large_tool_output_counter[0])
    _large_tool_outputs[output_id] = result
    if len(_large_tool_outputs) > _LARGE_TOOL_OUTPUT_MAX_STORED:
        oldest_id = min(_large_tool_outputs.keys(), key=lambda k: int(k))
        _large_tool_outputs.pop(oldest_id, None)

    if isinstance(result, dict):
        compressed = dict(result)
        for field in ("output", "content"):
            value = compressed.get(field)
            if isinstance(value, str) and len(value) > _LARGE_TOOL_OUTPUT_THRESHOLD:
                head, tail = value[:1200], value[-800:]
                omitted = len(value) - len(head) - len(tail)
                compressed[field] = (
                    f"{head}\n\n... [{omitted} characters omitted -- use get_full_tool_output with "
                    f"output_id '{output_id}' if you genuinely need the omitted middle] ...\n\n{tail}"
                )
                return compressed
    return {
        "note": f"This result was large ({len(serialized)} characters) and has been stored in full. "
        f"Use get_full_tool_output with output_id '{output_id}' to retrieve it if genuinely needed.",
        "preview": serialized[:1500],
    }


def get_full_tool_output_tool(output_id):
    result = _large_tool_outputs.get(str(output_id))
    if result is None:
        return {"error": f"No stored output with id '{output_id}'. It may have aged out, or the id is wrong."}
    return result


CONTEXT_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_full_tool_output",
            "description": "Retrieve the full, uncompressed result of an earlier tool call that was shown to you as a compressed head/tail preview because it was too large. Only call this if the omitted middle genuinely matters -- most of the time the preview already has what you need.",
            "parameters": {
                "type": "object",
                "properties": {"output_id": {"type": "string", "description": "The output_id named in the compressed result's note."}},
                "required": ["output_id"],
            },
        },
    },
]
