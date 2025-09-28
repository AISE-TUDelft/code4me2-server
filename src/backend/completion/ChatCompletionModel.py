"""
ChatCompletionModel: A LangChain-compatible chat model implementation using HuggingFace Transformers.

This module provides a custom chat model that integrates with the LangChain framework while
using HuggingFace transformers for local text generation. It supports various language models
and provides a convenient interface for chat-based interactions.
"""

import time
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from backend.completion.CompletionModel import CompletionModel
import logging


class ChatCompletionModel(CompletionModel, BaseChatModel):
    """
    A LangChain-compatible chat model that uses HuggingFace transformers for local text generation.

    This class extends LangChain's BaseChatModel to provide chat completion functionality
    using locally-hosted language models from HuggingFace. It supports message formatting,
    generation parameters customization, and proper integration with LangChain callbacks.

    Attributes:
        model_name (str): The HuggingFace model identifier to load.
                         Default: "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct"
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

    # Message Formatting
    system_prefix: str = "[SYSTEM]"
    user_prefix: str = "[USER]"
    assistant_prefix: str = "[ASSISTANT]"
    unknown_prefix: str = "[UNKNOWN]"

    def __init__(self, **data: Any):
        """
        Initialize the ChatCompletionModel.
        """
        super().__init__(**data)

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
                formatted += """
                Before you answer, please provide a very short 3-6 word title for the user's question.
                Place the summary in the tag in in a set of [Title] tags.
                Example: [Title] How to use Python? [/Title]
                """
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
        # Configure stop sequences
        stop_sequences = [self.system_prefix, self.user_prefix, self.assistant_prefix]
        combined_stop = list(stop_sequences)
        if stop:
            combined_stop.extend(stop)

        # Format messages into prompt
        prompt = self._format_messages(messages)

        # Notify callback manager of generation start
        if run_manager:
            run_manager.on_llm_start(
                {"name": self.__class__.__name__}, {"messages": messages}
            )

        try:
            # Generate text using the shared pipeline
            generated_text = self.pipeline.invoke(prompt, **kwargs)

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

    def warmup(self) -> None:
        """
        Perform a warm-up run to initialize the model and reduce cold-start latency.
        This helps with initial compilation overhead when using torch.compile.
        """
        logging.info(f"Performing warm-up for model {self.model_name}...")
        try:

            response = self.invoke(
                [
                    SystemMessage(content="You are a helpful assistant."),
                    HumanMessage(content="Hello, how are you?"),
                ]
            )
            logging.info(
                f"Warm-up completed successfully for model {self.model_name}.\nQuestion: Hello, how are you?\nResponse: {response['completion']}"
            )
        except Exception as e:
            logging.error(f"Warm-up failed for model {self.model_name}: {str(e)}")
            raise e


# CLI
def chat_cli(model):
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
        )

        print("Model loaded successfully.\n")
    except Exception as e:
        print(f"Error during model loading: {e}")
        exit(1)

    chat_cli(model)
