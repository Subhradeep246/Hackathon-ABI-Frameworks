"""Lightweight in-process caches and data-version for SSE."""

from __future__ import annotations

import time
from typing import Any

_stats_cache: dict[str, Any] = {"data": None, "at": 0.0}
_data_version: int = 0


def get_stats_cache(ttl: int) -> dict[str, Any] | None:
    if _stats_cache["data"] is None:
        return None
    if time.time() - _stats_cache["at"] >= ttl:
        return None
    return _stats_cache["data"]


def set_stats_cache(data: dict[str, Any]) -> None:
    _stats_cache["data"] = data
    _stats_cache["at"] = time.time()


def invalidate_stats_cache() -> None:
    _stats_cache["data"] = None
    _stats_cache["at"] = 0.0
    bump_data_version()


def bump_data_version() -> int:
    global _data_version
    _data_version += 1
    return _data_version


def get_data_version() -> int:
    return _data_version
