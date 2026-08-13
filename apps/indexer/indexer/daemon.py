"""Control daemon — the Electron↔Python bridge (spec §4 control channel).

A long-lived process the desktop app spawns once. It speaks line-delimited JSON
over stdio (one request per line in, one response per line out), so the app can
drive indexing and the *semantic* search path (which needs the CLIP model) without
paying model-load cost per call and without a socket/port.

  request:  {"id": 7, "cmd": "rescan"}
  response: {"id": 7, "ok": true, "result": {...}}          | {"ok": false, "error": "..."}
  event:    {"event": "progress", "files_done": 12, ...}     (unsolicited, --auto mode)

The search *fast path* stays in Node (better-sqlite3, §4); this daemon only ever
writes to the DB and answers the model-backed queries.

Launch:  python -m indexer.daemon [--auto]
  --auto  start the watchdog watcher + a background worker thread (real app mode).
          Without it, indexing is command-driven (rescan/work) — used by tests.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from . import config, db, heartbeat, status
from .scan import rescan
from .worker import drain


_emit_lock = threading.Lock()
_dependency_lock = threading.Lock()
_downloads_lock = threading.RLock()
_download_state_path = config.APP_DIR / "download-state.json"
try:
    _downloads: dict[str, dict] = {
        row["model"]: row for row in json.loads(
            _download_state_path.read_text(encoding="utf-8"))
        if isinstance(row, dict) and row.get("model")
    }
except Exception:
    _downloads = {}
for _entry in _downloads.values():
    if _entry.get("state") in ("queued", "running"):
        _entry.update(state="error", ok=False, finished_at=time.time(),
                      error="Download was interrupted when the app stopped",
                      message="Interrupted; click download to retry")

# Runtime control shared with the --auto worker/watcher (§7/§12): pause gates the
# worker loop without killing it; _state holds the live watchdog observer so the
# auto/manual toggle can start/stop it at runtime.
_paused = threading.Event()
_state: dict = {"observer": None}
_scan_lock = threading.Lock()


def _run_scan_async(root_id: int | None, priority: int) -> dict:
    """rescan() walks the filesystem (os.walk + a stat() per file), which can
    take a long time on large libraries and is often much slower still on
    network/cloud-synced drives. The command loop below handles one request
    at a time on a single thread (`for line in sys.stdin`), so running a scan
    inline used to block *every other request* — including a cheap read like
    "roots" — for the scan's full duration: clicking Rescan on the Search
    page could make the Sources page appear to lose its whole root list for
    however long the scan took, simply because its query was stuck in line
    behind the scan. Run the actual walk on its own thread with its own
    connection so the command loop stays responsive; the caller gets the real
    result via a "scan_done" event instead of the immediate RPC response."""
    if not _scan_lock.acquire(blocking=False):
        return {"started": False, "error": "a scan is already running"}

    def runner():
        con2 = db.connect(check_same_thread=False)
        try:
            res = rescan(con2, root_id=root_id, priority=priority)
            _emit({"event": "scan_done", "ok": True, "root_id": root_id,
                   "added": res.added, "changed": res.changed,
                   "removed": res.removed, "unchanged": res.unchanged,
                   "revived": res.revived})
        except Exception as e:
            _emit({"event": "scan_done", "ok": False, "root_id": root_id,
                   "error": repr(e)})
        finally:
            _scan_lock.release()
            _emit({"event": "progress", **db.progress(con2),
                   "paused": _paused.is_set(),
                   "mode": db.get_setting(con2, "index_mode", "auto")})

    threading.Thread(target=runner, daemon=True).start()
    return {"started": True}


def _emit(obj: dict) -> None:
    # Serialize writes: worker/download threads + the main loop all share stdout,
    # and interleaved partial lines would corrupt the JSON-RPC stream.
    line = json.dumps(obj) + "\n"
    with _emit_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def _semantic(con, query: str, k: int):
    """text -> CLIP text embedding -> sqlite-vec KNN -> file rows (§8)."""
    from . import config, vec
    from .models import clip
    if not vec.load(con):
        return {"available": False, "reason": "sqlite-vec not installed", "hits": []}
    from . import engine as engine_config
    engine = clip.get_engine(str(engine_config.active_model_dir(con, "clip")),
                             model_name=config.CLIP_MODEL,
                             pretrained=config.CLIP_PRETRAINED)
    if isinstance(engine, clip.NullClipEngine):
        return {"available": False, "reason": "CLIP backend unavailable", "hits": []}
    tvec = engine.encode_texts([query])[0]
    hits = []
    for fid, dist in vec.knn(con, tvec, k=k):
        row = con.execute("SELECT id, path, filename FROM files WHERE id=?",
                          (fid,)).fetchone()
        if row:
            hits.append({"id": row["id"], "path": row["path"],
                         "filename": row["filename"], "distance": dist})
    return {"available": True, "hits": hits}


def handle(con, msg: dict) -> dict:
    cmd = msg.get("cmd")
    rid = msg.get("id")
    try:
        if cmd == "ping":
            result = "pong"
        elif cmd == "add_root":
            db.add_root(con, msg["path"], mode=msg.get("mode", "include"),
                        recursive=msg.get("recursive", True))
            result = {"ok": True}
        elif cmd == "add_exclude":
            db.add_exclude_pattern(con, msg["pattern"])
            result = {"ok": True}
        elif cmd == "rescan":
            result = _run_scan_async(None, 50)
        elif cmd == "rescan_root":
            result = _run_scan_async(int(msg["root_id"]), 50)
        elif cmd == "recaption_file":
            # Regenerate just the caption/description for one file (§11) — the
            # preview pane's "↻ re-Description" button. Narrower than
            # reindex_file: doesn't touch OCR/wd14/clip/faces.
            import os
            from . import engine as engine_config
            row = con.execute("SELECT id FROM files WHERE path=?",
                              (msg["path"],)).fetchone()
            ready, ready_reason = engine_config.facet_model_ready(con, "caption")
            if row is None:
                result = {"ok": False, "error": "file not indexed"}
            elif not os.path.isfile(msg["path"]):
                result = {"ok": False, "error": "file no longer exists"}
            elif not config.facet_enabled(con, "caption"):
                result = {"ok": False, "error": "captioning is disabled — enable it on the Models tab"}
            elif not ready:
                result = {"ok": False,
                          "error": f"{ready_reason} — install it on the Models tab"}
            else:
                jid = db.enqueue_job(con, row["id"], "caption", priority=100)
                result = {"ok": True, "queued": True, "job_id": jid}
            _emit({"event": "progress", **db.progress(con),
                   "paused": _paused.is_set(),
                   "mode": db.get_setting(con, "index_mode", "auto")})
        elif cmd == "reindex_file":
            # Re-run ingest+infer for one file (§11 re-caption / refresh a stale
            # result). Enqueues a reindex job the worker picks up.
            import os
            row = con.execute("SELECT id FROM files WHERE path=?",
                              (msg["path"],)).fetchone()
            if row is None:
                result = {"ok": False, "error": "file not indexed"}
            elif not os.path.isfile(msg["path"]):
                db.delete_file(con, msg["path"])
                result = {"ok": False, "removed": True,
                          "error": "file no longer exists; stale index row removed"}
            else:
                jid = db.enqueue_job(con, row["id"], "reindex", priority=100)
                result = {"ok": True, "queued": True, "job_id": jid}
            _emit({"event": "progress", **db.progress(con),
                   "paused": _paused.is_set(),
                   "mode": db.get_setting(con, "index_mode", "auto")})
        elif cmd == "work":
            result = {"processed": drain(con, max_jobs=msg.get("max"))}
        elif cmd == "progress":
            result = {**db.progress(con), "paused": _paused.is_set(),
                      "mode": db.get_setting(con, "index_mode", "auto")}
        elif cmd == "pause":
            _paused.set()
            result = {"paused": True}
        elif cmd == "resume":
            _paused.clear()
            result = {"paused": False}
        elif cmd == "set_mode":
            result = _set_mode(con, msg.get("mode", "auto"))
        elif cmd == "retry_errors":
            fid = msg.get("file_id")
            result = {"requeued": db.retry_errors(con, int(fid) if fid is not None else None)}
        elif cmd == "reindex_all":
            # Backfill an existing library after a threshold/variant change (§5.3
            # UX) — unlike rescan/retry_errors this touches every already-`done`
            # file, not just new/changed/errored ones.
            result = {"queued": db.reindex_all(con)}
            _emit({"event": "progress", **db.progress(con),
                   "paused": _paused.is_set(),
                   "mode": db.get_setting(con, "index_mode", "auto")})
        elif cmd == "recaption_root":
            from . import engine as engine_config
            ready, ready_reason = engine_config.facet_model_ready(con, "caption")
            if not config.facet_enabled(con, "caption"):
                result = {"ok": False, "error": "captioning is disabled — enable it on the Models tab"}
            elif not ready:
                result = {"ok": False,
                          "error": f"{ready_reason} — install it on the Models tab"}
            else:
                result = {"ok": True, "queued": db.recaption_root(con, int(msg["root_id"])),
                          "root_id": int(msg["root_id"])}
                _emit({"event": "progress", **db.progress(con),
                       "paused": _paused.is_set(),
                       "mode": db.get_setting(con, "index_mode", "auto")})
        elif cmd == "reindex_root":
            # Scoped reindex_all — re-process just one source folder (Sources
            # page per-row "Index" action).
            result = {"queued": db.reindex_root(con, int(msg["root_id"])),
                      "root_id": int(msg["root_id"])}
            _emit({"event": "progress", **db.progress(con),
                   "paused": _paused.is_set(),
                   "mode": db.get_setting(con, "index_mode", "auto")})
        elif cmd == "list_errors":
            root_id = msg.get("root_id")
            result = {"errors": db.list_errors(
                con, int(root_id) if root_id is not None else None,
                int(msg.get("limit", 200)))}
        elif cmd == "roots":
            result = {"roots": db.list_roots(con),
                      "excludes": db.list_exclude_patterns(con)}
        elif cmd == "remove_root":
            db.remove_root(con, int(msg["root_id"]))
            result = {"ok": True}
        elif cmd == "toggle_root":
            db.set_root_enabled(con, int(msg["root_id"]), bool(msg["enabled"]))
            result = {"ok": True}
        elif cmd == "add_exclude_pattern":
            db.add_exclude_pattern(con, msg["pattern"])
            result = {"ok": True}
        elif cmd == "remove_exclude":
            db.remove_exclude_pattern(con, int(msg["rule_id"]))
            result = {"ok": True}
        elif cmd == "toggle_exclude":
            db.set_exclude_enabled(con, int(msg["rule_id"]), bool(msg["enabled"]))
            result = {"ok": True}
        elif cmd == "rename_tag":
            result = db.rename_tag(con, msg["category"], msg["old"], msg["new"])
        elif cmd == "list_tags":
            result = {"tags": db.list_all_tags(con, int(msg.get("limit", 2000)))}
        elif cmd == "learn_status":
            result = {"count": db.manual_tag_count(con, msg["category"], msg["name"])}
        elif cmd == "learn":
            result = _learn(con, msg["category"], msg["name"], msg.get("space", "clip"))
        elif cmd == "learn_confirm":
            result = _learn_feedback(con, msg, +1)
        elif cmd == "learn_reject":
            result = _learn_feedback(con, msg, -1)
        elif cmd == "reject_tag":
            result = _reject_auto_tag(con, msg)
        elif cmd == "confirm_tag":
            result = _confirm_auto_tag(con, msg)
        elif cmd == "list_learned_tags":
            from . import learned as _learned
            result = {"tags": db.list_learned_tags(con),
                      "in_progress": db.list_learning_progress(con),
                      "min_positives": _learned.MIN_POSITIVES}
        elif cmd == "get_setting":
            result = db.get_setting(con, msg["key"], msg.get("default"))
        elif cmd == "set_setting":
            db.set_setting(con, msg["key"], msg["value"])
            result = {"ok": True}
        elif cmd == "semantic":
            result = _semantic(con, msg["query"], int(msg.get("k", 20)))
        elif cmd == "download":
            result = _start_download(msg["model"], msg.get("variant"))
        elif cmd == "install_dependency":
            result = _start_dependency_install(msg["facet"])
        elif cmd == "download_status":
            result = {"downloads": _download_snapshot()}
        elif cmd == "doctor":
            from . import engine
            cfg = engine.get_engine_config(con)
            result = {"tier": cfg["tier"], "tier_source": cfg["tier_source"],
                      "providers": cfg["onnx_providers"],
                      "torch_device": cfg["torch_device"],
                      "hardware": cfg["hardware"]}
        elif cmd == "facets":
            from . import engine
            result = engine.facet_readiness(con)
        elif cmd == "set_facet_enabled":
            result = _set_facet_enabled(con, msg["facet"], bool(msg["enabled"]))
        elif cmd == "models_dir":
            result = str(db.get_models_dir(con))
        elif cmd == "variants":
            from . import engine
            result = engine.variants_view(con)
        elif cmd == "model_state":
            from . import engine
            result = {"facets": engine.facet_readiness(con),
                      "variants": engine.variants_view(con),
                      "downloads": _download_snapshot(),
                      "models_dir": str(db.get_models_dir(con))}
        elif cmd == "set_variant":
            # NB: the variant is 'variant', never 'id' — 'id' is the RPC id.
            result = _set_variant(con, msg["facet"], msg["variant"])
        elif cmd == "persons":
            result = db.list_persons(con)
        elif cmd == "person_files":
            result = db.person_files(con, int(msg["person_id"]))
        elif cmd == "name_person":
            db.name_person(con, int(msg["person_id"]), msg["name"])
            result = {"ok": True}
        elif cmd == "merge_persons":
            db.merge_persons(con, int(msg["src"]), int(msg["dst"]))
            result = {"ok": True}
        elif cmd == "heartbeat":
            # Cheap main-thread-only liveness probe (§7 resilience): lets the
            # Electron side tell "process alive, worker loop stuck" apart from
            # "process alive, worker loop fine" without touching the DB.
            beat = heartbeat.last()
            result = {"alive": True, "now": time.time(), "worker_last_beat": beat,
                      "worker_age": (time.time() - beat) if beat else None}
        elif cmd == "stop":
            result = {"stopping": True}
        else:
            return {"id": rid, "ok": False, "error": f"unknown cmd '{cmd}'"}
        return {"id": rid, "ok": True, "result": result}
    except Exception as e:  # never crash the daemon on one bad command
        return {"id": rid, "ok": False, "error": repr(e)}


def _set_variant(con, facet: str, variant_id: str) -> dict:
    """Persist a model-variant choice (§5.2). If the CLIP variant changes to a
    different embedding dimension, drop file_vec so the library re-indexes into
    the new space (semantic search + learned tags rebuild on next index)."""
    from . import engine, vec
    vs = engine.VARIANTS.get(facet, [])
    new = next((v for v in vs if v["id"] == variant_id), None)
    if new is None:
        return {"ok": False, "error": f"unknown variant '{variant_id}' for {facet}"}
    reindex = False
    recaptioned = 0
    if facet == "clip":
        old = engine.selected_variant(con, "clip")
        if old and old.get("dim") != new.get("dim"):
            vec.drop(con)
            reindex = True
    elif facet == "caption":
        old = engine.selected_variant(con, "caption")
        if old and old.get("id") != variant_id:
            # Every file that already has a caption was written by the
            # *previous* model — it's now stale, not just "missing", so queue
            # a fresh "caption" job for it too (enqueue_missing_captions only
            # catches files with no caption at all).
            recaptioned = db.enqueue_job_for_captioned_files(con)
    db.set_setting(con, f"{facet}_variant", variant_id)
    return {"ok": True, "facet": facet, "id": variant_id, "reindex_needed": reindex,
            "recaptioning": recaptioned}


def _set_facet_enabled(con, facet: str, enabled: bool) -> dict:
    """Persist a Models-screen toggle. Workers consult this setting per job,
    so the change is live and does not depend on process environment variables.
    Refuse to enable a facet whose dependency/model is not ready; the UI can
    then guide the user to install/download it instead of silently doing no work.
    """
    from . import engine
    allowed = {"ocr", "wd14", "clip", "faces", "caption"}
    if facet not in allowed:
        return {"ok": False, "error": f"unknown facet '{facet}'"}
    row = next((x for x in engine.facet_readiness(con)
                if x.get("facet") == facet), None)
    if enabled and row and (not row["dep_ok"] or not row["model_ok"]):
        missing = "dependency" if not row["dep_ok"] else "model"
        return {"ok": False, "error": f"{missing} not ready", "facet": row}
    if enabled:
        # This RPC is handled on the main thread. Preloading extension modules
        # here prevents the first automatic-index job from importing them in a
        # Windows background worker and deadlocking in OpenBLAS/the DLL loader.
        _preload_model_runtime(facet)
    db.set_setting(con, f"{facet}_enabled", "1" if enabled else "0")
    return {"ok": True, "facet": facet, "enabled": enabled}


def _set_mode(con, mode: str) -> dict:
    """auto = watchdog on (files index as they change); manual = watcher off,
    the user drives indexing with Rescan (§12 auto/manual toggle)."""
    mode = "manual" if mode == "manual" else "auto"
    db.set_setting(con, "index_mode", mode)
    obs = _state.get("observer")
    if mode == "manual" and obs is not None:
        try:
            obs.stop()
        except Exception:
            pass
        _state["observer"] = None
    elif mode == "auto" and _state.get("observer") is None:
        try:
            from .watcher import start_watchers
            _state["observer"] = start_watchers(db.connect(check_same_thread=False))
        except Exception as e:
            _emit({"event": "warning", "message": f"watcher not started: {e!r}"})
    return {"mode": mode}


def _learn(con, category: str, name: str, space: str) -> dict:
    """Teach a few-shot tag from its manual examples (§5.3). Honest about the
    minimum: needs a handful of hand-tagged images before it can generalize."""
    from . import learned
    have = db.manual_tag_count(con, category, name)
    if have < learned.MIN_POSITIVES:
        return {"ok": False,
                "error": f"needs at least {learned.MIN_POSITIVES} manual examples",
                "count": have, "usable": 0}
    prepared = 0
    prep: dict = {}
    if space == "clip":
        prep = _prepare_clip_examples(con, category, name)
        if not prep["ok"]:
            return {"ok": False, "count": have, **prep}
        prepared = prep["prepared"]
    elif space == "face":
        prep = _prepare_face_examples(con, category, name)
        if not prep["ok"]:
            return {"ok": False, "count": have, **prep}
        prepared = prep["prepared"]
    summary = learned.build(con, category, name, space=space)
    if summary is None:
        return {"ok": False, "error": "not enough usable embeddings yet",
                "count": have, "usable": prep.get("usable", 0), "prepared": prepared}
    return {"ok": True, **summary, "count": have, "prepared": prepared,
            "queued": prep.get("queued", 0)}


def _prepare_clip_examples(con, category: str, name: str) -> dict:
    """Create missing CLIP embeddings for the explicitly hand-tagged examples.

    Learned-tag training used to assume a full-library CLIP pass had already
    happened. When CLIP was disabled, the UI could report 17 examples while the
    learner saw zero usable vectors. A deliberate Train click now enables the
    ready CLIP facet and embeds only those examples immediately.
    """
    from . import engine, vec
    from .models import clip
    from .worker import INFERENCE_LOCK

    if not vec.load(con):
        return {"ok": False, "error": "sqlite-vec dependency is not ready",
                "prepared": 0, "usable": 0}

    rows = con.execute(
        """SELECT DISTINCT f.id, f.path FROM files f
             JOIN file_tags ft ON ft.file_id=f.id
             JOIN tags t ON t.id=ft.tag_id
             JOIN categories c ON c.id=t.category_id
            WHERE c.name=? AND t.name=? AND ft.source='manual'""",
        (category, name),
    ).fetchall()
    missing = [r for r in rows if vec.get_embedding(con, r["id"]) is None]
    if not missing:
        return {"ok": True, "prepared": 0, "usable": len(rows),
                "queued": db.enqueue_missing_clip_embeddings(con)}

    enabled = _set_facet_enabled(con, "clip", True)
    if not enabled.get("ok"):
        return {"ok": False,
                "error": f"CLIP is not ready: {enabled.get('error', 'unknown error')}",
                "prepared": 0, "usable": len(rows) - len(missing)}

    cfg = engine.get_engine_config(con)
    cv = engine.selected_variant(con, "clip") or {}
    clip_engine = clip.get_engine(
        str(engine.active_model_dir(con, "clip")),
        model_name=cv.get("model", config.CLIP_MODEL),
        pretrained=cv.get("pretrained", config.CLIP_PRETRAINED),
        device=cfg["torch_device"],
    )
    if isinstance(clip_engine, clip.NullClipEngine):
        return {"ok": False,
                "error": f"CLIP could not start: {clip.last_error() or 'backend unavailable'}",
                "prepared": 0, "usable": len(rows) - len(missing)}

    prepared = 0
    with INFERENCE_LOCK:
        for row in missing:
            try:
                embedding = clip_engine.encode_image(row["path"])
                if embedding is not None and vec.upsert(
                        con, row["id"], embedding, dim=len(embedding)):
                    prepared += 1
            except Exception:
                # Continue through the example set; the structured usable count
                # below makes a partial/corrupt set visible to the UI.
                continue
    usable = sum(1 for row in rows if vec.get_embedding(con, row["id"]) is not None)
    if usable < 5:
        return {"ok": False,
                "error": f"only {usable} usable CLIP embeddings; 5 are required",
                "prepared": prepared, "usable": usable}
    return {"ok": True, "prepared": prepared, "usable": usable,
            "queued": db.enqueue_missing_clip_embeddings(con)}


def _prepare_face_examples(con, category: str, name: str) -> dict:
    """Create missing face embeddings for the explicitly hand-tagged examples.

    Mirrors _prepare_clip_examples for the 'face' embedding space (§5.3): a
    deliberate Train click enables the Faces facet and detects only those
    examples immediately, instead of requiring the whole library to already
    have run through face detection first.
    """
    from . import engine
    from .models import faces
    from .worker import INFERENCE_LOCK

    rows = con.execute(
        """SELECT DISTINCT f.id, f.path FROM files f
             JOIN file_tags ft ON ft.file_id=f.id
             JOIN tags t ON t.id=ft.tag_id
             JOIN categories c ON c.id=t.category_id
            WHERE c.name=? AND t.name=? AND ft.source='manual'""",
        (category, name),
    ).fetchall()

    def has_face(file_id: int) -> bool:
        return con.execute(
            "SELECT 1 FROM faces WHERE file_id=? LIMIT 1", (file_id,)
        ).fetchone() is not None

    missing = [r for r in rows if not has_face(r["id"])]
    if not missing:
        return {"ok": True, "prepared": 0, "usable": len(rows), "queued": 0}

    enabled = _set_facet_enabled(con, "faces", True)
    if not enabled.get("ok"):
        return {"ok": False,
                "error": f"Faces is not ready: {enabled.get('error', 'unknown error')}",
                "prepared": 0, "usable": len(rows) - len(missing)}

    cfg = engine.get_engine_config(con)
    fv = engine.selected_variant(con, "insightface") or {}
    face_engine = faces.get_engine(
        str(engine.active_model_dir(con, "insightface")),
        providers=cfg["onnx_providers"], pack=fv.get("pack", "buffalo_l"),
    )
    if isinstance(face_engine, faces.NullFaceEngine):
        return {"ok": False,
                "error": f"Faces could not start: {faces.last_error() or 'backend unavailable'}",
                "prepared": 0, "usable": len(rows) - len(missing)}

    prepared = 0
    with INFERENCE_LOCK:
        for row in missing:
            try:
                detected = face_engine.detect(row["path"])
                db.write_faces(con, row["id"], detected, threshold=config.FACE_THRESHOLD)
                if detected:
                    prepared += 1
            except Exception:
                # Continue through the example set; the structured usable count
                # below makes a partial/corrupt set visible to the UI.
                continue
    usable = sum(1 for row in rows if has_face(row["id"]))
    if usable < 5:
        return {"ok": False,
                "error": f"only {usable} example(s) with a detectable face; 5 are required",
                "prepared": prepared, "usable": usable}
    return {"ok": True, "prepared": prepared, "usable": usable, "queued": 0}


def _learn_feedback(con, msg: dict, label: int) -> dict:
    """Confirm (+1) or reject (-1) a learned suggestion on one file, then retrain
    + re-apply so accuracy climbs with use (§5.3 active learning)."""
    from . import learned
    row = con.execute(
        """SELECT lt.tag_id, lt.space FROM learned_tags lt
             JOIN tags t ON t.id=lt.tag_id
             JOIN categories c ON c.id=t.category_id
            WHERE c.name=? AND t.name=?""",
        (msg["category"], msg["name"]),
    ).fetchone()
    if row is None:
        return {"ok": False, "error": "no learned tag by that name"}
    fn = learned.confirm if label > 0 else learned.reject
    fn(con, int(row["tag_id"]), int(msg["file_id"]), row["space"])
    return {"ok": True}


def _reject_auto_tag(con, msg: dict) -> dict:
    """Remove a wrong auto-tag (wd14/clip/learned/manual/path) from one file:
    delete it, remember the rejection so reindex/rescan never silently re-adds
    it, and — for model-driven sources — feed it to the few-shot learner as a
    negative example so visually similar images stop being suggested it too
    (§9/§5.3). Unlike learn_confirm/learn_reject, this works even when the tag
    has no learned_tags row yet, since it's meant for the plain "×" on any
    auto-tag chip, not just already-learned suggestions."""
    from . import learned
    tag_row = con.execute(
        """SELECT t.id FROM tags t JOIN categories c ON c.id=t.category_id
            WHERE c.name=? AND t.name=?""",
        (msg["category"], msg["name"]),
    ).fetchone()
    if tag_row is None:
        return {"ok": False, "error": "no such tag"}
    file_id = int(msg["file_id"])
    current = con.execute(
        "SELECT source FROM file_tags WHERE tag_id=? AND file_id=?",
        (tag_row["id"], file_id),
    ).fetchone()
    source = current["source"] if current else str(msg.get("source") or "wd14")
    learned.reject_tag(con, int(tag_row["id"]), file_id, source)
    return {"ok": True}


def _confirm_auto_tag(con, msg: dict) -> dict:
    """Mark an auto-tag (wd14/clip/learned) as user-confirmed on one file: a
    durable confirmed_at marker, plus — for model-driven sources — a positive
    few-shot example (§9/§5.3), mirroring _reject_auto_tag's "×" handling for
    the plain "✓" on any auto-tag chip, not just already-learned suggestions."""
    from . import learned
    tag_row = con.execute(
        """SELECT t.id FROM tags t JOIN categories c ON c.id=t.category_id
            WHERE c.name=? AND t.name=?""",
        (msg["category"], msg["name"]),
    ).fetchone()
    if tag_row is None:
        return {"ok": False, "error": "no such tag"}
    file_id = int(msg["file_id"])
    current = con.execute(
        "SELECT source FROM file_tags WHERE tag_id=? AND file_id=?",
        (tag_row["id"], file_id),
    ).fetchone()
    if current is None:
        return {"ok": False, "error": "tag not present on this file"}
    learned.confirm_tag(con, int(tag_row["id"]), file_id, current["source"])
    progress = db.tag_learning_progress(con, int(tag_row["id"]))
    return {"ok": True, **progress, "needed": learned.MIN_POSITIVES,
            "reinforces": current["source"] in ("wd14", "clip", "learned")}


def _download_snapshot() -> list[dict]:
    with _downloads_lock:
        return [dict(value) for value in _downloads.values()]


def _download_get(model: str) -> dict | None:
    with _downloads_lock:
        value = _downloads.get(model)
        return dict(value) if value else None


def _download_update(model: str, **values) -> dict:
    with _downloads_lock:
        state = _downloads.setdefault(model, {"model": model})
        state.update(values)
        state["updated_at"] = time.time()
        try:
            _download_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = _download_state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(_downloads.values())), encoding="utf-8")
            tmp.replace(_download_state_path)
        except Exception:
            pass
        return dict(state)


def _download_progress(model: str, **values) -> None:
    payload = _download_update(model, **values)
    _emit({"event": "download_progress", **payload})


def _download_finish(model: str, ok: bool, **values) -> None:
    payload = _download_update(
        model, state="done" if ok else "error", ok=ok,
        finished_at=time.time(), indeterminate=False if ok else values.get("indeterminate", False),
        pct=100 if ok else values.get("pct"), **{k: v for k, v in values.items()
                                                    if k not in ("indeterminate", "pct")})
    _emit({"event": "download_done", **payload})


def _download_heartbeat(model: str) -> None:
    while True:
        time.sleep(1.0)
        state = _download_get(model)
        if not state or state.get("state") not in ("queued", "running"):
            return
        started = state.get("started_at", time.time())
        state["elapsed_s"] = max(0, int(time.time() - started))
        _emit({"event": "download_progress", **state})


def _begin_download(model: str, target, args: tuple, preload=None) -> dict:
    current = _download_get(model)
    if current and current.get("state") in ("queued", "running"):
        return {"started": False, "busy": True, "model": model, "status": current}
    now = time.time()
    _download_update(model, state="queued", pct=0, indeterminate=True,
                     phase="queued", message="Waiting to start", error=None,
                     started_at=now, finished_at=None, ok=None)
    _emit({"event": "download_progress", **(_download_get(model) or {})})
    threading.Thread(target=_download_heartbeat, args=(model,), daemon=True).start()
    if preload is not None:
        try:
            preload()
        except Exception as exc:
            _download_finish(model, False, error=repr(exc),
                             message="Model runtime could not be loaded")
            return {"started": True, "model": model, "status": _download_get(model)}

    def runner():
        try:
            target(*args)
        except Exception as exc:
            _download_finish(model, False, error=repr(exc),
                             message="Background operation crashed")
    threading.Thread(target=runner, daemon=True).start()
    return {"started": True, "model": model, "status": _download_get(model)}


def _preload_model_runtime(model: str) -> None:
    """Load extension modules on the RPC/main thread before model workers run.

    On Windows, the first import of Torch/OpenBLAS/Scipy from a background
    thread can deadlock in the DLL loader while the main thread is blocked on
    the stdin pipe. Importing packages (not weights) here prevents downloads
    and automatic indexing from remaining indeterminate forever.
    """
    import importlib
    modules = {
        "ocr": ("numpy", "onnxruntime", "rapidocr_onnxruntime"),
        "wd14": ("numpy", "onnxruntime"),
        "clip": ("numpy", "torch", "open_clip"),
        "faces": ("numpy", "onnxruntime", "insightface"),
        "insightface": ("numpy", "onnxruntime", "insightface"),
        "caption": ("numpy", "torch", "transformers", "accelerate"),
        "sklearn": ("numpy", "scipy", "sklearn"),
    }.get(model, ())
    for module in modules:
        importlib.import_module(module)


def _start_download(model: str, variant_id: str | None = None) -> dict:
    """Run a download in the background, streaming progress events so the UI stays
    responsive (§12). Emits download_progress (determinate for onnx byte
    downloads; indeterminate for weight-fetching engines) and a final
    download_done event. variant_id optionally names a specific variant to
    fetch without applying it — see _download_worker."""
    preload = (lambda: _preload_model_runtime(model)) if model in (
        "clip", "insightface", "caption"
    ) else None
    return _begin_download(model, _download_worker, (model, variant_id), preload=preload)


def _start_dependency_install(facet: str) -> dict:
    allowed = {"ocr", "wd14", "clip", "faces", "caption", "joycaption", "sklearn"}
    if facet not in allowed:
        raise ValueError(f"unknown facet '{facet}'")
    key = f"dep:{facet}"
    return _begin_download(key, _dependency_install_worker, (facet,))


def _dependency_install_worker(facet: str) -> None:
    """Install an optional model dependency into the exact Python runtime used
    by the daemon. This makes the packaged Models screen self-service instead
    of telling users to open a terminal and guess which pip to run.
    """
    import os
    import subprocess
    from . import config
    key = f"dep:{facet}"
    _download_progress(key, state="running", phase="installing",
                       message=f"Installing dependency for {facet}",
                       indeterminate=True, pct=None)
    # Do not call engine.detect_hardware() here: it imports onnxruntime/torch,
    # and Windows then locks the very DLLs pip needs to install or replace.
    # The NVIDIA driver utility is sufficient for choosing CUDA vs CPU without
    # loading either target runtime into this long-lived daemon.
    try:
        subprocess.check_output(["nvidia-smi", "-L"], timeout=5,
                                stderr=subprocess.DEVNULL)
        gpu = True
    except Exception:
        gpu = False
    # Broader hardware coverage (§5.2): an Intel NPU ("AI Boost" on Core Ultra)
    # gets OpenVINO; any other display adapter (AMD, Intel Arc/integrated, or a
    # non-CUDA NVIDIA build) falls back to the vendor-agnostic DirectML EP.
    # Both are read-only PnP/WMI queries -- no runtime import, same reasoning
    # as the nvidia-smi check above.
    npu = False
    directml_gpu = False
    if not gpu:
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_PnPEntity | "
                 "Where-Object { $_.Name -match 'NPU|AI Boost' } | "
                 "Select-Object -First 1 -ExpandProperty Name)"],
                timeout=8, stderr=subprocess.DEVNULL, text=True,
            ).strip()
            npu = bool(out)
        except Exception:
            npu = False
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController | "
                 "Select-Object -First 1 -ExpandProperty Name)"],
                timeout=8, stderr=subprocess.DEVNULL, text=True,
            ).strip()
            directml_gpu = bool(out)
        except Exception:
            directml_gpu = False
    pip = [sys.executable, "-m", "pip", "install",
           "--break-system-packages", "--disable-pip-version-check",
           "--no-warn-script-location", "--no-input",
           "--target", str(config.RUNTIME_PACKAGES_DIR)]
    commands: list[list[str]] = []
    import importlib.util

    def installed(module: str) -> bool:
        """A namespace stub is not an installed dependency.

        Interrupted ``pip --target`` runs can leave an empty directory behind;
        ``find_spec`` still reports that as a namespace package, which used to
        make Models incorrectly show the dependency as ready forever.
        """
        importlib.invalidate_caches()
        spec = importlib.util.find_spec(module)
        return bool(spec and spec.origin and spec.origin != "namespace"
                    and Path(spec.origin).is_file())

    def cuda_torch_installed() -> bool:
        """Inspect wheel metadata without importing torch into the daemon.

        Importing torch here would lock its DLLs before a repair install.  The
        generated version.py is enough to distinguish a CUDA wheel from a CPU
        wheel and also catches a partially overwritten target directory.
        """
        if not installed("torch"):
            return False
        try:
            version_text = (config.RUNTIME_PACKAGES_DIR / "torch" / "version.py").read_text(
                encoding="utf-8", errors="replace"
            )
            return "+cu" in version_text and "cuda: Optional[str] = None" not in version_text
        except OSError:
            return False

    # Torch and ONNX Runtime wheels carry their own CUDA runtime. Do not use the
    # machine's stale CUDA_PATH: install one verified CUDA 12.8 stack for modern
    # NVIDIA cards, with CPU wheels as the universal fallback.
    torch_needs_install = facet in ("clip", "caption", "joycaption") and (
        not installed("torch") or (gpu and not cuda_torch_installed())
    )
    if torch_needs_install:
        torch_index = ("https://download.pytorch.org/whl/cu128" if gpu else
                       "https://download.pytorch.org/whl/cpu")
        # Install the small Python dependencies separately. Reinstalling the
        # whole dependency tree can try to delete PIL/numpy DLLs while another
        # process has them loaded on Windows.
        commands.append(pip + [
            "filelock", "typing-extensions>=4.10", "sympy>=1.13", "networkx",
            "jinja2", "fsspec",
        ])
        commands.append(pip + [
            "--upgrade", "--ignore-installed", "--force-reinstall", "--no-deps",
            "torch==2.11.0", "torchvision==0.26.0", "--index-url", torch_index
        ])

    specs = {
        "ocr": [] if installed("rapidocr_onnxruntime") else ["rapidocr-onnxruntime==1.4.4"],
        "wd14": [],
        "clip": ([] if installed("open_clip") else ["open_clip_torch==3.3.0"])
                + ([] if installed("sqlite_vec") else ["sqlite-vec==0.1.9"]),
        "faces": [] if installed("insightface") else ["insightface==1.0.1"],
        "caption": (([] if installed("transformers") else ["transformers==5.15.0"])
                    + ([] if installed("accelerate") else ["accelerate==1.14.0"])),
        "joycaption": (([] if installed("transformers") else ["transformers==5.15.0"])
                       + ([] if installed("accelerate") else ["accelerate==1.14.0"])
                       + ([] if installed("bitsandbytes") else ["bitsandbytes==0.49.2"])),
        "sklearn": [] if installed("sklearn") else ["scikit-learn==1.7.2"],
    }[facet]
    if specs:
        commands.append(pip + specs)

    if facet in ("ocr", "wd14", "faces") and not installed("onnxruntime"):
        # rapidocr/insightface may pull the CPU distribution, but all of these
        # wheels expose the same module files. Installing the target wheel last
        # with --ignore-installed overwrites those files without invoking pip
        # uninstall (which can hang inside an embedded runtime).
        if gpu:
            commands.append(pip + ["--ignore-installed", "--force-reinstall",
                                    "onnxruntime-gpu[cuda,cudnn]==1.23.2"])
        elif facet != "ocr" and npu:
            # RapidOCR's wrapper (models/ocr.py) has no raw onnxruntime
            # provider passthrough, so only WD14/Faces benefit from OpenVINO's
            # NPU device (engine.onnx_provider_options). Windows also needs the
            # standalone `openvino` package alongside the EP wheel.
            commands.append(pip + ["--ignore-installed", "--force-reinstall",
                                    "onnxruntime-openvino==1.23.0", "openvino"])
        elif directml_gpu:
            # Vendor-agnostic GPU fallback: AMD, Intel Arc/integrated, or any
            # other DX12 adapter (§5.2 DirectML compatibility fallback).
            commands.append(pip + ["--ignore-installed", "--force-reinstall",
                                    "onnxruntime-directml==1.21.0"])
        else:
            commands.append(pip + ["onnxruntime==1.23.2"])

    log_dir = config.APP_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "dependency-install.log"
    returncode = 0
    pip_env = os.environ.copy()
    existing_pythonpath = pip_env.get("PYTHONPATH", "")
    pip_env["PYTHONPATH"] = str(config.RUNTIME_PACKAGES_DIR) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    # "Download all" can request several facets together. pip writes into one
    # shared runtime and is not safe to run concurrently, while model-weight
    # downloads are independent and remain parallel.
    with _dependency_lock:
        # A pipe-backed capture can stall pip's rich/progress output when this
        # runs from a daemon thread. Stream to a durable log instead; it also
        # gives the Models screen a concrete diagnostic path after a failure.
        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] install {facet}\n")
            log.flush()
            for command in commands:
                log.write("command: " + " ".join(command[2:]) + "\n")
                log.flush()
                proc = subprocess.run(command, text=True, stdin=subprocess.DEVNULL,
                                      env=pip_env,
                                      stdout=log,
                                      stderr=subprocess.STDOUT)
                returncode = proc.returncode
                if returncode:
                    break

            # Validate in a clean interpreter before telling the UI that the
            # dependency is ready. This catches missing DLLs, partial target
            # installs, and a CPU wheel accidentally replacing the CUDA wheel.
            if returncode == 0:
                required = {
                    "ocr": ["rapidocr_onnxruntime", "onnxruntime"],
                    "wd14": ["onnxruntime"],
                    "clip": ["torch", "open_clip", "sqlite_vec"],
                    "faces": ["insightface", "onnxruntime"],
                    "caption": ["torch", "transformers", "accelerate"],
                    "joycaption": ["torch", "transformers", "accelerate", "bitsandbytes"],
                    "sklearn": ["sklearn"],
                }[facet]
                checks = [f"import {module}" for module in required]
                if gpu and "torch" in required:
                    checks.append("assert torch.cuda.is_available(), 'CUDA is unavailable in torch'")
                if gpu and "onnxruntime" in required:
                    checks.append(
                        "assert 'CUDAExecutionProvider' in onnxruntime.get_available_providers(), "
                        "'CUDAExecutionProvider is unavailable in onnxruntime'"
                    )
                validation = subprocess.run(
                    [sys.executable, "-c", ";".join(checks)],
                    text=True,
                    stdin=subprocess.DEVNULL,
                    env=pip_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                returncode = validation.returncode
                if returncode:
                    log.write("validation failed; dependency was not marked ready\n")
                    log.flush()
    try:
        import importlib
        import site
        site.addsitedir(str(config.RUNTIME_PACKAGES_DIR))
        importlib.invalidate_caches()
        tail = "\n".join(log_path.read_text(
            encoding="utf-8", errors="replace").splitlines()[-30:])
    except Exception:
        tail = f"See {log_path}"
    _download_finish(key, returncode == 0,
                     runtime="cuda" if gpu else "cpu",
                     log=str(log_path),
                     message=("Dependency installed" if returncode == 0
                              else "Dependency installation failed"),
                     error=None if returncode == 0 else tail)


def _download_worker(model: str, variant_id: str | None = None) -> None:
    import os
    con = db.connect(check_same_thread=False)
    from . import engine
    # A caller can name a specific variant to fetch its weights without
    # applying it (§12 Models UX: Download and Apply are separate actions —
    # "just looking" at a bigger/uncensored model must never silently make it
    # the active one). Falls back to whatever's already applied when omitted,
    # matching the old always-download-the-applied-variant behavior.
    applied = engine.selected_variant(con, model)
    variant = engine.variant_by_id(model, variant_id) if variant_id else applied
    if variant_id and variant is None:
        _download_finish(model, False, error=f"unknown {model} variant '{variant_id}'",
                         message="Model download failed")
        return
    if model == "caption" and variant and variant.get("load_in_4bit"):
        import importlib.util
        if importlib.util.find_spec("bitsandbytes") is None:
            _dependency_install_worker("joycaption")
            dep = _download_get("dep:joycaption") or {}
            if dep.get("state") != "done":
                _download_finish(
                    model, False,
                    error=dep.get("error") or "JoyCaption 4-bit runtime installation failed",
                    message="Model dependency installation failed",
                )
                return
    dest = engine.model_dir_for_variant(con, model, variant)
    os.makedirs(dest, exist_ok=True)
    ok, err = True, None
    is_applied = variant is not None and applied is not None and variant.get("id") == applied.get("id")
    _download_progress(model, state="running", phase="preparing",
                       message=f"Preparing {model} in {dest}", dir=str(dest),
                       indeterminate=True, pct=None)
    try:
        if model in ("clip", "insightface", "caption"):
            # These fetch their own weights on engine init — no byte-level hook,
            # so report an indeterminate "working" state, then done.
            _download_progress(model, state="running", phase="downloading",
                               message="Downloading weights and validating the model",
                               indeterminate=True, pct=None, dir=str(dest))
            ok = _warm_engine(con, model, dest, variant=variant)
            if not ok:
                err = "Model initialization returned no usable engine"
        else:
            from . import engine
            from .models.download import download_repo, MODELS

            def on_prog(fname, done, total):
                pct = int(done * 100 / total) if total else None
                _download_progress(model, state="running", phase="downloading",
                                   message=f"Downloading {fname}", file=fname,
                                   done=done, total=total, pct=pct,
                                   indeterminate=total == 0, dir=str(dest))

            if variant and variant.get("repo"):
                # variant-selected direct download (e.g. WD14 tagger family)
                files = MODELS.get(model, (None, ["selected_tags.csv", "model.onnx"]))[1]
                download_repo(variant["repo"], files, dest, on_progress=on_prog)
            elif model in MODELS:
                from .models.download import download
                download(model, dest, on_progress=on_prog)
            else:
                ok, err = False, f"unknown model '{model}'"
        if ok:
            if model in ("clip", "insightface", "caption"):
                marker = engine.model_ready_marker(con, model, variant)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("ready\n", encoding="utf-8")
        if ok and is_applied:
            # The ready marker and caption auto-enable/backfill below are
            # "this is now usable" side effects — only correct when the
            # variant just fetched is actually the applied one. Downloading a
            # variant the user is merely evaluating must not flip readiness
            # state for the applied variant, and must not touch enablement.
            if model == "caption":
                # Downloading a caption model with no prior toggle is an
                # unambiguous "I want descriptions" signal — auto-enable the
                # facet (wd14/clip/faces stay opt-in; this one has no reason
                # to make the user find a second checkbox) and backfill every
                # file that doesn't have one yet, so existing libraries don't
                # need a manual "reindex all" just to see the feature work.
                _set_facet_enabled(con, "caption", True)
                db.enqueue_missing_captions(con)
    except Exception as e:
        ok, err = False, repr(e)
    _download_finish(model, ok, error=err, dir=str(dest),
                     message="Model ready" if ok else "Model download failed")


def _warm_engine(con, model: str, dest, variant: dict | None = None) -> bool:
    from . import config, engine as _engine
    v = variant if variant is not None else (_engine.selected_variant(con, model) or {})
    runtime = _engine.get_engine_config(con)
    device, providers = runtime["torch_device"], runtime["onnx_providers"]
    if model == "clip":
        from .models import clip
        eng = clip.get_engine(str(dest),
                              model_name=v.get("model", config.CLIP_MODEL),
                              pretrained=v.get("pretrained", config.CLIP_PRETRAINED),
                              device=device)
        if isinstance(eng, clip.NullClipEngine):
            raise RuntimeError(clip.last_error() or "CLIP engine unavailable")
        return True
    if model == "insightface":
        from .models import faces
        eng = faces.get_engine(str(dest), pack=v.get("pack", "buffalo_l"),
                               providers=providers)
        if isinstance(eng, faces.NullFaceEngine):
            raise RuntimeError(faces.last_error() or "InsightFace engine unavailable")
        return True
    if model == "caption":
        from .models import caption
        mid = v.get("model_id") or db.get_setting(con, "caption_model") or config.CAPTION_MODEL
        _predownload_hf_model(model, mid, dest)
        eng = caption.get_engine(str(dest), model_id=mid, device=device,
                                 name=v.get("engine", "blip"),
                                 load_in_4bit=v.get("load_in_4bit", False))
        if isinstance(eng, caption.NullCaptionEngine):
            raise RuntimeError(caption.last_error() or "Caption engine unavailable")
        return True
    return False


def _predownload_hf_model(model: str, model_id: str, dest) -> None:
    """Pre-fetch a Hugging Face model's weights with real byte-level progress,
    so the subsequent from_pretrained() call inside the engine (which has no
    progress hook of its own) hits a warm local cache and loads instantly
    instead of leaving the UI stuck on an indeterminate spinner for however
    long a multi-GB download takes (JoyCaption is ~15.6GB at full precision).
    """
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import tqdm as hf_tqdm

    lock = threading.Lock()
    bars: dict[int, tuple[int, int]] = {}

    def report() -> None:
        with lock:
            done = sum(n for n, _ in bars.values())
            total = sum(t for _, t in bars.values())
        pct = int(done * 100 / total) if total else None
        _download_progress(model, state="running", phase="downloading",
                           message="Downloading weights and validating the model",
                           pct=pct, done=done, total=total,
                           indeterminate=total == 0, dir=str(dest))

    class _ProgressTqdm(hf_tqdm):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            if self.unit == "B" and self.total:
                with lock:
                    bars[id(self)] = (self.n or 0, self.total)
                report()

        def update(self, n=1):
            result = super().update(n)
            if self.unit == "B" and self.total:
                with lock:
                    bars[id(self)] = (self.n, self.total)
                report()
            return result

        def close(self):
            with lock:
                bars.pop(id(self), None)
            super().close()
            report()

    try:
        snapshot_download(repo_id=model_id, cache_dir=str(dest), tqdm_class=_ProgressTqdm)
    except Exception as exc:
        # Best-effort: fall back to from_pretrained()'s own (progress-less)
        # download rather than failing the whole load over this optimization.
        print(f"pre-download progress hook failed for {model_id}: {exc!r} "
              f"-- falling back to indeterminate progress", file=sys.stderr)


def _progress_change_key(p: dict) -> dict:
    """Coarsened copy of a progress payload for change-detection only.

    rss_mb drifts by a couple MB from ordinary allocator/GC noise even while
    the daemon is fully idle; comparing it exactly would defeat the "only
    emit when something changed" guard and re-fire every poll forever.
    Bucket it to the nearest 10MB so a real load/unload still shows up.
    """
    d = dict(p)
    if d.get("rss_mb") is not None:
        d["rss_mb"] = round(d["rss_mb"] / 10) * 10
    return d


def _worker_thread(stop: threading.Event) -> None:
    """--auto mode: continuously drain the queue + push progress events."""
    con = db.connect(check_same_thread=False)
    last = None
    while not stop.is_set():
        # Written every iteration before the (possibly slow) drain() call
        # below, and again from inside worker.py at each facet boundary
        # within a single file's processing — so a file that legitimately
        # takes a while (5 sequential facets, a cold model load) doesn't look
        # "stuck" to the Electron-side watchdog. Only a call that never
        # returns at all (e.g. a corrupt image wedging a native decode)
        # leaves the clock stale long enough to trip it.
        heartbeat.beat()
        if _paused.is_set():
            # Paused: leave the queue untouched but still surface state once so
            # the UI can show the paused progress bar (§12).
            status.set_idle()
            p = {**db.progress(con), "paused": True,
                 "mode": db.get_setting(con, "index_mode", "auto")}
            if _progress_change_key(p) != _progress_change_key(last or {}):
                _emit({"event": "progress", **p})
                last = p
            stop.wait(0.5)
            continue
        # Process one job at a time so progress is emitted while the queue is
        # running, rather than only after the entire queue has drained.
        n = drain(con, max_jobs=1)
        if not n:
            status.set_idle()
        p = {**db.progress(con), "paused": False,
             "mode": db.get_setting(con, "index_mode", "auto")}
        if _progress_change_key(p) != _progress_change_key(last or {}):
            _emit({"event": "progress", **p})
            last = p
        # Avoid adding a one-second delay to every image; back off only when
        # there is no work waiting.
        stop.wait(0.01 if n else 0.5)


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    auto = "--auto" in argv
    # NumPy/OpenBLAS can deadlock in Windows' DLL loader when its first import
    # happens in the model/indexing background thread while the main thread is
    # blocked reading the RPC pipe. Load the core numeric runtime once on the
    # main thread before creating any workers. Optional model dependencies are
    # installed from the Models screen, so a fresh installation must still be
    # allowed to reach the ready state when NumPy is not present yet.
    try:
        import numpy  # noqa: F401
    except ModuleNotFoundError:
        pass
    con = db.connect()
    recovered = db.recover_interrupted_jobs(con)
    # Electron may open the same library immediately on first launch. Signal
    # as soon as schema creation/migrations are committed; full model preload
    # can continue before the later general-purpose `ready` event.
    _emit({"event": "db_ready"})

    # Facets already enabled from a previous run must be safe before the auto
    # worker sees its first queued image. Learned tags may use Scipy/sklearn
    # even though they are not represented by an enable/disable facet.
    for facet in ("ocr", "wd14", "clip", "faces", "caption"):
        if config.facet_enabled(con, facet):
            try:
                _preload_model_runtime(facet)
            except Exception as exc:
                _emit({"event": "warning", "message":
                       f"could not preload {facet} runtime: {exc!r}"})
    try:
        import importlib.util
        if importlib.util.find_spec("sklearn") is not None:
            _preload_model_runtime("sklearn")
    except Exception as exc:
        _emit({"event": "warning", "message":
               f"could not preload learned-tag runtime: {exc!r}"})

    stop = threading.Event()
    worker = None
    observer = None
    if auto:
        worker = threading.Thread(target=_worker_thread, args=(stop,), daemon=True)
        worker.start()
        if db.get_setting(con, "index_mode", "auto") != "manual":
            try:
                from .watcher import start_watchers
                observer = start_watchers(db.connect(check_same_thread=False))
                _state["observer"] = observer
            except Exception as e:
                _emit({"event": "warning", "message": f"watcher not started: {e!r}"})

    _emit({"event": "ready", "auto": auto})
    if recovered:
        _emit({"event": "warning", "message":
               f"recovered {recovered} interrupted indexing job(s)"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _emit({"ok": False, "error": "invalid json"})
            continue
        resp = handle(con, msg)
        _emit(resp)
        if msg.get("cmd") == "stop":
            break

    stop.set()
    obs = _state.get("observer") or observer
    if obs is not None:
        obs.stop()


if __name__ == "__main__":
    main()
