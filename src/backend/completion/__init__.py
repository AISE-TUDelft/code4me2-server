from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Optional, Union

from pydantic import ValidationError

from backend.completion.OpenAICompatibleModel import (
    OpenAICompatibleChatModel,
    OpenAICompatibleCompletionModel,
    OpenAICompatibleConfig,
)

if TYPE_CHECKING:
    from Code4meV2Config import Code4meV2Config

try:
    import torch
except ImportError:
    torch = None

if torch is not None:
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True

# The completion model classes need the local torch/transformers stack. Import
# them at module level when it is present; otherwise expose placeholders so
# `backend.completion.<Class>` resolves and fails with a clear message only if
# something actually tries to use the local model stack in a torch-free image
# (e.g. the fast dev image, which always uses an external provider).
if torch is not None:
    from backend.completion.ChatCompletionModel import ChatCompletionModel
    from backend.completion.TemplateCompletionModel import TemplateCompletionModel
else:

    class _TorchRequiredModel:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "This image runs without the local Hugging Face/torch model stack; "
                "use an external OpenAI-compatible provider instead."
            )

    ChatCompletionModel = _TorchRequiredModel  # type: ignore[assignment,misc]
    TemplateCompletionModel = _TorchRequiredModel  # type: ignore[assignment,misc]

# Accepted model types for `POST /api/chat/request`. Defined from the names above
# so it resolves in both torch-present and torch-free images.
CLASSIC_CHAT_MODEL_TYPES = (ChatCompletionModel, OpenAICompatibleChatModel)


def _provider_config_for(
    model_parameters: str, model_name: str
) -> Optional[OpenAICompatibleConfig]:
    """Return the provider config for a row that opts in, else ``None`` (local row).

    A row with ``"provider": "openai_compatible"`` is validated eagerly. Invalid
    configuration raises a sanitized ``RuntimeError`` that names the model and the
    offending fields but never echoes raw parameter values (which could contain a
    mistakenly pasted secret).
    """
    try:
        params = json.loads(model_parameters)
    except (TypeError, ValueError):
        return None
    if not isinstance(params, dict) or "provider" not in params:
        return None
    if params.get("provider") != "openai_compatible":
        # A typo'd provider value must not silently fall back to a local model.
        raise RuntimeError(
            f"unsupported 'provider' value for model {model_name!r}; "
            "only 'openai_compatible' is supported"
        )
    try:
        return OpenAICompatibleConfig(**params)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise RuntimeError(
            f"invalid openai_compatible configuration for model {model_name!r}: {details}"
        ) from None


class CompletionModels:
    """
    Singleton class responsible for managing and caching model instances used for code completion.
    Supports both instruct-style and template-based models.
    """

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        """
        Enforces the singleton pattern to ensure only one instance of CompletionModels exists.
        """
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config: Code4meV2Config):
        """
        Initializes the CompletionModels instance with configuration and model cache.

        Args:
            config (Code4meV2Config): Configuration object containing model paths, cache directories, etc.
        """
        if self._initialized:
            return
        self.__config = config
        self.__models = {}  # Dictionary to store loaded models keyed by name/template.
        self._initialized = True

    @staticmethod
    def _model_cache_key(
        model_name: str, prompt_templates: str, model_parameters: str
    ) -> tuple[Optional[str], Optional[OpenAICompatibleConfig]]:
        """Return ``(cache key, provider config)`` for a row.

        Provider rows use a key that includes kind, base_url and provider model so
        distinct provider rows never collide. Returns ``(None, None)`` when the row
        opts in but its configuration is invalid (the error is logged here).
        """
        try:
            provider_config = _provider_config_for(model_parameters, model_name)
        except RuntimeError as exc:
            logging.error(exc)
            return None, None
        if provider_config is not None:
            fingerprint = hashlib.sha256(
                provider_config.model_dump_json().encode("utf-8")
            ).hexdigest()[:16]
            key = (
                f"{model_name}:provider:{provider_config.resolved_kind(model_name)}:"
                f"{provider_config.base_url}:{provider_config.resolved_model(model_name)}:"
                f"{fingerprint}"
            )
        else:
            key = (
                f"{model_name}:instruct"
                if "instruct" in model_name.lower()
                else f"{model_name}:{prompt_templates}"
            )
        return key, provider_config

    def load_model(
        self,
        model_name: str,
        prompt_templates: str,
        model_parameters: str,
    ) -> None:
        """
        Loads a model into memory and caches it using the specified template.

        Args:
            model_name (str): Name of the model to load.
            prompt_template (Template): Prompt formatting template (used for non-instruct models).
        """
        key, provider_config = self._model_cache_key(
            model_name, prompt_templates, model_parameters
        )
        if key is None:
            return

        if key in self.__models:
            logging.info(f"Model {key} is already loaded, skipping loading process.")
            return

        try:
            if provider_config is not None:
                # Provider-backed row: served over HTTP, no local ML stack needed.
                if provider_config.resolved_kind(model_name) == "chat":
                    self.__models[key] = OpenAICompatibleChatModel(
                        model_name=model_name, config=provider_config
                    )
                else:
                    self.__models[key] = OpenAICompatibleCompletionModel(
                        model_name=model_name,
                        prompt_templates=json.loads(prompt_templates),
                        config=provider_config,
                    )
                logging.info(f"Provider model {key} is ready.")
                return

            if torch is None:
                raise RuntimeError(
                    "Local model loading requires the optional ML dependencies "
                    "(torch, transformers, and sentence-transformers)."
                )
            from backend.completion.ChatCompletionModel import ChatCompletionModel
            from backend.completion.TemplateCompletionModel import TemplateCompletionModel

            logging.info(f"Loading model with cache directory: {self.__config.model_cache_dir}")

            model_parameters = json.loads(model_parameters)
            if "instruct" in model_name.lower():
                # Load an instruct-style chat model
                self.__models[key] = ChatCompletionModel(
                    model_name=model_name,
                    cache_dir=self.__config.model_cache_dir,
                    model_use_cache=self.__config.model_use_cache,
                    model_use_compile=self.__config.model_use_compile,
                    model_warmup=self.__config.model_warmup,
                    **model_parameters,
                )
            else:
                prompt_templates = json.loads(prompt_templates)
                # Load a fill-in-the-middle (template) model
                self.__models[key] = TemplateCompletionModel(
                    model_name=model_name,
                    prompt_templates=prompt_templates,
                    cache_dir=self.__config.model_cache_dir,
                    model_use_cache=self.__config.model_use_cache,
                    model_use_compile=self.__config.model_use_compile,
                    model_warmup=self.__config.model_warmup,
                    **model_parameters,
                )
            model_parameters = self.__models[key].model_dump()
            del model_parameters[
                "model"
            ]  # Remove the model from the model parameters for better logging
            del model_parameters[
                "tokenizer"
            ]  # Remove the tokenizer from the model parameters for better logging
            logging.log(logging.INFO, f"Model {key} is loaded successfully: {model_parameters}")
        except Exception as e:
            logging.error(e)
            logging.error(
                f"Failed to load model '{model_name}' with prompt templates'{prompt_templates}' and model parameters'{model_parameters}'"
            )

    def get_model(self, model_name: str, prompt_templates: str, model_parameters: str) -> Optional[
        Union[
            TemplateCompletionModel,
            ChatCompletionModel,
            OpenAICompatibleChatModel,
            OpenAICompatibleCompletionModel,
        ]
    ]:
        """
        Retrieves a model instance. Loads and caches it if not already loaded.

        Args:
            model_name (str): Name of the model to retrieve.
            prompt_template (Template): Prompt formatting template.

        Returns:
            Optional[Union[...]]: The model instance if loaded successfully.
        """
        key, _ = self._model_cache_key(model_name, prompt_templates, model_parameters)
        if key is None:
            return None

        if key in self.__models:
            return self.__models[key]

        logging.info(f"Model {key} not preloaded. Loading now...")
        self.load_model(
            model_name=model_name,
            prompt_templates=prompt_templates,
            model_parameters=model_parameters,
        )
        return self.__models.get(key)
