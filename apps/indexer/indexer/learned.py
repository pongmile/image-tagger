"""Few-shot learned tags — spec §5.3 (M7).

When the base models don't know a label (a new VTuber, a niche outfit), the user
tags ~5-10 examples by hand and the app learns to auto-apply that tag to similar
images. **No model fine-tuning** — we reuse the frozen embeddings already stored:

  * CLIP image embedding (file_vec, §6)   -> `clip` space: character/concept/outfit
  * InsightFace face embedding (faces)     -> `face` space: real people

Mechanism:
  1. Positives = files carrying the tag (manual, or confirmed suggestions).
  2. Prototype = mean of L2-normalized positive embeddings (a centroid).
  3. Score every file by cosine to the prototype; sim >= threshold -> apply the
     tag with source='learned', confidence=sim (a *suggestion* until confirmed).
  4. Once enough examples incl. negatives accrue (default >= 20), upgrade the
     centroid to a linear head (logistic regression on frozen embeddings) — trains
     in ms on CPU, sharper boundary. Falls back to centroid without scikit-learn.

Active learning: confirm -> +1 example, reject -> -1 example + drop the tag on that
file. Both retrain and auto-recalibrate the threshold. Runs on any tier (CPU): a
dot product to score, milliseconds to train the head.
"""
from __future__ import annotations

import math
import pickle
import re
import time

from . import db, vec

FLOOR = {"clip": 0.20, "face": 0.30}   # min cosine threshold per space
LINEAR_MIN = 20                         # examples (pos+neg) to upgrade to a head
MIN_POSITIVES = 5                       # do not generalize from one accidental tag
CHARACTER_NEGATIVES = 3                 # visual character spread needs feedback


# --- embedding access -------------------------------------------------------

def embedding_for(con, file_id: int, space: str):
    """Frozen embedding for a file in the given space, or None."""
    if space == "clip":
        return vec.get_embedding(con, file_id)
    if space == "face":
        rows = con.execute(
            "SELECT embedding FROM faces WHERE file_id=?", (file_id,)
        ).fetchall()
        if not rows:
            return None
        vecs = [db._blob_to_emb(r["embedding"]) for r in rows]
        acc = [sum(col) for col in zip(*vecs)]
        n = math.sqrt(sum(v * v for v in acc)) or 1.0
        return [v / n for v in acc]
    return None


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _eligible_for_auto_apply(con, lt, file_id: int, score: float) -> bool:
    """Guard character suggestions against positive-only CLIP overreach.

    CLIP often clusters art style/scene more strongly than anime identity. With
    no negative examples, an Ashe centroid spread to unrelated Mercy/Pharah
    images while missing newly named Ashe files. Until the user has rejected at
    least a few negatives, character suggestions therefore require transparent
    filename/folder/path-tag evidence for that same name. Other learned concepts
    retain normal visual matching.
    """
    info = con.execute(
        """SELECT t.name, c.name AS category FROM tags t
             JOIN categories c ON c.id=t.category_id WHERE t.id=?""",
        (lt["tag_id"],),
    ).fetchone()
    if info is None or info["category"].lower() != "character" \
            or int(lt["n_neg"] or 0) >= CHARACTER_NEGATIVES:
        return score >= lt["threshold"]
    file_row = con.execute(
        "SELECT path,filename FROM files WHERE id=?", (file_id,)
    ).fetchone()
    if file_row is None:
        return False
    name = str(info["name"])
    # Match the same human word boundary used by search.js; underscore is a
    # filename separator, not part of a character name.
    boundary = re.compile(
        rf"(^|[^\w]|_){re.escape(name)}([^\w]|_|$)", re.IGNORECASE
    )
    evidence = boundary.search(
        f"{file_row['filename'] or ''} {file_row['path'] or ''}"
    ) is not None or con.execute(
        """SELECT 1 FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
             JOIN categories c ON c.id=t.category_id
            WHERE ft.file_id=? AND ft.source='path' AND lower(t.name)=lower(?)
            LIMIT 1""",
        (file_id, name),
    ).fetchone() is not None
    # Exact transparent name/path evidence is stronger than a positive-only
    # character centroid, but still require the model's broad similarity floor.
    return evidence and score >= FLOOR.get(lt["space"], 0.20)


# --- examples ---------------------------------------------------------------

def add_example(con, tag_id: int, file_id: int, label: int, origin: str) -> None:
    with con:
        con.execute(
            "INSERT INTO tag_examples (tag_id, file_id, label, origin, added_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(tag_id, file_id) DO UPDATE SET "
            "label=excluded.label, origin=excluded.origin, added_at=excluded.added_at",
            (tag_id, file_id, label, origin, int(time.time())),
        )


def _seed_from_manual(con, tag_id: int) -> None:
    """Every file already carrying this tag by hand is a positive example."""
    rows = con.execute(
        "SELECT file_id FROM file_tags WHERE tag_id=? AND source='manual'",
        (tag_id,),
    ).fetchall()
    for r in rows:
        add_example(con, tag_id, r["file_id"], +1, "manual")


def _examples(con, tag_id: int, space: str):
    """Return (pos_embs, neg_embs, rejected_file_ids)."""
    rows = con.execute(
        "SELECT file_id, label FROM tag_examples WHERE tag_id=?", (tag_id,)
    ).fetchall()
    pos, neg, rejected = [], [], set()
    for r in rows:
        emb = embedding_for(con, r["file_id"], space)
        if r["label"] < 0:
            rejected.add(r["file_id"])
        if emb is None:
            continue
        (pos if r["label"] > 0 else neg).append(emb)
    return pos, neg, rejected


# --- train + calibrate ------------------------------------------------------

def train(con, tag_id: int, space: str) -> dict | None:
    """(Re)build the prototype or linear head + auto-calibrated threshold and
    persist to learned_tags. Returns a summary dict, or None if too few positives."""
    pos, neg, _ = _examples(con, tag_id, space)
    if len(pos) < MIN_POSITIVES:
        return None

    proto = _norm([sum(col) for col in zip(*pos)])
    method, classifier_blob, threshold = "centroid", None, None

    if len(pos) + len(neg) >= LINEAR_MIN and len(neg) >= 1:
        clf = _train_linear(pos, neg)
        if clf is not None:
            method, classifier_blob, threshold = "linear", pickle.dumps(clf), 0.5

    if threshold is None:  # centroid calibration
        pos_sims = [_cos(e, proto) for e in pos]
        mean_pos = sum(pos_sims) / len(pos_sims)
        if neg:
            neg_sims = [_cos(e, proto) for e in neg]
            mean_neg = sum(neg_sims) / len(neg_sims)
            threshold = max(FLOOR[space], (mean_pos + mean_neg) / 2)
        else:
            std = (sum((s - mean_pos) ** 2 for s in pos_sims) / len(pos_sims)) ** 0.5
            threshold = max(FLOOR[space], mean_pos - max(2 * std, 0.05))

    with con:
        con.execute(
            """INSERT INTO learned_tags
               (tag_id, space, method, threshold, n_pos, n_neg, prototype,
                classifier, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(tag_id) DO UPDATE SET space=excluded.space,
                 method=excluded.method, threshold=excluded.threshold,
                 n_pos=excluded.n_pos, n_neg=excluded.n_neg,
                 prototype=excluded.prototype, classifier=excluded.classifier,
                 updated_at=excluded.updated_at""",
            (tag_id, space, method, float(threshold), len(pos), len(neg),
             db._emb_to_blob(proto), classifier_blob, int(time.time())),
        )
    return {"method": method, "threshold": threshold,
            "n_pos": len(pos), "n_neg": len(neg)}


def _train_linear(pos, neg):
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
    except Exception:
        return None
    X = np.array(pos + neg, dtype="float32")
    y = np.array([1] * len(pos) + [0] * len(neg))
    clf = LogisticRegression(max_iter=1000, C=1.0)
    clf.fit(X, y)
    return clf


# --- score + apply ----------------------------------------------------------

def _score(lt, emb):
    if lt["method"] == "linear" and lt["classifier"] is not None:
        import numpy as np
        clf = pickle.loads(lt["classifier"])
        return float(clf.predict_proba(np.array([emb], dtype="float32"))[0][1])
    proto = db._blob_to_emb(lt["prototype"])
    return _cos(emb, proto)


def apply(con, tag_id: int) -> int:
    """Re-score the whole library for one learned tag and rewrite its
    source='learned' rows (idempotent). Skips files the user rejected. Returns
    the number of files the tag now applies to."""
    lt = con.execute(
        "SELECT * FROM learned_tags WHERE tag_id=?", (tag_id,)
    ).fetchone()
    if lt is None:
        return 0
    space = lt["space"]
    _, _, rejected = _examples(con, tag_id, space)

    # Candidate files: those with an embedding in this space.
    if space == "clip":
        vec.load(con)
        cand = [r["file_id"] for r in con.execute(
            "SELECT file_id FROM file_vec").fetchall()] if _has_vec(con) else []
    else:
        cand = [r["file_id"] for r in con.execute(
            "SELECT DISTINCT file_id FROM faces").fetchall()]

    applied = 0
    with con:
        con.execute(
            "DELETE FROM file_tags WHERE tag_id=? AND source='learned'", (tag_id,)
        )
    for fid in cand:
        if fid in rejected:
            continue
        emb = embedding_for(con, fid, space)
        if emb is None:
            continue
        s = _score(lt, emb)
        if _eligible_for_auto_apply(con, lt, fid, s):
            with con:
                # Never downgrade a manual/path/wd14 tag: the DO UPDATE only fires
                # on an existing *learned* row (WHERE guard); a manual row is left
                # intact and no learned duplicate is created.
                cur = con.execute(
                    """INSERT INTO file_tags (file_id, tag_id, source, confidence)
                       VALUES (?,?,'learned',?)
                       ON CONFLICT(file_id, tag_id) DO UPDATE SET
                         confidence=excluded.confidence
                       WHERE file_tags.source='learned'""",
                    (fid, tag_id, float(s)),
                )
                changed = bool(cur.rowcount)
                if changed:
                    # Same transaction as the write: refresh_fts() issues its
                    # delete+insert without committing, so the last candidate's
                    # refresh would otherwise never be persisted.
                    db.refresh_fts(con, fid)
            if changed:
                applied += 1
    return applied


def apply_all(con) -> dict:
    """Re-score every trained learned tag against the whole library.

    Pure vector math against embeddings that already exist (no GPU/model
    inference) — cheap enough to run on a throttle even while the main
    indexer is paused, so Learned tags keeps catching up on any file that
    already has an embedding/face instead of waiting for Resume Tagger.
    Untrained tags (no prototype/classifier yet) are skipped, same as a
    manual Train click would skip them. Returns {tag_id: applied_count}.
    """
    tag_ids = [r["tag_id"] for r in con.execute(
        "SELECT tag_id FROM learned_tags WHERE prototype IS NOT NULL OR classifier IS NOT NULL"
    ).fetchall()]
    return {tag_id: apply(con, tag_id) for tag_id in tag_ids}


def apply_to_file(con, file_id: int, space: str = "clip") -> int:
    """Score one newly embedded file against every learned tag in `space`.

    This closes the online-learning loop: once a centroid exists, later index
    jobs immediately add/remove its learned suggestions instead of requiring
    the user to press Train again after the whole library has embeddings.
    Manual/base-model tags are never overwritten.
    """
    embedding = embedding_for(con, file_id, space)
    if embedding is None:
        return 0
    rows = con.execute(
        "SELECT * FROM learned_tags WHERE space=?", (space,)
    ).fetchall()
    changed = 0
    with con:
        for lt in rows:
            rejected = con.execute(
                "SELECT 1 FROM tag_examples WHERE tag_id=? AND file_id=? AND label<0",
                (lt["tag_id"], file_id),
            ).fetchone() is not None
            current = con.execute(
                "SELECT source FROM file_tags WHERE tag_id=? AND file_id=?",
                (lt["tag_id"], file_id),
            ).fetchone()
            protected = current is not None and current["source"] != "learned"
            score = _score(lt, embedding)
            if not rejected and not protected and _eligible_for_auto_apply(
                    con, lt, file_id, score):
                con.execute(
                    """INSERT INTO file_tags (file_id,tag_id,source,confidence)
                       VALUES (?,?,'learned',?)
                       ON CONFLICT(file_id,tag_id) DO UPDATE SET
                         source='learned', confidence=excluded.confidence
                       WHERE file_tags.source='learned'""",
                    (file_id, lt["tag_id"], float(score)),
                )
                changed += 1
            elif current is not None and current["source"] == "learned":
                con.execute(
                    "DELETE FROM file_tags WHERE file_id=? AND tag_id=? AND source='learned'",
                    (file_id, lt["tag_id"]),
                )
                changed += 1
        if changed:
            db.refresh_fts(con, file_id)
    return changed


def _has_vec(con) -> bool:
    try:
        con.execute("SELECT 1 FROM file_vec LIMIT 1")
        return True
    except Exception:
        return False


# --- high-level operations (used by CLI/UI) ---------------------------------

def build(con, category: str, name: str, space: str = "clip") -> dict | None:
    """Create/refresh a learned tag from its manual examples, then apply it."""
    tag_id = db.get_or_create_tag(con, name, category)
    _seed_from_manual(con, tag_id)
    summary = train(con, tag_id, space)
    if summary is None:
        return None
    summary["applied"] = apply(con, tag_id)
    summary["tag_id"] = tag_id
    return summary


def forget(con, tag_id: int) -> dict:
    """Undo everything the few-shot loop did for one tag, keeping the tag (§5.3).

    A learned tag that has gone wrong -- a centroid that drifted onto the wrong
    cluster, feedback that taught it the opposite of what was meant -- has no
    way back short of this: retraining only ever *adds* to the same examples,
    and `apply()` rewrites the same bad rows. So remove exactly what the loop
    created and nothing else:

      * every ``source='learned'`` row (the "applied to N files" count),
      * the trained prototype/head, so nothing auto-applies again and
        ``apply_to_file()`` stops scoring newly indexed files against it,
      * the accumulated examples, so the next lesson starts from scratch
        instead of immediately relearning the same mistake,
      * the rejections that only existed because this tag was being suggested.

    Manual/path/wd14/clip rows for the same tag are deliberately untouched: the
    tag falls back to exactly the tagging it would have had if it had never
    been taught, and the hand tagging that seeded it survives (``build()``
    re-seeds from it via ``_seed_from_manual`` if the user teaches it again).
    Rejections recorded against *other* sources survive too -- they are what
    stops ``write_auto_tags()`` resurrecting a wd14/clip tag on the next
    reindex, and have nothing to do with the learned layer.
    """
    affected = [r["file_id"] for r in con.execute(
        "SELECT file_id FROM file_tags WHERE tag_id=? AND source='learned'",
        (tag_id,),
    ).fetchall()]
    with con:
        unapplied = con.execute(
            "DELETE FROM file_tags WHERE tag_id=? AND source='learned'", (tag_id,)
        ).rowcount
        examples = con.execute(
            "DELETE FROM tag_examples WHERE tag_id=?", (tag_id,)
        ).rowcount
        con.execute(
            "DELETE FROM rejected_tags WHERE tag_id=? AND source='learned'", (tag_id,)
        )
        trained = con.execute(
            "DELETE FROM learned_tags WHERE tag_id=?", (tag_id,)
        ).rowcount
    # refresh_fts() only issues the delete+insert; it never commits, so this
    # must stay inside a transaction. Left outside one, the tags disappear from
    # file_tags while files_fts keeps matching the tag text, and the tag goes on
    # being findable by search on every file it was ever applied to.
    with con:
        for fid in affected:
            db.refresh_fts(con, fid)
    return {"ok": True, "unapplied": unapplied, "examples_cleared": examples,
            "was_trained": bool(trained)}


def confirm(con, tag_id: int, file_id: int, space: str) -> None:
    add_example(con, tag_id, file_id, +1, "confirmed")
    with con:
        con.execute(
            "DELETE FROM rejected_tags WHERE tag_id=? AND file_id=?", (tag_id, file_id)
        )
    train(con, tag_id, space)
    apply(con, tag_id)
    # apply() deletes+reinserts every 'learned' row for this tag while
    # re-scoring the whole library, so confirmed_at has to be set *after* it —
    # otherwise the very call that confirms the tag would immediately wipe the
    # marker it just set. This is what lets the UI show "confirmed" for good
    # instead of "suggested" again on the next visit to this file.
    with con:
        con.execute(
            "UPDATE file_tags SET confirmed_at=? WHERE tag_id=? AND file_id=?",
            (int(time.time()), tag_id, file_id),
        )


def reject(con, tag_id: int, file_id: int, space: str) -> None:
    reject_tag(con, tag_id, file_id, "learned", space)


def confirm_tag(con, tag_id: int, file_id: int, source: str, space: str = "clip") -> None:
    """Mark an auto-tag of any source as user-confirmed on one file (§9):
    durable confirmed_at marker (so the UI shows it as confirmed for good,
    not just this visit) and — for model-driven sources — a positive few-shot
    example, symmetric with reject_tag()'s negative one, so confirming a
    wd14/clip tag actually reinforces recognition instead of being pure UI
    decoration. Safe to call even when this tag has no learned_tags row yet
    (train()/apply() both no-op gracefully in that case)."""
    with con:
        con.execute(
            "UPDATE file_tags SET confirmed_at=? WHERE tag_id=? AND file_id=?",
            (int(time.time()), tag_id, file_id),
        )
    if source in ("wd14", "clip", "learned"):
        add_example(con, tag_id, file_id, +1, "confirmed")
        train(con, tag_id, space)
        apply(con, tag_id)
        # apply() re-scores/reinserts every 'learned' row for this tag, which
        # would otherwise touch file_tags again after our UPDATE above and
        # could leave confirmed_at behind for a file that just got promoted
        # to 'learned' by this same call -- reassert it last, same ordering
        # reason as confirm()'s comment above.
        with con:
            con.execute(
                "UPDATE file_tags SET confirmed_at=? WHERE tag_id=? AND file_id=?",
                (int(time.time()), tag_id, file_id),
            )


def reject_tag(con, tag_id: int, file_id: int, source: str, space: str = "clip") -> None:
    """Remove a wrong auto-tag of any source from one file (§9): delete it,
    remember the rejection durably so write_auto_tags() never silently
    resurrects it on the next reindex/rescan, and — for model-driven sources —
    feed it to the few-shot learner as a negative example so visually similar
    images stop being suggested it too. Safe to call even when this tag has no
    learned_tags row yet (train()/apply() both no-op gracefully in that case)."""
    with con:
        con.execute("DELETE FROM file_tags WHERE tag_id=? AND file_id=?", (tag_id, file_id))
        con.execute(
            "INSERT INTO rejected_tags (file_id, tag_id, source, rejected_at) "
            "VALUES (?,?,?,?) ON CONFLICT(file_id, tag_id) DO UPDATE SET "
            "source=excluded.source, rejected_at=excluded.rejected_at",
            (file_id, tag_id, source, int(time.time())),
        )
    db.refresh_fts(con, file_id)
    if source in ("wd14", "clip", "learned"):
        add_example(con, tag_id, file_id, -1, "rejected")
        train(con, tag_id, space)
        apply(con, tag_id)
