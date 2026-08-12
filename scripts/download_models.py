#!/usr/bin/env python3
"""Model downloader CLI — a first cut of the §12 model manager.

Thin wrapper over indexer.models.download. The destination folder is
**user-selectable** and resolves in this order:
  1. --dir <path>                        (explicit, this run)
  2. env IMAGE_TAGGER_MODELS_DIR         (dev/test override)
  3. settings 'models_dir' in library.db (the folder chosen in-app / via
       `python -m indexer.cli models-dir <path>`)
  4. default <app home>/models

    python scripts/download_models.py wd14
    python scripts/download_models.py --dir D:/ai-models wd14

Prefer `python -m indexer.cli download wd14`, which always uses the configured
folder. This script exists for setup/CI without a running app.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "apps" / "indexer"))

from indexer.models.download import download, MODELS  # noqa: E402


def _app_home() -> Path:
    return Path(os.environ.get("IMAGE_TAGGER_HOME", str(Path.home() / ".image-tagger")))


def _configured_models_dir() -> Path:
    env = os.environ.get("IMAGE_TAGGER_MODELS_DIR")
    if env:
        return Path(env)
    db = _app_home() / "library.db"
    if db.exists():
        try:
            con = sqlite3.connect(db)
            row = con.execute(
                "SELECT value FROM settings WHERE key='models_dir'"
            ).fetchone()
            con.close()
            if row and row[0]:
                return Path(row[0])
        except sqlite3.Error:
            pass
    return _app_home() / "models"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+", choices=list(MODELS), help="model keys")
    ap.add_argument("--dir", default=None,
                    help="models base dir (default: the app's configured folder)")
    args = ap.parse_args()
    base = Path(args.dir) if args.dir else _configured_models_dir()
    print(f"models base dir: {base}")
    for key in args.models:
        download(key, base / key)


if __name__ == "__main__":
    main()
