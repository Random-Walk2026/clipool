"""Anthropic-compatible helpers for CLI proxy frontends."""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel, Field


class AnthropicMessagesRequest(BaseModel):
    """Subset of Anthropic /v1/messages used by Claude Code."""

    model: str
    messages: list[dict[str, Any]]
    max_tokens: int = Field(default=4096)
    stream: bool = False
    system: Any = None
    tools: Optional[list[dict[str, Any]]] = None
    temperature: Optional[float] = None
    thinking: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None
    output_config: Optional[dict[str, Any]] = None


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return str(block)

    block_type = block.get("type")
    if block_type == "text":
        return str(block.get("text", ""))
    if block_type == "tool_result":
        tool_id = str(block.get("tool_use_id", "")).strip()
        content = content_to_text(block.get("content", ""))
        label = f"[Tool result {tool_id}]" if tool_id else "[Tool result]"
        return f"{label}\n{content}".strip()
    if block_type == "tool_use":
        name = str(block.get("name", "tool")).strip() or "tool"
        return f"[Tool use {name}]\n{json.dumps(block.get('input', {}), ensure_ascii=False)}"
    if block_type in {"thinking", "redacted_thinking"}:
        return ""
    if block_type in {"image", "document"}:
        return f"[{block_type} omitted]"
    return str(block.get("text") or block.get("content") or "")


def content_to_text(content: Any) -> str:
    """Flatten Anthropic message content into prompt text."""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_block_text(item) for item in content]
        return "\n\n".join(part for part in parts if part)
    return _block_text(content)


def system_to_text(system: Any) -> str:
    if not system:
        return ""
    if isinstance(system, list):
        parts = [_block_text(item) for item in system]
        return "\n".join(part for part in parts if part)
    return content_to_text(system)


def messages_to_prompt(req: AnthropicMessagesRequest) -> str:
    """Convert Anthropic messages to a plain prompt for providers without tool IO."""

    parts: list[str] = []
    system = system_to_text(req.system)
    if system:
        parts.append(f"[System]\n{system}")

    for message in req.messages:
        role = str(message.get("role", "user")).strip().lower() or "user"
        text = content_to_text(message.get("content", ""))
        label = {
            "user": "User",
            "assistant": "Assistant",
            "system": "System",
        }.get(role, role.capitalize())
        if text:
            parts.append(f"[{label}]\n{text}")

    return "\n\n".join(parts)


def anthropic_message_body(content: str, model: str, req_id: str) -> dict[str, Any]:
    return {
        "id": f"msg_{req_id}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


async def anthropic_sse_response(
    content: str,
    model: str,
    req_id: str | None = None,
) -> AsyncIterator[str]:
    """Wrap a completed provider response as Anthropic SSE events."""

    mid = f"msg_{req_id or uuid.uuid4().hex[:8]}"
    def event(name: str, payload: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": mid,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    yield event(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    if content:
        yield event(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": content}},
        )
    yield event("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        },
    )
    yield event("message_stop", {"type": "message_stop"})


def estimated_token_count(req: AnthropicMessagesRequest) -> int:
    prompt = messages_to_prompt(req)
    return max(1, len(prompt) // 4)
