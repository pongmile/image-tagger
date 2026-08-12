"""M4 WD14 tagger test — spec §5, verification discipline §16.

Two layers:
  1. Deterministic pipeline wiring via FakeTaggerEngine — proves the infer stage
     writes source='wd14' tags with confidence, sets image_kind (kind router),
     lifts pose words into the `pose` category, and makes them FTS-searchable.
     Runs anywhere (no 300MB model needed) — this is the CI-safe path.
  2. Real-engine smoke (only if a WD14 model is installed) — loads the ONNX model
     and asserts it returns ranked, categorized tags on a real image.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_wd14
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    os.environ["IMAGE_TAGGER_WD14"] = "1"   # enable the facet for this test
    os.environ["IMAGE_TAGGER_OCR"] = "0"    # isolate: OCR off here

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_wd14_"))
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
    from indexer.scan import rescan
    from indexer.models import wd14

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # Inject a deterministic tagger: one character, two general, one pose word.
    fake = wd14.FakeTaggerEngine([
        wd14.TagResult("character", "hatsune miku", 0.98),
        wd14.TagResult("general", "twintails", 0.91),
        wd14.TagResult("general", "long hair", 0.88),
        wd14.TagResult("pose", "sitting", 0.80),
        wd14.TagResult("rating", "general", 0.95),
    ])
    wd14.set_engine(fake)

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (200, 100, 220)).save(lib / "miku.png")

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    print("scan:", rescan(con))
    n = worker.drain(con)
    print(f"worker processed {n} jobs (ingest + infer)")

    row = con.execute("SELECT * FROM files WHERE filename='miku.png'").fetchone()
    fid = row["id"]

    # image_kind set by the router
    check(row["image_kind"] == "anime", f"kind router set image_kind=anime (got {row['image_kind']})")

    # tags written with source='wd14' + confidence
    tags = con.execute(
        """SELECT c.name cat, t.name, ft.source, ft.confidence
           FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
           JOIN categories c ON c.id=t.category_id WHERE ft.file_id=?""", (fid,)
    ).fetchall()
    by = {(r["cat"], r["name"]): r for r in tags}
    check(("character", "hatsune miku") in by, "character tag written")
    check(by[("character", "hatsune miku")]["source"] == "wd14", "source=wd14")
    check(abs(by[("character", "hatsune miku")]["confidence"] - 0.98) < 1e-6,
          "confidence stored")
    check(("pose", "sitting") in by, "pose word lifted into 'pose' category")
    check(("rating", "general") in by, "rating tag written")

    # FTS searchable on the fast path
    def fts(q):
        return [r["rowid"] for r in con.execute(
            "SELECT rowid FROM files_fts WHERE files_fts MATCH ?", (q,))]
    check(fid in fts("twintails"), "wd14 general tag searchable via FTS")
    check(fid in fts("miku"), "wd14 character tag searchable via FTS")

    # Friendly aliases are additive: preserve the original model vocabulary and
    # expose common search terms at the same confidence.
    aliased = wd14.add_human_aliases([
        wd14.TagResult("general", "belly", 0.83),
        wd14.TagResult("general", "plump", 0.72),
        wd14.TagResult("general", "topless", 0.91),
    ])
    alias_by = {(t.category, t.name): t for t in aliased}
    for original in ("belly", "plump", "topless"):
        check(("general", original) in alias_by, f"original tag '{original}' retained")
    for alias in ("tummy", "chubby", "shirtless"):
        check(("general", alias) in alias_by, f"human alias '{alias}' added")
    check(wd14.filename_words("2025.01_mei1 fugtrup.png") ==
          "2025 01 mei 1 fugtrup png",
          "filename corroboration separates identity from sequence number")

    # idempotency: re-tagging replaces, doesn't duplicate
    db.write_auto_tags(con, fid, "wd14", fake.tag("x"))
    cnt = con.execute(
        "SELECT count(*) c FROM file_tags WHERE file_id=? AND source='wd14'", (fid,)
    ).fetchone()["c"]
    check(cnt == 5, f"re-tagging is idempotent (5 wd14 tags, got {cnt})")

    # --- Real-engine smoke (optional; uses the resolved models folder) ------
    model_dir = db.model_dir(con, "wd14")
    if (model_dir / "model.onnx").exists():
        print("\n[real WD14 model present — running smoke test]")
        real = wd14.Wd14Engine(model_dir, general_threshold=0.35)
        # a colorful image yields generic-but-real tags; assert structure
        Image.new("RGB", (128, 128), (10, 200, 120)).save(lib / "solid.png")
        rtags = real.tag(str(lib / "solid.png"))
        check(all(hasattr(t, "confidence") and 0 <= t.confidence <= 1 for t in rtags),
              "real engine returns confidences in [0,1]")
        check(all(t.category in ("rating", "general", "character", "pose") for t in rtags),
              "real engine tags are categorized")
        print(f"   real engine produced {len(rtags)} tag(s)")
    else:
        print("\n[no WD14 model installed — real-engine smoke skipped]")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — M4 WD14 pipeline verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
