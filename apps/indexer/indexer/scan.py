"""Manual rescan — spec §7 (manual mode).

Walk the enabled include roots, apply scope rules (§7.0), diff against the
`files` table (by path + mtime), and enqueue an ingest job for every new or
changed file. Also drops rows for files that vanished or fell out of scope.
Idempotent: unchanged files are skipped.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import db
from .config import SUPPORTED_EXT
from .scope import Root, _is_under, load_scope, iter_scoped_files


@dataclass
class ScanResult:
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    revived: int = 0

    def __str__(self) -> str:
        return (f"added={self.added} changed={self.changed} "
                f"removed={self.removed} unchanged={self.unchanged} "
                f"revived={self.revived}")


def rescan(con, *, enqueue: bool = True, root_id: int | None = None,
           priority: int = 0) -> ScanResult:
    scope = load_scope(con)
    res = ScanResult()

    target = None
    roots = None
    if root_id is not None:
        target = con.execute(
            "SELECT path, mode, recursive, enabled FROM roots WHERE id=?",
            (root_id,),
        ).fetchone()
        if target is None:
            raise ValueError(f"source root {root_id} not found")
        if target["mode"] != "include":
            raise ValueError("exclude sources cannot be scanned")
        if not target["enabled"]:
            raise ValueError("source is disabled")
        roots = [Root(path=target["path"], mode="include",
                      recursive=bool(target["recursive"]), enabled=True)]

    on_disk: set[str] = set()
    for path in iter_scoped_files(scope, SUPPORTED_EXT, roots=roots):
        on_disk.add(path)
        row = con.execute(
            "SELECT id, mtime FROM files WHERE path=?", (path,)
        ).fetchone()
        try:
            mtime = int(os.stat(path).st_mtime)
        except OSError:
            continue
        if row is None:
            # New file: create a pending row + ingest job.
            with con:
                cur = con.execute(
                    """INSERT INTO files (path, filename, folder, sha256, mtime,
                       index_status) VALUES (?,?,?,?,?,'pending')""",
                    (path, os.path.basename(path), os.path.dirname(path),
                     "", mtime),
                )
                fid = cur.lastrowid
            if enqueue:
                db.enqueue_job(con, fid, "ingest", priority=priority)
            res.added += 1
        elif row["mtime"] != mtime:
            if enqueue:
                db.enqueue_job(con, row["id"], "reindex", priority=priority)
            res.changed += 1
        else:
            res.unchanged += 1

    # Remove rows for files no longer present/in-scope -- but only under a
    # root this walk is confident it actually reached. A network/cloud drive
    # (SMB share, Google Drive/OneDrive virtual mount, ...) can go briefly
    # unresponsive mid-walk with no exception raised at all (os.walk() and
    # Path.exists() both just swallow the OSError and report "not found"),
    # which used to look identical to "every file under this root was
    # deleted" -- cascade-deleting all of their tags/captions/faces the
    # instant a drive so much as blinks. Compare what this pass actually
    # found under each root against what the DB already knew about it: a
    # near-total drop is a scan failure, not real deletions, so skip removal
    # for that root entirely this pass rather than guess.
    include_roots = roots if roots is not None else scope.enabled_include_roots()
    known = con.execute("SELECT path FROM files").fetchall()
    unsafe_roots: list[str] = []
    # Require both a big relative drop AND a meaningful absolute one: a root
    # with only a handful of known files (e.g. a small folder where the user
    # genuinely just deleted the one file in it) can legitimately go from
    # "found some" to "found none" as normal, expected behavior -- the ratio
    # alone can't tell that apart from a drive hiccup at small N. The failure
    # this guards against only matters at real scale (thousands of files
    # vanishing from one scan), so an absolute floor keeps small, ordinary
    # deletions working exactly as before.
    MIN_UNEXPLAINED_DROP = 5
    for ir in include_roots:
        known_under = sum(1 for k in known if _is_under(k["path"], ir.path))
        if known_under == 0:
            continue
        found_under = sum(1 for p in on_disk if _is_under(p, ir.path))
        if found_under < known_under * 0.5 and known_under - found_under >= MIN_UNEXPLAINED_DROP:
            unsafe_roots.append(ir.path)
            import sys
            print(f"rescan: '{ir.path}' only found {found_under}/{known_under} "
                  f"previously-known files -- treating as an unreachable/failed "
                  f"scan for this root and skipping removal detection under it "
                  f"(a real drive/share hiccup, not real deletions)",
                  file=sys.stderr)

    for r in known:
        p = r["path"]
        if target is not None:
            if not _is_under(p, target["path"]):
                continue
            if not target["recursive"] and os.path.normcase(os.path.dirname(p)) != \
                    os.path.normcase(os.path.normpath(target["path"])):
                continue
        if any(_is_under(p, u) for u in unsafe_roots):
            continue
        if p not in on_disk and (not os.path.exists(p) or not scope.is_included(p)):
            db.delete_file(con, p)
            res.removed += 1

    # Belt-and-suspenders recovery (§7 resilience): a file can be left
    # index_status='pending' with no live job behind it — e.g. a rare crash/
    # bug window between enqueue_job() and the job actually landing, or a
    # daemon killed by the heartbeat watchdog mid-transaction. Its mtime is
    # unchanged so the loop above never touches it; without this, "Rescan" or
    # "Reindex" would look like they did nothing for that file forever, with
    # no user-facing way to unstick it short of the app fully restarting.
    # Deliberately library-wide, not scoped to `target`/root_id — a stuck file
    # should get unstuck by *any* rescan action, not just one that happens to
    # touch its own root.
    if enqueue:
        stuck = con.execute(
            """SELECT id FROM files WHERE index_status='pending'
                 AND id NOT IN (SELECT file_id FROM jobs
                                 WHERE file_id IS NOT NULL
                                   AND state IN ('queued','running'))"""
        ).fetchall()
        for r in stuck:
            db.enqueue_job(con, r["id"], "reindex", priority=priority)
            res.revived += 1

    return res
