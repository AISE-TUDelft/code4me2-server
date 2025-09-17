import os
import json
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import torch
from pydantic import BaseModel, Field
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline
from langchain_huggingface import HuggingFacePipeline
import logging

class CompletionModel(BaseModel, ABC):
    """
    Base class for completion models providing shared configuration, fields,
    and helper methods for tokenizer/model initialization.
    """

    # Model identifiers
    model_name: str = Field(..., description="The HuggingFace model identifier")
    tokenizer_name: Optional[str] = Field(
        default=None, description="Optional tokenizer identifier; defaults to model_name"
    )

    # Generation defaults (subclasses may use some/all)
    temperature: float = Field(default=1.0, description="The temperature to use for the model")
    max_new_tokens: int = Field(default=64, description="The maximum number of new tokens to generate")
    num_beams: int = Field(default=1, description="The number of beams to use for the model")
    top_p: float = Field(default=0.95, description="The top p to use for the model")
    top_k: int = Field(default=50, description="The top k to use for the model")
    do_sample: bool = Field(default=False, description="Whether to use the sample")
    repetition_penalty: float = Field(default=1.0, description="The repetition penalty to use for the model")

    # Caching / loading
    cache_dir: str = Field(default="./.cache", description="Local cache directory")
    trust_remote_code: bool = Field(default=True, description="Whether to trust remote code")
    device_map: str = Field(default="auto", description="The device map to use for the model")

    # Torch/model loading knobs
    torch_dtype: str = Field(default="bfloat16", description="The torch dtype to use for the model")
    low_cpu_mem_usage: bool = Field(default=True, description="Whether to use low CPU memory usage")
    load_in_8bit: bool = Field(default=False, description="Whether to load the model in 8bit")
    load_in_4bit: bool = Field(default=False, description="Whether to load the model in 4bit")

    # Additional model-wide knobs used by some subclasses
    model_max_new_tokens: int = Field(default=512, description="The maximum number of new tokens to generate")
    model_use_cache: bool = Field(default=True, description="Whether to use the cache")
    model_num_beams: int = Field(default=1, description="The number of beams to use for the model")
    model_use_compile: bool = Field(default=False, description="Whether to use the compile")
    model_warmup: bool = Field(default=False, description="Whether to use the warmup")
    model_quantization: Optional[str] = Field(default=None, description="The quantization to use for the model")

    # Runtime instances
    tokenizer: Any = Field(default=None, description="The tokenizer to use for the model")
    model: Any = Field(default=None, description="The model to use for the model")
    pipeline: Any = Field(default=None, description="The pipeline to use for the model")


    def __init__(self, **data: Any):
        super().__init__(**data)
        self.tokenizer_name = self.tokenizer_name or self.model_name
        os.makedirs(self.cache_dir, exist_ok=True) 

        # Load tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(self.tokenizer_name, **self.tokenizer_kwargs)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token 
        # Change it to "left" → truncates from the beginning
        # self.tokenizer.truncation_side = "left" # TODO: Investigate if this is the correct way to truncate from the beginning specially in the ChatBased models
        
        # Load model
        self.model = AutoModelForCausalLM.from_pretrained(self.model_name, **self.model_kwargs)
        self.model.eval()

        # Optionally compile model
        if self.model_use_compile and hasattr(torch, "compile"):
            try:
                logging.info(f"Compiling model {self.model_name} with torch.compile...")
                self.model = torch.compile(self.model)
                logging.info("Model compilation successful")
            except Exception as e:
                logging.error(f"Failed to compile model: {str(e)}")

        # Log the device of the first parameter (layers are already on proper devices)
        param_device = next(self.model.parameters()).device
        logging.info(f"Model loaded on device: {param_device}")

        text_gen_pipeline = pipeline(task="text-generation", model=self.model, tokenizer=self.tokenizer, **self.model_generation_kwargs)
        self.pipeline = HuggingFacePipeline(pipeline=text_gen_pipeline)

        if self.model_warmup:
            self.warmup()

    @abstractmethod
    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def warmup(self) -> None:
        raise NotImplementedError

    @property
    def _llm_type(self) -> str:
        return type(self).__name__

    @property
    def tokenizer_kwargs(self) -> dict:
        return {
            "cache_dir": self.cache_dir,
            "trust_remote_code": self.trust_remote_code,
        }

    @property
    def model_kwargs(self) -> dict:
        return {
            "cache_dir": self.cache_dir,
            "trust_remote_code": self.trust_remote_code,
            "device_map": self.device_map,
            "torch_dtype": getattr(torch, self.torch_dtype) if hasattr(torch, self.torch_dtype) else torch.bfloat16,
            "low_cpu_mem_usage": self.low_cpu_mem_usage,
            "load_in_8bit": self.load_in_8bit if not self.load_in_4bit else False,
            "load_in_4bit": self.load_in_4bit,
        }
    @property
    def model_generation_kwargs(self) -> dict:
        generation_kwargs = {
            "max_new_tokens": self.max_new_tokens,
            "num_beams": self.model_num_beams,
            "use_cache": self.model_use_cache,
            "do_sample": self.do_sample,
            "repetition_penalty": self.repetition_penalty,
            "pad_token_id": self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
        } 
        if self.do_sample:
            generation_kwargs["top_p"] = self.top_p
            generation_kwargs["top_k"] = self.top_k
            generation_kwargs["temperature"] = self.temperature
        return generation_kwargs
    @property
    def tokenizer_generation_kwargs(self) -> dict:
        return {
            "padding": True,
            "truncation": True,
            "return_tensors": "pt",
        }


    