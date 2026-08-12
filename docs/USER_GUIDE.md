# Image Tagger User Guide

## 1. First launch

Install the `.exe`, or extract the complete portable `.zip` before running `Image Tagger.exe`. The app stores its database, thumbnails, settings, and default model directory under `%USERPROFILE%\.image-tagger`. It does not modify original image files.

1. Open **Sources**.
2. Select **Add folder** and choose an image folder.
3. Add exclude roots or patterns such as `**/node_modules/**`, `**/.git/**`, or `*.tmp` when needed.
4. Select **Rescan this source**, or return to Search and select **Rescan**.
5. Wait until the status becomes `idle`.

The `samples/` directory in the portable build or repository can be used for a privacy-safe trial.

## 2. Search

An empty query browses up to 1,000 indexed files. Results update as you type.

- Multiple terms use AND: `beach sunset`.
- OR: `cat | dog`.
- Exclusion: `!draft` or `-draft`.
- Exact phrase: `"blue sky"`.
- Category: `character:miku`.
- Named face: `person:alice`.
- Folder: `folder:D:/Photos`.
- Size: `size:>10mb`.
- Wildcard: `*.png`.

**Match Case**, **Whole Word**, **Match Path**, **Diacritics**, and **Regex** make matching more restrictive. Invalid queries show an error below the search box instead of freezing the app.

## 3. Views, selection, and full-resolution preview

- **List** is optimized for large filename/folder result sets.
- **Small** and **Large** show virtualized thumbnails.
- Click to select, `Shift+click` to select a range, or drag for marquee selection.
- Double-click or press `Enter` to open the original file.
- Right-click for open, reveal, copy path, tag, re-index, and properties actions.
- Click a preview or People sample to open the full-resolution viewer.
- The viewer streams the original file instead of sending a size-limited base64 copy. Large 5K/10K and 30 MB+ files therefore remain available when the format is supported by Chromium.
- Use the wheel or `+`/`-` to zoom from 100% to 800%, drag to pan, double-click to reset, and press `Esc` to close.

## 4. Manual and learned tags

Add tags as `category:name`, for example `project:website` or `character:alice`. Multi-selection supports bulk add/remove operations.

To train a learned tag:

1. Add the same tag manually to at least five correct examples.
2. Train the tag to create a CLIP-embedding centroid.
3. Confirm or reject suggestions with the check/cross controls.
4. With enough positive and negative examples, the app upgrades to a linear classifier.

Model output never overwrites manual tag provenance.

## 5. Models

The **Models** page displays dependencies, weights, variants, size, status, and detected hardware tier.

- OCR is included in the base installation for English and Thai text.
- WD14 produces anime/illustration tags.
- CLIP provides scene/clothing/pose tags, semantic search, and learned-tag embeddings.
- InsightFace detects and clusters real faces.
- BLIP and JoyCaption generate searchable natural-language descriptions.

JoyCaption is NVIDIA-only and never selected automatically:

- **JoyCaption 4-bit** uses bitsandbytes NF4 and requires about 6 GB VRAM; the download is about 16 GB.
- **JoyCaption full** uses BF16 and requires about 17 GB VRAM; the download is about 16 GB.

Select a variant, choose **Download**, wait for validation, and then select **Apply**. Each variant has its own directory and ready marker, so switching back does not download it again. Re-index existing images when a newly enabled model needs to backfill data.

## 6. People and OCR

Use **People** to name face clusters and merge clusters belonging to the same person. Named clusters are searchable with `person:name`.

Expand **Text in image (OCR)** in the preview to correct OCR text. Saving updates FTS immediately.

## 7. Backup and migration

Close the app before copying `%USERPROFILE%\.image-tagger`, which contains:

- `library.db` and its WAL/SHM files.
- `thumbs/`.
- `models/`, unless a different model directory was selected.
- Electron profile/settings data.

Original image paths must remain valid. If drive letters or folders change, add the new Source and rescan.

## 8. Troubleshooting

- **No images:** verify that the Source is enabled, no pattern excludes it, and run Rescan.
- **New tag not searchable:** wait for `idle`, or re-index that file.
- **Semantic search unavailable:** install and enable CLIP, then re-index to create embeddings.
- **GPU unavailable:** update the GPU driver and inspect Models; supported engines fall back to CPU.
- **Model download fails:** check free storage/network and retry. Partial downloads are not marked ready.
- **Google Drive/OneDrive image is 0 bytes:** make it available offline, wait for sync, and rescan. The app skips placeholders without an infinite retry loop.
- **Non-empty corrupt image:** restore or re-export it as JPEG/PNG/WebP, then rescan. The app never rewrites a damaged original.
- **Portable build cannot start:** extract the entire ZIP first; do not run the executable from inside the archive.
- **Second instance does not open:** the existing window is focused intentionally to avoid duplicate writers.
- **Reset:** back up and rename `%USERPROFILE%\.image-tagger`. Deleting it removes index/tag/settings data, but not original images.

## 9. Building from source

Run all commands from the repository root:

```powershell
git clone https://github.com/pongmile/image-tagger.git
cd image-tagger
npm run setup
npm test
npm run test:ui
npm run dev
```

Do not run `node scripts/run-electron.js apps/desktop` from inside `apps/desktop`; the supported entry point is `npm run dev` from the repository root.
