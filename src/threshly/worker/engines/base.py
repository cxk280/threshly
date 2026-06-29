"""The engine interface every backend implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class EngineResult:
    item_id: str
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    prompt_tokens: int = 0
    output_tokens: int = 0
    cache_hit: bool = False


@dataclass
class EngineRequest:
    item_id: str
    custom_id: str
    body: dict[str, Any]
    prefix_group: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Engine(Protocol):
    model: str

    def generate(self, requests: list[EngineRequest]) -> list[EngineResult]:
        """Run a chunk of requests and return one result per request.

        Implementations should set ``cache_hit`` per result when a prefix-cache hit occurred, so the
        coordinator can report the cache-hit rate the prefix-aware scheduler is producing.
        """
        ...
