"""Proves the reject-auto ('x') claim shown in preview.component.ts (§9):

    "Removed '<tag>' for '<file>' - won't come back on rescan/reindex, and
     similar images are less likely to get it too."

Exercises the REAL command handler daemon.py::_reject_auto_tag() (the exact
function the UI's reject-auto button calls through the daemon RPC), not a
re-implementation, so a pass here is proof the wired-up feature behaves as
advertised end-to-end:

  1. "won't come back on rescan/reindex" -> db.write_auto_tags() checks
     rejected_tags and must refuse to re-insert the tag for that file even
     when the (simulated) model tries to produce it again.
  2. "similar images are less likely to get it too" -> rejecting an auto-tag
     feeds the few-shot learner (learned.py, spec section 5.3) a negative
     example at that file's embedding. Once a tag has a trained learned_tags
     row, retraining with that negative raises the acceptance threshold and
     can revoke an existing 'learned' suggestion on a different, visually
     identical file.

Uses the deterministic FakeClipEngine (real cosine geometry, no weights) in
the real sqlite-vec store, same technique as test_learned.py.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_reject_propagation
"""
from __future__ import annotations

import os
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

AutoTag = namedtuple("AutoTag", "category name confidence")


def run() -> int:
    os.environ["IMAGE_TAGGER_CLIP"] = "1"
    os.environ["IMAGE_TAGGER_OCR"] = "0"
    os.environ["IMAGE_TAGGER_WD14"] = "0"
    os.environ["IMAGE_TAGGER_FACES"] = "0"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_rejectprop_"))
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
    from indexer.daemon import _reject_auto_tag

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    if not vec.available():
        print("sqlite-vec required — pip install sqlite-vec")
        return 1

    axes = ["yumi", "alpha", "beta", "gamma", "delta", "zeta"]
    clip.set_engine(clip.FakeClipEngine(axes))

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    # 5 positives (share 'yumi', vary the second axis), mirroring
    # test_learned.py's proven centroid setup. 'zeta' is never used by any
    # filename — it's a spare orthogonal direction used below to craft a
    # held-out embedding at a precise, chosen similarity to the centroid.
    pos_files = ["yumi_alpha.png", "yumi_beta.png", "yumi_gamma.png",
                 "yumi_delta.png", "yumi_alpha2.png"]
    similar = "yumi_new.png"          # a different, genuine look-alike photo
    wrong_auto = "unrelated_photo.png"  # will carry the WRONG raw auto-tag
    for fn in pos_files + [similar, wrong_auto]:
        Image.new("RGB", (32, 32), (100, 100, 100)).save(lib / fn)

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    rescan(con)
    worker.drain(con)  # CLIP runs -> file_vec populated for every file; no
                        # vocab labels configured, so nothing is zero-shot
                        # tagged yet — every file starts with no 'yumi' tag.

    fid = {Path(r["path"]).name: r["id"] for r in
           con.execute("SELECT id, path FROM files")}

    # --- Do all embedding math up front (right after CLIP runs) -------------
    # Mirror learned.train()'s own centroid + no-negative-threshold formula in
    # plain Python, using the *real* fake-CLIP embeddings already stored for
    # the 5 positives, so the held-out vector crafted below can be placed at
    # an exact, chosen similarity before learned.build()/apply() ever run.
    # (Deliberately front-loaded: sqlite-vec lookups get unreliable on this
    # connection after an FTS refresh runs, which build()/apply() trigger.)
    pos_embs = [vec.get_embedding(con, fid[fn]) for fn in pos_files]
    check(all(e is not None for e in pos_embs),
          "CLIP embeddings are present for all 5 positive examples")
    proto = learned._norm([sum(col) for col in zip(*pos_embs)])
    pos_sims = [learned._cos(e, proto) for e in pos_embs]
    mean_pos = sum(pos_sims) / len(pos_sims)
    std = (sum((s - mean_pos) ** 2 for s in pos_sims) / len(pos_sims)) ** 0.5
    threshold_before = max(learned.FLOOR["clip"], mean_pos - max(2 * std, 0.05))
    target_score = (threshold_before + mean_pos) / 2

    zeta_idx = axes.index("zeta")

    def blended_vec(t: float) -> list[float]:
        v = [(1 - t) * p for p in proto]
        v[zeta_idx] += t
        return learned._norm(v)

    # Binary search for a blend factor whose cosine-to-centroid hits
    # target_score exactly — a realistic "typical borderline match" (not an
    # extreme/adversarial one): it clears today's threshold (so it legitimately
    # auto-suggests first) but sits below the average positive, so a single
    # negative example at this point is guaranteed to raise the retrained
    # threshold above it (algebraically: new_threshold = (mean_pos+score)/2,
    # which exceeds `score` whenever `score < mean_pos`).
    lo, hi = 0.0, 1.0  # cosine-to-centroid decreases monotonically as t rises
    for _ in range(40):
        mid = (lo + hi) / 2
        if learned._cos(blended_vec(mid), proto) > target_score:
            lo = mid
        else:
            hi = mid
    crafted_vec = blended_vec((lo + hi) / 2)
    crafted_score = learned._cos(crafted_vec, proto)
    check(abs(crafted_score - target_score) < 1e-3,
          f"crafted a held-out embedding at the target similarity "
          f"(got {crafted_score:.4f}, wanted {target_score:.4f})")

    # Both the genuine look-alike and the wrong-auto file share this exact
    # embedding — realistic, since CLIP embeddings come from pixels, not
    # filenames: two differently-named files can be visually identical to
    # the model.
    vec.upsert(con, fid[similar], crafted_vec, dim=len(crafted_vec))
    vec.upsert(con, fid[wrong_auto], crafted_vec, dim=len(crafted_vec))

    # --- Now hand-tag the positives, train, and simulate the wrong auto-tag -
    for fn in pos_files:
        db.add_manual_tag(con, fid[fn], "yumi", "concept")
    # Simulate the model having (wrongly) auto-tagged wrong_auto, source='clip'
    # — the exact source the UI's reject-auto ("x") button targets.
    db.write_auto_tags(con, fid[wrong_auto], "clip", [AutoTag("concept", "yumi", 0.83)])

    summary = learned.build(con, "concept", "yumi", space="clip")
    check(summary is not None and summary["method"] == "centroid",
          f"learned tag trains a centroid from the 5 manual examples (got {summary})")
    check(abs(summary["threshold"] - threshold_before) < 1e-9,
          "the real trained threshold matches our precomputed value "
          f"(got {summary['threshold']:.4f}, expected {threshold_before:.4f})")
    tag_id = summary["tag_id"]

    def tag_of(file_id):
        q = ("""SELECT source FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
                JOIN categories c ON c.id=t.category_id
                WHERE ft.file_id=? AND t.name='yumi' AND c.name='concept'""")
        row = con.execute(q, (file_id,)).fetchone()
        return row["source"] if row else None

    check(tag_of(fid[similar]) == "learned",
          "genuine look-alike photo legitimately auto-suggested 'yumi' "
          "(source=learned) before anything is rejected")
    check(tag_of(fid[wrong_auto]) == "clip",
          "the wrong auto-tag file still carries its raw 'clip' tag "
          "before the user rejects it")
    print(f"  .. before reject: threshold={threshold_before:.4f}, "
          f"look-alike score={crafted_score:.4f}")

    # --- The user clicks the "x" (reject-auto) button on the wrong tag -----
    # Exercise the real daemon command handler, not a re-implementation.
    msg = {"category": "concept", "name": "yumi", "file_id": fid[wrong_auto]}
    result = _reject_auto_tag(con, msg)
    check(result.get("ok") is True, f"_reject_auto_tag() succeeds (got {result})")

    # Claim 1a: gone from this file immediately.
    check(tag_of(fid[wrong_auto]) is None,
          "rejected tag is removed from the file immediately")

    # Claim 1b (rigorous): even if the model tries to re-produce the exact
    # same wrong output again (simulating a reindex re-running inference),
    # write_auto_tags() must refuse to resurrect it because it's remembered
    # in rejected_tags.
    db.write_auto_tags(con, fid[wrong_auto], "clip", [AutoTag("concept", "yumi", 0.83)])
    check(tag_of(fid[wrong_auto]) is None,
          "a simulated reindex trying to re-tag it the same wrong way is "
          "still refused (rejected_tags suppression in write_auto_tags)")

    # Claim 2: the reject fed a negative example at the SAME embedding as the
    # genuine look-alike photo, retraining + re-applying the learned tag —
    # the look-alike photo must lose its (unprotected, source='learned')
    # suggestion too.
    threshold_after = con.execute(
        "SELECT threshold FROM learned_tags WHERE tag_id=?", (tag_id,)
    ).fetchone()["threshold"]
    n_neg_after = con.execute(
        "SELECT n_neg FROM learned_tags WHERE tag_id=?", (tag_id,)
    ).fetchone()["n_neg"]
    print(f"  .. after reject:  threshold={threshold_after:.4f}, n_neg={n_neg_after}")
    check(n_neg_after >= 1, "the reject recorded a negative few-shot example")
    check(threshold_after > threshold_before,
          f"retraining raised the acceptance threshold "
          f"({threshold_before:.4f} -> {threshold_after:.4f})")
    check(tag_of(fid[similar]) is None,
          "a different, visually-identical file also lost its 'learned' "
          "suggestion after the reject retrained + re-applied the tag")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — reject-auto persistence + negative-example propagation verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
