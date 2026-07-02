from __future__ import annotations

from ..config import COPILOT_BIN
from .base import BaseProvider


class CopilotProvider(BaseProvider):
    """GitHub Copilot CLI（copilot -p）。

    认证：COPILOT_GITHUB_TOKEN（GitHub Personal Access Token 或 fine-grained PAT）。
    多账号时通过 env_override["COPILOT_GITHUB_TOKEN"] 注入。
    """

    name = "copilot"
    label = "Copilot"

    def _build_cmd(self, text: str, model: str, effort: str) -> list[str]:
        # --available-tools（不带参数列表）= 禁用工具；禁止改成 --allow-all-tools。
        # --silent 只输出最终回答，不带进度信息。
        cmd = [COPILOT_BIN, "-p", text, "--available-tools", "--silent"]
        if effort:
            cmd += ["--reasoning-effort", effort]
        if model:
            cmd += ["--model", model]
        return cmd

    def run(self, text, model="", effort="", *, env_override=None):
        try:
            return super().run(text, model, effort, env_override=env_override)
        except RuntimeError as exc:
            # Copilot 可用模型随订阅变化；指定模型无效时回退到 CLI 默认模型再试一次，
            # 保证工作流不因模型名不符而中断（对齐 GrokProvider 的处理）。
            if model and "not available" in str(exc).lower():
                return super().run(text, "", effort, env_override=env_override)
            raise
