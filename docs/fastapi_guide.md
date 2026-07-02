# FastAPI 实现要点

本文梳理 `clipool` 服务端（`clipool/server.py`）用到的 FastAPI 核心技术点，方便后续扩展或独立成新服务时参考。

## 1. 路由函数只做协议适配

`server.py` 的核心路由：

```python
@app.post("/v1/messages")
async def anthropic_messages(req: AnthropicMessagesRequest, request: Request):
    _authorize_optional(request)
    content = await _run_anthropic_with_pool(req)
    ...
```

路由层只做三件事：

1. 校验本地 token
2. 把请求交给账号池和 provider
3. 把结果包装成 Anthropic JSON 或 SSE

具体 provider 细节不写在路由里。这样以后新增 `/v1/responses`、Gemini native API、OpenAI Responses API，都可以继续复用同一个账号池。

## 2. Pydantic model 接住外部协议

`AnthropicMessagesRequest` 定义在 `clipool/anthropic.py`：

```python
class AnthropicMessagesRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int = 4096
    stream: bool = False
```

Claude Code 会传很多字段（`tools`、`thinking`、`metadata` 等）。先用宽松 schema 全部接住，哪怕 MVP 阶段不完整处理，也能避免 FastAPI 因未知字段直接 422 失败。需要精确处理哪个字段时，再加对应字段定义。

## 3. StreamingResponse 输出 Anthropic SSE 格式

`anthropic_sse_response()` 当前是"模拟流式"——先等上游完整返回，再按顺序输出 Anthropic SSE 事件：

```text
event: message_start
event: content_block_start
event: content_block_delta
event: content_block_stop
event: message_delta
event: message_stop
```

已能让 Claude Code 识别为流式响应。后续要做真流式，只需把 provider 层改成边读上游边 `yield`，路由和 Claude Code 接入方式不变。

## 4. 阻塞调用放进线程池

`agy --print` 和 Cloud Code Assist HTTP 请求都是同步阻塞调用。在 async 路由里直接跑会卡住整个事件循环，所以用：

```python
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="clipool")

content = await loop.run_in_executor(
    _executor,
    lambda acc=account: provider.run(text, model, effort, env_override=acc.env_override()),
)
```

FastAPI 是 asyncio 服务，高并发时不要在 async 函数里做任何阻塞 IO。

## 5. TestClient 不占端口，可单元测试协议

```python
from fastapi.testclient import TestClient
client = TestClient(server.app)
response = client.post("/v1/messages", json={...})
```

不会真正监听 8318 端口，适合在 CI 里验证路由、响应 JSON 结构、SSE 格式和鉴权逻辑。上游真实连通性单独用最小 curl 验证。

## 6. 可选鉴权：本地 API Key

```python
def _authorize_optional(request: Request) -> None:
    expected = os.environ.get("CLIPOOL_API_KEY", "").strip()
    if not expected:
        return   # 没配置就跳过
    token = request.headers.get("authorization", "")[7:]  # Bearer xxx
    token = token or request.headers.get("x-api-key", "")
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid CLIPOOL_API_KEY")
```

`CLIPOOL_API_KEY` 未设置时完全不鉴权，设置后 Bearer token 和 `x-api-key` 两种方式都兼容。

## 7. 管理 API 设计

```text
GET  /v0/management/accounts           # 所有 backend 账号状态（脱敏）
GET  /v0/management/accounts/{backend} # 指定 backend
POST /v0/management/reload             # 热重载账号文件，无需重启服务
```

账号 JSON 新增或修改后，`POST /reload` 即可生效，开发时不必反复重启。

## 关键文件索引

```text
clipool/server.py           FastAPI 路由与线程池
clipool/anthropic.py        Anthropic 请求/响应 schema 和 SSE 生成
clipool/pool.py             账号池：主备号路由、冷却、永久禁用
clipool/account.py          账号模型：expiry、priority/weight、persist()
clipool/router.py           模型字符串解析（provider/model@effort）
clipool/providers/
  antigravity_http.py         直连 Cloud Code Assist，含 token 刷新
  antigravity.py              agy --print CLI 调用（直连失败的回退）
  claude.py / codex.py / …   其他 CLI 后端
```

## 后续优化方向

1. 真流式：把 Cloud Code Assist 响应增量转成 Anthropic SSE，逐 token 输出
2. `tool_use` 结构化映射，而不是只转成文本上下文
3. project_id 本地缓存，减少每次冷启动的探测请求
4. `/v1/responses` 支持（OpenAI Responses API 格式）
