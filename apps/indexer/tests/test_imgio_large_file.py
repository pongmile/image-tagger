"""Large-file guard on the cv2 raw-bytes fallback — spec §12 video scope,
verification discipline §16.

Video rows deliberately flow through the same `open_oriented()` used by every
image facet (§12: "no extra branching" — a video is just expected to fail as
"not a readable image" and get skipped). Pillow rejects a video's header
almost instantly. The cv2 fallback that used to run next, though, called
`np.fromfile(source, ...)`, which reads the *entire* file into memory before
`cv2.imdecode()` gets a look at it — for a real library that indexes personal
video alongside photos, some of those files are 15-20+ GB, so every reindex
quietly spent minutes per video copying gigabytes it was always going to
throw away (cv2.imdecode cannot demux video no matter how much of the file it
is handed).

Verified here, without depending on wall-clock timing (unreliable, and slow
on purpose is the very thing being fixed): a file above the size ceiling never
reaches `numpy.fromfile` at all, while a file at or below it still gets the
exact same fallback attempt as before — so an oddly-encoded but genuine small
image keeps its one real chance at cv2 decoding it.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_imgio_large_file
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _sparse_file(path: Path, size: int) -> None:
    """Create a `size`-byte file cheaply: a single byte at the target offset
    on NTFS leaves the rest sparse (unallocated), so this costs no meaningful
    disk space or time even at multi-hundred-MB sizes -- unlike the bug this
    test proves is gone, which really did read every byte."""
    with open(path, "wb") as f:
        if size > 0:
            f.seek(size - 1)
            f.write(b"\0")


def run() -> int:
    import numpy as np
    from indexer.imgio import ImageDecodeError, open_oriented, _CV2_FALLBACK_MAX_BYTES

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_imgio_"))

    calls: list[tuple] = []
    original_fromfile = np.fromfile

    def tracking_fromfile(*args, **kwargs):
        calls.append(args)
        return original_fromfile(*args, **kwargs)

    np.fromfile = tracking_fromfile
    try:
        # A "video" well over the ceiling. Header is all zero bytes -- not a
        # recognisable image to Pillow -- but real video files fail Pillow's
        # sniff just as fast, so this exercises the exact same branch.
        huge = tmp / "huge.mp4"
        _sparse_file(huge, _CV2_FALLBACK_MAX_BYTES + 1024)
        calls.clear()
        raised = False
        try:
            open_oriented(str(huge))
        except ImageDecodeError:
            raised = True
        check(raised, "a file over the size ceiling still raises ImageDecodeError")
        check(calls == [],
              f"and never touches numpy.fromfile doing it (got {len(calls)} call(s))")

        # Right at the boundary: unchanged behaviour, cv2 still gets its shot.
        boundary = tmp / "boundary.bin"
        _sparse_file(boundary, _CV2_FALLBACK_MAX_BYTES)
        calls.clear()
        raised = False
        try:
            open_oriented(str(boundary))
        except ImageDecodeError:
            raised = True
        check(raised, "a file exactly at the ceiling still raises ImageDecodeError")
        check(len(calls) == 1,
              f"and still gets the normal cv2 fallback attempt (got {len(calls)} call(s))")

        # An ordinary small non-image file: identical behaviour to before this
        # change existed at all -- the fix must not narrow the fallback for
        # anything that isn't genuinely huge.
        small = tmp / "small.bin"
        small.write_bytes(b"not an image" * 100)
        calls.clear()
        raised = False
        try:
            open_oriented(str(small))
        except ImageDecodeError:
            raised = True
        check(raised, "an ordinary small non-image file still raises ImageDecodeError")
        check(len(calls) == 1,
              f"and still gets the normal cv2 fallback attempt (got {len(calls)} call(s))")
    finally:
        np.fromfile = original_fromfile

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — large-file cv2-fallback guard verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
