#!/usr/bin/env python3
"""M1 fail-fast gate: prove the search path holds <100ms at 500k rows.

Seeds N synthetic files+tags into the real schema (packages/db/schema.sql),
then benchmarks representative FTS queries. No AI models involved.

Usage:
    python scripts/bench_search.py [--rows 500000] [--db /tmp/bench.db]
"""
import argparse
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "..", "packages", "db", "schema.sql")

CHARACTERS = ["hatsune_miku", "kasane_teto", "rem", "asuka", "nahida",
              "gawr_gura", "usada_pekora", "frieren", "makima", "yor_forger"]
SERIES = ["vocaloid", "hololive", "genshin", "evangelion", "spy_family",
          "chainsaw_man", "frieren_series", "re_zero"]
SCENES = ["beach", "classroom", "city_street", "forest", "bedroom",
          "concert_stage", "shrine", "office", "rooftop", "snow"]
POSES = ["standing", "sitting", "running", "lying", "jumping", "walking"]
CLOTHES = ["school_uniform", "swimsuit", "kimono", "hoodie", "dress",
           "maid_outfit", "casual", "armor"]
WORDS = ["smile", "long_hair", "twintails", "blue_eyes", "night", "rain",
         "sunset", "food", "cat", "guitar", "headphones", "sakura"]


def seed(db: sqlite3.Connection, rows: int) -> float:
    t0 = time.perf_counter()
    cur = db.cursor()
    batch, fts_batch = [], []
    for i in range(1, rows + 1):
        ch = random.choice(CHARACTERS)
        se = random.choice(SERIES)
        sc = random.choice(SCENES)
        po = random.choice(POSES)
        cl = random.choice(CLOTHES)
        extra = " ".join(random.sample(WORDS, 4))
        folder = f"D:/Pictures/{se}/{ch}"
        filename = f"{ch}_{i:07d}.png"
        path = f"{folder}/{filename}"
        tags_text = f"{ch} {se} {sc} {po} {cl} {extra}"
        caption = f"a picture of {ch} {po} at the {sc} wearing {cl}"
        batch.append((i, path, filename, folder, f"sha{i:064d}"[:64], "done"))
        fts_batch.append((i, path, filename, folder, tags_text, "", caption, ""))
        if len(batch) >= 20000:
            cur.executemany(
                "INSERT INTO files (id,path,filename,folder,sha256,index_status)"
                " VALUES (?,?,?,?,?,?)", batch)
            cur.executemany(
                "INSERT INTO files_fts (rowid,path,filename,folder,tags_text,"
                "meta_text,caption,ocr_text) VALUES (?,?,?,?,?,?,?,?)", fts_batch)
            db.commit()
            batch, fts_batch = [], []
    if batch:
        cur.executemany(
            "INSERT INTO files (id,path,filename,folder,sha256,index_status)"
            " VALUES (?,?,?,?,?,?)", batch)
        cur.executemany(
            "INSERT INTO files_fts (rowid,path,filename,folder,tags_text,"
            "meta_text,caption,ocr_text) VALUES (?,?,?,?,?,?,?,?)", fts_batch)
        db.commit()
    return time.perf_counter() - t0


def bench(db: sqlite3.Connection):
    queries = [
        ("single tag",        'SELECT rowid FROM files_fts WHERE files_fts MATCH ? LIMIT 100', "miku"),
        ("two terms (AND)",   'SELECT rowid FROM files_fts WHERE files_fts MATCH ? LIMIT 100', "miku beach"),
        ("substring (trigram)", 'SELECT rowid FROM files_fts WHERE files_fts MATCH ? LIMIT 100', "eto"),
        ("caption phrase",    'SELECT rowid FROM files_fts WHERE files_fts MATCH ? LIMIT 100', '"school_uniform"'),
        ("count all matches", 'SELECT count(*) FROM files_fts WHERE files_fts MATCH ?', "sitting"),
        ("join back to files",
         'SELECT f.id,f.path FROM files_fts t JOIN files f ON f.id=t.rowid '
         'WHERE files_fts MATCH ? LIMIT 100', "pekora snow"),
    ]
    print(f"\n{'query':24s} {'p50 ms':>8s} {'p95 ms':>8s} {'max ms':>8s}  hits(first run)")
    ok = True
    for name, sql, term in queries:
        times, hits = [], 0
        for run in range(20):
            t0 = time.perf_counter()
            rows = db.execute(sql, (term,)).fetchall()
            times.append((time.perf_counter() - t0) * 1000)
            if run == 0:
                hits = rows[0][0] if name.startswith("count") else len(rows)
        p50 = statistics.median(times)
        p95 = sorted(times)[int(len(times) * 0.95) - 1]
        flag = "" if p95 < 100 else "  <-- OVER BUDGET"
        if p95 >= 100:
            ok = False
        print(f"{name:24s} {p50:8.2f} {p95:8.2f} {max(times):8.2f}  {hits}{flag}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=500_000)
    ap.add_argument("--db", help="keep the benchmark database at this path")
    args = ap.parse_args()
    auto_db = args.db is None
    db_path = args.db or os.path.join(tempfile.gettempdir(), "bench_tagger.db")

    if os.path.exists(db_path):
        os.remove(db_path)
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(open(SCHEMA, encoding="utf-8").read())

    print(f"Seeding {args.rows:,} rows into {db_path} ...")
    dt = seed(db, args.rows)
    size_mb = os.path.getsize(db_path) / 1e6
    print(f"Seeded in {dt:.1f}s  |  db size ~{size_mb:.0f} MB")

    ok = bench(db)
    db.close()
    if auto_db:
        for suffix in ("", "-wal", "-shm"):
            path = db_path + suffix
            if os.path.exists(path):
                os.remove(path)
    print("\nRESULT:", "PASS — all p95 < 100ms" if ok else "FAIL — see over-budget rows")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
