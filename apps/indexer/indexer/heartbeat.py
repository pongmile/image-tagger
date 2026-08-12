"""Shared liveness clock for the background worker loop.

A plain module-level timestamp + two functions, kept out of daemon.py so
worker.py (which daemon.py imports) can pet it from inside a long-running job
without a circular import. daemon.py's "heartbeat" RPC command reports the
age since the last beat; the Electron side (indexer.js) polls that RPC and
restarts the daemon only if the age is implausibly large — i.e. the worker
loop is truly wedged (e.g. a corrupt image jamming a native decode call), not
just doing legitimate, if slow, work such as a cold model load.
"""
from __future__ import annotations

import time

_last: float | None = None


def beat() -> None:
    global _last
    _last = time.time()


def last() -> float | None:
    return _last
