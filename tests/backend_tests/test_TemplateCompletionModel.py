from unittest.mock import MagicMock, patch

import pytest
import torch
from langchain_core.prompts import PromptTemplate

from backend.completion.TemplateCompletionModel import (
    StopSequenceCriteria,
    TemplateCompletionModel,
)
from Code4meV2Config import Code4meV2Config  # Adjust import path accordingly


@pytest.fixture
def dummy_config():
    return Code4meV2Config(
        TEST_MODE=True,
        SERVER_VERSION_ID=1,
        SERVER_HOST="127.0.0.1",
        SERVER_PORT=8000,
        AUTHENTICATION_TOKEN_EXPIRES_IN_SECONDS=3600,
        SESSION_TOKEN_EXPIRES_IN_SECONDS=3600,
        TOKEN_HOOK_ACTIVATION_IN_SECONDS=60,
        DEFAULT_MAX_REQUEST_RATE_PER_HOUR={"*": 100},
        DB_HOST="localhost",
        DB_PORT=5432,
        DB_USER="user",
        DB_PASSWORD="pass",
        DB_NAME="testdb",
        PGADMIN_HOST="localhost",
        PGADMIN_PORT=5050,
        PGADMIN_DEFAULT_EMAIL="admin@test.com",
        PGADMIN_DEFAULT_PASSWORD="adminpass",
        WEBSITE_HOST="localhost",
        WEBSITE_PORT=3000,
        REACT_APP_GOOGLE_CLIENT_ID="fake-google-client-id",
        REDIS_HOST="localhost",
        REDIS_PORT=6379,
        CELERY_BROKER_HOST="localhost",
        CELERY_BROKER_PORT=5672,
        PRELOAD_MODELS=[],
        EMAIL_HOST="smtp.test.com",
        EMAIL_PORT=1025,
        EMAIL_USERNAME="emailuser",
        EMAIL_PASSWORD="emailpass",
        EMAIL_USE_TLS=False,
        EMAIL_FROM="noreply@test.com",
        VERIFICATION_URL="http://localhost/verify",
    )


@pytest.fixture
def mock_model_components():
    with patch(
        "backend.completion.TemplateCompletionModel.AutoTokenizer.from_pretrained"
    ) as tokenizer_mock, patch(
        "backend.completion.TemplateCompletionModel.AutoModelForCausalLM.from_pretrained"
    ) as model_mock:
        tokenizer = MagicMock()
        tokenizer.decode.side_effect = lambda ids, **kwargs: "decoded_text"
        tokenizer.return_tensors = "pt"

        model = MagicMock()
        model.generate.return_value = torch.tensor([[1, 2, 3]])

        tokenizer_mock.return_value = tokenizer
        model_mock.return_value = model

        yield tokenizer, model


@pytest.fixture
def model_instance(mock_model_components, dummy_config):
    tokenizer, model = mock_model_components
    prompt_template_str = "<|fim▁begin|>{prefix}<|fim▁hole|>{suffix}<|fim▁end|>"
    prompt_template = PromptTemplate.from_template(prompt_template_str)

    return TemplateCompletionModel(
        prompt_template=prompt_template,
        tokenizer=tokenizer,
        model=model,
        device=torch.device("cpu"),
        config=dummy_config,
        model_name="mock-model",
    )


def test_stop_sequence_criteria_triggers():
    tokenizer = MagicMock()
    tokenizer.decode.return_value = "Hello stop here"
    stop_criteria = StopSequenceCriteria(tokenizer, ["stop"], 0, "cpu")
    input_ids = torch.tensor([[1, 2, 3]])
    scores = torch.tensor([[0.1]])
    assert stop_criteria(input_ids, scores)


def test_stop_sequence_criteria_does_not_trigger():
    tokenizer = MagicMock()
    tokenizer.decode.return_value = "This is fine"
    stop_criteria = StopSequenceCriteria(tokenizer, ["stop"], 0, "cpu")
    input_ids = torch.tensor([[1, 2, 3]])
    scores = torch.tensor([[0.1]])
    assert not stop_criteria(input_ids, scores)
