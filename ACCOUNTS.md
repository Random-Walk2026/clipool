# Account Setup Guide

`cli_proxy` reads account registry files from `~/.cli_proxy_api/` by default.
You can override that location with `CLI_PROXY_AUTH_DIR`.

```text
~/.cli_proxy_api/
├── antigravity_1.json
├── antigravity_2.json
├── claude_work.json
├── codex_personal.json
└── profiles/
    ├── agy_main/
    ├── agy_secondary/
    └── codex_personal/
```

Do not commit this directory. It contains local login state and tokens.

## Antigravity Profiles

Antigravity is profile-based. Give each account its own `HOME` directory so the
CLI writes login state to separate files:

```bash
mkdir -p ~/.cli_proxy_api/profiles/agy_main
HOME="$HOME/.cli_proxy_api/profiles/agy_main" agy -p "ping"
```

Then create an account registry file:

```json
{
  "type": "antigravity",
  "email": "you@example.com",
  "home": "~/.cli_proxy_api/profiles/agy_main",
  "enabled": true
}
```

The token file normally lives under:

```text
~/.cli_proxy_api/profiles/agy_main/.gemini/antigravity-cli/antigravity-oauth-token
```

`cli_proxy` first tries to use that token directly. If direct HTTP is not
available, it falls back to `agy --print` with the same profile home.

## Codex Profiles

Codex is also profile-based:

```bash
mkdir -p ~/.cli_proxy_api/profiles/codex_personal
CODEX_HOME="$HOME/.cli_proxy_api/profiles/codex_personal" codex login
```

Account file:

```json
{
  "type": "codex",
  "email": "you@example.com",
  "home": "~/.cli_proxy_api/profiles/codex_personal",
  "enabled": true
}
```

## Token-Based Providers

Claude, Grok, and Copilot can be configured with token fields in account JSON
files or with environment variables.

```json
{
  "type": "claude",
  "email": "you@example.com",
  "token": "YOUR_CLAUDE_TOKEN",
  "enabled": true
}
```

```json
{
  "type": "grok",
  "email": "you@example.com",
  "token": "YOUR_GROK_TOKEN",
  "enabled": true
}
```

```json
{
  "type": "copilot",
  "email": "you@example.com",
  "token": "YOUR_GITHUB_TOKEN",
  "enabled": true
}
```

Supported environment fallbacks:

```bash
export CLAUDE_CODE_OAUTH_TOKEN="..."
export GROK_API_KEY="..."
export COPILOT_GITHUB_TOKEN="..."
```

## Reloading Accounts

After adding or editing account files while the server is running:

```bash
curl -X POST http://127.0.0.1:8317/v0/management/reload
```
