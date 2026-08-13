"""Engine & hardware tiers — spec §5.2 (M9).

Auto-detects the execution provider + VRAM bucket at first run and recommends a
tier; the user can override globally (settings 'tier' / 'provider') or per task.
Two independent knobs, per §5.2:

  * Execution provider (auto, overridable): CUDA -> DirectML -> CPU.
  * Model preset = per-task variant + precision + batch, keyed to a VRAM bucket.

**The VRAM bucket, not the GPU name, decides the preset.** Detection keys off
measured VRAM (torch/NVML); if detection misreports, the user overrides the tier.
CUDA OOM handling (retry smaller batch/variant, never hard-fail) lives in the
worker's batching path; this module just chooses the starting preset.
"""
from __future__ import annotations

# Tier -> preset knobs. Model *ids* are pinned in models/download.py and the
# engines; the preset tunes batch/precision/which facets default on, keyed to the
# VRAM bucket. Variant names in the spec are representative — confirmed at build.
PRESETS = {
    "low":     {"vram_bucket": "cpu / <6GB",  "wd14_batch": 1,  "precision": "int8",
                "clip_model": "ViT-B-32", "caption_default": False},
    "low-mid": {"vram_bucket": "6-8GB",       "wd14_batch": 4,  "precision": "fp16",
                "clip_model": "ViT-B-16", "caption_default": True},
    "mid":     {"vram_bucket": "8-12GB",      "wd14_batch": 8,  "precision": "fp16",
                "clip_model": "ViT-L-14", "caption_default": True},
    "high":    {"vram_bucket": "16GB+",       "wd14_batch": 16, "precision": "fp16",
                "clip_model": "ViT-H-14", "caption_default": True},
}
TIER_ORDER = ["low", "low-mid", "mid", "high"]


def recommend_tier(vram_gb: float, has_gpu: bool) -> str:
    """Map measured VRAM to a tier (§5.2). CPU-only -> low."""
    if not has_gpu or vram_gb <= 0:
        return "low"
    if vram_gb < 5.5:
        return "low"
    if vram_gb < 7.5:
        return "low-mid"
    # A marketed 8/16 GB card reports roughly 7.9/15.9 GiB after reservation.
    if vram_gb < 15.5:
        return "mid"
    return "high"


def _probe_python(code: str, timeout: float = 8.0) -> str | None:
    """Run `code` in a short-lived Python subprocess and return its stripped
    stdout, or None on any failure/timeout.

    Hardware probes must never import torch/onnxruntime into *this* process:
    detect_hardware() runs inside the long-lived indexer daemon (it backs
    get_engine_config(), called on almost every status/tier read), and once a
    native runtime is imported there, Windows keeps its DLLs locked for the
    rest of the daemon's life. A later ``pip install --target`` repair or
    provider switch (onnxruntime CPU/CUDA/DirectML/OpenVINO, or a torch
    CPU/CUDA swap — see _dependency_install_worker) then fails trying to
    replace those files with ``PermissionError: Access is denied``. Probing in
    a disposable subprocess sidesteps that entirely.
    """
    import os
    import subprocess
    import sys
    from . import config
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(config.RUNTIME_PACKAGES_DIR) + (
        os.pathsep + existing if existing else "")
    try:
        out = subprocess.check_output(
            [sys.executable, "-c", code], text=True, timeout=timeout,
            stderr=subprocess.DEVNULL, env=env,
        )
        return out.strip()
    except Exception:
        return None


def detect_hardware() -> dict:
    """Best-effort probe of GPU/VRAM + available onnxruntime providers. Never
    raises — missing torch/onnxruntime just yields the CPU answer."""
    has_gpu, vram_gb, gpu_name = False, 0.0, None
    torch_cuda = False
    torch_probe = _probe_python(
        "import json\n"
        "info = {'cuda': False, 'vram_gb': 0.0, 'name': None}\n"
        "try:\n"
        "    import torch\n"
        "    if torch.cuda.is_available():\n"
        "        info['cuda'] = True\n"
        "        props = torch.cuda.get_device_properties(0)\n"
        "        info['vram_gb'] = round(props.total_memory / (1024 ** 3), 1)\n"
        "        info['name'] = props.name\n"
        "except Exception:\n"
        "    pass\n"
        "print(json.dumps(info))\n"
    )
    if torch_probe:
        try:
            import json
            info = json.loads(torch_probe)
            torch_cuda = bool(info.get("cuda"))
            if torch_cuda:
                has_gpu, vram_gb = True, float(info.get("vram_gb") or 0.0)
                gpu_name = info.get("name")
        except Exception:
            torch_cuda = False

    # A CPU-only torch wheel must not hide an NVIDIA GPU from the Models screen.
    # nvidia-smi ships with the driver and is the most reliable pre-install probe
    # before the user has selected a CUDA torch/onnxruntime build.
    if not has_gpu:
        try:
            import subprocess
            raw = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                text=True, timeout=5, stderr=subprocess.DEVNULL,
            ).splitlines()[0]
            name, mib = [x.strip() for x in raw.rsplit(",", 1)]
            has_gpu = True
            gpu_name = name
            vram_gb = round(float(mib) / 1024, 1)
        except Exception:
            pass

    onnx_providers = []
    onnx_probe = _probe_python(
        "import json\n"
        "try:\n"
        "    import onnxruntime as ort\n"
        "    print(json.dumps(list(ort.get_available_providers())))\n"
        "except Exception:\n"
        "    print('[]')\n"
    )
    if onnx_probe:
        try:
            import json
            onnx_providers = json.loads(onnx_probe)
        except Exception:
            onnx_providers = []

    # Hardware presence and torch readiness are intentionally distinct. A GPU
    # can be detected while the current torch wheel is still CPU-only.
    torch_device = "cuda" if torch_cuda else "cpu"
    return {
        "has_gpu": has_gpu,
        "vram_gb": vram_gb,
        "gpu_name": gpu_name,
        "torch_cuda": torch_cuda,
        "onnx_providers": onnx_providers,
        "torch_device": torch_device,
    }


def resolve_onnx_providers(available: list[str] | None = None,
                           prefer: str | None = None) -> list[str]:
    """Ordered onnxruntime provider list, best-first (§5.2 CUDA -> DirectML ->
    OpenVINO (Intel NPU/iGPU) -> CPU). `prefer` pins a provider to the front if
    it's available. OpenVINOExecutionProvider only appears in `available` when
    the optional `onnxruntime-openvino` build is installed (requirements-models
    §NPU) — plain `onnxruntime`/`onnxruntime-gpu`/`onnxruntime-directml` don't
    ship it, matching how CUDA/DirectML are already gated on the installed
    wheel rather than a separate hardware probe."""
    if available is None:
        available = detect_hardware()["onnx_providers"]
    order = ["CUDAExecutionProvider", "DmlExecutionProvider",
             "OpenVINOExecutionProvider", "CPUExecutionProvider"]
    if prefer:
        order = [prefer] + [p for p in order if p != prefer]
    picked = [p for p in order if p in available]
    if "CPUExecutionProvider" not in picked:
        picked.append("CPUExecutionProvider")   # always the universal fallback
    return picked


def onnx_provider_options(providers: list[str],
                          npu_device_type: str = "NPU") -> list:
    """Pair provider names with the options onnxruntime needs to actually use
    them (§5.2 Intel NPU support). Plain provider-name strings default to
    whichever device each EP prefers on its own — but OpenVINOExecutionProvider
    defaults to CPU, not the NPU, so it must be paired with an explicit
    ``device_type`` to route work to the NPU. If the NPU isn't present,
    OpenVINO's own device init fails and onnxruntime falls through to the next
    provider in the list (CPU), same as an unavailable CUDA/DirectML device.
    Every other provider passes through unchanged; onnxruntime accepts a mixed
    list of plain names and (name, options) tuples for `providers=`."""
    return [(p, {"device_type": npu_device_type})
            if p == "OpenVINOExecutionProvider" else p
            for p in providers]


# The model catalog — the single source of truth for *which* model each facet
# uses, where it comes from, and how big it is (§12). Shown in the UI so the user
# never has to guess. `kind`: direct = we fetch the files ourselves; library =
# the facet's pip package downloads its own weights on first use; pip = the model
# ships inside the pip dependency (no separate download).
CATALOG = {
    "ocr": {"name": "PP-OCRv4 (Thai+English)", "source": "pip: rapidocr-onnxruntime",
            "url": "https://pypi.org/project/rapidocr-onnxruntime/",
            "size_mb": 12, "kind": "pip"},
    "wd14": {"name": "wd-v1-4-moat-tagger-v2", "source": "Hugging Face · SmilingWolf",
             "url": "https://huggingface.co/SmilingWolf/wd-v1-4-moat-tagger-v2",
             "size_mb": 326, "kind": "direct"},
    "clip": {"name": "CLIP ViT-B-32 (OpenAI)", "source": "open_clip · OpenAI weights",
             "url": "https://huggingface.co/timm/vit_base_patch32_clip_224.openai",
             "size_mb": 340, "kind": "library"},
    "sqlite_vec": {"name": "sqlite-vec extension", "source": "pip: sqlite-vec",
                   "url": "https://pypi.org/project/sqlite-vec/", "size_mb": 1, "kind": "pip"},
    "insightface": {"name": "buffalo_l (SCRFD + ArcFace)", "source": "InsightFace model zoo",
                    "url": "https://github.com/deepinsight/insightface/tree/master/model_zoo",
                    "size_mb": 326, "kind": "library"},
    "sklearn": {"name": "linear head (trained locally)", "source": "pip: scikit-learn",
                "url": "https://pypi.org/project/scikit-learn/", "size_mb": 0, "kind": "pip"},
    "caption": {"name": "BLIP image-captioning-base", "source": "Hugging Face · Salesforce",
                "url": "https://huggingface.co/Salesforce/blip-image-captioning-base",
                "size_mb": 990, "kind": "library"},
}


# Selectable model variants per facet, keyed to a hardware tier (§5.2). Bigger
# tier = bigger/better/slower model. The app recommends one by the detected
# GPU/NPU tier; the user can override per facet.
VARIANTS = {
    "wd14": [
        {"id": "moat-v2", "label": "MOAT v2 (balanced)", "tier": "low", "size_mb": 326,
         "repo": "SmilingWolf/wd-v1-4-moat-tagger-v2"},
        {"id": "convnext-v2", "label": "ConvNeXT v2", "tier": "low-mid", "size_mb": 378,
         "repo": "SmilingWolf/wd-v1-4-convnext-tagger-v2"},
        {"id": "swinv2-v2", "label": "SwinV2 v2 (accurate)", "tier": "mid", "size_mb": 377,
         "repo": "SmilingWolf/wd-v1-4-swinv2-tagger-v2"},
        {"id": "eva02-large-v3", "label": "EVA02-Large v3 (best)", "tier": "high", "size_mb": 1400,
         "repo": "SmilingWolf/wd-eva02-large-tagger-v3"},
    ],
    "clip": [
        {"id": "vitb32", "label": "ViT-B/32 (fast)", "tier": "low", "size_mb": 340,
         "model": "ViT-B-32", "pretrained": "openai", "dim": 512},
        {"id": "vitb16", "label": "ViT-B/16", "tier": "low-mid", "size_mb": 340,
         "model": "ViT-B-16", "pretrained": "openai", "dim": 512},
        {"id": "vitl14", "label": "ViT-L/14 (accurate)", "tier": "mid", "size_mb": 890,
         "model": "ViT-L-14", "pretrained": "openai", "dim": 768},
        {"id": "vith14", "label": "ViT-H/14 (best)", "tier": "high", "size_mb": 3900,
         "model": "ViT-H-14", "pretrained": "laion2b_s32b_b79k", "dim": 1024},
    ],
    "insightface": [
        {"id": "buffalo_s", "label": "buffalo_s (light)", "tier": "low", "size_mb": 100,
         "pack": "buffalo_s"},
        {"id": "buffalo_l", "label": "buffalo_l (accurate)", "tier": "mid", "size_mb": 326,
         "pack": "buffalo_l"},
    ],
    "caption": [
        {"id": "blip-base", "label": "BLIP base (fast)", "tier": "low", "size_mb": 990,
         "model_id": "Salesforce/blip-image-captioning-base", "engine": "blip"},
        {"id": "blip-large", "label": "BLIP large (accurate)", "tier": "mid", "size_mb": 1900,
         "model_id": "Salesforce/blip-image-captioning-large", "engine": "blip"},
        # Opt-in only: these share BLIP-large's tier and sort after it, so the
        # recommendation algorithm never selects a 16 GB GPU-only model by itself.
        {"id": "joycaption-4bit",
         "label": "JoyCaption (open, uncensored · 4-bit, ~6GB VRAM)",
         "tier": "mid", "size_mb": 16000,
         "model_id": "fancyfeast/llama-joycaption-beta-one-hf-llava",
         "engine": "joycaption", "load_in_4bit": True},
        {"id": "joycaption",
         "label": "JoyCaption (open, uncensored · full, ~17GB VRAM)",
         "tier": "mid", "size_mb": 16000,
         "model_id": "fancyfeast/llama-joycaption-beta-one-hf-llava",
         "engine": "joycaption", "load_in_4bit": False},
    ],
}
_TIER_RANK = {"low": 0, "low-mid": 1, "mid": 2, "high": 3}


def recommended_variant_id(facet: str, tier: str) -> str | None:
    """Highest-tier variant not exceeding the machine tier; else the smallest."""
    vs = VARIANTS.get(facet, [])
    if not vs:
        return None
    tr = _TIER_RANK.get(tier, 0)
    eligible = [v for v in vs if _TIER_RANK.get(v["tier"], 0) <= tr]
    pick = (max(eligible, key=lambda v: _TIER_RANK[v["tier"]]) if eligible
            else min(vs, key=lambda v: _TIER_RANK[v["tier"]]))
    return pick["id"]


def variant_by_id(facet: str, variant_id: str) -> dict | None:
    """Look up a specific variant definition regardless of what's applied —
    used to download a variant the user has picked but not yet committed to
    (§12 Models UX: browsing the dropdown must not itself change what's
    active; only an explicit Apply does that)."""
    return next((v for v in VARIANTS.get(facet, []) if v["id"] == variant_id), None)


def selected_variant(con, facet: str) -> dict | None:
    """The user's chosen variant for a facet, or the tier-recommended default."""
    vs = VARIANTS.get(facet, [])
    if not vs:
        return None
    from . import db as _db
    chosen = _db.get_setting(con, f"{facet}_variant") if con is not None else None
    vid = chosen if any(v["id"] == chosen for v in vs) else \
        recommended_variant_id(facet, get_engine_config(con)["tier"] if con else "low")
    return next((v for v in vs if v["id"] == vid), vs[0])


def variants_view(con) -> list[dict]:
    """All facets that have selectable variants, with the current + recommended
    selection (for the Models UI)."""
    tier = get_engine_config(con)["tier"]
    out = []
    for facet, vs in VARIANTS.items():
        out.append({
            "facet": facet,
            "tier": tier,
            "recommended": recommended_variant_id(facet, tier),
            "selected": selected_variant(con, facet)["id"],
            "variants": vs,
        })
    return out


def model_dir_for_variant(con, facet: str, variant: dict | None = None):
    """Directory for the selected variant. Variant weights often use identical
    filenames (``model.onnx``), so sharing one folder silently kept the previous
    model after a selection change. Explicit per-model env dirs retain their
    legacy exact-path behavior for tests and advanced users.
    """
    import os
    from . import db as _db
    base = _db.model_dir(con, facet)
    if os.environ.get(f"IMAGE_TAGGER_{facet.upper()}_DIR"):
        return base
    variant = variant if variant is not None else selected_variant(con, facet)
    return base / variant["id"] if variant else base


def active_model_dir(con, facet: str):
    """Directory for the currently applied variant."""
    return model_dir_for_variant(con, facet)


def model_ready_marker(con, facet: str, variant: dict | None = None):
    """Variant-specific marker written only after a library-managed model was
    successfully loaded. Importing a Python dependency is not proof that its
    weights were downloaded, which was the old Models-screen false positive.
    """
    return model_dir_for_variant(con, facet, variant) / ".ready"


def facet_readiness(con=None) -> list[dict]:
    """Per-facet readiness + catalog info for the model-download manager (§12):
    dependency importable? model files present? enabled? which model, from where,
    how big, and where it lands on disk."""
    import importlib.util
    from pathlib import Path
    from . import config, db as _db
    importlib.invalidate_caches()

    def has(mod):
        """A namespace stub is not an installed dependency.

        Interrupted ``pip --target`` runs (daemon._dependency_install_worker)
        can leave an empty directory behind; a bare find_spec() still reports
        that as a namespace package, which would make the Models screen show
        "ready" for a dependency that never actually finished installing.
        Mirrors daemon.py's own `installed()` guard for the same reason.
        """
        spec = importlib.util.find_spec(mod)
        return bool(spec and spec.origin and spec.origin != "namespace"
                    and Path(spec.origin).is_file())

    wd14_model = None
    clip_present = faces_present = caption_present = False
    if con is not None:
        wd14_dir = active_model_dir(con, "wd14")
        wd14_model = all((wd14_dir / name).exists()
                         for name in ("model.onnx", "selected_tags.csv"))
        clip_present = model_ready_marker(con, "clip").exists()
        faces_present = model_ready_marker(con, "insightface").exists()
        caption_present = model_ready_marker(con, "caption").exists()

    facets = [
        # label, milestone, dep module, catalog key, model_ok, enabled, download key
        ("ocr", "OCR (text in image)", "M3", "rapidocr_onnxruntime", "ocr",
         has("rapidocr_onnxruntime"), None),
        ("wd14", "Anime tagger (WD14)", "M4", "onnxruntime", "wd14",
         bool(wd14_model) if con is not None else None, "wd14"),
        ("clip", "CLIP scene/clothing", "M5", "open_clip", "clip",
         clip_present if con is not None else None, "clip"),
        (None, "Semantic search (sqlite-vec)", "M5/M7", "sqlite_vec", "sqlite_vec",
         has("sqlite_vec"), None),
        ("faces", "Real faces (InsightFace)", "M6", "insightface", "insightface",
         faces_present if con is not None else None, "insightface"),
        (None, "Learned tags", "M7", "sklearn", "sklearn", has("sklearn"), None),
        ("caption", "Captioning (BLIP / JoyCaption)", "M8", "transformers", "caption",
         caption_present if con is not None else None, "caption"),
    ]
    out = []
    for facet, label, ms, mod, ckey, model_ok, dl in facets:
        dep = has(mod)
        if facet == "caption":
            dep = dep and has("accelerate")
        m = dep if model_ok is None and dl is None else bool(model_ok)
        ready = dep and (m if dl else True)
        state = ("ready" if ready else
                 "dep missing" if not dep else "model not downloaded")
        cat = CATALOG.get(ckey, {})
        enabled = config.facet_enabled(con, facet) if facet else True
        row = {"facet": facet, "label": label, "milestone": ms,
               "dep": "transformers + accelerate" if facet == "caption" else mod,
               "dep_ok": dep,
               "model_ok": m, "enabled": bool(enabled), "download": dl,
               "state": state, "model_name": cat.get("name"),
               "source": cat.get("source"), "url": cat.get("url"),
               "size_mb": cat.get("size_mb"), "kind": cat.get("kind"),
               "variant_id": None, "has_variants": ckey in VARIANTS}
        row["install"] = facet or ("sklearn" if ckey == "sklearn" else None)
        # Reflect the selected variant's name + size in the catalog row.
        if con is not None and ckey in VARIANTS:
            sv = selected_variant(con, ckey)
            if sv:
                row["model_name"] = sv.get("label") or row["model_name"]
                row["size_mb"] = sv.get("size_mb", row["size_mb"])
                row["variant_id"] = sv["id"]
                if sv.get("repo"):
                    row["url"] = f"https://huggingface.co/{sv['repo']}"
                elif sv.get("model_id"):
                    row["url"] = f"https://huggingface.co/{sv['model_id']}"
        if dl and con is not None:
            row["dir"] = str(active_model_dir(con, dl))
        out.append(row)
    return out


def get_engine_config(con=None) -> dict:
    """Merge auto-detect with the user's persisted overrides (settings 'tier' and
    'provider'). Returns the resolved tier, preset, and provider order."""
    hw = detect_hardware()
    tier = recommend_tier(hw["vram_gb"], hw["has_gpu"])
    tier_source = "auto"
    prefer = None
    if con is not None:
        from . import db
        override = db.get_setting(con, "tier")
        if override in PRESETS:
            tier, tier_source = override, "override"
        prefer = db.get_setting(con, "provider")
    providers = resolve_onnx_providers(hw["onnx_providers"], prefer)
    return {
        "hardware": hw,
        "tier": tier,
        "tier_source": tier_source,
        "preset": PRESETS[tier],
        "onnx_providers": providers,
        "torch_device": hw["torch_device"],
    }
