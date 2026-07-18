from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class AnthropicCompatibilityTests(unittest.TestCase):
    def test_messages_endpoint_returns_anthropic_sse(self) -> None:
        from clipool import server

        async def fake_run(req):
            self.assertEqual(req.model, "claude-sonnet-4-6")
            return "hello from agy"

        previous = server._run_anthropic_with_pool
        server._run_anthropic_with_pool = fake_run
        try:
            client = TestClient(server.app)
            response = client.post(
                "/v1/messages?beta=true",
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 128,
                    "stream": True,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
        finally:
            server._run_anthropic_with_pool = previous

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertIn("event: message_start", response.text)
        self.assertIn("event: content_block_delta", response.text)
        self.assertIn("hello from agy", response.text)
        self.assertIn("event: message_stop", response.text)

    def test_token_file_shape_loaded_from_profile_home(self) -> None:
        from clipool.providers.antigravity_http import load_antigravity_profile_token
        from clipool.providers.antigravity import _PROFILE_MARKER_CONTENT

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".clipool-managed-profile").write_text(
                _PROFILE_MARKER_CONTENT, encoding="utf-8"
            )
            token_path = (
                Path(tmp)
                / ".gemini"
                / "antigravity-cli"
                / "antigravity-oauth-token"
            )
            token_path.parent.mkdir(parents=True)
            token_path.write_text(
                json.dumps(
                    {
                        "token": {
                            "access_token": "access-123",
                            "token_type": "Bearer",
                            "refresh_token": "refresh-123",
                            "expiry": "2099-01-02T03:04:05.000000Z",
                        },
                        "auth_method": "oauth",
                    }
                ),
                encoding="utf-8",
            )

            token = load_antigravity_profile_token(tmp)

        self.assertEqual(token.access_token, "access-123")
        self.assertEqual(token.refresh_token, "refresh-123")
        self.assertEqual(token.token_type, "Bearer")
        self.assertFalse(token.is_expired())

    def test_malformed_token_expiry_is_reported_as_runtime_error(self) -> None:
        from clipool.providers.antigravity import _PROFILE_MARKER_CONTENT
        from clipool.providers.antigravity_http import load_antigravity_profile_token

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / ".clipool-managed-profile").write_text(
                _PROFILE_MARKER_CONTENT, encoding="utf-8"
            )
            token_path = (
                profile
                / ".gemini"
                / "antigravity-cli"
                / "antigravity-oauth-token"
            )
            token_path.parent.mkdir(parents=True)
            token_path.write_text(
                json.dumps(
                    {
                        "token": {
                            "access_token": "access-123",
                            "expiry": "not-a-date",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "expiry"):
                load_antigravity_profile_token(profile)

    def test_messages_to_prompt_keeps_claude_code_blocks(self) -> None:
        from clipool.anthropic import AnthropicMessagesRequest, messages_to_prompt

        req = AnthropicMessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=[
                {"type": "text", "text": "You are concise."},
                {"type": "text", "text": "Use tools carefully."},
            ],
            messages=[
                {"role": "user", "content": "Open the file."},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I will inspect it."}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": "file contents"}],
                        }
                    ],
                },
            ],
        )

        prompt = messages_to_prompt(req)

        self.assertIn("[System]\nYou are concise.\nUse tools carefully.", prompt)
        self.assertIn("[User]\nOpen the file.", prompt)
        self.assertIn("[Assistant]\nI will inspect it.", prompt)
        self.assertIn("[Tool result toolu_1]\nfile contents", prompt)

    def test_antigravity_http_provider_falls_back_to_agy_cli(self) -> None:
        from clipool.account import Account
        from clipool.anthropic import AnthropicMessagesRequest
        from clipool.providers.antigravity_http import AntigravityHTTPProvider

        req = AnthropicMessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=64,
            messages=[{"role": "user", "content": "ping"}],
        )
        account = Account(
            backend="antigravity",
            id="agy-test",
            home="/tmp/agy-test-home",
        )

        with patch(
            "clipool.providers.antigravity_http.load_fresh_antigravity_token",
            side_effect=RuntimeError("token expired"),
        ), patch(
            "clipool.providers.antigravity_http.AntigravityProvider.run",
            return_value="cli fallback ok",
        ) as run:
            result = AntigravityHTTPProvider().run_messages(req, account)

        self.assertEqual(result, "cli fallback ok")
        args, kwargs = run.call_args
        self.assertIn("[User]\nping", args[0])
        self.assertEqual(args[1], "claude-sonnet-4-6")
        self.assertEqual(args[2], "high")
        self.assertEqual(kwargs["env_override"]["HOME"], "/tmp/agy-test-home")

    def test_non_json_generate_response_uses_runtime_fallback(self) -> None:
        from clipool.account import Account
        from clipool.anthropic import AnthropicMessagesRequest
        from clipool.providers.antigravity_http import (
            AntigravityHTTPProvider,
            AntigravityProfileToken,
        )

        class NonJsonResponse:
            status_code = 200
            text = "not-json"

            def json(self):
                raise ValueError("not json")

        req = AnthropicMessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=64,
            messages=[{"role": "user", "content": "ping"}],
        )
        account = Account(
            backend="antigravity",
            id="agy-test",
            home="/tmp/agy-test-home",
            extra_env={"ANTIGRAVITY_PROJECT_ID": "project-1"},
        )
        token = AntigravityProfileToken(access_token="access")

        with patch(
            "clipool.providers.antigravity_http.load_fresh_antigravity_token",
            return_value=token,
        ), patch(
            "clipool.providers.antigravity_http.requests.post",
            return_value=NonJsonResponse(),
        ), patch(
            "clipool.providers.antigravity_http.AntigravityProvider.run",
            return_value="fallback after non-json",
        ):
            result = AntigravityHTTPProvider().run_messages(req, account)

        self.assertEqual(result, "fallback after non-json")


if __name__ == "__main__":
    unittest.main()
