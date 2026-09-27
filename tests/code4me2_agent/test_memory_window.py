from __future__ import annotations

import json

from code4me2_agent.adapters import MemoryWindow, _estimate_tokens, _split_units


def _system() -> dict:
    return {"role": "system", "content": "system prompt"}


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _tool_calls(*ids: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "name": "read_file", "arguments": {"path": f"{call_id}.txt"}}
            for call_id in ids
        ],
    }


def _tool_result(call_id: str, size: int = 40) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "read_file",
        "content": json.dumps({"status": "ok", "content": "x" * size}),
    }


def _history() -> list[dict]:
    return [
        _system(),
        _user("first question"),
        _tool_calls("c1", "c2"),
        _tool_result("c1", 900),
        _tool_result("c2", 900),
        _assistant("first answer"),
        _user("second question"),
        _tool_calls("c3"),
        _tool_result("c3", 900),
        _assistant("second answer"),
        _user("current question"),
    ]


def _assert_groups_intact(window: list[dict]) -> None:
    index = 0
    while index < len(window):
        message = window[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            expected = [call["id"] for call in message["tool_calls"]]
            results = []
            index += 1
            while index < len(window) and window[index].get("role") == "tool":
                results.append(window[index]["tool_call_id"])
                index += 1
            assert results == expected, f"tool group split: {expected} vs {results}"
            continue
        assert message.get("role") != "tool", "stray tool message"
        index += 1


def test_window_never_splits_groups_or_starts_with_a_tool_message():
    for max_tokens in range(1, 1200, 37):
        memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=max_tokens)
        memory.replace_messages(_history())

        window = memory.window()

        assert window[0]["role"] == "system"
        assert window[1]["role"] != "tool"
        _assert_groups_intact(window)
        assert window[-1] == _user("current question")


def test_window_always_keeps_system_and_current_turn_even_when_over_budget():
    memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=1)
    memory.replace_messages(_history())

    assert memory.window() == [_system(), _user("current question")]


def test_window_elides_old_tool_outputs_before_dropping_exchanges():
    # Large enough for the newest tool group at full size plus the oldest one
    # elided, too small for both at full size.
    memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=650)
    memory.replace_messages(_history())

    window = memory.window()

    roles = [(message["role"], message.get("content", "")[:16]) for message in window]
    # Both older exchanges survive: the oldest tool group is elided, the newer
    # one still fits at full size, and no exchange had to be dropped.
    assert ("user", "first question") in roles
    assert ("user", "second question") in roles
    tool_messages = [message for message in window if message["role"] == "tool"]
    elided = [json.loads(message["content"]).get("elided", False) for message in tool_messages]
    assert elided == [True, True, False]
    payload = json.loads(tool_messages[0]["content"])
    assert payload["status"] == "ok"
    assert "call the tool again" in payload["note"]
    assert len(payload["preview"]) <= 160
    _assert_groups_intact(window)
    # The persisted history is untouched by windowing.
    assert memory.snapshot() == _history()


def test_full_history_fits_when_budget_is_large():
    memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=20000)
    memory.replace_messages(_history())

    assert memory.window() == _history()


def test_reserve_tokens_shrinks_selection():
    memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=20000)
    memory.replace_messages(_history())

    assert len(memory.window(reserve_tokens=0)) == len(_history())
    assert memory.window(reserve_tokens=20000) == [_system(), _user("current question")]


def test_estimate_tokens_uses_chars_over_four():
    message = {"role": "user", "content": "x" * 400}
    estimate = _estimate_tokens(message)
    assert 100 <= estimate <= 120


def test_split_units_groups_tool_results_and_drops_strays():
    units = _split_units(
        [
            _tool_result("stray"),
            _user("q"),
            _tool_calls("a", "b"),
            _tool_result("a"),
            _tool_result("b"),
            _assistant("done"),
        ]
    )
    assert [len(unit) for unit in units] == [1, 3, 1]


def test_replace_messages_repairs_orphaned_tool_calls_from_legacy_snapshots():
    memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=20000)
    memory.replace_messages(
        [
            _system(),
            _user("do it"),
            _tool_calls("orphan"),
            _user("next"),
            _tool_result("ghost"),
        ]
    )

    snapshot = memory.snapshot()
    assert [message["role"] for message in snapshot] == ["system", "user", "assistant", "tool", "user"]
    repaired = json.loads(snapshot[3]["content"])
    assert snapshot[3]["tool_call_id"] == "orphan"
    assert repaired["status"] == "cancelled"
    assert "interrupted" in repaired["error"]


def test_persisted_history_is_bounded():
    memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=100)
    memory.ensure_system_message("system")
    for round_number in range(60):
        memory.append(_user(f"question {round_number}"))
        memory.append(_tool_calls(f"c{round_number}"))
        memory.append(_tool_result(f"c{round_number}", 300))
        memory.append(_assistant(f"answer {round_number}"))

    assert memory.estimated_tokens() <= 6 * 100 + _estimate_tokens(_system()) + 150
    snapshot = memory.snapshot()
    assert snapshot[0]["role"] == "system"
    assert snapshot[-1] == _assistant("answer 59")
    _assert_groups_intact(snapshot[1:])


def test_last_messages_strategy_counts_messages_but_keeps_groups():
    memory = MemoryWindow(strategy="last_messages", max_messages=3, max_tokens=20000)
    memory.replace_messages(
        [
            _system(),
            _user("q1"),
            _tool_calls("c1", "c2"),
            _tool_result("c1"),
            _tool_result("c2"),
            _assistant("a1"),
            _user("q2"),
        ]
    )

    assert memory.window() == [_system(), _assistant("a1"), _user("q2")]


def test_oversize_newest_unit_is_elided_oldest_first_then_entirely():
    from code4me2_agent.adapters import _shrink_protected

    group = [_tool_calls("a", "b", "c"), _tool_result("a", 2000), _tool_result("b", 2000), _tool_result("c", 2000)]
    protected = [[_user("current")], group]

    # Eliding stops as soon as the turn fits: one result at 1500, two at 800.
    partly = _shrink_protected(protected, budget=1500)
    assert [json.loads(m["content"]).get("elided", False) for m in partly[-1] if m["role"] == "tool"] == [
        True,
        False,
        False,
    ]
    shrunk = _shrink_protected(protected, budget=900)
    elided = [json.loads(m["content"]).get("elided", False) for m in shrunk[-1] if m["role"] == "tool"]
    assert elided == [True, True, False]
    assert shrunk[0] == [_user("current")]

    fully = _shrink_protected(protected, budget=200)
    assert [json.loads(m["content"]).get("elided", False) for m in fully[-1] if m["role"] == "tool"] == [
        True,
        True,
        True,
    ]
    # Originals are never mutated.
    assert "elided" not in group[1]["content"]


def test_window_with_oversize_batch_keeps_the_newest_result_intact():
    memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=900)
    memory.replace_messages(
        [
            _system(),
            _user("current"),
            _tool_calls("a", "b", "c"),
            _tool_result("a", 2000),
            _tool_result("b", 2000),
            _tool_result("c", 2000),
        ]
    )

    window = memory.window()

    tool_messages = [m for m in window if m["role"] == "tool"]
    assert [json.loads(m["content"]).get("elided", False) for m in tool_messages] == [True, True, False]
    _assert_groups_intact(window)
