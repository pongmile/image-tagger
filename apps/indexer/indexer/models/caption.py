"""Image captioning facet — spec §11 (M8).

Generates a free-text, natural-language caption per image ("a girl on a beach at
sunset") that feeds FTS search and doubles as accessibility text. Runs entirely
on-device; nothing leaves the machine.

Per §11: the app injects **no** content filter of its own — captions should
describe what is actually in the image, including adult/explicit material the
user already owns; coverage is a function of whichever model the user loads
(swappable per library). The engine layer here is neutral: it just runs the
chosen model and stores its output.

  ── Hard line (§11), non-negotiable, overrides everything above ──
  This facet must never be built, tuned, or used to produce sexual or sexualized
  descriptions of minors, or of characters depicted as minors. No model, prompt,
  or vocabulary in this project may target that. Nothing in this module does, and
  nothing added to it may. ────────────────────────────────────────────────────

Pluggable behind `CaptionEngine`: a real BLIP backend (transformers), a
deterministic Fake for tests/CI, and a Null fallback.
"""
from __future__ import annotations


class CaptionEngine:
    name = "base"

    def caption(self, path: str) -> str:
        raise NotImplementedError


class NullCaptionEngine(CaptionEngine):
    name = "null"

    def caption(self, path):
        return ""


class BlipCaptionEngine(CaptionEngine):
    """BLIP conditional generation via transformers (CPU or GPU). Weights
    download once into cache_dir. Model id is swappable per library (§11)."""
    name = "blip"

    def __init__(self, model_id="Salesforce/blip-image-captioning-base",
                 cache_dir=None, device="cpu", max_new_tokens=30):
        from transformers import BlipProcessor, BlipForConditionalGeneration
        import torch
        from .. import engine as _engine
        self._torch = torch
        self.device = device
        self.max_new_tokens = max_new_tokens
        self.processor = BlipProcessor.from_pretrained(model_id, cache_dir=cache_dir)
        # fp16 on GPU, fp32 on CPU. BLIP was loading at fp32 everywhere, which
        # on a GPU is roughly twice the VRAM and about half the throughput for
        # a captioner whose output is a short sentence -- the precision is not
        # doing any visible work there. CPU stays fp32: half-precision matmuls
        # are emulated there and would be slower, not faster.
        dtype = torch.float16 if str(device).startswith("cuda") else torch.float32
        self.model = BlipForConditionalGeneration.from_pretrained(
            model_id, cache_dir=cache_dir, torch_dtype=dtype).eval().to(device)
        self._dtype = dtype
        _engine.note_torch_device(device)

    def caption(self, path):
        from ..imgio import open_oriented
        img = open_oriented(path).convert("RGB")
        inputs = self.processor(img, return_tensors="pt").to(self.device)
        # Only the pixel tensor follows the model's dtype; token ids must stay
        # integral, so a blanket .to(dtype) on the whole batch would break them.
        if "pixel_values" in inputs:
            inputs["pixel_values"] = inputs["pixel_values"].to(self._dtype)
        with self._torch.no_grad():
            out = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens)
        return self.processor.decode(out[0], skip_special_tokens=True).strip()


class JoyCaptionEngine(CaptionEngine):
    """JoyCaption (github.com/fpgaminer/joycaption) via transformers — an open
    LLaVA-architecture VLM captioner (a Llama backbone over a vision encoder)
    trained specifically to not refuse or sanitize adult content the way BLIP
    and most hosted captioners do, and to write long natural-language
    descriptions rather than short generic ones. Bound by the same §11 hard
    line as every other facet in this module: never sexualized minors.

    Much heavier than BLIP: ~17GB VRAM at native bf16, or roughly a third of
    that 4-bit-quantized (load_in_4bit=True, via bitsandbytes) at some quality
    cost. There is no usable CPU path — generation from an ~8B-parameter model
    is impractically slow there (single-digit minutes per image), so this
    engine refuses to load on CPU rather than silently hanging every file for
    minutes; pick BLIP for CPU-only machines.
    """
    name = "joycaption"

    SYSTEM_PROMPT = "You are a helpful image captioner."
    USER_PROMPT = "Write a long descriptive caption for this image in a formal tone."

    def __init__(self, model_id="fancyfeast/llama-joycaption-beta-one-hf-llava",
                 cache_dir=None, device="cpu", max_new_tokens=512,
                 load_in_4bit=False):
        if not device.startswith("cuda"):
            raise RuntimeError(
                "JoyCaption requires a CUDA GPU (~17GB VRAM, or ~6GB with "
                "4-bit quantization) — it is far too slow on CPU. Use a BLIP "
                "variant instead on CPU-only machines.")
        from transformers import AutoProcessor, LlavaForConditionalGeneration
        import torch
        from .. import engine as _engine
        _engine.note_torch_device(device)
        self._torch = torch
        self.device = device
        self.max_new_tokens = max_new_tokens
        self.processor = AutoProcessor.from_pretrained(model_id, cache_dir=cache_dir)
        kwargs: dict = {"cache_dir": cache_dir, "device_map": 0}
        if load_in_4bit:
            from transformers import BitsAndBytesConfig
            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_quant_type="nf4")
        else:
            kwargs["torch_dtype"] = torch.bfloat16
        self.model = LlavaForConditionalGeneration.from_pretrained(model_id, **kwargs)
        self.model.eval()

    def caption(self, path):
        from ..imgio import open_oriented
        img = open_oriented(path).convert("RGB")
        convo = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": self.USER_PROMPT},
        ]
        convo_string = self.processor.apply_chat_template(
            convo, tokenize=False, add_generation_prompt=True)
        inputs = self.processor(
            text=[convo_string], images=[img], return_tensors="pt").to(self.device)
        if "pixel_values" in inputs:
            inputs["pixel_values"] = inputs["pixel_values"].to(self._torch.bfloat16)
        with self._torch.no_grad():
            out = self.model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=True,
                suppress_tokens=None,
                use_cache=True,
                temperature=0.6,
                top_p=0.9,
            )
        text = self.processor.tokenizer.decode(
            out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True,
            clean_up_tokenization_spaces=False)
        return text.strip()


class FakeCaptionEngine(CaptionEngine):
    """Deterministic engine for tests: returns a caption built from the filename
    (or a provided map), so the pipeline is verifiable without the ~1GB weights."""
    name = "fake"

    def __init__(self, mapping: dict[str, str] | None = None, template="a photo of {}"):
        self.mapping = mapping or {}
        self.template = template

    def caption(self, path):
        import os
        base = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
        return self.mapping.get(os.path.basename(path), self.template.format(base))


_ENGINE: CaptionEngine | None = None
_ENGINE_KEY = None
_LAST_ERROR: str | None = None


def get_engine(model_dir=None, name="blip", model_id=None,
               device="cpu", load_in_4bit=False) -> CaptionEngine:
    global _ENGINE, _ENGINE_KEY, _LAST_ERROR
    if _ENGINE_KEY == "manual":
        return _ENGINE
    key = (str(model_dir) if model_dir else None, name, model_id, device, load_in_4bit)
    if (_ENGINE is not None and not isinstance(_ENGINE, NullCaptionEngine)
            and _ENGINE_KEY == key):
        return _ENGINE
    if name in (None, "null"):
        _ENGINE = NullCaptionEngine()
        _ENGINE_KEY = key
        return _ENGINE
    try:
        kw = {"cache_dir": model_dir, "device": device}
        if model_id:
            kw["model_id"] = model_id
        if name == "joycaption":
            _ENGINE = JoyCaptionEngine(load_in_4bit=load_in_4bit, **kw)
        else:
            _ENGINE = BlipCaptionEngine(**kw)
        _LAST_ERROR = None
    except Exception as exc:
        _LAST_ERROR = repr(exc)
        _ENGINE = NullCaptionEngine()
    _ENGINE_KEY = key
    return _ENGINE


def last_error() -> str | None:
    return _LAST_ERROR


def set_engine(engine) -> None:
    global _ENGINE, _ENGINE_KEY
    _ENGINE = engine
    _ENGINE_KEY = "manual"
