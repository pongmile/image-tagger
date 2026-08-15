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


_HARDWARE_CACHE: dict | None = None


def detect_hardware() -> dict:
    """Best-effort probe of GPU/VRAM + available onnxruntime providers. Never
    raises — missing torch/onnxruntime just yields the CPU answer.

    Cached for the daemon's lifetime after the first call. This used to run
    fresh on every call, and get_engine_config() -- which calls it -- runs on
    every single indexing job (worker.py, once per file: the combined
    infer() job and each narrow _run_caption/_run_clip job separately). Each
    call spawns two throwaway subprocesses to keep torch/onnxruntime out of
    this long-lived process (see _probe_python's docstring); measured directly
    against the packaged interpreter, the torch/CUDA one alone costs ~3.2s,
    almost entirely torch's own cold-import time, repeated per job for
    hardware that cannot change mid-session -- indexing was spending more
    wall-clock time re-discovering the GPU than any model spent using it,
    which is why Task Manager showed near-idle CPU/GPU/disk even while the
    queue was draining: the "work" was mostly Python interpreter start-up.
    Safe to cache indefinitely: a facet engine that has already imported
    torch/onnxruntime in *this* process (wd14.py/caption.py/clip.py's own
    engine caches) cannot pick up a different build without a process
    restart anyway, so nothing here can go stale mid-session that wasn't
    already effectively fixed for the session the moment it first loaded.
    """
    global _HARDWARE_CACHE
    if _HARDWARE_CACHE is not None:
        return _HARDWARE_CACHE
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
    _HARDWARE_CACHE = {
        "has_gpu": has_gpu,
        "vram_gb": vram_gb,
        "gpu_name": gpu_name,
        "torch_cuda": torch_cuda,
        "onnx_providers": onnx_providers,
        "torch_device": torch_device,
    }
    return _HARDWARE_CACHE


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


_DLL_DIRS_ADDED = False


def ensure_gpu_libs(providers: list[str] | None = None) -> None:
    """Make the pip-installed NVIDIA CUDA/cuDNN DLLs loadable by onnxruntime.

    onnxruntime-gpu's ``onnxruntime_providers_cuda.dll`` links against
    ``cublasLt64_12.dll``/``cudnn64_9.dll``, which ship inside the separate
    ``nvidia-*-cu12`` wheels under ``nvidia/<lib>/bin/``. Nothing puts those
    directories on the DLL search path on their own. Torch does it as a side
    effect of being imported -- which is why this bug hid for so long: with
    CLIP or captioning enabled, ``import torch`` ran during daemon preload and
    onnxruntime inherited a working search path by luck. Turn both of those
    facets off, leaving WD14/OCR, and the provider silently fails to load.

    "Silently" is the whole problem. onnxruntime does not raise; it logs to
    stderr and hands back a session bound to CPUExecutionProvider, while
    ``get_available_providers()`` -- which is what the Models screen reports
    from -- keeps listing CUDA, because that only ever meant "this build was
    compiled with CUDA support", never "CUDA loaded". The app therefore
    claimed GPU while measuring, on this machine's WD14 EVA02 model, 0.84s
    per image against 0.041s once the DLLs resolve: a 20x slowdown that
    reported itself as working correctly.

    Registering the directories is necessary but *not* sufficient, which is
    the first trap here: ``os.add_dll_directory`` only affects loads that go
    through ``LoadLibraryEx`` with the search-path flags, and onnxruntime's
    provider bridge does not, so directory registration alone leaves the
    failure exactly as it was (measured: still CPU, still 0.84s/image). The
    DLLs have to already be resident in the process; once they are, the
    provider bridge resolves them from the loaded-module list without
    searching at all.

    The second trap is *which copy* becomes resident. Torch ships its own
    ``torch/lib/{cublas64_12,cudnn64_9,...}.dll`` — the same file names as the
    standalone ``nvidia-*-cu12`` wheels, at whatever versions that torch build
    was compiled against. Windows resolves a DLL name to the copy already
    loaded, so preloading the wheel copies first silently rebinds torch onto
    them, and a later ``import torch`` dies with ``OSError(22, 'The specified
    procedure could not be found.', None, 127)`` — a missing export, because
    the versions do not match. Doing that broke captioning outright on a
    machine where it had been working.

    So: when torch is installed, torch owns the CUDA runtime. Importing it is
    both sufficient (its DLL set satisfies the same names onnxruntime needs)
    and necessary (nothing else may load a competing copy first). Only when
    torch is absent or CPU-only — a library running WD14/OCR without ever
    installing the CLIP/captioning dependencies, which is exactly the
    configuration where this bug bit and had no other workaround — do we load
    the standalone wheels ourselves.

    Only runs when CUDA is actually on the table, so a CPU-only or DirectML
    machine pays nothing and loads no CUDA runtime it will never use.
    Idempotent and never fatal.
    """
    global _DLL_DIRS_ADDED
    if _DLL_DIRS_ADDED or not any("CUDA" in p for p in (providers or ["CUDA"])):
        return
    _DLL_DIRS_ADDED = True
    import os
    if not hasattr(os, "add_dll_directory"):   # non-Windows: rpath handles it
        return
    try:
        import torch
        if torch.cuda.is_available():
            # Torch has loaded its own self-consistent CUDA/cuDNN set; that is
            # all onnxruntime needs, and anything we added on top could only
            # conflict with it.
            return
    except Exception:
        pass
    import ctypes
    from pathlib import Path
    from . import config
    roots = [config.RUNTIME_PACKAGES_DIR]
    try:
        import site
        roots.extend(Path(p) for p in site.getsitepackages())
    except Exception:
        pass
    libs: list[Path] = []
    for root in roots:
        try:
            for bindir in sorted((Path(root) / "nvidia").glob("*/bin")):
                dlls = sorted(bindir.glob("*.dll"))
                if dlls:
                    os.add_dll_directory(str(bindir))
                    libs.extend(dlls)
        except Exception:
            continue
    # Two passes: these libraries depend on each other (cudnn's kernels on
    # cublas, cublas on cublasLt), and a single alphabetical pass would fail
    # whichever ones happen to be reached before their dependency. Anything
    # still failing on the second pass is genuinely unusable — leave it to
    # onnxruntime to fall back rather than raising here.
    pending = libs
    for _ in range(2):
        retry = []
        for lib in pending:
            try:
                ctypes.CDLL(str(lib))
            except OSError:
                retry.append(lib)
        if not retry:
            break
        pending = retry


# What each runtime *actually* bound to, recorded at session/model creation
# rather than inferred from what was requested. See ensure_gpu_libs() for why
# the requested list is not evidence of anything.
_ACTIVE_DEVICES: dict[str, str] = {}
_EP_LABELS = {
    "CUDAExecutionProvider": "CUDA",
    "TensorrtExecutionProvider": "TensorRT",
    "DmlExecutionProvider": "DirectML",
    "OpenVINOExecutionProvider": "OpenVINO",
    "CPUExecutionProvider": "CPU",
}


def note_onnx_session(session) -> None:
    """Record the provider onnxruntime really bound for a freshly-built session."""
    try:
        providers = list(session.get_providers())
    except Exception:
        return
    accelerated = [p for p in providers if p != "CPUExecutionProvider"]
    _ACTIVE_DEVICES["onnx"] = (_EP_LABELS.get(accelerated[0], accelerated[0])
                               if accelerated else "CPU")


def note_torch_device(device: str) -> None:
    """Record the device a torch-backed engine (CLIP, captioning) loaded onto."""
    text = str(device or "cpu")
    _ACTIVE_DEVICES["torch"] = "CUDA" if text.startswith("cuda") else text.upper()


def active_device_label() -> str | None:
    """Short "what is doing the work right now" string for the UI (§12).

    Reports the two runtimes separately because they genuinely can differ --
    and did, on the machine this was diagnosed on: torch on CUDA, onnxruntime
    silently on CPU. A single "GPU" badge would have gone on hiding exactly
    the failure the user was asking about. Returns None until a model has
    actually loaded, so the UI shows nothing rather than a guess.
    """
    parts = []
    if (onnx := _ACTIVE_DEVICES.get("onnx")):
        parts.append(f"tagging/OCR {onnx}")
    if (torch_device := _ACTIVE_DEVICES.get("torch")):
        parts.append(f"CLIP/caption {torch_device}")
    return " · ".join(parts) or None


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


def _dep_importable(mod: str) -> bool:
    """A namespace stub is not an installed dependency.

    Interrupted ``pip --target`` runs (daemon._dependency_install_worker)
    can leave an empty directory behind; a bare find_spec() still reports
    that as a namespace package, which would make the Models screen show
    "ready" for a dependency that never actually finished installing.
    Mirrors daemon.py's own `installed()` guard for the same reason.
    """
    import importlib.util
    from pathlib import Path
    spec = importlib.util.find_spec(mod)
    return bool(spec and spec.origin and spec.origin != "namespace"
                and Path(spec.origin).is_file())


def facet_model_ready(con, facet: str) -> tuple[bool, str]:
    """Cheap "is this facet's dependency + a previously-downloaded model
    actually usable right now" check (§11/§5) — independent of the facet's
    ``<facet>_enabled`` toggle (the Models screen already gates *that*
    checkbox on readiness, but the two can still drift apart: the toggle
    persists in settings even if the model directory/dependency is later
    removed, corrupted, or the library is copied to a machine that never
    installed it). Returns ``(ready, reason)``; `reason` is a short,
    user-facing explanation when not ready.

    Only "caption" and "faces" call this today — both are heavyweight,
    opt-in facets (§11 captioning must never be *required* for the app to
    work; §5 says the same for real-face recognition) where a missing model
    is an expected, common state, not a bug to raise an error for.
    """
    import importlib
    importlib.invalidate_caches()
    if facet == "caption":
        if not (_dep_importable("transformers") and _dep_importable("accelerate")):
            return False, "captioning dependency (transformers + accelerate) is not installed"
        if not model_ready_marker(con, "caption").exists():
            return False, "no caption model has been downloaded yet"
        # Variant-specific runtime requirements. Reporting a bare "ready" for a
        # variant that cannot actually load is worse than reporting "not
        # installed": the facet passes this gate, then blows up at load time
        # with a raw exception, and because the previous model's caption is
        # still on the row it looks to the user as though the app silently
        # ignored their model choice and kept using the old one.
        variant = selected_variant(con, "caption") or {}
        if variant.get("load_in_4bit") and not _dep_importable("bitsandbytes"):
            return False, (f"{variant.get('label', variant.get('id'))} needs the "
                           "bitsandbytes 4-bit runtime, which is not installed")
        if variant.get("engine") == "joycaption" and \
                not get_engine_config(con)["torch_device"].startswith("cuda"):
            return False, (f"{variant.get('label', variant.get('id'))} needs a CUDA GPU "
                           "— pick a BLIP variant on CPU-only machines")
        return True, ""
    if facet == "faces":
        if not _dep_importable("insightface"):
            return False, "InsightFace dependency is not installed"
        if not model_ready_marker(con, "insightface").exists():
            return False, "no face-recognition model has been downloaded yet"
        return True, ""
    raise ValueError(f"no readiness check for facet: {facet}")


def facet_readiness(con=None) -> list[dict]:
    """Per-facet readiness + catalog info for the model-download manager (§12):
    dependency importable? model files present? enabled? which model, from where,
    how big, and where it lands on disk."""
    import importlib
    from pathlib import Path
    from . import config, db as _db
    importlib.invalidate_caches()

    def has(mod):
        return _dep_importable(mod)

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
