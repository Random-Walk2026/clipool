"""Antigravity (Google Cloud Code Assist) CLI Provider。

架构说明（对齐 CLIProxyAPI 的分析结果）：
  Antigravity 底层是 Google Cloud Code Assist API：
    - 端点：cloudcode-pa.googleapis.com
    - 认证：Google OAuth2（Bearer access_token）
    - 与普通 Gemini API（generativelanguage.googleapis.com）完全不同

  与其他 CLI 的关键区别：
    - 不支持独立的 --effort 参数
    - 思考强度编码在「模型名变体」里（如 gemini-3.5-flash-high）
    - 账号 token 是 Google OAuth access_token（非 API key）

模型变体映射表（effort → model variant suffix）：
  "gemini-3.5-flash" + "high"  → "gemini-3.5-flash-high"
  "gemini-3.1-pro" + "low"     → "gemini-3.1-pro-low"
  "claude-sonnet-4-6" + "high" → "claude-sonnet-4-6-thinking"
"""
from __future__ import annotations

from ..config import ANTIGRAVITY_BIN
from .base import BaseProvider

_EFFORT_SUFFIXES = ("low", "medium", "high", "thinking")

# 模型名 → {effort → 变体名}
_MODEL_VARIANTS: dict[str, dict[str, str]] = {
    "gemini-3.5-flash": {
        "low": "gemini-3.5-flash-low",
        "medium": "gemini-3.5-flash-medium",
        "high": "gemini-3.5-flash-high",
    },
    "gemini-3.1-pro": {
        "low": "gemini-3.1-pro-low",
        "high": "gemini-3.1-pro-high",
    },
    "claude-sonnet-4-6": {
        "low": "claude-sonnet-4-6-thinking",
        "medium": "claude-sonnet-4-6-thinking",
        "high": "claude-sonnet-4-6-thinking",
        "thinking": "claude-sonnet-4-6-thinking",
    },
    "claude-opus-4-6": {
        "low": "claude-opus-4-6-thinking",
        "medium": "claude-opus-4-6-thinking",
        "high": "claude-opus-4-6-thinking",
        "thinking": "claude-opus-4-6-thinking",
    },
}


def resolve_variant(model: str, effort: str) -> str:
    """把模型名 + effort 解析为 Antigravity 的 --model 参数值。

    已经是变体名（以 -low/-high/-thinking 结尾）时原样返回。
    没有匹配变体时也原样返回（由 CLI 自己处理）。
    """
    chosen = (model or "").strip()
    if not chosen or not effort:
        return chosen
    lower = chosen.lower()
    if any(lower.endswith(f"-{s}") for s in _EFFORT_SUFFIXES):
        return chosen  # 已经是变体名
    variants = _MODEL_VARIANTS.get(lower)
    if not variants:
        return chosen
    return variants.get(effort.strip().lower(), chosen)


# 向后兼容：llm_backends.py 用到这个名字
antigravity_model_variant = resolve_variant


class AntigravityProvider(BaseProvider):
    """Antigravity (Google Cloud Code Assist) CLI（agy --print）。

    认证：Google OAuth token 可通过 env_override["ANTIGRAVITY_TOKEN"] 注入
    （具体 env var 名称取决于 agy CLI 的实现）。
    """

    name = "antigravity"
    label = "Antigravity"

    def _build_cmd(self, text: str, model: str, effort: str) -> list[str]:
        cmd = [ANTIGRAVITY_BIN, "--print", text]
        variant = resolve_variant(model, effort)
        if variant:
            cmd += ["--model", variant]
        return cmd
