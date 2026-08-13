/**
 * Typed bridge to the Electron preload `window.api` (see apps/desktop/src/preload).
 * When running outside Electron (browser dev / headless verification), a mock
 * with seed data is used instead, so the UI is developable and testable without
 * the native better-sqlite3 build.
 */

export interface FileRow {
  id: number;
  path: string;
  filename: string;
  folder: string;
  image_kind: string | null;
  sha256?: string | null;
  width: number | null;
  height: number | null;
  size_bytes: number | null;
  mtime: number | null;
  mime?: string | null;
}

export interface FaceRef { id: number; person_id: number | null; name: string | null; }
export interface MetaKV { key: string; value: string; }
export interface FileDetail {
  id: number;
  caption: string;
  ocr_text: string;
  image_kind: string | null;
  faces: FaceRef[];
  metadata: MetaKV[];
}
export interface Category { id: number; name: string; color: string | null; is_builtin: number; }

// Update check (see apps/desktop/src/main/updater.js) — not a full
// auto-updater: it only reports whether the latest GitHub Release is newer
// than this build and hands back a URL to it, so the user still goes
// through the same, already-tested installer flow to actually update.
export interface UpdateCheckResult {
  ok: boolean;
  error?: string;
  currentVersion?: string;
  latestVersion?: string;
  updateAvailable?: boolean;
  url?: string;
  notes?: string;
  checkedAt?: number;
}

export interface TagRow {
  category: string;
  name: string;
  source: string;
  confidence: number | null;
  confirmed?: boolean;
}

export interface SearchOpts {
  limit?: number;
  sort?: 'name' | 'path' | 'size' | 'dim' | 'date' | 'kind';
  dir?: 'asc' | 'desc';
  matchCase?: boolean;
  wholeWord?: boolean;
  matchPath?: boolean;
  matchDiacritics?: boolean;
  minConfidence?: number;
  mediaType?: 'image' | 'video' | 'both';
  // §8.1 "use regex" checkbox (default off): every free-text term is tested
  // as a JS regex instead of substring/wildcard matching. Field filters
  // (tag:/folder:/person:/category:) are unaffected.
  regex?: boolean;
}

export interface Progress {
  files_total: number;
  files_done: number;
  jobs: Record<string, number>;
  // Per-stage counts for the split progress bars, all in *files* so they can
  // be compared with files_total and with each other. Derived from stored
  // output, not the jobs table (finished jobs are never deleted, so a
  // job-ratio bar would sit near 100% forever). Absent on older backends.
  scan_done?: number;
  caption_done?: number;
  tag_done?: number;
  // Which stages are switched on, so a bar that could never fill (captioning
  // disabled) is hidden rather than shown frozen at 0%.
  facets?: Record<string, boolean>;
  paused?: boolean;
  mode?: 'auto' | 'manual';
  current?: string;
  rss_mb?: number | null;
}
export interface Root {
  id: number; path: string; mode: 'include' | 'exclude';
  recursive: boolean; enabled: boolean; files: number;
  done?: number; pending?: number; errors?: number; last_indexed?: number | null;
}
export interface TagInfo { id: number; name: string; category: string; files: number; }
export interface RenameResult { ok: boolean; merged?: boolean; files?: number; name?: string; error?: string; }
export interface ExcludeRule { id: number; pattern: string; enabled: boolean; }
export interface LearnSummary {
  ok: boolean; error?: string; count?: number;
  method?: string; threshold?: number; n_pos?: number; n_neg?: number; applied?: number;
  prepared?: number; usable?: number; queued?: number;
}
export interface SemanticHit {
  id: number; path: string; filename: string; distance: number;
}
export interface Facet {
  facet?: string | null;
  label: string; milestone: string; dep: string; dep_ok: boolean;
  model_ok: boolean; enabled: boolean; download: string | null; state: string;
  model_name?: string | null; source?: string | null; url?: string | null;
  size_mb?: number | null; kind?: string | null; dir?: string | null;
  variant_id?: string | null; has_variants?: boolean;
  install?: string | null;
}
export interface Variant {
  id: string; label: string; tier: string; size_mb: number; dim?: number;
}
export interface FacetVariants {
  facet: string; tier: string; recommended: string; selected: string; variants: Variant[];
}
export interface DownloadProgress {
  model: string; pct: number | null; indeterminate?: boolean;
  done?: number; total?: number; file?: string;
  state?: 'queued' | 'running' | 'done' | 'error'; phase?: string;
  message?: string; error?: string | null; started_at?: number;
  updated_at?: number; finished_at?: number | null; elapsed_s?: number;
  dir?: string; log?: string;
}
export interface DownloadDone { model: string; ok: boolean; error?: string | null; dir?: string; }
export interface ModelState {
  facets: Facet[]; variants: FacetVariants[]; downloads: DownloadProgress[]; models_dir: string;
}
export interface Person { id: number; name: string | null; faces: number; sample: string | null; sample_id: number | null; }
export interface ErrorRow {
  id: number; file_id: number; kind: string; error: string | null;
  updated_at: number | null; path: string; filename: string;
}
export interface LearnedTagRow {
  tag_id: number; name: string; category: string; space: string; method: string;
  n_pos: number; n_neg: number; threshold: number;
  updated_at: number | null; applied: number;
}
// A tag accumulating confirm/reject examples that hasn't crossed the
// training floor yet (§5.3) — has no learned_tags row, so it's listed
// separately from LearnedTagRow rather than as a zero-filled variant of it.
export interface TagProgressRow {
  tag_id: number; name: string; category: string;
  n_pos: number; n_neg: number; updated_at: number | null;
}

export interface IndexerApi {
  semantic(query: string, k?: number): Promise<{ available: boolean; hits: SemanticHit[]; reason?: string }>;
  // rescan/rescan_root run the actual filesystem walk on a background thread
  // in the daemon so the command loop stays responsive to other requests
  // (§7) — this call returns as soon as the scan has *started*, not when
  // it's done. Await onScanDone for the real result (library.service.ts's
  // rescan()/rescanRoot() wrap that up into the old synchronous-looking API).
  rescan(): Promise<{ started: boolean; error?: string }>;
  rescanRoot(rootId: number): Promise<{ started: boolean; error?: string }>;
  onScanDone(cb: (e: {
    ok: boolean; root_id?: number | null; error?: string;
    added?: number; changed?: number; removed?: number; unchanged?: number; revived?: number;
  }) => void): () => void;
  addRoot(path: string, mode?: string): Promise<unknown>;
  progress(): Promise<Progress>;
  pause(): Promise<{ paused: boolean }>;
  resume(): Promise<{ paused: boolean }>;
  setMode(mode: 'auto' | 'manual'): Promise<{ mode: string }>;
  retryErrors(fileId?: number): Promise<{ requeued: number }>;
  reindexAll(): Promise<{ queued: number }>;
  reindexRoot(rootId: number): Promise<{ queued: number; root_id?: number }>;
  recaptionRoot(rootId: number): Promise<{ ok: boolean; queued?: number; root_id?: number; error?: string }>;
  // Explicit "↻ Regen all captions" (Sources page) — force-redoes every
  // file's caption with whichever model is active, unlike a plain variant
  // switch (which now only fills in files with no caption yet).
  recaptionAll(): Promise<{ ok: boolean; queued?: number; error?: string }>;
  listErrors(rootId?: number, limit?: number): Promise<{ errors: ErrorRow[] }>;
  roots(): Promise<{ roots: Root[]; excludes: ExcludeRule[] }>;
  addExclude(path: string): Promise<unknown>;
  removeRoot(rootId: number): Promise<unknown>;
  toggleRoot(rootId: number, enabled: boolean): Promise<unknown>;
  addExcludePattern(pattern: string): Promise<unknown>;
  removeExclude(ruleId: number): Promise<unknown>;
  toggleExclude(ruleId: number, enabled: boolean): Promise<unknown>;
  renameTag(category: string, oldName: string, newName: string): Promise<RenameResult>;
  listTags(): Promise<{ tags: TagInfo[] }>;
  learnStatus(category: string, name: string): Promise<{ count: number }>;
  learn(category: string, name: string, space?: string): Promise<LearnSummary>;
  learnConfirm(category: string, name: string, fileId: number): Promise<{ ok: boolean }>;
  learnReject(category: string, name: string, fileId: number): Promise<{ ok: boolean }>;
  // Removes any-source auto-tag (wd14/clip) from one file, remembers the
  // rejection so reindex/rescan never silently re-adds it, and feeds it to
  // the few-shot learner as a negative example (§9) — unlike learnReject,
  // works even when the tag has no learned_tags row yet.
  rejectAutoTag(category: string, name: string, fileId: number, source?: string): Promise<{ ok: boolean; error?: string }>;
  // Positive counterpart to rejectAutoTag (§9): durable confirmed_at marker
  // plus a positive few-shot example for model-driven sources, so confirming
  // a wd14/clip tag reinforces recognition instead of being pure UI state.
  confirmAutoTag(category: string, name: string, fileId: number): Promise<{
    ok: boolean; error?: string;
    n_pos?: number; n_neg?: number; trained?: boolean; applied?: number;
    needed?: number; reinforces?: boolean;
  }>;
  listLearnedTags(): Promise<{ tags: LearnedTagRow[]; in_progress: TagProgressRow[]; min_positives: number }>;
  // Reset one tag's few-shot state (§5.3): drops every auto-applied 'learned'
  // row, the trained model and the accumulated examples, leaving manual/base
  // tagging for that tag alone. For a learned tag that has started behaving
  // wrongly — retraining can only add to the same examples, never undo them.
  learnForget(category: string, name: string): Promise<{
    ok: boolean; error?: string;
    unapplied?: number; examples_cleared?: number; was_trained?: boolean;
  }>;
  download(model: string, variant?: string): Promise<{ started?: boolean; ok?: boolean; dir?: string; error?: string }>;
  installDependency(facet: string): Promise<{ started?: boolean; model?: string; error?: string }>;
  downloadStatus(): Promise<{ downloads: DownloadProgress[] }>;
  onDownloadProgress(cb: (e: DownloadProgress) => void): () => void;
  onDownloadDone(cb: (e: DownloadDone) => void): () => void;
  facets(): Promise<Facet[]>;
  setFacetEnabled(facet: string, enabled: boolean): Promise<{ ok: boolean; enabled?: boolean; error?: string }>;
  modelsDir(): Promise<string>;
  variants(): Promise<FacetVariants[]>;
  modelState(): Promise<ModelState>;
  setVariant(facet: string, variant: string): Promise<{ ok: boolean; reindex_needed?: boolean; recaptioning?: number }>;
  persons(): Promise<Person[]>;
  personFiles(id: number): Promise<{ id: number; path: string; filename: string }[]>;
  namePerson(id: number, name: string): Promise<unknown>;
  mergePersons(src: number, dst: number): Promise<unknown>;
  onProgress(cb: (p: Progress) => void): () => void;
  // Fires when the Python indexer daemon exited unexpectedly and the bridge
  // auto-restarted it (§7 resilience) — a crash/hang recovered within the
  // session rather than requiring the user to relaunch the app.
  onRestarted(cb: (e: { previousExitCode: number | null }) => void): () => void;
  // Daemon-side trouble that used to vanish silently (§7 resilience):
  // "warning" is structured ({message}), "stderr" is the raw stream (Python
  // tracebacks for anything not already turned into a per-job error).
  onWarning(cb: (e: { message: string }) => void): () => void;
  onStderr(cb: (line: string) => void): () => void;
}

export interface Api {
  pickFolder?(): Promise<string[]>;
  search(q: string, opts?: SearchOpts): Promise<FileRow[]>;
  count(q: string, opts?: SearchOpts): Promise<number>;
  tags(fileId: number, minConfidence?: number): Promise<TagRow[]>;
  addTag(fileId: number, category: string, name: string): Promise<TagRow[]>;
  removeTag(fileId: number, category: string, name: string): Promise<TagRow[]>;
  createCategory(name: string, color?: string | null): Promise<number>;
  listCategories(): Promise<Category[]>;
  fileDetail(fileId: number): Promise<FileDetail | null>;
  thumb(fileId: number): Promise<string | null>;
  fullImage(fileId: number): Promise<string | null>;
  setOcr(fileId: number, text: string): Promise<FileDetail | null>;
  // Always bypass Pause Tagger (§12) — a deliberate single-file click acts on
  // just that file, immediately, without touching the job queue or "resuming"
  // anything else. Like rescan, these return as soon as the work has *started*
  // (the daemon runs it on its own thread so a multi-second inference can't
  // block its single-threaded RPC loop); await onFileDone for the outcome.
  reindexFile(filePath: string): Promise<{
    ok: boolean; started?: boolean; removed?: boolean; error?: string
  }>;
  recaptionFile(filePath: string): Promise<{
    ok: boolean; started?: boolean; error?: string
  }>;
  retagFile(filePath: string): Promise<{
    ok: boolean; started?: boolean; error?: string
  }>;
  onFileDone(cb: (e: {
    ok: boolean; action: 'reindex' | 'recaption' | 'retag'; path: string; error?: string;
  }) => void): () => void;
  openFile?(filePath: string): Promise<string>;
  revealFile?(filePath: string): Promise<void>;
  copyText?(text: string): Promise<boolean>;
  getSetting(key: string, fallback?: string | null): Promise<string | null>;
  setSetting(key: string, value: string): Promise<string>;
  bulkAddTag(fileIds: number[], category: string, name: string): Promise<number>;
  bulkRemoveTag(fileIds: number[], category: string, name: string): Promise<number>;
  getAppVersion?(): Promise<string>;
  checkForUpdates?(force?: boolean): Promise<UpdateCheckResult>;
  openReleasePage?(url: string): Promise<boolean>;
  indexer: IndexerApi;
}

export const IS_ELECTRON = typeof (globalThis as any).api !== 'undefined';

let _api: Api | null = null;
export function getApi(): Api {
  if (_api) return _api;
  const w = globalThis as any;
  _api = IS_ELECTRON ? (w.api as Api) : mockApi();
  return _api;
}

// --- Browser mock -----------------------------------------------------------
function mockApi(): Api {
  interface Seed { file: FileRow; tags: TagRow[]; detail: FileDetail; }
  // A tiny inline SVG "thumbnail" so the mock grid/preview show real images.
  const mockThumb = (label: string, hue: number) =>
    'data:image/svg+xml;utf8,' + encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="240" height="180">` +
      `<rect width="240" height="180" fill="hsl(${hue},50%,60%)"/>` +
      `<text x="120" y="96" font-size="16" fill="white" text-anchor="middle" font-family="sans-serif">${label}</text></svg>`);
  const chars = ['hatsune miku', 'frieren', 'rem', 'makima', 'nahida'];
  const scenes = ['beach', 'city street', 'forest', 'bedroom', 'concert stage'];
  const kinds = ['anime', 'real', 'other'];
  const seed: Seed[] = [];
  for (let i = 1; i <= 500; i++) {
    const ch = chars[i % chars.length];
    const sc = scenes[i % scenes.length];
    const kind = kinds[i % 3];
    const folder = `D:/Pictures/${ch.replace(' ', '_')}`;
    const filename = `${ch.replace(' ', '_')}_${String(i).padStart(4, '0')}.png`;
    seed.push({
      file: {
        id: i, path: `${folder}/${filename}`, filename, folder, sha256: `sha${i}`,
        image_kind: kind, width: 512 + (i % 5) * 128, height: 768,
        size_bytes: 200000 + i * 137, mtime: 1_700_000_000 + i * 3600,
      },
      tags: [
        { category: 'character', name: ch, source: 'wd14', confidence: 0.94 },
        { category: 'scene', name: sc, source: 'clip', confidence: 0.71 },
        { category: 'general', name: 'long hair', source: 'wd14', confidence: 0.61 },
        ...(i % 4 === 0
          ? [{ category: 'general', name: 'favorite', source: 'manual', confidence: null } as TagRow]
          : []),
        ...(i % 5 === 0
          ? [{ category: 'character', name: 'new vtuber', source: 'learned', confidence: 0.63 } as TagRow]
          : []),
      ],
      detail: {
        id: i,
        caption: `${ch} standing on a ${sc}, ${kind} illustration, detailed lighting.`,
        ocr_text: i % 3 === 0 ? `LIVE 2024\n${ch.toUpperCase()}` : '',
        image_kind: kind,
        faces: kind === 'real'
          ? [{ id: i * 10, person_id: (i % 3) + 1, name: i % 2 ? 'Alex' : null }]
          : [],
        metadata: [
          { key: 'exif:Make', value: 'Canon' },
          ...(i % 2 === 0
            ? [{ key: 'png:parameters', value: `${ch}, masterpiece, best quality\nSteps: 28, Sampler: DPM++ 2M, CFG scale: 7, Seed: ${1000 + i}` }]
            : []),
        ],
      },
    });
  }
  const byId = new Map(seed.map((s) => [s.file.id, s]));
  const mockFacets: Facet[] = [
    { facet: 'ocr', label: 'OCR (text in image)', milestone: 'M3', dep: 'rapidocr_onnxruntime', dep_ok: true, model_ok: true, enabled: true, download: null, state: 'ready', model_name: 'PP-OCRv4 (Thai+English)', source: 'pip: rapidocr-onnxruntime', size_mb: 12, kind: 'pip' },
    { facet: 'wd14', label: 'Anime tagger (WD14)', milestone: 'M4', dep: 'onnxruntime', dep_ok: true, model_ok: false, enabled: false, download: 'wd14', state: 'model not downloaded', model_name: 'wd-v1-4-moat-tagger-v2', source: 'Hugging Face · SmilingWolf', url: 'https://huggingface.co/SmilingWolf/wd-v1-4-moat-tagger-v2', size_mb: 326, kind: 'direct', dir: 'D:/ai-models/wd14', has_variants: true, variant_id: 'moat-v2' },
    { facet: 'clip', label: 'CLIP scene/clothing', milestone: 'M5', dep: 'open_clip', dep_ok: true, model_ok: true, enabled: true, download: 'clip', state: 'ready', model_name: 'CLIP ViT-B-32 (OpenAI)', source: 'open_clip · OpenAI', size_mb: 340, kind: 'library', dir: 'D:/ai-models/clip', has_variants: true, variant_id: 'vitb32' },
    { label: 'Semantic search (sqlite-vec)', milestone: 'M5/M7', dep: 'sqlite_vec', dep_ok: true, model_ok: true, enabled: true, download: null, state: 'ready', model_name: 'sqlite-vec extension', source: 'pip: sqlite-vec', size_mb: 1, kind: 'pip' },
    { facet: 'faces', label: 'Real faces (InsightFace)', milestone: 'M6', dep: 'insightface', dep_ok: false, model_ok: false, enabled: false, download: 'insightface', state: 'dep missing', model_name: 'buffalo_l (SCRFD + ArcFace)', source: 'InsightFace model zoo', size_mb: 326, kind: 'library', dir: 'D:/ai-models/insightface', has_variants: true, variant_id: 'buffalo_s' },
    { label: 'Learned tags', milestone: 'M7', dep: 'sklearn', dep_ok: true, model_ok: true, enabled: true, download: null, state: 'ready', model_name: 'linear head (trained locally)', source: 'pip: scikit-learn', size_mb: 0, kind: 'pip' },
    { facet: 'caption', label: 'Captioning (BLIP)', milestone: 'M8', dep: 'transformers', dep_ok: true, model_ok: false, enabled: false, download: 'caption', state: 'model not downloaded', model_name: 'BLIP image-captioning-base', source: 'Hugging Face · Salesforce', url: 'https://huggingface.co/Salesforce/blip-image-captioning-base', size_mb: 990, kind: 'library', dir: 'D:/ai-models/caption', has_variants: true, variant_id: 'blip-base' },
  ];
  const mockPersons: Person[] = [
    { id: 1, name: null, faces: 42, sample: 'D:/Pictures/person1/a.png', sample_id: 1 },
    { id: 2, name: 'Alex', faces: 18, sample: 'D:/Pictures/person2/b.png', sample_id: 2 },
    { id: 3, name: null, faces: 7, sample: 'D:/Pictures/person3/c.png', sample_id: 3 },
  ];
  // Seed one learned tag matching the "new vtuber" learned suggestions already
  // scattered through `seed` above (i % 5 === 0), so the Learned tags tab has
  // something to show without requiring a teach() round-trip first.
  const mockLearned: LearnedTagRow[] = [
    { tag_id: 9001, name: 'new vtuber', category: 'character', space: 'clip', method: 'centroid', n_pos: 5, n_neg: 0,
      threshold: 0.42, updated_at: Math.floor(Date.now() / 1000) - 1800,
      applied: seed.filter((s) => s.file.id % 5 === 0).length },
  ];
  const mockProgress: TagProgressRow[] = [
    { tag_id: 9002, name: 'summer festival', category: 'scene', n_pos: 2, n_neg: 0,
      updated_at: Math.floor(Date.now() / 1000) - 600 },
  ];
  const mockVariants: FacetVariants[] = [
    { facet: 'wd14', tier: 'low', recommended: 'moat-v2', selected: 'moat-v2', variants: [
      { id: 'moat-v2', label: 'MOAT v2 (balanced)', tier: 'low', size_mb: 326 },
      { id: 'convnext-v2', label: 'ConvNeXT v2', tier: 'low-mid', size_mb: 378 },
      { id: 'swinv2-v2', label: 'SwinV2 v2 (accurate)', tier: 'mid', size_mb: 377 },
      { id: 'eva02-large-v3', label: 'EVA02-Large v3 (best)', tier: 'high', size_mb: 1400 },
    ] },
    { facet: 'clip', tier: 'low', recommended: 'vitb32', selected: 'vitb32', variants: [
      { id: 'vitb32', label: 'ViT-B/32 (fast)', tier: 'low', size_mb: 340, dim: 512 },
      { id: 'vitb16', label: 'ViT-B/16', tier: 'low-mid', size_mb: 340, dim: 512 },
      { id: 'vitl14', label: 'ViT-L/14 (accurate)', tier: 'mid', size_mb: 890, dim: 768 },
      { id: 'vith14', label: 'ViT-H/14 (best)', tier: 'high', size_mb: 3900, dim: 1024 },
    ] },
    { facet: 'insightface', tier: 'low', recommended: 'buffalo_s', selected: 'buffalo_s', variants: [
      { id: 'buffalo_s', label: 'buffalo_s (light)', tier: 'low', size_mb: 100 },
      { id: 'buffalo_l', label: 'buffalo_l (accurate)', tier: 'mid', size_mb: 326 },
    ] },
    { facet: 'caption', tier: 'low', recommended: 'blip-base', selected: 'blip-base', variants: [
      { id: 'blip-base', label: 'BLIP base (fast)', tier: 'low', size_mb: 990 },
      { id: 'blip-large', label: 'BLIP large (accurate)', tier: 'mid', size_mb: 1900 },
    ] },
  ];
  const nowSec = Math.floor(Date.now() / 1000);
  const mockRoots: Root[] = [
    { id: 1, path: 'D:/Pictures', mode: 'include', recursive: true, enabled: true, files: 420, done: 418, pending: 2, errors: 0, last_indexed: nowSec - 3600 },
    { id: 2, path: 'D:/Pictures/WIP', mode: 'exclude', recursive: true, enabled: true, files: 0, done: 0, pending: 0, errors: 0, last_indexed: null },
    { id: 3, path: 'E:/Anime', mode: 'include', recursive: true, enabled: false, files: 80, done: 80, pending: 0, errors: 1, last_indexed: nowSec - 172800 },
  ];
  const mockErrors: (ErrorRow & { root_id: number })[] = [
    { id: 1, file_id: 9001, kind: 'infer', error: "RuntimeError('CUDA out of memory')",
      updated_at: nowSec - 900, path: 'E:/Anime/broken/huge.png', filename: 'huge.png', root_id: 3 },
  ];
  const mockExcludes: ExcludeRule[] = [
    { id: 1, pattern: '**/.git/**', enabled: true },
    { id: 2, pattern: '**/node_modules/**', enabled: true },
    { id: 3, pattern: '*.tmp', enabled: true },
  ];
  const mockCategories: Category[] = [];
  let mockPaused = false;
  let mockMode: 'auto' | 'manual' = 'auto';
  let dlProg: (e: DownloadProgress) => void = () => {};
  let dlDone: (e: DownloadDone) => void = () => {};
  let scanDoneCb: (e: {
    ok: boolean; root_id?: number | null; error?: string;
    added?: number; changed?: number; removed?: number; unchanged?: number; revived?: number;
  }) => void = () => {};
  let fileDoneCb: (e: {
    ok: boolean; action: 'reindex' | 'recaption' | 'retag'; path: string; error?: string;
  }) => void = () => {};
  // The real daemon runs these on its own thread and emits file_done when the
  // work finishes; the demo has no work to do, so fire on the next tick.
  const mockFileDone = (action: 'reindex' | 'recaption' | 'retag', path: string) =>
    setTimeout(() => fileDoneCb({ ok: true, action, path }), 0);
  const settings = new Map<string, string>([
    ['models_dir', 'D:/ai-models'],
    ['tier', ''],
    ['caption_model', 'Salesforce/blip-image-captioning-base'],
  ]);

  const fold = (x: string) => x.normalize('NFD').replace(/[̀-ͯะ-๎]/g, '');
  // Demo-only approximation of the real grammar (apps/desktop/src/main/search.js):
  // AND/NOT/wildcards/regex are supported per-term; OR (|) and <grouping> are
  // not (the mock only ever runs outside Electron, never the path the user
  // actually tests against — see search.js for the fully verified engine).
  const matches = (s: Seed, q: string, o?: SearchOpts): boolean => {
    // Mirror the Node grammar's tokenizer so quoted values with spaces work.
    const cased = (x: string) => {
      let y = o?.matchCase ? x : x.toLowerCase();
      if (!o?.matchDiacritics) y = fold(y);
      return y;
    };
    const terms = (q.match(
      /[!-][a-z][\w-]*:"[^"]*"|[!-][a-z][\w-]*:\S+|[a-z][\w-]*:"[^"]*"|[a-z][\w-]*:\S+|[!-]"[^"]*"|"[^"]*"|[!-]\S+|\S+/gi) || []).map(cased);
    const tagText = s.tags.map((t) => `${t.category}:${t.name} ${t.name}`).join(' ');
    // matchPath restricts the free-text haystack to the file path only.
    const hay = cased(o?.matchPath ? s.file.path : s.file.path + ' ' + tagText);
    const hit = (needle: string) =>
      o?.wholeWord
        ? new RegExp(`(^|[^\\w])${needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}([^\\w]|$)`).test(hay)
        : hay.includes(needle);
    const globToRe = (pat: string) =>
      new RegExp(pat.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.'), o?.matchCase ? '' : 'i');
    // Confidence display filter (§5.3 UX): mirrors search.js's rule — manual
    // tags (confidence=null) are never gated, only auto tags are.
    const passesConf = (tg: TagRow) =>
      o?.minConfidence == null || tg.confidence == null || tg.confidence >= o.minConfidence;
    return terms.every((raw) => {
      const neg = raw.startsWith('-') || raw.startsWith('!');
      let t = neg ? raw.slice(1) : raw;
      if (!t) return true;
      const c = t.indexOf(':');
      let positive: boolean;
      if (c > 0 && /^[a-z][\w-]*$/i.test(t.slice(0, c))) {
        const [field, val] = [t.slice(0, c), t.slice(c + 1).replace(/"/g, '')];
        positive = field === 'folder'
          ? cased(s.file.folder).includes(val)
          : s.tags.some((tg) => tg.category === field && cased(tg.name).includes(val) && passesConf(tg));
      } else if (o?.regex) {
        try { positive = new RegExp(t.replace(/^"|"$/g, ''), o?.matchCase ? '' : 'i').test(hay); }
        catch { positive = false; }
      } else if (!t.startsWith('"') && (t.includes('*') || t.includes('?'))) {
        positive = globToRe(t).test(hay);
      } else {
        positive = hit(t.replace(/^"|"$/g, ''));
      }
      return neg ? !positive : positive;
    });
  };

  const sortFiles = (arr: Seed[], opts?: SearchOpts) => {
    if (!opts?.sort) return arr;
    const key = opts.sort, dir = opts.dir === 'desc' ? -1 : 1;
    const val = (f: FileRow): any =>
      key === 'name' ? f.filename : key === 'path' ? f.path :
      key === 'size' ? f.size_bytes : key === 'dim' ? (f.width || 0) * (f.height || 0) :
      key === 'date' ? f.mtime : f.image_kind;
    return [...arr].sort((a, b) => (val(a.file) > val(b.file) ? dir : val(a.file) < val(b.file) ? -dir : 0));
  };

  const filtered = (q: string, o?: SearchOpts) =>
    (!q.trim() ? seed : seed.filter((s) => matches(s, q, o)));

  return {
    async pickFolder() { return ['D:/Pictures/NewFolder']; },
    async search(q, opts) {
      return sortFiles(filtered(q, opts), opts).slice(0, opts?.limit ?? 500).map((s) => s.file);
    },
    async count(q, opts) { return filtered(q, opts).length; },
    // Return fresh arrays (as the real IPC does) so signal .set() detects change.
    async tags(id, minConfidence) {
      const all = byId.get(id)?.tags ?? [];
      return (minConfidence == null
        ? all
        : all.filter((t) => t.confidence == null || t.confidence >= minConfidence)
      ).map((t) => ({ ...t }));
    },
    async addTag(id, category, name) {
      const s = byId.get(id)!;
      if (!s.tags.some((t) => t.category === category && t.name === name))
        s.tags.push({ category, name, source: 'manual', confidence: null });
      return [...s.tags];
    },
    async removeTag(id, category, name) {
      const s = byId.get(id)!;
      s.tags = s.tags.filter((t) => !(t.category === category && t.name === name));
      return [...s.tags];
    },
    async createCategory(name: string, color?: string | null) {
      const id = 100 + mockCategories.length;
      mockCategories.push({ id, name, color: color ?? null, is_builtin: 0 });
      return id;
    },
    async listCategories() {
      return [
        { id: 1, name: 'character', color: '#8b5cf6', is_builtin: 1 },
        { id: 2, name: 'series', color: '#6366f1', is_builtin: 1 },
        { id: 3, name: 'scene', color: '#ec4899', is_builtin: 1 },
        { id: 4, name: 'clothing', color: '#14b8a6', is_builtin: 1 },
        { id: 5, name: 'pose', color: '#f59e0b', is_builtin: 1 },
        { id: 6, name: 'general', color: null, is_builtin: 1 },
        ...mockCategories,
      ];
    },
    async fileDetail(id) {
      const s = byId.get(id);
      return s ? { ...s.detail, faces: s.detail.faces.map((f) => ({ ...f })), metadata: s.detail.metadata.map((m) => ({ ...m })) } : null;
    },
    async thumb(id) {
      const s = byId.get(id);
      return s ? mockThumb(s.file.filename.slice(0, 12), (id * 47) % 360) : null;
    },
    async fullImage(id) {
      const s = byId.get(id);
      // Demo stand-in for the real full-resolution read — same mock SVG, larger.
      return s ? mockThumb(s.file.filename, (id * 47) % 360).replaceAll('width="240" height="180"', 'width="1200" height="900"') : null;
    },
    async setOcr(id, text) {
      const s = byId.get(id); if (s) s.detail.ocr_text = text;
      return s ? { ...s.detail } : null;
    },
    async reindexFile(filePath: string) { mockFileDone('reindex', filePath); return { ok: true, started: true }; },
    async recaptionFile(filePath: string) {
      const s = [...byId.values()].find((x) => x.file.path === filePath);
      if (s) s.detail.caption = `(demo) regenerated description for ${s.file.filename}`;
      mockFileDone('recaption', filePath);
      return { ok: true, started: true };
    },
    async retagFile(filePath: string) { mockFileDone('retag', filePath); return { ok: true, started: true }; },
    onFileDone(cb) { fileDoneCb = cb; return () => { fileDoneCb = () => {}; }; },
    async openFile() { return ''; },
    async revealFile() { return; },
    async getSetting(key, fallback) { return settings.get(key) ?? fallback ?? null; },
    async setSetting(key, value) { settings.set(key, value); return value; },
    async bulkAddTag(ids, category, name) {
      ids.forEach((id) => this.addTag(id, category, name));
      return ids.length;
    },
    async bulkRemoveTag(ids, category, name) {
      ids.forEach((id) => this.removeTag(id, category, name));
      return ids.length;
    },
    async getAppVersion() { return '0.0.0-dev'; },
    async checkForUpdates() {
      return { ok: true, currentVersion: '0.0.0-dev', latestVersion: '0.0.0-dev', updateAvailable: false };
    },
    async openReleasePage() { return false; },
    indexer: {
      // Demo semantic ranking: score by keyword overlap with tags, stable order.
      async semantic(query, k = 20) {
        const q = query.toLowerCase();
        const scored = seed
          .map((s) => ({
            s,
            score: s.tags.reduce((n, t) => n + (q.includes(t.name) || t.name.includes(q.split(' ')[0]) ? 1 : 0), 0),
          }))
          .filter((x) => x.score > 0)
          .sort((a, b) => b.score - a.score)
          .slice(0, k);
        return {
          available: true,
          hits: scored.map((x) => ({
            id: x.s.file.id, path: x.s.file.path,
            filename: x.s.file.filename, distance: 1 - x.score / 5,
          })),
        };
      },
      async rescan() {
        setTimeout(() => scanDoneCb({
          ok: true, root_id: null, added: 3, changed: 1, removed: 0, unchanged: seed.length - 4,
        }), 300);
        return { started: true };
      },
      async rescanRoot(rootId: number) {
        setTimeout(() => scanDoneCb({
          ok: true, root_id: rootId, added: 0, changed: 0, removed: 0, unchanged: seed.length,
        }), 300);
        return { started: true };
      },
      onScanDone(cb) { scanDoneCb = cb; return () => { scanDoneCb = () => {}; }; },
      async addRoot(path: string, mode?: string) {
        mockRoots.push({ id: mockRoots.length + 1, path, mode: (mode as any) || 'include',
          recursive: true, enabled: true, files: 0 });
        return { ok: true };
      },
      async progress() {
        return { files_total: seed.length, files_done: seed.length,
          jobs: { done: seed.length },
          scan_done: seed.length, caption_done: seed.length, tag_done: seed.length,
          facets: { wd14: true, caption: true },
          paused: mockPaused, mode: mockMode };
      },
      async pause() { mockPaused = true; return { paused: true }; },
      async resume() { mockPaused = false; return { paused: false }; },
      async setMode(mode: 'auto' | 'manual') { mockMode = mode; return { mode }; },
      async retryErrors(fileId?: number) {
        if (fileId == null) return { requeued: 0 };
        const i = mockErrors.findIndex((e) => e.file_id === fileId);
        if (i >= 0) mockErrors.splice(i, 1);
        return { requeued: i >= 0 ? 1 : 0 };
      },
      async reindexAll() { return { queued: seed.length }; },
      async reindexRoot(rootId: number) {
        return { queued: Math.max(1, Math.floor(seed.length / mockRoots.length)), root_id: rootId };
      },
      async recaptionRoot(rootId: number) {
        return { ok: true, queued: Math.max(1, Math.floor(seed.length / mockRoots.length)), root_id: rootId };
      },
      async recaptionAll() { return { ok: true, queued: seed.length }; },
      async listErrors(rootId?: number) {
        return { errors: mockErrors.filter((e) => rootId == null || e.root_id === rootId)
          .map(({ root_id, ...rest }) => rest) };
      },
      async roots() {
        return { roots: mockRoots.map((r) => ({ ...r })), excludes: mockExcludes.map((e) => ({ ...e })) };
      },
      async addExclude(path: string) { return this.addRoot(path, 'exclude'); },
      async removeRoot(rootId: number) {
        const i = mockRoots.findIndex((r) => r.id === rootId);
        if (i >= 0) mockRoots.splice(i, 1); return { ok: true };
      },
      async toggleRoot(rootId: number, enabled: boolean) {
        const r = mockRoots.find((x) => x.id === rootId); if (r) r.enabled = enabled; return { ok: true };
      },
      async addExcludePattern(pattern: string) {
        mockExcludes.push({ id: mockExcludes.length + 1, pattern, enabled: true }); return { ok: true };
      },
      async removeExclude(ruleId: number) {
        const i = mockExcludes.findIndex((e) => e.id === ruleId);
        if (i >= 0) mockExcludes.splice(i, 1); return { ok: true };
      },
      async toggleExclude(ruleId: number, enabled: boolean) {
        const e = mockExcludes.find((x) => x.id === ruleId); if (e) e.enabled = enabled; return { ok: true };
      },
      async renameTag(category: string, oldName: string, newName: string) {
        let merged = false, files = 0;
        const exists = seed.some((s) => s.tags.some((t) => t.category === category && t.name === newName));
        for (const s of seed) {
          const has = s.tags.some((t) => t.category === category && t.name === oldName);
          if (has) {
            files++;
            if (exists) {
              s.tags = s.tags.filter((t) => !(t.category === category && t.name === oldName));
              merged = true;
            } else {
              s.tags = s.tags.map((t) => t.category === category && t.name === oldName ? { ...t, name: newName } : t);
            }
          }
        }
        return { ok: files > 0, merged, files, name: newName };
      },
      async listTags() {
        const counts = new Map<string, TagInfo>();
        for (const s of seed) for (const t of s.tags) {
          const k = t.category + ' ' + t.name;
          const e = counts.get(k) ?? { id: counts.size + 1, name: t.name, category: t.category, files: 0 };
          e.files++; counts.set(k, e);
        }
        return { tags: [...counts.values()].sort((a, b) => b.files - a.files) };
      },
      async learnStatus(category: string, name: string) {
        const n = seed.filter((s) => s.tags.some(
          (t) => t.category === category && t.name === name && (t.source === 'manual' || t.source === 'path'))).length;
        return { count: n };
      },
      async learn(category: string, name: string, space?: string) {
        const n = seed.filter((s) => s.tags.some(
          (t) => t.category === category && t.name === name && (t.source === 'manual' || t.source === 'path'))).length;
        if (n < 1) return { ok: false, error: 'no manual examples for this tag', count: n };
        // Demo: apply the learned tag as a suggestion to a few similar files.
        let applied = 0;
        for (const s of seed) {
          if (applied >= 8) break;
          if (!s.tags.some((t) => t.category === category && t.name === name)) {
            s.tags.push({ category, name, source: 'learned', confidence: 0.6 }); applied++;
          }
        }
        const existing = mockLearned.find((t) => t.category === category && t.name === name);
        if (existing) { existing.n_pos = n; existing.applied += applied; existing.updated_at = Math.floor(Date.now() / 1000); }
        else mockLearned.unshift({ tag_id: 9000 + mockLearned.length + 1, name, category, space: space || 'clip',
          method: 'centroid', n_pos: n, n_neg: 0,
          threshold: 0.42, updated_at: Math.floor(Date.now() / 1000), applied });
        return { ok: true, count: n, method: 'centroid', threshold: 0.42, n_pos: n, n_neg: 0, applied };
      },
      async learnConfirm(category: string, name: string, fileId: number) {
        // Mirrors the real backend (learned.confirm): source stays 'learned'
        // forever, only a durable `confirmed` marker is set — the tag must
        // keep showing "confirmed" on a later visit, not flip to a plain tag.
        const s = byId.get(fileId);
        if (s) s.tags = s.tags.map((t) =>
          t.category === category && t.name === name ? { ...t, confirmed: true } : t);
        return { ok: true };
      },
      async learnReject(category: string, name: string, fileId: number) {
        const s = byId.get(fileId);
        if (s) s.tags = s.tags.filter((t) => !(t.category === category && t.name === name && t.source === 'learned'));
        return { ok: true };
      },
      async rejectAutoTag(category: string, name: string, fileId: number) {
        const s = byId.get(fileId);
        if (s) s.tags = s.tags.filter((t) => !(t.category === category && t.name === name));
        return { ok: true };
      },
      async confirmAutoTag(category: string, name: string, fileId: number) {
        const s = byId.get(fileId);
        const tag = s?.tags.find((t) => t.category === category && t.name === name);
        if (s && tag) s.tags = s.tags.map((t) =>
          t.category === category && t.name === name ? { ...t, confirmed: true } : t);
        const reinforces = !!tag && (tag.source === 'wd14' || tag.source === 'clip' || tag.source === 'learned');
        let prog = mockProgress.find((p) => p.category === category && p.name === name);
        const trainedRow = mockLearned.find((t) => t.category === category && t.name === name);
        if (reinforces && !trainedRow) {
          if (!prog) { prog = { tag_id: 9500 + mockProgress.length, name, category, n_pos: 0, n_neg: 0, updated_at: null }; mockProgress.unshift(prog); }
          prog.n_pos++; prog.updated_at = Math.floor(Date.now() / 1000);
          if (prog.n_pos >= 5) {
            mockProgress.splice(mockProgress.indexOf(prog), 1);
            mockLearned.unshift({ tag_id: prog.tag_id, name, category, space: 'clip', method: 'centroid',
              n_pos: prog.n_pos, n_neg: prog.n_neg, threshold: 0.42, updated_at: prog.updated_at, applied: 0 });
          }
        }
        const nowTrained = mockLearned.find((t) => t.category === category && t.name === name);
        return { ok: true, n_pos: nowTrained?.n_pos ?? prog?.n_pos ?? 0, n_neg: nowTrained?.n_neg ?? prog?.n_neg ?? 0,
          trained: !!nowTrained, applied: nowTrained?.applied ?? 0, needed: 5, reinforces };
      },
      async listLearnedTags() {
        return { tags: mockLearned.map((t) => ({ ...t })), in_progress: mockProgress.map((p) => ({ ...p })), min_positives: 5 };
      },
      async learnForget(category: string, name: string) {
        // Mirrors learned.forget(): the auto-applied 'learned' rows and all
        // training state go, manual/base tagging for the same tag stays.
        const trained = mockLearned.findIndex((t) => t.category === category && t.name === name);
        const inProgress = mockProgress.findIndex((p) => p.category === category && p.name === name);
        if (trained < 0 && inProgress < 0) return { ok: false, error: 'no such tag' };
        let unapplied = 0;
        for (const s of seed) {
          const before = s.tags.length;
          s.tags = s.tags.filter(
            (t) => !(t.category === category && t.name === name && t.source === 'learned'));
          unapplied += before - s.tags.length;
        }
        const examples = (trained >= 0 ? mockLearned[trained].n_pos + mockLearned[trained].n_neg
          : mockProgress[inProgress].n_pos + mockProgress[inProgress].n_neg);
        if (trained >= 0) mockLearned.splice(trained, 1);
        if (inProgress >= 0) mockProgress.splice(inProgress, 1);
        return { ok: true, unapplied, examples_cleared: examples, was_trained: trained >= 0 };
      },
      async download(model: string, variant?: string) {
        // Simulate streaming: determinate for the onnx tagger, indeterminate for
        // weight-fetching engines; then mark the facet ready + fire done.
        // Demo mode doesn't model the applied-vs-downloaded distinction — a
        // download always "completes" the facet here regardless of variant.
        void variant;
        const indet = model !== 'wd14';
        let pct = 0;
        const finish = () => {
          mockFacets.forEach((f) => { if (f.download === model) { f.model_ok = true; f.state = 'ready'; } });
          dlDone({ model, ok: true });
        };
        const tick = () => {
          if (indet) { dlProg({ model, pct: null, indeterminate: true }); setTimeout(finish, 600); return; }
          pct = Math.min(100, pct + 20);
          dlProg({ model, pct, indeterminate: false });
          pct >= 100 ? setTimeout(finish, 150) : setTimeout(tick, 140);
        };
        setTimeout(tick, 100);
        return { started: true, model };
      },
      async installDependency(facet: string) {
        const key = `dep:${facet}`;
        setTimeout(() => {
          const f = mockFacets.find((x) => x.facet === facet);
          if (f) f.dep_ok = true;
          dlDone({ model: key, ok: true });
        }, 500);
        return { started: true, model: key };
      },
      async downloadStatus() { return { downloads: [] }; },
      onDownloadProgress(cb: (e: DownloadProgress) => void) { dlProg = cb; return () => { dlProg = () => {}; }; },
      onDownloadDone(cb: (e: DownloadDone) => void) { dlDone = cb; return () => { dlDone = () => {}; }; },
      async facets() { return mockFacets.map((f) => ({ ...f })); },
      async setFacetEnabled(facet: string, enabled: boolean) {
        const f = mockFacets.find((x) => x.facet === facet);
        if (!f) return { ok: false, error: 'unknown facet' };
        if (enabled && (!f.dep_ok || !f.model_ok)) return { ok: false, error: 'facet not ready' };
        f.enabled = enabled;
        return { ok: true, enabled };
      },
      async modelsDir() { return settings.get('models_dir') || 'C:/Users/you/.image-tagger/models'; },
      async variants() { return mockVariants.map((v) => ({ ...v, variants: v.variants.map((x) => ({ ...x })) })); },
      async modelState() {
        return {
          facets: mockFacets.map((f) => ({ ...f })),
          variants: mockVariants.map((v) => ({ ...v, variants: v.variants.map((x) => ({ ...x })) })),
          downloads: [],
          models_dir: settings.get('models_dir') || 'C:/Users/you/.image-tagger/models',
        };
      },
      async setVariant(facet: string, variant: string) {
        const fv = mockVariants.find((v) => v.facet === facet);
        const chosen = fv?.variants.find((x) => x.id === variant);
        if (fv && chosen) {
          fv.selected = variant;
          // reflect the new variant's name + size on the matching facet row
          const key = facet;
          mockFacets.forEach((f) => { if (f.download === key) { f.model_name = chosen.label; f.size_mb = chosen.size_mb; f.variant_id = variant; } });
        }
        const reindex = facet === 'clip';
        return { ok: true, reindex_needed: reindex };
      },
      async persons() { return mockPersons.map((p) => ({ ...p })); },
      async personFiles(id: number) {
        return seed.filter((s) => s.file.id % mockPersons.length === id % mockPersons.length)
          .slice(0, 6).map((s) => ({ id: s.file.id, path: s.file.path, filename: s.file.filename }));
      },
      async namePerson(id: number, name: string) {
        const p = mockPersons.find((x) => x.id === id); if (p) p.name = name; return { ok: true };
      },
      async mergePersons(src: number, dst: number) {
        const s = mockPersons.findIndex((x) => x.id === src);
        const d = mockPersons.find((x) => x.id === dst);
        if (s >= 0 && d) { d.faces += mockPersons[s].faces; mockPersons.splice(s, 1); }
        return { ok: true };
      },
      onProgress() { return () => {}; },
      onRestarted() { return () => {}; },
      onWarning() { return () => {}; },
      onStderr() { return () => {}; },
    },
  };
}
