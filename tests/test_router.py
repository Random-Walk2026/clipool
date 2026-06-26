from __future__ import annotations

import unittest

from cli_proxy.router import is_cli_model, parse_model


class RouterTests(unittest.TestCase):
    def test_provider_prefix(self) -> None:
        self.assertEqual(parse_model("claude/sonnet@high"), ("claude", "sonnet", "high"))
        self.assertEqual(parse_model("antigravity/gemini-3.5-flash"), ("antigravity", "gemini-3.5-flash", ""))

    def test_parenthesized_effort_and_inference(self) -> None:
        self.assertEqual(parse_model("gpt-5.5(high)"), ("codex", "gpt-5.5", "high"))
        self.assertEqual(parse_model("gemini-3.5-flash(low)"), ("antigravity", "gemini-3.5-flash", "low"))

    def test_is_cli_model(self) -> None:
        self.assertTrue(is_cli_model("claude/sonnet"))
        self.assertTrue(is_cli_model("gemini-3.5-flash"))
        self.assertFalse(is_cli_model("unknown-model"))


if __name__ == "__main__":
    unittest.main()
