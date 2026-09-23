"""Model adapters."""

from .llm import ChatCompletionsLLM, LLM, ModelError, ModelProtocolError

__all__ = ["ChatCompletionsLLM", "LLM", "ModelError", "ModelProtocolError"]
