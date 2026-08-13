"""Per-model WD14 tag cache — verification discipline §16.

Switching the WD14 model in Settings used to have no memory at all: the old
model's tags just sat in file_tags until something reindexed the file, and a
reindex always re-ran inference from scratch even for a file already tagged
by the model that is currently active. That wastes a model load + a forward
pass for every file on every "↻ Reindex" click, even when nothing changed.

_run_wd14_facet() now checks facet_model_cache before calling the tagger:
  * same model as last time -> restore from cache, no inference at all.
  * a model switch -> run inference (cache miss), and stash the result so a
    switch *back* restores instantly instead of re-running.
  * file_tags (what search/FTS reads) always mirrors only the active model;
    an inactive model's cached tags are inert until it's selected again.
  * an explicit single-file "↻ re-Tag"/"↻ re-index" (force=True) always
    bypasses the cache -- that's the point of a manual override click.

Verified here via a call-counting wrapper around wd14.get_engine (same
technique as test_hardware_cache.py's probe counter): a cache hit never
reaches the tagger at all, not just "returns fast".

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_facet_model_cache
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    os.environ["IMAGE_TAGGER_CLIP"] = "0"
    os.environ["IMAGE_TAGGER_OCR"] = "0"
    os.environ["IMAGE_TAGGER_WD14"] = "1"
    os.environ["IMAGE_TAGGER_FACES"] = "0"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_facetcache_"))
    home, lib = tmp / "home", tmp / "lib"
    os.environ["IMAGE_TAGGER_HOME"] = str(home)

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    import indexer.worker as worker
    importlib.reload(worker)
    from indexer import engine
    from indexer.models import wd14

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    img_path = lib / "girl.png"
    Image.new("RGB", (32, 32), (120, 80, 200)).save(img_path)

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    from indexer.scan import rescan
    rescan(con)
    fid = con.execute("SELECT id FROM files WHERE filename='girl.png'").fetchone()["id"]

    variant_a_tags = [wd14.TagResult("general", "1girl", 0.95),
                       wd14.TagResult("general", "smile", 0.8)]
    variant_b_tags = [wd14.TagResult("general", "1girl", 0.9),
                       wd14.TagResult("general", "outdoors", 0.7)]

    calls = {"n": 0}
    original_get_engine = wd14.get_engine

    def counting_get_engine(*a, **kw):
        calls["n"] += 1
        variant = db.get_setting(con, "wd14_variant")
        results = variant_a_tags if variant == "moat-v2" else variant_b_tags
        return wd14.FakeTaggerEngine(results)

    def names_for(fid):
        return {r["name"] for r in con.execute(
            "SELECT t.name FROM file_tags ft JOIN tags t ON t.id=ft.tag_id "
            "WHERE ft.file_id=? AND ft.source='wd14'", (fid,))}

    wd14.get_engine = counting_get_engine
    try:
        db.set_setting(con, "wd14_variant", "moat-v2")
        worker._run_wd14_facet(con, fid, str(img_path), engine,
                                engine.get_engine_config(con)["onnx_providers"])
        check(calls["n"] == 1, f"cache miss on first run calls the tagger once (got {calls['n']})")
        check(names_for(fid) == {"1girl", "smile"}, "variant A's tags are written")

        worker._run_wd14_facet(con, fid, str(img_path), engine,
                                engine.get_engine_config(con)["onnx_providers"])
        check(calls["n"] == 1,
              f"re-running with the same active model never calls the tagger again (got {calls['n']})")
        check(names_for(fid) == {"1girl", "smile"}, "variant A's tags are unchanged after the cache hit")

        db.set_setting(con, "wd14_variant", "convnext-v2")
        worker._run_wd14_facet(con, fid, str(img_path), engine,
                                engine.get_engine_config(con)["onnx_providers"])
        check(calls["n"] == 2, f"switching model is a cache miss, calls the tagger (got {calls['n']})")
        check(names_for(fid) == {"1girl", "outdoors"}, "variant B's tags are now what search sees")
        check("smile" not in names_for(fid),
              "variant A's tag is not visible while B is active")

        db.set_setting(con, "wd14_variant", "moat-v2")
        worker._run_wd14_facet(con, fid, str(img_path), engine,
                                engine.get_engine_config(con)["onnx_providers"])
        check(calls["n"] == 2,
              f"switching back to A restores from cache, no tagger call (got {calls['n']})")
        check(names_for(fid) == {"1girl", "smile"},
              "variant A's own tags come back exactly as they were")

        # FTS must only ever show the active model's tags -- checked on a
        # separate read-only connection, same discipline as test_learned_forget.py.
        import sqlite3
        verify_con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        verify_con.row_factory = sqlite3.Row
        try:
            hits_smile = {r["rowid"] for r in verify_con.execute(
                "SELECT rowid FROM files_fts WHERE files_fts MATCH 'smile'")}
            hits_outdoors = {r["rowid"] for r in verify_con.execute(
                "SELECT rowid FROM files_fts WHERE files_fts MATCH 'outdoors'")}
        finally:
            verify_con.close()
        check(fid in hits_smile, "FTS matches the active model's tag ('smile', variant A active)")
        check(fid not in hits_outdoors, "FTS does not match the inactive model's cached tag ('outdoors')")

        # Explicit single-file force bypasses the cache even on a hit.
        worker._run_wd14_facet(con, fid, str(img_path), engine,
                                engine.get_engine_config(con)["onnx_providers"], force=True)
        check(calls["n"] == 3,
              f"force=True (re-Tag/re-index) always calls the tagger, even on a cache hit (got {calls['n']})")
    finally:
        wd14.get_engine = original_get_engine

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — per-model WD14 tag cache verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
