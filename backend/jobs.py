"""Background pipeline jobs — auto-sync, progress tracking, SSE-friendly state."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from backend.cache import bump_data_version, invalidate_stats_cache
from backend.pipeline import get_incremental_since, mark_stale_sync_runs, run_decide, run_extract, run_sync

_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "phase": None,
    "started_at": None,
    "finished_at": None,
    "message": None,
    "error": None,
    "progress_pct": 0,
}
_listeners: list[Callable[[], None]] = []
_sync_timer: threading.Timer | None = None
AUTO_SYNC = os.getenv("AUTO_SYNC", "true").lower() in ("1", "true", "yes")
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def subscribe(listener: Callable[[], None]) -> None:
    _listeners.append(listener)


def unsubscribe(listener: Callable[[], None]) -> None:
    try:
        _listeners.remove(listener)
    except ValueError:
        pass


def _notify() -> None:
    bump_data_version()
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass


def get_pipeline_status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def _set(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)
    _notify()


def _schedule_next_sync() -> None:
    global _sync_timer
    if SYNC_INTERVAL <= 0:
        return
    _sync_timer = threading.Timer(SYNC_INTERVAL, lambda: start_pipeline(incremental=True))
    _sync_timer.daemon = True
    _sync_timer.start()


def start_pipeline(incremental: bool = True) -> bool:
    with _lock:
        if _state["status"] == "running":
            return False

    def _run() -> None:
        try:
            _set(
                status="running",
                phase="sync",
                started_at=_now(),
                finished_at=None,
                error=None,
                progress_pct=5,
                message="Fetching patients from API…",
            )
            since = get_incremental_since() if incremental else None

            def on_progress(message: str, pct: int) -> None:
                _set(progress_pct=pct, message=message)

            asyncio.run(
                run_sync(since=since, on_progress=on_progress, ignore_watermarks=not incremental)
            )

            _set(phase="extract", progress_pct=55, message="Parsing wound notes and assessments…")
            run_extract()

            _set(phase="decide", progress_pct=85, message="Applying eligibility rules…")
            run_decide()

            invalidate_stats_cache()
            _set(
                status="complete",
                phase=None,
                progress_pct=100,
                finished_at=_now(),
                message="All patients updated.",
            )
            time.sleep(3)
            _set(status="idle", progress_pct=0, message=None)
        except Exception as e:
            _set(
                status="failed",
                phase=None,
                progress_pct=0,
                finished_at=_now(),
                error=str(e),
                message=f"Sync failed: {e}",
            )
            time.sleep(8)
            _set(status="idle", message=None, error=None)
        finally:
            _schedule_next_sync()

    threading.Thread(target=_run, daemon=True).start()
    return True


def start_auto_sync_on_boot() -> None:
    """Kick off incremental sync when server starts; schedule recurring syncs."""
    if not AUTO_SYNC:
        return

    def _boot() -> None:
        time.sleep(1.5)
        stale = mark_stale_sync_runs()
        if stale:
            print(f"Marked {stale} interrupted sync run(s) from previous process")
        if not start_pipeline(incremental=True):
            _schedule_next_sync()

    threading.Thread(target=_boot, daemon=True).start()
