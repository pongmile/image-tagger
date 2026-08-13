# Changelog

## 0.6.0

- Captioning (BLIP/JoyCaption) and Real faces (InsightFace) no longer block or error out the rest of a file's indexing when their model isn't installed yet — they're skipped gracefully (with a log line explaining why) instead of failing the whole file; a genuine engine load failure (as opposed to "not installed") still surfaces as a real error, unchanged
- "↻ re-Description" now returns a clear message instead of silently doing nothing when the caption model isn't actually installed yet, and works correctly once the model is installed and the facet enabled

## 0.5.0

- Preview: selecting more than one result now shows a plain thumbnail grid with filenames in the right-hand pane instead of a single file's tags/caption/metadata, which no longer applies to a multi-selection
- Lightbox: added previous/next navigation (⟨/⟩ buttons and the ArrowLeft/ArrowRight keys) to browse through the current search results without closing and reopening the image

## 0.4.0

- Settings: added a "Check for updates" section that compares the running build against this repo's latest GitHub Release and links to it — not a silent auto-updater, so it can't introduce the install/update bugs that come with one
- Learned tags: the "face" embedding space (for recognizing a specific real person across different photos) is now reachable from the "tag & teach" panel, and newly indexed faces are scored against face-space learned tags immediately instead of only on the next manual refresh
- Fixed the thumbnail grid's selection highlight being misaligned with the thumbnail image

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
