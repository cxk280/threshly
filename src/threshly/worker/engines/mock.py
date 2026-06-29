"""A GPU-free engine that lets the whole system run on a laptop, in CI, and in demos.

It returns deterministic chat-completion-shaped responses and *simulates prefix caching*: the first
request seen for a given ``prefix_group`` is a cache miss; later ones are hits — exactly the
behaviour the prefix-aware scheduler is designed to elicit from vLLM's automatic prefix caching.
"""

from __future__ import annotations

import time

from .base import EngineRequest, EngineResult


class MockEngine:
    def __init__(self, model: str = "demo-model", latency_s: float = 0.05) -> None:
        self.model = model
        self.latency_s = latency_s
        self._seen_prefixes: set[str] = set()

    def _answer(self, body: dict) -> str:
        messages = body.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            content = last.get("content", "") if isinstance(last, dict) else ""
            if not isinstance(content, str):
                content = str(content)
            return f"[mock:{self.model}] {content[:200]}"
        prompt = body.get("prompt", "")
        return f"[mock:{self.model}] {str(prompt)[:200]}"

    def generate(self, requests: list[EngineRequest]) -> list[EngineResult]:
        results: list[EngineResult] = []
        for req in requests:
            if self.latency_s:
                time.sleep(self.latency_s)
            cache_hit = req.prefix_group in self._seen_prefixes
            self._seen_prefixes.add(req.prefix_group)
            answer = self._answer(req.body)
            prompt_tokens = max(1, sum(len(str(m)) for m in req.body.get("messages", [])) // 4)
            output_tokens = max(1, len(answer) // 4)
            results.append(
                EngineResult(
                    item_id=req.item_id,
                    response={
                        "id": f"chatcmpl-{req.item_id}",
                        "object": "chat.completion",
                        "model": self.model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": answer},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": output_tokens,
                            "total_tokens": prompt_tokens + output_tokens,
                        },
                    },
                    prompt_tokens=prompt_tokens,
                    output_tokens=output_tokens,
                    cache_hit=cache_hit,
                )
            )
        return results
