"""M3 OCR pipeline test — spec §10, verification discipline §16.

Renders images with known text, runs the full pipeline (rescan -> worker, which
now chains ingest -> infer -> OCR), and asserts the text lands in ocr_regions +
files.ocr_text and is searchable on the FTS fast path. Uses the real RapidOCR
(PP-OCR onnx) backend when installed; skips gracefully otherwise.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_ocr
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _text_image(path: Path, text: str, size=(640, 180)) -> None:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    font = None
    for name in ("arial.ttf", "DejaVuSans.ttf", "Verdana.ttf"):
        try:
            font = ImageFont.truetype(name, 64)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    d.text((25, 55), text, fill=(0, 0, 0), font=font)
    img.save(path)


def run() -> int:
    # Unit check first: the language heuristic is pure and always testable.
    from indexer.models.ocr import guess_lang, get_engine, NullOcrEngine
    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    check(guess_lang("สวัสดี") == "th", "guess_lang detects Thai script")
    check(guess_lang("hello") == "en", "guess_lang defaults to English")

    engine = get_engine("rapidocr")
    if isinstance(engine, NullOcrEngine):
        print("\nNOTE: RapidOCR backend not installed — pipeline wiring covered "
              "by test_pipeline; skipping live recognition asserts.")
        return 1 if fails else 0

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_ocr_"))
    home, lib = tmp / "home", tmp / "lib"
    os.environ["IMAGE_TAGGER_HOME"] = str(home)

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    import indexer.ingest as ingest
    importlib.reload(ingest)
    from indexer.scan import rescan
    from indexer.worker import drain

    lib.mkdir(parents=True, exist_ok=True)
    _text_image(lib / "meme.png", "SALE FIFTY PERCENT OFF")
    _text_image(lib / "sign.jpg", "OPEN LATE NIGHT")

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    print("scan:", rescan(con))
    n = drain(con)          # runs ingest AND the chained infer (OCR) jobs
    print(f"worker processed {n} jobs")

    files = {Path(r["path"]).name: r for r in
             con.execute("SELECT * FROM files").fetchall()}
    meme = files.get("meme.png")

    # ocr_text stored on the file
    otext = (meme["ocr_text"] or "").upper() if meme else ""
    print("  meme ocr_text:", repr(otext))
    check("PERCENT" in otext or "SALE" in otext, "OCR text captured on files.ocr_text")

    # per-region rows stored (§10 ocr_regions)
    if meme:
        regs = con.execute(
            "SELECT text, lang, bbox, confidence FROM ocr_regions WHERE file_id=?",
            (meme["id"],)).fetchall()
        check(len(regs) >= 1, f"ocr_regions populated ({len(regs)} region(s))")
        check(all(r["bbox"] and r["confidence"] is not None for r in regs),
              "each region has bbox + confidence")

    # searchable on the FTS fast path
    def fts(q):
        return [r["rowid"] for r in con.execute(
            "SELECT rowid FROM files_fts WHERE files_fts MATCH ?", (q,))]

    hit = "PERCENT" in otext
    if hit:
        check(meme and meme["id"] in fts("percent"),
              "OCR word searchable via FTS ('percent')")
    else:
        # fall back to whatever the recognizer actually read
        word = next((w for w in otext.split() if len(w) >= 4), None)
        check(word is not None and meme["id"] in fts(word.lower()),
              f"OCR word searchable via FTS ('{word}')")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s) failed")
        return 1
    print("RESULT: PASS — M3 OCR pipeline verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
