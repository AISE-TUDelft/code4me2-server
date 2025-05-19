import logging
from enum import Enum
from typing import Optional

from backend.completion.TemplateCompletionModel import TemplateCompletionModel


class Template(Enum):
    PREFIX_SUFFIX = """<｜fim▁begin｜>{prefix}<｜fim▁hole｜>{suffix}<｜fim▁end｜>"""


class CompletionModels:
    _instance = None
    __models = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(CompletionModels, cls).__new__(cls)
        return cls._instance

    def load_model(
        self, model_name: str, prompt_template: Template = Template.PREFIX_SUFFIX
    ) -> None:
        key = f"{model_name}:{prompt_template.value}"
        if key in self.__models:
            logging.log(
                logging.INFO,
                f"{model_name} with template {prompt_template.value} is already loaded",
            )
        else:
            try:
                self.__models[key] = TemplateCompletionModel(
                    model_name=model_name, prompt_template=prompt_template.value
                )
            except Exception as e:
                logging.log(logging.ERROR, e)
                logging.log(
                    logging.ERROR,
                    f"{model_name} with template {prompt_template.value} can't be loaded from hugging face hub",
                )

    def get_model(
        self, model_name: str, prompt_template: Template = Template.PREFIX_SUFFIX
    ) -> Optional[TemplateCompletionModel]:
        key = f"{model_name}:{prompt_template.value}"
        if key in self.__models:
            return self.__models[key]
        else:
            logging.log(
                logging.ERROR,
                f"{model_name} with template {prompt_template.value} is not available! Make sure to first load the model.",
            )
