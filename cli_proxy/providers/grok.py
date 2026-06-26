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
