"""Model downloader — the fetch half of the §12 model manager.

Shared by the CLI (`indexer.cli download`) and the standalone
`scripts/download_models.py`. Downloads into a caller-chosen directory so the
user can put multi-GB models on any drive (see db.get_models_dir precedence).
Skips files already fully present (resumable-ish); the desktop app layers
checksum verification + cancel on top (§12).
"""
from __future__ import annotations

import concurrent.futures
import sys
import threading
import urllib.request
from pathlib import Path

_PART_SIZE = 1 << 20          # 1 MiB read chunk
_MULTI_MIN = 24 << 20         # only segment files larger than 24 MiB
_MULTI_PARTS = 8              # concurrent connections per file

# Registry: model key -> (hf_repo, [files]). Pin variants here; tier presets
# (§5.2) choose the key per VRAM bucket.
MODELS = {
    "wd14": ("SmilingWolf/wd-v1-4-moat-tagger-v2",
             ["selected_tags.csv", "model.onnx"]),
}


def _progress(done: int, total: int, name: str) -> None:
    if total > 0:
        pct = done * 100 // total
        sys.stdout.write(f"\r  {name:22s} [{'#' * (pct // 4):<25s}] {pct:3d}% "
                         f"({done/1e6:.0f}/{total/1e6:.0f} MB)")
    else:
        sys.stdout.write(f"\r  {name:22s} {done/1e6:.0f} MB")
    sys.stdout.flush()


def _probe(url: str):
    """Return (total_bytes, supports_range). HEAD first; fall back to a ranged
    GET probe if HEAD is unsupported."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req) as r:
            total = int(r.headers.get("Content-Length", 0))
            ranges = r.headers.get("Accept-Ranges", "").lower() == "bytes"
            return total, ranges
    except Exception:
        return 0, False


def _fetch(url: str, dst: Path, on_progress=None, parts: int = _MULTI_PARTS) -> None:
    """Download `url` -> `dst`. For large files on a Range-capable server, splits
    the file into `parts` byte ranges and downloads them concurrently (a download
    accelerator); otherwise streams single-connection. Atomic via a .part file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    total, ranges = _probe(url)
    if ranges and total >= _MULTI_MIN and parts > 1:
        _fetch_segmented(url, tmp, total, parts, dst.name, on_progress)
    else:
        _fetch_single(url, tmp, total, dst.name, on_progress)
    tmp.replace(dst)   # atomic: a partial download never looks complete
    if on_progress is None:
        print()


def _fetch_single(url: str, tmp: Path, total: int, name: str, on_progress) -> None:
    with urllib.request.urlopen(url) as r:
        if not total:
            total = int(r.headers.get("Content-Length", 0))
        done, last = 0, 0
        with open(tmp, "wb") as f:
            for chunk in iter(lambda: r.read(_PART_SIZE), b""):
                f.write(chunk)
                done += len(chunk)
                if on_progress is not None:
                    if done - last >= (2 << 20) or done == total:
                        on_progress(name, done, total); last = done
                else:
                    _progress(done, total, name)


def _fetch_segmented(url: str, tmp: Path, total: int, parts: int,
                     name: str, on_progress) -> None:
    with open(tmp, "wb") as f:      # preallocate so threads can seek+write
        f.truncate(total)
    seg = total // parts
    spans = [(i * seg, (total - 1 if i == parts - 1 else (i + 1) * seg - 1))
             for i in range(parts)]
    done = [0]
    lock = threading.Lock()
    last = [0]

    def worker(start: int, end: int) -> None:
        req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(req) as r, open(tmp, "r+b") as f:
            f.seek(start)
            for chunk in iter(lambda: r.read(_PART_SIZE), b""):
                f.write(chunk)
                with lock:
                    done[0] += len(chunk)
                    if on_progress is not None:
                        if done[0] - last[0] >= (2 << 20) or done[0] >= total:
                            on_progress(name, done[0], total); last[0] = done[0]
                    else:
                        _progress(done[0], total, name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=parts) as ex:
        futures = [ex.submit(worker, s, e) for s, e in spans]
        for fut in concurrent.futures.as_completed(futures):
            fut.result()   # re-raise any worker error


def download_repo(repo: str, files, dest_dir: Path, on_progress=None) -> Path:
    """Download an explicit set of files from a Hugging Face `repo` into
    `dest_dir` (segmented/multi-connection for large files). Used for variant
    selection where the repo isn't fixed in the registry."""
    dest_dir = Path(dest_dir)
    for fn in files:
        out = dest_dir / fn
        if out.exists() and out.stat().st_size > 0:
            if on_progress is None:
                print(f"  {fn:22s} already present ({out.stat().st_size/1e6:.0f} MB)")
            continue
        _fetch(f"https://huggingface.co/{repo}/resolve/main/{fn}", out, on_progress)
    return dest_dir


def download(key: str, dest_dir: Path, on_progress=None) -> Path:
    """Download model `key`'s files into `dest_dir` (already the model's folder).
    `on_progress(file_name, done_bytes, total_bytes)` streams progress; if omitted,
    a text bar is printed (CLI use)."""
    if key not in MODELS:
        raise ValueError(f"unknown model '{key}'. Known: {', '.join(MODELS)}")
    repo, files = MODELS[key]
    dest_dir = Path(dest_dir)
    if on_progress is None:
        print(f"{key} -> {dest_dir}  (from {repo})")
    for fn in files:
        out = dest_dir / fn
        if out.exists() and out.stat().st_size > 0:
            if on_progress is None:
                print(f"  {fn:22s} already present ({out.stat().st_size/1e6:.0f} MB)")
            continue
        _fetch(f"https://huggingface.co/{repo}/resolve/main/{fn}", out, on_progress)
    if on_progress is None:
        print(f"done: {key}")
    return dest_dir
