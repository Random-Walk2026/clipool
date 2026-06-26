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
