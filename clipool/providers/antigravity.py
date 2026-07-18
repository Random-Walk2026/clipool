"""Antigravity (Google Cloud Code Assist) CLI Provider。

架构说明（对齐 CLIProxyAPI 的分析结果）：
  Antigravity 底层是 Google Cloud Code Assist API：
    - 端点：cloudcode-pa.googleapis.com
    - 认证：Google OAuth2（Bearer access_token）
    - 与普通 Gemini API（generativelanguage.googleapis.com）完全不同

  与其他 CLI 的关键区别：
    - 不支持独立的 --effort 参数
    - 思考强度编码在「模型名变体」里（如 gemini-3.5-flash-high）
    - 账号 token 是 Google OAuth access_token（非 API key）

模型变体映射表（effort → model variant suffix）：
  "gemini-3.5-flash" + "high"  → "gemini-3.5-flash-high"
  "gemini-3.1-pro" + "low"     → "gemini-3.1-pro-low"
  "claude-sonnet-4-6" + "high" → "claude-sonnet-4-6-thinking"
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from ..config import ANTIGRAVITY_BIN
from .base import BaseProvider

# agy 登录态文件（相对 HOME）。没有它就别跑 agy——否则 agy 会弹浏览器要 OAuth 登录。
_AGY_TOKEN_RELATIVE = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"

# profile HOME 下的钥匙串目录。agy 通过 go-keyring shell 出
# /usr/bin/security 存取 token 副本；clipool 为它创建一个随机名的专用
# keychain，并把该虚拟 HOME 的 default/search list 只指向它。这样不会
# 碰真实 login.keychain-db，也不依赖人工输入一个虚拟环境并不知道的密码。
_PROFILE_KEYCHAIN_DIR_RELATIVE = Path("Library") / "Keychains"
_PROFILE_KEYCHAIN_STATE_RELATIVE = Path(".clipool-keychain.json")
_LEGACY_PROFILE_KEYCHAIN_RELATIVE = (
    _PROFILE_KEYCHAIN_DIR_RELATIVE / "login.keychain-db"
)
_PROFILE_MARKER = ".clipool-managed-profile"
_PROFILE_MARKER_CONTENT = "clipool isolated profile v1\n"

_EFFORT_SUFFIXES = ("low", "medium", "high", "thinking")

# 模型名 → {effort → 变体名}
_MODEL_VARIANTS: dict[str, dict[str, str]] = {
    "gemini-3.5-flash": {
        "low": "gemini-3.5-flash-low",
        "medium": "gemini-3.5-flash-medium",
        "high": "gemini-3.5-flash-high",
    },
    "gemini-3.1-pro": {
        "low": "gemini-3.1-pro-low",
        "high": "gemini-3.1-pro-high",
    },
    "claude-sonnet-4-6": {
        "low": "claude-sonnet-4-6-thinking",
        "medium": "claude-sonnet-4-6-thinking",
        "high": "claude-sonnet-4-6-thinking",
        "thinking": "claude-sonnet-4-6-thinking",
    },
    "claude-opus-4-6": {
        "low": "claude-opus-4-6-thinking",
        "medium": "claude-opus-4-6-thinking",
        "high": "claude-opus-4-6-thinking",
        "thinking": "claude-opus-4-6-thinking",
    },
}


def resolve_variant(model: str, effort: str) -> str:
    """把模型名 + effort 解析为 Antigravity 的 --model 参数值。

    已经是变体名（以 -low/-high/-thinking 结尾）时原样返回。
    没有匹配变体时也原样返回（由 CLI 自己处理）。
    """
    chosen = (model or "").strip()
    if not chosen or not effort:
        return chosen
    lower = chosen.lower()
    if any(lower.endswith(f"-{s}") for s in _EFFORT_SUFFIXES):
        return chosen  # 已经是变体名
    variants = _MODEL_VARIANTS.get(lower)
    if not variants:
        return chosen
    return variants.get(effort.strip().lower(), chosen)


# 向后兼容：llm_backends.py 用到这个名字
antigravity_model_variant = resolve_variant


# ── profile 钥匙串静默化（仅 macOS）──────────────────────────────────────────
# 目标：agy 的钥匙串查询在重定向 HOME 下静默成功，而不是弹「输入钥匙串密码」框。
# 做法：给每个 profile 一个随机机器密码、永不自动上锁、已解锁的专用
# keychain，并把该虚拟 HOME 的 default/search list 只指向它。
# 解锁状态由 securityd 按钥匙串文件缓存，重启后失效——所以每个进程对每个 home
# 至少跑一次（_prepared_homes 记忆化，热路径零开销）。

_prepared_homes: set[str] = set()
_prepare_lock = threading.Lock()


def _security(args: list[str], home: str) -> subprocess.CompletedProcess:
    """跑 /usr/bin/security，HOME 指向 profile（default-keychain 解析要用）。"""
    env = os.environ.copy()
    env["HOME"] = home
    return subprocess.run(
        ["/usr/bin/security", *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _managed_profile_root() -> Path:
    """返回 clipool 可以自动维护钥匙串的 profile 根目录。"""
    # 延迟导入避免 provider 注册阶段与 account 模块互相初始化。
    from ..account import AUTH_DIR

    return (AUTH_DIR / "profiles").expanduser().resolve()


def _real_user_home() -> Path:
    return Path.home().expanduser().resolve()


def _validated_profile_home(home: str) -> Path:
    """只允许管理隔离 profile，绝不触碰真实 HOME 的登录钥匙串。

    默认安全范围是 ``CLIPOOL_AUTH_DIR/profiles``。确需把隔离 profile 放在
    其它目录时，用户可在该目录创建内容完全匹配的 ``.clipool-managed-profile``
    标记；真实用户 HOME 即使有标记也始终拒绝。
    """
    resolved = Path(home).expanduser().resolve()
    if resolved == _real_user_home().resolve():
        raise RuntimeError(
            "拒绝把真实用户 HOME 当作 Antigravity 隔离 profile；"
            "clipool 不会触碰真实 login.keychain-db，也不会启动 agy 触发密码弹窗。"
        )

    root = _managed_profile_root().resolve()
    inside_managed_root = resolved == root or root in resolved.parents
    marker = resolved / _PROFILE_MARKER
    marker_owned = False
    try:
        marker_owned = (
            not marker.is_symlink()
            and marker.is_file()
            and marker.read_text(encoding="utf-8") == _PROFILE_MARKER_CONTENT
        )
    except OSError:
        pass
    if not inside_managed_root and not marker_owned:
        raise RuntimeError(
            f"Antigravity profile 不在 clipool 安全目录内：{resolved}。"
            f"请迁移到 {root}，或在确认这是专用虚拟 profile 后创建 "
            f"{marker} 标记；为避免钥匙串弹窗，本次不会启动 agy。"
        )
    return resolved


def validated_profile_token_file(home: str) -> Path:
    """返回受管 profile 内经路径和内容验证的 OAuth token 文件。"""
    profile_home = _validated_profile_home(home)
    profile_home.chmod(0o700)
    directories = [
        profile_home / ".gemini",
        profile_home / ".gemini" / "antigravity-cli",
    ]
    for directory in directories:
        if directory.is_symlink():
            raise RuntimeError(f"拒绝 Antigravity token 目录符号链接：{directory}")
        if not directory.is_dir():
            raise RuntimeError(
                f"Antigravity 账号未登录：{directory} 不存在；"
                "为避免交互式 OAuth 弹窗，本次不会启动 agy。"
            )
        resolved = directory.resolve()
        if profile_home not in resolved.parents:
            raise RuntimeError(f"Antigravity token 目录越出虚拟 profile：{directory}")
        directory.chmod(0o700)

    token_file = profile_home / _AGY_TOKEN_RELATIVE
    if token_file.is_symlink() or not token_file.exists():
        raise RuntimeError(
            f"Antigravity 账号未登录或 token 为符号链接：{token_file}；"
            "为避免交互式 OAuth 弹窗，本次不会启动 agy。"
        )
    token_info = token_file.stat()
    if not stat.S_ISREG(token_info.st_mode) or token_info.st_nlink != 1:
        raise RuntimeError(f"Antigravity token 必须是单链接普通文件：{token_file}")
    resolved_token = token_file.resolve()
    if resolved_token.parent != directories[-1].resolve():
        raise RuntimeError(f"Antigravity token 越出虚拟 profile：{token_file}")
    token_file.chmod(0o600)
    try:
        raw = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Antigravity token JSON 损坏：{token_file}；本次不会启动 agy。"
        ) from exc
    token_data = raw.get("token", raw) if isinstance(raw, dict) else None
    access_token = (
        str(token_data.get("access_token", "")).strip()
        if isinstance(token_data, dict)
        else ""
    )
    if not access_token:
        raise RuntimeError(
            f"Antigravity token 缺少 access_token：{token_file}；"
            "本次不会启动 agy。"
        )
    return resolved_token


def _run_security_checked(args: list[str], home: Path, action: str) -> None:
    result = _security(args, str(home))
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "").strip()[:300]
    raise RuntimeError(f"无法{action} Antigravity 虚拟钥匙串：{detail or 'security 命令失败'}")


def _archive_profile_file(path: Path) -> Path:
    """可恢复地归档虚拟 profile 中损坏/不匹配的状态文件。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.clipool-backup-{stamp}")
    counter = 1
    while backup.exists():
        backup = path.with_name(
            f"{path.name}.clipool-backup-{stamp}-{counter}"
        )
        counter += 1
    path.replace(backup)
    return backup


def _keychain_state_path(profile_home: Path) -> Path:
    return profile_home / _PROFILE_KEYCHAIN_STATE_RELATIVE


def _safe_keychain_dir(profile_home: Path) -> Path:
    """验证 keychain 与 security preferences 目录链，symlink 一律 fail closed。"""
    profile_home = profile_home.resolve()
    library = profile_home / "Library"
    keychain_dir = profile_home / _PROFILE_KEYCHAIN_DIR_RELATIVE
    # default-keychain/list-keychains 会写 ~/Library/Preferences，因此它也必须
    # 与 Keychains 目录一样留在虚拟 HOME 内，不得链到真实 HOME。
    preferences = library / "Preferences"
    directories = [profile_home, library, keychain_dir, preferences]
    for directory in directories:
        if directory.is_symlink():
            raise RuntimeError(f"拒绝钥匙串目录符号链接：{directory}")
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        resolved = directory.resolve()
        if resolved != profile_home and profile_home not in resolved.parents:
            raise RuntimeError(f"钥匙串目录越出虚拟 profile：{directory}")
        directory.chmod(0o700)
    return keychain_dir


def _assert_safe_keychain_file(profile_home: Path, keychain: Path) -> None:
    keychain_dir = _safe_keychain_dir(profile_home)
    if keychain.parent != keychain_dir or keychain.is_symlink():
        raise RuntimeError(f"拒绝不安全的虚拟钥匙串路径：{keychain}")
    if keychain.exists():
        info = keychain.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(f"拒绝非普通或硬链接的虚拟钥匙串：{keychain}")


def _load_keychain_state(profile_home: Path) -> Optional[tuple[Path, str]]:
    """读取专用 keychain 路径和机器生成的密码；严格限制路径在 profile 内。"""
    state_path = _keychain_state_path(profile_home)
    if not state_path.exists():
        return None
    if state_path.is_symlink():
        raise ValueError("虚拟钥匙串状态不能是符号链接")
    state_info = state_path.stat()
    if not stat.S_ISREG(state_info.st_mode) or state_info.st_nlink != 1:
        raise ValueError("虚拟钥匙串状态必须是单链接普通文件")
    state_path.chmod(0o600)
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("无法解析虚拟钥匙串状态") from exc
    if not isinstance(raw, dict):
        raise ValueError("虚拟钥匙串状态不是 JSON object")
    relative = raw.get("keychain")
    password = raw.get("password")
    if not isinstance(relative, str) or not isinstance(password, str) or not password:
        raise ValueError("虚拟钥匙串状态字段不完整")
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.parent != _PROFILE_KEYCHAIN_DIR_RELATIVE
    ):
        raise ValueError("虚拟钥匙串状态包含非法路径")
    candidate = profile_home / relative_path
    if candidate.is_symlink():
        raise ValueError("虚拟钥匙串不能是符号链接")
    keychain = candidate.resolve()
    keychain_dir = _safe_keychain_dir(profile_home)
    if (
        keychain.parent != keychain_dir
        or not keychain.name.startswith("clipool-")
        or not keychain.name.endswith(".keychain-db")
    ):
        raise ValueError("虚拟钥匙串状态越出安全目录")
    _assert_safe_keychain_file(profile_home, keychain)
    return keychain, password


def _write_keychain_state(profile_home: Path, keychain: Path, password: str) -> None:
    """原子写入仅本机使用的 keychain 密码，权限固定为 0600。"""
    state_path = _keychain_state_path(profile_home)
    tmp_path = state_path.with_name(
        f".{state_path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    payload = {
        "version": 1,
        "keychain": str(keychain.relative_to(profile_home)),
        "password": password,
    }
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, state_path)
        state_path.chmod(0o600)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _configure_profile_keychain(profile_home: Path, keychain: Path) -> None:
    """让虚拟 HOME 下的无显式路径 security 调用只命中专用 keychain。"""
    _run_security_checked(
        ["default-keychain", "-d", "user", "-s", str(keychain)],
        profile_home,
        "设置默认",
    )
    _run_security_checked(
        ["list-keychains", "-d", "user", "-s", str(keychain)],
        profile_home,
        "设置搜索列表",
    )


def _create_profile_keychain(profile_home: Path) -> tuple[Path, str]:
    keychain_dir = _safe_keychain_dir(profile_home)
    while True:
        keychain = keychain_dir / f"clipool-{secrets.token_hex(12)}.keychain-db"
        if not keychain.exists():
            break
    password = secrets.token_urlsafe(32)
    _run_security_checked(
        ["create-keychain", "-p", password, str(keychain)], profile_home, "创建"
    )
    _assert_safe_keychain_file(profile_home, keychain)
    _run_security_checked(
        ["unlock-keychain", "-p", password, str(keychain)], profile_home, "解锁"
    )
    # 无 -t/-l 参数 = 永不超时上锁、休眠不上锁。只作用于虚拟 profile。
    _run_security_checked(
        ["set-keychain-settings", str(keychain)], profile_home, "配置"
    )
    keychain.chmod(0o600)
    _configure_profile_keychain(profile_home, keychain)
    _write_keychain_state(profile_home, keychain, password)
    return keychain, password


def ensure_profile_keychain(home: str) -> None:
    """确保隔离 profile 的登录钥匙串可被静默访问（幂等、失败即中止）。

    密码由机器随机生成，只保存在 profile 内的 0600 状态文件；用户无需也
    不应输入它。专用 keychain 无法解锁时会先可恢复地归档，然后用新名字
    重建，避开 securityd 对原路径的缓存。真实用户 HOME 或未明确标记的
    外部目录一律拒绝；旧 login.keychain-db 不会被修改或加入搜索列表。
    """
    if sys.platform != "darwin":
        return
    profile_home = _validated_profile_home(home)
    home_key = str(profile_home)
    if home_key in _prepared_homes:
        return
    with _prepare_lock:
        if home_key in _prepared_homes:
            return
        profile_home.mkdir(parents=True, exist_ok=True)
        marker = profile_home / _PROFILE_MARKER
        if marker.is_symlink():
            raise RuntimeError(f"拒绝虚拟 profile 标记符号链接：{marker}")
        if not marker.exists():
            marker.write_text(_PROFILE_MARKER_CONTENT, encoding="utf-8")
            marker.chmod(0o600)
        state_path = _keychain_state_path(profile_home)
        try:
            state = _load_keychain_state(profile_home)
        except ValueError:
            _archive_profile_file(state_path)
            state = None

        if state is not None:
            keychain, password = state
            unlock = _security(
                ["unlock-keychain", "-p", password, str(keychain)], home_key
            )
            if keychain.exists() and unlock.returncode == 0:
                _run_security_checked(
                    ["set-keychain-settings", str(keychain)], profile_home, "配置"
                )
                keychain.chmod(0o600)
                _configure_profile_keychain(profile_home, keychain)
            else:
                if keychain.exists():
                    _archive_profile_file(keychain)
                if state_path.exists():
                    _archive_profile_file(state_path)
                _create_profile_keychain(profile_home)
        else:
            _create_profile_keychain(profile_home)
        _prepared_homes.add(home_key)


class AntigravityProvider(BaseProvider):
    """Antigravity (Google Cloud Code Assist) CLI（agy --print）。

    认证：Google OAuth token 可通过 env_override["ANTIGRAVITY_TOKEN"] 注入
    （具体 env var 名称取决于 agy CLI 的实现）。
    """

    name = "antigravity"
    label = "Antigravity"

    def run(self, text, model="", effort="", *, env_override=None):
        # 跑 agy 前先确认该账号 home 下有 OAuth 登录态：没有就直接报错，
        # 绝不让 agy 进入交互式登录弹出浏览器（曾因 home 指向空目录误触发）。
        home = (env_override or {}).get("HOME", "").strip()
        if not home:
            raise RuntimeError(
                "Antigravity CLI 必须使用 clipool 隔离 profile HOME；"
                "为避免 OAuth/钥匙串弹窗，本次不会启动 agy。"
            )
        self._ensure_logged_in(env_override)
        ensure_profile_keychain(home)  # 静默准备专用 keychain，避免 security 弹密码框
        return super().run(text, model, effort, env_override=env_override)

    @staticmethod
    def _ensure_logged_in(env_override: Optional[dict]) -> None:
        home = (env_override or {}).get("HOME", "").strip()
        if not home:
            raise RuntimeError("Antigravity 缺少隔离 profile HOME，已拒绝启动 agy")
        validated_profile_token_file(home)

    def _build_cmd(self, text: str, model: str, effort: str) -> list[str]:
        cmd = [ANTIGRAVITY_BIN, "--print", text]
        variant = resolve_variant(model, effort)
        if variant:
            cmd += ["--model", variant]
        return cmd
