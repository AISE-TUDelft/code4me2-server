from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from backend.completion.ChatCompletionModel import ChatCompletionModel


@pytest.fixture
def mock_model():
    with patch(
        "backend.completion.CompletionModel.AutoTokenizer.from_pretrained"
    ) as tokenizer_mock, patch(
        "backend.completion.CompletionModel.AutoModelForCausalLM.from_pretrained"
    ) as model_mock, patch(
        "backend.completion.CompletionModel.pipeline"
    ) as pipeline_mock, patch(
        "backend.completion.CompletionModel.HuggingFacePipeline"
    ) as hf_pipeline_mock:
        # Set up mocks
        tokenizer_mock.return_value = MagicMock(pad_token=None, eos_token="</s>")
        model_mock.return_value = MagicMock(hf_device_map={"": "cuda:0"})
        pipeline_mock.return_value = MagicMock()
        hf_pipeline_mock.return_value = MagicMock(
            invoke=MagicMock(return_value="[ASSISTANT] Hello")
        )

        yield ChatCompletionModel(
            model_name="dummy-model",
            temperature=0.5,
            max_new_tokens=128,
        )


def test_model_initialization(mock_model):
    assert isinstance(mock_model, ChatCompletionModel)
    assert mock_model.model_name == "dummy-model"


def test_format_messages(mock_model):
    messages = [
        SystemMessage(content="You are a helpful assistant."),
        HumanMessage(content="Hello!"),
        AIMessage(content="Hi there!"),
    ]
    formatted = mock_model._format_messages(messages)
    assert "[SYSTEM]" in formatted
    assert "[USER]" in formatted
    assert "[ASSISTANT]" in formatted
    assert formatted.strip().endswith("[ASSISTANT]")


def test_invoke_returns_dict(mock_model):
    messages = [HumanMessage(content="What's your name?")]
    response = mock_model.invoke(messages)
    assert isinstance(response, dict)
    assert "completion" in response
    assert "generation_time" in response
    assert response["role"] == "assistant"

