"""The gateway enforces BYOA tool selections without a fake agent setting."""

from agents.inference import _filter_chat_completion_tools


def _request():
    return {
        "tools": [
            {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "edit", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "shell", "parameters": {"type": "object"}}},
        ],
        "tool_choice": "auto",
    }


def test_selected_goose_tool_is_forwarded_and_other_tools_are_removed():
    body = _request()

    requested, kept, stripped = _filter_chat_completion_tools(
        body, '["read", "edit"]'
    )

    assert requested == ["read", "edit", "shell"]
    assert (kept, stripped) == (2, 1)
    assert [item["function"]["name"] for item in body["tools"]] == [
        "read", "edit"
    ]


def test_explicit_empty_profile_removes_all_tools_and_tool_choice():
    body = _request()

    _, kept, stripped = _filter_chat_completion_tools(body, "[]")

    assert (kept, stripped) == (0, 3)
    assert "tools" not in body
    assert "tool_choice" not in body


def test_legacy_profile_uses_catalogue_fallback():
    body = _request()

    _, kept, stripped = _filter_chat_completion_tools(body, None)

    assert (kept, stripped) == (3, 0)
