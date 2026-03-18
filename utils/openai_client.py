"""Helpers for constructing OpenAI-compatible clients with project defaults."""

from __future__ import annotations

from typing import Any

import httpx
from django.conf import settings

DEFAULT_LLM_CONNECT_TIMEOUT = 15.0
DEFAULT_LLM_READ_TIMEOUT = 120.0
DEFAULT_LLM_WRITE_TIMEOUT = 30.0
DEFAULT_LLM_POOL_TIMEOUT = 30.0
DEFAULT_LLM_MAX_RETRIES = 2


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def build_openai_timeout() -> httpx.Timeout:
    """Build a bounded timeout policy for OpenAI-compatible requests."""
    return httpx.Timeout(
        connect=_positive_float(getattr(settings, "LLM_CONNECT_TIMEOUT", None), DEFAULT_LLM_CONNECT_TIMEOUT),
        read=_positive_float(getattr(settings, "LLM_READ_TIMEOUT", None), DEFAULT_LLM_READ_TIMEOUT),
        write=_positive_float(getattr(settings, "LLM_WRITE_TIMEOUT", None), DEFAULT_LLM_WRITE_TIMEOUT),
        pool=_positive_float(getattr(settings, "LLM_POOL_TIMEOUT", None), DEFAULT_LLM_POOL_TIMEOUT),
    )


def build_openai_client_kwargs() -> dict[str, Any]:
    """Return shared kwargs for OpenAI/OpenAI-compatible client construction."""
    return {
        "timeout": build_openai_timeout(),
        "max_retries": _non_negative_int(getattr(settings, "LLM_MAX_RETRIES", None), DEFAULT_LLM_MAX_RETRIES),
    }
