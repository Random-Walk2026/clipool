"""Provider 注册表：统一入口，按 backend 名称获取 Provider 实例。"""
from __future__ import annotations

from .base import BaseProvider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .grok import GrokProvider
from .antigravity import AntigravityProvider
from .copilot import CopilotProvider

_REGISTRY: dict[str, BaseProvider] = {
    "claude": ClaudeProvider(),
    "codex": CodexProvider(),
    "grok": GrokProvider(),
    "antigravity": AntigravityProvider(),
    "copilot": CopilotProvider(),
}

SUPPORTED: tuple[str, ...] = tuple(_REGISTRY.keys())


def get_provider(backend: str) -> BaseProvider:
    provider = _REGISTRY.get(backend)
    if provider is None:
        raise RuntimeError(
            f"未知 provider：{backend!r}。支持：{', '.join(SUPPORTED)}"
        )
    return provider
