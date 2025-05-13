import os
import time
from typing import Any, List, Optional

import torch
from langchain_community.llms import BaseLLM
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.outputs import LLMResult
from langchain_core.prompts import PromptTemplate
from pydantic import Field
from transformers import AutoModelForCausalLM, AutoTokenizer


class TemplateCompletionModel(BaseLLM):
    tokenizer: Any = Field(..., description="The tokenizer to use for text generation")
    model: Any = Field(..., description="The model to use for text generation")
    prompt_template: PromptTemplate = Field(
        ..., description="The prompt to use for text generation"
    )

    @property
    def _llm_type(self) -> str:
        return self.model_name

    def __init__(
        self,
        prompt_template: str,
        model_name: str,
        tokenizer_name: str = None,
        *args: Any,
        **model_kwargs
    ):
        tokenizer_name = tokenizer_name if tokenizer_name else model_name

        # Load tokenizer
        tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_name,
            cache_dir=os.path.join(".", ".cache"),
            trust_remote_code=True,
        )

        # Load model
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            cache_dir=os.path.join(".", ".cache"),
            trust_remote_code=True,
            **model_kwargs
        )
        if torch.cuda.is_available():
            model = self.model.cuda()

        prompt_template = PromptTemplate.from_template(prompt_template)

        super().__init__(
            prompt_template=prompt_template,
            tokenizer=tokenizer,
            model=model,
            *args,
            **model_kwargs
        )

    def _generate(
        self,
        prompts: List[dict],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any
    ) -> LLMResult:
        generations = []
        if kwargs.get("max_new_tokens") is None:
            kwargs["max_new_tokens"] = 128
        for prompt in prompts:
            # Generate text using the pipeline
            formatted_prompt = self.prompt_template.format(**prompt)
            inputs = self.tokenizer(formatted_prompt, return_tensors="pt").to(
                self.model.device
            )
            outputs = self.model.generate(**inputs, **kwargs)
            generated_text = self.tokenizer.decode(
                outputs[0], skip_special_tokens=True
            )[len(formatted_prompt) :]
            # Append the result
            generations.append([{"text": generated_text}])

        # Return the results wrapped in an LLMResult
        return LLMResult(generations=generations)

    def invoke(self, input: Any, **kwargs: Any) -> str:
        if isinstance(input, dict):
            return self._generate([input], **kwargs).generations[0][0].text
        elif isinstance(input, str):
            return self.pipeline(input, **kwargs)[0]["generated_text"]
        else:
            raise ValueError("Input must be a dict or a string.")


if __name__ == "__main__":
    test_code1 = {
        "prefix": """
            def quick_sort(arr):
                if len(arr) <= 1:
                    return arr
                pivot = arr[0]
                left = []
                right = []
            """,
        "suffix": """
                    if arr[i] < pivot:
                        left.append(arr[i])
                    else:
                        right.append(arr[i])
                return quick_sort(left) + [pivot] + quick_sort(right)
            """,
    }

    test_code2 = {
        "prefix": """
            # define a function to calculate the factorial of a number
            def factorial(n):
            """,
        "suffix": "",
    }
    prompt_template = """<｜fim▁begin｜>{prefix}<｜fim▁hole｜>{suffix}<｜fim▁end｜>"""
    # Example usage
    t0 = time.time()
    print("Setting up the model...")
    completion_model = TemplateCompletionModel(
        prompt_template=prompt_template,
        model_name="deepseek-ai/deepseek-coder-1.3b-base",
    )
    print("Model setup completed in {} seconds".format(time.time() - t0))
    print("Generating code...")
    t0 = time.time()
    result = completion_model.invoke(test_code2)
    print("Code generation completed in {} seconds".format(time.time() - t0))
    print(result)
