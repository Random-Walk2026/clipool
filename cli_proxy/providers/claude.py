from __future__ import annotations

from ..config import CLAUDE_BIN
from .base import BaseProvider


class ClaudeProvider(BaseProvider):
    """Claude Code CLI（claude -p）。

    认证：
      - 默认用 CLI 自身登录态（~/.claude/）
      - 多账号时通过 env_override["CLAUDE_CODE_OAUTH_TOKEN"] 注入
    """

    name = "claude"
    label = "Claude"

    def _build_cmd(self, text: str, model: str, effort: str) -> list[str]:
        # --allowedTools WebSearch 仅供 collect 节点兜底联网；普通节点不会主动触发。
        # CLAUDE_CODE_OAUTH_TOKEN 通过 env_override 注入（BaseProvider._run_subprocess）。
        cmd = [CLAUDE_BIN, "-p", "--output-format", "text", "--allowedTools", "WebSearch"]
        if effort:
            cmd += ["--effort", effort]
        if model:
            cmd += ["--model", model]
        return cmd

    def run(self, text, model="", effort="", *, env_override=None):
        cmd = self._build_cmd(text, model, effort)
        proc = self._run_subprocess(cmd, stdin=text, env_override=env_override)
        return self._extract_output(proc, text)
