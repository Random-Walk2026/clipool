# clipool

**English** | [中文](./README_CN.md)

Turn your local **Claude / Codex / Grok / Copilot CLIs** and **Antigravity
profiles** into a standard **OpenAI- & Anthropic-compatible HTTP API** — with
automatic multi-account rotation.

```
OpenAI SDK / Claude Code / LangChain / curl ...
        │  standard /v1/chat/completions or /v1/messages
        ▼
┌─────────────────────────────┐
│   clipool  (localhost:8318) │      picks an account, injects its profile,
│   account pool · cooldown   │────► then runs the local CLI; Antigravity
│   rotation · retry          │      Messages may use a profile-token HTTP
└─────────────────────────────┘      fast path, with `agy --print` fallback
```

## Why clipool

- **Universal adapter** — anything that speaks the OpenAI or Anthropic API can use your local subscription tools: OpenAI/Anthropic SDKs, Claude Code, LangChain, IDE plugins, or plain `curl`.
- **CLI-first execution** — Claude, Codex, Grok, Copilot, and the normal Antigravity path run their local CLI binaries. Antigravity `/v1/messages` first tries its profile-backed Cloud Code Assist HTTP fast path and falls back to `agy --print`.
- **Multi-account pool** — put several accounts of the same service in `~/.clipool/`; requests rotate across them (priority groups + weighted round-robin). Exhausted accounts cool down with exponential backoff and rejoin automatically. Authentication failures stay disabled; only `invalid_grant` disables receive a leased recovery probe.
- **HOME-isolated account switching** — for CLIs whose login state is a directory (Antigravity, Codex), each account gets its own profile directory injected via subprocess env (`HOME` / `CODEX_HOME`). No global state, thread-safe.
- **No silent fallback** — if a pool has accounts but all are cooling/disabled, requests fail loudly instead of silently burning your default login's quota.
- **Built-in dashboard** — zero-dependency account status page at `/`, plus a `/v0/management/*` API and an optional Streamlit console; quota bars (5-hour / weekly) for codex, claude and antigravity.

> **Not a port of [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (Go).** It borrows model-string and management-API conventions, but primarily orchestrates local CLI processes and isolated login profiles. Antigravity Messages additionally has the narrow HTTP fast path described above. Authentication directories and file formats are **not** compatible (`~/.clipool/` vs `~/.cli-proxy-api/`); never share one directory between them.

## Quick start

```bash
git clone https://github.com/Random-Walk2026/clipool.git && cd clipool
python -m pip install -e .

python -m clipool          # serves http://127.0.0.1:8318/v1  (8317 is left for CLIProxyAPI)
```

With no account file, Claude, Codex, Grok, and Copilot can attempt their current
CLI login in single-account mode. **Antigravity is intentionally different:**
register a managed isolated profile first; clipool never runs `agy` against the
service process's default HOME. Then call it like any OpenAI endpoint:

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

`GET /v1/models` returns one default capability per backend plus Codex models
discovered in the loaded profiles' `models_cache.json`, including supporting
account counts. It is not a complete live upstream inventory.

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

On macOS, the `agy` fallback creates a random-name keychain with a random
machine password for each managed virtual profile. The password and relative
keychain path live in `.clipool-keychain.json` (`0600`); the profile and its
`Library/Keychains` and `Library/Preferences` trees are `0700`, and their
default/search lists point only to that dedicated keychain. clipool never uses,
rewrites, or searches an existing `login.keychain-db`. A broken dedicated
keychain or state file is recoverably archived before replacement. Real HOME,
unmarked external profiles, unsafe symlinks, or a missing HOME fail closed
before `agy` starts, so there is no password prompt to answer. See
[docs/accounts.md](./docs/accounts.md#macos-虚拟-profile-钥匙串).

Codex must be logged in with `CODEX_HOME` (not `HOME`) and registered by its
profile directory:

```bash
mkdir -p ~/.clipool/profiles/codex_personal
CODEX_HOME="$HOME/.clipool/profiles/codex_personal" codex login
```

clipool reads each Codex profile's `models_cache.json` and only routes a
requested model to accounts that list it. You can also register an existing
working login without copying tokens:

```json
{ "type": "codex", "email": "codex-pro-default", "home": "~/.codex", "enabled": true }
```

Optional per-account `priority` (primary/backup groups) and `weight` (weighted rotation within a group). Reload without restarting: `POST /v0/management/reload`. Reload atomically swaps a new request generation; completions from old account objects are ignored, and a revision check retries a stale disk read if persistent state changed while it was loading.

## Operations

- **Dashboard** — `http://127.0.0.1:8318/`: status badges, masked tokens, cooldown countdowns, quota bars; refreshes every 5 s.
- **Management API** — `GET /v0/management/accounts`, `POST .../accounts/action` (enable / disable / reset / refresh_quota), `POST .../quota/refresh`, `GET /health`.
- **Auth (optional on loopback)** — `CLIPOOL_API_KEY` protects generation, `/v1/models`, and every `/v0/management/*` endpoint with a Bearer token or `x-api-key`. Only the dashboard shell and `/health` stay public so a browser can load before you enter the key. Starting on a non-loopback address without a key is rejected.
- **Codex boundary** — by default Codex gets a private temporary HOME and working directory, a clean environment, and `--sandbox read-only --ephemeral --ignore-user-config --ignore-rules`. It still receives the selected `CODEX_HOME` for authentication and may read local data allowed by the Codex sandbox. This is defense in depth for a **trusted local service**, not a multi-tenant or confidentiality sandbox. `CLIPOOL_CODEX_UNSAFE=1` explicitly disables these default restrictions.
- **Cooldown policy** — quota/429: 60 s × 2ⁿ (cap 1 h) · transient: 15 s × 2ⁿ (cap 5 min) · auth failure: disabled persistently; only `invalid_grant` disables get one leased probe after 10 min.

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
| Backend | direct upstream HTTP with OAuth tokens | **local CLIs**, plus Antigravity Messages HTTP fast path |
| Multi-account | token pool | **HOME-isolated CLI login profiles** |
| Streaming | native | simulated (CLI is synchronous; SSE-wrapped) |
| Best for | high throughput, tokens available | local CLI/profile orchestration and rotation |

Both expose the same OpenAI-compatible surface and `/v0/management/*` conventions — run them side by side (8317 / 8318).

## Notes

- Requests run the CLI synchronously in a thread pool; expect seconds-level latency per call (`AGENT_LLM_CLI_TIMEOUT`, default 600 s).
- On Unix, a timed-out CLI's complete process group is terminated. Windows can
  only perform best-effort termination of the direct child process.
- `stream=True` is protocol-compatible but not token-by-token yet (on the roadmap).
- The default listener is `127.0.0.1`. Set a strong `CLIPOOL_API_KEY` before binding to a non-loopback interface.
- clipool is a trusted-local tool, not a hostile multi-user execution boundary;
  do not expose it to untrusted prompt authors even when API authentication is enabled.
- Use clipool only with accounts **you own**, and make sure your usage complies with each service's terms.

Security reports should follow [SECURITY.md](./SECURITY.md); development setup
and checks are in [CONTRIBUTING.md](./CONTRIBUTING.md).

## License

[MIT](./LICENSE)
