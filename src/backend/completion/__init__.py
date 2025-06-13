import logging
from enum import Enum
from typing import Optional, Union

from backend.completion.ChatCompletionModel import ChatCompletionModel
from backend.completion.TemplateCompletionModel import TemplateCompletionModel
from Code4meV2Config import Code4meV2Config


class Template(Enum):
    """
    Enum for model prompt templates. Currently supports FIM-style (Fill-in-the-Middle).
    """

    PREFIX_SUFFIX = """<｜fim▁begin｜>{prefix}<｜fim▁hole｜>{suffix}<｜fim▁end｜>"""


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
        prompt_template: Template = Template.PREFIX_SUFFIX,
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
            else f"{model_name}:{prompt_template.value}"
        )

        if key in self.__models:
            logging.info(f"Model {key} is already loaded, skipping loading process.")
            return

        try:
            logging.info(
                f"Loading model with cache directory: {self.__config.model_cache_dir}"
            )

            if "instruct" in model_name.lower():
                # Load an instruct-style chat model
                self.__models[key] = ChatCompletionModel(
                    model_name=model_name,
                    temperature=0.8,
                    max_new_tokens=256,
                    cache_dir=self.__config.model_cache_dir,
                )
            else:
                # Load a fill-in-the-middle (template) model
                self.__models[key] = TemplateCompletionModel.from_pretrained(
                    model_name=model_name,
                    prompt_template=prompt_template.value,
                    config=self.__config,
                )
        except Exception as e:
            logging.error(e)
            logging.error(
                f"Failed to load model '{model_name}' with template '{prompt_template.value}'"
            )

    def get_model(
        self,
        model_name: str,
        prompt_template: Template = Template.PREFIX_SUFFIX,
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
            else f"{model_name}:{prompt_template.value}"
        )

        if key in self.__models:
            return self.__models[key]

        logging.info(f"Model {key} not preloaded. Loading now...")
        self.load_model(model_name=model_name, prompt_template=prompt_template)
        return self.__models.get(key)
