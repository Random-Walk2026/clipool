"""cli_proxy 配置：二进制路径和超时，全部可由 .env 覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PORT = 8317
DEFAULT_HOST = "127.0.0.1"
DEFAULT_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

CLI_TIMEOUT = int(os.environ.get("AGENT_LLM_CLI_TIMEOUT", "600"))

CLAUDE_BIN = os.environ.get("CLAUDE_CLI_BIN", "claude")
CODEX_BIN = os.environ.get("CODEX_CLI_BIN", "codex")
GROK_BIN = os.environ.get("GROK_CLI_BIN", "grok")
COPILOT_BIN = os.environ.get("COPILOT_CLI_BIN", "copilot")


def _default_antigravity_bin() -> str:
    configured = os.environ.get("ANTIGRAVITY_CLI_BIN", "").strip()
    if configured:
        return configured
    local_bin = Path.home() / ".local" / "bin" / "agy"
    return str(local_bin) if local_bin.exists() else "agy"


ANTIGRAVITY_BIN = _default_antigravity_bin()
