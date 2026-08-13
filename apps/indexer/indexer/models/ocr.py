"""OCR facet — spec §10 (text visible in the image).

Reads literal text rendered *in* an image (manga bubbles, screenshots, memes,
signs) so the words become searchable on the same fast FTS path as tags. Runs
fully offline on CPU or GPU.

Engine: PP-OCR models via onnxruntime (RapidOCR) — the same detection +
recognition models PaddleOCR ships, without the heavy paddlepaddle runtime, so
it installs and runs on any tier including CPU (§10 tiering). The engine is
pluggable behind `OcrEngine`: swapping in PaddleOCR PP-OCRv5 with its dedicated
Thai recognition model (§10) is a backend change, not a pipeline change.

Design notes verified against the backend at build time (§16): RapidOCR's
callable returns `(result, elapse)` where result is a list of
`[box4pts, text, confidence]`, or `None` when no text is found.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass
class Region:
    text: str
    lang: str          # th | en | ...
    bbox: list[int]    # [x, y, w, h]
    confidence: float


_THAI = re.compile(r"[฀-๿]")


def guess_lang(text: str) -> str:
    """Cheap script heuristic — Thai if any Thai codepoint is present, else en.
    The recognizer itself is script-agnostic; this only labels the region."""
    return "th" if _THAI.search(text) else "en"


def _box_to_xywh(box) -> list[int]:
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    x, y = min(xs), min(ys)
    return [int(x), int(y), int(max(xs) - x), int(max(ys) - y)]


class OcrEngine:
    """Interface. `recognize(path)` returns detected text regions (may be empty)."""
    name = "base"

    def recognize(self, path: str) -> list[Region]:  # pragma: no cover
        raise NotImplementedError


class NullOcrEngine(OcrEngine):
    """Used when no OCR backend is installed: the pipeline still runs, files are
    still indexed, they just carry no ocr_text. Keeps M3 non-blocking (§16)."""
    name = "null"

    def recognize(self, path: str) -> list[Region]:
        return []


class RapidOcrEngine(OcrEngine):
    """PP-OCR (detection + recognition) via onnxruntime. Lazy, single instance."""
    name = "rapidocr"

    def __init__(self, min_confidence: float = 0.5,
                 providers: list[str] | None = None):
        from rapidocr_onnxruntime import RapidOCR  # lazy: import cost only if used
        providers = providers or ["CPUExecutionProvider"]
        use_cuda = "CUDAExecutionProvider" in providers
        use_dml = not use_cuda and "DmlExecutionProvider" in providers
        # RapidOCR's own wrapper only exposes cuda/dml toggles (no raw
        # onnxruntime `providers=`/provider_options passthrough), so unlike
        # WD14/InsightFace it can't be routed onto OpenVINOExecutionProvider /
        # the Intel NPU (§5.2) without vendoring a patched RapidOCR. It still
        # runs correctly on CPU/CUDA/DirectML tiers either way (§10 tiering).
        #
        # RapidOCR owns three ONNX sessions (detect/classify/recognize). Its
        # defaults force all three onto CPU and allow each session to consume
        # every core. Follow the same provider choice as WD14 and cap the CPU
        # fallback so indexing does not monopolize the machine.
        threads = 1 if (use_cuda or use_dml) else max(1, min(4, (os.cpu_count() or 2) // 2))
        self._ocr = RapidOCR(
            det_use_cuda=use_cuda, cls_use_cuda=use_cuda, rec_use_cuda=use_cuda,
            det_use_dml=use_dml, cls_use_dml=use_dml, rec_use_dml=use_dml,
            intra_op_num_threads=threads, inter_op_num_threads=1,
        )
        self.providers = providers
        self.device = "cuda" if use_cuda else ("dml" if use_dml else "cpu")
        self.min_confidence = min_confidence

    def recognize(self, path: str) -> list[Region]:
        result, _elapse = self._ocr(path)
        if not result:
            return []
        regions: list[Region] = []
        for box, text, conf in result:
            text = (text or "").strip()
            if not text or float(conf) < self.min_confidence:
                continue
            regions.append(Region(
                text=text,
                lang=guess_lang(text),
                bbox=_box_to_xywh(box),
                confidence=float(conf),
            ))
        return regions


_ENGINE: OcrEngine | None = None
_ENGINE_KEY = None


def get_engine(name: str | None = "rapidocr",
               providers: list[str] | None = None) -> OcrEngine:
    """Return a cached OCR engine. Falls back to NullOcrEngine if the requested
    backend isn't installed, so indexing never hard-fails on a missing model."""
    global _ENGINE, _ENGINE_KEY
    if _ENGINE_KEY == "manual":
        return _ENGINE
    key = (name, tuple(providers or ("CPUExecutionProvider",)))
    if (_ENGINE is not None and not isinstance(_ENGINE, NullOcrEngine)
            and _ENGINE_KEY == key):
        return _ENGINE
    if name in (None, "null"):
        _ENGINE = NullOcrEngine()
        _ENGINE_KEY = key
        return _ENGINE
    try:
        _ENGINE = RapidOcrEngine(providers=providers)
    except Exception:
        _ENGINE = NullOcrEngine()
    _ENGINE_KEY = key
    return _ENGINE


def set_engine(engine: OcrEngine) -> None:
    """Inject an engine (tests, or a tier/model switch)."""
    global _ENGINE, _ENGINE_KEY
    _ENGINE = engine
    _ENGINE_KEY = "manual"
