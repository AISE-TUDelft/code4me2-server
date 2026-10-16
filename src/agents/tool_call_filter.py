"""Withhold model tool calls to tools outside an enforced selection.

Offering the model only the selected tools is not enough on its own: a model
can still emit a call to a tool it was never offered (its agent's system prompt
may describe it), and an agent such as Goose executes a call to any tool its
extensions provide. Where a tool selection is enforced (the research inference
gateway, for a release that declares it), the response is filtered too, so the
agent never holds a call to a withheld tool.

Both Chat Completions shapes are handled. A JSON body simply loses its
withheld calls. A streamed response is filtered against the way Goose 1.51
assembles calls (``goose-provider-types`` ``formats/openai.rs``). Goose reads
only the first choice and collects calls in episodes:

* a chunk with tool calls starts one: each entry with an ``id`` and a ``name``
  creates a call keyed by its ``index``, else its position (the last entry for
  a key wins); other entries are ignored. A ``tool_calls`` or ``length``
  finish ends the episode at once;
* otherwise later chunks continue it until one finishes (any reason) or has no
  choices. Their entries need an ``index``: on a known key they append
  arguments and a non-empty ``name`` renames the call; an unknown key is
  created by an entry with an ``id`` and a ``name``;
* the calls held when the episode ends are run.

The filter follows the provider's stream by these rules and forwards only what
keeps the agent in step with it. A key the stream creates or renames as a tool
outside the selection (an empty name included) is poisoned for the episode:
that entry and every later one for the key are dropped. So the agent starts an
episode only with an allowed call, and reads each forwarded chunk as the
stream's own:

* an index-less first-chunk entry behind a dropped one gets its position as
  ``index``, the key Goose gives it anyway;
* after every earlier call of an episode was withheld, the agent's first chunk
  is a later one: a key repeated within it is dropped, and a finish other than
  ``tool_calls`` or ``length`` becomes ``tool_calls``, so both episodes end
  together.

A rename of a forwarded call to a withheld tool is dropped with the rest of its
key; the agent keeps the allowed call. When a call is withheld and the agent
holds none, that chunk carries a short notice (once per episode), and a
``tool_calls`` finish of such an episode becomes ``stop``. Calls in any other
choice are removed.

The stream is read as Goose reads it (``LinesCodec``): line by line, split on
LF with one trailing CR dropped, each ``data:`` line a chunk of its own, its
value trimmed of Unicode whitespace. What only Goose, or only an SSE reader,
could take as a chunk is dropped (fail closed): a line with any other CR, a
``data:`` line that is not valid JSON or UTF-8, and a chunk in which a choice, a
delta, a call or its ``function`` is not a JSON object (Goose's deserializer
also reads those from arrays). Lines that need no change pass through byte for
byte.
"""

from __future__ import annotations

import json
from typing import AbstractSet, Any, Optional

__all__ = ["WITHHELD_NOTICE", "SseToolCallFilter", "filter_chat_completion_body", "tool_allowed"]

#: Told to the participant (and the model, in its history) when a turn's only
#: tool calls were withheld, so the turn does not end in silence.
WITHHELD_NOTICE = "[Code4Me study] The model tried to use a tool this study does not allow; it was not run."

#: What Rust's ``str::trim`` removes (Unicode ``White_Space``); Goose trims ``data:`` values with it.
_TRIMMED = "\t\n\x0b\x0c\r \x85\xa0\u1680" + "".join(map(chr, range(0x2000, 0x200B))) + "\u2028\u2029\u202f\u205f\u3000"


def tool_allowed(name: Any, allowlist: AbstractSet[str]) -> bool:
    """Whether ``name`` is in ``allowlist`` (``mcp__*`` admits every ``mcp__`` tool)."""
    return (
        isinstance(name, str)
        and bool(name)
        and (name in allowlist or ("mcp__*" in allowlist and name.startswith("mcp__")))
    )


def _function_name(call: Any) -> Optional[str]:
    function = call.get("function") if isinstance(call, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    return name if isinstance(name, str) else None


def filter_chat_completion_body(body: Any, allowlist: AbstractSet[str]) -> list[str]:
    """Filter a non-streamed Chat Completions response in place; return the withheld names."""
    withheld: list[str] = []
    if not isinstance(body, dict):
        return withheld
    for choice in body.get("choices") or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        removed_all = False
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            kept = [call for call in calls if tool_allowed(_function_name(call), allowlist)]
            withheld.extend(str(_function_name(call)) for call in calls if call not in kept)
            if kept:
                message["tool_calls"] = kept
            else:
                message.pop("tool_calls", None)
                removed_all = bool(calls)
                if choice.get("finish_reason") == "tool_calls":
                    choice["finish_reason"] = "stop"
        legacy = message.get("function_call")
        if isinstance(legacy, dict) and not tool_allowed(legacy.get("name"), allowlist):
            withheld.append(str(legacy.get("name")))
            message.pop("function_call", None)
            removed_all = True
            if choice.get("finish_reason") == "function_call":
                choice["finish_reason"] = "stop"
        if removed_all and not message.get("content"):
            message["content"] = WITHHELD_NOTICE
    return withheld


def _not_json(constant: str) -> Any:
    raise ValueError(f"{constant} is not JSON")


def _finite(literal: str) -> float:
    number = float(literal)
    if number in (float("inf"), float("-inf")):
        raise ValueError(f"{literal} is out of range")
    return number


def _readable_shape(event: dict) -> bool:
    """Whether every choice, delta, call and ``function`` in the chunk is a JSON object."""
    if "choices" not in event:
        return True
    choices = event["choices"]
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get("delta", {}), dict):
            return False
        calls = choice.get("delta", {}).get("tool_calls")
        if calls is not None and not (
            isinstance(calls, list)
            and all(isinstance(call, dict) and isinstance(call.get("function"), dict) for call in calls)
        ):
            return False
    return True


#: A finish that ends an episode in its first chunk (any finish ends a later one).
_ENDS_AT_ONCE = ("tool_calls", "length")


def _index(call: dict) -> Optional[int]:
    index = call.get("index")
    return index if isinstance(index, int) and not isinstance(index, bool) else None


def _creates(call: Any) -> bool:
    """Whether Goose creates a call from this entry: ``id`` and ``name`` are present."""
    return isinstance(call, dict) and isinstance(call.get("id"), str) and _function_name(call) is not None


class _Episode:
    """The first choice's current episode, followed on the provider's stream."""

    def __init__(self, upstream: bool = False) -> None:
        self.upstream = upstream  # the provider's stream is inside an episode
        self.agent = False  # so is the agent: it holds at least one call
        self.held: dict[int, str] = {}  # the agent's calls: key -> name
        self.poisoned: set[int] = set()
        self.withheld = False
        self.noticed = False


class SseToolCallFilter:
    """Stateful filter for one streamed Chat Completions response (see the module doc)."""

    def __init__(self, allowlist: AbstractSet[str]) -> None:
        self._allowlist = allowlist
        self._buffer = b""
        self._episode = _Episode()
        self._legacy_allowed: Optional[bool] = None
        self.withheld: list[str] = []
        self.dropped = 0  # lines not forwarded because they could not be judged

    def feed(self, chunk: bytes) -> bytes:
        """Accept upstream bytes; return the filtered bytes of every complete line."""
        searched = len(self._buffer)  # the buffer holds no LF before this chunk
        self._buffer += chunk
        out: list[bytes] = []
        start = 0
        while (end := self._buffer.find(b"\n", max(start, searched))) >= 0:
            out.append(self._line(self._buffer[start : end + 1]))
            start = end + 1
        self._buffer = self._buffer[start:]
        return b"".join(out)

    def flush(self) -> bytes:
        """The filtered remainder once the upstream stream has ended (Goose reads it as a last line)."""
        rest, self._buffer = self._buffer, b""
        return self._line(rest) if rest else b""

    def _line(self, raw: bytes) -> bytes:
        body = raw[:-1] if raw.endswith(b"\n") else raw
        body = body[:-1] if body.endswith(b"\r") else body
        ending = raw[len(body) :]
        if b"\r" in body:
            self.dropped += 1  # Goose and SSE readers split this line differently
            return b""
        if not body.startswith(b"data:"):
            return raw
        try:
            value: Optional[str] = body[5:].decode("utf-8").strip(_TRIMMED)
        except UnicodeDecodeError:
            value = None
        if value in ("", "[DONE]"):
            return raw  # carries no chunk
        try:
            event = json.loads(value, parse_constant=_not_json, parse_float=_finite) if value is not None else None
        except (ValueError, RecursionError):
            event = None
        if not isinstance(event, dict) or not _readable_shape(event):
            self.dropped += 1
            return b""
        if not self._filter_chunk(event):
            return raw
        rewritten = json.dumps(event, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return b"data: " + rewritten.encode("ascii") + ending

    def _filter_chunk(self, event: dict) -> bool:
        choices = event.get("choices")
        if not isinstance(choices, list):
            return False
        if not choices:
            self._episode = _Episode()  # a chunk without choices ends the episode
            return False
        changed = False
        for choice in choices[1:]:
            changed |= self._drop_calls(choice)
        if isinstance(choices[0], dict):
            changed |= self._filter_first_choice(choices[0])
        return changed

    def _refuse(self, key: int, name: str) -> None:
        self._episode.poisoned.add(key)
        self._episode.withheld = True
        if name:
            self.withheld.append(name)

    def _first_chunk_calls(self, calls: list) -> list:
        episode = self._episode
        keys: list[Optional[int]] = []
        final: dict[int, str] = {}
        for position, call in enumerate(calls):
            key = None
            if _creates(call):
                index = _index(call)
                key = position if index is None else index
                final[key] = _function_name(call)
            keys.append(key)
        for key, name in final.items():
            if tool_allowed(name, self._allowlist):
                episode.held[key] = name
            else:
                self._refuse(key, name)
        kept: list = []
        for position, (call, key) in enumerate(zip(calls, keys)):
            if key is None or key in episode.poisoned or not tool_allowed(_function_name(call), self._allowlist):
                continue
            if _index(call) is None and len(kept) != position:
                call = {**call, "index": position}
            kept.append(call)
        return kept

    def _later_chunk_calls(self, calls: list) -> list:
        episode = self._episode
        agents_first = not episode.agent
        created: set[int] = set()
        kept: list = []
        for call in calls:
            index = _index(call) if isinstance(call, dict) else None
            if index is None or index in episode.poisoned:
                continue
            name = _function_name(call)
            if agents_first and index in created:
                # The stream continues this call here; the agent would start it over.
                if name and not tool_allowed(name, self._allowlist):
                    self._refuse(index, name)
                episode.poisoned.add(index)
                episode.held.pop(index)
                continue
            if index in episode.held:
                if name and not tool_allowed(name, self._allowlist):
                    self._refuse(index, name)
                    continue
                if name:
                    episode.held[index] = name
            elif _creates(call):
                if not tool_allowed(name, self._allowlist):
                    self._refuse(index, name)
                    continue
                episode.held[index] = name
                created.add(index)
            else:
                continue  # the agent ignores it
            kept.append(call)
        return [call for call in kept if _index(call) not in episode.poisoned]

    def _filter_first_choice(self, choice: dict) -> bool:
        episode = self._episode
        changed = False
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else None
        finish = choice.get("finish_reason")
        finish = finish if isinstance(finish, str) and finish else None  # Goose reads "" as none
        ends = episode.upstream and finish is not None
        calls = delta.get("tool_calls") if delta is not None else None
        if isinstance(calls, list) and calls:
            if episode.upstream:
                kept = self._later_chunk_calls(calls)
                if kept and not episode.agent and finish not in (None, *_ENDS_AT_ONCE):
                    choice["finish_reason"] = "tool_calls"
                    changed = True
            else:
                self._episode = episode = _Episode(upstream=True)
                kept = self._first_chunk_calls(calls)
                ends = finish in _ENDS_AT_ONCE
            episode.agent = episode.agent or bool(kept)
            if len(kept) != len(calls):
                changed = True
                if kept:
                    delta["tool_calls"] = kept
                else:
                    delta.pop("tool_calls")
        notice = False
        legacy = delta.get("function_call") if delta is not None else None
        if isinstance(legacy, dict):
            if self._legacy_allowed is None:
                self._legacy_allowed = tool_allowed(legacy.get("name"), self._allowlist)
                if not self._legacy_allowed:
                    self.withheld.append(str(legacy.get("name")))
            elif legacy.get("name") and not tool_allowed(legacy.get("name"), self._allowlist):
                self._legacy_allowed = False
                self.withheld.append(str(legacy.get("name")))
            if not self._legacy_allowed:
                delta.pop("function_call")
                changed = True
        if finish == "function_call" and self._legacy_allowed is False:
            choice["finish_reason"] = "stop"
            notice = True
        if episode.withheld and not episode.agent and not episode.noticed:
            # The agent reads this chunk as plain content: tell the participant now, since
            # the episode may end without a finish (a usage chunk, [DONE] or the end).
            episode.noticed = notice = True
        if ends:
            if not episode.agent and episode.withheld and finish == "tool_calls":
                choice["finish_reason"] = "stop"
                changed = True
            self._episode = _Episode()
        if notice:
            if delta is None:
                delta = choice["delta"] = {}
            content = delta.get("content")
            if isinstance(content, list):
                content.append({"type": "text", "text": ("\n\n" if content else "") + WITHHELD_NOTICE})
            elif isinstance(content, str) and content:
                delta["content"] = content + "\n\n" + WITHHELD_NOTICE
            elif not content:
                delta["content"] = WITHHELD_NOTICE
            changed = True
        return changed

    def _drop_calls(self, choice: Any) -> bool:
        """Goose reads only the first choice: calls in any other are removed."""
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if not isinstance(delta, dict) or not {"tool_calls", "function_call"} & delta.keys():
            return False
        calls = delta.pop("tool_calls", None)
        legacy = delta.pop("function_call", None)
        names = [_function_name(call) for call in calls] if isinstance(calls, list) else []
        names.append(legacy.get("name") if isinstance(legacy, dict) else None)
        self.withheld.extend(
            name for name in names if isinstance(name, str) and name and not tool_allowed(name, self._allowlist)
        )
        if choice.get("finish_reason") in ("tool_calls", "function_call"):
            choice["finish_reason"] = "stop"
        return True
