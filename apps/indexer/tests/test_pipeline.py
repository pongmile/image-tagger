"""End-to-end M2 pipeline test — spec §7/§9, verification discipline §16.

Generates real sample images (a Stable-Diffusion PNG with a `parameters` text
chunk, a JPEG with EXIF, plain images, and files inside an excluded subfolder),
then drives the full pipeline: add roots/excludes -> rescan -> drain worker ->
assert files/metadata/thumbs/path-tags/FTS, manual tag add/remove, scope
exclusion, and on-delete cleanup.

Run standalone (no pytest needed):
    apps/indexer/.venv/Scripts/python -m tests.test_pipeline
or with pytest:
    apps/indexer/.venv/Scripts/python -m pytest apps/indexer/tests -q
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make `indexer` importable when run as a script from apps/indexer/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_images(root: Path) -> None:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    import piexif

    (root / "Hatsune Miku").mkdir(parents=True, exist_ok=True)
    (root / "photos").mkdir(parents=True, exist_ok=True)
    (root / "WIP").mkdir(parents=True, exist_ok=True)          # excluded subtree
    (root / "photos" / "node_modules").mkdir(parents=True, exist_ok=True)  # excluded

    # 1) Stable Diffusion PNG with an A1111-style `parameters` text chunk.
    sd = Image.new("RGB", (64, 48), (90, 140, 210))
    info = PngInfo()
    info.add_text(
        "parameters",
        "masterpiece, 1girl, hatsune miku, twintails\n"
        "Negative prompt: lowres, bad anatomy\n"
        "Steps: 28, Sampler: DPM++ 2M Karras, CFG scale: 7, Seed: 12345, "
        "Size: 512x768, Model: anything-v5, <lora:mikuStyle:0.8>",
    )
    sd.save(root / "Hatsune Miku" / "miku_render.png", pnginfo=info)

    # 2) JPEG with EXIF Make/Model.
    photo = Image.new("RGB", (80, 60), (30, 160, 90))
    exif = {"0th": {piexif.ImageIFD.Make: b"Canon",
                    piexif.ImageIFD.Model: b"Canon EOS R5",
                    piexif.ImageIFD.Software: b"darktable"}}
    photo.save(root / "photos" / "beach_sunset.jpg", exif=piexif.dump(exif))

    # 3) Plain images (one supported, one in each excluded location).
    Image.new("RGB", (40, 40), (200, 60, 60)).save(root / "photos" / "cat.webp")
    Image.new("RGB", (40, 40), (10, 10, 10)).save(root / "WIP" / "draft.png")
    Image.new("RGB", (40, 40), (10, 10, 10)).save(
        root / "photos" / "node_modules" / "icon.png")


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="imgtag_test_"))
    home = tmp / "home"
    lib = tmp / "library"
    os.environ["IMAGE_TAGGER_HOME"] = str(home)

    # Import AFTER setting the env so config picks up the temp home.
    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    import indexer.ingest as ingest
    importlib.reload(ingest)
    from indexer.scan import rescan
    from indexer.worker import drain

    _make_images(lib)
    con = db.connect()

    # Configure scope: include the library, exclude the WIP subtree.
    db.add_root(con, str(lib), mode="include")
    db.add_root(con, str(lib / "WIP"), mode="exclude")
    # (node_modules is covered by the default seed exclude pattern.)

    fails: list[str] = []

    def check(cond: bool, msg: str):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # --- Rescan + drain -----------------------------------------------------
    res = rescan(con)
    print(f"scan: {res}")
    n = drain(con)
    print(f"worker processed {n} jobs")

    files = {Path(r["path"]).name: r for r in
             con.execute("SELECT * FROM files").fetchall()}
    check(set(files) == {"miku_render.png", "beach_sunset.jpg", "cat.webp"},
          f"only in-scope files indexed (got {sorted(files)})")
    check("draft.png" not in files, "WIP/ exclude root honored")
    check("icon.png" not in files, "node_modules exclude pattern honored")

    # --- Ingest results -----------------------------------------------------
    miku = files.get("miku_render.png")
    check(miku is not None and miku["width"] == 64 and miku["height"] == 48,
          "dimensions read (miku 64x48)")
    check(miku is not None and len(miku["sha256"]) == 64, "sha256 computed")
    check(miku is not None and miku["phash"] is not None, "perceptual hash computed")
    check(miku is not None and miku["index_status"] == "done", "status=done")

    # thumbnail on disk
    if miku:
        thumb = home / "thumbs" / miku["sha256"][:2] / (miku["sha256"] + ".webp")
        check(thumb.exists(), f"thumbnail generated ({thumb.name})")

    # --- Metadata extraction ------------------------------------------------
    if miku:
        md = {r["key"]: r["value"] for r in con.execute(
            "SELECT key,value FROM file_metadata WHERE file_id=?", (miku["id"],))}
        check("png:parameters" in md, "PNG parameters chunk stored")
        check("DPM++ 2M" in md.get("png:parameters", ""),
              "SD sampler present in parameters")

    beach = files.get("beach_sunset.jpg")
    if beach:
        md = {r["key"]: r["value"] for r in con.execute(
            "SELECT key,value FROM file_metadata WHERE file_id=?", (beach["id"],))}
        check(md.get("exif:Make") == "Canon", "EXIF Make extracted")
        check("EOS R5" in md.get("exif:Model", ""), "EXIF Model extracted")

    # --- Path-derived tags (§5.1) ------------------------------------------
    if miku:
        ptags = con.execute(
            """SELECT t.name, c.name cat, ft.source FROM file_tags ft
               JOIN tags t ON t.id=ft.tag_id JOIN categories c ON c.id=t.category_id
               WHERE ft.file_id=? AND ft.source='path'""", (miku["id"],)).fetchall()
        names = {r["name"] for r in ptags}
        check("hatsune miku" in names, f"path tag from folder (got {names})")

    # --- FTS fast-path search ----------------------------------------------
    def fts(q):
        return [r["rowid"] for r in con.execute(
            "SELECT rowid FROM files_fts WHERE files_fts MATCH ?", (q,))]

    check(miku and miku["id"] in fts("miku"), "FTS finds path-tag 'miku'")
    check(miku and miku["id"] in fts('"DPM++ 2M"'), "FTS finds SD sampler in meta_text")
    check(beach and beach["id"] in fts("Canon"), "FTS finds EXIF camera in meta_text")
    check(miku and miku["id"] in fts("iku"), "trigram substring match ('iku')")

    # --- Manual tagging (§9) -----------------------------------------------
    if miku:
        db.add_manual_tag(con, miku["id"], "hatsune miku", "character")
        check(miku["id"] in fts("hatsune"), "manual tag searchable after add")
        got = con.execute(
            """SELECT source FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
               JOIN categories c ON c.id=t.category_id
               WHERE ft.file_id=? AND c.name='character' AND t.name='hatsune miku'""",
            (miku["id"],)).fetchone()
        check(got and got["source"] == "manual", "manual tag stored with source=manual")
        db.remove_tag(con, miku["id"], "hatsune miku", "character")
        still = con.execute(
            """SELECT 1 FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
               JOIN categories c ON c.id=t.category_id
               WHERE ft.file_id=? AND c.name='character' AND t.name='hatsune miku'""",
            (miku["id"],)).fetchone()
        check(still is None, "manual tag removed")

    # --- Custom category (§9) ----------------------------------------------
    cid = db.get_or_create_category(con, "cosplay", color="#e91e63")
    con.commit()
    check(cid is not None, "custom category created")

    # --- Per-source scan ---------------------------------------------------
    other = tmp / "other-library"
    other.mkdir()
    from PIL import Image
    Image.new("RGB", (32, 32), (20, 30, 40)).save(other / "only-here.png")
    db.add_root(con, str(other), mode="include")
    other_id = con.execute("SELECT id FROM roots WHERE path=?", (str(other),)).fetchone()["id"]
    one = rescan(con, root_id=other_id)
    check(one.added == 1 and one.unchanged == 0,
          "per-source scan walks only the selected root")
    os.remove(other / "only-here.png")
    gone = rescan(con, root_id=other_id)
    check(gone.removed == 1, "per-source scan removes stale rows only in that root")

    # --- Idempotency: rescan again enqueues nothing new --------------------
    res2 = rescan(con)
    check(res2.added == 0 and res2.changed == 0,
          f"rescan idempotent (added={res2.added} changed={res2.changed})")

    # --- On-delete cleanup --------------------------------------------------
    if miku:
        os.remove(lib / "Hatsune Miku" / "miku_render.png")
        res3 = rescan(con)
        check(res3.removed == 1, "deleted file removed on rescan")
        check(miku["id"] not in fts("miku"), "FTS row gone after delete")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s) failed")
        for m in fails:
            print("   -", m)
        return 1
    print("RESULT: PASS — full M2 pipeline verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
