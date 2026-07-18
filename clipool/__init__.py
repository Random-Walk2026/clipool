"""proxy —— 本地 OpenAI-compatible 代理，把订阅 CLI 变成 HTTP API。

架构思路源自 CLIProxyAPI（Go 版），本包是纯 Python 版：
  - 服务端：FastAPI + uvicorn，暴露 /v1/chat/completions（streaming 支持）
  - 执行层：runner.py，封装 claude/codex/grok/antigravity/copilot 的 subprocess 调用
  - 路由层：router.py，解析 "provider/model@effort" 格式的模型名

快速上手
--------
启动服务（默认 8318 端口；8317 留给 Go 版 CLIProxyAPI 常驻服务）::

    python -m clipool
    python -m clipool --port 8319 --host 0.0.0.0

Python 调用（服务启动后）::

    from clipool import get_client
    client = get_client()
    resp = client.chat.completions.create(
        model="claude/sonnet@high",
        messages=[{"role": "user", "content": "你好"}],
    )
    print(resp.choices[0].message.content)

LangChain 调用::

    from clipool import get_langchain_model
    llm = get_langchain_model("claude/sonnet")
    print(llm.invoke("你好").content)

或直接在代码里不启服务、进程内调用（无 HTTP 开销）::

    from clipool import run_with_pool
    print(run_with_pool("claude", "你好", model="sonnet", effort="high"))

run_with_pool 走 ~/.clipool/ 账号池（多账号轮换 + 冷却 + 永久禁用），
与 HTTP 服务语义一致；只想跑单次、绕过账号池时用 clipool.runner.run_cli。
"""

import os
from typing import Optional

from .config import DEFAULT_PORT, DEFAULT_URL
from .executor import run_with_pool
from .providers import SUPPORTED
from .router import parse_model, is_cli_model
from .version import __version__

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_URL",
    "__version__",
    "SUPPORTED",
    "parse_model",
    "is_cli_model",
    "run_with_pool",
    "get_client",
    "get_langchain_model",
]


def get_client(port: int = DEFAULT_PORT, api_key: Optional[str] = None):
    """返回指向本地 proxy 的 openai.OpenAI 客户端。"""
    from openai import OpenAI

    resolved_key = api_key or os.environ.get("CLIPOOL_API_KEY") or "proxy"
    return OpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key=resolved_key)


def get_langchain_model(
    model: str = "claude/sonnet",
    port: int = DEFAULT_PORT,
    api_key: Optional[str] = None,
    **kwargs,
):
    """返回指向本地 proxy 的 LangChain ChatOpenAI 模型。"""
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    resolved_key = api_key or os.environ.get("CLIPOOL_API_KEY") or "proxy"
    return ChatOpenAI(
        model=model,
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key=SecretStr(resolved_key),
        **kwargs,
    )
