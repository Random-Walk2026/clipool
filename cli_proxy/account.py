"""账号模型与认证文件管理。

认证文件存放在 AUTH_DIR（默认 ~/.cli_proxy_api/）下，格式与 CLIProxyAPI 兼容：

    claude_work.json        → Claude 账号，文件名随意（backend 字段决定类型）
    codex_personal.json     → Codex 账号
    copilot_main.json       → Copilot 账号

每个文件的 JSON 结构：

    {
        "type": "claude",                    # backend 名称
        "email": "user@example.com",         # 账号标识（可选，仅用于显示）
        "token": "sk-ant-xxx...",            # 访问令牌 / OAuth token
        "refresh_token": "...",              # 可选，自动刷新用
        "enabled": true                      # false 则跳过此账号
    }

也可直接在 .env 里配置单账号（无需文件）：

    CLAUDE_CODE_OAUTH_TOKEN=sk-ant-xxx...   # cli_proxy 会自动读取
    COPILOT_GITHUB_TOKEN=ghp_xxx...
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import DEFAULT_HOST, DEFAULT_PORT

AUTH_DIR = Path(os.environ.get("CLI_PROXY_AUTH_DIR", Path.home() / ".cli_proxy_api"))

# token 型后端：CLI 直接认一个环境变量里的 token，注入不同 token 即切换账号。
# （codex / antigravity 不在此列——它们没有「一个 token 变量搞定」的入口，靠 _HOME_ENV 目录隔离。）
_TOKEN_ENV: dict[str, str] = {
    "claude": "CLAUDE_CODE_OAUTH_TOKEN",
    "grok": "GROK_API_KEY",
    "copilot": "COPILOT_GITHUB_TOKEN",
}

# 目录型后端：CLI 把登录态存在一个目录里，给每个账号一份独立 profile 目录，
# 调用时用这个环境变量把 CLI 指过去，实现 subprocess 级账号隔离（每个子进程独立 env，线程安全）。
#   codex       → CODEX_HOME       （官方支持，默认 ~/.codex）
#   claude      → CLAUDE_CONFIG_DIR（可选；claude 多用 token 注入更简单）
#   antigravity → HOME             （agy 登录态在 ~/.gemini/，靠 HOME 重定向；前提是 token 文件型而非钥匙串）
_HOME_ENV: dict[str, str] = {
    "codex": "CODEX_HOME",
    "claude": "CLAUDE_CONFIG_DIR",
    "antigravity": "HOME",
}

# .env 里的兜底 token（无账号文件时使用）——只有 token 型后端能从 env 兜底。
_ENV_FALLBACK: dict[str, str] = {
    "claude": "CLAUDE_CODE_OAUTH_TOKEN",
    "grok": "GROK_API_KEY",
    "copilot": "COPILOT_GITHUB_TOKEN",
}


@dataclass
class Account:
    """单个订阅账号。"""
    backend: str
    id: str                          # 唯一标识，通常是 email 或文件名 stem
    token: str = ""                  # 访问令牌（token 型后端用）
    refresh_token: str = ""
    home: str = ""                   # 该账号独立的登录态目录（目录型后端用，注入 CODEX_HOME / HOME 等）
    extra_env: dict = field(default_factory=dict)  # 账号 JSON 里 "env": {...} 的任意补充环境变量
    enabled: bool = True

    # 冷却状态（运行时，不序列化）
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cooling_until: float = field(default=0.0, init=False, repr=False)
    _error_count: int = field(default=0, init=False, repr=False)

    @property
    def is_cooling(self) -> bool:
        return time.monotonic() < self._cooling_until

    @property
    def error_count(self) -> int:
        return self._error_count

    def cool_down(self, seconds: float) -> None:
        with self._lock:
            self._cooling_until = time.monotonic() + seconds
            self._error_count += 1

    def reset(self) -> None:
        with self._lock:
            self._cooling_until = 0.0
            self._error_count = 0

    def env_override(self) -> dict[str, str]:
        """调 CLI 时注入的环境变量：登录态目录重定向 + token + 额外 env。

        - 目录型后端（codex/agy）：注入 CODEX_HOME / HOME 指向该账号独立 profile 目录，
          每个子进程拿到不同目录即切换账号；env 随 subprocess 传入，不改全局，线程安全。
        - token 型后端（claude/copilot/grok）：注入对应 token 环境变量。
        """
        env: dict[str, str] = {}
        home_key = _HOME_ENV.get(self.backend)
        if home_key and self.home:
            env[home_key] = self.home
        token_key = _TOKEN_ENV.get(self.backend)
        if token_key and self.token:
            env[token_key] = self.token
        if self.extra_env:
            env.update(self.extra_env)
        return env

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "id": self.id,
            "token": self.token[:8] + "…" if self.token else "",  # 脱敏
            "home": self.home,
            "enabled": self.enabled,
            "cooling": self.is_cooling,
            "error_count": self._error_count,
        }


# ── 认证文件加载 ──────────────────────────────────────────────────────────────

def _account_from_file(path: Path) -> Optional[Account]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    backend = str(data.get("type", "")).strip().lower()
    if not backend:
        return None
    token = str(data.get("token", data.get("access_token", data.get("key", "")))).strip()
    home = str(data.get("home", "")).strip()
    if home:
        home = str(Path(home).expanduser())
    raw_env = data.get("env")
    extra_env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
    # 既无 token 也无 home 也无 env → 这条目没有任何可注入信息，跳过（占位符文件不会污染池子）。
    if not token and not home and not extra_env:
        return None
    return Account(
        backend=backend,
        id=str(data.get("email", path.stem)),
        token=token,
        refresh_token=str(data.get("refresh_token", "")),
        home=home,
        extra_env=extra_env,
        enabled=bool(data.get("enabled", True)),
    )


def _account_from_env(backend: str) -> Optional[Account]:
    """从 .env / 环境变量读取兜底单账号。"""
    env_key = _ENV_FALLBACK.get(backend)
    if not env_key:
        return None
    token = os.environ.get(env_key, "").strip()
    if not token:
        return None
    return Account(backend=backend, id=f"env:{backend}", token=token)


def load_accounts(backend: Optional[str] = None) -> list[Account]:
    """从 AUTH_DIR 加载认证文件，合并 .env 兜底账号。

    backend 为 None 时加载所有 backend 的账号。
    """
    accounts: list[Account] = []
    seen_ids: set[str] = set()

    if AUTH_DIR.is_dir():
        for f in sorted(AUTH_DIR.glob("*.json")):
            acc = _account_from_file(f)
            if acc and acc.enabled and (backend is None or acc.backend == backend):
                key = f"{acc.backend}:{acc.id}"
                if key not in seen_ids:
                    accounts.append(acc)
                    seen_ids.add(key)

    # 补充 .env 兜底（每个 backend 最多一个兜底账号）
    backends_to_check = [backend] if backend else list(_ENV_FALLBACK.keys())
    for b in backends_to_check:
        if not any(a.backend == b for a in accounts):
            acc = _account_from_env(b)
            if acc:
                key = f"{acc.backend}:{acc.id}"
                if key not in seen_ids:
                    accounts.append(acc)
                    seen_ids.add(key)

    return accounts


def save_account(account: Account, name: Optional[str] = None) -> Path:
    """把账号信息写入 AUTH_DIR（供命令行注册账号使用）。"""
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    stem = name or f"{account.backend}_{account.id.replace('@', '_').replace('.', '_')}"
    path = AUTH_DIR / f"{stem}.json"
    path.write_text(
        json.dumps(
            {
                "type": account.backend,
                "email": account.id,
                "token": account.token,
                "refresh_token": account.refresh_token,
                "home": account.home,
                "enabled": account.enabled,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
