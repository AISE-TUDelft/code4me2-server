from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from backend.completion.ChatCompletionModel import ChatCompletionModel


@pytest.fixture
def mock_model():
    with patch(
        "backend.completion.ChatCompletionModel.AutoTokenizer.from_pretrained"
    ) as tokenizer_mock, patch(
        "backend.completion.ChatCompletionModel.AutoModelForCausalLM.from_pretrained"
    ) as model_mock, patch(
        "backend.completion.ChatCompletionModel.pipeline"
    ) as pipeline_mock, patch(
        "backend.completion.ChatCompletionModel.HuggingFacePipeline"
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


def test_generate_stops_on_stop_sequence(mock_model):
    mock_model.model.invoke = MagicMock(
        return_value="This is a test [USER] Should stop here"
    )
    messages = [HumanMessage(content="test")]
    result = mock_model._generate(messages)
    assert "Should stop here" not in result.generations[0].message.content


def test_generate_error_handling(mock_model):
    def error_fn(*args, **kwargs):
        raise RuntimeError("Mocked failure")

    mock_model.model.invoke = error_fn
    with pytest.raises(RuntimeError, match="Mocked failure"):
        mock_model._generate([HumanMessage(content="trigger error")])


def test_invalid_input_raises_error(mock_model):
    with pytest.raises(ValueError, match="Input must be a list of messages."):
        mock_model.invoke("this is not a list")
