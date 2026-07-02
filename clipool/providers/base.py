"""Provider 抽象基类。

每个订阅 CLI 对应一个 Provider 子类，实现 run() 方法。
所有 Provider 保证：
  - run() 是纯函数（同一时刻多线程安全）
  - env_override 通过 subprocess env 参数注入，不修改全局 os.environ
  - 错误统一抛 RuntimeError，由 pool + server 层决定是否重试
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from ..config import CLI_TIMEOUT


class BaseProvider(ABC):
    """所有 CLI 后端的抽象基类。"""

    name: str = ""           # backend 名称，子类必须设置
    label: str = ""          # 用于错误信息的可读名称

    def run(
        self,
        text: str,
        model: str = "",
        effort: str = "",
        *,
        env_override: Optional[dict[str, str]] = None,
    ) -> str:
        """执行 CLI 调用，返回纯文本结果。

        线程安全：env_override 通过 subprocess env 参数传入，不修改 os.environ。
        """
        cmd = self._build_cmd(text, model, effort)
        proc = self._run_subprocess(cmd, env_override=env_override)
        return self._extract_output(proc, text)

    @abstractmethod
    def _build_cmd(self, text: str, model: str, effort: str) -> list[str]:
        """构造 subprocess 命令列表。"""

    def _extract_output(self, proc: subprocess.CompletedProcess, text: str) -> str:
        """从 CompletedProcess 提取输出文本（子类可覆盖，如 codex 需读文件）。"""
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                f"{self.label} CLI 失败（exit {proc.returncode}）：{err[:500]}"
            )
        out = (proc.stdout or "").strip()
        return out

    def _run_subprocess(
        self,
        cmd: list[str],
        *,
        stdin: Optional[str] = None,
        env_override: Optional[dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        """线程安全地运行 subprocess，env_override 作为额外环境变量注入。

        stdout/stderr 落临时文件而不用 PIPE：agy 这类 CLI 会 spawn 孙进程
        （钥匙串查询、language server），它们继承管道且可能比 CLI 活得久——
        PIPE 模式要等**所有**持有者关闭写端才返回（实测被卡住过），
        文件模式在直接子进程退出后立即返回，孤儿进程与我们无关。
        """
        env: Optional[dict[str, str]] = None
        if env_override:
            env = os.environ.copy()
            env.update(env_override)
        out_fd, out_path = tempfile.mkstemp(suffix=".stdout")
        err_fd, err_path = tempfile.mkstemp(suffix=".stderr")
        try:
            with os.fdopen(out_fd, "w", encoding="utf-8") as out_f, \
                 os.fdopen(err_fd, "w", encoding="utf-8") as err_f:
                proc = subprocess.run(
                    cmd,
                    input=stdin,
                    stdout=out_f,
                    stderr=err_f,
                    text=True,
                    timeout=CLI_TIMEOUT,
                    cwd=tempfile.gettempdir(),
                    env=env,
                    start_new_session=True,  # 孙进程不挂在我们的进程组下
                )
            stdout = Path(out_path).read_text(encoding="utf-8", errors="replace")
            stderr = Path(err_path).read_text(encoding="utf-8", errors="replace")
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except FileNotFoundError:
            raise RuntimeError(
                f"找不到 {self.label} CLI（{cmd[0]}）。"
                f"请先安装并登录，或在 .env 里设置 {self.name.upper()}_CLI_BIN。"
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{self.label} CLI 调用超时（>{CLI_TIMEOUT}s）。")
        finally:
            for p in (out_path, err_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
