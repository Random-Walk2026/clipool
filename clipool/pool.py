"""账号池：多账号轮换 + 冷却管理 + 永久禁用。

CLIProxyAPI 的核心调度思路在这里，并吸收了 cockpit-tools 的「永久禁用」机制：
  - 每个 backend 维护一个账号列表
  - Round-robin 选号，跳过冷却中 / 永久禁用的账号
  - 请求失败时按错误类型分三档处理：
    * 认证失效（invalid_grant / HTTP 401 / token 撤销）→ 永久禁用并落盘（重启不再死磕）
    * 配额/频率限制 → 长冷却（默认 60s，配额会自己恢复）
    * 瞬时错误（超时/5xx）→ 短冷却（默认 15s）
  - 半开探测：仅 invalid_grant 自动禁用账号在 RECOVERY_PROBE_AFTER 秒后放行一次，
    成功（含刷新成功）则自动解禁；人工禁用和其它认证错误不自动复活。
  - 全部账号不可用时抛 RuntimeError（调用方可重试或报错）
"""
from __future__ import annotations

import re
import threading
import time
from enum import Enum
from typing import Callable, Optional

from .account import Account, load_accounts, persist_revision

# 冷却时长（秒）：基础值按连续失败次数指数放大（60→120→240…），封顶后不再翻倍。
# 配额窗口动辄 5 小时，固定 60s 冷却会让耗尽的账号每分钟白烧一次子进程冷启动。
COOLDOWN_QUOTA = int(60)          # 配额耗尽 / 限速（基础值）
COOLDOWN_QUOTA_MAX = int(3600)    # 配额冷却上限
COOLDOWN_TRANSIENT = int(15)      # 瞬时错误（超时、5xx，基础值）
COOLDOWN_TRANSIENT_MAX = int(300) # 瞬时冷却上限

# invalid_grant 自动禁用账号多久后允许半开探测一次（秒）。
# token 可能被外部重新登录修复，给一次受租约保护的复活机会。
RECOVERY_PROBE_AFTER = int(600)

# 判断是否配额/限速错误的关键词
_QUOTA_KEYWORDS = frozenset(
    ["quota", "rate limit", "429", "resource_exhausted", "too many requests", "exceeded"]
)
_TRANSIENT_KEYWORDS = frozenset(
    ["timeout", "500", "502", "503", "504", "connection", "unavailable", "deadline"]
)
# 认证彻底失效 → 永久禁用（光重试别的时间没用，得换 token / 重新登录）
_AUTH_KEYWORDS = frozenset(
    [
        "invalid_token",
        "unauthorized",
        "token expired",
        "token has expired",
        "revoked",
        "no refresh_token",
    ]
)

# 只把带 HTTP / status 语义的 401 当成认证失败。裸数字可能只是模型名或错误编号，
# 例如 ``model gpt-401 not found``，不能因此永久禁用账号。
_HTTP_401_RE = re.compile(
    r"(?:\bhttp(?:\s+(?:error|status))?|\bstatus(?:_code|\s+code)?)"
    r"\s*[:=]?\s*401\b|\b401\s+(?:client\s+error[^:]*:\s*)?unauthorized\b",
    re.IGNORECASE,
)


class FailureKind(str, Enum):
    """账号执行失败的最小结构化分类，provider 可逐步改为显式传入。"""

    INVALID_GRANT = "invalid_grant"
    AUTH = "auth"
    QUOTA = "quota"
    TRANSIENT = "transient"


class AccountExecutionError(RuntimeError):
    """带结构化类别的 provider 错误；字符串分类仅作为兼容兜底。"""

    def __init__(self, message: str, *, failure_kind: FailureKind) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


def _structured_failure_kind(exc: BaseException) -> Optional[FailureKind]:
    raw_kind = getattr(exc, "failure_kind", None)
    if raw_kind is not None:
        try:
            return FailureKind(raw_kind)
        except (TypeError, ValueError):
            pass

    status_code = getattr(exc, "status_code", None)
    if status_code == 401:
        return FailureKind.AUTH
    if status_code == 429:
        return FailureKind.QUOTA
    if isinstance(status_code, int) and 500 <= status_code < 600:
        return FailureKind.TRANSIENT
    return None


def _failure_kind(exc: Optional[BaseException]) -> FailureKind:
    """优先读结构化类别，再兼容旧 provider 的错误字符串。"""
    if exc is None:
        return FailureKind.TRANSIENT

    structured = _structured_failure_kind(exc)
    if structured is not None:
        return structured

    msg = str(exc).lower()
    if "invalid_grant" in msg:
        return FailureKind.INVALID_GRANT
    if any(k in msg for k in _AUTH_KEYWORDS) or _HTTP_401_RE.search(msg):
        return FailureKind.AUTH
    if any(k in msg for k in _QUOTA_KEYWORDS):
        return FailureKind.QUOTA
    return FailureKind.TRANSIENT


def _is_auth_error(exc: BaseException) -> bool:
    return _failure_kind(exc) in {FailureKind.INVALID_GRANT, FailureKind.AUTH}


def _is_quota_error(exc: BaseException) -> bool:
    return _failure_kind(exc) == FailureKind.QUOTA


def _is_transient_error(exc: BaseException) -> bool:
    structured = _structured_failure_kind(exc)
    if structured is not None:
        return structured == FailureKind.TRANSIENT
    msg = str(exc).lower()
    return any(k in msg for k in _TRANSIENT_KEYWORDS)


def _backoff(base: int, consecutive_failures: int, cap: int) -> float:
    """指数退避：第 n 次连续失败冷却 base * 2^n 秒，封顶 cap。"""
    return float(min(base * (2 ** min(consecutive_failures, 6)), cap))


class AccountPool:
    """线程安全的账号池，支持多账号轮换与冷却。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # backend → list[Account]（按加载顺序，round-robin 用 index）
        self._accounts: dict[str, list[Account]] = {}
        # 无 predicate 时 key 是 backend；过滤时 key 是 (backend, eligible ids)，
        # 让不同模型/能力子集各自公平轮换，互不消耗游标。
        self._index: dict[object, int] = {}
        # (backend, id) → 正在做半开探测的 Account 对象。对象身份用于避免 reload
        # 后旧请求完成时误释放新请求的租约。
        self._probe_leases: dict[tuple[str, str], Account] = {}
        # reload 可在锁外读文件；修改世代用来检测读期间的并发状态变更。
        self._revision = 0
        self._disk_revision = persist_revision()
        self._loaded = False

    @staticmethod
    def _build_snapshot(
        loaded_accounts: list[Account],
    ) -> tuple[dict[str, list[Account]], dict[object, int]]:
        accounts: dict[str, list[Account]] = {}
        index: dict[object, int] = {}
        for acc in loaded_accounts:
            accounts.setdefault(acc.backend, []).append(acc)
            index.setdefault(acc.backend, 0)
        return accounts, index

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self.reload()

    def _ensure_synced(self) -> None:
        """Account.persist 直接写回后，下一次读/选号前自动换入新快照。"""
        self._ensure_loaded()
        with self._lock:
            disk_revision = self._disk_revision
        if disk_revision != persist_revision():
            self.reload()

    def reload(self) -> None:
        """离线构建新快照后原子替换；加载期间继续服务旧快照。"""
        while True:
            with self._lock:
                revision = self._revision
            disk_revision = persist_revision()
            accounts, index = self._build_snapshot(load_accounts())
            if disk_revision != persist_revision():
                continue
            with self._lock:
                if revision != self._revision or disk_revision != persist_revision():
                    continue  # 读文件期间有变更，丢弃旧读取并重试。
                self._merge_runtime_state_locked(accounts)
                self._accounts = accounts
                self._index = index
                # reload 是明确的请求世代边界：旧对象的完成结果一律忽略，
                # 避免旧 token 的 401 禁用刚重新登录但 home/id 相同的新凭据。
                self._drop_stale_probe_leases_locked(accounts)
                self._revision += 1
                self._disk_revision = disk_revision
                self._loaded = True
                return

    def _merge_runtime_state_locked(self, accounts: dict[str, list[Account]]) -> None:
        """凭据未变时保留不落盘的冷却和连续失败计数。"""
        previous = {
            (account.backend, account.id): account
            for group in self._accounts.values()
            for account in group
        }
        for group in accounts.values():
            for account in group:
                old = previous.get((account.backend, account.id))
                if (
                    old is None
                    or old._credential_generation != account._credential_generation
                ):
                    continue
                with old._lock:
                    cooling_until = old._cooling_until
                    error_count = old._error_count
                with account._lock:
                    account._cooling_until = cooling_until
                    account._error_count = error_count

    def _drop_stale_probe_leases_locked(
        self, accounts: dict[str, list[Account]]
    ) -> None:
        current = {
            (account.backend, account.id): account
            for group in accounts.values()
            for account in group
        }
        for key, lease in list(self._probe_leases.items()):
            replacement = current.get(key)
            if (
                replacement is None
                or replacement._credential_generation != lease._credential_generation
            ):
                self._probe_leases.pop(key, None)

    def accounts(self, backend: Optional[str] = None) -> list[Account]:
        """返回指定 backend（或全部）的账号列表。"""
        self._ensure_synced()
        with self._lock:
            if backend:
                return list(self._accounts.get(backend, ()))
            return [acc for accs in self._accounts.values() for acc in accs]

    def pick(
        self,
        backend: str,
        *,
        predicate: Optional[Callable[[Account], bool]] = None,
    ) -> Optional[Account]:
        """选出一个可用账号：主备号（priority）+ 组内加权轮换（weight）。

        - 先在可用账号里取 priority 最小的「主号组」，组内按 weight 加权轮换；
          主号组全部不可用（冷却/禁用）时自然溢出到下一 priority 组（备号）。
        - 都不可用时，对「禁用够久」的账号做一次半开探测放行，让 token 被外部
          修复 / 刷新成功的账号自动复活。全无可放行账号返回 None。
        """
        self._ensure_synced()
        with self._lock:
            accs = self._accounts.get(backend, [])
            if predicate is not None:
                accs = [a for a in accs if predicate(a)]
            if not accs:
                return None
            cursor_key: object = (
                backend if predicate is None else (backend, tuple(a.id for a in accs))
            )
            available = [a for a in accs if a.is_available]
            if available:
                return self._weighted_pick(cursor_key, available)
            # 半开探测——只有 invalid_grant 自动禁用账号可试探；manual/auth_error
            # 永不自动复活。同一 backend/id 同时只发一个 probe。
            now = time.time()
            n = len(accs)
            start = self._index.get(cursor_key, 0)
            for offset in range(n):
                idx = (start + offset) % n
                acc = accs[idx]
                lease_key = (acc.backend, acc.id)
                if (
                    acc.is_invalid_grant_disabled
                    and not acc.is_cooling
                    and acc.disabled_at
                    and now - acc.disabled_at >= RECOVERY_PROBE_AFTER
                    and lease_key not in self._probe_leases
                ):
                    self._index[cursor_key] = (idx + 1) % n
                    self._probe_leases[lease_key] = acc
                    return acc
        return None  # 全部不可用

    def _weighted_pick(self, cursor_key: object, available: list[Account]) -> Account:
        """在可用账号中按 priority 分组 + 组内 weight 加权轮换选一个。须在持锁下调用。"""
        min_priority = min(a.priority for a in available)
        group = [a for a in available if a.priority == min_priority]  # 保持加载顺序
        total_weight = sum(max(1, a.weight) for a in group)
        cursor = self._index.get(cursor_key, 0)
        self._index[cursor_key] = cursor + 1
        slot = cursor % total_weight
        for acc in group:
            w = max(1, acc.weight)
            if slot < w:
                return acc
            slot -= w
        return group[-1]  # 兜底（理论不可达）

    def mark_success(self, account: Account) -> None:
        with self._lock:
            target = self._current_account_locked(account)
            self._release_probe_locked(account)
            if target is None:
                return
            target.reset()
            # 半开探测成功 / 刷新后恢复：自动解除 invalid_grant 类禁用并落盘
            if target.is_invalid_grant_disabled:
                target.clear_disabled()
                target.persist(fields={"enabled", "disabled"})
                self._revision += 1

    def mark_failed(self, account: Account, exc: Optional[BaseException] = None) -> None:
        """按错误类型决定：永久禁用（落盘）还是临时冷却。"""
        kind = _failure_kind(exc)
        with self._lock:
            target = self._current_account_locked(account)
            self._release_probe_locked(account)
            if target is None:
                return
            if kind in {FailureKind.INVALID_GRANT, FailureKind.AUTH}:
                # token 彻底失效：永久禁用并落盘，重启不再死磕
                reason = (
                    "invalid_grant" if kind == FailureKind.INVALID_GRANT else "auth_error"
                )
                target.disable(f"{reason}: {str(exc)[:200]}")
                target.persist(fields={"enabled", "disabled"})
                self._revision += 1
            elif kind == FailureKind.QUOTA:
                target.cool_down(
                    _backoff(COOLDOWN_QUOTA, target.error_count, COOLDOWN_QUOTA_MAX)
                )
            else:
                target.cool_down(
                    _backoff(
                        COOLDOWN_TRANSIENT,
                        target.error_count,
                        COOLDOWN_TRANSIENT_MAX,
                    )
                )

    def _release_probe_locked(self, account: Account) -> None:
        lease_key = (account.backend, account.id)
        if self._probe_leases.get(lease_key) is account:
            self._probe_leases.pop(lease_key, None)

    def status(self) -> dict:
        """返回所有 backend 的账号状态，供管理 API 展示。"""
        self._ensure_synced()
        with self._lock:
            return {
                backend: [a.to_dict() for a in accs]
                for backend, accs in self._accounts.items()
            }

    def find(self, backend: str, account_id: str) -> Optional[Account]:
        """按 backend + id 定位账号（供管理操作）。"""
        self._ensure_synced()
        with self._lock:
            return self._find_locked(backend, account_id)

    def _find_locked(self, backend: str, account_id: str) -> Optional[Account]:
        for acc in self._accounts.get(backend, ()):
            if acc.id == account_id:
                return acc
        return None

    def _current_account_locked(self, account: Account) -> Optional[Account]:
        """reload 后旧 Account 的完成结果一律忽略。"""
        if not self._loaded:
            return account  # 允许独立单元测试/库调用直接标记 Account。
        current = self._find_locked(account.backend, account.id)
        return current if current is account else None

    def set_enabled(self, backend: str, account_id: str, enabled: bool) -> Optional[Account]:
        """手动启用 / 禁用某账号并落盘。返回被操作的账号，找不到返回 None。

        - 启用：清除禁用态 + 重置冷却（等价于人工确认该号已修复）。
        - 禁用：标记人工禁用原因（与 invalid_grant 自动禁用区分）。
        """
        self._ensure_synced()
        with self._lock:
            acc = self._find_locked(backend, account_id)
            if acc is None:
                return None
            if enabled:
                if acc.disabled_reason.startswith("configuration_error:"):
                    raise ValueError(
                        "账号缺少该 backend 真正会使用的认证信息；"
                        "请先修正账号 JSON 并 reload"
                    )
                acc.clear_disabled()
                acc.reset()
            else:
                acc.disable("manual: 由管理面板禁用")
            self._probe_leases.pop((backend, account_id), None)
            acc.persist(fields={"enabled", "disabled"})
            self._revision += 1
        return acc

    def reset_account(self, backend: str, account_id: str) -> Optional[Account]:
        """清除某账号的冷却 / 错误计数（不动禁用态）。找不到返回 None。"""
        self._ensure_synced()
        with self._lock:
            acc = self._find_locked(backend, account_id)
            if acc is None:
                return None
            acc.reset()
            return acc


# 全局单例（server.py 和 runner.py 共用）
_pool: Optional[AccountPool] = None
_pool_lock = threading.Lock()


def get_pool() -> AccountPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = AccountPool()
    return _pool
