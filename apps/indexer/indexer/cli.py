"""Indexer control CLI — spec §7/§9/§12.

The Electron app drives indexing by shelling out to these subcommands (or by
importing the same functions). Also the manual test surface for M2.

    python -m indexer.cli add-root D:\\Pictures
    python -m indexer.cli add-exclude "**/WIP/**"
    python -m indexer.cli rescan
    python -m indexer.cli work            # drain the job queue once
    python -m indexer.cli watch           # auto mode: watch + drain until Ctrl-C
    python -m indexer.cli tag add <file_id> character "hatsune miku"
    python -m indexer.cli tag rm  <file_id> character "hatsune miku"
    python -m indexer.cli category add cosplay --color "#e91e63"
    python -m indexer.cli status
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import db
from .scan import rescan
from .worker import drain


def _cmd_add_root(con, args):
    db.add_root(con, args.path, mode=args.mode, recursive=not args.no_recursive)
    print(f"root {args.mode}: {args.path}")


def _cmd_add_exclude(con, args):
    db.add_exclude_pattern(con, args.pattern)
    print(f"exclude pattern: {args.pattern}")


def _cmd_rescan(con, args):
    print("scanning include roots ...")
    res = rescan(con)
    print(f"scan: {res}")
    if not args.no_work:
        n = drain(con)
        print(f"worker: processed {n} jobs")
    print("status:", db.progress(con))


def _cmd_work(con, args):
    n = drain(con, max_jobs=args.max)
    print(f"processed {n} jobs")
    print("status:", db.progress(con))


def _cmd_watch(con, args):
    # Separate connections per thread (watcher enqueues, worker drains).
    from .watcher import start_watchers
    work_con = db.connect(check_same_thread=False)
    watch_con = db.connect(check_same_thread=False)
    print("initial rescan ...")
    print("scan:", rescan(watch_con))
    obs = start_watchers(watch_con)
    print("watching. Ctrl-C to stop.")
    try:
        while True:
            n = drain(work_con)
            if n:
                print(f"  indexed {n} file(s) | {db.progress(work_con)}")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping ...")
    finally:
        obs.stop()
        obs.join()


def _cmd_tag(con, args):
    fid = int(args.file_id)
    if args.op == "add":
        db.add_manual_tag(con, fid, args.name, args.category)
        print(f"+ {args.category}:{args.name} on file {fid}")
    else:
        db.remove_tag(con, fid, args.name, args.category)
        print(f"- {args.category}:{args.name} on file {fid}")


def _cmd_category(con, args):
    cid = db.get_or_create_category(con, args.name, color=args.color)
    con.commit()
    print(f"category '{args.name}' (id={cid})")


def _cmd_ocr(con, args):
    """Run OCR on one file (or all files) on demand and store results (§10)."""
    from .worker import _run_infer
    if args.file_id == "all":
        ids = [r["id"] for r in con.execute("SELECT id FROM files").fetchall()]
    else:
        ids = [int(args.file_id)]
    for fid in ids:
        _run_infer(con, fid)
        row = con.execute(
            "SELECT ocr_text FROM files WHERE id=?", (fid,)
        ).fetchone()
        txt = (row["ocr_text"] or "").replace("\n", " / ") if row else ""
        print(f"file {fid}: {txt[:120] or '(no text found)'}")


def _cmd_wd14(con, args):
    """Run the WD14 anime tagger on one file (or all) on demand (§5)."""
    from . import config
    from .models import wd14
    from . import engine as engine_config
    mdir = engine_config.active_model_dir(con, "wd14")
    providers = engine_config.get_engine_config(con)["onnx_providers"]
    tagger = wd14.get_engine(
        mdir,
        general_threshold=config.WD14_GENERAL_THRESHOLD,
        character_threshold=config.WD14_CHARACTER_THRESHOLD,
        providers=providers,
    )
    if isinstance(tagger, wd14.NullTaggerEngine):
        print(f"WD14 model not found in {mdir} "
              f"(need model.onnx + selected_tags.csv). Download with:\n"
              f"  python -m indexer.cli download wd14")
        return
    if args.file_id == "all":
        rows = con.execute("SELECT id, path FROM files").fetchall()
    else:
        rows = con.execute(
            "SELECT id, path FROM files WHERE id=?", (int(args.file_id),)
        ).fetchall()
    for r in rows:
        tags = tagger.tag(r["path"])
        db.write_auto_tags(con, r["id"], "wd14", tags)
        db.set_image_kind(con, r["id"], tagger.image_kind(tags))
        top = ", ".join(f"{t.name}({t.confidence:.2f})"
                        for t in sorted(tags, key=lambda x: -x.confidence)[:6])
        print(f"file {r['id']} [{tagger.image_kind(tags)}]: {top or '(no tags)'}")


def _cmd_clip(con, args):
    """Run CLIP zero-shot + store embeddings for one file (or all) (§5)."""
    from . import config, vec
    from .models import clip
    from . import engine as engine_config
    engine = clip.get_engine(str(engine_config.active_model_dir(con, "clip")),
                             model_name=config.CLIP_MODEL,
                             pretrained=config.CLIP_PRETRAINED)
    if isinstance(engine, clip.NullClipEngine):
        print("CLIP backend unavailable (need open_clip + torch). "
              "pip install open_clip_torch torch")
        return
    vocab = db.get_clip_vocab(con)
    rows = (con.execute("SELECT id, path FROM files").fetchall()
            if args.file_id == "all"
            else con.execute("SELECT id, path FROM files WHERE id=?",
                             (int(args.file_id),)).fetchall())
    for r in rows:
        emb = engine.encode_image(r["path"])
        if emb is None:
            continue
        vec.upsert(con, r["id"], emb, dim=len(emb))
        zs = engine.classify(emb, vocab, threshold=config.CLIP_THRESHOLD)
        db.write_auto_tags(con, r["id"], "clip", zs)
        top = ", ".join(f"{z.category}:{z.name}({z.confidence:.2f})" for z in zs)
        print(f"file {r['id']}: {top or '(no labels above threshold)'}")


def _cmd_semantic(con, args):
    """Semantic search: text -> CLIP text embedding -> sqlite-vec KNN (§8)."""
    from . import config, vec
    from .models import clip
    if not vec.load(con):
        print("semantic search unavailable (sqlite-vec not installed).")
        return
    from . import engine as engine_config
    engine = clip.get_engine(str(engine_config.active_model_dir(con, "clip")),
                             model_name=config.CLIP_MODEL,
                             pretrained=config.CLIP_PRETRAINED)
    if isinstance(engine, clip.NullClipEngine):
        print("CLIP backend unavailable (need open_clip + torch).")
        return
    tvec = engine.encode_texts([args.query])[0]
    hits = vec.knn(con, tvec, k=args.k)
    for fid, dist in hits:
        row = con.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
        if row:
            print(f"  {dist:.3f}  {row['path']}")
    if not hits:
        print("  (no results — has CLIP indexed any files yet?)")


def _cmd_clip_vocab(con, args):
    if args.action == "list":
        for cat, labels in db.get_clip_vocab(con).items():
            print(f"{cat}: {', '.join(labels)}")
    elif args.action == "add":
        db.add_clip_label(con, args.category, args.label)
        print(f"+ clip label {args.category}:{args.label}")
    elif args.action == "rm":
        db.remove_clip_label(con, args.category, args.label)
        print(f"- clip label {args.category}:{args.label}")


def _cmd_faces(con, args):
    """Detect + cluster real faces for one file (or all) (§5)."""
    from . import config
    from .models import faces
    from . import engine as engine_config
    fe = faces.get_engine(str(engine_config.active_model_dir(con, "insightface")))
    if isinstance(fe, faces.NullFaceEngine):
        print("InsightFace backend unavailable (need `insightface` + buffalo_l).")
        return
    rows = (con.execute("SELECT id, path, image_kind FROM files").fetchall()
            if args.file_id == "all"
            else con.execute("SELECT id, path, image_kind FROM files WHERE id=?",
                             (int(args.file_id),)).fetchall())
    for r in rows:
        if r["image_kind"] == "anime":
            continue
        pids = db.write_faces(con, r["id"], fe.detect(r["path"]),
                              threshold=config.FACE_THRESHOLD)
        print(f"file {r['id']}: {len(pids)} face(s) -> persons {pids}")


def _cmd_persons(con, args):
    """List person clusters (id, name, face count)."""
    rows = con.execute(
        "SELECT p.id, p.name, count(f.id) n FROM persons p "
        "LEFT JOIN faces f ON f.person_id=p.id GROUP BY p.id ORDER BY n DESC"
    ).fetchall()
    for r in rows:
        print(f"  person {r['id']}: {r['name'] or '(unnamed)':20s}  {r['n']} face(s)")
    if not rows:
        print("  (no faces detected yet)")


def _cmd_name_person(con, args):
    db.name_person(con, int(args.person_id), args.name)
    print(f"person {args.person_id} named '{args.name}'")


def _cmd_merge_persons(con, args):
    db.merge_persons(con, int(args.src), int(args.dst))
    print(f"merged person {args.src} -> {args.dst}")


def _cmd_learn(con, args):
    """Build/refresh a few-shot learned tag from its manual examples (§5.3)."""
    from . import learned
    s = learned.build(con, args.category, args.name, space=args.space)
    if s is None:
        n = con.execute(
            """SELECT count(*) c FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
               JOIN categories c2 ON c2.id=t.category_id
               WHERE t.name=? AND c2.name=? AND ft.source IN ('manual','path')""",
            (args.name, args.category)).fetchone()["c"]
        print(f"not enough examples to learn '{args.category}:{args.name}' "
              f"({n} found). Tag a few images by hand first.")
        return
    print(f"learned '{args.category}:{args.name}' [{args.space}] "
          f"method={s['method']} threshold={s['threshold']:.3f} "
          f"pos={s['n_pos']} neg={s['n_neg']} -> applied to {s['applied']} file(s)")


def _cmd_learn_feedback(con, args):
    from . import learned
    tag_id = db.get_or_create_tag(con, args.name, args.category)
    if args.op == "confirm":
        learned.confirm(con, tag_id, int(args.file_id), args.space)
        print(f"confirmed {args.category}:{args.name} on file {args.file_id}")
    else:
        learned.reject(con, tag_id, int(args.file_id), args.space)
        print(f"rejected {args.category}:{args.name} on file {args.file_id}")


def _cmd_learned(con, args):
    rows = con.execute(
        """SELECT t.name, c.name cat, l.space, l.method, l.threshold, l.n_pos, l.n_neg
           FROM learned_tags l JOIN tags t ON t.id=l.tag_id
           JOIN categories c ON c.id=t.category_id ORDER BY c.name, t.name"""
    ).fetchall()
    for r in rows:
        applied = con.execute(
            """SELECT count(*) c FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
               JOIN categories c2 ON c2.id=t.category_id
               WHERE t.name=? AND c2.name=? AND ft.source='learned'""",
            (r["name"], r["cat"])).fetchone()["c"]
        print(f"  {r['cat']}:{r['name']} [{r['space']}/{r['method']}] "
              f"thr={r['threshold']:.3f} pos={r['n_pos']} neg={r['n_neg']} "
              f"applied={applied}")
    if not rows:
        print("  (no learned tags yet — use `learn <category> <name>`)")


def _cmd_caption(con, args):
    """Generate captions for one file (or all); re-captions overwrite (§11)."""
    from . import config
    from .models import caption
    model_id = db.get_setting(con, "caption_model") or config.CAPTION_MODEL
    from . import engine as engine_config
    engine = caption.get_engine(str(engine_config.active_model_dir(con, "caption")), model_id=model_id)
    if isinstance(engine, caption.NullCaptionEngine):
        print("caption backend unavailable (need transformers + torch). "
              "pip install transformers torch")
        return
    rows = (con.execute("SELECT id, path FROM files").fetchall()
            if args.file_id == "all"
            else con.execute("SELECT id, path FROM files WHERE id=?",
                             (int(args.file_id),)).fetchall())
    for r in rows:
        text = engine.caption(r["path"])
        if text:
            db.set_caption(con, r["id"], text)
        print(f"file {r['id']}: {text or '(no caption)'}")


def _cmd_caption_model(con, args):
    """Get or set the per-library caption model id (§11 swappable)."""
    if args.model_id:
        db.set_setting(con, "caption_model", args.model_id)
        print(f"caption model set to: {args.model_id}")
    else:
        from . import config
        print(f"caption model: {db.get_setting(con, 'caption_model') or config.CAPTION_MODEL}")


def _cmd_models_dir(con, args):
    """Get or set the folder where model files are downloaded/loaded (§12)."""
    if args.path:
        target = os.path.abspath(args.path)
        os.makedirs(target, exist_ok=True)
        db.set_setting(con, "models_dir", target)
        print(f"models dir set to: {target}")
    else:
        print(f"models dir: {db.get_models_dir(con)}")
        if os.environ.get("IMAGE_TAGGER_MODELS_DIR"):
            print("  (overridden this session by IMAGE_TAGGER_MODELS_DIR)")


def _cmd_download(con, args):
    """Download a model into the configured models folder (§12)."""
    from . import engine
    dest = engine.active_model_dir(con, args.model)
    if args.model == "clip":
        # open_clip fetches its own weights; warm the cache into the chosen dir.
        from . import config
        from .models import clip
        os.makedirs(dest, exist_ok=True)
        variant = engine.selected_variant(con, "clip") or {}
        model_name = variant.get("model", config.CLIP_MODEL)
        pretrained = variant.get("pretrained", config.CLIP_PRETRAINED)
        print(f"clip -> {dest}  (open_clip {model_name}/{pretrained})")
        eng = clip.get_engine(str(dest), model_name=model_name,
                              pretrained=pretrained,
                              device=engine.get_engine_config(con)["torch_device"])
        ok = not isinstance(eng, clip.NullClipEngine)
        if ok:
            engine.model_ready_marker(con, "clip").write_text("ready\n", encoding="utf-8")
        print("done: clip" if ok else "failed: need open_clip_torch + torch installed")
        return
    from .models.download import download, download_repo, MODELS
    if args.model == "wd14":
        variant = engine.selected_variant(con, "wd14") or {}
        repo = variant.get("repo")
        if repo:
            print(f"wd14 -> {dest}  (from {repo})")
            download_repo(repo, ["selected_tags.csv", "model.onnx"], dest)
            return
    if args.model not in MODELS:
        print(f"unknown model '{args.model}'. Known: {', '.join(MODELS)}, clip")
        return
    download(args.model, dest)


def _cmd_doctor(con, args):
    """Report hardware, recommended tier, execution providers, and per-facet
    model/dependency readiness (§5.2 / §12 model manager)."""
    from . import engine
    cfg = engine.get_engine_config(con)
    hw = cfg["hardware"]
    gpu = hw["gpu_name"] or "(none — CPU only)"
    vram = f"  {hw['vram_gb']} GB VRAM" if hw["has_gpu"] else ""
    preset = cfg["preset"]
    print("Hardware:")
    print(f"  GPU: {gpu}{vram}")
    print(f"  onnxruntime providers: {', '.join(hw['onnx_providers']) or '(none)'}")
    print(f"Tier: {cfg['tier']} ({cfg['tier_source']})  bucket={preset['vram_bucket']}"
          f"  batch={preset['wd14_batch']} precision={preset['precision']}")
    print(f"Execution order: {' -> '.join(cfg['onnx_providers'])}  | torch: {cfg['torch_device']}")
    print("Facets:")
    for facet in engine.facet_readiness(con):
        print(f"  {facet['label'][:24]:24s} dep={'y' if facet['dep_ok'] else 'n'} "
              f"model={'y' if facet['model_ok'] else 'n'} "
              f"enabled={'y' if facet['enabled'] else 'n'}  -> {facet['state']}")


def _cmd_tier(con, args):
    from . import engine
    if args.value:
        if args.value == "auto":
            con.execute("DELETE FROM settings WHERE key='tier'"); con.commit()
            print("tier override cleared (auto-detect)")
        elif args.value in engine.PRESETS:
            db.set_setting(con, "tier", args.value)
            print(f"tier set to: {args.value}")
        else:
            print(f"unknown tier '{args.value}'. Choices: {', '.join(engine.PRESETS)}, auto")
    else:
        cfg = engine.get_engine_config(con)
        print(f"tier: {cfg['tier']} ({cfg['tier_source']})")


def _cmd_retry(con, args):
    fid = int(args.file_id) if args.file_id and args.file_id != "all" else None
    n = db.retry_errors(con, fid)
    print(f"re-queued {n} errored job(s)")
    if not args.no_work:
        from .worker import drain
        print(f"worker: processed {drain(con)} jobs")


def _cmd_bulk_tag(con, args):
    ids = ([r["id"] for r in con.execute("SELECT id FROM files")]
           if args.file_ids == ["all"] else [int(x) for x in args.file_ids])
    if args.op == "add":
        n = db.bulk_add_manual_tag(con, ids, args.category, args.name)
        print(f"+ {args.category}:{args.name} on {n} file(s)")
    else:
        n = db.bulk_remove_tag(con, ids, args.category, args.name)
        print(f"- {args.category}:{args.name} on {n} file(s)")


def _cmd_status(con, args):
    p = db.progress(con)
    print(f"files: {p['files_done']}/{p['files_total']} done")
    print(f"jobs:  {p['jobs']}")
    print(f"models dir: {db.get_models_dir(con)}")
    roots = con.execute("SELECT path,mode,enabled FROM roots").fetchall()
    for r in roots:
        print(f"  root [{r['mode']}]"
              f"{'' if r['enabled'] else ' (disabled)'}: {r['path']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="indexer")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-root"); a.add_argument("path")
    a.add_argument("--mode", choices=["include", "exclude"], default="include")
    a.add_argument("--no-recursive", action="store_true")
    a.set_defaults(fn=_cmd_add_root)

    a = sub.add_parser("add-exclude"); a.add_argument("pattern")
    a.set_defaults(fn=_cmd_add_exclude)

    a = sub.add_parser("rescan"); a.add_argument("--no-work", action="store_true")
    a.set_defaults(fn=_cmd_rescan)

    a = sub.add_parser("work"); a.add_argument("--max", type=int, default=None)
    a.set_defaults(fn=_cmd_work)

    a = sub.add_parser("watch"); a.set_defaults(fn=_cmd_watch)

    a = sub.add_parser("tag")
    a.add_argument("op", choices=["add", "rm"])
    a.add_argument("file_id"); a.add_argument("category"); a.add_argument("name")
    a.set_defaults(fn=_cmd_tag)

    a = sub.add_parser("category"); a.add_argument("action", choices=["add"])
    a.add_argument("name"); a.add_argument("--color", default=None)
    a.set_defaults(fn=_cmd_category)

    a = sub.add_parser("ocr")
    a.add_argument("file_id", help="a file id, or 'all'")
    a.set_defaults(fn=_cmd_ocr)

    a = sub.add_parser("wd14")
    a.add_argument("file_id", help="a file id, or 'all'")
    a.set_defaults(fn=_cmd_wd14)

    a = sub.add_parser("clip", help="CLIP zero-shot + embeddings (§5)")
    a.add_argument("file_id", help="a file id, or 'all'")
    a.set_defaults(fn=_cmd_clip)

    a = sub.add_parser("semantic", help="semantic search over CLIP embeddings (§8)")
    a.add_argument("query")
    a.add_argument("--k", type=int, default=20)
    a.set_defaults(fn=_cmd_semantic)

    a = sub.add_parser("clip-vocab", help="manage the CLIP label vocabulary (§5)")
    a.add_argument("action", choices=["list", "add", "rm"])
    a.add_argument("category", nargs="?")
    a.add_argument("label", nargs="?")
    a.set_defaults(fn=_cmd_clip_vocab)

    a = sub.add_parser("faces", help="detect + cluster real faces (§5)")
    a.add_argument("file_id", help="a file id, or 'all'")
    a.set_defaults(fn=_cmd_faces)

    a = sub.add_parser("persons", help="list person clusters")
    a.set_defaults(fn=_cmd_persons)

    a = sub.add_parser("learn", help="build a few-shot learned tag (§5.3)")
    a.add_argument("category"); a.add_argument("name")
    a.add_argument("--space", choices=["clip", "face"], default="clip")
    a.set_defaults(fn=_cmd_learn)

    a = sub.add_parser("learn-confirm", help="confirm a learned-tag suggestion")
    a.add_argument("category"); a.add_argument("name"); a.add_argument("file_id")
    a.add_argument("--space", choices=["clip", "face"], default="clip")
    a.set_defaults(fn=_cmd_learn_feedback, op="confirm")

    a = sub.add_parser("learn-reject", help="reject a learned-tag suggestion")
    a.add_argument("category"); a.add_argument("name"); a.add_argument("file_id")
    a.add_argument("--space", choices=["clip", "face"], default="clip")
    a.set_defaults(fn=_cmd_learn_feedback, op="reject")

    a = sub.add_parser("learned", help="list learned tags + stats")
    a.set_defaults(fn=_cmd_learned)

    a = sub.add_parser("caption", help="generate natural-language captions (§11)")
    a.add_argument("file_id", help="a file id, or 'all'")
    a.set_defaults(fn=_cmd_caption)

    a = sub.add_parser("caption-model", help="get/set the per-library caption model")
    a.add_argument("model_id", nargs="?", default=None)
    a.set_defaults(fn=_cmd_caption_model)

    a = sub.add_parser("name-person", help="name a person cluster once (§5)")
    a.add_argument("person_id"); a.add_argument("name")
    a.set_defaults(fn=_cmd_name_person)

    a = sub.add_parser("merge-persons", help="merge two clusters (§15 tool)")
    a.add_argument("src"); a.add_argument("dst")
    a.set_defaults(fn=_cmd_merge_persons)

    a = sub.add_parser("models-dir", help="get or set the model download folder")
    a.add_argument("path", nargs="?", default=None,
                   help="folder to store models; omit to print the current one")
    a.set_defaults(fn=_cmd_models_dir)

    a = sub.add_parser("download", help="download a model into the models folder")
    a.add_argument("model", help="model key, e.g. wd14")
    a.set_defaults(fn=_cmd_download)

    a = sub.add_parser("doctor", help="hardware/tier/provider + facet readiness (§5.2/§12)")
    a.set_defaults(fn=_cmd_doctor)

    a = sub.add_parser("tier", help="show or set the engine tier (§5.2)")
    a.add_argument("value", nargs="?", default=None,
                   help="low|low-mid|mid|high|auto; omit to show current")
    a.set_defaults(fn=_cmd_tier)

    a = sub.add_parser("retry", help="re-queue errored jobs (§7)")
    a.add_argument("file_id", nargs="?", default=None, help="a file id, or 'all'")
    a.add_argument("--no-work", action="store_true")
    a.set_defaults(fn=_cmd_retry)

    a = sub.add_parser("bulk-tag", help="add/remove a manual tag on many files (§9)")
    a.add_argument("op", choices=["add", "rm"])
    a.add_argument("category"); a.add_argument("name")
    a.add_argument("file_ids", nargs="+", help="file ids, or 'all'")
    a.set_defaults(fn=_cmd_bulk_tag)

    a = sub.add_parser("status"); a.set_defaults(fn=_cmd_status)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "watch":
        _cmd_watch(None, args)
        return 0
    con = db.connect()
    args.fn(con, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
