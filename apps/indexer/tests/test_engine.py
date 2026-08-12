"""M9 engine/tier + ops test — spec §5.2 / §7 / §9, verification discipline §16.

Verifies the parts of M9 that live in the backend (the Electron packaging/UI is
out of scope for a headless test): tier recommendation from measured VRAM,
execution-provider resolution order, tier override via settings, errored-job
retry, and bulk manual tagging.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_engine
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="imgtag_eng_"))
    os.environ["IMAGE_TAGGER_HOME"] = str(tmp / "home")

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    from indexer import engine

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # --- tier recommendation (VRAM bucket decides, §5.2) -------------------
    check(engine.recommend_tier(0, False) == "low", "CPU-only -> low tier")
    check(engine.recommend_tier(4, True) == "low", "4GB GPU -> low")
    check(engine.recommend_tier(8, True) == "mid", "8GB -> mid")
    check(engine.recommend_tier(12, True) == "mid", "12GB -> mid")
    check(engine.recommend_tier(24, True) == "high", "24GB -> high")
    check(engine.recommend_tier(15.9, True) == "high",
          "marketed 16GB GPU (15.9 GiB reported) -> high")

    # --- provider resolution order (CUDA -> DirectML -> CPU) ---------------
    order = engine.resolve_onnx_providers(
        ["CUDAExecutionProvider", "CPUExecutionProvider"])
    check(order[0] == "CUDAExecutionProvider" and order[-1] == "CPUExecutionProvider",
          "CUDA preferred, CPU last")
    cpu_only = engine.resolve_onnx_providers(["CPUExecutionProvider"])
    check(cpu_only == ["CPUExecutionProvider"], "CPU-only box resolves to CPU")
    dml = engine.resolve_onnx_providers(
        ["DmlExecutionProvider", "CPUExecutionProvider"])
    check(dml[0] == "DmlExecutionProvider", "DirectML preferred when no CUDA")
    # CPU is always appended as universal fallback even if omitted
    check("CPUExecutionProvider" in engine.resolve_onnx_providers(["DmlExecutionProvider"]),
          "CPU fallback always present")

    # --- real detection on THIS machine doesn't crash ----------------------
    cfg = engine.get_engine_config()
    check(cfg["tier"] in engine.PRESETS and cfg["onnx_providers"],
          f"live detect -> tier={cfg['tier']} providers={cfg['onnx_providers']}")

    # --- tier override via settings ---------------------------------------
    con = db.connect()
    db.set_setting(con, "tier", "high")
    cfg2 = engine.get_engine_config(con)
    check(cfg2["tier"] == "high" and cfg2["tier_source"] == "override",
          "settings tier override wins over auto-detect")

    # --- Models-screen facet toggles are live DB settings -----------------
    check(config.facet_enabled(con, "clip") is False,
          "heavy facet defaults off")
    db.set_setting(con, "clip_enabled", "1")
    check(config.facet_enabled(con, "clip") is True,
          "Models toggle enables facet without daemon restart")
    db.set_setting(con, "clip_enabled", "0")
    check(config.facet_enabled(con, "clip") is False,
          "Models toggle disables facet without daemon restart")

    # --- Model variant persistence ----------------------------------------
    from indexer import daemon
    choices = {"wd14": "eva02-large-v3", "clip": "vith14",
               "insightface": "buffalo_l", "caption": "blip-large"}
    for facet, variant in choices.items():
        check(daemon._set_variant(con, facet, variant)["ok"],
              f"save {facet} variant")
    view = {row["facet"]: row["selected"] for row in engine.variants_view(con)}
    check(all(view[key] == value for key, value in choices.items()),
          "best/accurate model choices survive a fresh status read")
    joy4 = engine.variant_by_id("caption", "joycaption-4bit")
    joy_full = engine.variant_by_id("caption", "joycaption")
    check(bool(joy4 and joy4["engine"] == "joycaption" and joy4["load_in_4bit"]),
          "JoyCaption 4-bit variant is present and quantized")
    check(bool(joy_full and joy_full["engine"] == "joycaption"
               and not joy_full["load_in_4bit"]),
          "JoyCaption full-precision variant is present")
    check(engine.recommended_variant_id("caption", "mid") == "blip-large",
          "JoyCaption remains explicit opt-in, never auto-recommended")

    # Downloading a pending variant must write into that variant's directory,
    # not the currently applied model's folder. Otherwise Apply appears to lose
    # a successful multi-GB download.
    original_warm = daemon._warm_engine
    original_dependency_install = daemon._dependency_install_worker
    daemon._warm_engine = lambda *_args, **_kwargs: True
    daemon._dependency_install_worker = lambda _facet: daemon._download_update(
        "dep:joycaption", state="done", ok=True)
    try:
        daemon._download_worker("caption", "joycaption-4bit")
    finally:
        daemon._warm_engine = original_warm
        daemon._dependency_install_worker = original_dependency_install
    check(engine.model_ready_marker(con, "caption", joy4).exists(),
          "pending variant download writes its own ready marker")
    check(not engine.model_ready_marker(con, "caption").exists(),
          "pending download does not mark applied variant ready")
    daemon._download_update("unit-model", state="running", pct=42,
                            indeterminate=False, message="testing")
    status = daemon.handle(con, {"id": 1, "cmd": "download_status"})
    saved_download = next((row for row in status["result"]["downloads"]
                           if row["model"] == "unit-model"), None)
    check(status["ok"] and saved_download is not None and saved_download["pct"] == 42,
          "download status can be restored after reopening Models")

    # --- errored-job retry (§7) -------------------------------------------
    con.execute("INSERT INTO files (path, sha256, index_status) "
                "VALUES ('x.png','sha','error')")
    fid = con.execute("SELECT id FROM files WHERE path='x.png'").fetchone()["id"]
    db.enqueue_job(con, fid, "ingest")
    db.set_job_state(con, con.execute(
        "SELECT id FROM jobs WHERE file_id=?", (fid,)).fetchone()["id"],
        "error", "boom")
    n = db.retry_errors(con)
    check(n == 1, "retry re-queues the errored job")
    check(con.execute("SELECT index_status FROM files WHERE id=?", (fid,)
                      ).fetchone()["index_status"] == "pending",
          "retry resets file status to pending")

    # Empty cloud placeholders and corrupt sources are not model failures.
    # Standalone CLIP/caption backfills must complete rather than creating a
    # permanent error/retry loop before either heavyweight model is loaded.
    from indexer import worker
    for filename, payload in (("empty.jpg", b""), ("corrupt.jpg", b"not an image")):
        bad = tmp / filename
        bad.write_bytes(payload)
        con.execute("INSERT INTO files (path, sha256, index_status) VALUES (?, ?, 'done')",
                    (str(bad), filename))
        bad_id = con.execute("SELECT id FROM files WHERE path=?", (str(bad),)).fetchone()["id"]
        for kind in ("clip", "caption"):
            jid = db.enqueue_job(con, bad_id, kind)
            job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            worker.run_job(con, job)
            state = con.execute("SELECT state, error FROM jobs WHERE id=?", (jid,)).fetchone()
            check(state["state"] == "done" and not state["error"],
                  f"{kind} skips {filename} without retryable error")

    # Auto facets may agree on a tag name, but must not rewrite provenance from
    # another source (especially a user's manual tag).
    db.add_manual_tag(con, fid, "sitting", "pose")
    db.write_auto_tags(con, fid, "clip", [SimpleNamespace(
        category="pose", name="sitting", confidence=0.91)])
    source = con.execute(
        """SELECT ft.source FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
             JOIN categories c ON c.id=t.category_id
            WHERE ft.file_id=? AND c.name='pose' AND t.name='sitting'""",
        (fid,),
    ).fetchone()["source"]
    check(source == "manual", "auto tags never overwrite manual provenance")

    # --- bulk manual tagging (§9) -----------------------------------------
    # Per-file lifecycle: pending until the final live job completes.
    con.execute("INSERT INTO files (path, sha256, index_status) "
                "VALUES ('lifecycle.png','life','done')")
    life_id = con.execute(
        "SELECT id FROM files WHERE path='lifecycle.png'").fetchone()["id"]
    ingest_job = db.enqueue_job(con, life_id, "ingest")
    duplicate = db.enqueue_job(con, life_id, "ingest")
    infer_job = db.enqueue_job(con, life_id, "infer")
    check(duplicate == ingest_job, "duplicate live job is de-duplicated")
    check(con.execute("SELECT index_status FROM files WHERE id=?", (life_id,)
                      ).fetchone()["index_status"] == "pending",
          "queueing marks file pending")
    db.set_job_state(con, ingest_job, "done")
    check(con.execute("SELECT index_status FROM files WHERE id=?", (life_id,)
                      ).fetchone()["index_status"] == "pending",
          "file stays pending while another job is active")
    db.set_job_state(con, infer_job, "done")
    check(con.execute("SELECT index_status FROM files WHERE id=?", (life_id,)
                      ).fetchone()["index_status"] == "done",
          "file becomes done after its final job")
    interrupted = db.enqueue_job(con, life_id, "reindex")
    db.set_job_state(con, interrupted, "running")
    check(db.recover_interrupted_jobs(con) == 1,
          "daemon startup recovers interrupted jobs")
    check(con.execute("SELECT state FROM jobs WHERE id=?", (interrupted,)
                      ).fetchone()["state"] == "queued",
          "interrupted job returns to queue")
    db.set_job_state(con, interrupted, "done")

    # User-triggered reindex/rescan must jump ahead of a long background queue.
    con.execute("INSERT INTO files (path, sha256) VALUES ('background.png','bg')")
    bg_id = con.execute(
        "SELECT id FROM files WHERE path='background.png'").fetchone()["id"]
    con.execute("INSERT INTO files (path, sha256) VALUES ('urgent.png','urgent')")
    urgent_id = con.execute(
        "SELECT id FROM files WHERE path='urgent.png'").fetchone()["id"]
    db.enqueue_job(con, bg_id, "infer", priority=0)
    urgent_job = db.enqueue_job(con, urgent_id, "reindex", priority=100)
    check(db.next_job(con)["id"] == urgent_job,
          "interactive reindex jumps ahead of background queue")

    ids = []
    for i in range(3):
        con.execute("INSERT INTO files (path, sha256) VALUES (?, 'h')",
                    (f"bulk{i}.png",))
        ids.append(con.execute("SELECT id FROM files WHERE path=?",
                               (f"bulk{i}.png",)).fetchone()["id"])
        db.refresh_fts(con, ids[-1])
    db.bulk_add_manual_tag(con, ids, "collection", "summer trip")
    hit = [r["rowid"] for r in con.execute(
        "SELECT rowid FROM files_fts WHERE files_fts MATCH ?", ("summer",))]
    check(set(ids).issubset(set(hit)), "bulk add tags all files (FTS searchable)")
    db.bulk_remove_tag(con, ids[:2], "collection", "summer trip")
    hit2 = [r["rowid"] for r in con.execute(
        "SELECT rowid FROM files_fts WHERE files_fts MATCH ?", ("summer",))]
    check(ids[2] in hit2 and ids[0] not in hit2, "bulk remove affects only chosen files")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — M9 engine/tier + retry + bulk ops verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
