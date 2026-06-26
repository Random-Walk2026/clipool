"""proxy —— 本地 OpenAI-compatible 代理，把订阅 CLI 变成 HTTP API。

架构思路源自 CLIProxyAPI（Go 版），本包是纯 Python 版：
  - 服务端：FastAPI + uvicorn，暴露 /v1/chat/completions（streaming 支持）
  - 执行层：runner.py，封装 claude/codex/grok/antigravity/copilot 的 subprocess 调用
  - 路由层：router.py，解析 "provider/model@effort" 格式的模型名

快速上手
--------
启动服务（默认 8317 端口）::

    python -m proxy
    python -m proxy --port 8318 --host 0.0.0.0

Python 调用（服务启动后）::

    from proxy import get_client
    client = get_client()
    resp = client.chat.completions.create(
        model="claude/sonnet@high",
        messages=[{"role": "user", "content": "你好"}],
    )
    print(resp.choices[0].message.content)

LangChain 调用::

    from proxy import get_langchain_model
    llm = get_langchain_model("claude/sonnet")
    print(llm.invoke("你好").content)

或直接在代码里不启服务、只调 runner（无 HTTP 开销）::

    from proxy.runner import run_cli
    print(run_cli("claude", "你好", model="sonnet", effort="high"))
"""

from .config import DEFAULT_PORT, DEFAULT_URL
from .providers import SUPPORTED
from .router import parse_model, is_cli_model

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_URL",
    "SUPPORTED",
    "parse_model",
    "is_cli_model",
    "get_client",
    "get_langchain_model",
]


def get_client(port: int = DEFAULT_PORT):
    """返回指向本地 proxy 的 openai.OpenAI 客户端。"""
    from openai import OpenAI

    return OpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key="proxy")


def get_langchain_model(model: str = "claude/sonnet", port: int = DEFAULT_PORT, **kwargs):
    """返回指向本地 proxy 的 LangChain ChatOpenAI 模型。"""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="proxy",
        **kwargs,
    )
