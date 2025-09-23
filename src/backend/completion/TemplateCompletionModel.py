import logging
import numpy as np
# import threading
import time
from typing import Any, List, Optional

import torch
import torch.nn.functional as F
from langchain_community.llms import BaseLLM
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.prompts import PromptTemplate
from pydantic import Field
from transformers import (
    StoppingCriteria,
    StoppingCriteriaList,
)

from backend.completion.CompletionModel import CompletionModel


class StopSequenceCriteria(StoppingCriteria):
    """Custom stopping criteria that checks for stop sequences after the input length."""

    def __init__(self, tokenizer, stop_sequences, input_len):
        self.tokenizer = tokenizer
        self.stop_sequences = stop_sequences
        self.input_len = input_len  # Length of input tokens

    def __call__(self, input_ids, scores, **kwargs):
        # Decode full sequence
        full_text = self.tokenizer.decode(input_ids[0], skip_special_tokens=False)
        
        # Decode only the input tokens to get the input text
        input_text = self.tokenizer.decode(input_ids[0][:self.input_len], skip_special_tokens=False)
        input_text_len = len(input_text)

        # Only check for stop sequences after the input part
        generated_text = full_text[input_text_len:]

        for stop_seq in self.stop_sequences:
            if stop_seq in generated_text:
                logging.info(f"Stopping at: {stop_seq} in generated text: {repr(generated_text)}")
                return True

        return False


class TemplateCompletionModel(CompletionModel, BaseLLM):
    prompt_templates: dict = Field(..., description="The prompt templates for the model")
    def __init__(self, **data: Any) -> "TemplateCompletionModel":
        """
        Initialize the TemplateCompletionModel.
        """
        super().__init__(**data)
        self._validate_prompt_templates(self.prompt_templates)

    def _validate_prompt_templates(self, prompt_templates: dict) -> None:
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
        """Build a formatted prompt from a prompt dict using configured templates."""
        if "multi_file_context" in prompt:
            multi_file_context_prompt = ""
            for file_name, file_code in prompt["multi_file_context"].items():
                multi_file_context_prompt += self.prompt_templates["file_separator"].replace("{file_name}", file_name) + file_code + "\n"
            prompt = {**prompt, "multi_file_context": multi_file_context_prompt}

        
        formatted = PromptTemplate.from_template(
            self.prompt_templates["fim_template"][
                "multi_file_template" if "multi_file_context" in prompt else "single_file_template"
            ]
        ).format(**prompt)
        return formatted
    
    def warmup(self) -> None:
        """
        Perform a warm-up run to initialize the model and reduce cold-start latency.
        This helps with initial compilation overhead when using torch.compile.
        """
        logging.info(f"Performing warm-up for model {self.model_name}...")
        try:
            # Create a simple prompt for warm-up
            warm_up_prompt = {"prefix": "def hello_world():", "suffix": ""}
            # Run a single inference to warm up the model
            response = self.invoke(warm_up_prompt, max_new_tokens=32)
            logging.info(f"Warm-up completed successfully for model {self.model_name}. prefix: {warm_up_prompt['prefix']}, suffix: {warm_up_prompt['suffix']}\nResponse: {response['completion']}")
        except Exception as e:
            logging.error(f"Warm-up failed for model {self.model_name}: {str(e)}")
            raise e

    def _generate(
        self,
        prompt: Any,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        """
        LangChain BaseLLM entrypoint. Accepts either a pre-formatted string or
        a dict to be formatted via the configured templates, and returns text only.
        """
        if isinstance(prompt, dict):
            result = self.invoke(prompt, stop_sequences=stop, **kwargs)
            return result.get("completion", "")
        elif isinstance(prompt, str):
            # Treat the string as already-formatted; wrap into our dict shape
            prompt_dict = {"prefix": prompt, "suffix": ""}
            result = self.invoke(prompt_dict, stop_sequences=stop, **kwargs)
            return result.get("completion", "")
        else:
            raise ValueError("Unsupported prompt type; expected dict or str")

    def invoke(
        self, prompt: dict, max_new_tokens=None, stop_sequences=None, **kwargs
    ) -> dict:
        """
        Generate text completions using the model with optimized performance.

        This method includes several optimizations:
        1. Inference mode: Uses torch.inference_mode() for faster inference
        2. Vectorized operations: Token probabilities are calculated in a vectorized way
        3. KV caching: Enables key-value caching in the transformer for faster generation
        4. Thread safety: Uses a lock to ensure thread-safe access to the model

        Args:
            prompt (dict): Dictionary containing 'prefix' and 'suffix' for the prompt
            max_new_tokens (int): Maximum number of tokens to generate
            **kwargs: Additional arguments to pass to the model.generate() method

        Returns:
            dict: Dictionary containing the completion, generation time, logprobs, and confidence
        """
        t0 = time.perf_counter()

        formatted_prompt = self._format_prompt_from_dict(prompt)
        logging.info("Formatted prompt: %s", formatted_prompt)
        t1 = time.perf_counter()
        logging.info("Prompt formatting took %.2f seconds", t1 - t0)

        # Set up stopping criteria if stop_sequences is provided
        # Always include EOS token in stop sequences if defined
        eos_text = (
            self.tokenizer.decode([self.tokenizer.eos_token_id])
            if self.tokenizer.eos_token_id is not None
            else None
        )

        # Combine user-provided stop sequences with EOS and model-specific stop sequences
        stop_sequences = (stop_sequences or []) + (self.prompt_templates.get("stop_tokens", []) or [])
        if eos_text and eos_text not in stop_sequences:
            stop_sequences.append(eos_text)

        logging.info("Stop sequences: %s", stop_sequences)

        # Tokenize prompt to get token count
        
        inputs = self.tokenizer(formatted_prompt, **self.tokenizer_generation_kwargs)
        input_ids = inputs.input_ids.to(self.model.device)
        attention_mask = inputs.attention_mask.to(self.model.device)
        input_len = input_ids.shape[1]  # number of tokens including special token

        generation_kwargs = self.model_generation_kwargs.copy()
        generation_kwargs.update(**kwargs)
        if max_new_tokens is not None:
            generation_kwargs["max_new_tokens"] = max_new_tokens
       
        with torch.inference_mode():
            start_time = time.perf_counter()
            # Generate output
            output = self.model.generate(
                input_ids,
                attention_mask=attention_mask,
                return_dict_in_generate=True,
                output_scores=True,
                stopping_criteria=StoppingCriteriaList([StopSequenceCriteria(self.tokenizer, stop_sequences, input_len)]),
                **generation_kwargs
            )
            end_time = time.perf_counter()
        # Slice only newly generated tokens
        generated_ids = output.sequences[0][input_len:]
        # Decode the new tokens
        generated_text = self.tokenizer.decode(generated_ids, skip_special_tokens=True)
        # Token-level logprobs
        logits = torch.stack(output.scores, dim=1)[0]  # (new_tokens, vocab_size)
        log_probs = F.log_softmax(logits, dim=-1)
        token_indices = generated_ids.unsqueeze(-1)
        token_log_probs = torch.gather(log_probs, 1, token_indices).squeeze(-1)
        token_probs = torch.exp(token_log_probs)
        token_logprobs = np.round(token_log_probs.tolist(), decimals=4)
        token_probs_list = token_probs.tolist()
        confidence = np.round(sum(token_probs_list) / len(token_probs_list), decimals=4) if token_probs_list else None


        # Post-process the generated text to remove any content after the stop sequence
        if stop_sequences and generated_text:
            for stop_seq in stop_sequences:
                if stop_seq in generated_text:
                    # Truncate at the stop sequence
                    generated_text = generated_text.split(stop_seq)[0]
                    break

        result = {
            "completion": generated_text,
            "generation_time": int((end_time - start_time) * 1000),
            "logprobs": token_logprobs,
            "confidence": confidence,
        }

        return result


if __name__ == "__main__":
    import json
    import requests
    
    template = '{"fim_template":{"multi_file_template":"{multi_file_context}#{file_name}\\n{prefix}","single_file_template":"{prefix}"},"file_separator":"#{file_name}\\n","stop_tokens":["\\n\\n"]}'
    prompt_templates = json.loads(template)
    
    model = TemplateCompletionModel(
        model_name="deepseek-ai/deepseek-coder-1.3b-base",
        prompt_templates=prompt_templates,
        model_warmup=True,
        model_use_compile= True,
        do_sample=False,
    )
    # response = model.invoke({"prefix": "warmup", "suffix": ""})
    t0 = time.perf_counter()
    response = model.invoke({"prefix": "\n\ndef bubble_sort(arr):", "suffix": ""})
    t1 = time.perf_counter()
    print(f"Time taken: {t1 - t0} seconds")
    print(response)


# (Commented code containing hardcoded credentials removed for security reasons)