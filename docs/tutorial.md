# clipool 手把手教学

> 从零开始,把你手里的 **Claude / Codex / Grok / Antigravity / Copilot** 订阅 CLI
> 变成一个标准的 OpenAI / Anthropic 兼容 HTTP API,并让多个账号自动轮换。
>
> 读完本文你将学会:安装 → 配置账号池 → 启动服务 → 接入任意客户端 → 日常运维。
> 想了解内部实现原理,请看 [技术架构文档](./architecture.md)。

---

## 目录

1. [30 秒理解它在做什么](#1-30-秒理解它在做什么)
2. [前置条件](#2-前置条件)
3. [安装](#3-安装)
4. [第一次启动:零配置试跑](#4-第一次启动零配置试跑)
5. [搭建账号池(核心)](#5-搭建账号池核心)
6. [模型字符串速查](#6-模型字符串速查)
7. [接入你的工具](#7-接入你的工具)
8. [日常运维:面板、管理 API、额度](#8-日常运维面板管理-api额度)
9. [进程内直接调用(Python 库用法)](#9-进程内直接调用python-库用法)
10. [常见问题排查(FAQ)](#10-常见问题排查faq)

---

## 1. 30 秒理解它在做什么

你订阅了 Claude Pro / ChatGPT Plus / Copilot 等服务,拿到的是各家的**命令行工具**
(`claude`、`codex`、`grok`、`agy`、`copilot`)。它们只能在终端里交互使用,
没法被你的程序、Claude Code、LangChain 这些"只认 HTTP API"的工具直接调用。

clipool 做的事:

```
你的程序 / Claude Code / curl / LangChain ...
        │  标准 OpenAI 或 Anthropic HTTP 请求
        ▼
┌──────────────────────────────┐
│  clipool (127.0.0.1:8318)    │
│  1. 解析模型串,路由到对应 CLI  │
│  2. 从账号池选一个可用账号     │
│  3. 注入该账号的环境变量       │──► spawn 子进程: claude -p / codex exec / agy --print ...
│  4. 失败自动冷却、换号重试     │
└──────────────────────────────┘
```

两个关键词:

- **驱动真实 CLI**:不逆向上游 API,而是把你本机已登录的订阅 CLI 当后端。
  CLI 官方怎么变,它就怎么跟。
- **多账号轮换**:同一服务的多个账号(例如两个 Claude 订阅)自动 round-robin,
  某个账号额度耗尽自动冷却、切换下一个,全部恢复后自动回来。

> ⚠️ 请只用它管理**你本人拥有**的账号。多账号聚合请自行确认符合各服务的使用条款。

---

## 2. 前置条件

- **Python ≥ 3.10**
- 至少一个**已安装并登录**的订阅 CLI。每个后端对应的 CLI:

| 后端 | CLI 命令 | 安装/登录方式 |
|------|---------|--------------|
| claude | `claude` | [Claude Code](https://claude.com/claude-code),`claude` 首次运行按提示登录 |
| codex | `codex` | OpenAI Codex CLI,`codex login` |
| grok | `grok` | xAI Grok CLI,`grok login` |
| antigravity | `agy` | Google Antigravity CLI,首次运行按提示完成 Google OAuth |
| copilot | `copilot` | GitHub Copilot CLI,用 PAT 认证 |

只配你有的就行——**不需要五个全装**,没配置的后端调用时才会报错。

验证 CLI 可用(以 claude 为例):

```bash
claude -p "说 ok"      # 能输出内容 = CLI 就绪
```

---

## 3. 安装

```bash
git clone https://github.com/Random-Walk2026/clipool.git && cd clipool
python -m pip install -e .
```

验证:

```bash
python -m clipool --help    # 或等价的: clipool --help
```

可选附加依赖:

```bash
pip install -e ".[client]"     # openai SDK(用 get_client 快捷方式时需要)
pip install -e ".[langchain]"  # LangChain 接入
pip install -e ".[ui]"         # Streamlit 管理台
pip install -e ".[dev]"        # pytest 等开发依赖
```

---

## 4. 第一次启动:零配置试跑

**还没配置任何账号也能跑**——某个后端一个账号文件都没有时,clipool 直接用
你本机 CLI 当前的默认登录态(称为"单账号模式")。

```bash
# 启动服务(默认 127.0.0.1:8318)
python -m clipool
```

看到类似输出即成功:

```text
clipool API  →  http://127.0.0.1:8318/v1
账号状态面板   →  http://127.0.0.1:8318/
支持后端:claude / codex / grok / antigravity / copilot
```

另开一个终端,发第一个请求(假设你装了 claude CLI):

```bash
curl http://127.0.0.1:8318/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-value" \
  -d '{"model":"claude/sonnet","messages":[{"role":"user","content":"你好"}]}'
```

> 💡 CLI 是同步执行的,一次调用要跑完整个子进程,首次响应几秒到几十秒都正常。
> 💡 没设置 `CLIPOOL_API_KEY` 时,`Authorization` 随便填什么都行(见 [第 8 节](#鉴权可选))。

浏览器打开 <http://127.0.0.1:8318/> 能看到账号状态面板。

---

## 5. 搭建账号池(核心)

单账号模式只是起点。clipool 的核心价值在**多账号轮换**:同一后端放多个账号,
自动轮换 + 失败冷却 + 换号重试。

### 5.1 账号目录结构

所有账号文件放在 `~/.clipool/`(可用 `CLIPOOL_AUTH_DIR` 环境变量改):

```text
~/.clipool/
├── claude_work.json          # 一个 Claude 账号
├── claude_personal.json      # 另一个 Claude 账号 → 自动轮换
├── codex_main.json
├── antigravity_google.json
└── profiles/                 # 目录型后端的独立登录态(见 5.3)
    ├── agy_main/
    └── codex_personal/
```

规则:

- **文件名随意**,`.json` 结尾即可;文件里的 `"type"` 字段决定属于哪个后端。
- **同一后端多个文件 = 自动轮换**。
- token 还是占位符(`YOUR_...`、`XXXXX`)的文件**不会入池**,不用担心没填完的模板捣乱。
- 建议 `chmod 600 ~/.clipool/*.json` 保护令牌。

账号按**类型**分两种配置方式,别搞混:

| 类型 | 后端 | 原理 |
|------|------|------|
| **token 型** | claude / grok / copilot | JSON 里直接放 access token,调 CLI 时注入对应环境变量 |
| **目录型** | codex / antigravity | 每个账号一个独立的登录态目录,调 CLI 时把 `HOME` / `CODEX_HOME` 指过去 |

### 5.2 token 型后端:claude / grok / copilot

以 Claude 为例。先拿 token:`claude` 登录后,token 在 `~/.claude/` 的凭据文件里
(或从 `claude setup-token` 获取长期 token)。然后创建:

```bash
cat > ~/.clipool/claude_work.json <<'EOF'
{
    "type": "claude",
    "email": "work@example.com",
    "access_token": "sk-ant-oat01-..."
}
EOF
```

grok / copilot 同理:

```json
{"type": "grok", "email": "you@example.com", "access_token": "xai-..."}
```

```json
{"type": "copilot", "email": "you@github.com", "token": "ghp_..."}
```

> 💡 **只有一个账号时可以不建文件**,直接在项目根目录 `.env` 里写
> `CLAUDE_CODE_OAUTH_TOKEN=...` 或 `COPILOT_GITHUB_TOKEN=...` 即可。

### 5.3 目录型后端:antigravity / codex(HOME 隔离)

这类 CLI 的登录态是**一整个目录**,不是单个 token。clipool 的做法:给每个账号
准备一个独立目录,调 CLI 时把 `HOME`(agy)或 `CODEX_HOME`(codex)指过去——
子进程看到不同目录,就等于切换了账号,互不干扰、线程安全。

**Antigravity 完整流程**(每个 Google 账号做一遍):

```bash
# 1. 建独立 profile 目录
mkdir -p ~/.clipool/profiles/agy_main

# 2. 用这个目录当 HOME 登录一次(会走 Google OAuth,浏览器授权)
HOME="$HOME/.clipool/profiles/agy_main" agy -p "ping"

# 3. 确认登录态文件已生成
ls ~/.clipool/profiles/agy_main/.gemini/antigravity-cli/antigravity-oauth-token

# 4. 注册账号
cat > ~/.clipool/antigravity_main.json <<'EOF'
{
    "type": "antigravity",
    "email": "you@google.com",
    "home": "~/.clipool/profiles/agy_main"
}
EOF
```

> ⚠️ `home` 必须指向**已登录**的 profile(第 3 步的 token 文件存在)。
> 否则 clipool 会直接报「账号未登录」并跳过该账号——它**绝不会**让 `agy`
> 进入交互式登录、也不会弹浏览器,避免误刷别的账号。

**Codex** 同理,用 `CODEX_HOME`:

```bash
mkdir -p ~/.clipool/profiles/codex_personal
CODEX_HOME="$HOME/.clipool/profiles/codex_personal" codex login
```

```json
{"type": "codex", "email": "you@example.com", "home": "~/.clipool/profiles/codex_personal"}
```

### 5.4 进阶字段:主备与加权

每个账号 JSON 还支持:

```json
{
    "type": "claude",
    "email": "main@example.com",
    "access_token": "sk-ant-...",
    "priority": 0,
    "weight": 2,
    "enabled": true,
    "env": {"SOME_EXTRA_VAR": "value"}
}
```

| 字段 | 默认 | 含义 |
|------|------|------|
| `priority` | 0 | **主备分组**:数字小的先用;主号组全部冷却/禁用才溢出到备号组 |
| `weight` | 1 | **组内加权**:同 priority 组里按权重分配流量(2 = 拿双倍请求) |
| `enabled` | true | 手动开关 |
| `env` | — | 调该账号时额外注入的环境变量 |

### 5.5 让改动生效

账号文件是**懒加载 + 缓存**的。新增/修改文件后,二选一:

```bash
curl -X POST http://127.0.0.1:8318/v0/management/reload   # 免重启
# 或直接重启 python -m clipool
```

配好后刷新面板 <http://127.0.0.1:8318/>,应能看到所有账号绿灯 🟢。

更完整的账号文件字段说明见 [accounts.md](./accounts.md)。

---

## 6. 模型字符串速查

请求里的 `model` 字段格式:**`<后端>/<模型>@<思考强度>`**,后两段都可省略。

```text
claude                       → Claude CLI 默认模型
claude/sonnet@high           → Claude CLI,sonnet,high effort
codex/gpt-5.5                → Codex CLI
grok/grok-4                  → Grok CLI
antigravity/gemini-3.5-flash → Antigravity CLI
copilot/gpt-4.1@medium       → Copilot CLI
```

没写后端前缀时按模型名自动推断:`claude-*`→claude、`gpt-*`/`o1`/`o3`/`o4*`→codex、
`grok-*`→grok、`gemini-*`→antigravity。也兼容 CLIProxyAPI 的括号风格
`claude/sonnet(high)`。完整规则见 [README_CN](../README_CN.md#模型字符串格式)。

`GET /v1/models` 可以列出当前可用的模型串。

---

## 7. 接入你的工具

服务地址就是一个标准 API 端点:**`http://127.0.0.1:8318/v1`**。
任何允许自定义 `base_url` 的 OpenAI/Anthropic 客户端都能直接用。

### 7.1 OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8318/v1", api_key="any-value")
resp = client.chat.completions.create(
    model="claude/sonnet@high",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

流式(注意:目前是 CLI 跑完后包装成 SSE 的"模拟流式",接口兼容但不是逐 token):

```python
for chunk in client.chat.completions.create(model="claude/sonnet",
        messages=[{"role": "user", "content": "你好"}], stream=True):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

装了 `pip install -e ".[client]"` 的话有个快捷方式:

```python
from clipool import get_client
client = get_client()   # 等价于上面的 OpenAI(base_url=..., api_key=...)
```

### 7.2 Node.js(openai 包)

```javascript
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://127.0.0.1:8318/v1", apiKey: "any-value" });
const resp = await client.chat.completions.create({
  model: "codex/gpt-5.5",
  messages: [{ role: "user", content: "你好" }],
});
console.log(resp.choices[0].message.content);
```

### 7.3 LangChain

```python
from clipool import get_langchain_model   # 需要 pip install -e ".[langchain]"

llm = get_langchain_model("claude/sonnet")
print(llm.invoke("你好").content)
```

或者不依赖本包的快捷方式,直接用 `ChatOpenAI(base_url=..., api_key="any-value")`。

### 7.4 Claude Code(用 Anthropic 兼容接口)

clipool 同时提供 `/v1/messages`(Anthropic Messages API 兼容),
Claude Code 可以把它当成 Anthropic API,实际请求走你的 Antigravity 账号池:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8318"
export ANTHROPIC_AUTH_TOKEN="local-any-value"
claude --model claude-sonnet-4-6
```

完整指南(含鉴权、故障排查)见 [claude_code_guide.md](./claude_code_guide.md)。

### 7.5 其他工具

任何支持「自定义 OpenAI base URL」的工具(各类聊天客户端、IDE 插件、
Agent 框架)都是同一个配法:

- **API 地址**:`http://127.0.0.1:8318/v1`
- **API Key**:任意值(设了 `CLIPOOL_API_KEY` 则填它)
- **模型名**:第 6 节的模型串,如 `claude/sonnet@high`

---

## 8. 日常运维:面板、管理 API、额度

### 账号状态面板

服务自带零依赖 HTML 面板:<http://127.0.0.1:8318/>。
展示每个账号的状态徽章(🟢 可用 / 🟡 冷却中 / 🔴 已禁用)、脱敏令牌、
到期倒计时、失败次数、禁用原因,5 秒自动刷新。

「刷新额度」按钮可以拉取 **5 小时窗 / 周窗** 用量进度条(支持 codex / claude /
antigravity;额度接口有限流,所以不自动刷新、按需手动点)。

需要批量操作按钮(启用/禁用/重置)的话用 Streamlit 管理台:

```bash
pip install -e ".[ui]"
python -m streamlit run ui/streamlit_app.py
```

### 管理 API

```bash
curl http://127.0.0.1:8318/v0/management/accounts          # 全部账号状态
curl -X POST http://127.0.0.1:8318/v0/management/reload    # 重新加载账号文件
curl -X POST http://127.0.0.1:8318/v0/management/accounts/action \
  -H "Content-Type: application/json" \
  -d '{"backend":"claude","id":"work@example.com","action":"disable"}'
# action ∈ enable | disable | reset | refresh_quota
curl -X POST http://127.0.0.1:8318/v0/management/quota/refresh   # 刷新所有额度
curl http://127.0.0.1:8318/health                                # 健康检查
```

### 冷却机制(自动,通常无需干预)

| 失败类型 | 处理 |
|---------|------|
| 额度耗尽 / 429 限速 | 冷却 60s 起,连续失败指数翻倍,封顶 1 小时 |
| 超时 / 5xx / 连接错误 | 冷却 15s 起,指数翻倍,封顶 5 分钟 |
| 认证失效(invalid_grant / 401) | **永久禁用并写回文件**(重启不再死磕);600s 后允许放行一次探测,外部修好 token 就自动复活 |

成功一次即清零计数。**池里有账号但全部冷却/禁用时会直接报错**,
绝不静默回落到 CLI 默认登录态(防止偷用别的账号额度)。

### 鉴权(可选)

本地自用可以不设。要暴露给局域网/加一道锁:

```bash
export CLIPOOL_API_KEY="your-local-secret"
python -m clipool
```

之后所有请求需带 `Authorization: Bearer your-local-secret`(或 `x-api-key` 头)。

---

## 9. 进程内直接调用(Python 库用法)

如果你的调用方本身就是 Python,可以跳过 HTTP,直接 import(**免起服务**,
共用同一套账号池语义):

```python
from clipool import run_with_pool

reply = run_with_pool("claude", "你好", model="sonnet", effort="high")
```

适合把 clipool 当依赖库嵌进 Agent 管线的场景。只想裸跑一次、绕过账号池,
用更底层的 `clipool.runner.run_cli`。

---

## 10. 常见问题排查(FAQ)

**Q:`No module named clipool`**
当前 Python 环境没装本包。确认用同一个解释器执行了 `pip install -e .`
(多环境时注意 conda/venv 是否切对)。

**Q:为什么默认端口是 8318,不是 8317?**
8317 是 Go 版 CLIProxyAPI 的习惯端口,留给它避免撞车。两者可以并行跑。

**Q:报「账号池全部不可用(N 个冷却中/已禁用)」**
这是**设计行为**:池里有账号但全在冷却/禁用时直接报错,不回落默认登录态。
看面板确认哪些账号冷却、多久恢复;要立即恢复某账号,用管理 API 的 `reset`。

**Q:antigravity 账号报「未登录」/ 担心弹浏览器**
`home` 指向的 profile 里没有 `antigravity-oauth-token` 文件。clipool 只会跳过
该账号、**不会**触发交互式登录。按 [5.3 节](#53-目录型后端antigravity--codexhome-隔离)
重新登录该 profile 即可。

**Q:请求的模型名 CLI 不认(unknown model id)**
grok / copilot 的可用模型随订阅变化。clipool 检测到这类错误会**自动回退到该 CLI
的默认模型重试一次**,请求不会失败,但返回的可能不是你指定的模型。

**Q:流式输出为什么是一大块一起到的?**
当前是"模拟流式":CLI 同步跑完后把完整结果包装成 SSE。接口协议兼容
`stream=True`,但不是逐 token。真流式在路线图上。

**Q:响应很慢**
每次请求 spawn 一个 CLI 子进程,冷启动 + 模型思考都算在内。超时默认 600s,
可用 `AGENT_LLM_CLI_TIMEOUT` 调整。追求低延迟/高吞吐,考虑 Go 版 CLIProxyAPI
的直连路线(见 README 对比表)。

**Q:能和 Go 版 CLIProxyAPI 共用 `~/.cli-proxy-api/` 目录吗?**
**不能。** 两者账号文件格式不同,共用会互相污染。clipool 用自己的 `~/.clipool/`。

**Q:CLI 装在非标准路径?**
`.env` 或环境变量指定:`CLAUDE_CLI_BIN` / `CODEX_CLI_BIN` / `GROK_CLI_BIN` /
`ANTIGRAVITY_CLI_BIN` / `COPILOT_CLI_BIN`。完整环境变量表见
[README_CN](../README_CN.md#环境变量参考)。
