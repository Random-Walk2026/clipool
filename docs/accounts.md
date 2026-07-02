# 账号配置指南

`clipool` 从 `~/.clipool/`（可用 `CLIPOOL_AUTH_DIR` 覆盖）读取账号注册文件。

```text
~/.clipool/
├── antigravity_1.json
├── antigravity_2.json
├── claude_work.json
├── codex_personal.json
└── profiles/
    ├── agy_main/
    ├── agy_secondary/
    └── codex_personal/
```

该目录含本地登录态和 token，**不要提交到版本库**。

---

## Antigravity 账号

Antigravity 使用 profile 目录隔离多账号。为每个账号建独立 profile：

```bash
mkdir -p ~/.clipool/profiles/agy_main
HOME="$HOME/.clipool/profiles/agy_main" agy -p "ping"
```

然后创建账号注册文件：

```json
{
  "type": "antigravity",
  "email": "you@example.com",
  "home": "~/.clipool/profiles/agy_main",
  "enabled": true
}
```

token 文件路径：

```text
~/.clipool/profiles/agy_main/.gemini/antigravity-cli/antigravity-oauth-token
```

`clipool` 优先直接读取该 token 发起 HTTP 请求；直连失败时自动回退到 `agy --print`（同 profile）。

---

## Codex 账号

```bash
mkdir -p ~/.clipool/profiles/codex_personal
CODEX_HOME="$HOME/.clipool/profiles/codex_personal" codex login
```

账号文件：

```json
{
  "type": "codex",
  "email": "you@example.com",
  "home": "~/.clipool/profiles/codex_personal",
  "enabled": true
}
```

---

## Token 型账号（Claude / Grok / Copilot）

这三个后端直接在账号 JSON 里写 token 即可，也可以用环境变量兜底（无账号文件时生效）。

```json
{ "type": "claude",   "email": "you@example.com", "token": "YOUR_CLAUDE_TOKEN",  "enabled": true }
{ "type": "grok",     "email": "you@example.com", "token": "YOUR_GROK_TOKEN",    "enabled": true }
{ "type": "copilot",  "email": "you@example.com", "token": "YOUR_GITHUB_TOKEN",  "enabled": true }
```

环境变量兜底：

```bash
export CLAUDE_CODE_OAUTH_TOKEN="..."
export GROK_API_KEY="..."
export COPILOT_GITHUB_TOKEN="..."
```

---

## 主备号路由（priority 与 weight）

两个可选字段控制账号池的调度策略：

- **`priority`**（整数，默认 `0`）：数字越小越优先。只有当前 priority 组内全部账号不可用（冷却或禁用），才溢出到下一组（备号）。
- **`weight`**（整数，默认 `1`）：同一 priority 组内的流量份额。`weight: 3` 的账号命中频率是 `weight: 1` 的三倍。

示例：两个主号 3:1 分流，一个备号在主号全挂时顶上：

```json
{ "type": "claude", "email": "main@x.com",   "token": "...", "priority": 0, "weight": 3 }
{ "type": "claude", "email": "spare@x.com",  "token": "...", "priority": 0, "weight": 1 }
{ "type": "claude", "email": "backup@x.com", "token": "...", "priority": 1 }
```

不填这两个字段时，所有账号平等轮换（等同于原始 round-robin）。

---

## Token 生命周期与自动禁用

`clipool` 区分**临时故障**和**永久认证失效**：

| 错误类型 | 处理方式 |
|---|---|
| 超时、5xx 等瞬时错误 | 冷却 15 秒起，连续失败 ×2 递增（上限 5 分钟），成功即归零 |
| 429 配额 / 限速 | 冷却 60 秒起，连续失败 ×2 递增（上限 1 小时），成功即归零 |
| `invalid_grant`、401、token 撤销 | **永久禁用**，写回 JSON 文件，重启不再死磕 |

> 池中有账号但全部冷却/禁用时**直接报错**，不会回落到进程默认登录态；
> 只有该 backend 一个账号文件都没有时才用默认登录态（单账号模式）。
> token 还是 `YOUR_...` 占位符的模板文件不入池。

永久禁用后，JSON 文件会自动写入：

```json
{
  "enabled": false,
  "disabled_reason": "invalid_grant: Token has been expired or revoked.",
  "disabled_at": 1751000000.0
}
```

**半开自动恢复**：禁用满约 10 分钟后，账号池会放行一次试探请求。如果你已重新登录 / 刷新 token，该请求成功后会自动解除禁用，并清掉 JSON 里的 `disabled_reason`。

手动解除禁用：删掉 `disabled_reason` 字段（或改为 `"enabled": true`），再调用热重载接口。

---

## 直连 HTTP Token 刷新（Antigravity）

对于 Antigravity 直连 HTTP 路径，`clipool` 会在 token 过期前 **300 秒**主动刷新，刷新后把新的过期时间写回账号 JSON 的 `expiry` 字段。

自动刷新需要设置 OAuth client 环境变量：

```bash
export ANTIGRAVITY_OAUTH_CLIENT_ID="..."
export ANTIGRAVITY_OAUTH_CLIENT_SECRET="..."
```

未设置时，token 过期后自动回退到 `agy --print`（它会使用系统 Keychain 重新获取 token）。

账号 JSON 的可选字段：

```json
{
  "type": "antigravity",
  "email": "you@example.com",
  "home": "~/.clipool/profiles/agy_main",
  "enabled": true,
  "expiry": "2026-06-27T10:00:00Z",
  "priority": 0,
  "weight": 1
}
```

---

## 热重载账号

在服务运行期间新增或修改账号文件后，无需重启：

```bash
curl -X POST http://127.0.0.1:8318/v0/management/reload
```

查看当前账号池状态：

```bash
curl http://127.0.0.1:8318/v0/management/accounts
curl http://127.0.0.1:8318/v0/management/accounts/antigravity
```
