import logging
from typing import Optional, Union

import torch
import json

torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True
from backend.completion.ChatCompletionModel import ChatCompletionModel
from backend.completion.TemplateCompletionModel import TemplateCompletionModel
from Code4meV2Config import Code4meV2Config


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
        key = (
            f"{model_name}:instruct"
            if "instruct" in model_name.lower()
            else f"{model_name}:{prompt_templates}"
        )

        if key in self.__models:
            logging.info(f"Model {key} is already loaded, skipping loading process.")
            return

        try:
            logging.info(
                f"Loading model with cache directory: {self.__config.model_cache_dir}"
            )

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
            logging.log(
                logging.INFO, f"Model {key} is loaded successfully: {model_parameters}"
            )
        except Exception as e:
            logging.error(e)
            logging.error(
                f"Failed to load model '{model_name}' with prompt templates'{prompt_templates}' and model parameters'{model_parameters}'"
            )

    def get_model(
        self, model_name: str, prompt_templates: str, model_parameters: str
    ) -> Optional[Union[TemplateCompletionModel, ChatCompletionModel]]:
        """
        Retrieves a model instance. Loads and caches it if not already loaded.

        Args:
            model_name (str): Name of the model to retrieve.
            prompt_template (Template): Prompt formatting template.

        Returns:
            Optional[Union[TemplateCompletionModel, ChatCompletionModel]]: The model instance if loaded successfully.
        """
        key = (
            f"{model_name}:instruct"
            if "instruct" in model_name.lower()
            else f"{model_name}:{prompt_templates}"
        )

        if key in self.__models:
            return self.__models[key]

        logging.info(f"Model {key} not preloaded. Loading now...")
        self.load_model(
            model_name=model_name,
            prompt_templates=prompt_templates,
            model_parameters=model_parameters,
        )
        return self.__models.get(key)
