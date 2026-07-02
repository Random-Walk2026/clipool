"""executor：账号池同步执行层的调度语义测试。

重点覆盖三条规则（也是对旧版 server 轮换循环的回归修复）：
  ① 多账号轮换：失败冷却后自动切下一个账号，成功即标记恢复
  ② 池中有账号但全部不可用 → 直接报错，绝不回落到默认登录态
     （旧版会静默用进程默认 HOME/token 跑，等于偷用另一个账号，
      antigravity 还可能因未登录触发浏览器 OAuth）
  ③ 池中零账号 → 默认登录态跑一次（单账号模式不变）
外加 pool 冷却的指数退避、server 层的 OpenAI 兼容增强。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proxy import executor as executor_mod
from proxy import pool as poolmod
from proxy.account import Account


class _FakeProvider:
    """记录每次调用的 env_override，按脚本决定成败。"""

    def __init__(self, fail_ids: set[str] | None = None):
        self.fail_ids = fail_ids or set()
        self.calls: list[Optional[dict]] = []

    def run(self, text, model="", effort="", *, env_override=None):
        self.calls.append(env_override)
        token = (env_override or {}).get("CLAUDE_CODE_OAUTH_TOKEN", "")
        if token in self.fail_ids:
            raise RuntimeError("quota exceeded 429")
        return f"ok:{token or 'default'}"


def _make_pool(accounts: list[Account]) -> poolmod.AccountPool:
    pool = poolmod.AccountPool()
    pool._accounts = {"claude": list(accounts)} if accounts else {}
    pool._index = {"claude": 0}
    pool._loaded = True
    return pool


class _PatchedExecutorTest(unittest.TestCase):
    """公共脚手架：把 executor 的 get_pool / get_provider 换成受控对象。"""

    def _patch(self, pool: poolmod.AccountPool, provider: _FakeProvider) -> None:
        self._orig_pool = executor_mod.get_pool
        self._orig_provider = executor_mod.get_provider
        executor_mod.get_pool = lambda: pool
        executor_mod.get_provider = lambda backend: provider
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        executor_mod.get_pool = self._orig_pool
        executor_mod.get_provider = self._orig_provider


class RotationAcrossAccounts(_PatchedExecutorTest):
    def test_failed_account_cools_and_next_succeeds(self) -> None:
        a = Account("claude", "a", token="ta")
        b = Account("claude", "b", token="tb")
        provider = _FakeProvider(fail_ids={"ta"})
        self._patch(_make_pool([a, b]), provider)

        result = executor_mod.run_with_pool("claude", "hi")

        self.assertEqual(result, "ok:tb")
        self.assertTrue(a.is_cooling)          # 失败账号已冷却
        self.assertEqual(b.error_count, 0)     # 成功账号被 mark_success
        self.assertEqual(len(provider.calls), 2)

    def test_extra_env_overrides_account_env(self) -> None:
        a = Account("claude", "a", token="ta")
        provider = _FakeProvider()
        self._patch(_make_pool([a]), provider)

        executor_mod.run_with_pool(
            "claude", "hi", extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "caller-wins"}
        )

        self.assertEqual(
            provider.calls[0]["CLAUDE_CODE_OAUTH_TOKEN"], "caller-wins"
        )


class NoFallthroughToDefaultLogin(_PatchedExecutorTest):
    def test_all_cooling_raises_instead_of_default_env(self) -> None:
        a = Account("claude", "a", token="ta")
        b = Account("claude", "b", token="tb")
        a.cool_down(60)
        b.cool_down(60)
        provider = _FakeProvider()
        self._patch(_make_pool([a, b]), provider)

        with self.assertRaises(RuntimeError) as ctx:
            executor_mod.run_with_pool("claude", "hi")

        self.assertIn("全部不可用", str(ctx.exception))
        self.assertIn("冷却", str(ctx.exception))
        self.assertEqual(provider.calls, [])   # 绝不能用默认登录态偷跑

    def test_all_failed_reports_last_error(self) -> None:
        a = Account("claude", "a", token="ta")
        b = Account("claude", "b", token="tb")
        provider = _FakeProvider(fail_ids={"ta", "tb"})
        self._patch(_make_pool([a, b]), provider)

        with self.assertRaises(RuntimeError) as ctx:
            executor_mod.run_with_pool("claude", "hi")

        self.assertIn("2 个账号全部失败", str(ctx.exception))
        self.assertIn("quota exceeded", str(ctx.exception))


class EmptyPoolSingleAccountMode(_PatchedExecutorTest):
    def test_zero_accounts_runs_with_default_login(self) -> None:
        provider = _FakeProvider()
        self._patch(_make_pool([]), provider)

        result = executor_mod.run_with_pool("claude", "hi")

        self.assertEqual(result, "ok:default")
        self.assertEqual(provider.calls, [None])  # 无账号 env 注入


class ExponentialBackoff(unittest.TestCase):
    def test_quota_cooldown_doubles_and_caps(self) -> None:
        acc = Account("claude", "q", token="t")
        pool = poolmod.AccountPool()
        exc = RuntimeError("quota exceeded 429")

        waits = []
        for _ in range(8):
            acc._cooling_until = 0.0  # 只看每次新设的冷却时长
            pool.mark_failed(acc, exc)
            waits.append(acc.cooling_seconds)

        self.assertLessEqual(abs(waits[0] - poolmod.COOLDOWN_QUOTA), 1)
        self.assertLessEqual(abs(waits[1] - poolmod.COOLDOWN_QUOTA * 2), 1)
        self.assertLessEqual(abs(waits[2] - poolmod.COOLDOWN_QUOTA * 4), 1)
        self.assertLessEqual(waits[-1], poolmod.COOLDOWN_QUOTA_MAX)

    def test_success_resets_backoff(self) -> None:
        acc = Account("claude", "q", token="t")
        pool = poolmod.AccountPool()
        exc = RuntimeError("timeout 503")
        pool.mark_failed(acc, exc)
        pool.mark_failed(acc, exc)
        pool.mark_success(acc)
        pool.mark_failed(acc, exc)
        self.assertLessEqual(acc.cooling_seconds, poolmod.COOLDOWN_TRANSIENT)


class ServerOpenAICompat(unittest.TestCase):
    """server 层：list content / reasoning_effort / 鉴权。"""

    def _client(self):
        from fastapi.testclient import TestClient
        from proxy import server as servermod

        return TestClient(servermod.app), servermod

    def test_list_content_and_reasoning_effort(self) -> None:
        client, servermod = self._client()
        captured = {}

        def fake_run(backend, text, model, effort):
            captured.update(backend=backend, text=text, model=model, effort=effort)
            return "answer"

        orig = servermod.run_with_pool
        servermod.run_with_pool = fake_run
        try:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude/sonnet",
                    "reasoning_effort": "high",
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hello parts"}],
                        }
                    ],
                },
            )
        finally:
            servermod.run_with_pool = orig

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["effort"], "high")     # reasoning_effort 兜底生效
        self.assertIn("hello parts", captured["text"])   # content parts 被摊平
        body = resp.json()
        self.assertEqual(body["choices"][0]["message"]["content"], "answer")

    def test_model_effort_beats_reasoning_effort(self) -> None:
        client, servermod = self._client()
        captured = {}

        def fake_run(backend, text, model, effort):
            captured["effort"] = effort
            return "x"

        orig = servermod.run_with_pool
        servermod.run_with_pool = fake_run
        try:
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude/sonnet@low",
                    "reasoning_effort": "high",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        finally:
            servermod.run_with_pool = orig
        self.assertEqual(captured["effort"], "low")

    def test_api_key_enforced_when_configured(self) -> None:
        client, _ = self._client()
        os.environ["CLI_PROXY_API_KEY"] = "secret-key"
        try:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            self.assertEqual(resp.status_code, 401)
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer secret-key"},
                json={
                    "model": "unknown-model-xyz",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            self.assertEqual(resp.status_code, 400)  # 过了鉴权，栽在未知 provider
        finally:
            os.environ.pop("CLI_PROXY_API_KEY", None)


class AntigravityThinkingEffort(unittest.TestCase):
    def test_model_passes_through_unless_thinking_enabled(self) -> None:
        from proxy.anthropic import AnthropicMessagesRequest
        from proxy.providers.antigravity_http import _thinking_effort

        plain = AnthropicMessagesRequest(
            model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}]
        )
        self.assertEqual(_thinking_effort(plain), "")

        thinking = AnthropicMessagesRequest(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hi"}],
            thinking={"type": "enabled", "budget_tokens": 1024},
        )
        self.assertEqual(_thinking_effort(thinking), "thinking")


if __name__ == "__main__":
    unittest.main()
