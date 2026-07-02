from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from ..config import CODEX_BIN
from .base import BaseProvider


class CodexProvider(BaseProvider):
    """OpenAI Codex CLI（codex exec）。

    Codex 的 stdout 含过程信息，最终回答用 -o <file> 单独收。
    """

    name = "codex"
    label = "Codex"

    def _build_cmd(self, text: str, model: str, effort: str) -> list[str]:
        raise NotImplementedError  # codex 需要特殊处理，不用 _build_cmd

    def run(
        self,
        text: str,
        model: str = "",
        effort: str = "",
        *,
        env_override: Optional[dict[str, str]] = None,
    ) -> str:
        fd, out_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            cmd = [CODEX_BIN, "exec", "--skip-git-repo-check", "-c", "tools.web_search=true"]
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            cmd += ["-o", out_path]
            if model:
                cmd += ["-m", model]
            cmd.append(text)

            proc = self._run_subprocess(cmd, env_override=env_override)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"Codex CLI 失败（exit {proc.returncode}）：{err[:500]}")

            return Path(out_path).read_text(encoding="utf-8").strip()
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
