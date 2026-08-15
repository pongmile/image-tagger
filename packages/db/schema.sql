-- Local Image Tagger & Search — schema v0.1
-- Single SQLite DB shared by Node (reads) and Python indexer (writes).
-- See PROJECT_SPEC.md §6. WAL mode is set by the opener, not here.

PRAGMA foreign_keys = ON;

-- Files -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS files (
  id           INTEGER PRIMARY KEY,
  path         TEXT NOT NULL UNIQUE,
  filename     TEXT,
  folder       TEXT,
  sha256       TEXT NOT NULL,
  phash        TEXT,
  mime         TEXT,
  width        INTEGER,
  height       INTEGER,
  size_bytes   INTEGER,
  mtime        INTEGER,
  image_kind   TEXT,             -- anime | real | other
  caption      TEXT,
  ocr_text     TEXT,
  index_status TEXT NOT NULL DEFAULT 'pending', -- pending|done|error
  indexed_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_files_sha    ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(index_status);
CREATE INDEX IF NOT EXISTS idx_files_kind   ON files(image_kind);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder);

-- Embedded metadata (raw, one row per key) ------------------------
CREATE TABLE IF NOT EXISTS file_metadata (
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  key     TEXT NOT NULL,        -- e.g. exif:Make, png:parameters
  value   TEXT,
  PRIMARY KEY (file_id, key)
);
CREATE INDEX IF NOT EXISTS idx_meta_key ON file_metadata(key);

-- Categories (user-extensible) ------------------------------------
CREATE TABLE IF NOT EXISTS categories (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  color      TEXT,
  is_builtin INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO categories (name, is_builtin) VALUES
  ('person',1),('character',1),('series',1),('clothing',1),
  ('scene',1),('pose',1),('general',1),('rating',1),
  ('path',1),('metadata',1);

-- Tags ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tags (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  category_id INTEGER REFERENCES categories(id),
  UNIQUE(name, category_id)
);
CREATE INDEX IF NOT EXISTS idx_tags_cat ON tags(category_id);

-- File <-> Tag ----------------------------------------------------
CREATE TABLE IF NOT EXISTS file_tags (
  file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  tag_id       INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
  source       TEXT NOT NULL,   -- wd14|clip|insightface|manual|path|metadata|learned
  confidence   REAL,
  confirmed_at INTEGER,         -- set when the user confirms a 'learned' suggestion;
                                 -- survives re-scoring so "confirmed" stays visible on
                                 -- a later visit instead of showing "suggested" again
  PRIMARY KEY (file_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_ft_tag  ON file_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_ft_file ON file_tags(file_id);

-- Tags the user explicitly removed as wrong, per file (§9). Consulted by
-- write_auto_tags() so a reindex/rescan never silently resurrects a tag the
-- user just rejected — without this, wd14/clip auto-tags had no memory of
-- being removed and reappeared on the very next reindex.
CREATE TABLE IF NOT EXISTS rejected_tags (
  file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  tag_id      INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
  source      TEXT,             -- the source it was rejected from (wd14/clip/...)
  rejected_at INTEGER,
  PRIMARY KEY (file_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_rejtag_file ON rejected_tags(file_id);

-- Faces / persons -------------------------------------------------
CREATE TABLE IF NOT EXISTS persons (
  id   INTEGER PRIMARY KEY,
  name TEXT
);
CREATE TABLE IF NOT EXISTS faces (
  id        INTEGER PRIMARY KEY,
  file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  person_id INTEGER REFERENCES persons(id),
  bbox      TEXT,
  embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS idx_faces_file   ON faces(file_id);

-- Learned tags (few-shot self-training) ---------------------------
CREATE TABLE IF NOT EXISTS learned_tags (
  tag_id     INTEGER PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE,
  space      TEXT NOT NULL,   -- clip | face
  method     TEXT NOT NULL,   -- centroid | linear
  threshold  REAL NOT NULL,
  n_pos      INTEGER NOT NULL DEFAULT 0,
  n_neg      INTEGER NOT NULL DEFAULT 0,
  prototype  BLOB,
  classifier BLOB,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS tag_examples (
  tag_id   INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
  file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  label    INTEGER NOT NULL,  -- +1 / -1
  origin   TEXT NOT NULL,     -- manual | confirmed | rejected
  added_at INTEGER,
  PRIMARY KEY (tag_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_tagex_tag ON tag_examples(tag_id);

-- OCR text regions -------------------------------------------------
CREATE TABLE IF NOT EXISTS ocr_regions (
  id         INTEGER PRIMARY KEY,
  file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  text       TEXT NOT NULL,
  lang       TEXT,
  bbox       TEXT,
  confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_ocr_file ON ocr_regions(file_id);

-- Scan roots + exclude patterns -----------------------------------
CREATE TABLE IF NOT EXISTS roots (
  id        INTEGER PRIMARY KEY,
  path      TEXT NOT NULL UNIQUE,
  mode      TEXT NOT NULL,          -- include | exclude
  recursive INTEGER NOT NULL DEFAULT 1,
  enabled   INTEGER NOT NULL DEFAULT 1,
  added_at  INTEGER
);
CREATE TABLE IF NOT EXISTS exclude_rules (
  id      INTEGER PRIMARY KEY,
  pattern TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1
);
INSERT OR IGNORE INTO exclude_rules (id, pattern) VALUES
  (1,'**/.git/**'),(2,'**/node_modules/**'),(3,'*.tmp');

-- Indexing job queue ------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
  id         INTEGER PRIMARY KEY,
  file_id    INTEGER REFERENCES files(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,   -- ingest|infer|reindex|clip
  state      TEXT NOT NULL DEFAULT 'queued', -- queued|running|done|error
  priority   INTEGER NOT NULL DEFAULT 0,
  error      TEXT,
  created_at INTEGER,
  updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
-- Every per-file job lookup goes through (file_id, state): set_job_state()
-- asks "does this file still have a live job?" on *every* completion, and
-- enqueue_job() asks "is one already queued?" on every enqueue. Finished jobs
-- are never deleted, so the jobs table grows without bound relative to the
-- library -- unindexed, each of those questions was a full scan of it. On a
-- 7,500-file library that had already reached 42,000 job rows, one bulk
-- reindex meant tens of millions of row visits spent re-deriving something
-- an index answers directly. Created via CREATE INDEX IF NOT EXISTS in the
-- schema every connect(), so existing libraries pick it up on next launch
-- with no migration step.
CREATE INDEX IF NOT EXISTS idx_jobs_file  ON jobs(file_id, state);

-- Full-text search (fast path). Trigram = substring matching. ------
-- Regular (not contentless) FTS5: the Python writer runs on a bundled SQLite
-- (3.37) that predates `contentless_delete` (3.43), so per-file DELETE+INSERT
-- refresh (manual tags, reindex) needs an ordinary FTS5 table. Costs a text
-- copy on disk; still comfortably inside the 500k / <100ms search budget.
-- Rows are still managed explicitly by rowid = files.id.
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
  path, filename, folder, tags_text, meta_text, caption, ocr_text,
  tokenize='trigram'
);

-- App settings (tier, engine, thresholds, opt-ins) -----------------
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- CLIP zero-shot label vocabulary (§5, M5). Open-vocab: user-extensible at
-- runtime; new labels need no retraining. `category` maps to a tags category
-- (scene/clothing/pose/...), `enabled` lets a label be toggled off.
CREATE TABLE IF NOT EXISTS clip_labels (
  id       INTEGER PRIMARY KEY,
  category TEXT NOT NULL,
  label    TEXT NOT NULL,
  enabled  INTEGER NOT NULL DEFAULT 1,
  UNIQUE(category, label)
);
INSERT OR IGNORE INTO clip_labels (category, label) VALUES
  ('scene','beach'),('scene','forest'),('scene','city street'),
  ('scene','bedroom'),('scene','classroom'),('scene','office'),
  ('scene','mountain'),('scene','night'),('scene','sunset'),
  ('scene','snow'),('scene','underwater'),('scene','kitchen'),
  ('scene','restaurant'),('scene','concert stage'),('scene','indoors'),
  ('scene','outdoors'),
  ('clothing','school uniform'),('clothing','swimsuit'),('clothing','kimono'),
  ('clothing','dress'),('clothing','hoodie'),('clothing','business suit'),
  ('clothing','armor'),('clothing','casual clothes'),('clothing','maid outfit'),
  ('clothing','sportswear'),('clothing','coat'),
  ('pose','standing'),('pose','sitting'),('pose','lying down'),
  ('pose','running'),('pose','walking'),('pose','jumping');

-- Per-model facet output cache. A file switching between two previously-used
-- models (e.g. WD14 variant A -> B -> A) restores instantly from here instead
-- of re-running inference, and file_tags/search only ever reflect whichever
-- model is currently active -- an inactive model's cached output sits here,
-- invisible to search, until its model is selected again.
CREATE TABLE IF NOT EXISTS facet_model_cache (
  file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  facet      TEXT NOT NULL,        -- 'wd14' | 'caption'
  model_key  TEXT NOT NULL,        -- the variant id active when this was produced
  payload    TEXT NOT NULL,        -- JSON: wd14 -> {"image_kind","tags":[{"category","name","confidence"}]}
                                    --       caption -> {"text": "..."}
  cached_at  INTEGER,
  PRIMARY KEY (file_id, facet, model_key)
);
CREATE INDEX IF NOT EXISTS idx_facet_cache_file ON facet_model_cache(file_id);

-- Note: the CLIP embedding store `file_vec` is a sqlite-vec vec0 virtual table
-- created lazily by indexer/vec.py once CLIP runs (it needs the sqlite-vec
-- extension loaded, so it can't live in this always-applied schema). Semantic
-- search stays optional/second-class per §8; the app is fully useful without it.
