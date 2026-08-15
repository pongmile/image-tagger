"""WD14 anime tagger facet — spec §5 (M4).

WD14 (SmilingWolf's wd-v1-4 family) is a booru-trained multi-label tagger shipped
as ONNX. It produces three tag groups from one image:

  * rating    (danbooru category 9) — general/sensitive/questionable/explicit
  * general   (category 0)           — descriptive booru tags (incl. pose words)
  * character (category 4)           — character identities

Thresholds are tunable and *separate* for general vs character (§5). Every tag
carries source='wd14' + confidence so manual tags can override and low-confidence
autotags can be filtered.

Also drives the kind router (§5 step 2): a light real-vs-illustration heuristic
over the fired tags labels image_kind ∈ {anime, real, other}.

Pluggable behind `TaggerEngine` (same pattern as OCR): the ONNX engine, a Null
fallback when no model is installed, and a Fake engine for deterministic tests.
Model I/O verified against the actual model at load time (§16): input name/shape
are read from the session, not assumed; output is treated as per-tag probabilities.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TagResult:
    category: str      # rating | general | character | pose
    name: str
    confidence: float


# General danbooru tags we lift into the dedicated `pose` category (§5: coarse
# pose via WD14). Everything else category-0 stays `general`.
POSE_TAGS = {
    "standing", "sitting", "lying", "kneeling", "squatting", "crouching",
    "running", "walking", "jumping", "leaning", "on back", "on side",
    "on stomach", "arms up", "spread arms", "kneeling", "seiza",
}
# Tags that flag a photographic/realistic image for the kind router.
_REAL_HINTS = {"realistic", "photorealistic", "photo", "3d"}
_PATH_CORROBORATION_THRESHOLD = 0.15

# Human-language aliases retained alongside the model's original booru tags.
# This is deliberately additive: the UI/audit can still show exactly what WD14
# emitted, while common searches such as "tummy", "chubby", and "shirtless"
# work without requiring users to know the training vocabulary.
GENERAL_ALIASES = {
    "belly": "tummy",
    "big belly": "tummy",
    "stomach": "tummy",
    "plump": "chubby",
    "fat": "chubby",
    "fat rolls": "chubby",
    "topless": "shirtless",
    "nude": "shirtless",
    "completely nude": "shirtless",
}


def add_human_aliases(tags: list[TagResult]) -> list[TagResult]:
    """Return tags plus non-destructive, confidence-preserving aliases.

    If several source tags imply the same alias, keep the strongest confidence.
    Existing native tags always win, so this helper cannot duplicate or weaken a
    model result.
    """
    out = list(tags)
    existing = {(t.category, t.name): t.confidence for t in out}
    aliases: dict[tuple[str, str], float] = {}
    for tag in tags:
        if tag.category != "general":
            continue
        alias = GENERAL_ALIASES.get(tag.name)
        key = ("general", alias) if alias else None
        if key and key not in existing:
            aliases[key] = max(aliases.get(key, 0.0), tag.confidence)
    out.extend(TagResult(category, name, confidence)
               for (category, name), confidence in aliases.items())
    return out


def filename_words(path: str) -> str:
    """Normalize a filename into words, including letter/number boundaries.

    Camera/download filenames commonly join identity and sequence number (for
    example ``mei1.png``). Treating that as ``mei 1`` lets a strong-but-borderline
    model identity be corroborated without accepting arbitrary low scores.
    """
    value = os.path.basename(path).lower()
    value = re.sub(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


class TaggerEngine:
    name = "base"

    def tag(self, path: str) -> list[TagResult]:  # pragma: no cover
        raise NotImplementedError

    def image_kind(self, tags: list[TagResult]) -> str:
        """Light real-vs-illustration heuristic (§5 step 2)."""
        general = {t.name for t in tags if t.category in ("general", "pose")}
        if general & _REAL_HINTS:
            return "real"
        if len(general) >= 3:
            return "anime"
        return "other"


class NullTaggerEngine(TaggerEngine):
    name = "null"

    def tag(self, path: str) -> list[TagResult]:
        return []

    def image_kind(self, tags):
        return "other"


class Wd14Engine(TaggerEngine):
    """ONNX WD14 tagger via onnxruntime. Lazy, single instance."""
    name = "wd14"

    def __init__(self, model_dir: str | Path,
                 general_threshold: float = 0.35,
                 character_threshold: float = 0.75,
                 providers: list[str] | None = None):
        import onnxruntime as ort
        import numpy as np  # noqa: F401  (used in tag())

        model_dir = Path(model_dir)
        model_path = model_dir / "model.onnx"
        tags_path = model_dir / "selected_tags.csv"
        if not model_path.exists() or not tags_path.exists():
            raise FileNotFoundError(
                f"WD14 model files not found in {model_dir} "
                f"(need model.onnx + selected_tags.csv)")

        self.general_threshold = general_threshold
        self.character_threshold = character_threshold
        from .. import engine as _engine
        _engine.ensure_gpu_libs(providers)
        self.session = ort.InferenceSession(
            str(model_path),
            providers=_engine.onnx_provider_options(providers or ["CPUExecutionProvider"]),
        )
        _engine.note_onnx_session(self.session)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        # input shape [1, H, W, 3]; dims may be strings/None -> default 448.
        dims = inp.shape
        self.size = next((d for d in dims[1:3] if isinstance(d, int)), 448) or 448
        self.output_name = self.session.get_outputs()[0].name
        self._load_tags(tags_path)

    def _load_tags(self, path: Path) -> None:
        self.names: list[str] = []
        self.categories: list[int] = []
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                # Booru CSV names use underscores. Store human-readable tags in
                # the app DB; FTS still matches every word and the UI no longer
                # exposes model-internal naming conventions.
                self.names.append(row["name"].replace("_", " ").strip())
                self.categories.append(int(row["category"]))

    def _preprocess(self, path: str):
        import numpy as np
        from PIL import Image
        from ..imgio import open_oriented
        img = open_oriented(path).convert("RGBA")
        # Composite onto white so transparency doesn't read as black.
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img).convert("RGB")
        # Pad to square (white), then resize to model input.
        w, h = img.size
        side = max(w, h)
        square = Image.new("RGB", (side, side), (255, 255, 255))
        square.paste(img, ((side - w) // 2, (side - h) // 2))
        square = square.resize((self.size, self.size), Image.BILINEAR)
        arr = np.asarray(square, dtype="float32")      # HWC, RGB, 0-255
        arr = arr[:, :, ::-1]                          # RGB -> BGR (wd14 expects BGR)
        return np.expand_dims(arr, 0)                  # NHWC [1,S,S,3]

    def tag(self, path: str) -> list[TagResult]:
        batch = self._preprocess(path)
        probs = self.session.run([self.output_name], {self.input_name: batch})[0][0]
        out: list[TagResult] = []
        best_rating = None
        filename_signal = filename_words(path)
        for name, cat, p in zip(self.names, self.categories, probs):
            p = float(p)
            if cat == 9:  # rating: keep only the single most likely
                if best_rating is None or p > best_rating[1]:
                    best_rating = (name, p)
            elif cat == 4:  # character
                qualified = re.match(r"^(.+?)\s*\(([^()]+)\)$", name)
                character_name = qualified.group(1).strip() if qualified else name
                path_key = re.sub(r"[^a-z0-9]+", " ", character_name.lower()).strip()
                corroborated = (
                    p >= _PATH_CORROBORATION_THRESHOLD
                    and path_key
                    and re.search(rf"(?:^|\s){re.escape(path_key)}(?:\s|$)", filename_signal)
                )
                if p >= self.character_threshold or corroborated:
                    # WD14 encodes franchise-qualified identities as
                    # "mei_(overwatch)". Split that into two useful facets so a
                    # result visibly says both character:mei and series:overwatch.
                    if qualified:
                        out.append(TagResult("character", character_name, p))
                        out.append(TagResult("series", qualified.group(2).strip(), p))
                    else:
                        out.append(TagResult("character", name, p))
            elif cat == 0:  # general (may reclassify to pose)
                if p >= self.general_threshold:
                    category = "pose" if name in POSE_TAGS else "general"
                    out.append(TagResult(category, name, p))
        if best_rating:
            out.append(TagResult("rating", best_rating[0], best_rating[1]))
        return add_human_aliases(out)


class FakeTaggerEngine(TaggerEngine):
    """Deterministic engine for pipeline tests / CI without the 300MB model."""
    name = "fake"

    def __init__(self, results: list[TagResult]):
        self._results = results

    def tag(self, path: str) -> list[TagResult]:
        return list(self._results)


_ENGINE: TaggerEngine | None = None
_ENGINE_KEY = None


def get_engine(model_dir=None, name: str = "wd14", **kw) -> TaggerEngine:
    global _ENGINE, _ENGINE_KEY
    if _ENGINE_KEY == "manual":
        return _ENGINE
    key = (str(Path(model_dir).resolve()) if model_dir else None, name,
           kw.get("general_threshold"), kw.get("character_threshold"),
           tuple(kw.get("providers") or ()))
    if (_ENGINE is not None and not isinstance(_ENGINE, NullTaggerEngine)
            and _ENGINE_KEY == key):
        return _ENGINE
    if name in (None, "null"):
        _ENGINE = NullTaggerEngine()
        _ENGINE_KEY = key
        return _ENGINE
    try:
        _ENGINE = Wd14Engine(model_dir, **kw)
    except Exception:
        _ENGINE = NullTaggerEngine()
    _ENGINE_KEY = key
    return _ENGINE


def set_engine(engine: TaggerEngine) -> None:
    global _ENGINE, _ENGINE_KEY
    _ENGINE = engine
    _ENGINE_KEY = "manual"
