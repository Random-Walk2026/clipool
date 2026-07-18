# clipool

[English](./README.md) | **中文**

本地 OpenAI-compatible 代理包，把 **Claude / Codex / Grok / Copilot CLI**
和 **Antigravity profile** 变成标准 HTTP API。

> 📖 新用户建议从 [手把手教学](./docs/tutorial.md) 开始；想了解内部实现看 [技术架构](./docs/architecture.md)。

> **这不是 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)（Go 版）的移植或复刻。** 仅在「对外暴露成 OpenAI-compatible HTTP 接口」这一形态上与它对齐（方便互换、沿用其 model string / 管理 API 习惯），底层走的是一条**完全不同的实现路线**：
>
> - **CLIProxyAPI（Go）**：拿到 OAuth token 后**直连上游 HTTP**（`cloudcode-pa` / `api.anthropic.com` 等）。
> - **clipool（本包）**：主要驱动本机订阅 CLI 二进制（`claude -p` / `codex exec` / `grok` / `copilot` / `agy --print`），用每个账号独立的 `home` / `profiles/` 目录做 HOME 隔离轮换。Antigravity 的 `/v1/messages` 另有一条基于 profile token 的 Cloud Code Assist HTTP 快路径，失败才回退 `agy --print`。
>
> 因此两者的**认证目录与文件格式并不通用**：本包默认 `~/.clipool/`（下划线，账号文件带 `home`/`profiles` 指针），Go 版默认 `~/.cli-proxy-api/`（连字符，文件直接存 `access_token`）。**不要把两个目录合并或共用**——文件格式不同会互相污染账号池、token 各自刷新还会打架。

---

## 目录

- [快速上手](#快速上手)
- [Claude Code 接入 Antigravity](#claude-code-接入-antigravity)
- [账号配置目录 ~/.clipool/](#账号配置目录-clipool)
- [每个 backend 的账号文件格式](#每个-backend-的账号文件格式)
- [模型字符串格式](#模型字符串格式)
- [三种调用方式](#三种调用方式)
- [管理 API](#管理-api)
- [账号状态面板](#账号状态面板)
- [环境变量参考](#环境变量参考)
- [Antigravity 特殊说明](#antigravity-特殊说明)

---

## 快速上手

```bash
# 1. 安装本包（开发模式）
git clone https://github.com/Random-Walk2026/clipool.git && cd clipool
python -m pip install -e .

# 2. 配置账号（见下文）

# 3. 启动服务
python -m clipool              # 默认 127.0.0.1:8318（8317 留给 Go 版 CLIProxyAPI 常驻服务，避免撞车）
python -m clipool --port 8319  # 指定端口（也可用 CLIPOOL_PORT 环境变量改默认值）

# 4. 调用
curl http://127.0.0.1:8318/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-value" \
  -d '{"model":"claude/sonnet@high","messages":[{"role":"user","content":"你好"}]}'
```

默认只监听 loopback。使用 `--host 0.0.0.0` 或其他非 loopback 地址前必须设置
`CLIPOOL_API_KEY`，否则服务会拒绝启动。

---

## 目录结构说明

本仓库使用 flat layout，Python 包直接放在仓库根：

```text
clipool/                # GitHub 仓库根目录
├── clipool/            # 真正的 Python 包；import clipool 读这里
│   ├── providers/
│   ├── server.py
│   └── __main__.py
├── ui/                 # 账号状态面板 / Streamlit 管理台（随 wheel 分发）
├── docs/
├── tests/
└── pyproject.toml
```

Python 包名、CLI 脚本入口、分发名统一为 `clipool`。

如果运行：

```bash
python -m clipool
```

出现：

```text
No module named clipool
```

说明当前 Python 环境还没有安装本包。先在当前环境执行：

```bash
python -m pip install -e /path/to/clipool
```

再验证：

```bash
python -m clipool --help
clipool --help  # 等价的 CLI 脚本入口
```

---

## Claude Code 接入 Antigravity

`clipool` 现在同时提供 Anthropic-compatible `/v1/messages`，Claude Code 可以把本地代理当成 Anthropic API 使用，实际请求会走你的 Antigravity profile 账号池。

```bash
# 1. 启动本地代理
python -m clipool --port 8318

# 2. 让 Claude Code 指向本地代理
export ANTHROPIC_BASE_URL="http://127.0.0.1:8318"
export ANTHROPIC_AUTH_TOKEN="local-any-value"

# 3. 正常启动 Claude Code
claude --model claude-sonnet-4-6
```

如果想给本地代理加一层简单鉴权：

```bash
export CLIPOOL_API_KEY="your-local-secret"
python -m clipool --port 8318

export ANTHROPIC_BASE_URL="http://127.0.0.1:8318"
export ANTHROPIC_AUTH_TOKEN="your-local-secret"
claude --model claude-sonnet-4-6
```

当前实现是 MVP：Claude Code 的 Anthropic SSE 格式已支持，Antigravity 账号池会轮换；优先尝试直连 Cloud Code Assist，token 过期或直连不可用时会退回同 profile 的 `agy --print`。上游返回仍先完整收齐再包装成 SSE，不是逐 token 真流式。复杂 tool_use 的精确回传和 thinking signature 透传还可以继续增强。

完整接入指南见 [docs/claude_code_guide.md](./docs/claude_code_guide.md)。

---

## 账号配置目录 `~/.clipool/`

这是本包**专属**的认证文件目录（下划线命名，刻意区别于 Go 版的连字符 `~/.cli-proxy-api/`）。账号文件除 token 外还带 `home`/`profiles` 指针，供 CLI 子进程做 HOME 隔离——**与 Go 版的目录/格式不通用，别共用同一个目录**：

```
~/.clipool/           # 默认目录（可通过 CLIPOOL_AUTH_DIR 覆盖）
├── claude_work.json         # 一个 Claude 账号
├── claude_personal.json     # 另一个 Claude 账号（自动轮换）
├── codex_main.json          # Codex 账号
├── grok_account1.json       # Grok 账号
├── antigravity_google.json  # Antigravity（Google Cloud Code Assist）账号
└── copilot_pat.json         # GitHub Copilot 账号
```

**文件名规则**：随意命名，只要以 `.json` 结尾即可。`type` 字段决定属于哪个 backend。

**多账号**：同一 backend 放多个文件 → 自动 round-robin 轮换，失败账号自动冷却并切换下一个。

**按 backend 校验认证能力**：已识别 backend 的有效 JSON 必须提供该 CLI 真正会
使用的认证形态：

| backend | 至少需要 |
|---|---|
| `codex` | 隔离 `home`，或 `env.CODEX_HOME` |
| `antigravity` | 受管隔离 `home`，或 `env.HOME` |
| `grok` / `copilot` | 可注入 token |
| `claude` | 可注入 token，或隔离 `home` / `env.CLAUDE_CONFIG_DIR` |

未知 backend 会跳过。已知 backend 若缺少上述认证，或 token 仍是 `YOUR_...`、
`XXXXX` 等占位符，不再从池中消失，而会以 `configuration_error` 禁用项保留。
这样该 backend 仍被视为“已有账号”，请求会明确失败，不会静默回落到进程默认
登录态或 `.env` 兜底账号。面板不能直接启用配置错误；必须先修正/删除账号 JSON，
再 reload。

**权限建议**：

```bash
chmod 600 ~/.clipool/*.json   # 保护 token 文件
```

---

## 每个 backend 的账号文件格式

### Claude（claude_*.json）

Claude Code CLI 可使用 OAuth token，也可使用独立 `CLAUDE_CONFIG_DIR` profile。
token 可来自 `claude login` 后的凭据或 `CLAUDE_CODE_OAUTH_TOKEN`；目录方式可在
账号 JSON 写 `home`，或显式写 `env.CLAUDE_CONFIG_DIR`。

```json
{
    "type": "claude",
    "email": "you@example.com",
    "access_token": "YOUR_CLAUDE_TOKEN",
    "refresh_token": "rt-XXXXX...",
    "expired": "2025-12-31T00:00:00Z"
}
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `type` | ✅ | 固定为 `"claude"` |
| `email` | 可选 | 账号标识，仅用于显示 |
| `access_token` | 条件必填 | OAuth access token（`sk-ant-...`）；与 `home` / `CLAUDE_CONFIG_DIR` 至少一种 |
| `refresh_token` | 可选 | 刷新用（当前版本不自动刷新）|
| `expired` | 可选 | 过期时间，仅记录用 |

目录方式示例：

```json
{
    "type": "claude",
    "email": "work-profile",
    "home": "~/.clipool/profiles/claude_work"
}
```

> **最简版本**（单账号时可直接在 `.env` 里写，无需创建文件）：
> ```
> CLAUDE_CODE_OAUTH_TOKEN=YOUR_CLAUDE_TOKEN
> ```

---

### Codex（codex_*.json）

Codex 依赖整个 `CODEX_HOME` 登录态目录，不支持只在账号 JSON 填一个
`access_token` 来切号。先用 `CODEX_HOME` 完成登录：

```bash
mkdir -p ~/.clipool/profiles/codex_personal
CODEX_HOME="$HOME/.clipool/profiles/codex_personal" codex login
```

再注册这个 profile：

```json
{
    "type": "codex",
    "email": "you@example.com",
    "home": "~/.clipool/profiles/codex_personal",
    "enabled": true
}
```

`clipool` 会读取该目录的 `models_cache.json`，只把明确支持目标模型的账号
加入本次调度。机器上已登录的默认 `~/.codex` 也可直接作为 `home` 注册。
执行时默认使用临时 HOME/CWD、clean env、read-only sandbox、ephemeral 会话并
忽略用户配置/规则，但仍须读取所选 `CODEX_HOME` 的认证状态，也不能阻止 Codex
读取其 sandbox 允许的本地内容。这不是多租户或机密性沙箱，只适合可信本地调用；
即使配置 API key，也不要接收不可信用户的 prompt。详见下文环境变量说明。

---

### Grok（grok_*.json）

xAI Grok CLI 的认证。来源：`grok login` 后的登录态。

```json
{
    "type": "grok",
    "email": "you@example.com",
    "access_token": "xai-XXXXX..."
}
```

---

### Antigravity（antigravity_*.json）

> ⚠️ **Antigravity ≠ Gemini API**
>
> Antigravity 是 **Google Cloud Code Assist**（前身为 Cloud Code AI），底层走 `cloudcode-pa.googleapis.com`，使用 Google OAuth2 认证。和普通 `GEMINI_API_KEY` 完全不同——它是 Google 账号的订阅额度，不是 API key 计费。

推荐给每个 Antigravity 账号准备一个独立 profile home，并在账号 JSON 里填写
`home`。`clipool` 会先安全校验该 profile 的 `antigravity-oauth-token`，校验通过后
才读取并尝试 HTTP 快路径；上游调用失败可退回同 profile 的 `agy --print`，但
token 缺失、损坏或路径逃逸时会直接 fail closed，不会以 CLI 绕过校验。

```json
{
    "type": "antigravity",
    "email": "you@google.com",
    "home": "~/.clipool/profiles/agy_main",
    "enabled": true
}
```

profile 内的 token 文件路径通常是：

```text
~/.clipool/profiles/agy_main/.gemini/antigravity-cli/antigravity-oauth-token
```

> ⚠️ `home` 必须指向一个**已登录且安全的** agy profile。`.gemini` 与
> `antigravity-cli` 必须是留在 profile 内的真实目录，不能是 symlink；token 必须是
> 留在该目录内的单链接普通文件、JSON 中含非空 `access_token`。校验时目录权限会
> 收紧为 `0700`、token 为 `0600`。缺失、损坏、硬/软链接或路径逃逸都会在读取
> HTTP token、启动 `agy` 之前报错——不会进入交互式登录或弹浏览器。给新 profile
> 登录：`HOME=~/.clipool/profiles/agy_x agy`（按提示完成一次 OAuth）。

macOS 的 `agy` fallback 只会使用隔离 profile 自己的随机专用 keychain，并在任何
准备错误时中止，因此不会弹出无法正确输入的钥匙串密码框。详细安全边界见下文
[Antigravity 特殊说明](#antigravity-特殊说明)。

---

### Copilot（copilot_*.json）

GitHub Copilot 的 Personal Access Token（PAT）或 fine-grained PAT。

```json
{
    "type": "copilot",
    "email": "you@github.com",
    "token": "ghp_XXXXX..."
}
```

> 也可直接在 `.env` 里写：
> ```
> COPILOT_GITHUB_TOKEN=ghp_XXXXX...
> ```

---

## 模型字符串格式

支持多种格式，对齐 CLIProxyAPI 的 model string 规范：

### 显式 provider 前缀（推荐）

```
claude                      → Claude CLI，默认模型，无思考强度
claude/sonnet               → Claude CLI，sonnet 模型
claude/sonnet@high          → Claude CLI，sonnet 模型，high effort
claude@high                 → Claude CLI，默认模型，high effort
codex/gpt-5.5               → Codex CLI，gpt-5.5 模型
grok/grok-4                 → Grok CLI，grok-4 模型
antigravity/gemini-3.5-flash → Antigravity CLI
copilot/gpt-4.1@medium      → Copilot CLI，medium effort
```

### 括号 effort 格式（兼容 CLIProxyAPI 风格）

```
claude/sonnet(high)         → 等价于 claude/sonnet@high
gpt-5.5(high)               → 自动推断为 codex/gpt-5.5@high
grok-4(medium)              → 自动推断为 grok/grok-4@medium
gemini-3.5-flash(high)      → 自动推断为 antigravity/gemini-3.5-flash@high
```

### 模型名自动推断 backend

| 模型名前缀 | 推断为 |
|-----------|--------|
| `claude-*` | `claude` |
| `gpt-*`, `o1`, `o3`, `o4*` | `codex` |
| `grok-*` | `grok` |
| `gemini-*` | `antigravity` |

> 💡 **模型名容错**：`grok` / `copilot` 的可用模型随订阅变化（例如 grok CLI 实际只有 `grok-build`、`grok-composer-2.5-fast`）。当请求的模型名 CLI 不认（`unknown model id` / `not available`）时，`clipool` 会**自动回退到该 CLI 的默认模型**重试一次，保证工作流不因模型名不符而中断。

---

### Antigravity 的 effort → 模型变体

Antigravity CLI（`agy`）没有独立的 `--effort` 参数，思考强度通过**模型名变体**表达：

| 基础模型 | effort | 最终 --model 参数 |
|---------|--------|-----------------|
| `gemini-3.5-flash` | `high` | `gemini-3.5-flash-high` |
| `gemini-3.5-flash` | `low` | `gemini-3.5-flash-low` |
| `gemini-3.1-pro` | `low` | `gemini-3.1-pro-low` |
| `claude-sonnet-4-6` | `high` | `claude-sonnet-4-6-thinking` |

直接写变体名时原样传入，不再叠加：`antigravity/gemini-3.5-flash-high` → `--model gemini-3.5-flash-high`。

---

## 三种调用方式

### 方式 A：进程内直接调用（无 HTTP 开销，适合同进程内嵌）

```python
from clipool import run_with_pool

reply = run_with_pool("claude", "你好", model="sonnet", effort="high")
print(reply)
```

`run_with_pool` 与 HTTP 服务共用同一套账号池语义（轮换 / 冷却 / 永久禁用），
**无需启动服务**，也不需要 fastapi/requests 等重依赖。只想单跑一次、绕过账号池时
用底层的 `clipool.runner.run_cli`。

> 📌 外部 Python 项目检测到本包已安装后即可直接委托 `run_with_pool`，
> 免起 HTTP 服务就获得同一套多账号轮换 / 冷却 / 永久禁用语义。

### 方式 B：HTTP 客户端（先 `python -m clipool` 启动服务）

```python
from clipool import get_client

client = get_client()   # 默认 http://127.0.0.1:8318/v1
resp = client.chat.completions.create(
    model="claude/sonnet@high",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,        # 支持流式
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

### 方式 C：LangChain 接入（先启动服务）

```python
from clipool import get_langchain_model

llm = get_langchain_model("claude/sonnet")
result = llm.invoke("你好")
print(result.content)
```

---

## 管理 API

服务运行时可通过以下端点管理账号池（对齐 CLIProxyAPI `/v0/management/*`）：

```bash
# 查看所有账号状态
curl http://127.0.0.1:8318/v0/management/accounts

# 查看指定 backend 的账号
curl http://127.0.0.1:8318/v0/management/accounts/claude

# 添加账号文件后重新加载（无需重启服务）
curl -X POST http://127.0.0.1:8318/v0/management/reload

# 启用 / 禁用 / 重置 / 刷新额度（action ∈ enable | disable | reset | refresh_quota）
curl -X POST http://127.0.0.1:8318/v0/management/accounts/action \
  -H "Content-Type: application/json" \
  -d '{"backend":"claude","id":"work@example.com","action":"disable"}'

# 刷新所有账号的额度（5 小时 / 周；目前支持 codex / claude / antigravity）
curl -X POST http://127.0.0.1:8318/v0/management/quota/refresh

# 查看 backend 默认能力 + 从 Codex profile 缓存发现的模型
curl http://127.0.0.1:8318/v1/models

# 健康检查
curl http://127.0.0.1:8318/health
```

热重载会先在锁外读取完整新快照，再一次性切换请求世代；旧世代 Account
对象随后返回的成功或失败不会改变新世代。池内 `_revision` 保护请求/管理世代；
本进程每次成功原子写入账号 token、expiry、quota 或启用/禁用状态，还会推进全局
`persist_revision`。若写入发生在 reload 读取期间，reload 会丢弃旧快照并重读；
若写入发生在平时，下一次账号列表、状态、查找或选号前会自动换入磁盘新快照。

自动同步/重载时，backend、id 与凭据世代都相同的账号会继承内存中的 cooldown 和
`error_count`，已有半开 probe lease 也继续占用，避免一次 quota 写回意外清空退避或
重复探测。token、refresh token、home、认证 profile 文件或注入 env 改变时视为新
凭据世代，不合并这些运行态并释放旧 lease。无论是否合并，reload 前发出的请求仍
持有旧 Account 对象，其迟到成功/失败结果一律忽略。

若设置了 `CLIPOOL_API_KEY`，以上所有 `/v0/management/*` 和 `/v1/models` 请求都要加
`-H "Authorization: Bearer $CLIPOOL_API_KEY"`（或 `x-api-key`）。
只有 `/health` 与面板外壳保持公开，方便先打开页面再输入 key。

账号状态返回示例：

```json
{
  "accounts": {
    "claude": [
      {"backend": "claude", "id": "work@example.com", "status": "available", "available": true, "cooling": false, "cooling_seconds": 0, "error_count": 0, "quota": null}
    ],
    "codex": [
      {"backend": "codex", "id": "hk@example.com", "status": "available", "available": true, "error_count": 0,
       "quota": {"plan_type": "plus",
                 "five_hour": {"used_percent": 4, "reset_at": 1782556912, "window_minutes": 300},
                 "weekly": {"used_percent": 33, "reset_at": 1783084966, "window_minutes": 10080}},
       "quota_error": ""}
    ]
  }
}
```

---

## 账号状态面板

两种可视化界面，按需选用：

**① 内嵌 HTML 面板（零依赖，随服务自带）**

服务启动后直接访问 <http://127.0.0.1:8318/>（或 `/dashboard`）。卡片式展示各 backend 的账号、状态徽章（🟢 可用 / 🟡 冷却中 / 🔴 已禁用）、脱敏令牌、优先级/权重、令牌到期倒计时、冷却剩余、失败次数、禁用原因，5 秒自动刷新。无需安装任何额外依赖。

点「刷新额度」可拉取各账号的 **5 小时 / 周额度**（进度条 + 重置倒计时 + 套餐类型）。额度查询较慢且有限流，因此不随状态自动刷新，按需手动触发。

目前支持 **codex**、**claude** 和 **antigravity**：

| backend | 数据来源 | 说明 |
| --- | --- | --- |
| codex | `chatgpt.com/backend-api/wham/usage` | 每次用 `refresh_token` 刷新后查询；primary=5h、secondary=周 |
| claude | `api.anthropic.com/api/oauth/usage`（需 `anthropic-beta` + `User-Agent`） | 先用现有 token 查，401 才刷新（刷新端点限流严，避免空刷） |
| antigravity | 本地 `agy` 语言服务 `RetrieveUserQuotaSummary`（127.0.0.1，Connect 协议） | 反映「当前本地登录的 agy 账号」，按 email 匹配挂到对应账号；分 Gemini / Claude&GPT 两组各 5h+周 |

其余 backend（grok / copilot）暂无可用 usage 入口，不显示额度。说明：

- antigravity 需要本机正在运行 `agy`（它没有公开 HTTP usage 端点，额度只能从本地服务取，这也正是 `agy` TUI 里 `/usage` 的数据来源）。本地服务只有一个登录态，因此只会给 email 匹配的那个账号显示额度，其余账号提示去 `agy` 切换账号。
- Claude 的 `User-Agent` 版本号可用 `CLIPOOL_CLAUDE_CODE_VERSION` 覆盖。

**② Streamlit 管理台（带操作按钮）**

需要表格筛选 + 一键启用/禁用/重置/reload 时使用：

```bash
python -m pip install -e ".[ui]"            # 安装 streamlit
python -m streamlit run ui/streamlit_app.py
```

默认连接 <http://127.0.0.1:8318>，可用 `CLIPOOL_URL` 环境变量或左侧栏修改；若服务设了 `CLIPOOL_API_KEY`，在左侧栏填入即可。操作按钮通过 `/v0/management/accounts/action` 端点生效并落盘，含每账号「额度」按钮与左侧栏「刷新额度」批量按钮，额度以进度条展示 5 小时 / 周用量。

---

## 环境变量参考

所有变量都可以写在项目根目录的 `.env` 里，优先级高于账号文件中的默认值。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CLIPOOL_AUTH_DIR` | `~/.clipool` | 认证文件目录 |
| `CLIPOOL_PORT` | `8318` | 服务默认端口（8317 留给 Go 版 CLIProxyAPI）|
| `AGENT_LLM_CLI_TIMEOUT` | `600` | CLI 调用超时（秒）；Unix 会终止完整进程组，Windows 仅尽力终止直接子进程 |
| `CLAUDE_CLI_BIN` | `claude` | Claude CLI 可执行文件路径 |
| `CODEX_CLI_BIN` | `codex` | Codex CLI 可执行文件路径 |
| `GROK_CLI_BIN` | `grok` | Grok CLI 可执行文件路径 |
| `ANTIGRAVITY_CLI_BIN` | `~/.local/bin/agy` 或 `agy` | Antigravity CLI 路径 |
| `COPILOT_CLI_BIN` | `copilot` | Copilot CLI 可执行文件路径 |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | Claude 单账号兜底 token（无文件时用）|
| `COPILOT_GITHUB_TOKEN` | — | Copilot 单账号兜底 token |
| `CLIPOOL_API_KEY` | — | 保护生成接口、`/v1/models` 与全部 `/v0/management/*`；支持 Bearer token 或 `x-api-key` |
| `CLIPOOL_CODEX_UNSAFE` | — | 设为 `1`（也支持 `true/yes/on`）时关闭 Codex 默认的临时 HOME/CWD、clean env、read-only、ephemeral 与忽略配置/规则限制；仅限完全可信的本地调用 |
| `ANTIGRAVITY_PROJECT_ID` | 自动探测 | Cloud Code Assist project，自动探测失败时可手动指定 |
| `ANTIGRAVITY_CLOUDCODE_ENDPOINT` | 多端点 fallback | 覆盖 Cloud Code Assist v1internal endpoint |
| `ANTIGRAVITY_OAUTH_CLIENT_ID` | — | Antigravity token 过期后自动刷新所需 OAuth client id |
| `ANTIGRAVITY_OAUTH_CLIENT_SECRET` | — | Antigravity token 过期后自动刷新所需 OAuth client secret |

---

## Antigravity 特殊说明

在 macOS 上，`agy` 可能通过 Keychain 保存 token 副本。隔离 profile 并不知道
真实登录钥匙串密码，因此密码弹窗没有可用的正确输入。clipool 会在启动 CLI
fallback 前为每个受管虚拟 profile 准备一个随机名称的专用 keychain，并生成
随机机器密码；用户无需、也不应输入这个密码。路径和密码只写入 profile 根目录的
`.clipool-keychain.json`（`0600`）。

- 只管理 `CLIPOOL_AUTH_DIR/profiles/` 下的 profile，或包含精确标记文件
  `.clipool-managed-profile` 的外部专用 profile；
- 专用 keychain 使用 `clipool-<随机值>.keychain-db`，虚拟 HOME 的 default/search
  list 只指向它；已有 `login.keychain-db` 保持原样，也不会加入搜索列表；
- profile、`Library/Keychains` 与 `Library/Preferences` 固定为 `0700`；这些目录、
  状态文件或专用 keychain 出现符号链接等不安全路径时一律 fail closed；
- 专用 keychain 无法解锁时会连同状态文件可恢复地改名归档后换随机名称重建；
  状态文件自身损坏时也会先归档，不覆盖原始证据；
- 真实用户 HOME、未标记的外部目录和缺少 HOME 的调用一律拒绝；
- 任一 macOS `security` 命令失败都会 fail closed：不启动 `agy`，因此不弹窗；
- `antigravity-oauth-token` 仍是登录态来源；HTTP 读取和 CLI fallback 都共用同一
  安全校验：`.gemini/antigravity-cli` 目录链不能含 symlink 或越出 profile，token
  必须是单链接普通文件、JSON 含非空 `access_token`，并收紧为 `0600`；
- token 缺失、损坏、硬/软链接或路径逃逸都会 fail closed，不接受 HTTP 凭据、
  也不启动 `agy`。只有 token 已校验通过后的上游 HTTP 失败才允许 CLI fallback。

如果确实要把专用 profile 放到默认目录以外，可显式标记：

```bash
printf 'clipool isolated profile v1\n' > /path/to/dedicated-agy-profile/.clipool-managed-profile
```

不要给真实 HOME 创建这个标记。`.clipool-keychain.json` 含机器密码，应与账号
token 一样视为机密；被归档的状态文件或专用 keychain 会保留在原目录，待人工检查。

---

## 冷却机制

账号失败时自动进入冷却，冷却期间跳过并切换下一个账号。冷却时长按**连续失败次数指数放大**（成功一次即归零）：

| 错误类型 | 首次 | 连续失败 | 上限 |
|---------|------|---------|------|
| 配额耗尽 / 限速(429、quota exceeded)| 60 秒 | ×2 递增(60→120→240…) | 1 小时 |
| 瞬时错误(超时、5xx、连接失败)| 15 秒 | ×2 递增 | 5 分钟 |
| `invalid_grant` | 永久禁用并落盘 | 600 秒后至多一个租约探测 | — |
| 其它认证失效(HTTP 401、token 撤销、人工禁用)| 永久禁用并落盘 | 不自动探测，需人工修复/启用 | — |
| `configuration_error` | 配置禁用项 | 修 JSON 后 reload，不能直接启用 | — |

配额与瞬时错误的冷却结束后自动恢复参与轮换，无需手动干预。

> ⚠️ **池中有账号但全部冷却/禁用时直接报错**，绝不回落到进程默认登录态——那等于
> 静默偷用另一个账号的额度；Antigravity 更会在无受管 HOME 时直接拒绝启动 `agy`。
> Claude、Codex、Grok、Copilot 只有在该 backend **一个账号都没有**时才尝试默认
> 登录态（单账号模式）；Antigravity 始终要求注册受管隔离 profile。

---

## 与 CLIProxyAPI（Go 版）的关系

**同形不同源**：两者都对外长成 OpenAI-compatible 代理；Go 版以直连上游
API 为主，本包以本地 CLI 与隔离 profile 编排为主。唯一的窄例外是
Antigravity Messages HTTP 快路径。它们各有适用场景，不是谁移植谁。

| 维度 | CLIProxyAPI（Go）| clipool（本包，Python）|
|------|-----------------|-------------------|
| **后端实现** | OAuth token 直连上游 HTTP | **本地 CLI 为主**；Antigravity Messages 有 HTTP 快路径与 CLI fallback |
| **多账号隔离** | token 池 | **`home`/`profiles/` 做 HOME 隔离**（每账号独立 CLI 登录态）|
| 认证目录 | `~/.cli-proxy-api/`（连字符）| `~/.clipool/`（下划线，**格式不通用、不可共用**）|
| 适用场景 | 已有可直连的 OAuth token、追求高吞吐 | 需要本机 CLI/profile 登录态、多账号隔离与轮换 |
| OAuth 自动刷新 | ✅ | Antigravity 直连分支支持；CLI 分支交给 CLI 自身续期 |
| 真·流式 | ✅ | 模拟（CLI 同步后包装 SSE）|
| 管理 API / 冷却轮换 | ✅ `/v0/management/*` | ✅（沿用其 `/v0/management/*` 习惯）|
| Docker | ✅ | `python -m clipool` |
