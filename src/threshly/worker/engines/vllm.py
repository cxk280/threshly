"""The real GPU throughput path: vLLM's offline engine with automatic prefix caching.

A worker processes a leased chunk through one ``LLM.chat`` call so vLLM batches them together and
reuses the KV cache across requests that share a prefix (which the prefix-aware scheduler arranges
to be true within a chunk). Requires the ``vllm`` extra: ``pip install "threshly[vllm]"``.
"""

from __future__ import annotations

from typing import Any

from .base import EngineRequest, EngineResult


class VLLMEngine:
    def __init__(
        self,
        model: str,
        max_num_seqs: int = 256,
        gpu_memory_utilization: float = 0.90,
        max_model_len: int | None = None,
        **engine_kwargs: Any,
    ) -> None:
        from vllm import LLM  # imported lazily; only needed on GPU workers

        self.model = model
        self._llm = LLM(
            model=model,
            enable_prefix_caching=True,  # the whole point of prefix-aware scheduling
            max_num_seqs=max_num_seqs,
            gpu_memory_utilization=gpu_memory_utilization,
            max_model_len=max_model_len,
            **engine_kwargs,
        )

    def _sampling_params(self, body: dict[str, Any]):
        from vllm import SamplingParams

        return SamplingParams(
            temperature=body.get("temperature", 0.0),
            top_p=body.get("top_p", 1.0),
            max_tokens=body.get("max_tokens", body.get("max_completion_tokens", 512)),
            stop=body.get("stop"),
        )

    def generate(self, requests: list[EngineRequest]) -> list[EngineResult]:
        # Group requests so vLLM sees a single batched call (shared prefixes => KV-cache reuse).
        conversations = [r.body.get("messages", []) for r in requests]
        sampling = [self._sampling_params(r.body) for r in requests]
        try:
            outputs = self._llm.chat(conversations, sampling, use_tqdm=False)
        except Exception as e:  # surface a per-chunk error rather than crashing the worker
            return [
                EngineResult(item_id=r.item_id, error={"message": str(e), "type": "engine_error"})
                for r in requests
            ]

        results: list[EngineResult] = []
        for req, out in zip(requests, outputs, strict=False):
            completion = out.outputs[0]
            text = completion.text
            prompt_tokens = len(out.prompt_token_ids or [])
            output_tokens = len(completion.token_ids or [])
            # vLLM reports cached prefix tokens on newer versions; fall back to 0 if absent.
            cached = getattr(out, "num_cached_tokens", 0) or 0
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
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": completion.finish_reason or "stop",
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
                    cache_hit=cached > 0,
                )
            )
        return results
