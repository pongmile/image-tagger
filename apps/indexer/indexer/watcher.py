"""Auto-mode filesystem watcher — spec §7.

watchdog observers on each enabled include root. On create/modify -> check scope
-> upsert a pending `files` row + enqueue ingest. On delete -> remove row
(cascade). On move -> update path (or drop if moved out of scope).

The watcher only *enqueues*; the worker (worker.py) does the heavy ingest, so
filesystem events stay cheap and never block on hashing/thumbnailing.
"""
from __future__ import annotations

import os

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import db
from .config import SUPPORTED_EXT
from .scope import load_scope


def _is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXT


class _Handler(FileSystemEventHandler):
    def __init__(self, con):
        self.con = con
        self.scope = load_scope(con)

    def _enqueue(self, path: str, kind: str) -> None:
        if not _is_image(path) or not self.scope.is_included(path):
            return
        fid = db.get_file_id(self.con, path)
        if fid is None:
            with self.con:
                cur = self.con.execute(
                    """INSERT INTO files (path, filename, folder, sha256, mtime,
                       index_status) VALUES (?,?,?,?,?, 'pending')""",
                    (path, os.path.basename(path), os.path.dirname(path), "",
                     int(os.stat(path).st_mtime) if os.path.exists(path) else 0),
                )
                fid = cur.lastrowid
        db.enqueue_job(self.con, fid, kind)

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue(event.src_path, "ingest")

    def on_modified(self, event):
        if not event.is_directory:
            self._enqueue_if_changed(event.src_path)

    def _enqueue_if_changed(self, path: str) -> None:
        """Windows' ReadDirectoryChangesW (what watchdog polls) fires a plain
        "modified" event for metadata-only touches too — not just real content
        edits. Network/cloud-synced drives (Google Drive, OneDrive, SMB
        shares, ...) are especially prone to this: a periodic re-verification
        pass can touch every file's attributes with no content change at all,
        which used to queue a full reindex for the entire library on every
        such pass. Compare against the stored mtime/size first, the same way
        rescan() already does, and only enqueue when something really
        changed."""
        if not _is_image(path) or not self.scope.is_included(path):
            return
        fid = db.get_file_id(self.con, path)
        if fid is None:
            self._enqueue(path, "ingest")
            return
        try:
            st = os.stat(path)
        except OSError:
            return
        row = self.con.execute(
            "SELECT mtime, size_bytes FROM files WHERE id=?", (fid,)).fetchone()
        if row and row["mtime"] == int(st.st_mtime) and row["size_bytes"] == st.st_size:
            return
        db.enqueue_job(self.con, fid, "reindex")

    def on_deleted(self, event):
        if not event.is_directory:
            db.delete_file(self.con, event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        db.delete_file(self.con, event.src_path)
        self._enqueue(event.dest_path, "ingest")


def start_watchers(con) -> Observer:
    """Attach observers to every enabled include root. Returns the running
    Observer; caller stops it. Watchers attach only to include roots (§7.0)."""
    scope = load_scope(con)
    obs = Observer()
    handler = _Handler(con)
    for root in scope.enabled_include_roots():
        if os.path.isdir(root.path):
            obs.schedule(handler, root.path, recursive=root.recursive)
    obs.start()
    return obs
