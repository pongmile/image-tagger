"""Write-side DB access — spec §4, §6, §7, §9.

The indexer NEVER serves reads to the UI; the Electron main process reads the
same file directly via better-sqlite3. This module owns all *writes*: file
upsert, metadata, tags/categories, and the per-file FTS refresh that keeps the
Everything-fast search index current.

Every mutating op that changes a file's searchable text calls refresh_fts() for
that file, in the same transaction (§7: "single transaction per file").
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path

from .config import DB_PATH

def _schema_path() -> Path:
    """Resolve the shared schema in both source and packaged layouts.

    Source:   <repo>/apps/indexer/indexer/db.py -> <repo>/packages/db/schema.sql
    Packaged: <resources>/indexer/indexer/db.py -> <resources>/db/schema.sql
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "packages" / "db" / "schema.sql",
        here.parents[2] / "db" / "schema.sql",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    tried = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(f"Image Tagger database schema not found; tried: {tried}")


SCHEMA = _schema_path()


def connect(db_path: Path = DB_PATH, *, check_same_thread: bool = True
            ) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")  # tolerate Electron + worker writers
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.row_factory = sqlite3.Row
    # Forward-compatible migration for libraries created before interactive
    # reindex/rescan jobs had priority over a long background queue.
    columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)").fetchall()}
    if "priority" not in columns:
        try:
            con.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            con.commit()
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    # Forward-compatible migration: durable "user confirmed this suggestion"
    # marker, added after file_tags already existed in deployed libraries.
    ft_columns = {row[1] for row in con.execute("PRAGMA table_info(file_tags)").fetchall()}
    if "confirmed_at" not in ft_columns:
        try:
            con.execute("ALTER TABLE file_tags ADD COLUMN confirmed_at INTEGER")
            con.commit()
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    return con


def _now() -> int:
    return int(time.time())


# --- App settings (per-library key/value, incl. the models directory) --------

def get_setting(con, key: str, default=None):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con, key: str, value) -> None:
    with con:
        con.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def flag(con, key: str, default: bool = True) -> bool:
    """Read a boolean opt-in setting (§5.1 per-source toggles). Anything but an
    explicit '0'/'false'/'off' counts as on, so a missing setting = default."""
    v = get_setting(con, key)
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "off", "no", "")


def get_models_dir(con) -> Path:
    """Resolve where model files live. Precedence (§12, user-selectable folder):
      1. env IMAGE_TAGGER_MODELS_DIR   (dev/test override)
      2. settings 'models_dir'         (user's chosen folder, persisted)
      3. default <app home>/models
    """
    import os
    from .config import MODELS_DIR
    env = os.environ.get("IMAGE_TAGGER_MODELS_DIR")
    if env:
        return Path(env)
    s = get_setting(con, "models_dir")
    return Path(s) if s else MODELS_DIR


def model_dir(con, key: str) -> Path:
    """Directory for one model `key` (e.g. 'wd14'). A per-model explicit override
    env (IMAGE_TAGGER_WD14_DIR) wins; otherwise it's <models_dir>/<key>."""
    import os
    explicit = os.environ.get(f"IMAGE_TAGGER_{key.upper()}_DIR")
    if explicit:
        return Path(explicit)
    return get_models_dir(con) / key


# --- Categories & tags ------------------------------------------------------

def get_or_create_category(con, name: str, *, color: str | None = None,
                           is_builtin: int = 0) -> int:
    row = con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "INSERT INTO categories (name, color, is_builtin) VALUES (?,?,?)",
        (name, color, is_builtin),
    )
    return cur.lastrowid


def get_or_create_tag(con, name: str, category: str) -> int:
    cat_id = get_or_create_category(con, category)
    row = con.execute(
        "SELECT id FROM tags WHERE name=? AND category_id=?", (name, cat_id)
    ).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "INSERT INTO tags (name, category_id) VALUES (?,?)", (name, cat_id)
    )
    return cur.lastrowid


# --- File upsert (ingest) ---------------------------------------------------

def get_file_id(con, path: str) -> int | None:
    row = con.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
    return row["id"] if row else None


def upsert_file(con, ing) -> int:
    """Insert or update a `files` row + its metadata + path-derived tags, then
    refresh its FTS row — all in one transaction. Returns the file id."""
    with con:  # transaction
        fid = get_file_id(con, ing.path)
        if fid is None:
            cur = con.execute(
                """INSERT INTO files
                   (path, filename, folder, sha256, phash, mime, width, height,
                    size_bytes, mtime, index_status, indexed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,'done',?)""",
                (ing.path, ing.filename, ing.folder, ing.sha256, ing.phash,
                 ing.mime, ing.width, ing.height, ing.size_bytes, ing.mtime,
                 _now()),
            )
            fid = cur.lastrowid
        else:
            con.execute(
                """UPDATE files SET filename=?, folder=?, sha256=?, phash=?,
                   mime=?, width=?, height=?, size_bytes=?, mtime=?,
                   index_status='done', indexed_at=? WHERE id=?""",
                (ing.filename, ing.folder, ing.sha256, ing.phash, ing.mime,
                 ing.width, ing.height, ing.size_bytes, ing.mtime, _now(), fid),
            )

        con.execute("DELETE FROM file_metadata WHERE file_id=?", (fid,))
        if ing.metadata:
            con.executemany(
                "INSERT OR REPLACE INTO file_metadata (file_id,key,value) VALUES (?,?,?)",
                [(fid, k, v) for k, v in ing.metadata.items()],
            )

        # Path-derived tags: source='path', low confidence, kept separate (§5.1).
        # Opt-in per source: when 'tag_from_path' is off, we still clear stale
        # path tags (so toggling off removes them on reindex) but write none.
        con.execute(
            "DELETE FROM file_tags WHERE file_id=? AND source='path'", (fid,)
        )
        if flag(con, "tag_from_path", True):
            for category, name in ing.path_tags:
                tag_id = get_or_create_tag(con, name, category)
                con.execute(
                    """INSERT OR IGNORE INTO file_tags (file_id,tag_id,source,confidence)
                       VALUES (?,?,'path',0.3)""",
                    (fid, tag_id),
                )

        # Embedded metadata as searchable text (§5.1) is opt-in too.
        mt = ing.meta_text if flag(con, "tag_from_metadata", True) else ""
        refresh_fts(con, fid, meta_text=mt)
    return fid


# --- Manual tagging (§9) ----------------------------------------------------

def add_manual_tag(con, file_id: int, name: str, category: str) -> None:
    """Add a manual tag (source='manual', confidence=NULL) and refresh FTS."""
    with con:
        tag_id = get_or_create_tag(con, name, category)
        con.execute(
            """INSERT INTO file_tags (file_id,tag_id,source,confidence)
               VALUES (?,?,'manual',NULL)
               ON CONFLICT(file_id,tag_id)
               DO UPDATE SET source='manual', confidence=NULL""",
            (file_id, tag_id),
        )
        # The user is explicitly asking for this tag now — a past rejection
        # (§9 "×" on a wrong auto-tag) should no longer suppress it.
        con.execute(
            "DELETE FROM rejected_tags WHERE file_id=? AND tag_id=?", (file_id, tag_id)
        )
        refresh_fts(con, file_id)


def remove_tag(con, file_id: int, name: str, category: str) -> None:
    with con:
        row = con.execute(
            """SELECT t.id FROM tags t JOIN categories c ON c.id=t.category_id
               WHERE t.name=? AND c.name=?""", (name, category)
        ).fetchone()
        if row:
            con.execute(
                "DELETE FROM file_tags WHERE file_id=? AND tag_id=?",
                (file_id, row["id"]),
            )
        refresh_fts(con, file_id)


def rename_tag(con, category: str, old_name: str, new_name: str) -> dict:
    """Rename a tag, or MERGE it into an existing tag if the new name already
    exists in that category (§9). Every file carrying the old tag is moved to the
    new one (keeping the strongest source), the old tag is deleted, and the FTS
    rows of all affected files are rebuilt. Returns a summary."""
    new_name = new_name.strip()
    if not new_name:
        return {"ok": False, "error": "empty name"}
    old = con.execute(
        """SELECT t.id FROM tags t JOIN categories c ON c.id=t.category_id
           WHERE t.name=? AND c.name=?""", (old_name, category)
    ).fetchone()
    if old is None:
        return {"ok": False, "error": "tag not found"}
    old_id = old["id"]
    cat_id = get_or_create_category(con, category)
    existing = con.execute(
        "SELECT id FROM tags WHERE name=? AND category_id=?", (new_name, cat_id)
    ).fetchone()

    with con:
        if existing is None:
            # Pure rename — no collision, just relabel the tag row.
            con.execute("UPDATE tags SET name=? WHERE id=?", (new_name, old_id))
            affected = [r["file_id"] for r in con.execute(
                "SELECT file_id FROM file_tags WHERE tag_id=?", (old_id,)).fetchall()]
            merged = False
        else:
            # Merge — repoint file_tags to the target, dedupe, drop the old tag.
            new_id = existing["id"]
            affected = [r["file_id"] for r in con.execute(
                "SELECT file_id FROM file_tags WHERE tag_id IN (?,?)",
                (old_id, new_id)).fetchall()]
            con.execute(
                """INSERT OR IGNORE INTO file_tags (file_id,tag_id,source,confidence)
                   SELECT file_id, ?, source, confidence FROM file_tags WHERE tag_id=?""",
                (new_id, old_id),
            )
            con.execute("DELETE FROM file_tags WHERE tag_id=?", (old_id,))
            con.execute("DELETE FROM tags WHERE id=?", (old_id,))
            merged = True
        for fid in set(affected):
            refresh_fts(con, fid)
    return {"ok": True, "merged": merged, "files": len(set(affected)),
            "name": new_name}


def list_all_tags(con, limit: int = 2000) -> list[dict]:
    """All tags with their category + file count, for a tag-management view (§9)."""
    rows = con.execute(
        """SELECT t.id, t.name, c.name AS category, count(ft.file_id) AS files
             FROM tags t JOIN categories c ON c.id=t.category_id
             LEFT JOIN file_tags ft ON ft.tag_id=t.id
            GROUP BY t.id ORDER BY files DESC, c.name, t.name LIMIT ?""",
        (limit,),
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "category": r["category"],
             "files": r["files"]} for r in rows]


# --- FTS refresh (the fast-path index, §6/§8) -------------------------------

def _tags_text(con, file_id: int) -> str:
    rows = con.execute(
        "SELECT t.name FROM file_tags ft JOIN tags t ON t.id=ft.tag_id "
        "WHERE ft.file_id=?", (file_id,)
    ).fetchall()
    names = [r["name"] for r in rows]
    # Named persons on this file are searchable by name too (§5/§8).
    persons = con.execute(
        "SELECT DISTINCT p.name FROM faces f JOIN persons p ON p.id=f.person_id "
        "WHERE f.file_id=? AND p.name IS NOT NULL", (file_id,)
    ).fetchall()
    names.extend(r["name"] for r in persons)
    return " ".join(names)


def _meta_text(con, file_id: int) -> str:
    # Reconstruct meta_text from stored rows when not supplied by caller.
    if not flag(con, "tag_from_metadata", True):
        return ""  # §5.1 opt-out: keep metadata out of the search index
    from . import metadata as meta
    md = {r["key"]: r["value"] for r in con.execute(
        "SELECT key,value FROM file_metadata WHERE file_id=?", (file_id,)
    ).fetchall()}
    return meta.meta_text(md)


def refresh_fts(con, file_id: int, *, meta_text: str | None = None) -> None:
    """Rebuild the contentless files_fts row for one file from current sources.
    Contentless FTS5 needs an explicit delete-then-insert to update a rowid."""
    f = con.execute(
        "SELECT path, filename, folder, caption, ocr_text FROM files WHERE id=?",
        (file_id,)
    ).fetchone()
    if f is None:
        return
    tags_text = _tags_text(con, file_id)
    mt = meta_text if meta_text is not None else _meta_text(con, file_id)
    con.execute("DELETE FROM files_fts WHERE rowid=?", (file_id,))
    con.execute(
        """INSERT INTO files_fts
           (rowid, path, filename, folder, tags_text, meta_text, caption, ocr_text)
           VALUES (?,?,?,?,?,?,?,?)""",
        (file_id, f["path"], f["filename"], f["folder"], tags_text, mt,
         f["caption"] or "", f["ocr_text"] or ""),
    )


# --- Auto tags (wd14 / clip / learned ...) ----------------------------------

def write_auto_tags(con, file_id: int, source: str, tags) -> None:
    """Replace all tags from one auto `source` on a file with a fresh set, then
    refresh FTS. `tags` is an iterable of objects with .category/.name/.confidence
    (e.g. models.wd14.TagResult). Manual/path tags are untouched — only rows of
    this exact source are rewritten, so re-tagging is idempotent.

    Tags the user previously removed as wrong (rejected_tags, §9) are skipped —
    otherwise every reindex/rescan would silently resurrect a tag right after
    the user deleted it."""
    suppressed = {r["tag_id"] for r in con.execute(
        "SELECT tag_id FROM rejected_tags WHERE file_id=?", (file_id,)
    ).fetchall()}
    with con:
        con.execute(
            "DELETE FROM file_tags WHERE file_id=? AND source=?", (file_id, source)
        )
        for t in tags:
            tag_id = get_or_create_tag(con, t.name, t.category)
            if tag_id in suppressed:
                continue
            con.execute(
                """INSERT INTO file_tags (file_id,tag_id,source,confidence)
                   VALUES (?,?,?,?)
                   ON CONFLICT(file_id,tag_id) DO UPDATE SET
                     confidence=excluded.confidence
                   WHERE file_tags.source=excluded.source""",
                (file_id, tag_id, source, float(t.confidence)),
            )
        refresh_fts(con, file_id)


def set_image_kind(con, file_id: int, kind: str) -> None:
    with con:
        con.execute("UPDATE files SET image_kind=? WHERE id=?", (kind, file_id))


# --- Per-model facet cache (skip re-inference when a model's output for a
# file is already known — §"per-model tag caching") -------------------------

def get_facet_cache(con, file_id: int, facet: str, model_key: str) -> dict | None:
    """The cached {"image_kind", "tags": [...]} for this exact (file, facet,
    model), or None on a cache miss. `tags` entries are plain dicts with
    category/name/confidence, ready to hand to write_auto_tags() after
    reconstructing lightweight objects (see worker.py)."""
    row = con.execute(
        "SELECT payload FROM facet_model_cache WHERE file_id=? AND facet=? AND model_key=?",
        (file_id, facet, model_key),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload"])


def set_facet_cache(con, file_id: int, facet: str, model_key: str,
                     image_kind: str | None, tags) -> None:
    """Save this model's output for a file so switching away and back restores
    it without re-running inference. Does not touch file_tags/FTS itself --
    callers already do that via write_auto_tags()."""
    payload = json.dumps({
        "image_kind": image_kind,
        "tags": [{"category": t.category, "name": t.name, "confidence": t.confidence}
                  for t in tags],
    })
    with con:
        con.execute(
            """INSERT INTO facet_model_cache (file_id, facet, model_key, payload, cached_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(file_id, facet, model_key)
               DO UPDATE SET payload=excluded.payload, cached_at=excluded.cached_at""",
            (file_id, facet, model_key, payload, int(time.time())),
        )


def set_caption(con, file_id: int, caption: str) -> None:
    """Store a natural-language caption (§11) and refresh FTS so it's searchable."""
    with con:
        con.execute("UPDATE files SET caption=? WHERE id=?", (caption, file_id))
        refresh_fts(con, file_id)


# --- CLIP zero-shot vocabulary (§5, M5) -------------------------------------

def get_clip_vocab(con) -> dict:
    """Return {category: [label, ...]} of enabled CLIP labels."""
    vocab: dict[str, list[str]] = {}
    for r in con.execute(
        "SELECT category, label FROM clip_labels WHERE enabled=1 "
        "ORDER BY category, label"
    ).fetchall():
        vocab.setdefault(r["category"], []).append(r["label"])
    return vocab


def add_clip_label(con, category: str, label: str) -> None:
    with con:
        con.execute(
            "INSERT OR IGNORE INTO clip_labels (category, label) VALUES (?,?)",
            (category, label),
        )


def remove_clip_label(con, category: str, label: str) -> None:
    with con:
        con.execute(
            "DELETE FROM clip_labels WHERE category=? AND label=?",
            (category, label),
        )


# --- Faces / persons: detect -> cluster -> name -> auto-attach (§5, M6) -----

import struct as _struct


def _emb_to_blob(emb) -> bytes:
    return _struct.pack(f"{len(emb)}f", *[float(x) for x in emb])


def _blob_to_emb(blob: bytes) -> list[float]:
    return list(_struct.unpack(f"{len(blob)//4}f", blob))


def _cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _person_centroids(con):
    """Return {person_id: (name, centroid_vec)} over all persons that have faces.
    Centroid = mean of that person's (normalized) face embeddings, renormalized."""
    rows = con.execute(
        "SELECT f.person_id, f.embedding, p.name FROM faces f "
        "JOIN persons p ON p.id=f.person_id WHERE f.person_id IS NOT NULL"
    ).fetchall()
    acc: dict[int, list] = {}
    names: dict[int, str] = {}
    for r in rows:
        pid = r["person_id"]
        names[pid] = r["name"]
        emb = _blob_to_emb(r["embedding"])
        if pid not in acc:
            acc[pid] = list(emb)
        else:
            for i, v in enumerate(emb):
                acc[pid][i] += v
    out = {}
    for pid, s in acc.items():
        n = math.sqrt(sum(v * v for v in s)) or 1.0
        out[pid] = (names[pid], [v / n for v in s])
    return out


def assign_person(con, embedding, *, threshold: float = 0.5,
                  named_bonus: float = 0.0) -> int:
    """Find the nearest existing person cluster by cosine; attach if within
    `threshold`, otherwise create a new (unnamed) person. Named clusters can be
    given a small `named_bonus` so a face prefers a confirmed identity on ties
    (§5 auto-attach to nearest *named* cluster). Returns the person_id."""
    best_pid, best_sim = None, -1.0
    for pid, (name, centroid) in _person_centroids(con).items():
        sim = _cos(embedding, centroid) + (named_bonus if name else 0.0)
        if sim > best_sim:
            best_pid, best_sim = pid, sim
    if best_pid is not None and best_sim >= threshold:
        return best_pid
    with con:
        return con.execute("INSERT INTO persons (name) VALUES (NULL)").lastrowid


def write_faces(con, file_id: int, faces, *, threshold: float = 0.5) -> list[int]:
    """Replace a file's faces: detect->assign each to a person cluster->store.
    Returns the list of person_ids assigned. Refreshes FTS so named persons on
    this file become searchable."""
    assigned: list[int] = []
    with con:
        con.execute("DELETE FROM faces WHERE file_id=?", (file_id,))
    for fc in faces:
        pid = assign_person(con, fc.embedding, threshold=threshold)
        with con:
            con.execute(
                "INSERT INTO faces (file_id, person_id, bbox, embedding) "
                "VALUES (?,?,?,?)",
                (file_id, pid, json.dumps(fc.bbox), _emb_to_blob(fc.embedding)),
            )
        assigned.append(pid)
    refresh_fts(con, file_id)
    return assigned


def name_person(con, person_id: int, name: str) -> None:
    with con:
        con.execute("UPDATE persons SET name=? WHERE id=?", (name, person_id))
    # Named person appears in FTS on every file they're in.
    for r in con.execute(
        "SELECT DISTINCT file_id FROM faces WHERE person_id=?", (person_id,)
    ).fetchall():
        refresh_fts(con, r["file_id"])


def list_persons(con) -> list[dict]:
    """Person clusters with face counts + a representative file, for the naming
    UI (§5/§15). Unnamed clusters come first (they need attention). sample_id
    lets the UI fetch a real thumbnail (via the existing file:thumb IPC)
    instead of a placeholder icon."""
    rows = con.execute(
        """SELECT p.id, p.name, count(f.id) AS faces, fi.id AS sample_id, fi.path AS sample
           FROM persons p
           LEFT JOIN faces f ON f.person_id=p.id
           LEFT JOIN files fi ON fi.id = (
               SELECT f2.file_id FROM faces f2 WHERE f2.person_id=p.id LIMIT 1
           )
           GROUP BY p.id
           ORDER BY (p.name IS NOT NULL), faces DESC"""
    ).fetchall()
    return [dict(id=r["id"], name=r["name"], faces=r["faces"],
                 sample_id=r["sample_id"], sample=r["sample"]) for r in rows]


def person_files(con, person_id: int) -> list[dict]:
    rows = con.execute(
        """SELECT DISTINCT fi.id, fi.path, fi.filename FROM faces f
           JOIN files fi ON fi.id=f.file_id WHERE f.person_id=? LIMIT 500""",
        (person_id,)).fetchall()
    return [dict(id=r["id"], path=r["path"], filename=r["filename"]) for r in rows]


def merge_persons(con, src_id: int, dst_id: int) -> None:
    """Fold cluster src into dst (the re-cluster/merge tool of §15)."""
    with con:
        con.execute("UPDATE faces SET person_id=? WHERE person_id=?",
                    (dst_id, src_id))
        con.execute("DELETE FROM persons WHERE id=?", (src_id,))
    for r in con.execute(
        "SELECT DISTINCT file_id FROM faces WHERE person_id=?", (dst_id,)
    ).fetchall():
        refresh_fts(con, r["file_id"])


# --- OCR results (§10) ------------------------------------------------------

def write_ocr(con, file_id: int, regions) -> None:
    """Replace a file's OCR regions and refresh its concatenated `ocr_text`
    (the FTS-searchable form), all in one transaction. `regions` is a list of
    models.ocr.Region."""
    import json
    with con:
        con.execute("DELETE FROM ocr_regions WHERE file_id=?", (file_id,))
        for r in regions:
            con.execute(
                """INSERT INTO ocr_regions (file_id, text, lang, bbox, confidence)
                   VALUES (?,?,?,?,?)""",
                (file_id, r.text, r.lang, json.dumps(r.bbox), r.confidence),
            )
        ocr_text = "\n".join(r.text for r in regions)
        con.execute("UPDATE files SET ocr_text=? WHERE id=?", (ocr_text, file_id))
        refresh_fts(con, file_id)


# --- Deletion (file left scope / removed on disk) ---------------------------

def delete_file(con, path: str) -> None:
    fid = get_file_id(con, path)
    if fid is None:
        return
    from . import vec
    vec.delete(con, fid)  # drop the CLIP embedding (its own vec0 table, no cascade)
    with con:
        con.execute("DELETE FROM files_fts WHERE rowid=?", (fid,))
        con.execute("DELETE FROM files WHERE id=?", (fid,))  # cascades the rest


# --- Roots / exclude management (§7.0) --------------------------------------

def add_root(con, path: str, mode: str = "include", recursive: bool = True) -> None:
    with con:
        con.execute(
            """INSERT INTO roots (path, mode, recursive, enabled, added_at)
               VALUES (?,?,?,1,?)
               ON CONFLICT(path) DO UPDATE SET mode=excluded.mode,
                 recursive=excluded.recursive, enabled=1""",
            (path, mode, 1 if recursive else 0, _now()),
        )


def add_exclude_pattern(con, pattern: str) -> None:
    with con:
        con.execute(
            "INSERT OR IGNORE INTO exclude_rules (pattern, enabled) VALUES (?,1)",
            (pattern,),
        )


def list_roots(con) -> list[dict]:
    """Every include/exclude scope root, with live index status per root (§7.0/§12):
    total files under it, how many are fully indexed, how many still pending, and
    when it was last indexed — so the UI can show 'D:\\Pictures — include · 12,431
    files · 12,400 indexed · last indexed 3h ago'."""
    rows = con.execute(
        "SELECT id, path, mode, recursive, enabled FROM roots ORDER BY mode, path"
    ).fetchall()
    out = []
    for r in rows:
        like = r["path"].replace("\\", "/").rstrip("/") + "/%"
        agg = con.execute(
            """SELECT count(*) total,
                      sum(CASE WHEN index_status='done'  THEN 1 ELSE 0 END) done,
                      sum(CASE WHEN index_status='error' THEN 1 ELSE 0 END) errors,
                      max(indexed_at) last_indexed
                 FROM files WHERE REPLACE(path,'\\','/') LIKE ?""",
            (like,),
        ).fetchone()
        total = agg["total"] or 0
        done = agg["done"] or 0
        out.append({"id": r["id"], "path": r["path"], "mode": r["mode"],
                    "recursive": bool(r["recursive"]), "enabled": bool(r["enabled"]),
                    "files": total, "done": done, "pending": total - done,
                    "errors": agg["errors"] or 0,
                    "last_indexed": agg["last_indexed"]})
    return out


def remove_root(con, root_id: int) -> None:
    with con:
        con.execute("DELETE FROM roots WHERE id=?", (root_id,))


def set_root_enabled(con, root_id: int, enabled: bool) -> None:
    with con:
        con.execute("UPDATE roots SET enabled=? WHERE id=?",
                    (1 if enabled else 0, root_id))


def list_exclude_patterns(con) -> list[dict]:
    rows = con.execute(
        "SELECT id, pattern, enabled FROM exclude_rules ORDER BY id"
    ).fetchall()
    return [{"id": r["id"], "pattern": r["pattern"], "enabled": bool(r["enabled"])}
            for r in rows]


def remove_exclude_pattern(con, rule_id: int) -> None:
    with con:
        con.execute("DELETE FROM exclude_rules WHERE id=?", (rule_id,))


def set_exclude_enabled(con, rule_id: int, enabled: bool) -> None:
    with con:
        con.execute("UPDATE exclude_rules SET enabled=? WHERE id=?",
                    (1 if enabled else 0, rule_id))


def manual_tag_count(con, category: str, name: str) -> int:
    """How many files carry this tag by hand — the pool of positive
    examples available to teach a learned tag (§5.3)."""
    row = con.execute(
        """SELECT count(*) c FROM file_tags ft
             JOIN tags t ON t.id=ft.tag_id
             JOIN categories c ON c.id=t.category_id
            WHERE c.name=? AND t.name=? AND ft.source='manual'""",
        (category, name),
    ).fetchone()
    return row["c"]


def list_learned_tags(con) -> list[dict]:
    """Every few-shot learned tag with its live training state (§5.3) — powers
    the "Learned tags" transparency view so the self-training loop is visible
    rather than an implicit backend detail. Includes `space` so the UI's
    Refresh button can retrain with the same space the tag was built in."""
    rows = con.execute(
        """SELECT t.id AS tag_id, t.name, c.name AS category, lt.space, lt.method,
                  lt.n_pos, lt.n_neg, lt.threshold, lt.updated_at,
                  (SELECT count(*) FROM file_tags
                    WHERE tag_id=lt.tag_id AND source='learned') AS applied
             FROM learned_tags lt
             JOIN tags t ON t.id=lt.tag_id
             JOIN categories c ON c.id=t.category_id
            ORDER BY lt.updated_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def list_learning_progress(con) -> list[dict]:
    """Tags accumulating confirm/reject examples that haven't crossed the
    training floor yet (§5.3) -- train() silently no-ops below MIN_POSITIVES,
    so without this a user confirming a tag 1-4 times sees no visible effect
    anywhere, which reads as "confirm does nothing" rather than "not enough
    examples yet". Surfaced separately from list_learned_tags() since these
    tags have no learned_tags row at all."""
    rows = con.execute(
        """SELECT t.id AS tag_id, t.name, c.name AS category,
                  SUM(CASE WHEN te.label>0 THEN 1 ELSE 0 END) AS n_pos,
                  SUM(CASE WHEN te.label<0 THEN 1 ELSE 0 END) AS n_neg,
                  MAX(te.added_at) AS updated_at
             FROM tag_examples te
             JOIN tags t ON t.id=te.tag_id
             JOIN categories c ON c.id=t.category_id
            WHERE t.id NOT IN (SELECT tag_id FROM learned_tags)
            GROUP BY t.id
            ORDER BY n_pos DESC, updated_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def tag_learning_progress(con, tag_id: int) -> dict:
    """Positive/negative example counts + trained state for one tag (§5.3
    active-learning transparency) -- lets a single confirm/reject say exactly
    how many more are needed before it starts reinforcing recognition,
    instead of looking like a no-op until the training floor is crossed."""
    row = con.execute(
        """SELECT SUM(CASE WHEN label>0 THEN 1 ELSE 0 END) AS n_pos,
                  SUM(CASE WHEN label<0 THEN 1 ELSE 0 END) AS n_neg
             FROM tag_examples WHERE tag_id=?""",
        (tag_id,),
    ).fetchone()
    n_pos = int(row["n_pos"] or 0)
    n_neg = int(row["n_neg"] or 0)
    lt = con.execute("SELECT 1 FROM learned_tags WHERE tag_id=?", (tag_id,)).fetchone()
    applied = 0
    if lt is not None:
        applied = con.execute(
            "SELECT count(*) c FROM file_tags WHERE tag_id=? AND source='learned'",
            (tag_id,),
        ).fetchone()["c"]
    return {"n_pos": n_pos, "n_neg": n_neg, "trained": lt is not None, "applied": applied}


# --- Job queue (control channel, §4/§7) -------------------------------------

def recover_interrupted_jobs(con) -> int:
    """Return jobs abandoned by a stopped daemon to the runnable queue."""
    with con:
        rows = con.execute(
            "SELECT id, file_id FROM jobs WHERE state='running'"
        ).fetchall()
        if not rows:
            return 0
        now = _now()
        con.executemany(
            "UPDATE jobs SET state='queued', error=NULL, updated_at=? WHERE id=?",
            [(now, row["id"]) for row in rows],
        )
        file_ids = {row["file_id"] for row in rows if row["file_id"] is not None}
        con.executemany(
            "UPDATE files SET index_status='pending' WHERE id=?",
            [(file_id,) for file_id in file_ids],
        )
        return len(rows)


def enqueue_job(con, file_id: int | None, kind: str, *, priority: int = 0) -> int:
    with con:
        # Watcher events and repeated button clicks can arrive faster than the
        # worker drains them. Keep at most one live job of a kind per file --
        # including a job that previously ended in 'error': re-enqueueing (a
        # retry, a rescan, a bulk backfill sweep) is the system trying again
        # after whatever failed, so reuse and reset that row rather than
        # inserting a second one alongside it. Without this, a transient
        # failure (e.g. a missing dependency, since fixed) plus any later
        # re-enqueue leaves a duplicate queued job that never resolves the
        # dedup check in set_job_state() -- the file's real work finishes but
        # index_status can never flip to 'done' because that stale duplicate
        # is still sitting there "active". The original error stays visible
        # in list_errors() only until this reuse claims it; if the retry also
        # fails it becomes a fresh, equally visible error, so nothing is
        # silently swallowed (§7).
        if file_id is not None:
            existing = con.execute(
                """SELECT id, state FROM jobs WHERE file_id=? AND kind=?
                     AND state IN ('queued','running','error') ORDER BY id DESC LIMIT 1""",
                (file_id, kind),
            ).fetchone()
            con.execute("UPDATE files SET index_status='pending' WHERE id=?",
                        (file_id,))
            if existing:
                if existing["state"] == "error":
                    # Reuse the row as a fresh retry -- do NOT touch a job
                    # that's already 'queued' or 'running' beyond bumping its
                    # priority below; forcing those back to 'queued' could
                    # make the worker double-process a job mid-flight.
                    con.execute(
                        """UPDATE jobs SET state='queued', error=NULL,
                           priority=max(priority,?), updated_at=? WHERE id=?""",
                        (int(priority), _now(), existing["id"]),
                    )
                else:
                    con.execute(
                        "UPDATE jobs SET priority=max(priority,?), updated_at=? WHERE id=?",
                        (int(priority), _now(), existing["id"]),
                    )
                return existing["id"]
        cur = con.execute(
            """INSERT INTO jobs (file_id, kind, state, priority, created_at, updated_at)
               VALUES (?,?,'queued',?,?,?)""",
            (file_id, kind, int(priority), _now(), _now()),
        )
        return cur.lastrowid


def next_job(con):
    return con.execute(
        "SELECT * FROM jobs WHERE state='queued' ORDER BY priority DESC, id LIMIT 1"
    ).fetchone()


def set_job_state(con, job_id: int, state: str, error: str | None = None) -> None:
    with con:
        job = con.execute("SELECT file_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        con.execute(
            "UPDATE jobs SET state=?, error=?, updated_at=? WHERE id=?",
            (state, error, _now(), job_id),
        )
        if job is None or job["file_id"] is None:
            return
        fid = job["file_id"]
        if state in ("queued", "running"):
            con.execute("UPDATE files SET index_status='pending' WHERE id=?", (fid,))
        elif state == "error":
            con.execute("UPDATE files SET index_status='error' WHERE id=?", (fid,))
        elif state == "done":
            active = con.execute(
                """SELECT 1 FROM jobs WHERE file_id=?
                     AND state IN ('queued','running') LIMIT 1""", (fid,)
            ).fetchone()
            if active is None:
                con.execute(
                    "UPDATE files SET index_status='done', indexed_at=? WHERE id=?",
                    (_now(), fid),
                )


def retry_errors(con, file_id: int | None = None) -> int:
    """Re-queue errored jobs (§7: 'error files can be retried'). Resets the file
    status to pending so the progress bar reflects the retry. Returns the count."""
    with con:
        if file_id is None:
            cur = con.execute(
                "UPDATE jobs SET state='queued', error=NULL, updated_at=? "
                "WHERE state='error'", (_now(),))
            con.execute(
                "UPDATE files SET index_status='pending' WHERE index_status='error'")
        else:
            cur = con.execute(
                "UPDATE jobs SET state='queued', error=NULL, updated_at=? "
                "WHERE state='error' AND file_id=?", (_now(), file_id))
            con.execute(
                "UPDATE files SET index_status='pending' WHERE id=?", (file_id,))
    return cur.rowcount


def recaption_root(con, root_id: int, *, priority: int = 100) -> int:
    """Queue a caption-only regenerate (not a full reindex) for every indexed
    file under one root — the Sources page's per-row "Desc" action, for
    backfilling/refreshing descriptions on just one folder without re-running
    wd14/clip/faces/ocr on files that don't need it.

    Same priority=100 "interactive" tier as reindex_root — see its docstring.
    """
    r = con.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
    if r is None:
        raise ValueError(f"source root {root_id} not found")
    like = r["path"].replace("\\", "/").rstrip("/") + "/%"
    file_ids = [row["id"] for row in con.execute(
        "SELECT id FROM files WHERE REPLACE(path,'\\','/') LIKE ?", (like,)
    ).fetchall()]
    for fid in file_ids:
        enqueue_job(con, fid, "caption", priority=priority)
    return len(file_ids)


def reindex_root(con, root_id: int, *, priority: int = 100) -> int:
    """Scoped version of reindex_all: re-run ingest+infer for every indexed file
    under one source root only, so a change (new model, threshold) can be
    backfilled one folder at a time instead of the whole library.

    Priority defaults to 100 -- the same "interactive" tier as a single-file
    reindex -- because this is a deliberate, targeted click on one specific
    folder, not a background sweep. At the default priority=0 it would just
    join the tail of whatever's already queued (e.g. a library-wide backlog
    of thousands) and could sit for a very long time before starting, making
    the button feel broken even though it worked.
    """
    r = con.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
    if r is None:
        raise ValueError(f"source root {root_id} not found")
    like = r["path"].replace("\\", "/").rstrip("/") + "/%"
    file_ids = [row["id"] for row in con.execute(
        "SELECT id FROM files WHERE REPLACE(path,'\\','/') LIKE ?", (like,)
    ).fetchall()]
    for fid in file_ids:
        enqueue_job(con, fid, "reindex", priority=priority)
    return len(file_ids)


def list_errors(con, root_id: int | None = None, limit: int = 200) -> list[dict]:
    """Recent job failures with the actual exception text, not just a count
    (§7: errors must be visible, not silently swallowed). Optionally scoped to
    one source root, using the same path-prefix match as list_roots()."""
    where = "j.state='error'"
    params: list = []
    if root_id is not None:
        r = con.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
        if r is None:
            raise ValueError(f"source root {root_id} not found")
        like = r["path"].replace("\\", "/").rstrip("/") + "/%"
        where += " AND REPLACE(fi.path,'\\','/') LIKE ?"
        params.append(like)
    rows = con.execute(
        f"""SELECT j.id, j.file_id, j.kind, j.error, j.updated_at, fi.path, fi.filename
            FROM jobs j JOIN files fi ON fi.id=j.file_id
            WHERE {where}
            ORDER BY j.updated_at DESC LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return [dict(id=row["id"], file_id=row["file_id"], kind=row["kind"],
                 error=row["error"], updated_at=row["updated_at"],
                 path=row["path"], filename=row["filename"]) for row in rows]


def reindex_all(con, *, priority: int = 100) -> int:
    """Re-run ingest+infer for every indexed file (used after lowering a model's
    confidence threshold, changing a variant, etc. — a plain Rescan only picks up
    new/changed files by mtime/sha and won't reprocess unchanged ones). enqueue_job
    manages its own transaction per file (mirrors scan.py's rescan loop) and
    already dedups queued/running jobs of the same kind, so calling this twice
    before the queue drains doesn't double-enqueue. Returns the number queued.

    Same "interactive" priority=100 as its scoped sibling reindex_root(), and
    for the identical reason (see that docstring): at the old default
    priority=0, a full-library reindex silently joined the tail of whatever
    background sweep already happened to be queued -- e.g. a caption-variant
    switch's re-caption backlog -- and could sit behind thousands of older,
    unrelated jobs for the better part of an hour before doing anything
    visible. reindex_root() got this fix; reindex_all(), triggered by the same
    kind of deliberate click, had been missed.
    """
    file_ids = [r["id"] for r in con.execute("SELECT id FROM files").fetchall()]
    for fid in file_ids:
        enqueue_job(con, fid, "reindex", priority=priority)
    return len(file_ids)


def recaption_all(con, *, priority: int = 100) -> int:
    """Force-regenerate captions for every file, regardless of whether it
    already has one -- the Sources page's explicit "↻ Regen all captions"
    action (§11/§12). Unlike a caption-model switch (which now only fills in
    files with no caption at all, see daemon._set_variant), this is the
    deliberate "yes, I want every caption redone with the model I just
    picked" opt-in. Same "interactive" priority=100 tier as reindex_all(),
    for the same reason -- see its docstring.
    """
    file_ids = [r["id"] for r in con.execute("SELECT id FROM files").fetchall()]
    for fid in file_ids:
        enqueue_job(con, fid, "caption", priority=priority)
    return len(file_ids)


def enqueue_missing_clip_embeddings(con) -> int:
    """Queue inference for files that do not have a CLIP vector yet.

    Do not duplicate a live reindex/infer job: a queued reindex will chain its
    own infer job after CLIP has been enabled. This makes learned-tag training
    backfill the library without doubling an existing rescan queue.
    """
    from . import vec
    existing: set[int] = set()
    if vec.load(con):
        try:
            existing = {int(r["file_id"]) for r in
                        con.execute("SELECT file_id FROM file_vec").fetchall()}
        except Exception:
            pass
    active = {int(r["file_id"]) for r in con.execute(
        """SELECT DISTINCT file_id FROM jobs
            WHERE file_id IS NOT NULL AND state IN ('queued','running')
              AND kind IN ('reindex','infer','clip')"""
    ).fetchall()}
    wanted = [int(r["id"]) for r in con.execute("SELECT id FROM files").fetchall()
              if int(r["id"]) not in existing and int(r["id"]) not in active]
    for file_id in wanted:
        enqueue_job(con, file_id, "clip")
    return len(wanted)


def enqueue_missing_captions(con) -> int:
    """Queue caption generation for files that have no caption yet — the
    backfill run when the caption facet is first enabled (§11), so existing
    library files get a description without a full library-wide reindex.
    Same "don't duplicate a live job" guard as enqueue_missing_clip_embeddings."""
    active = {int(r["file_id"]) for r in con.execute(
        """SELECT DISTINCT file_id FROM jobs
            WHERE file_id IS NOT NULL AND state IN ('queued','running')
              AND kind IN ('reindex','infer','caption')"""
    ).fetchall()}
    wanted = [int(r["id"]) for r in con.execute(
        "SELECT id FROM files WHERE caption IS NULL OR caption=''"
    ).fetchall() if int(r["id"]) not in active]
    for file_id in wanted:
        enqueue_job(con, file_id, "caption")
    return len(wanted)


def enqueue_job_for_captioned_files(con) -> int:
    """Re-queue captioning for every file that already has one — used when the
    caption model variant changes, since an existing caption was written by
    the model the user just switched away from (§11/§12)."""
    active = {int(r["file_id"]) for r in con.execute(
        """SELECT DISTINCT file_id FROM jobs
            WHERE file_id IS NOT NULL AND state IN ('queued','running')
              AND kind IN ('reindex','infer','caption')"""
    ).fetchall()}
    wanted = [int(r["id"]) for r in con.execute(
        "SELECT id FROM files WHERE caption IS NOT NULL AND caption<>''"
    ).fetchall() if int(r["id"]) not in active]
    for file_id in wanted:
        enqueue_job(con, file_id, "caption")
    return len(wanted)


# --- Bulk manual tagging (§9) -----------------------------------------------

def bulk_add_manual_tag(con, file_ids, category: str, name: str) -> int:
    """Apply one manual tag to many files in a single transaction (§9)."""
    tag_id = get_or_create_tag(con, name, category)
    n = 0
    with con:
        for fid in file_ids:
            con.execute(
                "INSERT INTO file_tags (file_id,tag_id,source,confidence) "
                "VALUES (?,?,'manual',NULL) ON CONFLICT(file_id,tag_id) "
                "DO UPDATE SET source='manual', confidence=NULL", (fid, tag_id))
            refresh_fts(con, fid)
            n += 1
    return n


def bulk_remove_tag(con, file_ids, category: str, name: str) -> int:
    row = con.execute(
        "SELECT t.id FROM tags t JOIN categories c ON c.id=t.category_id "
        "WHERE t.name=? AND c.name=?", (name, category)).fetchone()
    if not row:
        return 0
    n = 0
    with con:
        for fid in file_ids:
            con.execute("DELETE FROM file_tags WHERE file_id=? AND tag_id=?",
                        (fid, row["id"]))
            refresh_fts(con, fid)
            n += 1
    return n


def progress(con) -> dict:
    """Counts for the UI progress bar (§7/§12)."""
    rows = con.execute(
        "SELECT state, count(*) c FROM jobs GROUP BY state"
    ).fetchall()
    by_state = {r["state"]: r["c"] for r in rows}
    total = con.execute("SELECT count(*) c FROM files").fetchone()["c"]
    done = con.execute(
        "SELECT count(*) c FROM files WHERE index_status='done'"
    ).fetchone()["c"]
    # Per-stage counts for the split Scan/Tag/Caption progress bars (§12).
    #
    # All four are counted in *files*, deliberately: the earlier single bar
    # mixed file counts with job counts, which made "7,503 files" sit next to
    # "13,565 jobs" with no way to tell they measured different things.
    #
    # They are derived from stored output rather than the jobs table because
    # finished jobs are never deleted -- an ingest bar computed as
    # done/(done+queued) sits at 99.9% forever and shows nothing. sha256 is
    # written by ingest() (scan.py inserts a placeholder row with sha256='')
    # so it marks "this file has actually been read", and WD14/caption have
    # no job kind of their own at all -- they run as sub-steps inside one
    # 'infer' job -- so output existence is the only thing that can measure
    # them.
    # Files still waiting on the queue. Reported separately from files_done
    # because the two answer different questions and were being read as if
    # they answered the same one: index_status tracks freshness *relative to
    # the queue*, so one "Reindex all" click resets nearly every row to
    # pending and files_done collapses to ~0 even though the library still
    # has tags and captions on 6,000+ files. Shown to the user as "N queued"
    # rather than as a done/total ratio, so it cannot be mistaken for the
    # coverage bars below.
    files_pending = con.execute(
        "SELECT count(*) c FROM files WHERE index_status<>'done'"
    ).fetchone()["c"]
    scan_done = con.execute(
        "SELECT count(*) c FROM files WHERE sha256<>''"
    ).fetchone()["c"]
    caption_done = con.execute(
        "SELECT count(*) c FROM files WHERE caption IS NOT NULL AND caption<>''"
    ).fetchone()["c"]
    tag_done = con.execute(
        "SELECT count(*) c FROM files WHERE image_kind IS NOT NULL"
    ).fetchone()["c"]
    # Which stages are actually switched on, so the UI can hide a bar that
    # could never reach 100% (a Caption bar frozen at 0% while captioning is
    # off reads as a bug, not as "that feature is disabled").
    from . import config as _config
    facets = {f: _config.facet_enabled(con, f) for f in ("wd14", "caption")}
    # "current"/"rss_mb" surface *why* the process is using the memory it's
    # using (§12) -- otherwise a loaded multi-GB model just looks like an
    # unexplained number in Task Manager with no link back to this app.
    from . import status
    return {"files_total": total, "files_done": done, "jobs": by_state,
            "files_pending": files_pending,
            "scan_done": scan_done, "caption_done": caption_done,
            "tag_done": tag_done, "facets": facets,
            "current": status.get(), "rss_mb": status.rss_mb()}
