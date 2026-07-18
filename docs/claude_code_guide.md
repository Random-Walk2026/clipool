# Claude Code 接入 Antigravity 使用指南

本文说明如何把 `clipool` 当成本地代理，让 Claude Code 通过 Anthropic 兼容接口使用你的 Antigravity 账号池。

## 目标

你有多个 Antigravity 账号，额度分散在不同 Google 账号里。`clipool` 做三件事：

1. 暴露本地 HTTP 服务：`http://127.0.0.1:8318/v1/messages`
2. 兼容 Claude Code 的 Anthropic API 请求格式
3. 从 `~/.clipool/` 读取多个 Antigravity profile，按账号池轮换使用

调用链：

```text
Claude Code
  -> ANTHROPIC_BASE_URL=http://127.0.0.1:8318
  -> clipool /v1/messages
  -> AccountPool 选择一个 antigravity profile（含主备号路由）
  -> 读取 profile 内的 antigravity-oauth-token（按需自动刷新）
  -> Google Cloud Code Assist v1internal API
```

## 准备账号目录

`clipool` 默认读取 `~/.clipool/`，每个 Antigravity 账号放一个 JSON：

```text
~/.clipool/
├── antigravity_1.json
├── antigravity_2.json
└── profiles/
    ├── agy_main/
    └── agy_2/
```

先为每个账号建独立 profile 目录并登录：

```bash
mkdir -p ~/.clipool/profiles/agy_main
HOME="$HOME/.clipool/profiles/agy_main" agy -p "ping"
```

再创建对应的账号 JSON：

```json
{
  "type": "antigravity",
  "email": "account@example.com",
  "home": "~/.clipool/profiles/agy_main",
  "enabled": true
}
```

token 文件实际位于：

```text
~/.clipool/profiles/agy_main/.gemini/antigravity-cli/antigravity-oauth-token
```

`clipool` 会读取其中的 `access_token`、`refresh_token` 和过期时间。token 过期前 5 分钟会自动刷新，刷新后把新的过期时间写回账号 JSON。如果没有配置刷新所需的 OAuth client 环境变量，会提示你重新用 `agy` 登录对应 profile。

详细账号配置（主备号、priority / weight 字段、禁用恢复等）见 [accounts.md](accounts.md)。

## 启动本地代理

```bash
python -m clipool --port 8318
```

验证：

```bash
curl http://127.0.0.1:8318/health
# {"status":"ok","version":"0.1.0"}

curl http://127.0.0.1:8318/v0/management/accounts/antigravity
```

新增或修改账号 JSON 后，无需重启：

```bash
curl -X POST http://127.0.0.1:8318/v0/management/reload
```

## 接入 Claude Code

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8318"
export ANTHROPIC_AUTH_TOKEN="local-any-value"
claude --model claude-sonnet-4-6
```

如果想限制只有本机可调代理，加一个本地密钥：

```bash
# 启动代理时：
export CLIPOOL_API_KEY="your-local-secret"
python -m clipool --port 8318

# 另一个终端：
export ANTHROPIC_BASE_URL="http://127.0.0.1:8318"
export ANTHROPIC_AUTH_TOKEN="your-local-secret"   # 与 CLIPOOL_API_KEY 一致
claude --model claude-sonnet-4-6
```

`CLIPOOL_API_KEY` 只在本地代理层校验，与 Google / Anthropic 无关。它保护
`/v1/messages` 等生成接口、`/v1/models` 和全部 `/v0/management/*`；只有
面板外壳与 `/health` 保持公开。

## 手动测试

不启动 Claude Code，直接 curl 验证代理是否正常：

```bash
curl http://127.0.0.1:8318/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer local-any-value" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 256,
    "stream": false,
    "messages": [{"role": "user", "content": "用一句话说明你是谁"}]
  }'
```

## 常见问题

**Claude Code 报 401**
确认 `ANTHROPIC_AUTH_TOKEN` 与 `CLIPOOL_API_KEY` 值一致：

```bash
echo "$CLIPOOL_API_KEY"
echo "$ANTHROPIC_AUTH_TOKEN"
```

**找不到 token 文件**

```bash
cat ~/.clipool/antigravity_1.json
find ~/.clipool/profiles/agy_main -name antigravity-oauth-token
```

**token 过期**

```bash
HOME="$HOME/.clipool/profiles/agy_main" agy -p "ping"
```

**macOS 钥匙串弹窗**

正常的 clipool CLI fallback 不应弹密码框：它只为
`CLIPOOL_AUTH_DIR/profiles/` 下的虚拟 profile 创建随机名称、随机机器密码的专用
keychain，并把该虚拟 HOME 的 default/search list 只指向它；状态保存在
`.clipool-keychain.json`（`0600`），原有 `login.keychain-db` 不会动。使用默认目录
之外的专用 profile 时，需要按账号指南写入内容精确的
`.clipool-managed-profile` 标记。真实 HOME、未标记外部目录、Keychains 或
Preferences 目录 symlink、缺失 HOME、损坏状态无法安全恢复或任一 `security`
失败都会在 `agy` 启动前中止。详见
[accounts.md](accounts.md#macos-虚拟-profile-钥匙串)。

**账号额度打满**

`clipool` 会把失败账号冷却一段时间，自动尝试下一个账号：

```bash
curl http://127.0.0.1:8318/v0/management/accounts/antigravity
```

`cooling: true` 表示该账号临时跳过。`disabled: true` 表示认证失效；只有
`invalid_grant` 自动禁用会在约 10 分钟后尝试一次租约探测，一般 401、token
撤销与人工禁用需修复登录态后手动启用或 reload。

## 当前能力范围

已支持：
- Claude Code 通过 `ANTHROPIC_BASE_URL` 接入
- `/v1/messages` 基础请求及 Anthropic SSE 响应
- `/v1/messages/count_tokens` 轻量估算
- Antigravity profile token 读取与自动刷新（提前 300s）
- 多账号轮换、主备号路由（priority / weight）、失败冷却与 `invalid_grant` 租约探测
- 直连失败时自动回退到 `agy --print`
- macOS fallback 使用受管虚拟 profile 的随机专用 keychain；准备失败时 fail closed，不弹密码框
- 管理 API：查看账号状态、热重载

仍是 MVP：
- 上游完整收齐后再包装成 SSE，不是逐 token 真流式
- `tool_use` 转为文本上下文，尚无结构化工具回传
- `thinking` / signature 透传未完整实现
- token 自动刷新需提供 `ANTIGRAVITY_OAUTH_CLIENT_ID` 和 `ANTIGRAVITY_OAUTH_CLIENT_SECRET`
