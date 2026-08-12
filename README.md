# Image Tagger

Image Tagger is a local-first Windows desktop application for indexing, tagging, and searching image libraries. It searches filenames, folders, tags, people, OCR text, metadata, captions, and CLIP embeddings without uploading images.

## Download and install

Download the latest Windows x64 release from [GitHub Releases](https://github.com/pongmile/image-tagger/releases/latest):

- `Image-Tagger-<version>-win-x64.exe`: installer for most users.
- `Image-Tagger-<version>-win-x64.zip`: portable build; extract the entire ZIP and run `Image Tagger.exe`.
- `SHA256SUMS.txt`: checksums for both downloads.

The installer and portable ZIP include Electron, Python 3.12, OCR, ONNX Runtime, and sqlite-vec. End users do not need Node.js, Python, CUDA Toolkit, or Visual Studio. Large optional AI models are installed from the **Models** page because including every model would add tens of gigabytes to the base download.

Unsigned development releases may trigger Windows SmartScreen. Verify the SHA-256 checksum and download only from this repository; do not disable antivirus protection.

## System requirements

| Profile | CPU and RAM | Free storage | GPU | Intended use |
|---|---|---|---|---|
| Minimum | x64 4-core CPU, 8 GB RAM | 2 GB plus thumbnails/index | Not required | Search, manual tags, metadata, OCR |
| Recommended | x64 6-core CPU, 16 GB RAM | 10 GB | NVIDIA 6 GB VRAM or CPU | WD14, CLIP, face detection |
| Heavy models | x64 8+ cores, 32 GB RAM | 25 GB+ | NVIDIA 12 GB+ VRAM | Accurate/high-tier models and captioning |

- Windows 10 22H2 or Windows 11, 64-bit.
- 1280x720 or larger display.
- Internet access is required only to download optional models.
- Model storage varies by selection: WD14 0.3-1.4 GB, CLIP 0.34-3.9 GB, InsightFace 0.1-0.33 GB, BLIP 1-1.9 GB, and JoyCaption about 16 GB.

JoyCaption is an explicit opt-in model and is never selected automatically:

- **JoyCaption 4-bit**: NVIDIA Pascal/GTX 10-series or newer, approximately 6 GB VRAM, 16 GB+ RAM, and 20 GB free storage for weights/cache.
- **JoyCaption full**: BF16-capable NVIDIA GPU, approximately 17 GB VRAM, 32 GB+ RAM, and 20 GB free storage.
- CPU-only systems should use BLIP; the app rejects JoyCaption on CPU instead of allowing an unusably slow load.

## Quick start

1. Open **Sources**, select **Add folder**, and choose an image folder.
2. Add exclude folders or patterns if required.
3. Select **Rescan** and wait until indexing becomes `idle`.
4. Search for queries such as `beach`, `character:"hatsune miku"`, or `folder:travel !draft`.
5. Select an image to inspect its preview, OCR, metadata, faces, caption, and tags.
6. Click a preview to open the full-resolution viewer. Use the mouse wheel or `+`/`-` to zoom from 100% to 800%, drag to pan, double-click to reset, and press `Esc` to close.
7. Open **Models** to enable WD14, semantic search, face recognition, BLIP, or JoyCaption.

Redistributable test images are included in [`samples/`](samples/).

### Search syntax

| Query | Meaning |
|---|---|
| `cat dog` | Both terms must match |
| `cat \| dog` | Either term may match |
| `!draft` or `-draft` | Exclude matches |
| `"blue sky"` | Exact phrase |
| `character:miku` | Match a category/tag |
| `person:alice` | Match a named face cluster |
| `folder:D:/Photos` | Restrict the folder |
| `size:>10mb` | Restrict file size |
| `*.png` | Wildcard |

See the complete [user guide](docs/USER_GUIDE.md).

## Features

- SQLite FTS5 trigram search with AND, OR, NOT, grouping, phrases, wildcard, regex, size, category, folder, and person filters.
- List, small-thumbnail, and large-thumbnail virtualized views.
- Full-resolution streamed preview for large files, with 100-800% zoom and pan.
- Manual/bulk tags, custom categories, tag rename/merge, and few-shot learned tags.
- English/Thai OCR, EXIF/PNG metadata, and Stable Diffusion parameters.
- WD14 anime tagging; CLIP scene/clothing/pose tags and semantic search.
- InsightFace clustering/naming; BLIP and opt-in JoyCaption descriptions.
- Background indexing, filesystem watching, retry, pause/manual mode, and crash recovery.
- Local data in `%USERPROFILE%\.image-tagger` by default; no telemetry or image upload.

## Build from source

Requirements:

- Node.js 24 LTS and npm 10+.
- Python 3.12 (3.10-3.12 supported for development).
- Git.
- `uv` when building the bundled Python runtime.
- Visual Studio Build Tools with “Desktop development with C++” only if a native dependency has no prebuilt binary.

```powershell
git clone https://github.com/pongmile/image-tagger.git
cd image-tagger
npm run setup
npm test
npm run dev
```

Build and verify distributable files:

```powershell
npm run test:ui
npm run bench
npm run dist:win
```

`npm run dist:win` creates and verifies the NSIS installer and portable ZIP under `apps/desktop/dist/`. See [development](docs/DEVELOPMENT.md) and [release](docs/RELEASING.md) documentation.

## Repository layout

```text
apps/desktop/             Electron main/preload and Angular renderer
apps/indexer/             Python indexing and local AI pipeline
packages/db/              Canonical SQLite schema and migrations
samples/                  Redistributable example images
scripts/                  Setup, tests, benchmarks, and packaging
.github/workflows/        CI and tagged Windows release
```

## Limitations

- Prebuilt releases are currently Windows x64 only. macOS/Linux require separate source builds and OS-specific bundled runtimes.
- Video files can be browsed/searched but do not run image AI tagging or captioning.
- Optional model licenses are controlled by their upstream projects.
- AI predictions are probabilistic; use confidence filters and confirm/reject feedback.
- Builds are not Authenticode-signed unless release signing secrets are configured.

## Privacy and license

Indexing and inference run locally. The current code has no telemetry or image-upload path. See [SECURITY.md](SECURITY.md) for vulnerability reporting.

Source code and repository sample images are distributed under the [MIT License](LICENSE).
