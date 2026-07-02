"""额度查询：归一化、刷新流程、错误处理（HTTP 全部 mock，不触网）。"""
from __future__ import annotations

import base64
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipool import quota
from clipool.account import Account


def _fake_jwt(account_id: str) -> str:
    """造一个能被 _codex_account_id 解析出 chatgpt_account_id 的假 JWT。"""
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{b64}.sig"


class _Resp:
    def __init__(self, status: int, data: dict):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class NormalizeWindow(unittest.TestCase):
    def test_reset_at_passthrough(self) -> None:
        w = quota._normalize_window(
            {"used_percent": 33, "reset_at": 1782556912, "limit_window_seconds": 18000}
        )
        self.assertEqual(w, {"used_percent": 33, "reset_at": 1782556912, "window_minutes": 300})

    def test_reset_after_seconds_to_absolute(self) -> None:
        before = time.time()
        w = quota._normalize_window({"used_percent": 4, "reset_after_seconds": 600})
        self.assertGreaterEqual(w["reset_at"], int(before + 600) - 2)
        self.assertEqual(w["window_minutes"], None)

    def test_clamps_and_handles_missing(self) -> None:
        self.assertIsNone(quota._normalize_window(None))
        self.assertEqual(quota._normalize_window({"used_percent": 250})["used_percent"], 100)
        self.assertIsNone(quota._normalize_window({})["used_percent"])


class CodexAccountId(unittest.TestCase):
    def test_extracts_account_id(self) -> None:
        self.assertEqual(quota._codex_account_id(_fake_jwt("acct-123")), "acct-123")

    def test_garbage_token_returns_none(self) -> None:
        self.assertIsNone(quota._codex_account_id("not-a-jwt"))


class NormalizeClaudeWindow(unittest.TestCase):
    def test_utilization_and_iso_reset(self) -> None:
        w = quota._normalize_claude_window(
            {"utilization": 12.6, "resets_at": "2026-06-27T15:00:00Z"}, 300
        )
        self.assertEqual(w["used_percent"], 13)  # round
        self.assertEqual(w["window_minutes"], 300)
        self.assertEqual(w["reset_at"], quota._iso_to_ts("2026-06-27T15:00:00Z"))

    def test_missing_window(self) -> None:
        self.assertIsNone(quota._normalize_claude_window(None, 300))


class RefreshQuota(unittest.TestCase):
    def _codex(self) -> Account:
        return Account("codex", "a@x.com", token="old", refresh_token="r1", source_path="")

    def _claude(self, **kw) -> Account:
        return Account("claude", "c@x.com", token="oat-old", refresh_token="r1", source_path="", **kw)

    def test_unsupported_backend_returns_none(self) -> None:
        acc = Account("grok", "g@x.com", token="t")
        self.assertIsNone(quota.refresh_quota(acc))

    def test_claude_success_without_refresh(self) -> None:
        # 现有 token 直接可用（200），不应触发刷新
        usage = {
            "five_hour": {"utilization": 5, "resets_at": "2026-06-27T15:00:00Z"},
            "seven_day": {"utilization": 40, "resets_at": "2026-07-01T00:00:00Z"},
        }
        acc = self._claude()
        with mock.patch.object(quota.requests, "get", return_value=_Resp(200, usage)) as get, \
             mock.patch.object(quota.requests, "post") as post:
            q = quota.refresh_quota(acc)
        self.assertEqual(q["five_hour"]["used_percent"], 5)
        self.assertEqual(q["weekly"]["used_percent"], 40)
        self.assertEqual(q["weekly"]["window_minutes"], 7 * 24 * 60)
        post.assert_not_called()  # 未过期不刷新（避免触发限流）
        self.assertEqual(acc.token, "oat-old")

    def test_claude_refreshes_on_401(self) -> None:
        usage = {"five_hour": {"utilization": 1, "resets_at": "2026-06-27T15:00:00Z"}, "seven_day": None}
        responses = [_Resp(401, {}), _Resp(200, usage)]  # 第一次 401 → 刷新 → 第二次 200
        acc = self._claude()
        with mock.patch.object(quota.requests, "get", side_effect=responses), \
             mock.patch.object(quota.requests, "post", return_value=_Resp(200, {"access_token": "oat-new"})):
            q = quota.refresh_quota(acc)
        self.assertEqual(q["five_hour"]["used_percent"], 1)
        self.assertEqual(acc.token, "oat-new")  # 刷新后的新 token 已回写

    def test_claude_pre_refresh_refreshes_first(self) -> None:
        # pre_refresh=True：先刷新 token 再查，usage 只调用一次（已是新 token）
        usage = {"five_hour": {"utilization": 7, "resets_at": "2026-06-27T15:00:00Z"}, "seven_day": None}
        acc = self._claude()
        with mock.patch.object(quota.requests, "get", return_value=_Resp(200, usage)) as get, \
             mock.patch.object(quota.requests, "post", return_value=_Resp(200, {"access_token": "oat-fresh"})) as post:
            q = quota.refresh_quota(acc, pre_refresh=True)
        post.assert_called_once()      # 预刷新发生了
        self.assertEqual(get.call_count, 1)  # 无需 401 兜底重试
        self.assertEqual(acc.token, "oat-fresh")
        self.assertEqual(q["five_hour"]["used_percent"], 7)

    def test_claude_401_without_refresh_token_errors(self) -> None:
        acc = self._claude()
        acc.refresh_token = ""
        with mock.patch.object(quota.requests, "get", return_value=_Resp(401, {})):
            with self.assertRaises(RuntimeError):
                quota.refresh_quota(acc)
        self.assertIn("失效", acc.quota_error)

    def test_success_populates_quota(self) -> None:
        acc = self._codex()
        fresh = _fake_jwt("acct-9")
        usage = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 4, "limit_window_seconds": 18000, "reset_after_seconds": 100},
                "secondary_window": {"used_percent": 33, "limit_window_seconds": 604800, "reset_after_seconds": 200},
            },
        }
        with mock.patch.object(quota.requests, "post", return_value=_Resp(200, {"access_token": fresh})), \
             mock.patch.object(quota.requests, "get", return_value=_Resp(200, usage)):
            q = quota.refresh_quota(acc)
        self.assertEqual(q["plan_type"], "plus")
        self.assertEqual(q["five_hour"]["used_percent"], 4)
        self.assertEqual(q["weekly"]["used_percent"], 33)
        self.assertEqual(acc.quota, q)
        self.assertEqual(acc.quota_error, "")
        self.assertEqual(acc.token, fresh)  # 新 token 已回写
        self.assertGreater(acc.quota_updated_at, 0)

    def test_refresh_failure_records_error(self) -> None:
        acc = self._codex()
        with mock.patch.object(
            quota.requests, "post",
            return_value=_Resp(401, {"error": {"code": "refresh_token_invalidated"}}),
        ):
            with self.assertRaises(RuntimeError):
                quota.refresh_quota(acc)
        self.assertIn("refresh_token_invalidated", acc.quota_error)
        self.assertIsNone(acc.quota)

    def test_missing_refresh_token_errors(self) -> None:
        acc = Account("codex", "a@x.com", token="old", refresh_token="", source_path="")
        with self.assertRaises(RuntimeError):
            quota.refresh_quota(acc)
        self.assertIn("refresh_token", acc.quota_error)


class AntigravityQuota(unittest.TestCase):
    QUOTA_RESPONSE = {
        "response": {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"window": "weekly", "displayName": "Weekly Limit",
                         "remainingFraction": 0.61, "resetTime": "2026-06-28T14:32:27Z"},
                        {"window": "5h", "displayName": "Five Hour Limit",
                         "remainingFraction": 1, "resetTime": "2026-06-27T11:56:13Z"},
                    ],
                },
            ],
        }
    }
    STATUS_RESPONSE = {"userStatus": {"email": "me@x.com", "planStatus": {"planInfo": {"planName": "Pro"}}}}

    def setUp(self) -> None:
        quota._agy_cache.update(ts=0.0, snap=None)  # 清缓存避免串测

    def _patch(self):
        def fake_post(port, path, body):
            return self.QUOTA_RESPONSE if path == quota.ANTIGRAVITY_QUOTA_PATH else self.STATUS_RESPONSE
        return (
            mock.patch.object(quota, "_agy_listening_ports", return_value=[52705]),
            mock.patch.object(quota, "_agy_post", side_effect=fake_post),
        )

    def test_parse_groups_orders_and_converts_fraction(self) -> None:
        windows = quota._parse_agy_groups(self.QUOTA_RESPONSE["response"]["groups"])
        # 5 小时排在本周前；remainingFraction → used_percent
        self.assertEqual(windows[0]["label"], "Gemini Models · Five Hour Limit")
        self.assertEqual(windows[0]["used_percent"], 0)
        self.assertEqual(windows[1]["label"], "Gemini Models · Weekly Limit")
        self.assertEqual(windows[1]["used_percent"], 39)  # round((1-0.61)*100)

    def test_matching_email_attaches_quota(self) -> None:
        acc = Account("antigravity", "me@x.com", home="/h", source_path="")
        p1, p2 = self._patch()
        with p1, p2:
            q = quota.refresh_quota(acc)
        self.assertEqual(q["plan_type"], "Pro")
        self.assertEqual(len(q["windows"]), 2)
        self.assertEqual(acc.quota_error, "")

    def test_non_matching_email_errors(self) -> None:
        acc = Account("antigravity", "other@x.com", home="/h", source_path="")
        p1, p2 = self._patch()
        with p1, p2:
            with self.assertRaises(RuntimeError):
                quota.refresh_quota(acc)
        self.assertIn("me@x.com", acc.quota_error)

    def test_agy_not_running_errors(self) -> None:
        acc = Account("antigravity", "me@x.com", home="/h", source_path="")
        with mock.patch.object(quota, "_agy_listening_ports", return_value=[]):
            with self.assertRaises(RuntimeError):
                quota.refresh_quota(acc)
        self.assertIn("agy", acc.quota_error)


if __name__ == "__main__":
    unittest.main()
