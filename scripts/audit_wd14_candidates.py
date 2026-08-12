"""Inspect raw WD14 character candidates without changing the library DB."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "apps" / "indexer"))

from indexer import db, engine  # noqa: E402
from indexer.models.wd14 import Wd14Engine  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Documents" / "test_search"
    con = db.connect()
    cfg = engine.get_engine_config(con)
    tagger = Wd14Engine(
        engine.active_model_dir(con, "wd14"),
        general_threshold=1.1,
        character_threshold=0.01,
        providers=cfg["onnx_providers"],
    )
    print("providers:", tagger.session.get_providers())
    for image in sorted(root.iterdir()):
        if image.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}:
            continue
        candidates = [
            (tag.category, tag.name, round(tag.confidence, 4))
            for tag in tagger.tag(str(image))
            if tag.category in {"character", "series"} and tag.confidence >= 0.05
        ]
        print(f"{image.name}: {candidates}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
