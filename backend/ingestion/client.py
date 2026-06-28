"""PCC API client — 429-aware retry, pagination, concurrency control.

The hackathon API returns HTTP 429 on ~30% of requests with a Retry-After header.
Every fetch honors Retry-After before retrying. See API.md for details.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from contextvars import ContextVar
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("PCC_BASE_URL", "https://hackathon.prod.pulsefoundry.ai")
PAGE_SIZE = int(os.getenv("API_PAGE_SIZE", "500"))
MAX_429_RETRIES = int(os.getenv("API_MAX_429_RETRIES", "60"))
MAX_ERROR_RETRIES = int(os.getenv("API_MAX_ERROR_RETRIES", "8"))
MAX_RETRY_AFTER = float(os.getenv("API_MAX_RETRY_AFTER", "15"))

_api_sem: ContextVar[asyncio.Semaphore | None] = ContextVar("api_sem", default=None)

_stats: dict[str, int] = {"requests": 0, "rate_limited": 0, "retries": 0, "errors": 0}


def bind_api_semaphore(sem: asyncio.Semaphore | None = None) -> asyncio.Semaphore:
    """Bind a per-event-loop semaphore (call once at start of asyncio.run)."""
    if sem is None:
        sem = asyncio.Semaphore(int(os.getenv("API_MAX_CONCURRENT", "4")))
    _api_sem.set(sem)
    return sem


def _get_api_semaphore() -> asyncio.Semaphore:
    sem = _api_sem.get()
    if sem is None:
        sem = bind_api_semaphore()
    return sem


def get_client_stats() -> dict[str, int]:
    return dict(_stats)


def reset_client_stats() -> None:
    for k in _stats:
        _stats[k] = 0


class RateLimitError(Exception):
    def __init__(self, retry_after: float, path: str = ""):
        self.retry_after = retry_after
        self.path = path
        super().__init__(f"429 on {path}, retry after {retry_after}s")


class APIError(Exception):
    pass


def _parse_retry_after(header: str | None, default: float = 1.0) -> float:
    if not header:
        return default
    try:
        return min(max(float(header.strip()), 0.1), MAX_RETRY_AFTER)
    except ValueError:
        return default


async def _fetch_once(client: httpx.AsyncClient, path: str, params: dict | None = None) -> Any:
    async with _get_api_semaphore():
        _stats["requests"] += 1
        resp = await client.get(path, params=params or {})
    if resp.status_code == 429:
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        _stats["rate_limited"] += 1
        raise RateLimitError(retry_after, path)
    if resp.status_code == 422:
        raise APIError(f"422 invalid params: {path} {params}")
    if resp.status_code >= 500:
        raise APIError(f"{resp.status_code} server error: {path}")
    resp.raise_for_status()
    return resp.json()


async def fetch_json(client: httpx.AsyncClient, path: str, params: dict | None = None) -> Any:
    """Fetch JSON with mandatory 429 retry (honors Retry-After) and backoff on 5xx."""
    rate_limit_hits = 0
    error_attempts = 0

    while True:
        try:
            return await _fetch_once(client, path, params)
        except RateLimitError as e:
            rate_limit_hits += 1
            _stats["retries"] += 1
            if rate_limit_hits > MAX_429_RETRIES:
                _stats["errors"] += 1
                raise APIError(
                    f"429 rate limit exceeded after {MAX_429_RETRIES} retries on {path}"
                ) from e
            delay = e.retry_after + random.uniform(0, 0.25)
            await asyncio.sleep(delay)
        except APIError as e:
            if "429 rate limit" in str(e):
                raise
            error_attempts += 1
            _stats["retries"] += 1
            if error_attempts > MAX_ERROR_RETRIES:
                _stats["errors"] += 1
                raise
            delay = min(2**error_attempts, 30) + random.uniform(0, 0.5)
            await asyncio.sleep(delay)
        except httpx.TransportError as e:
            error_attempts += 1
            _stats["retries"] += 1
            if error_attempts > MAX_ERROR_RETRIES:
                _stats["errors"] += 1
                raise APIError(f"Transport error on {path}: {e}") from e
            delay = min(2**error_attempts, 30)
            await asyncio.sleep(delay)


def _normalize_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "data", "results", "patients"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


async def fetch_all_pages(
    client: httpx.AsyncClient,
    path: str,
    base_params: dict[str, Any],
) -> list[dict]:
    """Fetch all pages when the API supports limit/offset; otherwise return one response."""
    all_rows: list[dict] = []
    offset = 0
    while True:
        params = {**base_params, "limit": PAGE_SIZE, "offset": offset}
        try:
            data = await fetch_json(client, path, params)
        except APIError as e:
            if offset == 0 and "422" in str(e):
                data = await fetch_json(client, path, base_params)
                return _normalize_list(data)
            raise

        batch = _normalize_list(data)
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return all_rows


async def get_patients(client: httpx.AsyncClient, facility_id: int, since: str | None = None) -> list[dict]:
    params: dict[str, Any] = {"facility_id": facility_id}
    if since:
        params["since"] = since
    return await fetch_all_pages(client, "/pcc/patients", params)


async def get_diagnoses(client: httpx.AsyncClient, patient_id: str) -> list[dict]:
    return _normalize_list(await fetch_json(client, "/pcc/diagnoses", {"patient_id": patient_id}))


async def get_coverage(client: httpx.AsyncClient, patient_id: str) -> list[dict]:
    return _normalize_list(await fetch_json(client, "/pcc/coverage", {"patient_id": patient_id}))


async def get_notes(client: httpx.AsyncClient, patient_internal_id: int) -> list[dict]:
    return _normalize_list(await fetch_json(client, "/pcc/notes", {"patient_id": patient_internal_id}))


async def get_assessments(client: httpx.AsyncClient, patient_internal_id: int) -> list[dict]:
    return _normalize_list(await fetch_json(client, "/pcc/assessments", {"patient_id": patient_internal_id}))
