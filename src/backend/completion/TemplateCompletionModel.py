import logging

# import threading
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

from Code4meV2Config import Code4meV2Config


class TemplateCompletionModel(BaseLLM):
    tokenizer: Any = Field(..., description="The tokenizer to use for text generation")
    model: Any = Field(..., description="The model to use for text generation")
    prompt_template: PromptTemplate = Field(
        ..., description="The prompt to use for text generation"
    )
    device: torch.device = Field(..., description="The device to use")
    config: Code4meV2Config = Field(..., description="Configuration for the model")
    # model_lock: Any = Field(default_factory=threading.RLock, exclude=True)
    model_name: str = Field(..., description="Name of the model")

    @property
    def _llm_type(self) -> str:
        return self.model_name

    @classmethod
    def from_pretrained(
        cls,
        model_name: str,
        prompt_template: str,
        tokenizer_name: Optional[str] = None,
        config: Optional[Code4meV2Config] = None,
        **model_kwargs,
    ) -> "TemplateCompletionModel":
        if config is None:
            config = Code4meV2Config()

        tokenizer_name = tokenizer_name or model_name

        tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_name,
            cache_dir=config.model_cache_dir,
            trust_remote_code=True,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            cache_dir=config.model_cache_dir,
            trust_remote_code=True,
            **model_kwargs,
        )
        model.eval()
        logging.log(
            logging.INFO, f"TORCH CUDA IS AVAILABLE: {torch.cuda.is_available()}"
        )
        if torch.cuda.is_available():
            logging.log(
                logging.INFO, f"CUDA DEVICE NAME: {torch.cuda.get_device_name(0)}"
            )
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model.to(device)

        # Apply torch.compile if enabled in config
        if config.model_use_compile and hasattr(torch, "compile"):
            try:
                logging.info(f"Compiling model {model_name} with torch.compile...")
                model = torch.compile(model)
                logging.info("Model compilation successful")
            except Exception as e:
                logging.error(f"Failed to compile model: {str(e)}")

        param_device = next(model.parameters()).device
        logging.info(f"Model successfully moved to device: {param_device}")

        prompt_template_obj = PromptTemplate.from_template(prompt_template)
        # model_lock = threading.RLock()
        return cls(
            prompt_template=prompt_template_obj,
            tokenizer=tokenizer,
            model=model,
            device=device,
            config=config,
            model_name=model_name,
            # model_lock=model_lock,
        )

    def _warmup(self):
        """
        Perform a warm-up run to initialize the model and reduce cold-start latency.
        This helps with initial compilation overhead when using torch.compile.
        """
        logging.info(f"Performing warm-up for model {self.model_name}...")
        try:
            # Create a simple prompt for warm-up
            warm_up_prompt = {"prefix": "def hello_world():", "suffix": ""}
            # Run a single inference to warm up the model
            self.invoke(warm_up_prompt, max_new_tokens=10)
            logging.info(f"Warm-up completed successfully for model {self.model_name}")
        except Exception as e:
            logging.error(f"Warm-up failed for model {self.model_name}: {str(e)}")

    def _generate(
        self,
        prompts: List[dict],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> LLMResult:
        """
        Generate text completions for multiple prompts using the model with optimized performance.

        This method includes several optimizations:
        1. Inference mode: Uses torch.inference_mode() for faster inference
        2. KV caching: Enables key-value caching in the transformer for faster generation
        3. Greedy decoding: Uses num_beams=1 for faster generation
        4. Thread safety: Uses a lock to ensure thread-safe access to the model

        Args:
            prompts: List of dictionaries containing prompt data
            stop: Optional list of stop sequences
            run_manager: Optional callback manager
            **kwargs: Additional arguments to pass to the model.generate() method

        Returns:
            LLMResult: Object containing the generated text for each prompt
        """
        generations = []

        # Set default max_new_tokens if not provided
        if kwargs.get("max_new_tokens") is None:
            kwargs["max_new_tokens"] = self.config.model_max_new_tokens

        # Add performance optimization parameters if not explicitly overridden
        if "use_cache" not in kwargs:
            kwargs["use_cache"] = self.config.model_use_cache
        if "num_beams" not in kwargs:
            kwargs["num_beams"] = self.config.model_num_beams

        # Process each prompt with optimized generation
        for prompt in prompts:
            # Generate text using the pipeline with inference_mode for speed
            formatted_prompt = self.prompt_template.format(**prompt)
            inputs = self.tokenizer(formatted_prompt, return_tensors="pt").to(
                self.device
            )

            # Use model_lock for thread safety and inference_mode for faster inference
            # with self.model_lock, torch.inference_mode():
            with torch.inference_mode():
                outputs = self.model.generate(**inputs, **kwargs)

            # Extract generated text, skipping the input prompt
            generated_text = self.tokenizer.decode(
                outputs[0], skip_special_tokens=True
            )[len(formatted_prompt) :]

            # Append the result
            generations.append([{"text": generated_text.strip()}])

        # Return the results wrapped in an LLMResult
        return LLMResult(generations=generations)

    def invoke(self, prompt: dict, max_new_tokens=None, **kwargs) -> dict:
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
        if not isinstance(prompt, dict):
            raise ValueError("Input must be a dict for this model setup.")

        # Use config values if not explicitly provided
        if max_new_tokens is None:
            max_new_tokens = self.config.model_max_new_tokens

        formatted_prompt = self.prompt_template.format(**prompt)
        inputs = self.tokenizer(formatted_prompt, return_tensors="pt").to(self.device)

        # Measure generation time with perf_counter for higher precision
        start_time = time.perf_counter()

        # Use model_lock for thread safety and inference_mode for faster inference
        # with self.model_lock, torch.inference_mode():
        with torch.inference_mode():
            output = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                return_dict_in_generate=True,
                output_scores=True,
                # Add performance-optimized generation parameters from config
                use_cache=self.config.model_use_cache,
                num_beams=self.config.model_num_beams,
                **kwargs,
            )
            end_time = time.perf_counter()

            generated_ids = output.sequences[0][
                inputs["input_ids"].shape[1] :
            ]  # Only new tokens
            generated_text = self.tokenizer.decode(
                generated_ids, skip_special_tokens=True
            )

            # with self.model_lock, torch.inference_mode():
            logits = torch.stack(output.scores, dim=1)[
                0
            ]  # shape: (num_tokens, vocab_size)
            log_probs = F.log_softmax(logits, dim=-1)  # shape: (num_tokens, vocab_size)

            # Vectorized token probability calculation
            token_indices = generated_ids.unsqueeze(-1)
            token_log_probs = torch.gather(log_probs, 1, token_indices).squeeze(-1)
            token_probs = torch.exp(token_log_probs)

            # Convert to Python lists
            token_logprobs = token_log_probs.tolist()
            token_probs_list = token_probs.tolist()

            confidence = (
                sum(token_probs_list) / len(token_probs_list)
                if token_probs_list
                else None
            )

        result = {
            "completion": generated_text.strip(),
            "generation_time": int((end_time - start_time) * 1000),
            "logprobs": token_logprobs,
            "confidence": confidence,
        }

        return result


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
    t0 = time.perf_counter()
    print("Setting up the model...")
    # https://huggingface.co/deepseek-ai/deepseek-coder-1.3b-base
    completion_model = TemplateCompletionModel(
        prompt_template=prompt_template,
        model_name="deepseek-ai/deepseek-coder-1.3b-base",
    )
    print("Model setup completed in {} seconds".format(time.perf_counter() - t0))
    print("Generating code...")
    t0 = time.perf_counter()
    result = completion_model.invoke(test_code2)
    print("Code generation completed in {} seconds".format(time.perf_counter() - t0))
    print(result)
