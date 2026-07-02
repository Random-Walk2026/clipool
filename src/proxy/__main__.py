"""入口：python -m proxy [--port 8318] [--host 127.0.0.1]

启动后任意 OpenAI-compatible 客户端指向 http://127.0.0.1:8318/v1 即可使用。
（默认端口 8318：8317 留给 Go 版 CLIProxyAPI 的常驻服务，避免撞车。）

模型命名格式：<backend>/<model>@<effort>
  claude/sonnet@high   → Claude Code CLI，sonnet 模型，high effort
  codex/gpt-5          → Codex CLI，gpt-5 模型
  grok                 → Grok CLI，默认模型
  antigravity/gemini-3.5-flash-high  → Antigravity CLI
  copilot/gpt-4.1@medium             → GitHub Copilot CLI
"""
import argparse

import uvicorn

from .config import DEFAULT_PORT
from .server import app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="cli_proxy API — 本地 OpenAI-compatible 代理，路由到订阅 CLI"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"监听端口（默认 {DEFAULT_PORT}；8317 留给 Go 版 CLIProxyAPI）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    parser.add_argument("--reload", action="store_true", help="代码变更时自动重载（开发用）")
    args = parser.parse_args()

    print(f"cli_proxy API  →  http://{args.host}:{args.port}/v1")
    print(f"账号状态面板   →  http://{args.host}:{args.port}/")
    print(f"支持后端：claude / codex / grok / antigravity / copilot")
    print(f"模型格式：<backend>/<model>@<effort>，如 claude/sonnet@high")
    print()

    uvicorn.run(
        "proxy.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
