"""CLIP zero-shot facet — spec §5 (scene/clothing/type) + §8 (semantic search).

CLIP maps images and text into one shared embedding space. Two uses here:

  * Zero-shot tagging over an *editable* label vocabulary (clip_labels table):
    embed the image, score it against "a photo of {label}" prompts per category,
    emit the winners as source='clip' tags. Open-vocab — new labels need no
    retraining (§5).
  * The image embedding is stored in file_vec (§6) and powers optional semantic
    search (§8) and the few-shot learned-tag space (§5.3).

Pluggable behind `ClipEngine`: a real open_clip backend, a deterministic Fake
(for CI/tests without the ~600MB weights), and a Null fallback. Embeddings are
always L2-normalized, so cosine similarity is a plain dot product.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

DEFAULT_DIM = 512
PROMPT = "a photo of {}"


@dataclass
class ZeroShot:
    category: str
    name: str
    confidence: float


def _l2(vec):
    n = math.sqrt(sum(float(x) * float(x) for x in vec)) or 1.0
    return [float(x) / n for x in vec]


def _softmax(xs):
    m = max(xs) if xs else 0.0
    exps = [math.exp(x - m) for x in xs]
    s = sum(exps) or 1.0
    return [e / s for e in exps]


class ClipEngine:
    dim = DEFAULT_DIM
    name = "base"

    def encode_image(self, path: str):  # -> list[float], L2-normalized
        raise NotImplementedError

    def encode_texts(self, texts):      # -> list[list[float]], each L2-normalized
        raise NotImplementedError

    def classify(self, image_emb, vocab: dict[str, list[str]],
                 threshold: float = 0.5, temperature: float = 100.0
                 ) -> list[ZeroShot]:
        """Zero-shot over a {category: [labels]} vocab. Per category, softmax the
        cosine scores (CLIP-style temperature) and keep labels above `threshold`.
        Returns the fired (category, label, prob) triples."""
        out: list[ZeroShot] = []
        for category, labels in vocab.items():
            if not labels:
                continue
            texts = [PROMPT.format(lbl) for lbl in labels]
            tembs = self.encode_texts(texts)
            sims = [sum(a * b for a, b in zip(image_emb, te)) for te in tembs]
            probs = _softmax([s * temperature for s in sims])
            for lbl, p in zip(labels, probs):
                if p >= threshold:
                    out.append(ZeroShot(category, lbl, float(p)))
        return out


class NullClipEngine(ClipEngine):
    name = "null"

    def encode_image(self, path):
        return None

    def encode_texts(self, texts):
        return []

    def classify(self, image_emb, vocab, threshold=0.5, temperature=100.0):
        return []


class RealClipEngine(ClipEngine):
    """open_clip backend (CPU or GPU). Weights download once into cache_dir."""
    name = "clip"

    def __init__(self, model_name="ViT-B-32", pretrained="openai",
                 cache_dir=None, device="cpu"):
        import open_clip
        import torch
        self._torch = torch
        self.device = device
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained, cache_dir=cache_dir)
        self.model.eval().to(device)
        self.tokenizer = open_clip.get_tokenizer(model_name)
        self.dim = self.model.text_projection.shape[1]
        self._text_cache: dict[str, list[float]] = {}

    def encode_image(self, path):
        from ..imgio import open_oriented
        img = self.preprocess(open_oriented(path).convert("RGB")).unsqueeze(0).to(self.device)
        with self._torch.no_grad():
            feats = self.model.encode_image(img)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats[0].cpu().tolist()

    def encode_texts(self, texts):
        texts = list(texts)
        missing = [t for t in texts if t not in self._text_cache]
        if missing:
            tok = self.tokenizer(missing).to(self.device)
            with self._torch.no_grad():
                feats = self.model.encode_text(tok)
                feats = feats / feats.norm(dim=-1, keepdim=True)
            for t, f in zip(missing, feats.cpu().tolist()):
                self._text_cache[t] = f
        return [self._text_cache[t] for t in texts]


class FakeClipEngine(ClipEngine):
    """Deterministic engine for tests: embeds text/images as one-hot-ish vectors
    over a fixed keyword axis list, so a 'beach.png' image aligns with the 'beach'
    label. Real cosine geometry, no weights."""
    name = "fake"

    def __init__(self, axes: list[str]):
        self.axes = axes
        self.dim = len(axes)

    def _vec(self, text: str):
        t = (text or "").lower()
        raw = [1.0 if ax in t else 0.0 for ax in self.axes]
        if not any(raw):
            raw = [1e-3] * self.dim
        return _l2(raw)

    def encode_image(self, path):
        return self._vec(os.path.basename(path))

    def encode_texts(self, texts):
        return [self._vec(t) for t in texts]


_ENGINE: ClipEngine | None = None
_ENGINE_KEY = None
_LAST_ERROR: str | None = None


def get_engine(model_dir=None, name="clip", model_name="ViT-B-32",
               pretrained="openai", device="cpu") -> ClipEngine:
    global _ENGINE, _ENGINE_KEY, _LAST_ERROR
    if _ENGINE_KEY == "manual":
        return _ENGINE
    key = (os.path.abspath(str(model_dir)) if model_dir else None, name,
           model_name, pretrained, device)
    if (_ENGINE is not None and not isinstance(_ENGINE, NullClipEngine)
            and _ENGINE_KEY == key):
        return _ENGINE
    if name in (None, "null"):
        _ENGINE = NullClipEngine()
        _ENGINE_KEY = key
        return _ENGINE
    try:
        _ENGINE = RealClipEngine(model_name, pretrained, cache_dir=model_dir,
                                 device=device)
        _LAST_ERROR = None
    except Exception as exc:
        _LAST_ERROR = repr(exc)
        _ENGINE = NullClipEngine()
    _ENGINE_KEY = key
    return _ENGINE


def last_error() -> str | None:
    return _LAST_ERROR


def set_engine(engine) -> None:
    global _ENGINE, _ENGINE_KEY
    _ENGINE = engine
    _ENGINE_KEY = "manual"
