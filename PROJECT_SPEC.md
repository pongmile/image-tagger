# Local Image Tagger & Search — Project Spec

> **TL;DR:** A local desktop application that indexes images and applies AI tags
> for real faces, anime characters, poses, scenes, clothing, and other facets.
> Users can edit tags and create categories. Indexing may run automatically or
> manually, but **search must remain Everything-fast**. All data stays in the
> application database; original XMP is not modified and images never leave the machine.

Language note: this specification is written entirely in English so it remains portable across development tools and contributors.

---

## 1. Goals

- Index a local image library and auto-tag each image across multiple facets.
- Tag facets required:
  - **Real human faces** → cluster identical faces into a `person`, user names them once, system remembers.
  - **Anime/illustration** → character name, series/franchise, clothing type, general booru tags.
  - **Pose** → coarse only: `standing`, `sitting`, `running`, etc. (no skeleton keypoints).
  - **Scene / place**, **clothing type**, **general characteristics** → open-vocabulary via CLIP.
  - **Text visible in the image** (signs, manga speech bubbles, screenshots, memes) → OCR, in Thai and English.
- **Manual tagging**: user can add/remove tags on any file by hand.
- **Path & metadata as signal**: filename, folder path, and embedded metadata (EXIF, PNG text chunks / Stable Diffusion generation params, XMP if present) are extracted at ingest and become both searchable text and a tag source.
- **Custom categories**: user can create their own tag categories at runtime.
- **Indexing**: runs in `auto` (filesystem watcher) or `manual` (user-triggered rescan) mode. Indexing may be slow; that is acceptable.
- **Search MUST be fast** — sub-100ms, as-you-type, "Everything-like" feel, even at hundreds of thousands of files.
- **Local-only**: no image, embedding, or tag leaves the machine.

## 2. Non-Goals

- No cloud sync, no external calls at runtime. Everything stays on the machine.
- No XMP / sidecar / EXIF writeback (v1). All data lives in the app DB only.
- No skeleton pose estimation (DWPose/OpenPose) in v1.
- No image editing/generation. Read-only over the user's files.
- No multi-user / sharing.

## 3. Explicit Assumptions

> Correct invalid assumptions before implementation begins.

- Library size ≤ ~500k images → **SQLite + `sqlite-vec`** is sufficient; no Postgres/pgvector.
- Runs on a machine with an NVIDIA GPU (e.g. RTX 5070 Ti) for inference; must also degrade to CPU (onnxruntime CPU) if no GPU.
- Desktop app (Windows primary; keep cross-platform doors open).
- Single library per app instance in v1 (multiple root folders allowed within it).
- File formats v1: jpg, jpeg, png, webp, gif, bmp, tiff. (Video out of scope v1.)

## 4. Architecture

**Core principle — split the two paths:**

```
                 ┌──────────────────────────────────────────┐
                 │            Electron app                    │
                 │                                            │
 user  ── UI ──► │  Angular renderer  ◄── IPC ──►  Node main  │
                 │       (search UI)              (better-    │
                 │                                 sqlite3)   │
                 └───────────────────────────┬────────────────┘
                                              │ reads (WAL, fast)
                                     ┌────────▼────────┐
                                     │  SQLite DB      │  ◄── the shared contract
                                     │  (library.db)   │
                                     └────────▲────────┘
                                              │ writes (batched)
                 ┌────────────────────────────┴───────────────┐
                 │        Python indexing/inference service     │
                 │  watchdog → job queue → routers → models     │
                 │  WD14 · InsightFace · CLIP  (onnxruntime)    │
                 └──────────────────────────────────────────────┘
```

- **Search path never touches Python.** Node main process reads SQLite directly with `better-sqlite3` (synchronous, in-process, microsecond-level). This is what buys the Everything-like latency.
- **Index path is fully async/background** in the Python service. It only ever *writes* to the DB.
- SQLite in **WAL mode** so background writes don't block foreground reads.
- The two processes communicate **only through the DB + a tiny control channel** (a `jobs` table or a local socket) — no heavy IPC, no request/response on the hot path.

### 4.1 Why this stack

- Angular + TypeScript + Node → matches existing skillset; Electron gives real filesystem + child-process access.
- Python for models → WD14, InsightFace, CLIP all ship as onnxruntime / torch models with mature Python bindings; no good native TS equivalents.
- SQLite → single file, zero-admin, FTS5 built in, `sqlite-vec` for optional semantic search. `better-sqlite3` on the Node side is the fastest embedded read path.
- **Alternative considered:** Tauri (Rust shell) — lighter binaries, but backend would be Rust, off-stack. Electron chosen for stack fit; revisit if binary size matters.

> ⚠️ **Verify at implementation time:** exact APIs for `onnxruntime`, `insightface` (buffalo_l pack), `open_clip`/`transformers` CLIP, `sqlite-vec`, and `better-sqlite3` against their current docs before wiring. Do not assume method signatures from memory.

## 5. Model Routing

Router runs a cheap check first, then dispatches:

1. **Ingest & hash** — compute sha256 (dedup exact) + perceptual hash (near-dup). Read dimensions. Split `path` into `filename` + `folder`. Extract embedded metadata (EXIF via Pillow/piexif, PNG text chunks incl. Stable Diffusion `parameters`, XMP if present) → `file_metadata`. Derive tags from path/metadata where meaningful (see §5.1).
2. **Kind router** — run WD14; use its `rating`/general-tag signal (plus a light real-vs-illustration heuristic) to label `image_kind ∈ {anime, real, other}`.
3. Dispatch by kind:

| Facet | anime/illustration | real photo |
|---|---|---|
| Character / person | WD14 character tags | InsightFace → face embedding → cluster → `person` |
| Series / franchise | WD14 | — |
| General tags | WD14 (general, threshold-tunable) | CLIP zero-shot over a curated vocabulary |
| Clothing type | WD14 + CLIP | CLIP zero-shot |
| Scene / place | CLIP zero-shot | CLIP zero-shot |
| Pose (coarse) | WD14 (`standing`/`sitting`/etc.) | CLIP zero-shot over pose vocab |
| Text in image | PaddleOCR (both) | PaddleOCR (both) |

- **CLIP zero-shot** handles scene / clothing / "type" with an editable label vocabulary — open-vocab means new categories need no retraining.
- **WD14** thresholds configurable; separate `character_threshold` from general threshold.
- **InsightFace** produces face embeddings only; clustering (e.g. incremental/HDBSCAN-style) groups them; naming is a one-time manual step per cluster, then new faces auto-attach to the nearest named cluster above a similarity cutoff.

Every produced tag carries `source` (`wd14` | `clip` | `insightface` | `manual` | `path` | `metadata` | `learned`) and `confidence`, so manual tags can override and low-confidence auto tags can be filtered.

### 5.1 Path & metadata as tags/search

- **Filename & folder** are always searchable via FTS (`filename`, `folder` columns). Optionally auto-tag: e.g. a folder named `Hatsune Miku/` → a low-confidence `character:hatsune miku` candidate the user can confirm. Path-derived tags use `source='path'` and are kept separate so they never silently outrank AI or manual tags.
- **Embedded metadata** (`file_metadata`): selected keys are flattened into `meta_text` for FTS (so you can search `"DPM++ 2M"` or a camera model). High-value keys can also become structured tags with `source='metadata'` — notably Stable Diffusion PNG `parameters` (model/sampler/seed/LoRA), which are common in AI-art libraries.
- All of the above is **opt-in per source** in settings — some users want folder names as tags, others only as search text.

### 5.2 Engine & hardware tiers (selectable)

The app must run on anything from a CPU-only notebook to an RTX 5090. Provide a **tier selector** plus **auto-detect** that reads GPU + VRAM at first run (via NVML / torch) and recommends a tier; the user can override globally or per-task (e.g. big tagger + small captioner).

Two independent knobs:

- **Execution provider** (auto-detected, overridable): `CUDA` (NVIDIA) → `DirectML` (any GPU incl. AMD/Intel) → `CPU` (onnxruntime). DirectML is the compatibility fallback; CPU is the universal fallback.
- **Model preset** = per-task model variant + precision + batch size, keyed to a VRAM bucket.

| Tier | VRAM bucket | Example GPUs | Tagger (WD14) | CLIP | Caption VLM | Precision / batch |
|---|---|---|---|---|---|---|
| **Low** | CPU or 4–6GB | notebook iGPU, GTX 1050 Ti (4GB), 1660 Ti (6GB), RTX 3050 laptop | smallest variant | ViT-B/32 | off by default (small model optional) | int8 / batch 1–2, CPU ok |
| **Low-Mid** | 8GB | laptop dGPU, RTX 3050, 5050, 5060 | standard (ConvNeXt-class) | ViT-B/16 | small | fp16 / batch 4 |
| **Mid** | 8–12GB | RTX 5060 Ti (8/16GB), 5070 (12GB) | large (SwinV2/EVA02-class) | ViT-L/14 | mid | fp16 / batch 8 |
| **High** | 16GB+ | RTX 5080 (16GB), 5090 (32GB) | largest (EVA02-Large-class) | ViT-H / bigG | large (7B-class) | fp16 / batch 16+ |

Rules:

- **VRAM bucket, not GPU name, decides the preset.** Card names are illustrative; laptop parts carry different VRAM than desktop (e.g. 5090 laptop = 24GB, 5070 laptop = 8GB). Auto-detect keys off measured VRAM.
- Preset controls: which model file to download (§11), batch size, precision (fp16/int8), and input resolution. Bigger tier = better tags/captions, slower + more VRAM.
- **Per-task override:** each facet (tagger / clip / caption / face) can be pinned to a different variant, so a mid GPU can still run a big tagger by dropping caption to small or off.
- **Graceful downgrade:** on CUDA OOM, auto-retry the file at a smaller batch, then a smaller variant, and log it — never hard-fail the queue.
- Selected engine + presets live in app settings (per library), surfaced in a "Performance" settings pane with rough throughput estimate (img/s).

> Model variant names above are representative. Confirm exact available WD14 / CLIP / caption model IDs and their VRAM footprints against their sources at build time before wiring — do not hardcode from memory.

### 5.3 Learned tags (few-shot self-training)

Goal: when the base models don't know a label (a new anime character, a new VTuber, a niche outfit), the user tags ~5–10 examples by hand and the app learns to auto-apply that tag to similar images going forward.

**Approach: frozen-embedding few-shot — no model fine-tuning.** We do *not* retrain WD14/CLIP (heavy, GPU-bound, risks catastrophic forgetting, not incremental). Instead we reuse the embeddings already computed per image:

- **CLIP image embedding** (already stored in `file_vec`, §6) → space for character / series / concept / outfit learned tags.
- **InsightFace face embedding** (already in `faces.embedding`) → space for real-person learned tags. This is the same mechanism as face clustering (§5), generalized to arbitrary user labels.

**Flow:**

1. User applies a custom tag to N images (min configurable, default 5).
2. Build a **prototype** = mean of L2-normalized embeddings of those images (a centroid). Store per tag.
3. On index (or an on-demand "apply learned tags" rescan), score each image by cosine similarity to every prototype; `sim ≥ threshold` → apply tag with `source='learned'`, `confidence = sim`.
4. When examples grow (≥ ~20 incl. negatives), **upgrade** the centroid to a lightweight **linear head** (logistic regression / linear SVM via scikit-learn) trained on frozen embeddings — trains in milliseconds on CPU, sharper boundaries than a single centroid.

**Feedback loop (active learning):** learned tags render as *suggestions* (distinct color) until confirmed. Confirm → positive example; reject → negative example. Both feed back into the prototype/head, and the per-tag threshold auto-calibrates. This is how accuracy climbs from "5 examples, rough" to "reliable" over normal use.

**Why this fits:**

- Runs on **any tier including CPU** — scoring is a dot product; the linear head trains in ms. No GPU needed for the learning itself.
- **Incremental & instant** — a new tag is usable the moment 5 examples exist; no training job, no waiting.
- Reuses embeddings already on disk, so it costs almost nothing extra at index time.

**Limits (be honest in UI):** CLIP embeddings are strong on scenes/style but weaker at *fine-grained anime character identity* — two characters with the same hair color can collide. Mitigations: require more examples, add negatives, and use the linear head. For a stronger anime space, optionally tap the tagger's own penultimate feature vector as an alternative embedding (verify feasibility with the chosen WD14 model at build time). Real-person learning via the face space is far more reliable than character learning via CLIP.

## 6. Data Model (SQLite)

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Files -----------------------------------------------------------
CREATE TABLE files (
  id           INTEGER PRIMARY KEY,
  path         TEXT NOT NULL UNIQUE,
  filename     TEXT,             -- basename, extracted for search/tagging
  folder       TEXT,             -- parent dir path, extracted for search/tagging
  sha256       TEXT NOT NULL,
  phash        TEXT,
  mime         TEXT,
  width        INTEGER,
  height       INTEGER,
  size_bytes   INTEGER,
  mtime        INTEGER,          -- file modified time (epoch)
  image_kind   TEXT,             -- anime | real | other
  caption      TEXT,             -- natural-language caption (§10)
  ocr_text     TEXT,             -- concatenated OCR text (§10.1)
  index_status TEXT NOT NULL DEFAULT 'pending', -- pending|done|error
  indexed_at   INTEGER
);
CREATE INDEX idx_files_sha       ON files(sha256);
CREATE INDEX idx_files_status    ON files(index_status);
CREATE INDEX idx_files_kind      ON files(image_kind);
CREATE INDEX idx_files_folder    ON files(folder);

-- Embedded metadata (raw, one row per key) ------------------------
-- EXIF, PNG text chunks (incl. Stable Diffusion params), XMP, etc.
CREATE TABLE file_metadata (
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  key     TEXT NOT NULL,        -- e.g. exif:Make, png:parameters, xmp:Rating
  value   TEXT,
  PRIMARY KEY (file_id, key)
);
CREATE INDEX idx_meta_key ON file_metadata(key);

-- Categories (user-extensible) ------------------------------------
CREATE TABLE categories (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,   -- e.g. character, series, clothing, scene, pose, person, custom-*
  color      TEXT,
  is_builtin INTEGER NOT NULL DEFAULT 0
);

-- Tags ------------------------------------------------------------
CREATE TABLE tags (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  category_id INTEGER REFERENCES categories(id),
  UNIQUE(name, category_id)
);
CREATE INDEX idx_tags_cat ON tags(category_id);

-- File <-> Tag ----------------------------------------------------
CREATE TABLE file_tags (
  file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  tag_id     INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
  source     TEXT NOT NULL,        -- wd14|clip|insightface|manual|path|metadata|learned
  confidence REAL,                 -- null for manual
  PRIMARY KEY (file_id, tag_id)
);
CREATE INDEX idx_ft_tag  ON file_tags(tag_id);
CREATE INDEX idx_ft_file ON file_tags(file_id);

-- Faces / persons -------------------------------------------------
CREATE TABLE persons (
  id   INTEGER PRIMARY KEY,
  name TEXT               -- null = unnamed cluster
);
CREATE TABLE faces (
  id        INTEGER PRIMARY KEY,
  file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  person_id INTEGER REFERENCES persons(id),
  bbox      TEXT,          -- json [x,y,w,h]
  embedding BLOB           -- face vector
);
CREATE INDEX idx_faces_person ON faces(person_id);
CREATE INDEX idx_faces_file   ON faces(file_id);

-- Learned tags (few-shot self-training, §5.3) ---------------------
CREATE TABLE learned_tags (
  tag_id     INTEGER PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE,
  space      TEXT NOT NULL,        -- clip | face
  method     TEXT NOT NULL,        -- centroid | linear
  threshold  REAL NOT NULL,        -- cosine cutoff (auto-calibrated)
  n_pos      INTEGER NOT NULL DEFAULT 0,
  n_neg      INTEGER NOT NULL DEFAULT 0,
  prototype  BLOB,                 -- centroid vector (method=centroid)
  classifier BLOB,                 -- serialized linear head (method=linear)
  updated_at INTEGER
);

-- Positive/negative examples backing each learned tag -------------
CREATE TABLE tag_examples (
  tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  label   INTEGER NOT NULL,        -- +1 positive, -1 negative
  origin  TEXT NOT NULL,           -- manual | confirmed | rejected
  added_at INTEGER,
  PRIMARY KEY (tag_id, file_id)
);
CREATE INDEX idx_tagex_tag ON tag_examples(tag_id);

-- OCR text regions (one row per detected text block) --------------
-- ocr_text on `files` is the concatenated version for fast FTS;
-- this table keeps per-region detail for future highlight-in-preview.
CREATE TABLE ocr_regions (
  id      INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  text    TEXT NOT NULL,
  lang    TEXT,             -- th | en | ...
  bbox    TEXT,             -- json [x,y,w,h]
  confidence REAL
);
CREATE INDEX idx_ocr_file ON ocr_regions(file_id);

-- Full-text search (the fast path) --------------------------------
-- One row per file, refreshed whenever any source changes.
--   tags_text = space-joined tag names
--   meta_text = space-joined selected metadata values (SD params, camera, etc.)
CREATE VIRTUAL TABLE files_fts USING fts5(
  path,
  filename,
  folder,
  tags_text,
  meta_text,
  caption,
  ocr_text,
  content=''             -- contentless; we manage rows explicitly
);

-- Optional semantic search + required for learned tags (sqlite-vec)
-- Loaded as extension; holds one CLIP embedding per file.
-- Also the embedding space for §5.3 learned tags, so it's populated
-- whenever CLIP runs, even if semantic search UI is disabled.
-- CREATE VIRTUAL TABLE file_vec USING vec0(file_id INTEGER PRIMARY KEY, embedding FLOAT[512]);

-- Scan roots: included / excluded drives & folders ---------------
CREATE TABLE roots (
  id       INTEGER PRIMARY KEY,
  path     TEXT NOT NULL UNIQUE,   -- drive or folder, e.g. D:\ or /home/mile/pics
  mode     TEXT NOT NULL,          -- include | exclude
  recursive INTEGER NOT NULL DEFAULT 1,
  enabled  INTEGER NOT NULL DEFAULT 1,
  added_at INTEGER
);

-- Glob/substring patterns excluded anywhere under included roots --
CREATE TABLE exclude_rules (
  id      INTEGER PRIMARY KEY,
  pattern TEXT NOT NULL,           -- e.g. **/node_modules/**, *.tmp, **/.git/**
  enabled INTEGER NOT NULL DEFAULT 1
);

-- Indexing job queue (control channel) ----------------------------
CREATE TABLE jobs (
  id        INTEGER PRIMARY KEY,
  file_id   INTEGER REFERENCES files(id) ON DELETE CASCADE,
  kind      TEXT NOT NULL,   -- ingest|infer|reindex
  state     TEXT NOT NULL DEFAULT 'queued', -- queued|running|done|error
  error     TEXT,
  created_at INTEGER,
  updated_at INTEGER
);
CREATE INDEX idx_jobs_state ON jobs(state);
```

**Seed built-in categories:** `person`, `character`, `series`, `clothing`, `scene`, `pose`, `general`, `rating`, `path`, `metadata`. User-created categories are just extra rows with `is_builtin = 0`.

## 7. Indexing Pipeline

### 7.0 Scan scope (include / exclude)

- User manages a list of **roots** (`roots` table): drives or folders marked `include` or `exclude`, each optionally recursive, each toggleable without deletion.
- Plus a list of **exclude patterns** (`exclude_rules`): globs/substrings skipped anywhere (default seeds: `**/.git/**`, `**/node_modules/**`, the app's own thumbnail cache dir, `*.tmp`).
- **Resolution rule (most-specific wins):** a file is indexed iff its path is under some enabled `include` root AND not under a more-specific enabled `exclude` root AND matches no enabled exclude pattern. Excludes always beat includes at equal-or-deeper depth, so you can include `D:\Pictures` but exclude `D:\Pictures\WIP`.
- Changing roots/patterns triggers a diff: newly-included paths get enqueued; newly-excluded paths get their rows (and derived tags) removed from the library. Watchers attach only to enabled include roots.

- **Auto mode:** Python `watchdog` observers on each enabled include root (§7.0). On create/modify → check scope rules → upsert `files` row + enqueue `ingest` job. On delete → remove row (cascade). On move → update `path` (or drop if moved out of scope).
- **Manual mode:** UI "Rescan" button → Python walks the include roots, applies scope rules, diffs against `files` (by path + mtime + sha256), enqueues only new/changed files.
- **Worker:** pulls `jobs` (FIFO), runs ingest→route→infer, writes tags in a **single transaction per file**, then updates `files_fts.tags_text` for that file, sets `index_status='done'`. Batches inference (`batch_size` configurable) for GPU throughput.
- **Progress:** UI reads counts from `jobs`/`files.index_status` (`pending` vs `done`) to render a progress bar. Search stays fully responsive during indexing (WAL).
- **Idempotent & resumable:** re-running never duplicates; `error` files can be retried.

## 8. Search Design (the "Everything" bar)

- **Default fast path:** FTS5 `MATCH` over `files_fts(tags_text, path)` → returns `file_id`s → join `files`. Prepared statements via `better-sqlite3`, synchronous, in Node main. Debounced as-you-type (e.g. 60–120ms).
- **Structured filters:** category/tag filters use `file_tags` + `categories` indexes (e.g. `category:character = "hatsune miku" AND category:scene = "beach"`). Compose in SQL, not in JS.
- **Query syntax (v1):**
  - bare words → FTS match on tags + path + caption + OCR text (§10)
  - `cat:value` → filter by category (e.g. `character:miku`, `pose:sitting`)
  - `person:name` → join faces/persons
  - `folder:<path>` → restrict results to a folder subtree (prefix match on `folder`); `-folder:<path>` to exclude a subtree
  - `-word` → exclude
- **Semantic search (optional, second-class):** typed phrase → CLIP text embedding → `sqlite-vec` KNN over `file_vec`. Explicitly a separate mode/toggle; ANN is slower than FTS so it is NOT the default keystroke path.
- **Targets:** p95 keystroke query < 100ms at 500k files. If FTS alone can't hold it, add a covering index / cache hot tag→file_id sets; do NOT move search into Python.

### 8.1 "Everything-like" UX

The felt behavior of voidtools Everything is the bar:

- **No submit step.** Results update on every keystroke (debounced), result count shown live.
- **Substring / instant match**, not just prefix. FTS5 with a `trigram` tokenizer (or a prefix index + LIKE fallback for path) so partial words match mid-token.
- **Virtualized result grid/list** — render only visible rows; scroll through 500k results with zero lag. Never build the full DOM.
- **Sortable columns:** name, path, size, dimensions, date modified, `image_kind`, tag count. Sort in SQL (`ORDER BY` on indexed cols), not JS.
- **Live library updates** reflect in open results (a file finishes indexing → its tags appear) without a manual refresh.
- Keyboard-first: arrow-key navigation, enter to open, no mouse required.

### 8.2 Fast preview

- **On-disk thumbnail cache** (e.g. `thumbs/{sha256[:2]}/{sha256}.webp`), generated during indexing, keyed by sha256. Grid renders thumbs, never full images.
- **Preview pane** shows a larger render + all tags (grouped by category, source-colored) + faces/persons + caption. Loads async; grid stays responsive.
- Thumb generation is part of the ingest job so it's ready by the time a file is searchable.

## 9. Manual Tagging & Custom Categories

- Add/remove tag on a file → write `file_tags(source='manual', confidence=NULL)`, then refresh that file's `files_fts.tags_text`. Manual tags rank/override auto ones in the UI.
- Create category → insert into `categories(is_builtin=0)`; immediately usable in tagging and `cat:` filters.
- Rename/merge tags → update `tags` + rebuild affected `files_fts` rows (bounded, background).
- Bulk tagging → multi-select in UI → one transaction.

## 10. OCR — Text Visible in the Image

Separate from captioning (§11): this reads literal text *rendered in* the image — manga speech bubbles, screenshots, memes, signs, watermarks — so you can search for the words themselves.

- **Engine: PaddleOCR.** As of PP-OCRv5 (2025+), it ships a dedicated Thai recognition model alongside English, rather than treating Thai as an afterthought<cite index="53-1">PaddleOCR 3.2.0 added PP-OCRv5 recognition models for English, Thai, and Greek, with the Thai model reaching about 82.68% accuracy</cite>. Runs fully offline, onnxruntime/Paddle-Inference backends, CPU or GPU<cite index="46-1">PaddleOCR runs entirely offline so files never leave the device</cite>.
- **Why not Tesseract/EasyOCR:** Tesseract's Thai support is weaker for mixed Thai/English real-world images (screenshots, manga); PaddleOCR is the stronger pick specifically for mixed-script, multilingual libraries<cite index="48-1">for pipelines mixing multiple scripts including Thai from the same source, PaddleOCR avoids maintaining separate language-specific configs that Tesseract needs</cite>. EasyOCR is fine for quick multilingual scene text but is slower and heavier than needed here<cite index="49-1">EasyOCR gives quick multilingual scene-text results but isn't the first choice when throughput matters</cite>.
- **Pipeline step:** runs per image (both anime and real — memes/screenshots aren't kind-specific) → detect text regions → recognize per region (language auto or Thai+English combined model) → store each region in `ocr_regions` (text, lang, bbox, confidence) and the concatenated text in `files.ocr_text`.
- **Search:** `ocr_text` is a first-class FTS column (§8) — typing a word/phrase that appears *in* an image finds it, same fast path as tags.
- **Tiering:** PaddleOCR's mobile/lightweight (PP-OCRv5 "mobile") models run acceptably on CPU — this facet is available even at the **Low** tier (§5.2); GPU speeds up throughput but isn't required.
- **Honest limits:** rotated/vertical text (common in manga), heavy stylization, and low-contrast overlays reduce accuracy; Thai recognition, while now dedicated, trails the English model's accuracy — treat `ocr_text` as best-effort search aid, not ground truth, and let the user manually correct/add text via a normal tag/edit if OCR misses something important.

## 11. Image Captioning (natural-language)

In addition to discrete tags, generate a free-text caption per image. Captions feed FTS search ("girl on a beach at sunset" finds the file) and double as accessibility text.

- **Model:** a local vision-language captioning model (BLIP-family or a local VLM). Runs entirely on-device via the same Python service; nothing leaves the machine.
- **No app-side content filter.** The library is a personal collection that may include adult/explicit anime/illustration; captions must describe what is actually in the image rather than refuse or sanitize. The app does not inject its own refusal layer — coverage is a function of which model the user loads. Choose captioning models without built-in content refusal so descriptions stay complete and accurate for the user's own material.
- **Storage:** add `caption TEXT` to `files`; include it in `files_fts.tags_text` (or a dedicated FTS column) so it's searchable on the fast path.
- **Regeneration:** user can re-caption a file or swap the caption model per-library.


## 12. Standalone App: Model Download & Progress

Single self-contained desktop app. No manual Python/model setup expected from the user.

- **First-run model manager:** downloads required models on demand — WD14 (ONNX), InsightFace `buffalo_l`, CLIP, PaddleOCR (Thai+English), caption VLM — in the variant chosen by the active tier preset (§5.2). Per-model: size, progress bar, **resumable** download, checksum verification, cancel. Stored under the app data dir. App is usable feature-by-feature as each model finishes (e.g. FTS + manual tagging work before any model downloads).
- **Classification progress (runtime):** global bar from `jobs`/`files.index_status` (queued vs done vs error), current file + stage (ingest → route → infer → OCR → caption → thumb), throughput (img/s), ETA. Live-updating, never blocks search.
- **Auto/manual toggle** surfaced in UI: auto = watcher on; manual = watcher off, "Rescan" button drives indexing.
- **Degrade to CPU** if no GPU (onnxruntime CPU); show a notice that indexing will be slower.

## 13. Project Structure

```
image-tagger/
├─ PROJECT_SPEC.md          # this file
├─ apps/
│  ├─ desktop/              # Electron + Angular
│  │  ├─ main/              # Node main: better-sqlite3, search queries, spawn python
│  │  ├─ renderer/          # Angular: search UI, gallery, tag editor
│  │  └─ preload/
│  └─ indexer/              # Python service
│     ├─ watcher.py         # watchdog → jobs
│     ├─ worker.py          # queue consumer
│     ├─ routers.py         # kind detection + dispatch
│     ├─ models/            # wd14.py, insightface.py, clip.py, ocr.py, caption.py
│     └─ db.py              # writes only
├─ packages/
│  └─ db/                   # schema.sql, migrations, shared query builders
└─ scripts/                 # model download, first-run setup
```

## 14. Milestones

1. **M1 — DB + search shell.** schema.sql, `better-sqlite3` read layer, FTS5, Angular search UI over hand-inserted rows. Prove <100ms search. *(Fail-fast: seed 500k synthetic rows, benchmark before any model work.)*
2. **M2 — Ingest + scope + manual tagging.** include/exclude roots + exclude patterns (§7.0), watchdog, jobs queue, files table, filename/folder split, embedded-metadata extraction (EXIF/PNG/XMP) into `file_metadata` + FTS, manual tag add/remove, custom categories. No AI yet.
3. **M3 — OCR.** PaddleOCR (Thai+English) → `ocr_regions` + `files.ocr_text` + FTS. Independent of kind-routing; can ship before WD14/CLIP.
4. **M4 — WD14 (anime).** character/series/general/clothing/pose tags for illustrations.
5. **M5 — CLIP (scene/clothing/type).** curated vocab, open-vocab additions, optional semantic search via sqlite-vec.
6. **M6 — InsightFace (real faces).** face detect → embed → cluster → name once → auto-attach.
7. **M7 — Learned tags (few-shot).** store CLIP/face embeddings, prototype from manual examples, cosine auto-apply as suggestions, confirm/reject feedback, linear-head upgrade. Depends on M5/M6 embeddings.
8. **M8 — Captioning.** local caption VLM into `files`/FTS; per-library swappable model.
9. **M9 — Polish + packaging.** model-download manager, engine/tier selector + auto-detect, progress UI, auto/manual toggle, thumbnail cache, error retry, bulk ops, single-app packaging + CPU fallback.

## 15. Risks / Open Questions

- **GPU packaging in Electron:** shipping onnxruntime-gpu + CUDA with an Electron app is heavy. Decide: bundle vs first-run download vs require user-installed Python env. *(Recommend first-run downloader.)*
- **Face clustering quality:** incremental clustering drifts; need a re-cluster/merge tool in UI.
- **CLIP vocab curation:** open-vocab is only as good as the label list; ship a sensible default vocab per category, let user extend.
- **FTS refresh cost** on tag rename/merge at scale — keep it background + bounded.
- **`sqlite-vec` maturity** — treat semantic search as optional; app must be fully useful with FTS alone.
- **Caption model** — licensing, VRAM footprint, and quality vary widely; make the caption model swappable per-library. Uncensored coverage is a model-selection choice, bounded by the §11 hard line on minors.
- **Trigram FTS cost** — substring search (trigram tokenizer) inflates the FTS index size and write cost; benchmark against the 500k target and fall back to prefix + LIKE if needed.
- **Model download size/UX** — several GB total; resumable + checksummed downloads are mandatory, not nice-to-have.
- **Engine/VRAM detection** — NVML/torch may misreport on some laptops/eGPUs/shared-memory setups; always let the user override the auto-picked tier, and handle CUDA OOM with the §5.2 downgrade path rather than crashing the queue.
- **Learned-tag false positives** — few-shot on CLIP embeddings will misfire on look-alike characters; keep learned tags as *suggestions* until confirmed, never silently commit them, and make per-tag threshold + min-examples tunable. Real-person (face-space) learning is reliable; character (CLIP-space) learning is best-effort until enough examples/negatives accumulate.
- **OCR accuracy on Thai + stylized text:** dedicated Thai model is new (2025) and trails English accuracy; rotated/vertical manga text and heavy stylization degrade further. Treat `ocr_text` as a search aid, allow manual correction, don't gate any other feature on it.

## 16. Verification Discipline (per working agreement)

- No feature is "done" until run + output shown. "I edited the file" ≠ done.
- Benchmark search latency with real row counts before declaring the perf goal met.
- Verify every library API against current docs; do not code from remembered signatures.
- All infra assumptions above are explicit; flag any that turn out false.
