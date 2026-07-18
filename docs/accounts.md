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

## 加载与认证能力校验

账号文件是 JSON object，`type` 必须是已知 backend。未知 backend、损坏 JSON 或
非 object JSON 会跳过；已知 backend 则按实际调用方式验证：

| backend | 可用认证配置 |
|---|---|
| `codex` | 隔离 `home`，或 `env.CODEX_HOME` |
| `antigravity` | 受管隔离 `home`，或 `env.HOME` |
| `grok` / `copilot` | `token` / `access_token` 等可注入 token，或对应 token env |
| `claude` | 可注入 token，或隔离 `home` / `env.CLAUDE_CONFIG_DIR` |

有效 JSON 若缺少对应认证，或只留下 `YOUR_...`、`XXXXX` 等占位 token，会保留成
带 `configuration_error: ...` 的禁用账号，而不是被过滤掉。这个禁用项会占住该
backend，使执行层明确报告账号池不可用，阻断进程默认登录态和 `.env` 兜底账号。
管理 API/面板的“启用”不能绕过配置校验：先修正或删除 JSON，再显式 reload。

这与普通人工禁用不同：`configuration_error` 不是可恢复运行态，也不会参加半开探测。

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

`clipool` 先校验该 token 的路径、文件类型、权限和内容，再发起 HTTP 请求；只有
token 已通过校验后的上游失败才回退到同 profile 的 `agy --print`。token 本身缺失、
损坏或逃逸时 fail closed，CLI 不能绕过校验。

### OAuth token 文件安全边界

HTTP 快路径读取 token 和 CLI fallback 启动 `agy` 前都会调用同一验证：

- `.gemini` 与 `.gemini/antigravity-cli` 必须是 profile 内的真实目录，祖先链中
  不能出现 symlink 或解析到 profile 外；目录权限收紧为 `0700`；
- `antigravity-oauth-token` 不能是 symlink，必须是链接数为 1 的普通文件，并且
  resolve 后仍位于 `antigravity-cli` 目录内；文件权限收紧为 `0600`；
- 文件必须是有效 JSON，并在顶层或 `token` object 中含非空 `access_token`；
- 缺失、损坏、硬链接、软链接或路径逃逸全部 fail closed：不接受凭据，也不启动
  `agy`，从而不会转入交互式 OAuth。

### macOS 虚拟 profile 钥匙串

`agy` 的 CLI fallback 会访问 macOS Keychain。为避免隔离 profile 弹出一个
实际无法正确输入的密码框，`clipool` 会为每个受管虚拟 profile 创建随机名称的
专用 keychain（`Library/Keychains/clipool-<随机值>.keychain-db`）和随机机器密码。
专用 keychain 已解锁且不自动上锁，用户无需、也不应输入它的密码；相对路径与密码
只保存在 profile 根目录的 `.clipool-keychain.json`，权限固定为 `0600`。

安全边界如下：

- `CLIPOOL_AUTH_DIR/profiles/` 下的 profile 自动视为受管目录；
- 自定义到其他位置的专用 profile，必须自行创建精确标记文件
  `.clipool-managed-profile`；
- profile、`Library/Keychains` 和 `Library/Preferences` 权限固定为 `0700`；
  任一目录、状态文件或专用 keychain 是符号链接等不安全路径时立即拒绝；
- 虚拟 HOME 的 default keychain 和 search list 只指向随机专用 keychain；原有
  `login.keychain-db` 不会使用、修改、归档或加入搜索列表；
- 专用 keychain 无法解锁时，其文件和状态会改名为
  `*.clipool-backup-<时间戳>` 后用新随机名称重建；状态本身损坏时也先归档；
- 真实用户 HOME、未标记的外部目录或未提供 HOME 一律拒绝；
- 任一 `security` 命令失败都会中止本次 `agy` fallback，不启动可能弹窗的 CLI。

如果你把专用 profile 放在默认目录之外，可显式确认归属：

```bash
printf 'clipool isolated profile v1\n' > /path/to/dedicated-agy-profile/.clipool-managed-profile
```

只应给专门为 clipool 创建的隔离目录加此标记，不要给真实 HOME 加标记。
`.clipool-keychain.json` 含机器密码，应与 token 一样限制访问、禁止提交到版本库。

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

`clipool` 会读取每个 `CODEX_HOME/models_cache.json` 中 `visibility=list` 的模型，
只把请求送给明确支持目标模型的账号。不兼容账号会被跳过，不会调用 CLI、进入冷却，
也不会污染失败率。缓存缺失时为兼容旧安装仍允许试跑；可用 `codex` 登录或正常执行
一次命令刷新该 profile 的模型缓存。

如果机器默认 `~/.codex` 已经是可用的 Pro 登录，也可直接注册这个现有 profile，
无需复制 `auth.json` 或任何 token：

```json
{
  "type": "codex",
  "email": "codex-pro-default",
  "home": "~/.codex",
  "enabled": true
}
```

通过 HTTP 调用时，Codex 默认使用权限为 `0700` 的临时 HOME 与工作目录、clean
env、read-only sandbox、ephemeral 会话，并忽略用户配置/规则。它仍须读取所选
`CODEX_HOME` 中的认证状态，也可能读取 Codex sandbox 允许的其它本地内容，因此
这只是可信本地服务的纵深收敛，**不是多租户或机密性沙箱**。只有完全信任调用方时
才可设置 `CLIPOOL_CODEX_UNSAFE=1` 关闭这些默认限制。

---

## Claude / Grok / Copilot 账号

这三个后端都可直接在账号 JSON 里写 token，也可在完全没有该 backend 账号文件时
使用环境变量兜底。Grok 与 Copilot 必须有可注入 token；Claude 还允许以独立
`CLAUDE_CONFIG_DIR` profile 作为认证能力。

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

Claude profile 示例（`home` 会作为 `CLAUDE_CONFIG_DIR` 注入）：

```json
{ "type": "claude", "email": "work-profile", "home": "~/.clipool/profiles/claude_work" }
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
| `invalid_grant` | **永久禁用**，写回 JSON；约 10 分钟后可领取一次恢复探测 |
| 一般 HTTP 401、token 撤销、人工禁用 | **永久禁用**，写回 JSON；只接受人工修复/启用 |
| `configuration_error` | **配置禁用项**；修 JSON 后 reload，不能直接启用 |

> 池中有账号但全部冷却/禁用时**直接报错**，不会回落到进程默认登录态；
> Claude、Codex、Grok、Copilot 只有在该 backend 一个账号都没有时才尝试默认登录态。
> Antigravity 还是更严格的例外：即使没有账号文件也不使用进程默认 HOME，必须先
> 注册受管隔离 profile。缺少认证或只有占位 token 的已知 backend JSON 会以
> `configuration_error` 禁用项留在池内，专门阻断默认登录态回落。

永久禁用后，JSON 文件会自动写入：

```json
{
  "enabled": false,
  "disabled_reason": "invalid_grant: Token has been expired or revoked.",
  "disabled_at": 1751000000.0
}
```

**半开自动恢复**：只有因 `invalid_grant` 自动禁用的账号在满约 10 分钟后，
且当前能力子集没有其它可用账号时，才会获得一个受租约保护的试探请求；同一
backend/id 不会并发探测。请求成功后自动解除禁用并清掉 JSON 里的
`disabled_reason`。一般 HTTP 401、token 撤销和人工禁用不会自动复活。

普通人工/认证禁用可在修复凭据后手动启用。`configuration_error` 不能从面板直接
启用；必须先补齐 JSON 中该 backend 真正使用的 token/profile（或删除该文件），
再调用热重载接口。

---

## 直连 HTTP Token 刷新（Antigravity）

对于 Antigravity 直连 HTTP 路径，`clipool` 会在 token 过期前 **300 秒**主动刷新，刷新后把新的过期时间写回账号 JSON 的 `expiry` 字段。

自动刷新需要设置 OAuth client 环境变量：

```bash
export ANTIGRAVITY_OAUTH_CLIENT_ID="..."
export ANTIGRAVITY_OAUTH_CLIENT_SECRET="..."
```

未设置时，token 过期后自动回退到 `agy --print`。在 macOS 上，该 fallback
只使用上文所述的虚拟 profile keychain；准备失败就中止，不会弹钥匙串密码框。

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

重载先在锁外构建完整快照，再原子切换请求世代；加载期间旧快照继续服务，不会出现
空池。旧世代 Account 对象随后完成的成功/失败结果会被忽略，不能禁用或复活新世代
同名账号。池内 `_revision` 跟踪请求/管理世代；本进程内任一成功原子写入还会推进
全局 `persist_revision`，包括 token、expiry、quota 和启用/禁用状态。reload 读取
期间若任一世代改变便丢弃旧读取并重试；平时直接写回后，下一次账号列表、状态、
查找或选号会检测该持久化世代并自动同步磁盘快照。它不是跨进程文件监视器，外部
进程手改文件后仍应显式调用 reload。

重载新旧账号的 backend/id 与 `_credential_generation` 相同（source path、home、
token/refresh token、注入 env 及 Codex/Antigravity profile 凭据文件身份未变）时，
会把旧对象尚未落盘的 cooldown 与 `error_count` 合并到新对象，并保留正在占用的
probe lease。凭据文件、token、home 或认证 env 改变则视为新凭据，不继承运行态，
旧 lease 也会释放。两种情况下请求世代边界都不变：reload 前拿到的旧 Account
即使稍后完成，其成功/失败结果也不会写到新对象。

查看当前账号池状态：

```bash
curl http://127.0.0.1:8318/v0/management/accounts
curl http://127.0.0.1:8318/v0/management/accounts/antigravity
```
