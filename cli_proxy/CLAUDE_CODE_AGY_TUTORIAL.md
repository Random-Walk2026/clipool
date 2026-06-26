# Claude Code 接入 Antigravity 教学文档

这篇文档说明如何把 `cli_proxy` 当成一个可复用模块，让 Claude Code 通过本地 Anthropic-compatible API 使用你的 Antigravity 账号池。

## 目标

你现在有多个 Antigravity 账号，额度分散在不同 Google 账号里。`cli_proxy` 做三件事：

1. 暴露本地 HTTP 服务：`http://127.0.0.1:8317/v1/messages`
2. 兼容 Claude Code 的 Anthropic API 请求格式
3. 从 `~/.cli_proxy_api/` 读取多个 Antigravity profile，按账号池轮换使用

调用链是：

```text
Claude Code
  -> ANTHROPIC_BASE_URL=http://127.0.0.1:8317
  -> cli_proxy /v1/messages
  -> AccountPool 选择一个 antigravity profile
  -> 读取 profile 内的 antigravity-oauth-token
  -> Google Cloud Code Assist v1internal API
```

这样做的价值是把“账号轮换、冷却、统一 API 协议”集中在一个模块里。之后其他项目也可以引用同一个 `cli_proxy`，不需要每个项目都重新写一套多账号调度。

## 账号目录

`cli_proxy` 默认读取：

```text
~/.cli_proxy_api/
├── antigravity_1.json
├── antigravity_2.json
├── antigravity_3.json
├── antigravity_4.json
└── profiles/
    ├── agy_main/
    ├── agy_2/
    ├── agy_3/
    └── agy_4/
```

每个账号 JSON 只需要指向自己的 profile home：

```json
{
  "type": "antigravity",
  "email": "account@example.com",
  "home": "~/.cli_proxy_api/profiles/agy_main",
  "enabled": true
}
```

真正的 OAuth token 在：

```text
~/.cli_proxy_api/profiles/agy_main/.gemini/antigravity-cli/antigravity-oauth-token
```

`cli_proxy` 会读取这个文件里的 `access_token`、`refresh_token` 和 `expiry`。token 没过期时直接用；过期时会尝试刷新。如果没有配置刷新所需的 OAuth client env，就会提示你重新用 `agy` 登录对应 profile。

## 启动本地代理

在仓库根目录运行：

```bash
python -m cli_proxy --port 8317
```

健康检查：

```bash
curl http://127.0.0.1:8317/health
```

查看账号池：

```bash
curl http://127.0.0.1:8317/v0/management/accounts/antigravity
```

如果你新增或修改了账号 JSON，不用重启服务：

```bash
curl -X POST http://127.0.0.1:8317/v0/management/reload
```

## 让 Claude Code 使用本地代理

Claude Code 读取 Anthropic 兼容环境变量：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8317"
export ANTHROPIC_AUTH_TOKEN="local-any-value"
claude --model claude-sonnet-4-6
```

如果你想避免其他本机进程误调代理，可以给代理加本地密钥：

```bash
export CLI_PROXY_API_KEY="your-local-secret"
python -m cli_proxy --port 8317
```

另一个终端：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8317"
export ANTHROPIC_AUTH_TOKEN="your-local-secret"
claude --model claude-sonnet-4-6
```

`CLI_PROXY_API_KEY` 只在本地代理层校验。它不是 Anthropic key，也不是 Google key。

## FastAPI 知识点

这个模块里用到的 FastAPI 内容不复杂，但很适合以后独立成服务：

### 1. 路由函数就是协议适配层

`cli_proxy/server.py` 里的核心路由是：

```python
@app.post("/v1/messages")
async def anthropic_messages(req: AnthropicMessagesRequest, request: Request):
    _authorize_optional(request)
    content = await _run_anthropic_with_pool(req)
    ...
```

这里不要写具体 provider 细节。路由只做三件事：

1. 校验本地 token
2. 把请求交给账号池和 provider
3. 把结果包装成 Anthropic JSON 或 SSE

这样以后新增 `/v1/responses`、Gemini native API、OpenAI Responses API，都可以继续复用同一个账号池。

### 2. Pydantic model 负责接住外部协议

`AnthropicMessagesRequest` 放在 `cli_proxy/anthropic.py`：

```python
class AnthropicMessagesRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int = 4096
    stream: bool = False
```

Claude Code 会传很多字段，比如 `tools`、`thinking`、`metadata`。我们先把它们接住，哪怕 MVP 不完整处理，也避免 FastAPI 因未知字段或形状直接失败。

### 3. StreamingResponse 只负责输出 SSE 形状

当前 `anthropic_sse_response()` 是“模拟流式”：先等上游完整返回，再输出 Anthropic SSE 事件：

```text
message_start
content_block_start
content_block_delta
content_block_stop
message_delta
message_stop
```

这已经能让 Claude Code 识别为流式响应。后续要做真流式，只需要把 provider 层改成边读上游边 yield，不需要改 Claude Code 接入方式。

### 4. 慢任务放进线程池，避免阻塞事件循环

`agy --print` 和 HTTP 上游请求都是同步阻塞调用，所以 `server.py` 用：

```python
await loop.run_in_executor(_executor, ...)
```

FastAPI 本身是 async 服务。如果直接在 async 路由里跑阻塞命令，高并发时会卡住整个事件循环。

### 5. TestClient 可以不启动端口测试协议

测试里用的是：

```python
client = TestClient(server.app)
response = client.post("/v1/messages?beta=true", json={...})
```

这不会真的监听 `8317` 端口，适合验证路由、响应 JSON、SSE 格式和鉴权逻辑。真实上游 smoke 再单独用最小请求验证。

## 手动测试 `/v1/messages`

不启动 Claude Code，也可以直接 curl：

```bash
curl http://127.0.0.1:8317/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer local-any-value" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 256,
    "stream": false,
    "messages": [
      {"role": "user", "content": "用一句话说明你是谁"}
    ]
  }'
```

如果设置了 `CLI_PROXY_API_KEY`，这里的 Bearer 值必须和它一致。没设置时，本地代理不强制鉴权。

## 代码结构

关键文件：

```text
cli_proxy/server.py
  FastAPI 路由。新增了 /v1/messages 和 /v1/messages/count_tokens。

cli_proxy/anthropic.py
  Anthropic 请求和响应 helper。负责把 Claude Code 的 system/messages/tool_result
  归一化成 prompt，并生成 Anthropic SSE 事件。

cli_proxy/providers/antigravity_http.py
  直接读取 Antigravity profile token，调用 Cloud Code Assist HTTP API。

cli_proxy/account.py
  账号文件加载。antigravity 使用 home/profile 目录隔离账号。

cli_proxy/pool.py
  多账号轮换和失败冷却。
```

这个边界适合后续独立成一个包：

```text
cli_proxy/
  account.py
  pool.py
  anthropic.py
  providers/
    antigravity_http.py
  server.py
```

如果其他项目只需要“发请求”，可以直接启动 `python -m cli_proxy`，然后用 HTTP 调它。如果其他项目想内嵌，可以引用 `AccountPool` 和 `AntigravityHTTPProvider`。

## 当前能力边界

已支持：

- Claude Code 指向 `ANTHROPIC_BASE_URL`
- `/v1/messages` 基础请求
- `/v1/messages/count_tokens` 轻量估算
- Anthropic SSE 响应格式
- Antigravity profile token 文件读取
- token 过期或直连失败时退回同 profile 的 `agy --print`
- 多账号轮换、失败冷却、管理 API reload

仍是 MVP：

- 上游目前先完整收齐，再包装成 SSE，不是逐 token 真流式
- Claude Code 的复杂 `tool_use` 可以被转成文本上下文，但还没有做精确的工具调用回传
- `thinking` / signature 透传还没有完整实现
- token 过期自动刷新需要你提供 `ANTIGRAVITY_OAUTH_CLIENT_ID` 和 `ANTIGRAVITY_OAUTH_CLIENT_SECRET`；否则建议重新登录 profile

## 常见问题

### 1. Claude Code 报 401

如果你设置了 `CLI_PROXY_API_KEY`，确认：

```bash
echo "$CLI_PROXY_API_KEY"
echo "$ANTHROPIC_AUTH_TOKEN"
```

两个值必须一致。

### 2. 找不到 token 文件

检查账号 JSON 的 `home` 是否是正确 profile：

```bash
cat ~/.cli_proxy_api/antigravity_1.json
find ~/.cli_proxy_api/profiles/agy_main -name antigravity-oauth-token
```

### 3. token 过期

重新登录对应 profile：

```bash
HOME="$HOME/.cli_proxy_api/profiles/agy_main" agy -p "ping"
```

如果弹出 Keychain 相关窗口，继续按你原来的方式让 token 落到 profile 文件里。

### 4. 账号额度打满

`cli_proxy` 会把失败账号冷却一段时间，并尝试下一个账号。你可以查看状态：

```bash
curl http://127.0.0.1:8317/v0/management/accounts/antigravity
```

`cooling: true` 表示该账号临时跳过。

## 后续优化建议

优先级最高的下一步不是 UI，而是协议完整度：

1. 做真实上游 streaming，把 Cloud Code Assist 的增量响应逐段转成 Anthropic SSE
2. 增加 Claude Code tool_use 的结构化映射，而不是只转成文本
3. 为 project id 增加本地缓存，减少每次启动后的探测请求
4. 其他项目通过 `pip install -e ../cli_proxy` 引用这个独立包
