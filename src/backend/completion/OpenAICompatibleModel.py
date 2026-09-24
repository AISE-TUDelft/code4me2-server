"""OpenAI-compatible provider-backed models for the classic endpoints.

The classic chat (``POST /api/chat/request``) and completion
(``POST /api/completion/request``) endpoints normally run local HuggingFace
models. A ``model_name`` row can opt into an OpenAI-compatible provider
(OpenRouter, Ollama, OpenAI, Groq, vLLM) by carrying a ``model_parameters`` JSON
object with ``"provider": "openai_compatible"``; the row is then served over
HTTP instead of the local stack.

Secrets follow the repository rule used by ``src/agents/provider.py``: the
configuration stores only the *name* of the environment variable that holds the
key (``api_key_ref``). The value is read from ``os.environ`` at request time and
is never stored, logged, or embedded in an error message. There is no fallback:
a missing key variable or an invalid configuration is a hard error.

This module must stay free of torch/transformers imports so a torch-free image
can serve the classic endpoints.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Literal, Optional

import httpx
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "OpenAICompatibleChatModel",
    "OpenAICompatibleCompletionModel",
    "OpenAICompatibleConfig",
]

COMPLETION_SYSTEM_INSTRUCTION = (
    "You are a code completion engine. Continue the code at the cursor. "
    "Return only the code that belongs at the cursor, with no explanations."
)

_MAX_ERROR_EXCERPT_CHARS = 200

# Environment-variable names only; a value pasted here by mistake must be
# rejected without ever being echoed back.
_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class OpenAICompatibleConfig(BaseModel):
    """Per-row provider configuration parsed from ``model_name.model_parameters``.

    Unknown keys are rejected so a typo cannot silently change the wire shape.
    ``base_url`` is required; ``api_key_ref`` is optional for providers that need
    no authentication (e.g. a local Ollama).
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai_compatible"] = "openai_compatible"
    kind: Optional[Literal["chat", "completion"]] = None
    base_url: str = Field(..., min_length=1)
    api_key_ref: Optional[str] = None
    provider_model: Optional[str] = None
    endpoint: Literal["chat", "completions"] = "chat"
    max_new_tokens: int = Field(default=256, ge=1)
    temperature: float = Field(default=0.2, ge=0.0)
    top_p: float = Field(default=0.95, gt=0.0, le=1.0)
    timeout_seconds: float = Field(default=60.0, gt=0.0)

    @field_validator("base_url")
    @classmethod
    def _strip_base_url(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("base_url must not be empty")
        return stripped

    def resolved_kind(self, model_name: str) -> Literal["chat", "completion"]:
        """The row's kind, defaulting from the model name like the local loader."""
        if self.kind is not None:
            return self.kind
        return "chat" if "instruct" in model_name.lower() else "completion"

    def resolved_model(self, model_name: str) -> str:
        """The provider-side model id; defaults to the row's ``model_name``."""
        return self.provider_model or model_name

    def resolve_api_key(self) -> Optional[str]:
        """Read the provider key from the environment by variable *name*.

        Returns ``None`` when the row configures no ``api_key_ref``. A configured
        but missing/empty variable is a hard error that names the variable; the
        value is never stored or logged.
        """
        env_name = (self.api_key_ref or "").strip()
        if not env_name:
            return None
        if not _ENV_VAR_NAME.match(env_name):
            # Never echo the value: an operator may have pasted the key itself.
            raise RuntimeError(
                "api_key_ref must be an environment variable name "
                "(letters, digits and underscores)"
            )
        key = os.getenv(env_name, "").strip()
        if not key:
            raise RuntimeError(f"provider API key environment variable ${env_name} is not set")
        return key

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/completions"


def _post_json(
    config: OpenAICompatibleConfig, url: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """POST a JSON payload and return the decoded JSON object.

    Raises ``RuntimeError`` for transport failures, non-2xx responses and
    non-object JSON bodies. Messages never include the API key.
    """
    headers = {"Content-Type": "application/json"}
    api_key = config.resolve_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=config.timeout_seconds)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"provider request to {url} failed: {exc}") from exc

    if not 200 <= response.status_code < 300:
        excerpt = " ".join(str(getattr(response, "text", "") or "").split())
        if api_key:
            # Defensive: never surface the key even if an upstream echoes it.
            excerpt = excerpt.replace(api_key, "[redacted]")
        raise RuntimeError(
            f"provider request to {url} failed with status {response.status_code}: "
            f"{excerpt[:_MAX_ERROR_EXCERPT_CHARS]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"provider response from {url} was not valid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"provider response from {url} was not a JSON object")
    return data


def _first_choice(data: Dict[str, Any], provider_model: str) -> Dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"provider response for model {provider_model!r} contained no choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise RuntimeError(
            f"provider response for model {provider_model!r} had a malformed choice"
        )
    return choice


def _chat_message_content(choice: Dict[str, Any], provider_model: str) -> str:
    """Extract ``choices[0].message.content``, failing clearly on malformed bodies."""
    message = choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError(f"provider response for model {provider_model!r} had no message object")
    content = message.get("content")
    return "" if content is None else str(content)


def _wire_role(message: BaseMessage) -> str:
    """Map a LangChain message to an OpenAI chat role.

    ``SystemMessage``/``AIMessage``/``HumanMessage`` map to
    ``system``/``assistant``/``user``; every other message type maps to ``user``.
    """
    if isinstance(message, SystemMessage):
        return "system"
    if isinstance(message, AIMessage):
        return "assistant"
    return "user"


class OpenAICompatibleChatModel:
    """Provider-backed chat model with the ``ChatCompletionModel.invoke`` contract.

    ``invoke(messages)`` returns ``completion``, ``generation_time``, ``role``
    and ``model_name`` exactly like the local model.
    """

    def __init__(self, model_name: str, config: OpenAICompatibleConfig) -> None:
        self.model_name = model_name
        self.config = config
        self.provider_model = config.resolved_model(model_name)

    def invoke(self, messages: List[BaseMessage], **kwargs: Any) -> Dict[str, Any]:
        t0 = time.perf_counter()
        payload: Dict[str, Any] = {
            "model": self.provider_model,
            "messages": [
                {"role": _wire_role(message), "content": str(message.content)}
                for message in messages
            ],
            "max_tokens": self.config.max_new_tokens,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
        }
        data = _post_json(self.config, self.config.chat_completions_url, payload)
        t1 = time.perf_counter()

        content = _chat_message_content(
            _first_choice(data, self.provider_model), self.provider_model
        )

        return {
            "completion": str(content).strip(),
            "generation_time": int((t1 - t0) * 1000),
            "role": "assistant",
            "model_name": self.model_name,
        }


class OpenAICompatibleCompletionModel:
    """Provider-backed completion model with the local model's result shape.

    Prompt formatting mirrors ``TemplateCompletionModel._format_prompt_from_dict``
    (same multi-file/single-file template selection, ``file_separator`` semantics
    and ``stop_tokens``) without importing torch/transformers or LangChain prompt
    helpers.
    """

    def __init__(
        self,
        model_name: str,
        prompt_templates: dict,
        config: OpenAICompatibleConfig,
    ) -> None:
        self.model_name = model_name
        self.prompt_templates = prompt_templates
        self.config = config
        self.provider_model = config.resolved_model(model_name)
        self._validate_prompt_templates(prompt_templates)

    @staticmethod
    def _validate_prompt_templates(prompt_templates: dict) -> None:
        """Same shape checks as the local template model, so bad rows fail fast."""
        if not isinstance(prompt_templates, dict):
            raise ValueError("prompt_templates must be a dictionary.")
        if "fim_template" not in prompt_templates:
            raise KeyError("prompt_templates must contain the 'fim_template' key.")
        if "file_separator" not in prompt_templates:
            raise KeyError("prompt_templates must contain the 'file_separator' key.")
        fim_template = prompt_templates["fim_template"]
        if not isinstance(fim_template, dict):
            raise ValueError("'fim_template' must be a dictionary.")
        if "single_file_template" not in fim_template:
            raise KeyError("'fim_template' must contain the 'single_file_template' key.")
        if "multi_file_template" not in fim_template:
            raise KeyError("'fim_template' must contain the 'multi_file_template' key.")

    def _format_prompt_from_dict(self, prompt: dict) -> str:
        """Build a formatted FIM prompt, mirroring the local template model."""
        if "multi_file_context" in prompt:
            multi_file_context_prompt = ""
            for file_name, file_code in prompt["multi_file_context"].items():
                multi_file_context_prompt += (
                    self.prompt_templates["file_separator"].replace("{file_name}", file_name)
                    + file_code
                    + "\n"
                )
            prompt = {**prompt, "multi_file_context": multi_file_context_prompt}

        template_key = (
            "multi_file_template" if "multi_file_context" in prompt else "single_file_template"
        )
        return self.prompt_templates["fim_template"][template_key].format(**prompt)

    def invoke(
        self,
        prompt: dict,
        max_new_tokens: Optional[int] = None,
        stop_sequences: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> dict:
        """Generate a completion and return the local model's result shape.

        Returns ``completion``, ``generation_time``, ``confidence`` and
        ``logprobs``. The provider path has no token logprobs, so ``confidence``
        is ``0.0`` and ``logprobs`` is empty (the router persists both).
        """
        t0 = time.perf_counter()
        formatted_prompt = self._format_prompt_from_dict(prompt)
        stop = list(stop_sequences or []) + list(self.prompt_templates.get("stop_tokens") or [])
        max_tokens = max_new_tokens if max_new_tokens is not None else self.config.max_new_tokens

        if self.config.endpoint == "completions":
            payload: Dict[str, Any] = {
                "model": self.provider_model,
                "prompt": formatted_prompt,
                "max_tokens": max_tokens,
                "temperature": self.config.temperature,
                "top_p": self.config.top_p,
            }
            if stop:
                payload["stop"] = stop
            data = _post_json(self.config, self.config.completions_url, payload)
            completion = str(_first_choice(data, self.provider_model).get("text") or "")
        else:
            payload = {
                "model": self.provider_model,
                "messages": [
                    {"role": "system", "content": COMPLETION_SYSTEM_INSTRUCTION},
                    {"role": "user", "content": formatted_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": self.config.temperature,
                "top_p": self.config.top_p,
            }
            if stop:
                payload["stop"] = stop
            data = _post_json(self.config, self.config.chat_completions_url, payload)
            completion = _chat_message_content(
                _first_choice(data, self.provider_model), self.provider_model
            )

        t1 = time.perf_counter()
        return {
            "completion": completion,
            "generation_time": int((t1 - t0) * 1000),
            "confidence": 0.0,
            "logprobs": [],
        }
