"""账号池：永久禁用 / 主动刷新 / 落盘 行为测试。

覆盖移植自 cockpit-tools 的三项能力：
  ① 认证失效 → 永久禁用 + 落盘；配额/瞬时 → 仅冷却；半开探测自动恢复
  ② expiry 入模型 + needs_refresh 提前刷新
  ③ 刷新/禁用状态写回 registry JSON
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipool import pool as poolmod
from clipool.account import (
    Account,
    _account_from_file,
    _codex_models_from_home,
    _parse_expiry_ts,
)


def _write_account(dir_path: Path, **overrides) -> Path:
    data = {
        "type": "claude",
        "email": "a@example.com",
        "token": "sk-abc12345",
        "refresh_token": "r1",
        "enabled": True,
    }
    data.update(overrides)
    path = dir_path / "claude_work.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class AuthFailureDisablesPermanently(unittest.TestCase):
    def test_invalid_grant_disables_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_account(Path(tmp))
            acc = _account_from_file(path)
            pool = poolmod.AccountPool()

            pool.mark_failed(acc, RuntimeError("HTTP 400 invalid_grant: token revoked"))

            self.assertTrue(acc.is_disabled)
            self.assertTrue(acc.is_invalid_grant_disabled)
            saved = json.loads(path.read_text())
            self.assertIn("disabled_reason", saved)
            self.assertFalse(saved["enabled"])

            # 重新从磁盘加载仍是禁用态
            reloaded = _account_from_file(path)
            self.assertTrue(reloaded.is_disabled)
            self.assertFalse(reloaded.enabled)

    def test_quota_error_only_cools_down(self) -> None:
        acc = Account("claude", "q", token="t")
        poolmod.AccountPool().mark_failed(
            acc, RuntimeError("429 too many requests quota exceeded")
        )
        self.assertFalse(acc.is_disabled)
        self.assertTrue(acc.is_cooling)

    def test_transient_error_only_cools_down(self) -> None:
        acc = Account("claude", "t", token="t")
        poolmod.AccountPool().mark_failed(acc, RuntimeError("connection timeout 503"))
        self.assertFalse(acc.is_disabled)
        self.assertTrue(acc.is_cooling)

    def test_bare_401_in_model_error_does_not_disable(self) -> None:
        acc = Account("codex", "model", home="/tmp/model")

        poolmod.AccountPool().mark_failed(
            acc, RuntimeError("model gpt-401-preview was not found")
        )

        self.assertFalse(acc.is_disabled)
        self.assertTrue(acc.is_cooling)

    def test_explicit_http_401_still_disables(self) -> None:
        acc = Account("claude", "auth", token="t")

        poolmod.AccountPool().mark_failed(
            acc, RuntimeError("HTTP status 401: credentials rejected")
        )

        self.assertTrue(acc.is_disabled)
        self.assertFalse(acc.is_invalid_grant_disabled)

    def test_structured_failure_kind_takes_precedence(self) -> None:
        acc = Account("claude", "structured", token="t")
        exc = poolmod.AccountExecutionError(
            "provider returned an opaque error",
            failure_kind=poolmod.FailureKind.INVALID_GRANT,
        )

        poolmod.AccountPool().mark_failed(acc, exc)

        self.assertTrue(acc.is_invalid_grant_disabled)


class LoadIncludesDisabled(unittest.TestCase):
    def test_disabled_account_still_loaded(self) -> None:
        """禁用账号也要入池：否则半开探测无法在重启后复活，状态面板也看不到它。"""
        from clipool import account as accountmod

        with tempfile.TemporaryDirectory() as tmp:
            _write_account(
                Path(tmp),
                enabled=False,
                disabled_reason="invalid_grant: old",
                disabled_at=time.time(),
            )
            orig = accountmod.AUTH_DIR
            accountmod.AUTH_DIR = Path(tmp)
            try:
                loaded = accountmod.load_accounts("claude")
            finally:
                accountmod.AUTH_DIR = orig
            self.assertEqual(len(loaded), 1)
            self.assertTrue(loaded[0].is_disabled)
            self.assertEqual(loaded[0].status, "disabled")

    def test_malformed_files_are_skipped_without_aborting_reload(self) -> None:
        from clipool import account as accountmod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "list.json").write_text("[]", encoding="utf-8")
            (root / "binary.json").write_bytes(b"\xff\xfe")
            _write_account(
                root,
                email="valid@example.com",
                priority="bad",
                weight="bad",
                disabled_at="bad",
                quota_updated_at="bad",
            )
            with patch.object(accountmod, "AUTH_DIR", root):
                loaded = accountmod.load_accounts("claude")

            self.assertEqual([account.id for account in loaded], ["valid@example.com"])
            self.assertEqual(loaded[0].priority, 0)
            self.assertEqual(loaded[0].weight, 1)

    def test_backend_specific_auth_shapes_block_default_login_fallback(self) -> None:
        invalid_cases = [
            {"type": "codex", "email": "c", "token": "ignored-token"},
            {"type": "antigravity", "email": "a", "token": "ignored-token"},
            {"type": "grok", "email": "g", "home": "/tmp/ignored-home"},
            {"type": "copilot", "email": "p", "env": {"UNRELATED": "x"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for index, data in enumerate(invalid_cases):
                path = Path(tmp) / f"invalid-{index}.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                account = _account_from_file(path)
                self.assertIsNotNone(account)
                self.assertTrue(account.is_disabled)
                self.assertTrue(
                    account.disabled_reason.startswith("configuration_error:")
                )

    def test_home_env_is_normalized_for_directory_backends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex.json"
            path.write_text(
                json.dumps(
                    {
                        "type": "codex",
                        "email": "codex-env",
                        "env": {"CODEX_HOME": "~/isolated-codex"},
                    }
                ),
                encoding="utf-8",
            )
            account = _account_from_file(path)

        self.assertIsNotNone(account)
        self.assertFalse(account.is_disabled)
        self.assertEqual(account.home, str(Path("~/isolated-codex").expanduser()))

    def test_configuration_error_cannot_be_enabled_from_management(self) -> None:
        account = Account(
            "codex",
            "broken",
            enabled=False,
            disabled_reason="configuration_error: missing profile home",
        )
        pool = poolmod.AccountPool()
        pool._accounts = {"codex": [account]}
        pool._index = {"codex": 0}
        pool._loaded = True

        with self.assertRaisesRegex(ValueError, "修正账号 JSON"):
            pool.set_enabled("codex", "broken", True)
        self.assertTrue(account.is_disabled)


class HalfOpenRecovery(unittest.TestCase):
    @staticmethod
    def _pool(acc: Account) -> poolmod.AccountPool:
        pool = poolmod.AccountPool()
        pool._accounts = {acc.backend: [acc]}
        pool._index = {acc.backend: 0}
        pool._loaded = True
        return pool

    def test_disabled_account_probed_then_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_account(
                Path(tmp),
                enabled=False,
                disabled_reason="invalid_grant: old",
                disabled_at=time.time() - poolmod.RECOVERY_PROBE_AFTER - 1,
            )
            acc = _account_from_file(path)
            pool = poolmod.AccountPool()
            pool._accounts = {"claude": [acc]}
            pool._index = {"claude": 0}
            pool._loaded = True

            # 正常账号都不可用 → 半开探测放行这个禁用账号
            probed = pool.pick("claude")
            self.assertIs(probed, acc)

            # 探测成功 → 自动解禁并落盘
            pool.mark_success(probed)
            self.assertFalse(acc.is_disabled)
            self.assertNotIn("disabled_reason", json.loads(path.read_text()))

    def test_recently_disabled_not_probed(self) -> None:
        acc = Account("claude", "r", token="t")
        acc.disable("invalid_grant: fresh")  # disabled_at = now
        pool = poolmod.AccountPool()
        pool._accounts = {"claude": [acc]}
        pool._index = {"claude": 0}
        pool._loaded = True
        self.assertIsNone(pool.pick("claude"))

    def test_manual_disable_is_never_probed(self) -> None:
        acc = Account("claude", "manual", token="t")
        acc.disable("manual: operator disabled")
        acc.disabled_at = time.time() - poolmod.RECOVERY_PROBE_AFTER - 1

        self.assertIsNone(self._pool(acc).pick("claude"))

    def test_non_invalid_grant_auth_disable_is_never_probed(self) -> None:
        acc = Account("claude", "auth", token="t")
        acc.disable("auth_error: HTTP 401")
        acc.disabled_at = time.time() - poolmod.RECOVERY_PROBE_AFTER - 1

        self.assertIsNone(self._pool(acc).pick("claude"))


class InFlightReloadCompletion(unittest.TestCase):
    @staticmethod
    def _loaded_pool(account: Account) -> poolmod.AccountPool:
        pool = poolmod.AccountPool()
        pool._accounts = {account.backend: [account]}
        pool._index = {account.backend: 0}
        pool._loaded = True
        return pool

    _pool = _loaded_pool

    def test_old_request_failure_is_ignored_after_reload(self) -> None:
        old = Account("claude", "same", token="same-token")
        current = Account("claude", "same", token="same-token")
        pool = self._loaded_pool(old)
        selected = pool.pick("claude")
        self.assertIs(selected, old)

        with patch.object(poolmod, "load_accounts", return_value=[current]):
            pool.reload()
        pool.mark_failed(selected, RuntimeError("connection timeout 503"))

        self.assertIs(pool.find("claude", "same"), current)
        self.assertFalse(current.is_cooling)

    def test_old_auth_failure_cannot_disable_rotated_credentials(self) -> None:
        old = Account("claude", "same", token="revoked-token")
        current = Account("claude", "same", token="fresh-token")
        pool = self._loaded_pool(old)
        selected = pool.pick("claude")
        self.assertIs(selected, old)

        with patch.object(poolmod, "load_accounts", return_value=[current]):
            pool.reload()
        pool.mark_failed(selected, RuntimeError("HTTP status 401: old token revoked"))

        self.assertTrue(current.is_available)
        self.assertEqual(current.disabled_reason, "")

    def test_old_probe_success_cannot_clear_current_reload_snapshot(self) -> None:
        old = Account("claude", "same", token="same-token")
        old.disable("invalid_grant: old")
        old.disabled_at = time.time() - poolmod.RECOVERY_PROBE_AFTER - 1
        current = Account("claude", "same", token="same-token")
        current.disable("invalid_grant: reloaded")
        current.disabled_at = old.disabled_at
        pool = self._loaded_pool(old)
        selected = pool.pick("claude")
        self.assertIs(selected, old)

        with patch.object(poolmod, "load_accounts", return_value=[current]):
            pool.reload()
        pool.mark_success(selected)

        self.assertIs(pool.find("claude", "same"), current)
        self.assertTrue(current.is_disabled)

    def test_only_one_concurrent_probe_gets_lease(self) -> None:
        acc = Account("claude", "probe", token="t")
        acc.disable("invalid_grant: old")
        acc.disabled_at = time.time() - poolmod.RECOVERY_PROBE_AFTER - 1
        pool = self._pool(acc)
        barrier = threading.Barrier(3)
        results: list[Account | None] = []

        def pick() -> None:
            barrier.wait()
            results.append(pool.pick("claude"))

        threads = [threading.Thread(target=pick) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(sum(result is acc for result in results), 1)
        self.assertEqual(sum(result is None for result in results), 1)

        # 失败完成后租约释放；冷却结束即可再次探测。
        pool.mark_failed(acc, RuntimeError("temporary connection problem"))
        acc._cooling_until = 0.0
        self.assertIs(pool.pick("claude"), acc)


class AtomicReload(unittest.TestCase):
    def test_old_snapshot_remains_visible_until_atomic_swap(self) -> None:
        old = Account("claude", "old", token="old")
        new = Account("claude", "new", token="new")
        pool = poolmod.AccountPool()
        pool._accounts = {"claude": [old]}
        pool._index = {"claude": 0}
        pool._loaded = True
        load_started = threading.Event()
        finish_load = threading.Event()
        original_load = poolmod.load_accounts

        def blocking_load() -> list[Account]:
            load_started.set()
            self.assertTrue(finish_load.wait(timeout=2))
            return [new]

        poolmod.load_accounts = blocking_load
        reload_thread = threading.Thread(target=pool.reload)
        try:
            reload_thread.start()
            self.assertTrue(load_started.wait(timeout=2))

            self.assertEqual(pool.accounts("claude"), [old])
            self.assertIs(pool.find("claude", "old"), old)
            self.assertEqual(pool.status()["claude"][0]["id"], "old")

            finish_load.set()
            reload_thread.join(timeout=2)
            self.assertFalse(reload_thread.is_alive())
            self.assertEqual(pool.accounts("claude"), [new])
            self.assertIsNone(pool.find("claude", "old"))
            self.assertIs(pool.find("claude", "new"), new)
        finally:
            finish_load.set()
            reload_thread.join(timeout=2)
            poolmod.load_accounts = original_load

    def test_reload_retries_if_management_mutates_after_disk_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_account(Path(tmp))
            current = _account_from_file(path)
            pool = poolmod.AccountPool()
            pool._accounts = {"claude": [current]}
            pool._index = {"claude": 0}
            pool._loaded = True
            first_read_done = threading.Event()
            allow_first_swap = threading.Event()
            load_count = 0

            def racing_load() -> list[Account]:
                nonlocal load_count
                load_count += 1
                loaded = _account_from_file(path)
                self.assertIsNotNone(loaded)
                if load_count == 1:
                    first_read_done.set()
                    self.assertTrue(allow_first_swap.wait(timeout=2))
                return [loaded]

            with patch.object(poolmod, "load_accounts", side_effect=racing_load):
                thread = threading.Thread(target=pool.reload)
                thread.start()
                self.assertTrue(first_read_done.wait(timeout=2))
                pool.set_enabled("claude", "a@example.com", False)
                allow_first_swap.set()
                thread.join(timeout=3)

            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(load_count, 2)
            self.assertTrue(pool.find("claude", "a@example.com").is_disabled)
            self.assertFalse(json.loads(path.read_text())["enabled"])

    def test_reload_retries_if_token_refresh_persists_after_disk_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_account(Path(tmp), token="old-token")
            current = _account_from_file(path)
            pool = poolmod.AccountPool()
            pool._accounts = {"claude": [current]}
            pool._index = {"claude": 0}
            pool._loaded = True
            first_read_done = threading.Event()
            allow_first_swap = threading.Event()
            load_count = 0

            def racing_load() -> list[Account]:
                nonlocal load_count
                load_count += 1
                loaded = _account_from_file(path)
                self.assertIsNotNone(loaded)
                if load_count == 1:
                    first_read_done.set()
                    self.assertTrue(allow_first_swap.wait(timeout=2))
                return [loaded]

            with patch.object(poolmod, "load_accounts", side_effect=racing_load):
                thread = threading.Thread(target=pool.reload)
                thread.start()
                self.assertTrue(first_read_done.wait(timeout=2))
                current.token = "fresh-token"
                self.assertTrue(current.persist(fields={"token"}))
                allow_first_swap.set()
                thread.join(timeout=3)

            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(load_count, 2)
            self.assertEqual(json.loads(path.read_text())["token"], "fresh-token")
            self.assertEqual(pool.find("claude", "a@example.com").token, "fresh-token")

    def test_unrelated_persist_does_not_clear_another_accounts_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path_a = root / "a.json"
            path_b = root / "b.json"
            for path, email, token in (
                (path_a, "a@example.com", "token-a"),
                (path_b, "b@example.com", "token-b"),
            ):
                path.write_text(
                    json.dumps(
                        {
                            "type": "claude",
                            "email": email,
                            "token": token,
                            "enabled": True,
                        }
                    ),
                    encoding="utf-8",
                )
            account_a = _account_from_file(path_a)
            account_b = _account_from_file(path_b)
            pool = poolmod.AccountPool()
            pool._accounts = {"claude": [account_a, account_b]}
            pool._index = {"claude": 0}
            pool._loaded = True
            account_a.cool_down(3600)

            account_b.quota = {"weekly": {"used_percent": 25}}
            self.assertTrue(account_b.persist(fields={"quota"}))

            def fresh_accounts() -> list[Account]:
                return [_account_from_file(path_a), _account_from_file(path_b)]

            with patch.object(poolmod, "load_accounts", side_effect=fresh_accounts):
                selected = pool.pick("claude")
                current_a = pool.find("claude", "a@example.com")

            self.assertEqual(selected.id, "b@example.com")
            self.assertTrue(current_a.is_cooling)


class ExpiryAndRefresh(unittest.TestCase):
    def test_parse_expiry_iso_and_epoch(self) -> None:
        self.assertGreater(_parse_expiry_ts("2030-01-01T00:00:00Z"), 0)
        self.assertEqual(_parse_expiry_ts(1893456000.0), 1893456000.0)
        self.assertEqual(_parse_expiry_ts(""), 0.0)
        self.assertEqual(_parse_expiry_ts(None), 0.0)

    def test_needs_refresh_respects_skew(self) -> None:
        acc = Account("antigravity", "e", expiry=time.time() + 100)
        self.assertTrue(acc.needs_refresh(skew_seconds=300))
        self.assertFalse(acc.needs_refresh(skew_seconds=10))
        # 不追踪 expiry 的后端永不刷新
        self.assertFalse(Account("claude", "c", token="t").needs_refresh())

    def test_persist_writes_expiry_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_account(Path(tmp))
            acc = _account_from_file(path)
            acc.expiry = 1893456000.0
            acc.persist()
            self.assertIn("expiry", json.loads(path.read_text()))


class CodexModelCapabilities(unittest.TestCase):
    def test_reads_only_list_visible_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "models_cache.json").write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": "gpt-5.6-terra", "visibility": "list"},
                            {"slug": "codex-auto-review", "visibility": "hide"},
                        ]
                    }
                )
            )
            self.assertEqual(
                _codex_models_from_home(str(home)), frozenset({"gpt-5.6-terra"})
            )

    def test_account_matches_model_and_effort_suffix(self) -> None:
        acc = Account(
            "codex",
            "pro",
            home="/tmp/pro",
            supported_models=frozenset({"gpt-5.6-terra"}),
        )
        self.assertTrue(acc.supports_model("gpt-5.6-terra@medium"))
        self.assertFalse(acc.supports_model("gpt-5.3-codex-spark"))

class PriorityWeightRouting(unittest.TestCase):
    def _pool(self, accounts: list[Account]) -> poolmod.AccountPool:
        pool = poolmod.AccountPool()
        pool._accounts = {"claude": list(accounts)}
        pool._index = {"claude": 0}
        pool._loaded = True
        return pool

    def test_primary_group_served_before_backup(self) -> None:
        primary = Account("claude", "primary", token="t", priority=0)
        backup = Account("claude", "backup", token="t", priority=1)
        pool = self._pool([primary, backup])
        # 主号可用时，连续多次都只命中主号
        picks = {pool.pick("claude").id for _ in range(10)}
        self.assertEqual(picks, {"primary"})

    def test_spillover_to_backup_when_primary_unavailable(self) -> None:
        primary = Account("claude", "primary", token="t", priority=0)
        backup = Account("claude", "backup", token="t", priority=1)
        pool = self._pool([primary, backup])
        primary.cool_down(60)  # 主号冷却
        self.assertEqual(pool.pick("claude").id, "backup")
        primary.reset()  # 主号恢复 → 重新优先主号
        self.assertEqual(pool.pick("claude").id, "primary")

    def test_weighted_distribution_within_group(self) -> None:
        heavy = Account("claude", "heavy", token="t", priority=0, weight=3)
        light = Account("claude", "light", token="t", priority=0, weight=1)
        pool = self._pool([heavy, light])
        counts = {"heavy": 0, "light": 0}
        for _ in range(40):  # 4 的整数倍，weight 3:1 → 30:10
            counts[pool.pick("claude").id] += 1
        self.assertEqual(counts["heavy"], 30)
        self.assertEqual(counts["light"], 10)

    def test_defaults_are_plain_round_robin(self) -> None:
        a = Account("claude", "a", token="t")
        b = Account("claude", "b", token="t")
        pool = self._pool([a, b])
        seq = [pool.pick("claude").id for _ in range(4)]
        self.assertEqual(seq, ["a", "b", "a", "b"])

    def test_predicate_subsets_have_independent_cursors(self) -> None:
        x1 = Account(
            "codex", "x1", home="/tmp/x1", supported_models=frozenset({"x"})
        )
        x2 = Account(
            "codex", "x2", home="/tmp/x2", supported_models=frozenset({"x"})
        )
        y1 = Account(
            "codex", "y1", home="/tmp/y1", supported_models=frozenset({"y"})
        )
        y2 = Account(
            "codex", "y2", home="/tmp/y2", supported_models=frozenset({"y"})
        )
        pool = poolmod.AccountPool()
        pool._accounts = {"codex": [x1, x2, y1, y2]}
        pool._index = {"codex": 0}
        pool._loaded = True

        def supports(model: str):
            return lambda account: account.supports_model(model)

        sequence = [
            pool.pick("codex", predicate=supports("x")).id,
            pool.pick("codex", predicate=supports("y")).id,
            pool.pick("codex", predicate=supports("x")).id,
            pool.pick("codex", predicate=supports("y")).id,
        ]

        self.assertEqual(sequence, ["x1", "y1", "x2", "y2"])


if __name__ == "__main__":
    unittest.main()
