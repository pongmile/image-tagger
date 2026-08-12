// UI-side writes — spec §9 (manual tagging & custom categories).
// Manual edits are tiny, user-driven, and must feel instant, so the Electron
// main process writes them directly instead of round-tripping to Python. WAL +
// busy_timeout let this coexist with the indexer's background writes. The
// indexer still owns all *bulk*/AI writes; this file only touches manual tags,
// categories, and the affected file's FTS row.

function getOrCreateCategory(db, name, color = null) {
  const row = db.prepare("SELECT id FROM categories WHERE name=?").get(name);
  if (row) return row.id;
  return db
    .prepare("INSERT INTO categories (name,color,is_builtin) VALUES (?,?,0)")
    .run(name, color).lastInsertRowid;
}

function getOrCreateTag(db, name, category) {
  const catId = getOrCreateCategory(db, category);
  const row = db
    .prepare("SELECT id FROM tags WHERE name=? AND category_id=?")
    .get(name, catId);
  if (row) return row.id;
  return db
    .prepare("INSERT INTO tags (name,category_id) VALUES (?,?)")
    .run(name, catId).lastInsertRowid;
}

// Rebuild the FTS row for one file from current sources (regular FTS5 table).
function refreshFts(db, fileId) {
  const f = db
    .prepare(
      "SELECT path,filename,folder,caption,ocr_text FROM files WHERE id=?"
    )
    .get(fileId);
  if (!f) return;
  const tagsText = db
    .prepare(
      "SELECT group_concat(t.name,' ') s FROM file_tags ft " +
        "JOIN tags t ON t.id=ft.tag_id WHERE ft.file_id=?"
    )
    .get(fileId).s || "";
  const metaText = db
    .prepare(
      "SELECT group_concat(value,' ') s FROM file_metadata WHERE file_id=? AND key IN " +
        "('png:parameters','png:Comment','exif:Make','exif:Model','exif:LensModel','exif:Software')"
    )
    .get(fileId).s || "";
  db.prepare("DELETE FROM files_fts WHERE rowid=?").run(fileId);
  db.prepare(
    "INSERT INTO files_fts (rowid,path,filename,folder,tags_text,meta_text,caption,ocr_text) " +
      "VALUES (?,?,?,?,?,?,?,?)"
  ).run(
    fileId,
    f.path,
    f.filename,
    f.folder,
    tagsText,
    metaText,
    f.caption || "",
    f.ocr_text || ""
  );
}

function addManualTag(db, fileId, category, name) {
  const tx = db.transaction(() => {
    const tagId = getOrCreateTag(db, name, category);
    db.prepare(
      "INSERT INTO file_tags (file_id,tag_id,source,confidence) VALUES (?,?,'manual',NULL) " +
        "ON CONFLICT(file_id,tag_id) DO UPDATE SET source='manual', confidence=NULL"
    ).run(fileId, tagId);
    // The user is explicitly asking for this tag now — a past rejection
    // (§9 "×" on a wrong auto-tag) should no longer suppress it on reindex.
    db.prepare("DELETE FROM rejected_tags WHERE file_id=? AND tag_id=?").run(fileId, tagId);
    refreshFts(db, fileId);
  });
  tx();
}

function removeTag(db, fileId, category, name) {
  const tx = db.transaction(() => {
    const row = db
      .prepare(
        "SELECT t.id FROM tags t JOIN categories c ON c.id=t.category_id " +
          "WHERE t.name=? AND c.name=?"
      )
      .get(name, category);
    if (row)
      db.prepare("DELETE FROM file_tags WHERE file_id=? AND tag_id=?").run(
        fileId,
        row.id
      );
    refreshFts(db, fileId);
  });
  tx();
}

// All tags for a file, grouped for the preview pane (source-colored in UI).
// minConfidence (optional): hide auto tags below this cosine/probability floor —
// manual tags (confidence=NULL) always show, never filtered (spec §9: manual
// tags override/aren't gated by auto-tag confidence).
function tagsForFile(db, fileId, minConfidence) {
  const hasFloor = minConfidence != null && minConfidence !== "";
  return db
    .prepare(
      "SELECT c.name AS category, t.name, ft.source, ft.confidence, " +
        "(ft.confirmed_at IS NOT NULL) AS confirmed " +
        "FROM file_tags ft JOIN tags t ON t.id=ft.tag_id " +
        "JOIN categories c ON c.id=t.category_id WHERE ft.file_id=? " +
        (hasFloor ? "AND (ft.confidence IS NULL OR ft.confidence >= ?) " : "") +
        "ORDER BY c.name, ft.source, t.name"
    )
    .all(...(hasFloor ? [fileId, Number(minConfidence)] : [fileId]))
    .map((r) => ({ ...r, confirmed: !!r.confirmed }));
}

function createCategory(db, name, color = null) {
  return getOrCreateCategory(db, name, color);
}

// All categories (for the tag editor / category manager, §9).
function listCategories(db) {
  return db
    .prepare("SELECT id, name, color, is_builtin FROM categories ORDER BY is_builtin DESC, name")
    .all();
}

// Rich detail for the preview pane (§8.2): caption (§11), OCR text (§10),
// per-file faces/persons (§8.2), and embedded metadata / SD params (§5.1).
// All of this already lives in the DB from indexing — the preview just never
// surfaced it before.
function fileDetail(db, fileId) {
  const f = db
    .prepare("SELECT id, path, sha256, caption, ocr_text, image_kind FROM files WHERE id=?")
    .get(fileId);
  if (!f) return null;
  const faces = db
    .prepare(
      "SELECT fa.id, fa.person_id, p.name FROM faces fa " +
        "LEFT JOIN persons p ON p.id=fa.person_id WHERE fa.file_id=? ORDER BY fa.id"
    )
    .all(fileId);
  const metadata = db
    .prepare(
      "SELECT key, value FROM file_metadata WHERE file_id=? " +
        "ORDER BY (key LIKE 'png:parameters') DESC, key"
    )
    .all(fileId)
    // Pillow exposes ICC profiles and some EXIF blocks as bytes. Older index
    // entries decoded those bytes with replacement characters, producing a
    // wall of binary garbage in the preview. Keep textual fields only.
    .filter((row) => !looksBinaryMetadata(row.key, row.value));
  return {
    id: f.id,
    caption: f.caption || "",
    ocr_text: f.ocr_text || "",
    image_kind: f.image_kind || null,
    faces: faces.map((x) => ({
      id: x.id,
      person_id: x.person_id,
      name: x.name || null,
    })),
    metadata,
  };
}

function looksBinaryMetadata(key, value) {
  const k = String(key || "").toLowerCase();
  if (/(?:icc[_ -]?profile|thumbnail|maker[_ -]?note)/.test(k)) return true;
  const s = String(value || "");
  if (!s) return false;
  let suspicious = 0;
  for (const ch of s) {
    const code = ch.codePointAt(0);
    if (ch === "\uFFFD" || (code < 32 && ch !== "\n" && ch !== "\r" && ch !== "\t")) {
      suspicious++;
    }
  }
  return suspicious > 8 && suspicious / s.length > 0.01;
}

// Manual OCR correction (§10: "let the user manually correct/add text"). Writes
// files.ocr_text and refreshes FTS so the corrected words are searchable.
function setOcrText(db, fileId, text) {
  const tx = db.transaction(() => {
    db.prepare("UPDATE files SET ocr_text=? WHERE id=?").run(text || "", fileId);
    refreshFts(db, fileId);
  });
  tx();
  return text || "";
}

// Resolve the on-disk thumbnail (webp keyed by sha256, §8.2) and return it as a
// data URI so the renderer can show it under Electron's CSP without a custom
// protocol. Falls back to null (UI shows a placeholder) if not generated yet.
function thumbDataUri(db, fileId) {
  const fs = require("fs");
  const path = require("path");
  const os = require("os");
  const f = db.prepare("SELECT sha256 FROM files WHERE id=?").get(fileId);
  if (!f || !f.sha256) return null;
  const home =
    process.env.IMAGE_TAGGER_HOME ||
    path.join(os.homedir(), ".image-tagger");
  const thumb = path.join(home, "thumbs", f.sha256.slice(0, 2), `${f.sha256}.webp`);
  try {
    if (fs.existsSync(thumb)) {
      return "data:image/webp;base64," + fs.readFileSync(thumb).toString("base64");
    }
  } catch {
    /* unreadable cache entry — fall through to null */
  }
  return null;
}

// Full-resolution image for the preview-pane lightbox (click-to-enlarge). The
// on-disk thumbnail cache is capped at 320px (ingest.py _THUMB_MAX) — too small
// to "enlarge" meaningfully — so this reads the original file directly. One-shot
// on-demand load, not part of the hot search path; guarded with a size cap so a
// stray huge file can't block the renderer on a multi-MB base64 string.
const _FULL_IMAGE_MAX_BYTES = 30 * 1024 * 1024;
const _MIME_BY_EXT = {
  ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
  ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
  ".tiff": "image/tiff",
};
function fileFullDataUri(db, fileId) {
  const fs = require("fs");
  const path = require("path");
  const f = db.prepare("SELECT path, mime FROM files WHERE id=?").get(fileId);
  if (!f || !f.path) return null;
  try {
    const stat = fs.statSync(f.path);
    if (stat.size > _FULL_IMAGE_MAX_BYTES) return null; // caller falls back to thumb
    const mime = f.mime || _MIME_BY_EXT[path.extname(f.path).toLowerCase()] || "image/png";
    return `data:${mime};base64,` + fs.readFileSync(f.path).toString("base64");
  } catch {
    return null; // moved/deleted/unreadable — fall through to the thumb
  }
}

// Bulk manual tagging (§9): one transaction for a multi-select in the grid.
function bulkAddTag(db, fileIds, category, name) {
  const tx = db.transaction(() => {
    const tagId = getOrCreateTag(db, name, category);
    const ins = db.prepare(
      "INSERT INTO file_tags (file_id,tag_id,source,confidence) VALUES (?,?,'manual',NULL) " +
        "ON CONFLICT(file_id,tag_id) DO UPDATE SET source='manual', confidence=NULL"
    );
    for (const fid of fileIds) {
      ins.run(fid, tagId);
      refreshFts(db, fid);
    }
  });
  tx();
  return fileIds.length;
}

function bulkRemoveTag(db, fileIds, category, name) {
  const row = db
    .prepare(
      "SELECT t.id FROM tags t JOIN categories c ON c.id=t.category_id " +
        "WHERE t.name=? AND c.name=?"
    )
    .get(name, category);
  if (!row) return 0;
  const tx = db.transaction(() => {
    const del = db.prepare("DELETE FROM file_tags WHERE file_id=? AND tag_id=?");
    for (const fid of fileIds) {
      del.run(fid, row.id);
      refreshFts(db, fid);
    }
  });
  tx();
  return fileIds.length;
}

// App settings (key/value) — e.g. the user-selected models download folder (§12).
function getSetting(db, key, fallback = null) {
  const row = db.prepare("SELECT value FROM settings WHERE key=?").get(key);
  return row ? row.value : fallback;
}

function setSetting(db, key, value) {
  db.prepare(
    "INSERT INTO settings (key,value) VALUES (?,?) " +
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
  ).run(key, String(value));
  return value;
}

module.exports = {
  addManualTag,
  removeTag,
  tagsForFile,
  createCategory,
  listCategories,
  fileDetail,
  setOcrText,
  thumbDataUri,
  fileFullDataUri,
  refreshFts,
  getSetting,
  setSetting,
  bulkAddTag,
  bulkRemoveTag,
};
