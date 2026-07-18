"""HTTP bind/authentication and Codex execution security boundaries."""
from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from clipool.__main__ import _is_loopback_host, main
from clipool.providers.base import BaseProvider
from clipool.providers.codex import CodexProvider
from clipool.server import app


def test_management_endpoints_require_configured_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPOOL_API_KEY", "management-secret")
    client = TestClient(app)
    requests = [
        ("get", "/v0/management/accounts", None),
        ("get", "/v0/management/accounts/claude", None),
        (
            "post",
            "/v0/management/accounts/action",
            None,
        ),
        ("post", "/v0/management/quota/refresh", None),
        ("post", "/v0/management/reload", None),
    ]

    for method, path, body in requests:
        response = client.request(method, path, json=body)
        assert response.status_code == 401, path

    response = client.get(
        "/v0/management/accounts",
        headers={"Authorization": "Bearer management-secret"},
    )
    assert response.status_code == 200


def test_api_key_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPOOL_API_KEY", "expected")
    client = TestClient(app)
    with patch("clipool.server.secrets.compare_digest", return_value=False) as compare:
        response = client.get(
            "/v0/management/accounts", headers={"X-API-Key": "provided"}
        )

    assert response.status_code == 401
    compare.assert_called_once_with(b"provided", b"expected")


def test_dashboard_can_store_and_send_api_key() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert 'type="password" id="apiKey"' in response.text
    assert "sessionStorage" in response.text
    assert 'headers.set("Authorization", "Bearer " + key)' in response.text
    assert 'apiFetch("/v0/management/accounts"' in response.text


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost.", "127.0.0.1", "127.99.2.3", "::1", "[::1]"],
)
def test_loopback_hosts_are_recognized(host: str) -> None:
    assert _is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_non_loopback_hosts_are_recognized(host: str) -> None:
    assert not _is_loopback_host(host)


def test_non_loopback_bind_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIPOOL_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["clipool", "--host", "0.0.0.0"])
    with patch("clipool.__main__.uvicorn.run") as run:
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 2
    run.assert_not_called()


def test_non_loopback_bind_allowed_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPOOL_API_KEY", "network-secret")
    monkeypatch.setattr(sys, "argv", ["clipool", "--host", "0.0.0.0"])
    with patch("clipool.__main__.uvicorn.run") as run:
        main()
    assert run.call_args.kwargs["host"] == "0.0.0.0"


def test_direct_uvicorn_style_remote_request_is_rejected_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLIPOOL_API_KEY", raising=False)
    client = TestClient(app, client=("203.0.113.10", 50000))
    response = client.get("/health")
    assert response.status_code == 403
    assert "CLIPOOL_API_KEY" in response.json()["detail"]


def test_remote_dashboard_shell_is_allowed_once_network_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIPOOL_API_KEY", "network-secret")
    client = TestClient(app, client=("203.0.113.10", 50000))
    assert client.get("/").status_code == 200


def _run_codex_and_capture(
    monkeypatch: pytest.MonkeyPatch, *, unsafe: bool
) -> tuple[list[str], str, int, bool]:
    if unsafe:
        monkeypatch.setenv("CLIPOOL_CODEX_UNSAFE", "1")
    else:
        monkeypatch.delenv("CLIPOOL_CODEX_UNSAFE", raising=False)

    captured: dict[str, object] = {}
    provider = CodexProvider()

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = list(cmd)
        out_path = Path(cmd[cmd.index("-o") + 1])
        captured["workdir"] = str(out_path.parent)
        captured["mode"] = stat.S_IMODE(out_path.parent.stat().st_mode)
        out_path.write_text("safe answer", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(provider, "_run_subprocess", fake_run)
    answer = provider.run("hello", model="gpt-test", effort="high")
    workdir = str(captured["workdir"])
    return (
        captured["cmd"],  # type: ignore[return-value]
        answer,
        captured["mode"],  # type: ignore[return-value]
        Path(workdir).exists(),
    )


def test_codex_defaults_to_isolated_read_only_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd, answer, mode, workdir_exists_after = _run_codex_and_capture(
        monkeypatch, unsafe=False
    )
    assert answer == "safe answer"
    assert mode == 0o700
    assert not workdir_exists_after
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in cmd
    assert "--ignore-user-config" in cmd
    assert "--ignore-rules" in cmd
    assert "-C" in cmd


def test_codex_unsafe_escape_hatch_restores_legacy_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd, answer, mode, workdir_exists_after = _run_codex_and_capture(
        monkeypatch, unsafe=True
    )
    assert answer == "safe answer"
    assert mode == 0o700
    assert not workdir_exists_after
    for flag in (
        "--sandbox",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
    ):
        assert flag not in cmd


def test_clean_subprocess_env_drops_service_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIPOOL_API_KEY", "must-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/private-agent.sock")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = BaseProvider._subprocess_env(
        {"HOME": "/tmp/safe-home", "CODEX_HOME": "/tmp/codex-home"},
        clean_env=True,
    )

    assert env is not None
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/tmp/safe-home"
    assert env["CODEX_HOME"] == "/tmp/codex-home"
    assert "CLIPOOL_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
