"""Live "what is the daemon actually doing right now" status (§12 visibility).

Separate module for the same reason as heartbeat.py: both daemon.py and
worker.py need it, and daemon.py -> worker.py already goes one direction.
Tracks the current facet/file being processed and this process's own memory
use, so the UI can show something better than a silent, unexplained RAM
number in Task Manager while a multi-GB model sits loaded in memory.
"""
from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_current = "idle"


def set(text: str) -> None:
    global _current
    with _lock:
        _current = text


def set_idle() -> None:
    set("idle")


def get() -> str:
    with _lock:
        return _current


def rss_mb() -> float | None:
    """This process's resident memory in MB, or None if unavailable.

    Best-effort: psutil is an existing dependency (pulled in by accelerate),
    but this must never fail a progress event just because memory reporting
    doesn't work in some environment.
    """
    try:
        import psutil
        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
    except Exception:
        return None
