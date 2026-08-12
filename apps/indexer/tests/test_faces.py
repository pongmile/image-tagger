"""M6 faces test — spec §5/§15, verification discipline §16.

Deterministic FakeFaceEngine (stable embedding per identity) verifies the real
intellectual content of M6: incremental clustering, one-time naming, auto-attach
of new faces to the nearest named cluster, merge, FTS-by-name, and that an anime
image is skipped by the kind router. The neural model is faked; the clustering
and DB logic are real.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_faces
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    os.environ["IMAGE_TAGGER_FACES"] = "1"
    os.environ["IMAGE_TAGGER_OCR"] = "0"
    os.environ["IMAGE_TAGGER_WD14"] = "0"
    os.environ["IMAGE_TAGGER_CLIP"] = "0"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_faces_"))
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
    from indexer.models import faces

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # Face layout: alice appears in img1 & img2; bob in img2; carol alone in img3.
    # img_anime should be skipped (kind router), so its 'ghost' face never lands.
    layout = {
        "img1.jpg": [("alice", [10, 10, 40, 40])],
        "img2.jpg": [("alice", [5, 5, 40, 40]), ("bob", [90, 8, 40, 40])],
        "img3.jpg": [("carol", [20, 20, 40, 40])],
        "toon.png": [("ghost", [0, 0, 20, 20])],
    }
    faces.set_engine(faces.FakeFaceEngine(layout, dim=16))

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    for fn in layout:
        Image.new("RGB", (160, 80), (120, 120, 120)).save(lib / fn)

    con = db.connect()
    # Mark the toon as anime so the router skips face detection on it.
    db.add_root(con, str(lib), mode="include")
    rescan(con)
    # set kind before infer runs: easiest is to run rescan (creates pending rows),
    # mark anime, then drain.
    tid = con.execute("SELECT id FROM files WHERE filename='toon.png'").fetchone()["id"]
    db.set_image_kind(con, tid, "anime")
    n = worker.drain(con)
    print(f"worker processed {n} jobs")

    fid = {Path(r["path"]).name: r["id"] for r in
           con.execute("SELECT id, path FROM files")}

    # clustering: alice's two faces should land in ONE person; bob & carol distinct
    def person_of(file, idx=0):
        rows = con.execute(
            "SELECT person_id, bbox FROM faces WHERE file_id=? ORDER BY id",
            (file,)).fetchall()
        return rows[idx]["person_id"] if rows else None

    alice1 = person_of(fid["img1.jpg"])
    alice2 = person_of(fid["img2.jpg"], 0)   # first face in img2 is alice
    bob = person_of(fid["img2.jpg"], 1)
    carol = person_of(fid["img3.jpg"])
    check(alice1 is not None and alice1 == alice2,
          "same identity across images clusters into one person")
    check(len({alice1, bob, carol}) == 3, "distinct identities -> distinct persons")

    # anime image skipped -> no faces from toon.png
    check(con.execute("SELECT count(*) c FROM faces WHERE file_id=?",
                      (tid,)).fetchone()["c"] == 0, "anime image skipped by kind router")

    # total persons = 3 (alice, bob, carol)
    np = con.execute("SELECT count(*) c FROM persons").fetchone()["c"]
    check(np == 3, f"three clusters formed (got {np})")

    # name once, then it's searchable and person: works
    db.name_person(con, alice1, "Alice Smith")

    def fts(q):
        return [r["rowid"] for r in con.execute(
            "SELECT rowid FROM files_fts WHERE files_fts MATCH ?", (q,))]
    check(fid["img1.jpg"] in fts("Alice"), "named person searchable via FTS")

    # person: grammar (same SQL the Node reader emits)
    def person_search(name):
        return [r["id"] for r in con.execute(
            "SELECT f.id FROM files f WHERE EXISTS (SELECT 1 FROM faces fa "
            "JOIN persons p ON p.id=fa.person_id WHERE fa.file_id=f.id "
            "AND p.name=? COLLATE NOCASE)", (name,))]
    ps = person_search("alice smith")
    check(set(ps) == {fid["img1.jpg"], fid["img2.jpg"]},
          f"person: search returns both of Alice's files (got {ps})")

    # auto-attach: a NEW image with alice attaches to her (now named) cluster
    faces.set_engine(faces.FakeFaceEngine(
        {**layout, "img4.jpg": [("alice", [12, 12, 40, 40])]}, dim=16))
    Image.new("RGB", (160, 80), (120, 120, 120)).save(lib / "img4.jpg")
    rescan(con); worker.drain(con)
    f4 = con.execute("SELECT id FROM files WHERE filename='img4.jpg'").fetchone()["id"]
    check(person_of(f4) == alice1, "new face auto-attaches to the named cluster")
    check(f4 in person_search("Alice Smith"), "auto-attached file found by person:")

    # merge tool (§15): fold bob into alice, bob's file now matches Alice
    db.merge_persons(con, bob, alice1)
    check(con.execute("SELECT count(*) c FROM persons").fetchone()["c"] == 2,
          "merge reduces cluster count")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — M6 face clustering / naming / auto-attach verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
