"""账号模型与认证文件管理。

认证文件存放在 AUTH_DIR（默认 ~/.clipool/）下，格式与 CLIProxyAPI 兼容：

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

    CLAUDE_CODE_OAUTH_TOKEN=sk-ant-xxx...   # clipool 会自动读取
    COPILOT_GITHUB_TOKEN=ghp_xxx...
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

try:  # Unix/macOS：账号文件也可能被另一个 clipool 进程刷新。
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from .config import load_project_env

logger = logging.getLogger(__name__)

# token 提前刷新余量（秒）：expiry 距现在不足这个值就当作需要刷新。
# 对齐 cockpit-tools 的 ensure_fresh_token（300s 余量），比只在过期瞬间刷新更稳。
REFRESH_SKEW_SECONDS = 300

load_project_env()


def _auth_dir_from_env() -> Path:
    configured = os.environ.get("CLIPOOL_AUTH_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".clipool"


AUTH_DIR = _auth_dir_from_env()

_persist_locks_guard = threading.Lock()
_persist_locks: dict[str, threading.Lock] = {}
_persist_revision_lock = threading.Lock()
_persist_revision = 0
_PERSIST_FIELDS = frozenset(
    {"token", "refresh_token", "enabled", "quota", "expiry", "disabled"}
)


def persist_revision() -> int:
    """返回本进程内账号文件成功原子写入的世代。"""
    with _persist_revision_lock:
        return _persist_revision


def _bump_persist_revision() -> None:
    global _persist_revision
    with _persist_revision_lock:
        _persist_revision += 1


def _persist_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _persist_locks_guard:
        return _persist_locks.setdefault(key, threading.Lock())


@contextmanager
def _locked_account_file(path: Path) -> Iterator[None]:
    """同进程线程锁 + Unix advisory lock，串行化同一账号文件更新。"""
    thread_lock = _persist_lock_for(path)
    with thread_lock:
        lock_path = path.with_name(f".{path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _atomic_write_json(path: Path, data: dict) -> None:
    """用同目录临时文件 + fsync + replace 原子写入，并强制凭据权限为 0600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # 部分文件系统不允许对目录 fsync；文件本身仍已原子替换。
        _bump_persist_revision()
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

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

_KNOWN_BACKENDS = frozenset({"claude", "codex", "grok", "copilot", "antigravity"})


@dataclass
class Account:
    """单个订阅账号。"""
    backend: str
    id: str                          # 唯一标识，通常是 email 或文件名 stem
    token: str = ""                  # 访问令牌（token 型后端用）
    refresh_token: str = ""
    home: str = ""                   # 该账号独立的登录态目录（目录型后端用，注入 CODEX_HOME / HOME 等）
    extra_env: dict = field(default_factory=dict)  # 账号 JSON 里 "env": {...} 的任意补充环境变量
    # Codex 账号按各自 CODEX_HOME/models_cache.json 暴露模型能力。
    # None 表示目录没有可读缓存（为兼容旧安装，不据此拦截）；空集合表示缓存有效但没有 list 模型。
    supported_models: Optional[frozenset[str]] = None
    enabled: bool = True

    # ── 路由权重（主备号）──────────────────────────────────────────────────
    # priority：数字越小越优先；只有当某个 priority 组内全部不可用时才溢出到下一组（备号）。
    # weight：同一 priority 组内的加权轮换份额（越大命中越多），最小按 1 计。
    priority: int = 0
    weight: int = 1

    # ── token 生命周期（持久化）─────────────────────────────────────────────
    # 直连 HTTP 后端（antigravity_http 等）把 access_token 的过期时间存进来，
    # 调用前用 needs_refresh() 提前刷新；CLI 型后端（claude/codex…）token 由 CLI 自管，expiry 留 0。
    expiry: float = 0.0              # access_token 过期的 unix 时间戳（0 = 未知/不追踪）

    # ── 永久禁用（持久化）──────────────────────────────────────────────────
    # 区别于「冷却」（临时、内存态）：disabled_reason 是 token 彻底失效（如 invalid_grant）这类
    # 需要人工/刷新才能恢复的问题，落盘后重启不再死磕；下次刷新成功自动解除（见 clear_disabled）。
    disabled_reason: str = ""        # 非空即视为永久禁用
    disabled_at: float = 0.0         # 禁用发生的 unix 时间戳

    source_path: str = ""            # 该账号来源的 JSON 文件路径（用于刷新/禁用状态写回；env 兜底账号为空）

    # ── 额度快照（持久化，懒刷新）─────────────────────────────────────────────
    # 由 quota.py 通过各 provider 的 usage 端点抓取并归一化后写入；状态面板直接读这里的缓存，
    # 不在每次轮询时实时拉取（usage 端点昂贵且限流）。手动「刷新额度」时才更新。
    quota: Optional[dict] = None     # 归一化额度：{"five_hour":{...}, "weekly":{...}, "plan_type":...}
    quota_error: str = ""            # 上次刷新失败原因（如 refresh_token_invalidated）
    quota_updated_at: float = 0.0    # 额度快照的 unix 时间戳

    # 冷却状态（运行时，不序列化）
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cooling_until: float = field(default=0.0, init=False, repr=False)
    _error_count: int = field(default=0, init=False, repr=False)
    _credential_generation: tuple[str, str, str, str, str, str] = field(
        default=("", "", "", "", "", ""), init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._capture_credential_generation()

    def _capture_credential_generation(self) -> None:
        """捕获加载时凭据世代，仅用于 reload 合并临时冷却/租约。"""
        credential_file = ""
        if self.home:
            relative: Optional[Path] = None
            if self.backend == "codex":
                relative = Path("auth.json")
            elif self.backend == "antigravity":
                relative = (
                    Path(".gemini")
                    / "antigravity-cli"
                    / "antigravity-oauth-token"
                )
            if relative is not None:
                path = Path(self.home).expanduser() / relative
                try:
                    info = path.stat()
                    credential_file = ":".join(
                        str(value)
                        for value in (
                            info.st_dev,
                            info.st_ino,
                            info.st_mtime_ns,
                            info.st_size,
                        )
                    )
                except OSError:
                    credential_file = "missing"
        self._credential_generation = (
            self.source_path,
            self.home,
            self.token,
            self.refresh_token,
            json.dumps(self.extra_env, ensure_ascii=False, sort_keys=True, default=str),
            credential_file,
        )

    @property
    def is_cooling(self) -> bool:
        return time.monotonic() < self._cooling_until

    @property
    def is_disabled(self) -> bool:
        """永久禁用：显式 enabled=False，或带有 disabled_reason。"""
        return not self.enabled or bool(self.disabled_reason)

    @property
    def is_available(self) -> bool:
        return self.enabled and not self.is_disabled and not self.is_cooling

    @property
    def is_invalid_grant_disabled(self) -> bool:
        """因 token 失效（invalid_grant）被禁用——刷新成功后可自动解除。"""
        return bool(self.disabled_reason) and self.disabled_reason.startswith("invalid_grant")

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

    def needs_refresh(self, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
        """access_token 是否需要提前刷新（仅对追踪 expiry 的直连后端有意义）。"""
        if not self.expiry:
            return False
        return time.time() + skew_seconds >= self.expiry

    def disable(self, reason: str) -> None:
        """永久禁用（落盘语义）：标记原因与时间，并把 enabled 置 False。"""
        with self._lock:
            self.enabled = False
            self.disabled_reason = reason
            self.disabled_at = time.time()

    def clear_disabled(self) -> None:
        """解除永久禁用（三个字段一起重置）。"""
        with self._lock:
            self.enabled = True
            self.disabled_reason = ""
            self.disabled_at = 0.0

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

    def supports_model(self, model: str) -> bool:
        """当前账号是否能执行目标模型。

        目前只有 Codex 的订阅模型与登录账号强绑定，并且 CLI 会把账号可见模型
        缓存在各自 ``CODEX_HOME/models_cache.json``。其它 backend、未指定模型、
        或没有可读缓存时保持向后兼容，允许调度器继续尝试。
        """
        if self.backend != "codex" or not model or self.supported_models is None:
            return True
        chosen = model.strip()
        if chosen.startswith("codex/"):
            chosen = chosen.removeprefix("codex/")
        chosen = chosen.split("@", 1)[0]
        return chosen in self.supported_models

    @property
    def cooling_seconds(self) -> int:
        """距冷却结束还剩多少秒（非冷却中为 0）。供 UI 展示倒计时。"""
        if not self.is_cooling:
            return 0
        return max(0, int(self._cooling_until - time.monotonic()))

    @property
    def status(self) -> str:
        """聚合状态标签，供 UI 直接着色：available / cooling / disabled。"""
        if self.is_disabled:
            return "disabled"
        if self.is_cooling:
            return "cooling"
        return "available"

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "id": self.id,
            "token": self.token[:8] + "…" if self.token else "",  # 脱敏
            "home": self.home,
            "enabled": self.enabled,
            "available": self.is_available,
            "status": self.status,
            "cooling": self.is_cooling,
            "cooling_seconds": self.cooling_seconds,
            "disabled": self.is_disabled,
            "disabled_reason": self.disabled_reason,
            "expiry": self.expiry,
            "priority": self.priority,
            "weight": self.weight,
            "supported_models": sorted(self.supported_models) if self.supported_models is not None else None,
            "error_count": self._error_count,
            "quota": self.quota,
            "quota_error": self.quota_error,
            "quota_updated_at": self.quota_updated_at,
        }

    def persist(self, fields: Optional[Iterable[str]] = None) -> bool:
        """按字段补丁原子写回来源 JSON，避免旧 Account 覆盖新凭据。

        env 兜底账号（无 source_path）静默跳过。供刷新成功后落盘新 token、
        以及 disable()/clear_disabled() 后持久化禁用态使用，重启后状态不丢。
        内部调用应传 ``fields``；无参保留给兼容调用，表示写入所有已知状态。
        """
        if not self.source_path:
            return False
        selected = _PERSIST_FIELDS if fields is None else frozenset(fields)
        unknown = selected - _PERSIST_FIELDS
        if unknown:
            raise ValueError(f"未知持久化字段：{', '.join(sorted(unknown))}")
        path = Path(self.source_path)
        try:
            with _locked_account_file(path):
                if path.exists():
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        logger.error("拒绝覆盖损坏的账号 JSON %s：%s", path, exc)
                        return False
                    if not isinstance(data, dict):
                        logger.error("拒绝覆盖非对象账号 JSON：%s", path)
                        return False
                else:
                    logger.warning("账号文件已不存在，拒绝由旧快照重建：%s", path)
                    return False

                data["type"] = self.backend
                if "token" in selected and self.token:
                    data["token"] = self.token
                    # 同步原文件采用的兼容字段，避免刷新后残留旧 token。
                    if "access_token" in data:
                        data["access_token"] = self.token
                    if "accessToken" in data:
                        data["accessToken"] = self.token
                if "refresh_token" in selected and self.refresh_token:
                    data["refresh_token"] = self.refresh_token
                    if "refreshToken" in data:
                        data["refreshToken"] = self.refresh_token
                if "enabled" in selected:
                    data["enabled"] = self.enabled

                if "quota" in selected:
                    if self.quota is not None:
                        data["quota"] = self.quota
                    if self.quota_error:
                        data["quota_error"] = self.quota_error
                    else:
                        data.pop("quota_error", None)
                    if self.quota_updated_at:
                        data["quota_updated_at"] = self.quota_updated_at
                if "expiry" in selected and self.expiry:
                    data["expiry"] = (
                        datetime.fromtimestamp(self.expiry, tz=timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                if "disabled" in selected:
                    if self.disabled_reason:
                        data["disabled_reason"] = self.disabled_reason
                        data["disabled_at"] = self.disabled_at
                    else:
                        data.pop("disabled_reason", None)
                        data.pop("disabled_at", None)

                _atomic_write_json(path, data)
            return True
        except OSError as exc:
            logger.error("账号状态写回失败 %s：%s", path, exc)
            return False


# ── 认证文件加载 ──────────────────────────────────────────────────────────────

def _parse_expiry_ts(value: object) -> float:
    """把账号文件里的 expiry 解析成 unix 时间戳。

    支持两种写法：ISO8601 字符串（如 "2026-06-27T10:00:00Z"）或纯数字 epoch 秒。
    无法解析时返回 0.0（= 不追踪过期）。
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    # 纯数字 → epoch 秒
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _looks_placeholder(token: str) -> bool:
    """模板里没替换的占位 token（YOUR_XXX_TOKEN…）——入池只会白吃一次失败+冷却。"""
    upper = token.upper()
    return upper.startswith("YOUR_") or upper.startswith("<YOUR") or "XXXXX" in upper


def _codex_models_from_home(home: str) -> Optional[frozenset[str]]:
    """读取一个 Codex profile 的 list-visible 模型集合。

    返回 None 表示缓存缺失/损坏，调度层会保持旧行为；有效缓存即使没有模型也
    返回空集合，从而避免把明确不支持目标模型的账号送进 CLI 白白失败。
    """
    if not home:
        return None
    cache = Path(home).expanduser() / "models_cache.json"
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    raw_models = payload.get("models", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_models, list):
        return None
    models: set[str] = set()
    for item in raw_models:
        if not isinstance(item, dict) or item.get("visibility", "list") != "list":
            continue
        model_id = str(item.get("slug") or item.get("id") or "").strip()
        if model_id:
            models.add(model_id)
    return frozenset(models)


def _account_from_file(path: Path) -> Optional[Account]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.warning("跳过无法解析的账号文件：%s", path)
        return None
    if not isinstance(data, dict):
        logger.warning("跳过非 JSON object 账号文件：%s", path)
        return None
    backend = str(data.get("type", "")).strip().lower()
    if backend not in _KNOWN_BACKENDS:
        logger.warning("跳过未知 backend 账号文件 %s：%r", path, backend)
        return None
    # 兼容多种 token 字段写法：token / access_token / accessToken（Claude Code 凭据格式）/ key
    token = str(
        data.get("token")
        or data.get("access_token")
        or data.get("accessToken")
        or data.get("key")
        or ""
    ).strip()
    if _looks_placeholder(token):
        token = ""  # 占位 token 视同无 token；下方「无任何可注入信息则跳过」兜底
    home = str(data.get("home", "")).strip()
    if home:
        home = str(Path(home).expanduser())
    raw_env = data.get("env")
    extra_env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
    home_key = _HOME_ENV.get(backend)
    if home_key:
        effective_home = str(extra_env.get(home_key, "")).strip() or home
        if effective_home:
            home = str(Path(effective_home).expanduser())
            if home_key in extra_env:
                extra_env[home_key] = home
    token_key = _TOKEN_ENV.get(backend)
    effective_token = (
        str(extra_env.get(token_key, "")).strip() if token_key else ""
    ) or token

    authentication_error = ""
    if backend in {"codex", "antigravity"} and not home:
        authentication_error = f"{backend} 账号必须配置隔离 profile home"
    elif backend == "claude" and not (effective_token or home):
        authentication_error = "claude 账号需要 token 或 CLAUDE_CONFIG_DIR"
    elif backend in {"grok", "copilot"} and not effective_token:
        authentication_error = f"{backend} 账号缺少可注入 token"

    disabled_reason = str(data.get("disabled_reason", "")).strip()
    if authentication_error:
        disabled_reason = f"configuration_error: {authentication_error}"
        logger.warning("账号配置将保留为禁用项 %s：%s", path, authentication_error)
    return Account(
        backend=backend,
        id=str(data.get("email", path.stem)),
        token=token,
        refresh_token=str(data.get("refresh_token") or data.get("refreshToken") or ""),
        home=home,
        extra_env=extra_env,
        supported_models=_codex_models_from_home(home) if backend == "codex" else None,
        # disabled_reason 落盘后，重启仍视为禁用（除非用户手动改 enabled / 删原因）
        enabled=bool(data.get("enabled", True)) and not disabled_reason,
        expiry=_parse_expiry_ts(data.get("expiry", data.get("expires_at"))),
        disabled_reason=disabled_reason,
        disabled_at=_coerce_float(data.get("disabled_at", 0.0)),
        priority=_coerce_int(data.get("priority", 0), 0),
        weight=max(1, _coerce_int(data.get("weight", 1), 1)),
        source_path=str(path),
        quota=data.get("quota") if isinstance(data.get("quota"), dict) else None,
        quota_error=str(data.get("quota_error", "")),
        quota_updated_at=_coerce_float(data.get("quota_updated_at", 0.0)),
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
            # 注意：禁用账号（enabled=False / 带 disabled_reason）也照常入池——
            # pick() 用 is_available 跳过它们，半开探测据此在重启后自动复活（见 pool.py），
            # 状态面板也才能把禁用账号显示出来。过滤交给调度层，而非加载层。
            if acc and (backend is None or acc.backend == backend):
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
    AUTH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    AUTH_DIR.chmod(0o700)
    if name is not None:
        stem = name.strip()
        if (
            not stem
            or stem in {".", ".."}
            or re.fullmatch(r"[A-Za-z0-9_.-]+", stem) is None
        ):
            raise ValueError("account name 只能包含字母、数字、_ . -")
    else:
        raw_stem = f"{account.backend}_{account.id}"
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_stem).strip("._")
        if not stem:
            raise ValueError("无法从 account backend/id 生成安全文件名")

    auth_root = AUTH_DIR.resolve()
    candidate = auth_root / f"{stem}.json"
    if candidate.is_symlink():
        raise ValueError(f"拒绝覆盖符号链接账号文件：{candidate}")
    path = candidate.resolve()
    if path.parent != auth_root:
        raise ValueError(f"账号文件越出 AUTH_DIR：{path}")
    account.source_path = str(path)
    account._capture_credential_generation()
    data = {
        "type": account.backend,
        "email": account.id,
        "token": account.token,
        "refresh_token": account.refresh_token,
        "home": account.home,
        "enabled": account.enabled,
    }
    with _locked_account_file(path):
        _atomic_write_json(path, data)
    return path
