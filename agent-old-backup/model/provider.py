"""Model provider abstraction — the model can be swapped without rewriting the agent."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ModelResponse:
    text: str
    raw: dict | None = None


class ModelProvider(ABC):
    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str,
                 max_tokens: int = 2048) -> ModelResponse:
        ...

    @abstractmethod
    def health_check(self) -> bool:
        ...
