"""模型字符串解析：(provider, model, effort)。

支持的格式（对齐 CLIProxyAPI 的 model string 规范）：

  backend 前缀格式（推荐，最明确）：
    "claude"                    → (claude, "", "")
    "claude/sonnet"             → (claude, "sonnet", "")
    "claude/sonnet@high"        → (claude, "sonnet", "high")
    "claude@high"               → (claude, "", "high")

  CLIProxyAPI 括号 effort 格式（兼容其他工具的 model string）：
    "claude/sonnet(high)"       → (claude, "sonnet", "high")
    "gpt-5.5(high)"             → (codex, "gpt-5.5", "high")   ← 按模型名推断 backend
    "grok-4(medium)"            → (grok, "grok-4", "medium")
    "gemini-3.5-flash(high)"    → (antigravity, "gemini-3.5-flash", "high")

  模型名推断（无 provider 前缀时）：
    "gpt-*"      → codex
    "o*"         → codex  (o3, o4-mini, ...)
    "claude-*"   → claude
    "grok-*"     → grok
    "gemini-*"   → antigravity
    其他         → ("unknown", original, "")
"""
from __future__ import annotations

import re

# 模型名前缀 → backend 推断规则（顺序有意义，长前缀先匹配）
_MODEL_INFER: list[tuple[str, str]] = [
    ("claude-", "claude"),
    ("gpt-", "codex"),
    ("o1", "codex"),
    ("o3", "codex"),
    ("o4", "codex"),
    ("grok-", "grok"),
    ("gemini-", "antigravity"),
    ("copilot-", "copilot"),
]

# 括号 effort：model(effort) 或 model (effort)
_PAREN_RE = re.compile(r"^(.+?)\s*\((\w+)\)\s*$")

# @effort 后缀
_AT_RE = re.compile(r"^(.+?)@(\w+)$")


def _parse_effort(s: str) -> tuple[str, str]:
    """从 'model@effort' 或 'model(effort)' 里拆出 (model, effort)。"""
    m = _PAREN_RE.match(s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = _AT_RE.match(s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return s, ""


def _infer_backend(model_name: str) -> str:
    """从模型名推断 provider（无显式 provider 前缀时使用）。"""
    lower = model_name.lower()
    for prefix, backend in _MODEL_INFER:
        if lower.startswith(prefix) or lower == prefix.rstrip("-"):
            return backend
    return "unknown"


def parse_model(model_string: str) -> tuple[str, str, str]:
    """解析模型字符串，返回 (provider, model, effort)。

    所有格式见模块文档。
    """
    s = (model_string or "").strip()

    # 有 "/" 分隔符：显式 provider 前缀
    if "/" in s:
        provider, _, rest = s.partition("/")
        provider = provider.strip().lower()
        model, effort = _parse_effort(rest.strip())
        return provider, model, effort

    # 无 "/"：先尝试从已知 backend 名称匹配
    from .providers import SUPPORTED
    for backend in SUPPORTED:
        if s == backend:
            return backend, "", ""
        if s.startswith(backend + "@"):
            effort = s[len(backend) + 1:].strip()
            return backend, "", effort
        if s.startswith(backend + "("):
            model, effort = _parse_effort(s[len(backend):].strip())
            return backend, model, effort

    # 最后：靠模型名推断 backend
    model, effort = _parse_effort(s)
    backend = _infer_backend(model)
    return backend, model, effort


def is_cli_model(model_string: str) -> bool:
    """model_string 是否路由到已支持的 CLI provider。"""
    from .providers import SUPPORTED
    provider, _, _ = parse_model(model_string)
    return provider in SUPPORTED
