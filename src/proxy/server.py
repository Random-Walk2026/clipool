"""FastAPI 服务：OpenAI-compatible /v1/chat/completions。

架构要点（对齐 CLIProxyAPI）：
  1. asyncio + ThreadPoolExecutor：CLI subprocess 在线程里跑，不阻塞事件循环
  2. 账号池轮换：pool.pick() → env_override 注入 → mark_success/mark_failed
  3. 重试循环：单个账号失败时自动切换下一个（最多 max_retries 次）
  4. SSE streaming：CLI 是同步的，完整结果包装成 SSE data 流返回
  5. 管理 API：GET /v0/management/accounts 查看账号池状态
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .account import Account
from .anthropic import (
    AnthropicMessagesRequest,
    anthropic_message_body,
    anthropic_sse_response,
    content_to_text,
    estimated_token_count,
)
from .executor import execute_with_pool, run_with_pool
from .pool import get_pool
from .providers import SUPPORTED
from .providers.antigravity_http import AntigravityHTTPProvider
from .quota import QUOTA_SUPPORTED, refresh_quota, supports_quota
from .router import parse_model

app = FastAPI(title="cli_proxy API", version="2.0.0")

# 账号状态仪表盘 / Streamlit 管理台放在仓库根目录的 ui/ 下（不随包分发）。
# 默认从源码树定位（src/proxy/server.py → 上两级是仓库根），可用 CLI_PROXY_UI_DIR 覆盖。
_UI_DIR = Path(
    os.environ.get("CLI_PROXY_UI_DIR") or (Path(__file__).resolve().parents[2] / "ui")
)

# CLI subprocess 在专用线程池里跑
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cli_proxy")

_antigravity_http_provider = AntigravityHTTPProvider()


# ── 请求 / 响应 schema ────────────────────────────────────────────────────────

class Message(BaseModel):
    role: str
    # OpenAI 客户端既发纯字符串也发 content parts 列表（[{"type":"text","text":...}]），
    # 复用 anthropic.content_to_text 摊平（两种 parts 形状兼容）。
    content: Any = ""


class CompletionRequest(BaseModel):
    model: str
    messages: list[Message]
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    # OpenAI 的思考强度字段。model 串没写 @effort 时兜底用它
    # （agent_workflow 的 llm.transport_http 对 gpt-*/grok-* 正是这么传的）。
    reasoning_effort: Optional[str] = None


# ── 消息 → 纯文本 ─────────────────────────────────────────────────────────────

def _to_text(messages: list[Message]) -> str:
    parts: list[str] = []
    for m in messages:
        text = content_to_text(m.content)
        if m.role == "system":
            parts.append(f"[System]\n{text}")
        elif m.role == "user":
            parts.append(text)
        else:
            parts.append(f"[{m.role.capitalize()}]\n{text}")
    return "\n\n".join(parts)


# ── 响应构造 ──────────────────────────────────────────────────────────────────

def _non_stream_body(content: str, model: str, req_id: str) -> dict:
    return {
        "id": f"chatcmpl-{req_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _stream_response(
    content: str, model: str, req_id: str
) -> AsyncIterator[str]:
    """把完整文本包装成 SSE stream（CLI 同步，模拟流式）。"""
    cid = f"chatcmpl-{req_id}"
    ts = int(time.time())

    def chunk(delta: dict, finish: Optional[str] = None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": ts,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield chunk({"role": "assistant", "content": ""})
    yield chunk({"content": content})
    yield chunk({}, finish="stop")
    yield "data: [DONE]\n\n"


# ── CLI 执行（含账号轮换 + 重试；同步逻辑统一在 executor.py）─────────────────

async def _run_with_pool(provider_name: str, text: str, model: str, effort: str) -> str:
    """在线程池里跑 CLI，多账号轮换由 proxy.executor 统一处理。"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, lambda: run_with_pool(provider_name, text, model, effort)
    )


def _authorize_optional(request: Request) -> None:
    """Require a local API key only when CLI_PROXY_API_KEY is configured."""

    expected = os.environ.get("CLI_PROXY_API_KEY", "").strip()
    if not expected:
        return
    auth = request.headers.get("authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    token = token or request.headers.get("x-api-key", "").strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid CLI_PROXY_API_KEY")


async def _run_anthropic_with_pool(req: AnthropicMessagesRequest) -> str:
    """Run Anthropic /v1/messages through Antigravity profile accounts.

    轮换/冷却语义与 /v1/chat/completions 完全一致（executor.execute_with_pool）：
    池中有账号时绝不回落到默认 HOME，池为空才用进程默认登录态跑一次。
    """

    def _call(account: Optional[Account]) -> str:
        if account is None:
            account = Account(
                backend="antigravity",
                id="env-default",
                home=os.environ.get("HOME", ""),
            )
        return _antigravity_http_provider.run_messages(req, account)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, lambda: execute_with_pool("antigravity", _call)
    )


# ── 路由 ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """账号状态仪表盘：可视化展示账号池（调用 /v0/management/accounts）。"""
    page = _UI_DIR / "dashboard.html"
    try:
        return HTMLResponse(page.read_text(encoding="utf-8"))
    except OSError:
        raise HTTPException(status_code=500, detail="dashboard.html 缺失")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/v1/models")
async def list_models():
    pool = get_pool()
    status = pool.status()
    data = []
    for backend in SUPPORTED:
        accounts = status.get(backend, [])
        data.append({
            "id": backend,
            "object": "model",
            "created": 0,
            "owned_by": "cli_proxy",
            "accounts": len(accounts),
        })
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(req: CompletionRequest, request: Request):
    _authorize_optional(request)
    provider_name, model, effort = parse_model(req.model)
    # model 串没写 @effort 时，用 OpenAI 的 reasoning_effort 字段兜底
    effort = effort or (req.reasoning_effort or "").strip()

    if provider_name not in SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail=(
                f"未知 provider '{provider_name}'。支持：{', '.join(SUPPORTED)}。\n"
                f"格式：<provider>/<model>@<effort> 或 <model>(effort)，"
                f"例如 claude/sonnet@high、gpt-5.5(high)、grok-4(medium)"
            ),
        )

    text = _to_text(req.messages)
    req_id = uuid.uuid4().hex[:8]

    try:
        content = await _run_with_pool(provider_name, text, model, effort)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if req.stream:
        return StreamingResponse(
            _stream_response(content, req.model, req_id),
            media_type="text/event-stream",
        )

    return JSONResponse(_non_stream_body(content, req.model, req_id))


@app.post("/v1/messages")
async def anthropic_messages(req: AnthropicMessagesRequest, request: Request):
    _authorize_optional(request)
    req_id = uuid.uuid4().hex[:8]

    try:
        content = await _run_anthropic_with_pool(req)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if req.stream:
        return StreamingResponse(
            anthropic_sse_response(content, req.model, req_id),
            media_type="text/event-stream",
        )

    return JSONResponse(anthropic_message_body(content, req.model, req_id))


@app.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(req: AnthropicMessagesRequest, request: Request):
    _authorize_optional(request)
    return {"input_tokens": estimated_token_count(req)}


# ── 管理 API（对齐 CLIProxyAPI /v0/management/*）────────────────────────────

@app.get("/v0/management/accounts")
async def management_accounts():
    """列出所有 provider 的账号池状态（脱敏）。"""
    pool = get_pool()
    return {"accounts": pool.status()}


@app.get("/v0/management/accounts/{backend}")
async def management_accounts_backend(backend: str):
    """列出指定 backend 的账号状态。"""
    if backend not in SUPPORTED:
        raise HTTPException(status_code=404, detail=f"未知 provider：{backend}")
    pool = get_pool()
    return {"backend": backend, "accounts": pool.status().get(backend, [])}


class AccountAction(BaseModel):
    backend: str
    id: str
    action: str  # enable | disable | reset | refresh_quota
    pre_refresh: bool = False  # refresh_quota 时是否强制先刷新 token（claude 默认 usage-first）


@app.post("/v0/management/accounts/action")
async def management_account_action(req: AccountAction):
    """对单个账号执行管理操作（启用 / 禁用 / 重置冷却 / 刷新额度）。供管理面板按钮调用。"""
    pool = get_pool()
    action = req.action.lower().strip()
    if action == "enable":
        acc = pool.set_enabled(req.backend, req.id, True)
    elif action == "disable":
        acc = pool.set_enabled(req.backend, req.id, False)
    elif action == "reset":
        acc = pool.reset_account(req.backend, req.id)
    elif action == "refresh_quota":
        acc = pool.find(req.backend, req.id)
        if acc is None:
            raise HTTPException(status_code=404, detail=f"未找到账号：{req.backend}/{req.id}")
        if not supports_quota(acc.backend):
            raise HTTPException(status_code=400, detail=f"{acc.backend} 不支持额度查询")
        try:
            await asyncio.get_event_loop().run_in_executor(
                _executor, lambda: refresh_quota(acc, pre_refresh=req.pre_refresh)
            )
        except RuntimeError as exc:
            # 刷新失败时 quota_error 已写入账号，仍返回账号（前端展示错误）
            return JSONResponse(
                status_code=502,
                content={"status": "error", "detail": str(exc), "account": acc.to_dict()},
            )
        return {"status": "ok", "account": acc.to_dict()}
    else:
        raise HTTPException(status_code=400, detail=f"未知操作：{req.action}")
    if acc is None:
        raise HTTPException(
            status_code=404, detail=f"未找到账号：{req.backend}/{req.id}"
        )
    return {"status": "ok", "account": acc.to_dict()}


@app.post("/v0/management/quota/refresh")
async def management_quota_refresh(pre_refresh: bool = False):
    """刷新所有支持额度查询的账号（codex / claude），返回每个账号的结果。

    pre_refresh=true 时强制先刷新各账号 token 再查（claude 默认 usage-first 以避开刷新端点限流）。
    """
    pool = get_pool()
    loop = asyncio.get_event_loop()
    results: list[dict] = []
    for backend in QUOTA_SUPPORTED:
        for acc in pool.accounts(backend):
            entry = {"backend": acc.backend, "id": acc.id}
            try:
                await loop.run_in_executor(
                    _executor, lambda a=acc: refresh_quota(a, pre_refresh=pre_refresh)
                )
                entry["status"] = "ok"
            except RuntimeError as exc:
                entry["status"] = "error"
                entry["detail"] = str(exc)
            results.append(entry)
    return {"status": "ok", "results": results}


@app.post("/v0/management/reload")
async def management_reload():
    """重新从磁盘加载账号文件（添加新账号后调用）。"""
    get_pool().reload()
    return {"status": "reloaded"}
