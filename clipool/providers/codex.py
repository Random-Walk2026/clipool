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

    @staticmethod
    def _unsafe_mode_enabled() -> bool:
        """Explicit escape hatch for callers that intentionally trust prompts."""
        return os.environ.get("CLIPOOL_CODEX_UNSAFE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def run(
        self,
        text: str,
        model: str = "",
        effort: str = "",
        *,
        env_override: Optional[dict[str, str]] = None,
    ) -> str:
        # 每次请求使用独立目录，避免不可信 prompt 继承 clipool 自身仓库作为工作区。
        # TemporaryDirectory 通常已经是 0700；显式 chmod 让该安全边界不依赖 umask。
        with tempfile.TemporaryDirectory(prefix="clipool-codex-") as workdir:
            os.chmod(workdir, 0o700)
            out_path = str(Path(workdir) / "last-message.txt")
            cmd = [CODEX_BIN, "exec", "--skip-git-repo-check"]
            unsafe_mode = self._unsafe_mode_enabled()
            if not unsafe_mode:
                cmd += [
                    "--sandbox",
                    "read-only",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "-C",
                    workdir,
                ]
            cmd += ["-c", "tools.web_search=true"]
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            cmd += ["-o", out_path]
            if model:
                cmd += ["-m", model]
            cmd.append(text)

            subprocess_env = env_override
            if not unsafe_mode:
                # Codex 必须读它自己的 profile，但不应继承 clipool API key、
                # 其它 provider token、SSH agent 等服务进程机密。HOME 指向临时目录，
                # CODEX_HOME 则精确保留所选账号的认证目录。
                codex_home = (env_override or {}).get("CODEX_HOME") or os.environ.get(
                    "CODEX_HOME"
                ) or str(Path.home() / ".codex")
                subprocess_env = {"HOME": workdir, "CODEX_HOME": codex_home}
            proc = self._run_subprocess(
                cmd,
                env_override=subprocess_env,
                clean_env=not unsafe_mode,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"Codex CLI 失败（exit {proc.returncode}）：{err[:500]}")

            return Path(out_path).read_text(encoding="utf-8").strip()
