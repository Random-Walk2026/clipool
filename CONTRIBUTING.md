# Contributing to clipool

Thanks for helping improve clipool. Bug reports, documentation fixes, tests,
and focused provider improvements are welcome.

## Before opening an issue

- Search existing issues first.
- Remove tokens, account email addresses, profile paths, and command output that
  may contain credentials.
- For security vulnerabilities, follow [SECURITY.md](./SECURITY.md) instead of
  opening a public issue.

## Development setup

clipool supports Python 3.10 through 3.13.

```bash
git clone https://github.com/Random-Walk2026/clipool.git
cd clipool
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the same checks used by CI:

```bash
python -m coverage run -m pytest -q
python -m coverage report --fail-under=60
python -m ruff check .
python -m mypy clipool
python -m build
```

Tests must not call a real subscription CLI or make a real upstream request.
Use temporary profile directories and mocks, and never add live credentials to
fixtures.

## Pull requests

- Keep each pull request focused and explain the user-visible behavior change.
- Add or update tests for behavior changes.
- Update the relevant README or document when a command, environment variable,
  account format, endpoint, or security boundary changes.
- Preserve the default localhost-only posture. Changes that widen network,
  filesystem, subprocess, or credential access need an explicit security
  rationale.
- Add an entry under `Unreleased` in [CHANGELOG.md](./CHANGELOG.md) for notable
  user-facing changes.

By submitting a contribution, you agree that it is licensed under the project's
[MIT License](./LICENSE).
