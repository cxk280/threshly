"""Pluggable inference engines. The runner only knows the ``Engine`` interface."""

from __future__ import annotations

from .base import Engine, EngineResult


def make_engine(name: str, model: str, **kwargs) -> Engine:
    if name == "mock":
        from .mock import MockEngine

        return MockEngine(model=model, **kwargs)
    if name == "vllm":
        from .vllm import VLLMEngine

        return VLLMEngine(model=model, **kwargs)
    raise ValueError(f"unknown engine: {name!r} (expected 'mock' or 'vllm')")


__all__ = ["Engine", "EngineResult", "make_engine"]
