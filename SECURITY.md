# Security policy

clipool launches locally installed AI CLIs and can read their profile tokens.
A vulnerability may therefore expose credentials or allow commands to run with
the service process's permissions.

## Supported versions

Security fixes are provided on the latest commit of the default branch while
the project is in alpha. There is not yet a stable release line.

## Reporting a vulnerability

Please use GitHub's private **Report a vulnerability** flow on the repository's
Security tab. Do not open a public issue and do not attach real tokens, account
files, or unredacted profile paths.

Include the affected commit, platform, reproduction steps, expected impact, and
whether the issue requires a non-loopback listener. You should receive an
initial acknowledgement within seven days. Please allow time for a fix before
publishing details.

## Deployment boundary

- Keep the default `127.0.0.1` listener unless you have a controlled reason to
  expose the service.
- A non-loopback listener is rejected unless `CLIPOOL_API_KEY` is set. The key
  protects generation, model discovery, and management endpoints; only the
  dashboard shell and health check remain public.
- Codex receives a private temporary HOME/CWD, a clean environment, and
  read-only/ephemeral/ignore-config flags by default. It still receives the
  selected `CODEX_HOME` for authentication and may read local data permitted by
  the Codex sandbox. This is defense in depth for a trusted local service, not
  a multi-tenant or confidentiality sandbox. `CLIPOOL_CODEX_UNSAFE=1` disables
  these restrictions; accept prompts only from clients you fully trust either
  way.
- On macOS, an Antigravity CLI fallback uses only a random-name keychain owned
  by its managed virtual profile. The machine password is stored in
  `.clipool-keychain.json` (`0600`), so treat that file and archived copies as
  credentials. Existing login keychains are not modified or searched. Real
  HOME, unmarked external profiles, unsafe Keychains/Preferences symlinks, and
  missing HOME fail closed before `agy` starts.
- Treat `~/.clipool/`, its JSON files, and all referenced profile directories as
  secrets. Restrict their filesystem permissions and never commit them.
- API-key authentication does not turn clipool into a hostile multi-user
  execution service; do not expose it to untrusted prompt authors.
- Use clipool only with accounts you own and in accordance with each provider's
  terms.
