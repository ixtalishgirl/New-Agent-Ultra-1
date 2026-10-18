"""Configuration for the Haley autonomous coding agent."""
from __future__ import annotations
import os
from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    base_url: str = field(default_factory=lambda: os.environ.get(
        "HALEY_API_URL", "https://shun-encrypt-sandpaper.ngrok-free.dev"))
    model_name: str = "haley-qwen2.5-coder-7b"
    timeout: int = 120
    max_tokens: int = 2048
    context_limit: int = 32768


@dataclass
class ExecutionConfig:
    python_timeout: int = 60
    shell_timeout: int = 60
    max_memory_mb: int = 1024
    workspace: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "workspace", "projects"))


@dataclass
class AgentConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    max_iterations: int = 10
    germany_proxy_url: str = field(default_factory=lambda: os.environ.get(
        "GERMANY_PROXY_URL", ""))
    supabase_url: str = field(default_factory=lambda: os.environ.get(
        "SUPABASE_URL", ""))
    supabase_key: str = field(default_factory=lambda: os.environ.get(
        "SUPABASE_ANON_KEY", ""))


def load_config() -> AgentConfig:
    return AgentConfig()
