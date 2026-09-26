"""Unit tests for the OpenAI-compatible provider-backed classic models.

No database or network access: ``httpx.post`` is monkeypatched, and the local
model modules are replaced in ``sys.modules`` so a selection regression can never
download a real HuggingFace model.
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError

from backend import completion
from backend.completion import CompletionModels
from backend.completion.OpenAICompatibleModel import (
    OpenAICompatibleChatModel,
    OpenAICompatibleCompletionModel,
    OpenAICompatibleConfig,
)

PROMPT_TEMPLATES = {
    "fim_template": {
        "single_file_template": "<fim_begin>{prefix}<fim_hole>{suffix}<fim_end>",
        "multi_file_template": (
            "{multi_file_context}#{file_name}\n{prefix}<fim_hole>{suffix}<fim_end>"
        ),
    },
    "file_separator": "#{file_name}\n",
    "stop_tokens": ["\n\n"],
}


def _response(status_code=200, payload=None, text=""):
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = payload if payload is not None else {}
    return response


@pytest.fixture
def post_recorder(monkeypatch):
    """Install a fake ``httpx.post``; return a callable that records one call."""

    def install(response):
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
            return response

        monkeypatch.setattr(httpx, "post", fake_post)
        return calls

    return install


@pytest.fixture
def local_models_patched(monkeypatch):
    """Replace the local model modules so nothing can load a real HF model."""
    local_chat_module = MagicMock()
    local_template_module = MagicMock()
    monkeypatch.setitem(sys.modules, "backend.completion.ChatCompletionModel", local_chat_module)
    monkeypatch.setitem(
        sys.modules, "backend.completion.TemplateCompletionModel", local_template_module
    )
    return local_chat_module, local_template_module


def test_provider_module_imports_no_torch_or_transformers():
    source = Path(completion.OpenAICompatibleModel.__file__).read_text(encoding="utf-8")
    forbidden = re.compile(r"^\s*(?:import|from)\s+(?:torch|transformers)\b", re.MULTILINE)
    assert forbidden.search(source) is None


def test_chat_invoke_posts_payload_and_maps_response(post_recorder, monkeypatch):
    monkeypatch.setenv("CODE4ME_TEST_PROVIDER_KEY", "sk-test-secret")
    calls = post_recorder(_response(200, {"choices": [{"message": {"content": "  generated  "}}]}))
    model = OpenAICompatibleChatModel(
        model_name="vendor-instruct-row",
        config=OpenAICompatibleConfig(
            base_url="https://provider.test/v1/",
            api_key_ref="CODE4ME_TEST_PROVIDER_KEY",
            provider_model="vendor/chat-model",
            max_new_tokens=128,
            timeout_seconds=30,
        ),
    )

    result = model.invoke(
        [
            SystemMessage(content="be brief"),
            HumanMessage(content="hello"),
            AIMessage(content="hi"),
            ToolMessage(content="tool output", tool_call_id="call-1"),
        ]
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://provider.test/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test-secret"
    assert call["timeout"] == 30
    assert call["json"]["model"] == "vendor/chat-model"
    assert call["json"]["max_tokens"] == 128
    assert call["json"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "tool output"},
    ]
    assert result["completion"] == "generated"
    assert isinstance(result["generation_time"], int)
    assert result["role"] == "assistant"
    assert result["model_name"] == "vendor-instruct-row"


def test_chat_without_api_key_ref_omits_authorization(post_recorder):
    calls = post_recorder(_response(200, {"choices": [{"message": {"content": "ok"}}]}))
    model = OpenAICompatibleChatModel(
        model_name="ollama-instruct-row",
        config=OpenAICompatibleConfig(base_url="http://localhost:11434/v1"),
    )

    model.invoke([HumanMessage(content="hi")])

    assert "Authorization" not in calls[0]["headers"]


def test_missing_api_key_env_raises_naming_variable(monkeypatch):
    monkeypatch.delenv("CODE4ME_TEST_MISSING_KEY", raising=False)

    def fail_post(*args, **kwargs):
        raise AssertionError("httpx.post must not be called without a resolved key")

    monkeypatch.setattr(httpx, "post", fail_post)
    model = OpenAICompatibleChatModel(
        model_name="vendor-instruct-row",
        config=OpenAICompatibleConfig(
            base_url="https://provider.test/v1",
            api_key_ref="CODE4ME_TEST_MISSING_KEY",
        ),
    )

    with pytest.raises(RuntimeError) as excinfo:
        model.invoke([HumanMessage(content="hi")])

    assert "CODE4ME_TEST_MISSING_KEY" in str(excinfo.value)


def test_non_2xx_raises_with_status_and_excerpt_without_key(post_recorder, monkeypatch):
    monkeypatch.setenv("CODE4ME_TEST_PROVIDER_KEY", "sk-do-not-leak")
    post_recorder(_response(status_code=502, text="upstream exploded"))
    model = OpenAICompatibleChatModel(
        model_name="vendor-instruct-row",
        config=OpenAICompatibleConfig(
            base_url="https://provider.test/v1",
            api_key_ref="CODE4ME_TEST_PROVIDER_KEY",
        ),
    )

    with pytest.raises(RuntimeError) as excinfo:
        model.invoke([HumanMessage(content="hi")])

    message = str(excinfo.value)
    assert "502" in message
    assert "upstream exploded" in message
    assert "sk-do-not-leak" not in message


def test_missing_base_url_and_unknown_keys_rejected():
    with pytest.raises(ValidationError) as excinfo:
        OpenAICompatibleConfig(provider="openai_compatible")
    assert "base_url" in str(excinfo.value)

    with pytest.raises(ValidationError):
        OpenAICompatibleConfig(base_url="https://provider.test/v1", surprise="nope")

    with pytest.raises(ValidationError):
        OpenAICompatibleConfig(base_url="   ")


def test_completion_single_file_prompt_and_chat_endpoint(post_recorder):
    calls = post_recorder(_response(200, {"choices": [{"message": {"content": "    return 1"}}]}))
    model = OpenAICompatibleCompletionModel(
        model_name="vendor-base-row",
        prompt_templates=PROMPT_TEMPLATES,
        config=OpenAICompatibleConfig(
            base_url="https://provider.test/v1",
            provider_model="vendor/fim-model",
        ),
    )

    result = model.invoke(
        {"prefix": "def f():\n    ", "suffix": "", "file_name": "main.py"},
        stop_sequences=["\nclass "],
    )

    assert calls[0]["url"] == "https://provider.test/v1/chat/completions"
    assert calls[0]["json"]["model"] == "vendor/fim-model"
    assert calls[0]["json"]["messages"][0]["role"] == "system"
    assert "code completion" in calls[0]["json"]["messages"][0]["content"]
    assert calls[0]["json"]["messages"][1] == {
        "role": "user",
        "content": "<fim_begin>def f():\n    <fim_hole><fim_end>",
    }
    assert calls[0]["json"]["stop"] == ["\nclass ", "\n\n"]
    assert result["completion"] == "    return 1"
    assert isinstance(result["generation_time"], int)
    assert result["confidence"] is None
    assert result["logprobs"] == []


def test_completion_multi_file_prompt_and_completions_endpoint(post_recorder):
    calls = post_recorder(_response(200, {"choices": [{"text": "return x"}]}))
    model = OpenAICompatibleCompletionModel(
        model_name="vendor-base-row",
        prompt_templates=PROMPT_TEMPLATES,
        config=OpenAICompatibleConfig(
            base_url="https://provider.test/v1",
            endpoint="completions",
            max_new_tokens=64,
        ),
    )

    result = model.invoke(
        {
            "prefix": "def f():\n    ",
            "suffix": "\n# end",
            "multi_file_context": {"a.py": "x = 1\n", "b.py": "y = 2\n"},
            "file_name": "main.py",
        },
        max_new_tokens=32,
    )

    expected_context = "#a.py\nx = 1\n\n#b.py\ny = 2\n\n"
    expected_prompt = f"{expected_context}#main.py\ndef f():\n    <fim_hole>\n# end<fim_end>"
    assert calls[0]["url"] == "https://provider.test/v1/completions"
    assert calls[0]["json"]["prompt"] == expected_prompt
    assert calls[0]["json"]["max_tokens"] == 32
    assert calls[0]["json"]["stop"] == ["\n\n"]
    assert result["completion"] == "return x"
    assert result["confidence"] is None
    assert result["logprobs"] == []


def test_completion_models_selects_provider_chat_model(local_models_patched):
    models = CompletionModels(MagicMock())
    params = json.dumps(
        {
            "provider": "openai_compatible",
            "base_url": "https://provider.test/v1",
            "provider_model": "vendor/chat-model",
        }
    )

    model = models.get_model(
        model_name="provider-backed-instruct-row",
        prompt_templates="{}",
        model_parameters=params,
    )

    assert isinstance(model, OpenAICompatibleChatModel)
    assert model.provider_model == "vendor/chat-model"
    # Cached under kind/base_url/provider_model, so a second lookup is the same instance.
    assert models.get_model("provider-backed-instruct-row", "{}", params) is model
    assert not local_models_patched[0].ChatCompletionModel.called


def test_completion_models_selects_provider_completion_model(local_models_patched):
    models = CompletionModels(MagicMock())
    params = json.dumps(
        {
            "provider": "openai_compatible",
            "kind": "completion",
            "base_url": "https://provider.test/v1",
        }
    )

    model = models.get_model(
        model_name="provider-backed-base-row",
        prompt_templates=json.dumps(PROMPT_TEMPLATES),
        model_parameters=params,
    )

    assert isinstance(model, OpenAICompatibleCompletionModel)
    assert model.provider_model == "provider-backed-base-row"
    assert model.prompt_templates == PROMPT_TEMPLATES
    assert not local_models_patched[1].TemplateCompletionModel.called


def test_completion_models_keeps_local_path_for_non_provider_rows(local_models_patched):
    pytest.importorskip("torch")
    models = CompletionModels(MagicMock())
    local_chat_module, _ = local_models_patched

    model = models.get_model(
        model_name="local-instruct-row-provider-test",
        prompt_templates="{}",
        model_parameters='{"max_new_tokens": 64}',
    )

    assert model is local_chat_module.ChatCompletionModel.return_value


def test_api_key_is_resolved_per_request(post_recorder, monkeypatch):
    """The key must be read at request time, not cached at construction."""
    monkeypatch.delenv("CODE4ME_TEST_LATE_KEY", raising=False)
    calls = post_recorder(_response(200, {"choices": [{"message": {"content": "ok"}}]}))
    model = OpenAICompatibleChatModel(
        model_name="vendor-instruct-row",
        config=OpenAICompatibleConfig(
            base_url="https://provider.test/v1",
            api_key_ref="CODE4ME_TEST_LATE_KEY",
        ),
    )

    monkeypatch.setenv("CODE4ME_TEST_LATE_KEY", "sk-late-secret")
    model.invoke([HumanMessage(content="hi")])

    assert calls[0]["headers"]["Authorization"] == "Bearer sk-late-secret"


def test_api_key_ref_must_be_env_var_name_without_echoing_value(post_recorder):
    calls = post_recorder(_response(200, {"choices": [{"message": {"content": "ok"}}]}))
    model = OpenAICompatibleChatModel(
        model_name="vendor-instruct-row",
        config=OpenAICompatibleConfig(
            base_url="https://provider.test/v1",
            api_key_ref="sk-pasted-secret-value",
        ),
    )

    with pytest.raises(RuntimeError) as excinfo:
        model.invoke([HumanMessage(content="hi")])

    assert "sk-pasted-secret-value" not in str(excinfo.value)
    assert calls == []


def test_error_excerpt_redacts_resolved_key(post_recorder, monkeypatch):
    monkeypatch.setenv("CODE4ME_TEST_PROVIDER_KEY", "sk-echoed-by-upstream")
    post_recorder(_response(status_code=502, text="auth header was Bearer sk-echoed-by-upstream"))
    model = OpenAICompatibleChatModel(
        model_name="vendor-instruct-row",
        config=OpenAICompatibleConfig(
            base_url="https://provider.test/v1",
            api_key_ref="CODE4ME_TEST_PROVIDER_KEY",
        ),
    )

    with pytest.raises(RuntimeError) as excinfo:
        model.invoke([HumanMessage(content="hi")])

    message = str(excinfo.value)
    assert "sk-echoed-by-upstream" not in message
    assert "[redacted]" in message


def test_malformed_provider_bodies_raise_runtime_error(post_recorder):
    model = OpenAICompatibleChatModel(
        model_name="vendor-instruct-row",
        config=OpenAICompatibleConfig(base_url="https://provider.test/v1"),
    )
    for payload in ("not-a-list", [], [{"message": "not-a-dict"}], [{"message": {}}]):
        post_recorder(_response(200, {"choices": payload}))
        if payload == [{"message": {}}]:
            assert model.invoke([HumanMessage(content="hi")])["completion"] == ""
        else:
            with pytest.raises(RuntimeError):
                model.invoke([HumanMessage(content="hi")])


def test_classic_chat_model_types_accept_provider_and_local_classes():
    assert OpenAICompatibleChatModel in completion.CLASSIC_CHAT_MODEL_TYPES
    assert completion.ChatCompletionModel in completion.CLASSIC_CHAT_MODEL_TYPES


def test_unknown_provider_value_is_rejected_without_local_fallback(local_models_patched):
    models = CompletionModels(MagicMock())
    params = json.dumps({"provider": "openai_compat", "base_url": "https://provider.test/v1"})

    model = models.get_model(
        model_name="provider-backed-instruct-row", prompt_templates="{}", model_parameters=params
    )

    assert model is None
    assert not local_models_patched[0].ChatCompletionModel.called


def test_invalid_provider_config_is_rejected_without_local_fallback(local_models_patched):
    models = CompletionModels(MagicMock())
    params = json.dumps({"provider": "openai_compatible"})  # base_url missing

    model = models.get_model(
        model_name="provider-backed-instruct-row", prompt_templates="{}", model_parameters=params
    )

    assert model is None
    assert not local_models_patched[0].ChatCompletionModel.called


def test_provider_cache_key_changes_with_config(local_models_patched):
    models = CompletionModels(MagicMock())
    base = {"provider": "openai_compatible", "base_url": "https://provider.test/v1"}

    first = models.get_model("cache-row-instruct", "{}", json.dumps(base))
    second = models.get_model(
        "cache-row-instruct", "{}", json.dumps({**base, "max_new_tokens": 7})
    )

    assert first is not None
    assert second is not None
    assert first is not second
