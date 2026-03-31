# domain/ai/services/llm_service.py
from abc import ABC, abstractmethod
from typing import AsyncIterator
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage


class GenerationConfig:
    """生成配置"""
    def __init__(
        self,
        model: str = "claude-3-5-sonnet-20241022",
        max_tokens: int = 4096,
        temperature: float = 1.0
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature


class GenerationResult:
    """生成结果"""
    def __init__(self, content: str, token_usage: TokenUsage):
        self.content = content
        self.token_usage = token_usage


class LLMService(ABC):
    """LLM 服务接口（领域服务）"""

    @abstractmethod
    async def generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> GenerationResult:
        """生成内容"""
        pass

    @abstractmethod
    async def stream_generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> AsyncIterator[str]:
        """流式生成内容"""
        pass
