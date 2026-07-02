"""同步执行层：账号池轮换 + 失败切换的唯一实现。

server.py 的 async 端点把这里的函数丢进线程池跑；外部项目（如 agent_workflow 的
llm.transport_cli）也可以直接进程内调用，免起 HTTP 服务就拿到同一套
多账号轮换 / 冷却 / 永久禁用语义::

    from clipool.executor import run_with_pool
    reply = run_with_pool("antigravity", "你好", model="gemini-3.5-flash", effort="low")

调度规则（对齐 CLIProxyAPI，并修正旧版的回落漏洞）：
  - 池中有该 backend 的账号：只在池内轮换，每个账号至多试一次；
    全部冷却/禁用时**直接报错**，绝不回落到进程默认登录态——那等于静默
    偷用另一个账号的额度，antigravity 还可能因未登录触发浏览器 OAuth。
  - 池中零账号：用进程默认登录态跑一次（单账号模式，无需任何配置文件）。
"""
from __future__ import annotations

from typing import Callable, Optional

from .account import Account
from .pool import get_pool
from .providers import get_provider


def _all_unavailable_message(backend: str, accounts: list[Account]) -> str:
    cooling = [a for a in accounts if a.is_cooling]
    disabled = [a for a in accounts if a.is_disabled]
    parts = [f"{backend} 账号池全部不可用（共 {len(accounts)} 个）"]
    if cooling:
        wait = min(a.cooling_seconds for a in cooling)
        parts.append(f"{len(cooling)} 个冷却中（最快 {wait}s 后恢复）")
    if disabled:
        reason = disabled[0].disabled_reason or "enabled=false"
        parts.append(f"{len(disabled)} 个已禁用（{reason[:80]}）")
    return "，".join(parts) + "。"


def execute_with_pool(backend: str, fn: Callable[[Optional[Account]], str]) -> str:
    """在 backend 的账号池上执行 fn(account)，处理轮换与成败标记。

    fn 收到选中的账号（池为空时收到 None，表示用默认登录态），返回文本结果；
    失败抛 RuntimeError。每个账号至多尝试一次，失败即冷却/禁用并切下一个。
    """
    pool = get_pool()
    accounts = pool.accounts(backend)
    if not accounts:
        return fn(None)  # 单账号模式：无任何账号文件 / env 兜底

    tried: set[str] = set()
    last_exc: Optional[BaseException] = None

    for _ in range(len(accounts)):
        account = pool.pick(backend)
        if account is None or account.id in tried:
            break
        tried.add(account.id)
        try:
            result = fn(account)
        except RuntimeError as exc:
            last_exc = exc
            pool.mark_failed(account, exc)
            print(f"  [clipool] {backend}/{account.id} 失败：{exc}；切换下一个账号…")
            continue
        pool.mark_success(account)
        return result

    if last_exc is not None:
        raise RuntimeError(
            f"{backend} 账号池 {len(tried)} 个账号全部失败，最后错误：{last_exc}"
        ) from last_exc
    raise RuntimeError(_all_unavailable_message(backend, accounts))


def run_with_pool(
    backend: str,
    text: str,
    model: str = "",
    effort: str = "",
    *,
    extra_env: Optional[dict[str, str]] = None,
) -> str:
    """跑一次 CLI 调用，在该 backend 的账号池上轮换。

    extra_env 叠加在账号自身注入的环境变量之上（调用方显式指定的优先），
    经 subprocess env 传入子进程，不污染本进程 os.environ。
    """
    provider = get_provider(backend)  # 未知 backend 立即抛 RuntimeError

    def _call(account: Optional[Account]) -> str:
        env = account.env_override() if account else {}
        if extra_env:
            env.update(extra_env)
        return provider.run(text, model, effort, env_override=env or None)

    return execute_with_pool(backend, _call)
