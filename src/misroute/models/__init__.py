from misroute.config import ModelConfig
from misroute.models.base import Adapter


def build(cfg: ModelConfig) -> Adapter:
    if cfg.provider == "mock":
        from misroute.models.mock import MockAdapter

        return MockAdapter(cfg)
    if cfg.provider in ("openai-compatible", "azure-openai"):
        from misroute.models.openai_compat import OpenAICompatAdapter

        return OpenAICompatAdapter(cfg)
    from misroute.models.anthropic import AnthropicAdapter

    return AnthropicAdapter(cfg)
