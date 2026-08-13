# Changelog

## 0.3.0

- Execution providers: added OpenVINO support for Intel NPUs ("AI Boost") and DirectML auto-detection for other GPUs (AMD, Intel Arc/integrated, non-CUDA NVIDIA), broadening hardware acceleration beyond CUDA-only
- Models: `insightface` upgraded to 1.0.1, fixing Windows installs that previously failed without Microsoft C++ Build Tools
- Models: hardware/dependency detection now runs in a subprocess, preventing Windows DLL-lock errors when installing or switching ONNX Runtime providers
- Models: fixed a false "ready" status caused by an incomplete dependency install being misreported as installed
- Lightbox: fixed oversized images pinning to the top-left instead of being centered

## 0.2.2

- Model manager: variant dropdown no longer truncates long labels, and its options are no longer washed-out/unreadable in dark mode
- Lightbox: portrait images no longer render as a zoomed-in center crop
- Lightbox: zoomed image quality no longer degrades relative to the source at high zoom

## 0.1.0

- Local-first image indexing and SQLite FTS5 search
- Angular/Electron desktop UI with list and thumbnail views
- OCR, WD14, CLIP semantic search, InsightFace, captioning and learned tags
- Windows x64 NSIS installer and portable ZIP packaging
- Bundled Python runtime, sample images, CI, release automation and security hardening
