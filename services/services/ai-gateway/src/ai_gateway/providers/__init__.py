from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ai_gateway.models import SSEChunk


class BaseProvider(ABC):
    @abstractmethod
    def chat_completion(
        self,
        messages: list[dict],
        model: str,
        api_key: str,
        base_url: str,
        stream: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> AsyncIterator[SSEChunk]: ...


_PROVIDERS: dict[str, type[BaseProvider]] = {}


def register_provider(provider_type: str, cls: type[BaseProvider]) -> None:
    _PROVIDERS[provider_type] = cls


def get_provider(provider_type: str) -> BaseProvider:
    cls = _PROVIDERS.get(provider_type)
    if not cls:
        raise ValueError(f"Unsupported provider_type: {provider_type}")
    return cls()


# Register built-in providers
from ai_gateway.providers.anthropic import AnthropicProvider  # noqa: E402
from ai_gateway.providers.openai_compat import OpenAICompatibleProvider  # noqa: E402

register_provider("openai_compatible", OpenAICompatibleProvider)
register_provider("anthropic", AnthropicProvider)
