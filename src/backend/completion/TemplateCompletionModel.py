import os
import time
from typing import Any, List, Optional

import torch
import torch.nn.functional as F
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
    device: torch.device = Field(..., description="The device to use")

    @property
    def _llm_type(self) -> str:
        return self.model_name

    def __init__(
        self,
        model_name: str,
        prompt_template: str,
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
            **model_kwargs,
        )
        model.eval()
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model.to(device)

        prompt_template = PromptTemplate.from_template(prompt_template)
        super().__init__(
            prompt_template=prompt_template,
            tokenizer=tokenizer,
            model=model,
            device=device,
            *args,
            **model_kwargs,
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

    def invoke(self, prompt: dict, max_new_tokens=128, **kwargs) -> dict:
        if not isinstance(prompt, dict):
            raise ValueError("Input must be a dict for this model setup.")
        formatted_prompt = self.prompt_template.format(**prompt)
        inputs = self.tokenizer(formatted_prompt, return_tensors="pt").to(self.device)

        # Measure generation time
        start_time = time.time()
        output = self.model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            return_dict_in_generate=True,
            output_scores=True,
        )
        end_time = time.time()

        generated_ids = output.sequences[0][
            inputs["input_ids"].shape[1] :
        ]  # Only new tokens
        generated_text = self.tokenizer.decode(generated_ids, skip_special_tokens=True)

        # Compute logprobs from logits
        logits = torch.stack(output.scores, dim=1)[0]  # shape: (num_tokens, vocab_size)
        log_probs = F.log_softmax(logits, dim=-1)  # shape: (num_tokens, vocab_size)

        # Get token logprobs
        token_logprobs = []
        token_probs = []
        for i, token_id in enumerate(generated_ids):
            token_logprob = log_probs[i, token_id].item()
            token_prob = torch.exp(log_probs[i, token_id]).item()
            token_logprobs.append(token_logprob)
            token_probs.append(token_prob)

        confidence = sum(token_probs) / len(token_probs) if token_probs else None

        return {
            "completion": generated_text.strip(),
            "generation_time": int((end_time - start_time) * 1000),
            "logprobs": token_logprobs,
            "confidence": confidence,
        }


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
    # https://huggingface.co/deepseek-ai/deepseek-coder-1.3b-base
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
