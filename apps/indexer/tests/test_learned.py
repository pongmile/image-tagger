"""M7 few-shot learned-tags test — spec §5.3, verification discipline §16.

Uses FakeClip embeddings in the REAL sqlite-vec store to verify the learning
mechanism end-to-end:
  * prototype (centroid) from a few hand-tagged examples auto-applies the tag to
    a held-out similar image, and NOT to dissimilar ones
  * manual tags are never downgraded to 'learned'
  * confirm / reject feedback adjusts the model and the applied set
  * the centroid upgrades to a linear head (real scikit-learn) once enough
    examples incl. negatives accrue, and still classifies correctly

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_learned
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
    os.environ["IMAGE_TAGGER_FACES"] = "0"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_learn_"))
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
    from indexer import vec, learned
    from indexer.scan import rescan
    from indexer.models import clip

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    if not vec.available():
        print("sqlite-vec required for M7 — pip install sqlite-vec")
        return 1

    axes = ["nova", "beach", "forest", "city", "park", "misc"]
    clip.set_engine(clip.FakeClipEngine(axes))

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    # 5 positives (share 'nova', vary second axis), 1 held-out positive, 3 negatives
    pos_files = ["nova_beach.png", "nova_forest.png", "nova_city.png",
                 "nova_park.png", "nova_beach2.png"]
    heldout = "nova_new.png"
    neg_files = ["misc_beach.png", "misc_forest.png", "misc_city.png"]
    for fn in pos_files + [heldout] + neg_files:
        Image.new("RGB", (32, 32), (100, 100, 100)).save(lib / fn)

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    rescan(con)
    worker.drain(con)   # CLIP runs -> file_vec populated for every file

    fid = {Path(r["path"]).name: r["id"] for r in
           con.execute("SELECT id, path FROM files")}
    check(vec.get_embedding(con, fid[heldout]) is not None,
          "CLIP embeddings populated in file_vec")
    # Deliberately make one generically named file visually identical in CLIP
    # space. Positive-only character learning must not label it without
    # filename/folder/path evidence for the character name.
    lookalike = neg_files[0]
    heldout_vec = vec.get_embedding(con, fid[heldout])
    vec.upsert(con, fid[lookalike], heldout_vec, dim=len(heldout_vec))

    # Hand-tag the 5 positives as a brand-new concept the base models don't know.
    for fn in pos_files:
        db.add_manual_tag(con, fid[fn], "nova", "character")

    # --- Build the learned tag (centroid) + apply --------------------------
    s = learned.build(con, "character", "nova", space="clip")
    check(s is not None and s["method"] == "centroid",
          f"learned tag trains a centroid (got {s})")

    def has_tag(file, source=None):
        q = ("""SELECT source FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
                JOIN categories c ON c.id=t.category_id
                WHERE ft.file_id=? AND t.name='nova' AND c.name='character'""")
        row = con.execute(q, (file,)).fetchone()
        return row["source"] if row else None

    check(has_tag(fid[heldout]) == "learned",
          "held-out similar image auto-tagged (source=learned)")
    check(all(has_tag(fid[f]) is None for f in neg_files),
          "unrelated names are NOT character-tagged, even with a similar vector")
    check(all(has_tag(fid[f]) == "manual" for f in pos_files),
          "manual tags never downgraded to learned")

    # searchable on the fast path
    hit = [r["rowid"] for r in con.execute(
        "SELECT rowid FROM files_fts WHERE files_fts MATCH 'nova'")]
    check(fid[heldout] in hit, "learned tag folded into FTS")

    # --- Reject feedback removes it + keeps it off -------------------------
    tag_id = db.get_or_create_tag(con, "nova", "character")
    learned.reject(con, tag_id, fid[heldout], "clip")
    check(has_tag(fid[heldout]) is None, "reject removes the learned tag")
    learned.apply(con, tag_id)
    check(has_tag(fid[heldout]) is None, "rejected file stays untagged on re-apply")

    # --- Confirm feedback (re-accept) --------------------------------------
    # add it back as a positive; it should be applied again
    con.execute("DELETE FROM tag_examples WHERE tag_id=? AND file_id=?",
                (tag_id, fid[heldout])); con.commit()
    learned.confirm(con, tag_id, fid[heldout], "clip")
    check(has_tag(fid[heldout]) in ("learned", "manual"),
          "confirm re-applies the tag")

    # --- Linear-head upgrade (real scikit-learn) ---------------------------
    learned.LINEAR_MIN = 4   # keep the test small; real default is 20
    for f in neg_files:      # add negatives to trigger the upgrade
        learned.add_example(con, tag_id, fid[f], -1, "rejected")
    summary = learned.train(con, tag_id, "clip")
    try:
        import sklearn  # noqa: F401
        have_sklearn = True
    except Exception:
        have_sklearn = False
    if have_sklearn:
        check(summary["method"] == "linear",
              f"upgrades to a linear head with enough examples (got {summary['method']})")
        learned.apply(con, tag_id)
        check(all(has_tag(fid[f]) is None for f in neg_files),
              "linear head still rejects the negatives")
        lt = con.execute("SELECT classifier FROM learned_tags WHERE tag_id=?",
                         (tag_id,)).fetchone()
        check(lt["classifier"] is not None, "linear classifier persisted")
    else:
        print("  -- scikit-learn absent; linear-head upgrade skipped --")

    # --- Face-space learned tags (§5.3): the same few-shot mechanism, but on
    # InsightFace embeddings instead of CLIP, so it keeps recognizing one real
    # person across photos where CLIP similarity would drift. Also verifies the
    # online-learning hook added to worker.py: a newly indexed file with a
    # detected face is scored against 'face'-space learned tags immediately,
    # not only when the user presses Train/Refresh again. --------------------
    os.environ["IMAGE_TAGGER_FACES"] = "1"
    from indexer.models import faces

    face_pos = ["mira_1.png", "mira_2.png", "mira_3.png", "mira_4.png", "mira_5.png"]
    face_heldout = "mira_new.png"
    face_layout = {fn: [("mira", [10, 10, 40, 40])] for fn in face_pos}
    faces.set_engine(faces.FakeFaceEngine(face_layout, dim=16))
    for fn in face_pos:
        Image.new("RGB", (64, 64), (150, 150, 150)).save(lib / fn)
    rescan(con)
    worker.drain(con)

    fid.update({Path(r["path"]).name: r["id"] for r in
                con.execute("SELECT id, path FROM files")})
    check(all(con.execute("SELECT count(*) c FROM faces WHERE file_id=?",
                          (fid[fn],)).fetchone()["c"] == 1 for fn in face_pos),
          "FakeFaceEngine embeddings populated in faces table")

    for fn in face_pos:
        db.add_manual_tag(con, fid[fn], "mira", "person")

    fs = learned.build(con, "person", "mira", space="face")
    check(fs is not None and fs["method"] == "centroid",
          f"face-space learned tag trains a centroid (got {fs})")

    # A new photo of the SAME person, indexed AFTER training: worker.py's faces
    # facet must score it against the 'face'-space learned tag as part of
    # normal indexing, with no explicit learned.apply() call in this test.
    faces.set_engine(faces.FakeFaceEngine(
        {**face_layout, face_heldout: [("mira", [12, 12, 40, 40])]}, dim=16))
    Image.new("RGB", (64, 64), (150, 150, 150)).save(lib / face_heldout)
    rescan(con)
    worker.drain(con)
    fid[face_heldout] = con.execute(
        "SELECT id FROM files WHERE filename=?", (face_heldout,)).fetchone()["id"]

    def has_person_tag(file):
        q = ("""SELECT source FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
                JOIN categories c ON c.id=t.category_id
                WHERE ft.file_id=? AND t.name='mira' AND c.name='person'""")
        row = con.execute(q, (file,)).fetchone()
        return row["source"] if row else None

    check(has_person_tag(fid[face_heldout]) == "learned",
          "new face auto-scored against face-space learned tag during "
          "indexing (worker.py online-apply hook), no manual Train/Refresh")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — M7 few-shot learned tags verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
