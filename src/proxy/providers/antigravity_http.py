"""Direct Antigravity / Google Cloud Code Assist HTTP backend.

This module is intentionally independent from ``agy --print`` so cli_proxy can
serve tools such as Claude Code through an Anthropic-compatible local API.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from ..account import Account, REFRESH_SKEW_SECONDS
from ..anthropic import AnthropicMessagesRequest, messages_to_prompt
from ..config import CLI_TIMEOUT
from .antigravity import AntigravityProvider, resolve_variant

TOKEN_RELATIVE_PATH = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"
DEFAULT_ENDPOINTS = (
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal",
    "https://daily-cloudcode-pa.googleapis.com/v1internal",
    "https://cloudcode-pa.googleapis.com/v1internal",
)


@dataclass(frozen=True)
class AntigravityProfileToken:
    access_token: str
    refresh_token: str = ""
    token_type: str = "Bearer"
    expiry: Optional[datetime] = None
    path: Optional[Path] = None

    def is_expired(self, skew_seconds: int = 120) -> bool:
        if self.expiry is None:
            return False
        return datetime.now(timezone.utc).timestamp() + skew_seconds >= self.expiry.timestamp()


def _parse_expiry(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def token_file_for_home(home: str | os.PathLike[str]) -> Path:
    return Path(home).expanduser() / TOKEN_RELATIVE_PATH


def load_antigravity_profile_token(home: str | os.PathLike[str]) -> AntigravityProfileToken:
    """Load Antigravity OAuth token JSON from an isolated profile home."""

    path = token_file_for_home(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Antigravity token file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Antigravity token file is not valid JSON: {path}") from exc

    token_data = raw.get("token", raw) if isinstance(raw, dict) else {}
    if not isinstance(token_data, dict):
        raise RuntimeError(f"Antigravity token file has unexpected shape: {path}")

    access_token = str(token_data.get("access_token", "")).strip()
    if not access_token:
        raise RuntimeError(f"Antigravity token file does not contain access_token: {path}")

    return AntigravityProfileToken(
        access_token=access_token,
        refresh_token=str(token_data.get("refresh_token", "")).strip(),
        token_type=str(token_data.get("token_type", "Bearer")).strip() or "Bearer",
        expiry=_parse_expiry(token_data.get("expiry")),
        path=path,
    )


def _write_refreshed_token(current: AntigravityProfileToken, payload: dict[str, Any]) -> AntigravityProfileToken:
    if current.path is None:
        raise RuntimeError("Cannot persist refreshed Antigravity token without token path")

    expires_in = int(payload.get("expires_in", 3600))
    expiry = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc)
    token_data = {
        "access_token": str(payload["access_token"]),
        "token_type": str(payload.get("token_type", current.token_type or "Bearer")),
        "refresh_token": str(payload.get("refresh_token", current.refresh_token)),
        "expiry": expiry.isoformat().replace("+00:00", "Z"),
    }
    current.path.write_text(
        json.dumps({"token": token_data, "auth_method": "oauth"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return AntigravityProfileToken(
        access_token=token_data["access_token"],
        refresh_token=token_data["refresh_token"],
        token_type=token_data["token_type"],
        expiry=expiry,
        path=current.path,
    )


def refresh_antigravity_profile_token(token: AntigravityProfileToken) -> AntigravityProfileToken:
    """Refresh an expired profile token using user-provided OAuth client env vars."""

    client_id = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET", "").strip()
    if not token.refresh_token:
        raise RuntimeError("Antigravity token is expired and has no refresh_token")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Antigravity token is expired. Set ANTIGRAVITY_OAUTH_CLIENT_ID and "
            "ANTIGRAVITY_OAUTH_CLIENT_SECRET, or refresh the profile with agy login."
        )

    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": token.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Antigravity token refresh failed: {response.status_code} {response.text[:300]}")
    return _write_refreshed_token(token, response.json())


def load_fresh_antigravity_token(home: str | os.PathLike[str]) -> AntigravityProfileToken:
    token = load_antigravity_profile_token(home)
    # 提前 REFRESH_SKEW_SECONDS（300s）刷新，对齐 cockpit-tools 的 ensure_fresh_token，
    # 避免「刚好过期瞬间」并发命中导致请求 401。
    if token.is_expired(REFRESH_SKEW_SECONDS):
        return refresh_antigravity_profile_token(token)
    return token


def _headers(token: AntigravityProfileToken) -> dict[str, str]:
    return {
        "authorization": f"{token.token_type} {token.access_token}",
        "content-type": "application/json",
        "user-agent": "cli_proxy/2.0",
        "x-client-name": "cli_proxy",
        "x-client-version": "2.0",
        "x-machine-id": os.environ.get("CLI_PROXY_MACHINE_ID", uuid.uuid4().hex),
        "x-vscode-sessionid": os.environ.get("CLI_PROXY_SESSION_ID", uuid.uuid4().hex),
    }


def _endpoints() -> tuple[str, ...]:
    configured = os.environ.get("ANTIGRAVITY_CLOUDCODE_ENDPOINT", "").strip()
    if configured:
        return (configured.rstrip("/"),)
    return DEFAULT_ENDPOINTS


def _fetch_project_id(token: AntigravityProfileToken) -> str:
    configured = os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()
    if configured:
        return configured

    body = {"metadata": {"ideType": "ANTIGRAVITY"}}
    last_error = ""
    for endpoint in _endpoints():
        response = requests.post(
            f"{endpoint}:loadCodeAssist",
            headers=_headers(token),
            json=body,
            timeout=30,
        )
        if response.status_code < 400:
            data = response.json()
            project = str(data.get("cloudaicompanionProject", "")).strip()
            if project:
                return project
        last_error = f"{response.status_code} {response.text[:300]}"
    raise RuntimeError(f"Unable to resolve Antigravity Cloud Code project: {last_error}")


def build_generate_body(
    req: AnthropicMessagesRequest,
    *,
    model: str,
    project_id: str,
) -> dict[str, Any]:
    generation_config: dict[str, Any] = {"maxOutputTokens": req.max_tokens}
    if req.temperature is not None:
        generation_config["temperature"] = req.temperature

    return {
        "model": model,
        "project": project_id,
        "request": {
            "contents": [{"role": "user", "parts": [{"text": messages_to_prompt(req)}]}],
            "generationConfig": generation_config,
        },
    }


def extract_text_from_generate_response(data: Any) -> str:
    """Extract text from Cloud Code/Gemini-like response shapes."""

    texts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str):
                texts.append(text)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(data)
    return "\n".join(text for text in texts if text).strip()


class AntigravityHTTPProvider:
    """Run Anthropic requests through an Antigravity profile without spawning agy."""

    def run_messages(self, req: AnthropicMessagesRequest, account: Account) -> str:
        if not account.home:
            raise RuntimeError(f"Antigravity account {account.id!r} has no profile home")

        try:
            return self._run_direct(req, account)
        except RuntimeError as exc:
            if os.environ.get("ANTIGRAVITY_DISABLE_CLI_FALLBACK", "").strip() == "1":
                raise
            return self._run_cli_fallback(req, account, exc)

    def _run_direct(self, req: AnthropicMessagesRequest, account: Account) -> str:
        token = load_fresh_antigravity_token(account.home)
        # 把（可能刚刷新的）过期时间回写账号 registry，供 needs_refresh() 与管理接口展示。
        if token.expiry is not None:
            new_expiry = token.expiry.timestamp()
            if new_expiry != account.expiry:
                account.expiry = new_expiry
                account.persist()
        project_id = str(account.extra_env.get("ANTIGRAVITY_PROJECT_ID", "")).strip() or _fetch_project_id(token)
        model = resolve_variant(req.model.removeprefix("antigravity/"), "high")
        body = build_generate_body(req, model=model, project_id=project_id)

        last_error = ""
        for endpoint in _endpoints():
            response = requests.post(
                f"{endpoint}:generateContent",
                headers=_headers(token),
                json=body,
                timeout=CLI_TIMEOUT,
            )
            if response.status_code < 400:
                text = extract_text_from_generate_response(response.json())
                if text:
                    return text
                raise RuntimeError("Antigravity response did not contain text")
            last_error = f"{response.status_code} {response.text[:500]}"

        raise RuntimeError(f"Antigravity HTTP request failed: {last_error}")

    def _run_cli_fallback(
        self,
        req: AnthropicMessagesRequest,
        account: Account,
        direct_error: RuntimeError,
    ) -> str:
        """Fallback to agy --print when direct HTTP cannot use the profile token."""

        provider = AntigravityProvider()
        prompt = messages_to_prompt(req)
        model = req.model.removeprefix("antigravity/")
        try:
            return provider.run(
                prompt,
                model,
                "high",
                env_override=account.env_override(),
            )
        except RuntimeError as cli_error:
            raise RuntimeError(
                f"Antigravity HTTP failed ({direct_error}); agy CLI fallback failed ({cli_error})"
            ) from cli_error
