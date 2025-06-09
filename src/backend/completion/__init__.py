import logging
from enum import Enum
from typing import Optional, Union

from backend.completion.ChatCompletionModel import ChatCompletionModel
from backend.completion.TemplateCompletionModel import TemplateCompletionModel


class Template(Enum):
    PREFIX_SUFFIX = """<｜fim▁begin｜>{prefix}<｜fim▁hole｜>{suffix}<｜fim▁end｜>"""


class CompletionModels:
    _instance = None
    __models = {}
    __config = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(CompletionModels, cls).__new__(cls)
        return cls._instance

    def load_model(
        self,
        model_name: str,
        config=None,
        prompt_template: Template = Template.PREFIX_SUFFIX,
    ) -> None:
        if self.__config is None:
            if config is None:
                raise ValueError("Configuration must be provided to load models.")
            self.__config = config
        if "instruct" in model_name.lower():
            key = f"{model_name}:instruct"
        else:
            key = f"{model_name}:{prompt_template.value}"
        if key in self.__models:
            logging.log(
                logging.INFO,
                f"Model {key} is already loaded, skipping loading process.",
            )
        else:
            try:
                logging.info(
                    f"Loading model with cache directory: {config.model_cache_dir}"
                )

                if "instruct" in model_name.lower():
                    self.__models[key] = ChatCompletionModel(
                        model_name=model_name,
                        temperature=0.8,
                        max_new_tokens=512,
                        cache_dir=config.model_cache_dir,  # Explicit cache dir
                    )
                else:
                    self.__models[key] = TemplateCompletionModel.from_pretrained(
                        model_name=model_name,
                        prompt_template=prompt_template.value,
                        config=config,  # Pass the config explicitly
                    )
            except Exception as e:
                logging.log(logging.ERROR, e)
                logging.log(
                    logging.ERROR,
                    f"{model_name} with template {prompt_template.value} can't be loaded from hugging face hub",
                )

    def get_model(
        self, model_name: str, prompt_template: Template = Template.PREFIX_SUFFIX
    ) -> Optional[Union[TemplateCompletionModel, ChatCompletionModel]]:
        if "instruct" in model_name.lower():
            key = f"{model_name}:instruct"
        else:
            key = f"{model_name}:{prompt_template.value}"
        if key in self.__models:
            return self.__models[key]
        else:
            logging.log(
                logging.INFO, f"Loading the {key} model since it's not preloaded..."
            )
            self.load_model(model_name, prompt_template)
            return self.__models.get(key)
