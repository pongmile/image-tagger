"""M5 CLIP + semantic-search test — spec §5/§8, verification discipline §16.

Verifies the full CLIP pipeline with a deterministic FakeClipEngine (one-hot-ish
embeddings over keyword axes) against a REAL sqlite-vec store:
  * zero-shot classification writes source='clip' tags from the editable vocab
  * the image embedding is stored in file_vec (sqlite-vec vec0)
  * semantic search (text -> embedding -> KNN) ranks the right file first
  * open-vocab: a user-added label is picked up
The neural encoder is faked; the vector store, KNN, vocab, and wiring are real.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_clip
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    os.environ["IMAGE_TAGGER_CLIP"] = "1"
    os.environ["IMAGE_TAGGER_OCR"] = "0"
    os.environ["IMAGE_TAGGER_WD14"] = "0"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_clip_"))
    home, lib = tmp / "home", tmp / "lib"
    os.environ["IMAGE_TAGGER_HOME"] = str(home)

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    import indexer.ingest as ingest
    importlib.reload(ingest)
    import indexer.worker as worker
    importlib.reload(worker)
    from indexer import vec
    from indexer.scan import rescan
    from indexer.models import clip

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    if not vec.available():
        print("sqlite-vec not installed — M5 requires it. pip install sqlite-vec")
        return 1

    # Deterministic encoder: image basename & label text share a keyword axis.
    axes = ["beach", "forest", "kimono", "swimsuit", "night", "sitting"]
    clip.set_engine(clip.FakeClipEngine(axes))

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (48, 48), (0, 90, 200)).save(lib / "beach.png")
    Image.new("RGB", (48, 48), (0, 160, 40)).save(lib / "forest.png")
    Image.new("RGB", (48, 48), (200, 40, 120)).save(lib / "kimono.png")

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    print("scan:", rescan(con))
    n = worker.drain(con)
    print(f"worker processed {n} jobs")

    files = {Path(r["path"]).name: r["id"] for r in
             con.execute("SELECT id, path FROM files").fetchall()}
    beach, forest = files["beach.png"], files["forest.png"]

    # zero-shot tags written with source='clip'
    def tags(fid):
        return {(r["cat"], r["name"]): r for r in con.execute(
            """SELECT c.name cat, t.name, ft.source, ft.confidence FROM file_tags ft
               JOIN tags t ON t.id=ft.tag_id JOIN categories c ON c.id=t.category_id
               WHERE ft.file_id=? AND ft.source='clip'""", (fid,))}
    bt = tags(beach)
    check(("scene", "beach") in bt, f"zero-shot scene:beach on beach.png (got {set(bt)})")
    check(("scene", "beach") in bt and bt[("scene", "beach")]["source"] == "clip",
          "source=clip")
    check(("scene", "forest") in tags(forest), "zero-shot scene:forest on forest.png")
    check(("scene", "beach") not in tags(forest), "forest not mislabeled beach")

    # embedding stored in file_vec
    check(vec.get_embedding(con, beach) is not None, "CLIP embedding stored in file_vec")

    # FTS: clip tag searchable on the fast path
    hit = [r["rowid"] for r in con.execute(
        "SELECT rowid FROM files_fts WHERE files_fts MATCH 'beach'")]
    check(beach in hit, "clip tag folded into FTS tags_text")

    # semantic search: text -> embedding -> KNN ranks the matching file first
    tvec = clip.get_engine().encode_texts(["beach"])[0]
    hits = vec.knn(con, tvec, k=3)
    check(hits and hits[0][0] == beach, f"semantic 'beach' ranks beach.png first (got {hits})")
    tvec2 = clip.get_engine().encode_texts(["forest"])[0]
    hits2 = vec.knn(con, tvec2, k=3)
    check(hits2 and hits2[0][0] == forest, "semantic 'forest' ranks forest.png first")

    # open-vocab: add a new label, re-run, it gets applied
    db.add_clip_label(con, "clothing", "kimono")  # already seeded, ensure present
    db.add_clip_label(con, "scene", "shrine")
    check("shrine" in db.get_clip_vocab(con).get("scene", []),
          "user-added CLIP label appears in vocab (open-vocab)")

    # delete cleans the embedding row too
    os.remove(lib / "beach.png")
    rescan(con)
    check(vec.get_embedding(con, beach) is None, "embedding removed when file deleted")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — M5 CLIP zero-shot + sqlite-vec semantic search verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
