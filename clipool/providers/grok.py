from __future__ import annotations

from ..config import GROK_BIN
from .base import BaseProvider


class GrokProvider(BaseProvider):
    """xAI Grok CLI（grok -p）。"""

    name = "grok"
    label = "Grok"

    def _build_cmd(self, text: str, model: str, effort: str) -> list[str]:
        cmd = [GROK_BIN, "-p", text, "--output-format", "plain"]
        if effort:
            cmd += ["--reasoning-effort", effort]
        if model:
            cmd += ["-m", model]
        return cmd

    def run(self, text, model="", effort="", *, env_override=None):
        try:
            return super().run(text, model, effort, env_override=env_override)
        except RuntimeError as exc:
            # grok CLI 的模型清单随订阅变化（如没有 grok-4）；指定模型无效时
            # 回退到 CLI 默认模型再试一次，保证工作流不因模型名不符而中断。
            if model and "unknown model id" in str(exc).lower():
                return super().run(text, "", effort, env_override=env_override)
            raise
