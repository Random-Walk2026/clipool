# Changelog

Notable changes to this project are documented here. The project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and intends to use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) for releases.

## [Unreleased]

### Added

- OpenAI Chat Completions and Anthropic Messages compatible local endpoints.
- Multi-account priority, weighted rotation, cooldown, and recovery handling.
- HOME-isolated Antigravity profiles and CODEX_HOME-isolated Codex profiles.
- Antigravity Cloud Code Assist HTTP fast path with `agy --print` fallback.
- Account dashboard, management endpoints, and quota snapshots.
- Backend-default and cached Codex model discovery through `/v1/models`.
- Default Codex execution with a temporary HOME/CWD, clean environment,
  read-only and ephemeral flags, and ignored user config/rules; an explicit
  `CLIPOOL_CODEX_UNSAFE=1` opt-out remains available. This is a trusted-local
  hardening layer, not a multi-tenant confidentiality sandbox, because Codex
  still needs the selected `CODEX_HOME`.
- Python 3.10–3.13 CI, package build checks, and installed-wheel dashboard smoke
  coverage.

### Changed

- The dashboard assets now ship in both the wheel and source distribution.
- `CLIPOOL_API_KEY` now protects every management endpoint and model discovery;
  a non-loopback listener requires the key.

### Fixed

- Pool reloads now atomically switch request generations. Results from old
  account objects are ignored. A process-global persistence revision makes a
  concurrent token, expiry, quota, or management-state write retry reload, and
  automatically refreshes the snapshot before the next pool read or pick.
- Reloads merge cooldown and error counts, and retain an active probe lease,
  when backend, account id, and credential generation are unchanged. A token,
  home, injected environment, or profile credential-file change starts a clean
  runtime generation; old-object completions remain ignored in both cases.
- Known-backend account files are validated against the credentials that backend
  can actually use. Missing authentication and placeholder-only files remain as
  disabled `configuration_error` entries, blocking accidental default-login
  fallback, and cannot be enabled until the JSON is fixed and reloaded. Unknown
  backends continue to be skipped.
- Recovery probes are leased once and do not revive manually disabled accounts.
- Codex model subsets use independent rotation cursors, preventing starvation.
- Account JSON persistence uses field-level, locked atomic updates with `0600`
  permissions.
- CLI timeouts terminate the complete subprocess group on Unix; Windows uses
  best-effort direct-child termination.

### Security

- Each managed Antigravity virtual profile now gets a random-name dedicated
  keychain and random machine password recorded in `.clipool-keychain.json`
  (`0600`). Its `Keychains` and `Preferences` trees are `0700`, and default/search
  lists reference only the dedicated keychain. Existing login keychains remain
  untouched; recoverable dedicated-state failures are archived. Real HOME,
  unmarked external profiles, unsafe symlinks, missing HOME, and setup errors
  fail closed before `agy` can show a password prompt.
- Antigravity HTTP reads and `agy` fallback now share strict OAuth token-file
  validation: ancestor directories cannot be symlinks or escape the profile;
  the token must be a single-link regular file with valid JSON and a non-empty
  `access_token`, and is tightened to `0600`. Missing, corrupt, linked, or
  escaped tokens fail closed before HTTP use or `agy` startup.

[Unreleased]: https://github.com/Random-Walk2026/clipool/commits/main
