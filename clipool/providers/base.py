"""Provider 抽象基类。

每个订阅 CLI 对应一个 Provider 子类，实现 run() 方法。
所有 Provider 保证：
  - run() 是纯函数（同一时刻多线程安全）
  - env_override 通过 subprocess env 参数注入，不修改全局 os.environ
  - 错误统一抛 RuntimeError，由 pool + server 层决定是否重试
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from ..config import CLI_TIMEOUT

_CLEAN_ENV_KEYS = frozenset(
    {
        "PATH",
        "LANG",
        "TMPDIR",
        "TEMP",
        "TMP",
        "TZ",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


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
        clean_env: bool = False,
    ) -> subprocess.CompletedProcess:
        """线程安全地运行 subprocess，env_override 作为额外环境变量注入。

        stdout/stderr 落临时文件而不用 PIPE：agy 这类 CLI 会 spawn 孙进程
        （钥匙串查询、language server），它们继承管道且可能比 CLI 活得久——
        PIPE 模式要等**所有**持有者关闭写端才返回（实测被卡住过），
        文件模式在直接子进程退出后立即返回，孤儿进程与我们无关。
        """
        env = self._subprocess_env(env_override, clean_env=clean_env)
        out_fd, out_path = tempfile.mkstemp(suffix=".stdout")
        err_fd, err_path = tempfile.mkstemp(suffix=".stderr")
        proc: Optional[subprocess.Popen] = None
        try:
            with os.fdopen(out_fd, "w", encoding="utf-8") as out_f, \
                 os.fdopen(err_fd, "w", encoding="utf-8") as err_f:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=out_f,
                    stderr=err_f,
                    text=True,
                    cwd=tempfile.gettempdir(),
                    env=env,
                    start_new_session=os.name != "nt",
                )
                try:
                    proc.communicate(input=stdin, timeout=CLI_TIMEOUT)
                except subprocess.TimeoutExpired as exc:
                    self._terminate_process_tree(proc)
                    raise RuntimeError(
                        f"{self.label} CLI 调用超时（>{CLI_TIMEOUT}s）。"
                    ) from exc
            stdout = Path(out_path).read_text(encoding="utf-8", errors="replace")
            stderr = Path(err_path).read_text(encoding="utf-8", errors="replace")
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except FileNotFoundError:
            raise RuntimeError(
                f"找不到 {self.label} CLI（{cmd[0]}）。"
                f"请先安装并登录，或在 .env 里设置 {self.name.upper()}_CLI_BIN。"
            )
        finally:
            if proc is not None and proc.poll() is None:
                self._terminate_process_tree(proc)
            for p in (out_path, err_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    @staticmethod
    def _subprocess_env(
        env_override: Optional[dict[str, str]], *, clean_env: bool
    ) -> Optional[dict[str, str]]:
        """Build subprocess env; clean mode uses an allowlist before explicit overrides."""
        if not clean_env and not env_override:
            return None
        if clean_env:
            env = {
                key: value
                for key, value in os.environ.items()
                if key in _CLEAN_ENV_KEYS or key.startswith("LC_")
            }
        else:
            env = os.environ.copy()
        if env_override:
            env.update(env_override)
        return env

    @staticmethod
    def _terminate_process_tree(proc: subprocess.Popen, grace_seconds: float = 1.0) -> None:
        """终止整个 CLI 进程组，避免 timeout 后 language server 等孙进程遗留。"""
        if os.name == "nt":  # pragma: no cover - Windows best-effort fallback
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    proc.kill()
            return

        process_group = proc.pid  # start_new_session=True 保证 leader pid == pgid
        try:
            os.killpg(process_group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

        # 不能只等 leader：leader 可以先退出，但忽略 TERM 的孙进程仍在同一组。
        # 固定给整组宽限期；不用 killpg(..., 0) 探活，因为某些沙箱会对
        # 只读探活返回 EPERM，且 leader 状态不能代表孙进程状态。
        if grace_seconds > 0:
            time.sleep(grace_seconds)
        try:
            # grace 后总是针对原进程组补 SIGKILL；即使 leader 已退出也不提前 return。
            os.killpg(process_group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:  # pragma: no cover - pathological OS state
            pass
