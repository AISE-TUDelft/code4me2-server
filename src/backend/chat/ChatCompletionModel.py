"""
ChatCompletionModel: A LangChain-compatible chat model implementation using HuggingFace Transformers.

This module provides a custom chat model that integrates with the LangChain framework while
using HuggingFace transformers for local text generation. It supports various language models
and provides a convenient interface for chat-based interactions.
"""

import os
import time
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_huggingface import HuggingFacePipeline
from pydantic import BaseModel, Field
from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline


class ChatCompletionModel(BaseChatModel, BaseModel):
    """
    A LangChain-compatible chat model that uses HuggingFace transformers for local text generation.

    This class extends LangChain's BaseChatModel to provide chat completion functionality
    using locally-hosted language models from HuggingFace. It supports message formatting,
    generation parameters customization, and proper integration with LangChain callbacks.

    Attributes:
        model_name (str): The HuggingFace model identifier to load.
                         Default: "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct"
        model_type (str): Type of model deployment. Currently only "local" is supported.
        tokenizer_name (Optional[str]): Override tokenizer name. If None, uses model_name.
        temperature (float): Controls randomness in generation (0.0-1.0). Default: 0.7
        max_new_tokens (int): Maximum number of new tokens to generate. Default: 512
        top_p (float): Nucleus sampling parameter (0.0-1.0). Default: 0.95
        top_k (int): Top-k sampling parameter. Default: 50
        do_sample (bool): Whether to use sampling or greedy decoding. Default: True
        repetition_penalty (float): Penalty for token repetition. Default: 1.0
        system_prefix (str): Prefix for system messages. Default: "[SYSTEM]"
        user_prefix (str): Prefix for user messages. Default: "[USER]"
        assistant_prefix (str): Prefix for assistant messages. Default: "[ASSISTANT]"
        unknown_prefix (str): Prefix for unknown message types. Default: "[UNKNOWN]"
        stop_sequences (List[str]): List of sequences that stop generation.
        cache_dir (str): Directory for model caching. Default: "./.cache"
        trust_remote_code (bool): Whether to trust remote code in models. Default: True
        device_map (str): Device mapping strategy for model loading. Default: "auto"
        model (Any): The loaded HuggingFace pipeline instance.
        tokenizer (Any): The loaded tokenizer instance.

    Example:
        >>> model = ChatCompletionModel(
        ...     model_name="codellama/CodeLlama-7b-Instruct-hf",
        ...     temperature=0.8,
        ...     max_new_tokens=256
        ... )
        >>> messages = [HumanMessage(content="Hello, how are you?")]
        >>> response = model.invoke(messages)
        >>> print(response["completion"])
    """

    # model configuration
    model_name: str = Field(default="deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct")
    model_type: str = Field(default="local")
    tokenizer_name: Optional[str] = None

    # generation parameters
    temperature: float = 0.7
    max_new_tokens: int = 512
    top_p: float = 0.95
    top_k: int = 50
    do_sample: bool = True
    repetition_penalty: float = 1.0

    # Message Formatting
    system_prefix: str = "[SYSTEM]"
    user_prefix: str = "[USER]"
    assistant_prefix: str = "[ASSISTANT]"
    unknown_prefix: str = "[UNKNOWN]"

    # model loading configuration
    stop_sequences: List[str] = Field(default_factory=list)
    cache_dir: str = Field(default=os.path.join(".", ".cache"))
    trust_remote_code: bool = True
    device_map: str = "auto"

    # Internal model instances
    model: Any = Field(default=None)
    tokenizer: Any = Field(default=None)

    def __init__(self, **data: Any):
        """
        Initialize the ChatCompletionModel.

        This method loads the specified HuggingFace model and tokenizer, sets up the
        text generation pipeline, and configures all necessary components for chat completion.

        Args:
            **data: Keyword arguments for model configuration. See class attributes for options.

        Raises:
            NotImplementedError: If model_type is not "local".
            Exception: If model or tokenizer loading fails.

        Note:
            Model loading can take significant time depending on model size and hardware.
            Progress is printed to stdout during initialization.
        """
        super().__init__(**data)

        # Set default tokenizer name
        self.tokenizer_name = self.tokenizer_name or self.model_name
        os.makedirs(self.cache_dir, exist_ok=True)

        # Validate model type
        if self.model_type.lower() != "local":
            raise NotImplementedError("Only 'local' model_type is currently supported.")

        # Load tokenizer
        start_time = time.time()
        print(f"Loading tokenizer {self.tokenizer_name}...")
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.tokenizer_name,
            cache_dir=self.cache_dir,
            trust_remote_code=self.trust_remote_code,
        )
        print(f"Tokenizer loaded in {time.time() - start_time:.2f} seconds")

        # Set pad token if not available
        if self.tokenizer.pad_token is None and self.tokenizer.eos_token is not None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        # Load model with optimizations
        start_time = time.time()
        print(f"Loading model {self.model_name}...")

        # Configure model loading parameters
        model_kwargs = {
            "cache_dir": self.cache_dir,
            "device_map": self.device_map,
            "trust_remote_code": self.trust_remote_code,
        }

        raw_model = AutoModelForCausalLM.from_pretrained(
            self.model_name, **model_kwargs
        )
        print(f"Model loaded in {time.time() - start_time:.2f} seconds")

        # Create HuggingFace pipeline
        start_time = time.time()
        print("Setting up text generation pipeline...")
        text_gen_pipeline = pipeline(
            "text-generation",
            model=raw_model,
            tokenizer=self.tokenizer,
            max_new_tokens=self.max_new_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            top_k=self.top_k,
            do_sample=self.do_sample,
            repetition_penalty=self.repetition_penalty,
        )
        print(f"Pipeline set up in {time.time() - start_time:.2f} seconds")

        # Wrap in LangChain HuggingFacePipeline
        self.model = HuggingFacePipeline(pipeline=text_gen_pipeline)

        # Configure stop sequences
        self.stop_sequences = self.stop_sequences or [
            self.system_prefix,
            self.user_prefix,
            self.assistant_prefix,
        ]

        print("ChatCompletionModel initialization completed")

    @property
    def _llm_type(self) -> str:
        """
        Return the LLM type identifier for LangChain compatibility.

        Returns:
            str: The LLM type identifier "huggingface_chat_model".
        """
        return "huggingface_chat_model"

    def _format_messages(self, messages: List[BaseMessage]) -> str:
        """
        Format a list of messages into a single prompt string.

        This method converts LangChain message objects into a formatted string
        suitable for the language model, using the configured prefixes for each
        message type.

        Args:
            messages (List[BaseMessage]): List of LangChain message objects.

        Returns:
            str: Formatted prompt string ending with the assistant prefix.

        Example:
            >>> messages = [
            ...     SystemMessage(content="You are helpful"),
            ...     HumanMessage(content="Hello")
            ... ]
            >>> formatted = model._format_messages(messages)
            >>> print(formatted)
            [SYSTEM] You are helpful
            [USER] Hello
            [ASSISTANT]
        """

        formatted = ""
        for msg in messages:
            if isinstance(msg, SystemMessage):
                formatted += f"{self.system_prefix} {msg.content}\n"
            elif isinstance(msg, HumanMessage):
                formatted += f"{self.user_prefix} {msg.content}\n"
            elif isinstance(msg, AIMessage):
                formatted += f"{self.assistant_prefix} {msg.content}\n"
            else:
                formatted += f"{self.unknown_prefix} {msg.content}\n"
        return formatted.strip() + f"\n{self.assistant_prefix}"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """
        Generate a chat completion response.

        This is the core generation method that formats messages, calls the underlying
        language model, and processes the response to return a structured ChatResult.

        Args:
            messages (List[BaseMessage]): List of conversation messages.
            stop (Optional[List[str]]): Additional stop sequences for generation.
            run_manager (Optional[CallbackManagerForLLMRun]): LangChain callback manager.
            **kwargs: Additional generation parameters that override defaults.

        Returns:
            ChatResult: LangChain ChatResult containing the generated response.

        Raises:
            Exception: If generation fails, re-raises the underlying exception.

        Note:
            Generation parameters in kwargs will override the model's default parameters
            for this specific generation call.
        """
        # Combine stop sequences
        combined_stop = list(self.stop_sequences)
        if stop:
            combined_stop.extend(stop)

        # Format messages into prompt
        prompt = self._format_messages(messages)

        # Prepare generation parameters
        generation_params = {
            "max_new_tokens": kwargs.pop("max_new_tokens", self.max_new_tokens),
            "temperature": kwargs.pop("temperature", self.temperature),
            "top_p": kwargs.pop("top_p", self.top_p),
            "top_k": kwargs.pop("top_k", self.top_k),
            "do_sample": kwargs.pop("do_sample", self.do_sample),
            "repetition_penalty": kwargs.pop(
                "repetition_penalty", self.repetition_penalty
            ),
        }
        generation_params.update(kwargs)

        # Notify callback manager of generation start
        if run_manager:
            run_manager.on_llm_start(
                {"name": self.__class__.__name__}, {"messages": messages}
            )

        try:
            # Generate text using the model
            generated_text = self.model.invoke(prompt, **generation_params)

            # Remove prompt from generated text if present
            if generated_text.startswith(prompt):
                generated_text = generated_text[len(prompt) :]

            # Apply stop sequences
            for stop_seq in combined_stop:
                index = generated_text.find(stop_seq)
                if index != -1:
                    generated_text = generated_text[:index].strip()
                    break

            # Create ChatResult
            result = ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content=generated_text.strip()))
                ]
            )

            # Notify callback manager of completion
            if run_manager:
                run_manager.on_llm_end(result)

            return result

        except Exception as e:
            # Notify callback manager of error
            if run_manager:
                run_manager.on_llm_error(e)
            raise e

    def _call(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        """
        Generate a string response (LangChain compatibility method).

        This method provides compatibility with LangChain's BaseChatModel interface
        by returning just the generated text content as a string.

        Args:
            messages (List[BaseMessage]): List of conversation messages.
            stop (Optional[List[str]]): Additional stop sequences for generation.
            run_manager (Optional[CallbackManagerForLLMRun]): LangChain callback manager.
            **kwargs: Additional generation parameters.

        Returns:
            str: The generated response text.
        """
        result = self._generate(messages, stop, run_manager, **kwargs)
        return result.generations[0].message.content

    def invoke(self, messages: List[BaseMessage], **kwargs) -> Dict[str, Any]:
        """
        High-level interface for chat completion with timing and metadata.

        This method provides a convenient interface for chat completion that includes
        timing information and structured output format. It's the recommended method
        for most use cases.

        Args:
            messages (List[BaseMessage]): List of conversation messages.
            **kwargs: Additional generation parameters that override defaults.

        Returns:
            Dict[str, Any]: Dictionary containing:
                - completion (str): The generated response text
                - generation_time (int): Generation time in milliseconds
                - role (str): Always "assistant"
                - model_name (str): The name of the model used

        Raises:
            ValueError: If messages is not a list.

        Example:
            >>> messages = [HumanMessage(content="What is Python?")]
            >>> response = model.invoke(messages)
            >>> print(f"Response: {response['completion']}")
            >>> print(f"Generated in: {response['generation_time']}ms")
        """
        if not isinstance(messages, list):
            raise ValueError("Input must be a list of messages.")

        # Time the generation
        t0 = time.time()
        result = self._generate(messages, **kwargs)
        t1 = time.time()

        # Extract content
        content = result.generations[0].message.content if result.generations else ""

        return {
            "completion": content.strip(),
            "generation_time": int((t1 - t0) * 1000),  # Convert to milliseconds
            "role": "assistant",
            "model_name": self.model_name,
        }


# CLI
def chat_cli():
    messages = [SystemMessage(content="You are a helpful assistant.")]
    print("Chat started. Type 'exit' to quit, 'clear' to restart conversation.")

    try:
        while True:
            user_input = input("\nYou: ").strip()
            if user_input.lower() == "exit":
                break
            if user_input.lower() == "clear":
                messages = [SystemMessage(content="You are a helpful assistant.")]
                print("Conversation cleared.")
                continue

            messages.append(HumanMessage(content=user_input))
            print("Assistant is typing...")

            try:
                response = model.invoke(messages)
                messages.append(AIMessage(content=response["completion"]))
                print(f"Assistant: {response['completion']}")
            except Exception as e:
                print(f"Error generating response: {e}")

    except KeyboardInterrupt:
        print("\nSession interrupted.")


if __name__ == "__main__":
    print("Loading model...")
    try:
        model = ChatCompletionModel(
            model_name="codellama/CodeLlama-7b-Instruct-hf",
            temperature=0.8,
            max_new_tokens=256,
        )
        print("Model loaded successfully.\n")
    except Exception as e:
        print(f"Error during model loading: {e}")
        exit(1)

    chat_cli()
