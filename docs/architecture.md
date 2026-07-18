# clipool 技术架构

> 面向想读懂源码、排查问题或贡献代码的人。使用教学请看 [tutorial.md](./tutorial.md)。

---

## 目录

1. [定位与设计原则](#1-定位与设计原则)
2. [总览](#2-总览)
3. [模块地图](#3-模块地图)
4. [一次请求的生命周期](#4-一次请求的生命周期)
5. [账号池调度(pool.py)](#5-账号池调度poolpy)
6. [账号模型与 HOME 隔离(account.py)](#6-账号模型与-home-隔离accountpy)
7. [Provider 层(providers/)](#7-provider-层providers)
8. [Antigravity 双路径](#8-antigravity-双路径)
9. [Anthropic 兼容层(anthropic.py)](#9-anthropic-兼容层anthropicpy)
10. [并发模型](#10-并发模型)
11. [额度子系统(quota.py)](#11-额度子系统quotapy)
12. [配置系统(config.py)](#12-配置系统configpy)
13. [测试](#13-测试)
14. [已知限制与路线图](#14-已知限制与路线图)

---

## 1. 定位与设计原则

clipool 把**本机已登录的订阅 CLI 二进制**当作 LLM 后端来编排,对外暴露成
OpenAI / Anthropic 兼容的 HTTP API。四条贯穿全部代码的原则:

1. **CLI-first。** Claude、Codex、Grok、Copilot 与 Antigravity 的常规路径
   都 spawn 本机 CLI 子进程。Antigravity `/v1/messages` 是唯一例外：先尝试
   profile token 驱动的 Cloud Code Assist HTTP 快路径，失败自动退回 CLI
   （见第 8 节）。
2. **HOME 隔离实现多账号。** 登录态是目录的 CLI(agy/codex),按账号给独立目录,
   子进程 env 里把 `HOME`/`CODEX_HOME` 指过去即完成"切号"。不改全局 `os.environ`,
   天然线程安全。
3. **绝不静默回落。** 池中有账号但全部冷却/禁用 → 直接报错,绝不偷用进程默认
   登录态(那会静默消耗另一个账号的额度；Antigravity 还会拒绝无受管 HOME 的调用)。
   Claude、Codex、Grok、Copilot 只有池中**零账号**时才尝试默认登录态；
   Antigravity 始终要求受管隔离 profile，绝不使用进程默认 HOME。
4. **对外习惯对齐 CLIProxyAPI(Go)。** model string 格式、`/v0/management/*`
   管理端点都沿用其约定,方便两者互换部署;但实现路线相反、认证目录格式不通用。

---

## 2. 总览

```
                     HTTP 客户端                        Python 进程内
        (OpenAI SDK / Claude Code / curl ...)          (import clipool)
                          │                                   │
┌─────────────────────────▼───────────────────────┐           │
│ server.py (FastAPI)                              │           │
│  /v1/chat/completions   OpenAI 兼容              │           │
│  /v1/messages           Anthropic 兼容           │           │
│  /v1/models             backend + Codex 缓存能力 │           │
│  /v0/management/*       管理 API                 │           │
│  /  /dashboard          HTML 状态面板            │           │
│        └── asyncio → ThreadPoolExecutor(8) ──┐   │           │
└──────────────────────────────────────────────┼───┘           │
                                               ▼               ▼
                              ┌────────────────────────────────────┐
                              │ executor.py                        │
                              │  execute_with_pool / run_with_pool │ ← 轮换+重试的唯一实现
                              └──────┬──────────────────┬──────────┘
                                     ▼                  ▼
                          ┌──────────────────┐  ┌─────────────────────────┐
                          │ pool.py          │  │ providers/              │
                          │  AccountPool     │  │  base.py  BaseProvider  │
                          │  pick/mark_*     │  │  claude/codex/grok/     │
                          └────────┬─────────┘  │  antigravity/copilot    │
                                   ▼            └───────────┬─────────────┘
                          ┌──────────────────┐              ▼
                          │ account.py       │      subprocess.run(CLI 二进制,
                          │  Account 模型     │        env=账号注入的环境变量)
                          │  ~/.clipool/ 加载 │
                          └──────────────────┘
```

依赖方向严格单向:`server → executor → {pool, providers} → account → config`。
无循环依赖;`executor` 及以下不依赖 FastAPI,可脱离 HTTP 独立使用。

---

## 3. 模块地图

| 模块 | 职责 | 关键导出 |
|------|------|---------|
| `server.py` | FastAPI 路由:OpenAI/Anthropic 兼容端点、管理 API、面板;async→线程池桥接 | `app` |
| `executor.py` | **轮换+重试的唯一同步实现**;HTTP 与进程内共用 | `execute_with_pool`, `run_with_pool` |
| `pool.py` | 线程安全账号池:主备选号、加权轮换、错误分类、指数冷却、半开探测 | `AccountPool`, `get_pool()` |
| `account.py` | `Account` 数据类、`~/.clipool/` 加载/写回、`env_override()` 环境注入 | `Account`, `load_accounts` |
| `router.py` | model string → `(provider, model, effort)` 解析与后端推断 | `parse_model`, `is_cli_model` |
| `providers/base.py` | Provider 抽象基类 + 子进程执行的公共实现 | `BaseProvider` |
| `providers/<name>.py` | 各 CLI 的命令构造与输出提取 | `get_provider`, `SUPPORTED` |
| `providers/antigravity_http.py` | Antigravity 直连 Cloud Code Assist 的快路径(可选) | `AntigravityHTTPProvider` |
| `anthropic.py` | Anthropic Messages 请求/响应 schema、SSE 生成 | `AnthropicMessagesRequest` 等 |
| `quota.py` | codex/claude/antigravity 三种额度查询 + token 刷新 | `refresh_quota` |
| `config.py` | `.env` 加载、CLI 二进制路径、超时、默认端口 | `CLI_TIMEOUT`, `DEFAULT_PORT` |
| `version.py` | 从已安装 package metadata 读取运行时版本 | `__version__` |
| `runner.py` | 向后兼容薄壳:绕过账号池的单次 `run_cli` | `run_cli` |
| `ui/`(仓库根) | 零依赖 HTML 面板 + Streamlit 管理台；作为 `ui` 包随 wheel 分发 | — |

---

## 4. 一次请求的生命周期

以 `POST /v1/chat/completions`、`model="claude/sonnet@high"` 为例:

1. **解析**:`router.parse_model()` → `("claude", "sonnet", "high")`。
   无前缀时按模型名推断后端(`gpt-*`→codex 等);`@high` 与 `(high)` 两种 effort 写法等价。
2. **摊平消息**:多轮 `messages` 拼成单段纯文本 prompt(system 段前置)。
   CLI 是单发式接口,不保留服务端会话。
3. **投递线程池**:async 端点把同步调用丢进 `ThreadPoolExecutor(8)`,不阻塞事件循环。
4. **轮换执行**(`executor.execute_with_pool`):
   ```
   accounts = pool.accounts("claude")
   若为空 → fn(None)                    # 支持的后端尝试默认登录态
   否则循环(每个账号至多试一次):
       account = pool.pick("claude")     # 主备 + 加权选号
       env     = account.env_override()  # token / HOME 注入
       result  = provider.run(text, model, effort, env_override=env)
       成功 → pool.mark_success(account) → 返回
       失败(RuntimeError) → pool.mark_failed(account, exc) → 换下一个
   耗尽 → 抛 RuntimeError(带各账号状态摘要)
   ```
   Antigravity provider 会拒绝上面 `fn(None)` 的无 HOME 调用，因此它必须先有受管
   profile 账号；已知 backend 的 `configuration_error` 禁用项则使 accounts 非空，
   统一阻断所有 backend 的意外默认登录态回落。
5. **子进程执行**(`BaseProvider.run`):`_build_cmd()` 构造如
   `claude -p <text> --model sonnet ...`,`_run_subprocess()` 跑它(细节见第 7 节),
   `_extract_output()` 提取纯文本。
6. **响应包装**:非流式 → 标准 OpenAI JSON;`stream=True` → 完整结果切成 SSE
   `data:` 帧回放(**模拟流式**,协议兼容但非逐 token)。

`/v1/messages`(Anthropic)流程相同,仅进出口的 schema/SSE 事件序列不同,
且 antigravity 后端会先试直连快路径(第 8 节)。

---

## 5. 账号池调度(pool.py)

### 选号:`pick(backend)`

两级策略,在持锁状态下完成:

1. **主备分组(priority)**:可用账号里取 `priority` 最小的组;整组不可用才
   溢出到下一组——备号平时完全不消耗。
2. **组内加权轮换(weight)**:游标 `_index` 单调递增,对组内权重总和取模,
   落在哪个账号的权重区间就选谁。`weight=2` 的账号拿到两倍请求。
   带能力 predicate 时，游标按 `(backend, eligible account ids)` 隔离，Codex
   不同模型对应的账号子集不会互相推进游标或造成饥饿。

### 错误分类:`mark_failed(account, exc)`

优先读取异常的结构化 `failure_kind` / `status_code`，旧 provider 才回退到
错误字符串匹配，分三档处理：

| 类别 | 关键词(节选) | 处理 |
|------|--------------|------|
| 可探测认证失效 | `invalid_grant` | **永久禁用 + `persist()`；600s 后可领取一次 lease** |
| 其它认证失效 | HTTP 401、`token expired`、`revoked`、人工禁用 | **永久禁用；只接受人工修复/启用** |
| 额度/限速 | `quota` `429` `rate limit` `exceeded` | 冷却 `60 × 2^n` 秒,封顶 3600 |
| 瞬时错误 | `timeout` `5xx` `connection` | 冷却 `15 × 2^n` 秒,封顶 300 |

`n` 为连续失败次数(`_backoff()` 中 `2^min(n,6)`),成功一次即归零。
额度窗口动辄 5 小时,指数退避避免耗尽的账号每分钟白烧一次子进程冷启动。

### 复活:半开探测

只有 `invalid_grant` 自动禁用的账号在 600 秒(`RECOVERY_PROBE_AFTER`)后、
当前能力子集无可用账号时，才会领取一次半开探测 lease。同一 backend/id
并发只能有一个 probe；成功后 `mark_success` 解除禁用并落盘，失败则释放
lease。人工禁用和一般 auth error 永不自动复活。

### 单例

`get_pool()` 双检锁单例,server 与进程内调用共享同一个池(同一进程内
冷却状态一致)。账号懒加载；`reload()` 先在锁外构建完整新快照，再持锁一次
替换，加载期间请求继续使用旧快照，不会短暂看见空池并回落默认账号。reload
同时是请求世代边界：旧 Account 对象之后返回的成功/失败不再作用于同名的新对象。
池内 `_revision` 跟踪 reload 与管理操作的请求世代；`account.py` 另有进程全局
`persist_revision`，每次原子 JSON 写入成功都会推进。reload 在读前后同时校验这
两个世代，任一改变就丢弃旧读取并重试。`accounts/status/find/pick` 以及管理入口在
访问池前还会比较已加载的 disk revision：token、expiry、quota 等直接写回后，
下一次读/选号自动 reload，从而不会长期使用旧 Account 快照。

reload 换对象前按 `(backend, id, _credential_generation)` 合并纯运行态。凭据世代
相同就继承 `_cooling_until` 与 `_error_count`，并保留旧 probe lease，避免 quota/
expiry 写回触发的自动 reload 清空退避或发出重复半开探测。source path、home、
token/refresh token、extra env，或 Codex `auth.json` / Antigravity token 文件身份
任一改变就不合并，并丢弃对应 lease。即使凭据世代相同，旧 Account 请求的迟到
完成结果仍因对象身份不匹配而忽略；运行态合并不等于允许旧请求修改新世代。

---

## 6. 账号模型与 HOME 隔离(account.py)

`Account` 是 dataclass,关键字段:

```python
backend: str        # 所属后端
id: str             # email 或文件名 stem
token: str          # token 型后端的访问令牌
home: str           # 目录型后端的独立登录态目录
priority / weight   # 调度参数(见第 5 节)
disabled_reason/at  # 永久禁用状态(会写回 JSON,重启保留)
quota / quota_error # 额度快照(面板展示用)
supported_models    # Codex profile 缓存发现的模型集合
source_path         # 来源 JSON 路径,persist() 写回用
```

**多账号切换的核心是 `env_override()`**——按后端类型返回一小撮环境变量:

| 类型 | 后端 | 注入 |
|------|------|------|
| 目录型 | antigravity | `HOME=<home>` |
| 目录型 | codex | `CODEX_HOME=<home>` |
| token / 目录型 | claude | `CLAUDE_CODE_OAUTH_TOKEN=<token>` 或 `CLAUDE_CONFIG_DIR=<home>` |
| token 型 | copilot / grok | 对应 token 变量 |

这组 env 通过 `subprocess.Popen(env=...)` 传给子进程,**不碰全局 `os.environ`**,
因此任意并发度下各账号互不串号。账号 JSON 里的 `"env": {...}` 会叠加注入,
调用方 `extra_env` 优先级最高。

加载侧要点:文件按 `*.json` 扫描，未知 backend、损坏/非 object JSON 跳过。已知
backend 按真实认证能力校验：codex/antigravity 必须有对应隔离 home，grok/copilot
必须有 token，claude 必须有 token 或 `CLAUDE_CONFIG_DIR`。缺少认证或只有占位
token 的有效 JSON 保留为 `configuration_error` 禁用项，使 backend 非空并阻断
默认登录态/env 兜底；`set_enabled(..., True)` 会拒绝该项，必须修 JSON 后 reload。
完全没有该 backend 账号时才使用支持的 env 兜底；Antigravity provider 还会拒绝
无隔离 HOME 的默认模式。`persist(fields=...)` 只合并调用方声明的字段，
同一文件使用线程锁与 Unix advisory lock 串行化，再通过同目录临时文件、
`fsync + os.replace` 原子落盘并强制 `0600`；字段级补丁和 reload 世代检查共同
阻止旧 Account / 旧磁盘读取覆盖新 token 或新管理状态。每次成功原子替换都会推进
进程内 `persist_revision`，因此 token、expiry、quota 等不经 pool lock 的写回也能
在下一次池读取/选号前触发自动同步；跨进程外部修改仍需显式 reload。

---

## 7. Provider 层(providers/)

### 契约(base.py)

```python
class BaseProvider(ABC):
    name / label                       # 后端名 / 错误信息里的可读名
    run(text, model, effort, *, env_override) -> str   # 模板方法
    _build_cmd(text, model, effort) -> list[str]       # 子类必须实现
    _extract_output(proc, text) -> str                 # 子类可覆盖
```

三条保证:**run 线程安全**(env 走 subprocess 参数)、**错误统一抛
`RuntimeError`**(由 pool 分类)、**不修改全局状态**。
新增一个后端 = 写一个 `_build_cmd`(几十行),在 `providers/__init__.py` 注册。

### 子进程执行的两个坑(踩过才写进代码的)

`_run_subprocess()` 有两处非直觉设计:

1. **stdout/stderr 落临时文件,不用 `PIPE`。**
   agy 这类 CLI 会 spawn 孙进程(钥匙串查询、language server),它们继承管道
   且可能比 CLI 本体活得久。PIPE 模式要等**所有**写端持有者关闭才返回——
   实测被孤儿孙进程卡死过。文件模式下直接子进程退出即返回,孤儿进程无关紧要。
2. **新进程组 + 完整超时清理（Unix）。** Unix 上
   `start_new_session=True` 让每个 CLI 成为独立进程组；超时或清理时先向整个组
   发 `SIGTERM`，宽限后再发 `SIGKILL` 并 `wait()` 回收，避免 language server
   等孙进程遗留。Windows 没有这条进程组实现，只能尽力 terminate/kill 直接子进程。

### 模型名容错

grok / copilot 的可用模型随订阅漂移。CLI 报 `unknown model id` /
`not available` 时,provider 自动**用该 CLI 默认模型重试一次**,
保证上游工作流不因模型名过期而中断。

### Codex 默认安全边界

`CodexProvider` 默认把 HOME 和 CWD 指向权限为 `0700` 的临时目录，只从 allowlist
构建 clean env，并传入 `--sandbox read-only --ephemeral --ignore-user-config
--ignore-rules`。认证仍要求精确暴露所选账号的 `CODEX_HOME`；read-only 也不承诺
阻止读取 sandbox 允许的本地文件。因此这是可信本地服务的纵深收敛，**不是多租户
隔离或机密性沙箱**。只有显式设置 `CLIPOOL_CODEX_UNSAFE=1`（也接受
`true/yes/on`）才关闭这些默认限制；即使开启 API 鉴权，也不应把 prompt 入口交给
不可信租户。

---

## 8. Antigravity 双路径

antigravity 是唯一有两条执行路径的后端:

```
/v1/messages 请求(antigravity 账号)
    │
    ├─ 快路径: antigravity_http.py 直连 Cloud Code Assist
    │   · 从 profile 读 antigravity-oauth-token
    │   · 过期前 300s 主动刷新(需 ANTIGRAVITY_OAUTH_CLIENT_ID/SECRET),
    │     新 expiry 写回账号 JSON
    │   · 多个 v1internal endpoint 依次 fallback
    │
    └─ 慢路径(快路径不可用时): AntigravityProvider 跑 `agy --print`
        · HOME=<profile> 隔离,与其他后端一致
```

其余细节:

- **effort → 模型名变体**:agy 没有 `--effort` 参数,思考强度编码在模型名里
  (`gemini-3.5-flash` + high → `gemini-3.5-flash-high`;claude 系 + high →
  `-thinking` 后缀)。已是变体名则原样透传。
- **绝不交互登录**:profile 里没有 token 文件 → 直接报「账号未登录」跳过,
  不让 agy 弹浏览器 OAuth(否则会在错误的默认账号上完成登录)。
- **token 路径与内容 fail closed**:HTTP 读取与 CLI fallback 共用
  `validated_profile_token_file()`。`.gemini` 和 `antigravity-cli` 必须是 profile
  内的真实目录且无 symlink；token 必须是留在目标目录的单链接普通文件、有效 JSON
  且含非空 `access_token`。目录权限收紧为 `0700`、token 为 `0600`。缺失、损坏、
  硬/软链接或逃逸都会在 HTTP 凭据读取和 `agy` 启动前失败；CLI 不能绕过校验。
- **虚拟 keychain fail closed(macOS)**:CLI fallback 只为
  `CLIPOOL_AUTH_DIR/profiles/` 或带 `.clipool-managed-profile` 标记的专用
  profile 创建 `clipool-<随机值>.keychain-db` 和随机机器密码；路径与密码保存在
  `.clipool-keychain.json`（`0600`）。profile、`Library/Keychains`、
  `Library/Preferences` 均固定为 `0700`，default/search list 只指向该专用
  keychain。已有 `login.keychain-db` 保持原样且不参与搜索；损坏的专用 keychain
  或状态会可恢复归档后重建。真实 HOME、未标记外部目录、安全目录链中的 symlink、
  缺失 HOME 或任一 `security` 失败都会在启动 `agy` 前 fail closed。

---

## 9. Anthropic 兼容层(anthropic.py)

为 Claude Code 这类 Anthropic SDK 客户端提供 `/v1/messages`:

- `AnthropicMessagesRequest`(pydantic)接收原生请求,`messages_to_prompt()`
  把 system + 多轮 content blocks 摊平成单段 prompt;
- 响应按 Anthropic 格式返回,流式时生成完整的 SSE 事件序列
  (`message_start → content_block_delta → message_stop`);
- `/v1/messages/count_tokens` 提供估算值,满足客户端的预检调用。

当前为 MVP:文本进出完整可用;复杂 `tool_use` 精确回传、thinking signature
透传是路线图项(见第 14 节)。

---

## 10. 并发模型

- **CLI 本质是同步的**(一次跑完一个子进程),所以核心执行层是纯同步代码;
- server 端 `asyncio` 事件循环通过 `loop.run_in_executor` 把同步调用投进
  `ThreadPoolExecutor(max_workers=8)`——最多 8 个 CLI 子进程并行,事件循环
  始终不阻塞,管理 API/面板在重负载下依然响应;
- 共享状态只有 `AccountPool`(内部 `threading.Lock`)与各 `Account`
  (自带锁),Provider 无状态,因此线程池并发是安全的;
- 进程内调用方(`run_with_pool`)直接在自己的线程里同步执行,与 HTTP 路径
  共享同一个池单例。

---

## 11. 额度子系统(quota.py)

面板"刷新额度"背后,三个后端三条数据通路:

| 后端 | 来源 | 要点 |
|------|------|------|
| codex | `chatgpt.com/backend-api/wham/usage` | 每次先用 `refresh_token` 换新 token 再查;primary=5h 窗、secondary=周窗 |
| claude | `api.anthropic.com/api/oauth/usage` | 先用现有 token 查,401 才刷新(刷新端点限流严,避免空刷);需 `anthropic-beta` + 仿 Claude Code 的 `User-Agent` |
| antigravity | 本地 `agy` language server 的 `RetrieveUserQuotaSummary`(Connect 协议) | agy 无公开 usage HTTP 端点,只能从本地服务取;只反映当前本地登录的账号,按 email 匹配挂载 |

结果归一化为 `{"five_hour": {...}, "weekly": {...}, "plan_type": ...}` 存进
`Account.quota`。额度查询慢且有限流 → 不随状态自动刷新,只按需手动触发。

> 设计债:本模块是"按后端 if-else"的集合,token 刷新逻辑也混在其中。
> 计划把 `fetch_quota` / `refresh_token` 下放为 Provider 可选能力(见第 14 节)。

---

## 12. 配置系统(config.py)

- 启动时加载项目根 `.env`(不覆盖已存在的环境变量);
- 三类配置:CLI 二进制路径(`*_CLI_BIN`)、超时(`AGENT_LLM_CLI_TIMEOUT`,
  默认 600s)、服务参数(`CLIPOOL_PORT` 默认 8318、`CLIPOOL_AUTH_DIR` 默认
  `~/.clipool`、`CLIPOOL_API_KEY` 可选鉴权)、Codex 信任边界
  (`CLIPOOL_CODEX_UNSAFE`,默认关闭);
- 配置 `CLIPOOL_API_KEY` 后，生成接口、`/v1/models` 和全部
  `/v0/management/*` 都要求 Bearer token 或 `x-api-key`；只有面板外壳与
  `/health` 保持公开。绑定非 loopback 地址但没有 key 时，入口直接拒绝启动。
- 完整变量表见 [README_CN](../README_CN.md#环境变量参考)。

> 设计债:配置在 import 时读取,测试需要子进程隔离(`test_auth_dir_config.py`
> 就是这么做的)。计划改为惰性 `get_settings()`。

---

## 13. 测试

```bash
python -m pytest -q
python -m ruff check .
python -m mypy clipool
python -m build
```

| 文件 | 覆盖 |
|------|------|
| `test_router.py` | model string 全格式解析、后端推断 |
| `test_pool.py` | 选号、priority/weight、冷却退避、禁用/复活、账号文件解析 |
| `test_executor.py` | 轮换重试、全不可用报错、API key 鉴权、effort 传递 |
| `test_anthropic.py` | /v1/messages schema、prompt 摊平、antigravity HTTP mock |
| `test_dashboard.py` | 面板路由、管理 API |
| `test_quota.py` | 额度归一化 |
| `test_auth_dir_config.py` | AUTH_DIR 解析(env / .env / 默认),子进程隔离 |
| `test_keychain_safety.py` | 真实 HOME 拒绝、token 目录链/内容验证、专用 keychain 可恢复归档、fail closed |
| `test_persistence_safety.py` | 原子写入、字段级合并、并发更新与文件权限 |
| `test_security.py` | 管理 API 鉴权、非 loopback 启动保护、Codex 默认 sandbox |
| `test_subprocess_cleanup.py` | Unix CLI 超时后完整清理子进程组 |

原则:不真实 spawn 订阅 CLI、不发真实网络请求——provider/HTTP 层用 mock,
核心调度逻辑全部纯内存可测。

---

## 14. 已知限制与路线图

| 项 | 现状 | 方向 |
|----|------|------|
| 流式 | 模拟(CLI 跑完再包 SSE) | Provider 接口演进为可 yield 增量,接 `claude -p --output-format stream-json` 等实现真流式 |
| tool_use / thinking 透传 | MVP,文本为主 | 完整 Anthropic content blocks 回传 |
| quota/token 刷新集中在 quota.py | 按后端 if-else | 下放为 Provider 可选能力(`fetch_quota`/`refresh_token`) |
| Provider 注册 | 硬编码 5 个 | entry points 插件化,支持第三方 `clipool-xxx` 包扩展后端 |
| 配置 | import 时读取 | 惰性 `get_settings()` |
