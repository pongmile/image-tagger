"""Real-face facet — spec §5 / §15 (M6).

InsightFace (buffalo_l) detects faces and produces a 512-d ArcFace embedding per
face. We never train: faces are grouped by *incremental clustering* on those
embeddings, the user names a cluster once (persons.name), and future faces
auto-attach to the nearest named cluster above a cosine cutoff (§5).

Pluggable behind `FaceEngine` (same pattern as the other facets): a real
InsightFace backend, a deterministic Fake for tests/CI, and a Null fallback.
Embeddings are L2-normalized so cosine similarity is a dot product; the
clustering logic in db.py operates purely on those vectors and is model-agnostic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Face:
    bbox: list[int]        # [x, y, w, h]
    embedding: list[float] # L2-normalized, len = dim
    det_score: float = 1.0


def l2(vec):
    n = math.sqrt(sum(float(x) * float(x) for x in vec)) or 1.0
    return [float(x) / n for x in vec]


class FaceEngine:
    dim = 512
    name = "base"

    def detect(self, path: str) -> list[Face]:
        raise NotImplementedError


class NullFaceEngine(FaceEngine):
    name = "null"

    def detect(self, path):
        return []


class InsightFaceEngine(FaceEngine):
    """buffalo_l detection + ArcFace recognition via insightface + onnxruntime."""
    name = "insightface"

    def __init__(self, model_dir=None, det_size=640, providers=None,
                 min_det_score=0.5, pack="buffalo_l"):
        from insightface.app import FaceAnalysis
        self.min_det_score = min_det_score
        self.pack = pack
        self.app = FaceAnalysis(
            name=pack, root=str(model_dir) if model_dir else None,
            providers=providers or ["CPUExecutionProvider"])
        self.app.prepare(ctx_id=0, det_size=(det_size, det_size))

    def detect(self, path):
        import numpy as np
        from ..imgio import open_oriented
        img = np.asarray(open_oriented(path).convert("RGB"))[:, :, ::-1]  # RGB->BGR
        out = []
        for f in self.app.get(img):
            if float(f.det_score) < self.min_det_score:
                continue
            x1, y1, x2, y2 = [int(v) for v in f.bbox]
            out.append(Face(
                bbox=[x1, y1, x2 - x1, y2 - y1],
                embedding=l2(f.normed_embedding.tolist()
                             if hasattr(f, "normed_embedding")
                             else f.embedding.tolist()),
                det_score=float(f.det_score),
            ))
        return out


class FakeFaceEngine(FaceEngine):
    """Deterministic engine for tests: maps a per-file list of face 'identities'
    to fixed embeddings, so clustering/naming/auto-attach are verifiable without
    the model. Configure with {basename: [(identity, bbox), ...]}; each identity
    string hashes to a stable unit vector (same identity -> same vector)."""
    name = "fake"

    def __init__(self, layout: dict[str, list], dim: int = 8, jitter: float = 0.0):
        self.layout = layout
        self.dim = dim
        self.jitter = jitter

    def _emb(self, identity: str):
        # stable pseudo-vector from the identity string
        import hashlib
        h = hashlib.sha256(identity.encode()).digest()
        vec = [((h[i % len(h)] / 255.0) - 0.5) for i in range(self.dim)]
        if self.jitter:
            # small deterministic per-instance jitter keeps same-identity close
            for i in range(self.dim):
                vec[i] += self.jitter * (((h[(i + 1) % len(h)]) / 255.0) - 0.5)
        return l2(vec)

    def detect(self, path):
        import os
        entries = self.layout.get(os.path.basename(path), [])
        faces = []
        for identity, bbox in entries:
            faces.append(Face(bbox=bbox, embedding=self._emb(identity)))
        return faces


_ENGINE: FaceEngine | None = None
_ENGINE_KEY = None
_LAST_ERROR: str | None = None


def get_engine(model_dir=None, name="insightface", **kw) -> FaceEngine:
    global _ENGINE, _ENGINE_KEY, _LAST_ERROR
    if _ENGINE_KEY == "manual":
        return _ENGINE
    key = (str(model_dir) if model_dir else None, name,
           tuple(kw.get("providers") or ()), kw.get("pack", "buffalo_l"))
    if (_ENGINE is not None and not isinstance(_ENGINE, NullFaceEngine)
            and _ENGINE_KEY == key):
        return _ENGINE
    if name in (None, "null"):
        _ENGINE = NullFaceEngine()
        _ENGINE_KEY = key
        return _ENGINE
    try:
        _ENGINE = InsightFaceEngine(model_dir=model_dir, **kw)
        _LAST_ERROR = None
    except Exception as exc:
        _LAST_ERROR = repr(exc)
        _ENGINE = NullFaceEngine()
    _ENGINE_KEY = key
    return _ENGINE


def last_error() -> str | None:
    return _LAST_ERROR


def set_engine(engine) -> None:
    global _ENGINE, _ENGINE_KEY
    _ENGINE = engine
    _ENGINE_KEY = "manual"
