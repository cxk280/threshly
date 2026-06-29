"""Prefix-aware grouping.

vLLM's automatic prefix caching reuses the KV cache for a request whose token prefix matches one
already resident on that GPU. To exploit it across a sharded batch we must (a) give requests that
share a leading context the same ``prefix_group`` and (b) dispatch items of the same group to the
same worker contiguously (the lease query orders by ``prefix_group, seq``).

The shared part of a chat request is almost always the leading context — a system prompt and/or a
shared document/RAG block in early turns — while the final user turn varies per request. So we key
the group on everything *except* the final user message, capped to a budget, plus the model.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Only the first this-many characters of the leading context define the group. Long enough to
# distinguish distinct system prompts / shared contexts, short enough to be cheap and stable.
PREFIX_BUDGET_CHARS = 4096


def _messages_prefix_text(body: dict[str, Any]) -> str:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        # Non-chat or single-shot payloads: fall back to the raw prompt if present.
        prompt = body.get("prompt")
        return prompt if isinstance(prompt, str) else ""

    # Everything before the final message is the part most likely shared across the batch.
    leading = messages[:-1] if len(messages) > 1 else []
    parts: list[str] = []
    for m in leading:
        if isinstance(m, dict):
            role = str(m.get("role", ""))
            content = m.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, sort_keys=True)
            parts.append(f"{role}:{content}")
    return "\n".join(parts)


def prefix_group(body: dict[str, Any], model: str) -> str:
    """A stable bucket id for the request's shared leading context (empty-ish -> common bucket)."""
    text = _messages_prefix_text(body)[:PREFIX_BUDGET_CHARS]
    h = hashlib.sha1(f"{model}\x00{text}".encode()).hexdigest()
    return h[:16]
