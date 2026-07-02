# clipool

**English** | [中文](./README_CN.md)

Turn your **Claude / Codex / Grok / Antigravity / Copilot** subscription CLIs into a standard **OpenAI- & Anthropic-compatible HTTP API** — with automatic multi-account rotation.

```
OpenAI SDK / Claude Code / LangChain / curl ...
        │  standard /v1/chat/completions or /v1/messages
        ▼
┌─────────────────────────────┐
│   clipool  (localhost:8318) │      picks an account from your pool,
│   account pool · cooldown   │────► injects its credentials, then drives
│   rotation · retry          │      the real CLI:  claude -p · codex exec
└─────────────────────────────┘      agy --print · grok · copilot ...
```

## Why clipool

- **Universal adapter** — anything that speaks the OpenAI or Anthropic API can now use your subscription CLIs: OpenAI/Anthropic SDKs, Claude Code, LangChain, IDE plugins, plain `curl`.
- **Drives the real CLI binaries** — no reverse-engineered upstream APIs. If the official CLI works, clipool works.
- **Multi-account pool** — put several accounts of the same service in `~/.clipool/`; requests rotate across them (priority groups + weighted round-robin). Exhausted accounts cool down with exponential backoff and rejoin automatically; revoked tokens are disabled persistently with a self-healing probe.
- **HOME-isolated account switching** — for CLIs whose login state is a directory (Antigravity, Codex), each account gets its own profile directory injected via subprocess env (`HOME` / `CODEX_HOME`). No global state, thread-safe.
- **No silent fallback** — if a pool has accounts but all are cooling/disabled, requests fail loudly instead of silently burning your default login's quota.
- **Built-in dashboard** — zero-dependency account status page at `/`, plus a `/v0/management/*` API and an optional Streamlit console; quota bars (5-hour / weekly) for codex, claude and antigravity.

> **Not a port of [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (Go).** It aligns with its model-string and management-API conventions so the two are interchangeable at the HTTP surface, but takes the opposite implementation route: CLIProxyAPI calls upstream HTTP APIs directly with OAuth tokens; clipool orchestrates your local subscription CLI binaries with HOME-isolated multi-account rotation — something the direct-API route cannot do. Their auth directories and file formats are **not** compatible (`~/.clipool/` vs `~/.cli-proxy-api/`); never share one directory between them.

## Quick start

```bash
git clone https://github.com/Random-Walk2026/clipool.git && cd clipool
python -m pip install -e .

python -m clipool          # serves http://127.0.0.1:8318/v1  (8317 is left for CLIProxyAPI)
```

With zero configuration, each backend uses your machine's current CLI login (single-account mode). Then call it like any OpenAI endpoint:

```bash
curl http://127.0.0.1:8318/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-value" \
  -d '{"model":"claude/sonnet@high","messages":[{"role":"user","content":"hello"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8318/v1", api_key="any-value")
resp = client.chat.completions.create(
    model="claude/sonnet@high",
    messages=[{"role": "user", "content": "hello"}],
)
print(resp.choices[0].message.content)
```

## Model strings

`<backend>/<model>@<effort>` — later parts optional. Backend is inferred from the model name when the prefix is omitted (`gpt-*` → codex, `claude-*` → claude, `gemini-*` → antigravity, `grok-*` → grok). CLIProxyAPI's `model(effort)` style is also accepted.

```
claude/sonnet@high            codex/gpt-5.5           grok/grok-4
antigravity/gemini-3.5-flash  copilot/gpt-4.1@medium  gpt-5.5(high)
```

`GET /v1/models` lists what's currently available.

## Use it from Claude Code

clipool also serves the Anthropic Messages API (`/v1/messages`), so Claude Code can point at it and run on your Antigravity account pool:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8318"
export ANTHROPIC_AUTH_TOKEN="local-any-value"
claude --model claude-sonnet-4-6
```

## Account pool in 60 seconds

Drop one JSON file per account into `~/.clipool/` (any filename; the `type` field selects the backend). Multiple files of the same backend rotate automatically.

```jsonc
// token-style backends: claude / grok / copilot
{ "type": "claude", "email": "work@example.com", "access_token": "sk-ant-..." }

// directory-style backends: antigravity / codex — one login profile per account
{ "type": "antigravity", "email": "you@google.com", "home": "~/.clipool/profiles/agy_main" }
```

Directory-style accounts are logged in once via an isolated HOME:

```bash
mkdir -p ~/.clipool/profiles/agy_main
HOME="$HOME/.clipool/profiles/agy_main" agy -p "ping"   # complete OAuth once
```

Optional per-account `priority` (primary/backup groups) and `weight` (weighted rotation within a group). Reload without restarting: `POST /v0/management/reload`.

## Operations

- **Dashboard** — `http://127.0.0.1:8318/`: status badges, masked tokens, cooldown countdowns, quota bars; refreshes every 5 s.
- **Management API** — `GET /v0/management/accounts`, `POST .../accounts/action` (enable / disable / reset / refresh_quota), `POST .../quota/refresh`, `GET /health`.
- **Auth (optional)** — set `CLIPOOL_API_KEY` to require a Bearer token / `x-api-key` on every request.
- **Cooldown policy** — quota/429: 60 s × 2ⁿ (cap 1 h) · transient: 15 s × 2ⁿ (cap 5 min) · auth failure: disabled persistently, self-healing probe after 10 min.

## In-process use (Python, no HTTP)

The same pool semantics are importable directly — handy for embedding in agent pipelines:

```python
from clipool import run_with_pool
reply = run_with_pool("claude", "hello", model="sonnet", effort="high")
```

## Documentation

Detailed docs are currently in Chinese (contributions translating them are welcome):

| Doc | Contents |
|-----|----------|
| [README_CN](./README_CN.md) | Full reference: account file formats, env vars, cooldown, comparison table |
| [docs/tutorial.md](./docs/tutorial.md) | Step-by-step tutorial: install → pool setup → client integration → FAQ |
| [docs/architecture.md](./docs/architecture.md) | Internals: request lifecycle, pool scheduling, HOME isolation, provider layer |
| [docs/claude_code_guide.md](./docs/claude_code_guide.md) | Claude Code integration guide |
| [docs/accounts.md](./docs/accounts.md) | Account registration details per backend |

## clipool vs CLIProxyAPI (Go)

| | CLIProxyAPI (Go) | clipool (Python) |
|---|---|---|
| Backend | direct upstream HTTP with OAuth tokens | **spawns the real CLI binaries** |
| Multi-account | token pool | **HOME-isolated CLI login profiles** |
| Streaming | native | simulated (CLI is synchronous; SSE-wrapped) |
| Best for | high throughput, tokens available | CLI-only login states, exact CLI behavior |

Both expose the same OpenAI-compatible surface and `/v0/management/*` conventions — run them side by side (8317 / 8318).

## Notes

- Requests run the CLI synchronously in a thread pool; expect seconds-level latency per call (`AGENT_LLM_CLI_TIMEOUT`, default 600 s).
- `stream=True` is protocol-compatible but not token-by-token yet (on the roadmap).
- Use clipool only with accounts **you own**, and make sure your usage complies with each service's terms.

## License

[MIT](./LICENSE)
