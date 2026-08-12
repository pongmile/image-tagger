"""sqlite-vec embedding store — spec §6 (file_vec) / §8 (semantic search) / §5.3.

Holds one CLIP image embedding per file in a vec0 virtual table. This is the
KNN backend for optional semantic search AND the embedding space few-shot
learned tags (§5.3) score against, so it's populated whenever CLIP runs even if
the semantic-search UI is off.

sqlite-vec is a *soft* dependency (§15): if the extension can't load, semantic
search is simply unavailable and everything else keeps working. All helpers here
degrade to no-ops / empty results in that case rather than raising.
"""
from __future__ import annotations

_DIM_DEFAULT = 512


def available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except Exception:
        return False


def load(con) -> bool:
    """Load the sqlite-vec extension into a connection. Returns True on success.
    Safe to call repeatedly."""
    try:
        import sqlite_vec
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        return True
    except Exception:
        return False


def ensure_table(con, dim: int = _DIM_DEFAULT) -> bool:
    """Create the file_vec vec0 table if missing. Requires the extension loaded.
    Returns True if the table exists/was created."""
    if not load(con):
        return False
    try:
        con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS file_vec "
            f"USING vec0(file_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
        )
        return True
    except Exception:
        return False


def _ser(vec):
    import sqlite_vec
    return sqlite_vec.serialize_float32([float(x) for x in vec])


def upsert(con, file_id: int, vec, dim: int | None = None) -> bool:
    """Store/replace a file's embedding. Returns True if written."""
    d = dim or len(vec)
    if not ensure_table(con, d):
        return False
    try:
        with con:
            con.execute("DELETE FROM file_vec WHERE file_id=?", (file_id,))
            con.execute(
                "INSERT INTO file_vec (file_id, embedding) VALUES (?,?)",
                (file_id, _ser(vec)),
            )
        return True
    except Exception:
        return False


def drop(con) -> None:
    """Drop the whole embedding table (used when the CLIP variant changes and its
    dimension no longer matches stored vectors — the library must be re-indexed)."""
    if load(con):
        try:
            with con:
                con.execute("DROP TABLE IF EXISTS file_vec")
        except Exception:
            pass


def delete(con, file_id: int) -> None:
    if load(con):
        try:
            with con:
                con.execute("DELETE FROM file_vec WHERE file_id=?", (file_id,))
        except Exception:
            pass


def knn(con, query_vec, k: int = 50):
    """Return [(file_id, distance), ...] nearest to query_vec, or [] if the
    store/extension is unavailable. Uses sqlite-vec's `k = ?` KNN form."""
    if not load(con):
        return []
    try:
        rows = con.execute(
            "SELECT file_id, distance FROM file_vec "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (_ser(query_vec), k),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


def get_embedding(con, file_id: int):
    """Fetch a stored embedding back as a list[float] (for learned-tag prototypes,
    §5.3). Returns None if absent."""
    if not load(con):
        return None
    try:
        import struct
        row = con.execute(
            "SELECT embedding FROM file_vec WHERE file_id=?", (file_id,)
        ).fetchone()
        if not row:
            return None
        blob = row[0]
        return list(struct.unpack(f"{len(blob)//4}f", blob))
    except Exception:
        return None
