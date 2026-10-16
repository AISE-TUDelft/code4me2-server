"""Model calls to withheld tools never reach the agent (enforced selections).

The stream filter is judged by reading its output the way Goose 1.51 reads a
stream (``_goose``: ``LinesCodec`` lines, ``parse_streaming_chunk`` with serde's
type checks, and the streamed tool-call assembly of ``formats/openai.rs``), next
to the provider's own stream.
"""

from __future__ import annotations

import json
import random
import re

from agents.inference import restrict_chat_completion_tools
from agents.tool_call_filter import (
    WITHHELD_NOTICE,
    SseToolCallFilter,
    filter_chat_completion_body,
    tool_allowed,
)


def _event(payload) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _chunk(delta=None, finish=None, index=0):
    return {"choices": [{"index": index, "delta": delta if delta is not None else {}, "finish_reason": finish}]}


def _call(index=None, name=None, arguments="", call_id=None):
    call = {"type": "function", "function": {"arguments": arguments}}
    if index is not None:
        call["index"] = index
    if name is not None:
        call["function"]["name"] = name
        call["id"] = call_id if call_id is not None else f"call-{name}"
    elif call_id is not None:
        call["id"] = call_id
    return call


def _run(stream: bytes, allowlist, *, pieces=None):
    filt = SseToolCallFilter(allowlist)
    out, start = [], 0
    for cut in (pieces or []) + [len(stream)]:
        out.append(filt.feed(stream[start:cut]))
        start = cut
    out.append(filt.flush())
    raw = b"".join(out)
    return list(_chunks(raw)), filt, raw


def _calls(events):
    return [call for event in events for call in event["choices"][0]["delta"].get("tool_calls", [])]


# --- Goose 1.51 as a reader -------------------------------------------------------------------

#: What Rust's ``str::trim`` removes (Unicode White_Space).
_WHITESPACE = "\t\n\x0b\x0c\r \x85\xa0 " + "".join(map(chr, range(0x2000, 0x200B))) + "    　"


class _Stop(Exception):
    """Goose ends the stream with an error here; an open episode is lost."""


class _Arrays(Exception):
    """A struct written as a JSON array, which serde reads in field order (not modelled)."""


def _lines(raw: bytes):
    """``LinesCodec``: split on LF, one trailing CR dropped, the unterminated rest last."""
    lines = raw.split(b"\n")
    rest = lines.pop()
    if rest not in (b"", b"\r"):
        lines.append(rest)
    return [line[:-1] if line.endswith(b"\r") else line for line in lines]


def _data(line: bytes):
    """``strip_data_prefix`` on a decoded line: the trimmed value, or None."""
    text = line.decode("utf-8")
    return text[5:].strip(_WHITESPACE) if text.startswith("data:") else None


def _chunks(raw: bytes):
    """The JSON objects of the data lines Goose would read (the rest skipped)."""
    for line in _lines(raw):
        try:
            value = json.loads(_data(line) or "null")
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(value, dict):
            yield value


def _stop_unless(ok: bool) -> None:
    if not ok:
        raise _Stop


def _optional(value, kind) -> None:
    _stop_unless(value is None or (isinstance(value, kind) and not isinstance(value, bool)))


def _int(value, bits: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)


def _i32(value) -> None:
    _stop_unless(value is None or _int(value, 32))


def _finite(text: str) -> float:
    number = float(text)
    _stop_unless(number not in (float("inf"), float("-inf")))  # serde_json refuses to read it
    return number


def _struct(value) -> dict:
    if isinstance(value, list):
        raise _Arrays
    _stop_unless(isinstance(value, dict))
    return value


def _no_lone_surrogates(value) -> None:
    if isinstance(value, str):
        _stop_unless(not re.search("[\ud800-\udfff]", value))
    elif isinstance(value, list):
        for item in value:
            _no_lone_surrogates(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _no_lone_surrogates(key)
            _no_lone_surrogates(item)


def _status(raw):
    """``as_i64``, else a string parsed as i64; None when neither fits."""
    if isinstance(raw, str) and re.fullmatch(r"[+-]?[0-9]+", raw):
        raw = int(raw)
    return raw if _int(raw, 64) else None


def _parse_chunk(data: str):
    """``parse_streaming_chunk`` plus serde's checks: the chunk, or None for a metadata frame."""
    try:
        value = json.loads(data, parse_int=lambda text: -0.0 if text == "-0" else int(text),
                           parse_float=_finite, parse_constant=lambda text: _stop_unless(False))
    except ValueError:
        raise _Stop from None
    _no_lone_surrogates(value)
    if isinstance(value, dict):
        _stop_unless("error" not in value and value.get("object") != "error")
        if "choices" not in value:
            status = next((s for s in map(_status, (value.get(k) for k in ("status", "statusCode", "code"))) if s is not None), None)
            _stop_unless((status or 0) < 400 and value.get("type") != "error" and value.get("detail") is None)
            return None
    chunk = _struct(value)
    _stop_unless(isinstance(chunk["choices"], list))
    _optional(chunk.get("id"), str)
    _optional(chunk.get("model"), str)
    _stop_unless(chunk.get("created") is None or _int(chunk["created"], 64))
    for choice in chunk["choices"]:
        choice = _struct(choice)
        _i32(choice.get("index"))
        _optional(choice.get("finish_reason"), str)
        delta = _struct(choice.get("delta", {}))
        for field in ("role", "reasoning", "reasoning_content"):
            _optional(delta.get(field), str)
        _optional(delta.get("reasoning_details"), list)
        content = delta.get("content")
        for part in content if isinstance(content, list) else ():
            _stop_unless(isinstance(_struct(part).get("type"), str))
            _optional(part.get("text"), str)
        _stop_unless(content is None or isinstance(content, (str, list)))
        calls = delta.get("tool_calls")
        _optional(calls, list)
        for call in calls or ():
            _stop_unless(isinstance(call, dict))  # flattened: never read from an array
            _optional(call.get("id"), str)
            _optional(call.get("type"), str)
            _i32(call.get("index"))
            _stop_unless("function" in call)
            function = _struct(call["function"])
            _optional(function.get("name"), str)
            _optional(function.get("arguments"), str)
    return chunk


def _goose(raw: bytes, allowlist=frozenset()):
    """What Goose 1.51 runs from this stream: (episodes, number of the line it stopped at, or None).

    An episode is (number of the line that ended it, or "end"; {key: call}), a call being
    [id, name, arguments, ever named as a tool outside ``allowlist``]. Raises ``_Arrays``
    where serde would read a struct from an array.
    """
    episodes, calls = [], None
    for number, line in enumerate(_lines(raw), 1):
        try:
            data = _data(line)
            if data is None:
                continue
            if data == "[DONE]":
                return episodes, None  # an open episode is dropped
            _stop_unless(bool(data) or calls is None)
            chunk = _parse_chunk(data) if data else None
        except (_Stop, UnicodeDecodeError):
            return episodes, number
        if chunk is None:
            continue
        choices = chunk["choices"]
        if calls is None:
            entries = choices[0].get("delta", {}).get("tool_calls") if choices else None
            if not entries:
                continue
            calls, first = {}, True
        elif not choices:
            episodes.append((number, calls))
            calls = None
            continue
        else:
            entries, first = choices[0].get("delta", {}).get("tool_calls") or [], False
        for position, entry in enumerate(entries):
            index, name = entry.get("index"), entry["function"].get("name")
            arguments = entry["function"].get("arguments") or ""
            creates = entry.get("id") is not None and name is not None
            if first:
                if creates:
                    key = position if index is None else index
                    calls[key] = [entry["id"], name, arguments, not tool_allowed(name, allowlist)]
            elif index is not None:
                if index in calls:
                    call = calls[index]
                    call[1] = name or call[1]
                    call[2] += arguments
                    call[3] = call[3] or not tool_allowed(call[1], allowlist)
                elif creates:
                    calls[index] = [entry["id"], name, arguments, not tool_allowed(name, allowlist)]
        finish = choices[0].get("finish_reason") or None
        if finish in ("tool_calls", "length") or (finish and not first):
            episodes.append((number, calls))
            calls = None
    if calls is not None:
        episodes.append(("end", calls))
    return episodes, None


def _ran(raw: bytes):
    return [{key: call[1] for key, call in calls.items()} for _, calls in _goose(raw)[0]]


# --- targeted cases ---------------------------------------------------------------------------

STREAM = b"".join([
    _event(_chunk({"role": "assistant", "content": "Let me look."})),
    _event(_chunk({"tool_calls": [_call(0, "shell")]})),
    _event(_chunk({"tool_calls": [_call(0, None, '{"command":"rm -rf /tmp/x"}')]})),
    _event(_chunk({"tool_calls": [_call(1, "edit", '{"path":"a.py"}')]})),
    _event(_chunk(finish="tool_calls")),
    b"data: [DONE]\n\n",
])


def test_withheld_calls_vanish_at_any_split_and_indices_are_untouched():
    for seed in range(20):
        pieces = sorted(random.Random(seed).sample(range(1, len(STREAM)), 12))
        events, filt, raw = _run(STREAM, {"edit"}, pieces=pieces)
        assert _ran(raw) == [{1: "edit"}]
        assert [(call["index"], call["function"].get("name")) for call in _calls(events)] == [(1, "edit")]
        assert events[0]["choices"][0]["delta"]["content"] == "Let me look."
        assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"  # a call was kept
        assert raw.endswith(b"data: [DONE]\n\n")
        assert filt.withheld == ["shell"] and filt.dropped == 0


def test_parallel_calls_without_an_index_keep_the_key_goose_gives_them():
    for order in (("edit", "shell"), ("shell", "edit")):
        position = order.index("edit")
        stream = (
            _event(_chunk({"tool_calls": [_call(None, name) for name in order]}))
            + _event(_chunk({"tool_calls": [_call(position, None, '{"path":"a.py"}')]}))
            + _event(_chunk(finish="tool_calls"))
        )
        events, filt, raw = _run(stream, {"edit"})
        assert [{key: call[1:3] for key, call in calls.items()} for _, calls in _goose(raw)[0]] == [
            {position: ["edit", '{"path":"a.py"}']}
        ]
        # An index appears only where a dropped call moved this one.
        assert ("index" in _calls(events)[0]) == (position == 1)
        assert filt.withheld == ["shell"]


def test_a_repeated_index_or_a_later_rename_cannot_smuggle_a_withheld_tool():
    # Goose keeps the last call for a repeated key in the first chunk.
    for order, expected in ((("edit", "shell"), []), (("shell", "edit"), [{0: "edit"}])):
        stream = _event(_chunk({"tool_calls": [_call(0, name) for name in order]})) + _event(
            _chunk({"tool_calls": [_call(0, None, '{"x":1}')]}))
        _, _, raw = _run(stream, {"edit"})
        assert _ran(raw) == expected
    # A later non-empty name renames the held call.
    stream = (
        _event(_chunk({"tool_calls": [_call(0, "edit", '{"path":')]}))
        + _event(_chunk({"tool_calls": [{"index": 0, "function": {"name": "shell", "arguments": '"ls"}'}}]}))
        + _event(_chunk({"tool_calls": [_call(0, None, "tail")]}))
        + _event(_chunk(finish="tool_calls"))
    )
    events, filt, raw = _run(stream, {"edit"})
    assert _ran(raw) == [{0: "edit"}]
    assert [call["function"]["arguments"] for call in _calls(events)] == ['{"path":']
    assert filt.withheld == ["shell"]


def test_later_deltas_follow_the_agents_rules():
    stream = (
        _event(_chunk({"tool_calls": [_call(0, "edit")]}))
        + _event(_chunk({"tool_calls": [_call(None, "shell")]}))  # no index: ignored
        + _event(_chunk({"tool_calls": [_call(3, "write")]}))  # new allowed key
        + _event(_chunk({"tool_calls": [_call(4, "shell")]}))  # new withheld key
        + _event(_chunk({"tool_calls": [{"index": 5, "function": {"name": "edit", "arguments": ""}}]}))  # no id
    )
    events, _, raw = _run(stream, {"edit", "write"})
    assert _ran(raw) == [{0: "edit", 3: "write"}]
    assert [call.get("index") for call in _calls(events)] == [0, 3]


def test_each_episode_starts_afresh():
    stream = (
        _event(_chunk({"tool_calls": [_call(0, "edit")]}, finish="tool_calls"))
        # A new episode: key 0 is unknown again, and an empty name is no allowed tool.
        + _event(_chunk({"tool_calls": [{"index": 0, "id": "b", "function": {"name": "", "arguments": ""}}]}))
        + _event(_chunk({"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": "{}"}}]}))
        + _event(_chunk(finish="tool_calls"))
        # And another, whose first chunk keys calls by position.
        + _event(_chunk({"tool_calls": [_call(None, "write")]}, finish="tool_calls"))
    )
    events, filt, raw = _run(stream, {"edit", "write"})
    assert _ran(raw) == [{0: "edit"}, {0: "write"}]
    assert events[1]["choices"][0]["delta"] == {"content": WITHHELD_NOTICE}
    assert events[3]["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "stop"}
    assert filt.withheld == []  # an empty name is not a tool


def test_an_empty_id_still_creates_a_call():
    stream = _event(_chunk({"tool_calls": [_call(0, "edit", "{}", call_id="")]}, finish="tool_calls"))
    _, _, raw = _run(stream, {"edit"})
    assert _goose(raw, {"edit"}) == ([(1, {0: ["", "edit", "{}", False]})], None)


def test_a_call_after_withheld_ones_ends_with_the_providers_episode():
    stream = (
        _event(_chunk({"tool_calls": [_call(0, "shell", "{}")]}))
        # Goose would read this as the first chunk of its own episode.
        + _event(_chunk({"tool_calls": [_call(1, "edit", "{}")]}, finish="stop"))
        + _event(_chunk({"content": "done"}))
        + b"data: [DONE]\n\n"
    )
    events, _, raw = _run(stream, {"edit"})
    assert _ran(raw) == [{1: "edit"}]
    assert events[1]["choices"][0]["finish_reason"] == "tool_calls"


def test_a_key_repeated_where_the_agent_starts_late_is_dropped():
    stream = (
        _event(_chunk({"tool_calls": [_call(0, "shell", "{}")]}))
        # The provider's stream appends here; Goose, starting afresh, would restart the call.
        + _event(_chunk({"tool_calls": [_call(1, "edit", '{"a":'), _call(1, None, "1}")]}))
        + _event(_chunk(finish="tool_calls"))
    )
    events, _, raw = _run(stream, {"edit"})
    assert _ran(raw) == []
    assert events[0]["choices"][0]["delta"] == {"content": WITHHELD_NOTICE}
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


def test_a_turn_whose_only_calls_are_withheld_gets_a_notice_where_they_were():
    events, filt, _ = _run(STREAM, {"write"})
    assert _calls(events) == []
    assert [event["choices"][0]["delta"].get("content") for event in events] == [
        "Let me look.", WITHHELD_NOTICE, None, None, None]
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert sorted(filt.withheld) == ["edit", "shell"]
    # Calls and a stop in one chunk, then the usage chunk: Goose's episode ends without a finish.
    stream = (
        _event(_chunk({"content": "Deleting it.", "tool_calls": [_call(0, "shell")]}, finish="stop"))
        + _event({"choices": [], "usage": {"total_tokens": 9}})
        + b"data: [DONE]\n\n"
    )
    events, _, raw = _run(stream, {"write"})
    assert events[0]["choices"][0] == {
        "index": 0, "delta": {"content": "Deleting it.\n\n" + WITHHELD_NOTICE}, "finish_reason": "stop"}
    assert _ran(raw) == []


def test_only_the_first_choice_can_carry_calls():
    event = {"choices": [
        {"index": 0, "delta": {"tool_calls": [_call(0, "edit")]}, "finish_reason": None},
        {"index": 1, "delta": {"tool_calls": [_call(0, "shell"), _call(1, "edit")]}, "finish_reason": "tool_calls"},
    ]}
    events, filt, _ = _run(_event(event), {"edit"})
    assert events[0]["choices"][0]["delta"]["tool_calls"] == [_call(0, "edit")]
    assert events[0]["choices"][1] == {"index": 1, "delta": {}, "finish_reason": "stop"}
    assert filt.withheld == ["shell"]


def test_structs_written_as_arrays_are_dropped():
    shell = _call(0, "shell", "{}")
    for line in (
        json.dumps({"choices": [[{"tool_calls": [shell]}, 0, "tool_calls"]]}),  # a choice
        json.dumps({"choices": [{"index": 0, "delta": [None, None, [shell], None, None, None], "finish_reason": "tool_calls"}]}),
        json.dumps([[{"index": 0, "delta": {"tool_calls": [shell]}, "finish_reason": "tool_calls"}]]),  # the chunk
    ):
        stream = b"data: " + line.encode() + b"\n\ndata: [DONE]\n\n"
        filt = SseToolCallFilter({"edit"})
        raw = filt.feed(stream) + filt.flush()
        assert raw == b"\ndata: [DONE]\n\n" and filt.dropped == 1
    # A call's function as [name, arguments] would rename the held call.
    stream = (
        _event(_chunk({"tool_calls": [_call(0, "edit", '{"path":"a"}')]}))
        + _event(_chunk({"tool_calls": [{"index": 0, "function": ["shell", '{"command":"id"}']}]}))
        + _event(_chunk(finish="tool_calls"))
    )
    _, filt, raw = _run(stream, {"edit"})
    assert [calls for _, calls in _goose(raw, {"edit"})[0]] == [{0: ["call-edit", "edit", '{"path":"a"}', False]}]
    assert b"shell" not in raw and filt.dropped == 1


def test_lines_are_read_as_goose_reads_them():
    shell_chunk = json.dumps(_chunk({"tool_calls": [_call(0, "shell", "{}")]}, finish="tool_calls")).encode()
    # A bare CR: Goose trims it away and reads the call; SSE readers split the line there.
    for stream in (b"data:\r" + shell_chunk + b"\ndata: [DONE]\n\n",
                   b"event: x\rdata: " + shell_chunk + b"\n\n"):
        _, filt, raw = _run(stream, {"edit"})
        assert b"shell" not in raw and filt.dropped == 1 and _ran(raw) == []
    assert _ran(b"data:\r" + shell_chunk + b"\ndata: [DONE]\n\n") == [{0: "shell"}]  # unfiltered
    # [DONE] after Unicode whitespace, and twice in one event, still ends the stream.
    edit = _event(_chunk({"tool_calls": [_call(0, "edit")]}))
    for done in ("data: [DONE] \n\n".encode(), b"data: [DONE]\ndata: [DONE]\n\n"):
        _, filt, raw = _run(edit + done + _event(_chunk(finish="tool_calls")), {"edit"})
        assert raw.endswith(done + _event(_chunk(finish="tool_calls"))) and _ran(raw) == []
    # Lines are handled as they complete, whatever separates events; CRLF survives a rewrite.
    filt = SseToolCallFilter({"edit"})
    assert filt.feed(b'data: {"choices":[]}\n') == b'data: {"choices":[]}\n'
    assert filt.feed(b"\r\n") == b"\r\n"
    out = filt.feed(b"data: " + json.dumps(_chunk({"tool_calls": [_call(0, "shell")]})).encode() + b"\r\n")
    assert out.endswith(b"\r\n") and b"shell" not in out
    # Unreadable data, and JSON split over two data lines, which Goose cannot read either.
    for stream in (b'data: {"choices": [{"delta": {"tool_calls": [\n\n',
                   b'data: {"choices":[{"index":0,\ndata: "delta":{"content":"hi"}}]}\n\n'):
        filt = SseToolCallFilter({"edit"})
        assert filt.feed(stream) + filt.flush() == b"\n" and filt.dropped == stream.count(b"data:")
    comment_and_done = _event(_chunk({"content": "hi"})) + b": keep-alive\n\n" + b"data: [DONE]\n\n"
    filt = SseToolCallFilter({"edit"})
    assert filt.feed(comment_and_done) + filt.flush() == comment_and_done  # untouched byte for byte


def test_a_rewritten_event_survives_a_lone_surrogate():
    # Half an emoji, as JS serialisers emit it. Goose rejects the escape; the filter must not crash.
    call = json.dumps(_call(0, "shell")).encode()
    stream = b'data: {"choices":[{"index":0,"delta":{"content":"\\ud83d","tool_calls":[' + call + b"]}}]}\n\n"
    events, _, raw = _run(stream, {"edit"})
    assert raw.isascii() and b"shell" not in raw
    assert events[0]["choices"][0]["delta"]["content"].startswith("\ud83d")


def test_legacy_function_calls_are_filtered_too():
    stream = (
        _event(_chunk({"function_call": {"name": "shell", "arguments": ""}}))
        + _event(_chunk({"function_call": {"arguments": "{}"}}))
        + _event(_chunk(finish="function_call"))
    )
    events, filt, _ = _run(stream, {"edit"})
    assert all("function_call" not in event["choices"][0]["delta"] for event in events)
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert filt.withheld == ["shell"]


# --- random streams ---------------------------------------------------------------------------


def _random_entry(rng: random.Random) -> dict:
    entry = {"function": {"arguments": rng.choice(["", "{", "}", "x"])}}
    if rng.random() < 0.8:
        entry["index"] = rng.randint(0, 2)
    if rng.random() < 0.5:
        entry["id"] = rng.choice(["", "a", "b"])
    name = rng.choice(["edit", "write", "shell", "", None])
    if name is not None:
        entry["function"]["name"] = name
    return entry


def _odd(rng: random.Random, chunk: dict) -> str:
    """The chunk in a shape only a non-conforming provider sends."""
    choice = (chunk.get("choices") or [{}])[0]
    delta, finish = choice.get("delta", {}), choice.get("finish_reason")
    call = dict((delta.get("tool_calls") or [_random_entry(rng)])[0])
    roll = rng.randrange(9)
    if roll == 0:
        return json.dumps([chunk.get("choices", [])])
    if roll == 1:
        return json.dumps({"choices": [[delta, 0, finish]]})
    if roll == 2:
        return json.dumps({"choices": [{"delta": [None, None, [call], None, None, None], "finish_reason": finish}]})
    if roll == 3:
        call["function"] = [call["function"].get("name"), call["function"].get("arguments")]
    elif roll == 4:
        return json.dumps({"choices": [{"index": 0, "delta": None}]})
    elif roll == 5:
        call["index"] = rng.choice([True, 1.0, "0", 2**40])
    elif roll == 6:
        call.pop("function")
    elif roll == 7:
        return json.dumps(rng.choice([{"error": {"message": "x"}}, {"object": "error"}, {"status": 500}, {"detail": "x"}]))
    else:
        return json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}).replace(
            '"index": 0, "delta"', '"index": -0, "delta"')
    return json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": finish}]})


def _framed(rng: random.Random, data: str) -> bytes:
    roll, data = rng.random(), data.encode()
    if roll < 0.8:
        return b"data: " + data + b"\n\n"
    if roll < 0.87:
        return b"data: " + data + b"\r\n\r\n"
    if roll < 0.9:
        return b"data:" + data + b"\n"
    if roll < 0.92:
        return b"data: " + data + b"\n\r\n"
    if roll < 0.94:
        return b"data:\r" + data + b"\n\n"  # Goose trims the CR; SSE readers split on it
    if roll < 0.96:
        return b"event: x\r" + b"data: " + data + b"\n\n"
    cut = rng.randrange(1, len(data)) if len(data) > 1 else 1
    return b"data: " + data[:cut] + b"\ndata: " + data[cut:] + b"\n\n"


def _random_stream(rng: random.Random, odd: float = 0.06) -> bytes:
    out = []
    for _ in range(rng.randint(1, 7)):
        roll = rng.random()
        if roll < 0.05:
            out.append(rng.choice([b"data: [DONE]\n\n", "data: [DONE] \n\n".encode(), b"data:\n\n", b": ok\n\n"]))
            continue
        if roll < 0.1:
            chunk = {"choices": [], "usage": {"total_tokens": 1}}
        else:
            delta = {"content": "text"} if rng.random() < 0.2 else {}
            if rng.random() < 0.85:
                delta["tool_calls"] = [_random_entry(rng) for _ in range(rng.randint(1, 3))]
            finish = rng.choice([None, None, None, "", "stop", "tool_calls", "length"])
            chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if rng.random() < 0.05:
                chunk["choices"].append({"index": 1, "delta": {"tool_calls": [_call(0, "shell")]}, "finish_reason": None})
        out.append(_framed(rng, _odd(rng, chunk) if rng.random() < odd else json.dumps(chunk)))
    return b"".join(out)


def _repeats_a_key(stream: bytes) -> bool:
    for chunk in _chunks(stream):
        choices = chunk.get("choices")
        delta = choices[0].get("delta") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        calls = delta.get("tool_calls") if isinstance(delta, dict) else None
        keys = [call.get("index") for call in calls or () if isinstance(call, dict) and call.get("index") is not None]
        if len(keys) != len(set(keys)):
            return True
    return False


def check_random_stream(seed: int, allowlist=frozenset({"edit", "write"}), odd: float = 0.06) -> tuple[bool, bool]:
    """Assert the filter's guarantees on one random stream; return (compared, checked for losses)."""
    rng = random.Random(seed)
    stream = _random_stream(rng, odd)
    cuts = sorted(rng.sample(range(1, len(stream)), min(4, len(stream) - 1)))
    filt = SseToolCallFilter(allowlist)
    raw = b"".join(filt.feed(stream[a:b]) for a, b in zip([0, *cuts], [*cuts, len(stream)])) + filt.flush()
    # Safety, on every stream: nothing Goose reads from the output names a withheld tool, no
    # struct reaches it as an array (_goose would raise), and every call it runs is allowed.
    for chunk in _chunks(raw):
        for choice in chunk.get("choices") or ():
            for entry in choice.get("delta", {}).get("tool_calls") or ():
                name = entry["function"].get("name")
                assert not name or tool_allowed(name, allowlist), seed
    agent, _ = _goose(raw, allowlist)
    assert all(tool_allowed(call[1], allowlist) and not call[3] for _, calls in agent for call in calls.values()), seed
    # Fidelity, where the filter read every line and Goose reads the provider's stream as modelled:
    # each call Goose runs is the provider stream's own, unless that stream named it as a withheld tool.
    try:
        provider, stopped = _goose(stream, allowlist)
    except _Arrays:
        return False, False
    if filt.dropped:
        return False, False
    theirs = dict(provider)
    for end, calls in agent:
        if stopped is None or (end != "end" and end < stopped):
            for key, call in calls.items():
                other = theirs[end][key]
                assert other[0] == call[0] and (other[3] or other[1:3] == call[1:3]), seed
    if stopped is not None or _repeats_a_key(stream):
        return True, False
    ours = dict(agent)
    for end, calls in provider:
        for key, call in calls.items():
            assert call[3] or ours.get(end, {}).get(key) == call, seed  # no allowed call lost
    return True, True


def test_random_streams_keep_the_agent_in_step_with_the_provider():
    results = [check_random_stream(seed) for seed in range(3000)]
    assert sum(compared for compared, _ in results) > 1500
    assert sum(lossless for _, lossless in results) > 800


# --- non-streamed bodies and requests ---------------------------------------------------------


def test_non_streamed_bodies_are_filtered_in_place():
    body = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "edit", "arguments": "{}"}},
    ]}, "finish_reason": "tool_calls"}]}
    assert filter_chat_completion_body(body, {"edit"}) == ["shell"]
    assert [call["id"] for call in body["choices"][0]["message"]["tool_calls"]] == ["b"]
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert filter_chat_completion_body(body, set()) == ["edit"]
    message = body["choices"][0]["message"]
    assert "tool_calls" not in message and message["content"] == WITHHELD_NOTICE
    assert body["choices"][0]["finish_reason"] == "stop"


def test_request_filter_drops_fields_providers_reject_without_tools():
    body = {"tools": [{"type": "function", "function": {"name": "shell"}}],
            "tool_choice": "auto", "parallel_tool_calls": True}
    restrict_chat_completion_tools(body, set())
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & set(body)


def test_request_filter_narrows_an_allowed_tools_choice():
    tools = [{"type": "function", "function": {"name": name}} for name in ("shell", "edit")]
    body = {"tools": list(tools), "tool_choice": {"type": "allowed_tools", "allowed_tools": {
        "mode": "auto", "tools": [{"type": "function", "function": {"name": "shell"}},
                                  {"type": "function", "function": {"name": "edit"}}]}}}
    restrict_chat_completion_tools(body, {"edit"})
    assert [item["function"]["name"] for item in body["tool_choice"]["allowed_tools"]["tools"]] == ["edit"]
    body = {"tools": list(tools), "tool_choice": {"type": "allowed_tools", "allowed_tools": {
        "mode": "auto", "tools": [{"type": "function", "function": {"name": "shell"}}]}}}
    restrict_chat_completion_tools(body, {"edit"})
    assert "tool_choice" not in body
