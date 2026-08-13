import { Injectable, NgZone, inject, signal } from '@angular/core';
import { Api, FileDetail, FileRow, LearnSummary, ModelState, Person, Progress, SearchOpts, TagRow, getApi } from './api';

const folderOf = (p: string) => {
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return i >= 0 ? p.slice(0, i) : '';
};

/** Reactive facade over the read/write API (spec §8). Holds search state as
 * signals; the fast path stays synchronous in the Electron main process — this
 * only orchestrates calls and caches results for the view. */
@Injectable({ providedIn: 'root' })
export class LibraryService {
  private api: Api = getApi();
  private readonly zone = inject(NgZone);

  readonly query = signal('');
  readonly results = signal<FileRow[]>([]);
  readonly total = signal(0);
  readonly loading = signal(false);
  readonly selectedId = signal<number | null>(null);
  readonly selectedIds = signal<Set<number>>(new Set());
  readonly sort = signal<SearchOpts['sort'] | undefined>(undefined);
  readonly dir = signal<'asc' | 'desc'>('asc');
  readonly tags = signal<TagRow[]>([]);
  readonly detail = signal<FileDetail | null>(null);
  readonly thumbUri = signal<string | null>(null);
  readonly queryMs = signal(0);
  readonly semantic = signal(false);
  readonly progress = signal<Progress | null>(null);
  // Everything-style match toggles (§8.1). Off by default = fast, forgiving.
  readonly matchCase = signal(false);
  readonly wholeWord = signal(true);
  readonly matchPath = signal(false);
  readonly matchDiacritics = signal(false);
  // §8.1 "use regex" checkbox — off by default (a typo'd pattern shouldn't
  // silently break plain search); persisted like mediaType since it's a
  // per-user habit, not per-query state.
  readonly regexMode = signal(this.loadRegexMode());
  // Surfaces a bad regex (or any other thrown search error) near the search
  // bar instead of leaving the query looking like it just returned nothing.
  readonly queryError = signal('');
  // Confidence display filter (§5.3 UX): null = "All" — show everything the
  // storage floor kept. Raising it hides low-confidence auto tags from search's
  // cat: filters and from the preview pane's tag list; manual tags are never
  // gated (search.js / writes.js both special-case confidence IS NULL).
  readonly minConfidence = signal<number | null>(null);
  // Media-type filter (§12): videos are browse/search-only (no AI facets run
  // on them), so mixing them into results by default would be surprising —
  // default to images only, matching every user's experience before video
  // support existed at all.
  readonly mediaType = signal<'image' | 'video' | 'both'>(this.loadMediaType());
  // Set briefly when the daemon crashed/hung and the bridge auto-restarted it
  // (§7 resilience) — app.component.ts surfaces this so a recovered failure
  // is visible instead of indexing/search just silently having gone stale
  // for a while with no explanation.
  readonly restartNotice = signal<string | null>(null);
  // Stale-while-revalidate cache for the Models and People pages: both
  // components get destroyed/recreated on every navigation (the app switches
  // views via a signal, not a router), so their own local state always came
  // back empty and showed a loading spinner on every visit even when nothing
  // had changed. Caching here, on the singleton service that survives
  // navigation, lets each page show its last-known data immediately and
  // silently refetch in the background instead.
  readonly modelStateCache = signal<ModelState | null>(null);
  readonly personsCache = signal<Person[] | null>(null);
  readonly personAvatarsCache = signal<Map<number, string>>(new Map());
  // Rolling buffer of daemon-side trouble (§7 resilience) — warnings, raw
  // stderr, and restarts — that used to have no visible surface at all, so a
  // real failure just looked like indexing silently doing nothing.
  readonly log = signal<{ ts: number; level: 'warn' | 'error' | 'info'; message: string }[]>([]);
  private static readonly LOG_MAX = 300;

  private seq = 0;
  private selectionSeq = 0;
  private progressWasActive = false;
  private progressSearchTimer: ReturnType<typeof setTimeout> | null = null;
  private queryDebounceTimer: ReturnType<typeof setTimeout> | null = null;
  private restartNoticeTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    // Live indexing progress (§7/§12): daemon pushes events; poll once at start.
    this.api.indexer.onProgress((p) => this.zone.run(() => this.acceptProgress(p)));
    void this.api.indexer.progress().then((p) => this.acceptProgress(p)).catch(() => {});
    this.api.indexer.onRestarted((e) => this.zone.run(() => {
      this.restartNotice.set('The background indexer recovered from an unexpected stop — resuming…');
      if (this.restartNoticeTimer) clearTimeout(this.restartNoticeTimer);
      this.restartNoticeTimer = setTimeout(() => this.restartNotice.set(null), 8000);
      this.pushLog('error', `indexer restarted after exit code ${e.previousExitCode}`);
      void this.refreshProgress();
    }));
    this.api.indexer.onWarning((e) => this.zone.run(() => this.pushLog('warn', e.message)));
    this.api.indexer.onStderr((line) => this.zone.run(() => {
      for (const l of line.split('\n')) if (l.trim()) this.pushLog('error', l.trim());
    }));
    void this.api.getSetting('min_confidence', '').then((v) => {
      const n = v ? Number(v) : NaN;
      if (Number.isFinite(n)) this.minConfidence.set(n);
    });
    void this.runSearch();
  }

  private pushLog(level: 'warn' | 'error' | 'info', message: string) {
    const entry = { ts: Date.now(), level, message };
    this.log.update((l) => [...l, entry].slice(-LibraryService.LOG_MAX));
  }

  private activeJobs(p: Progress): number {
    return (p.jobs['queued'] ?? 0) + (p.jobs['running'] ?? 0);
  }

  private acceptProgress(p: Progress) {
    const previous = this.progress();
    const active = this.activeJobs(p) > 0;
    const libraryChanged = previous != null &&
      (previous.files_total !== p.files_total || previous.files_done !== p.files_done);
    const workFinished = this.progressWasActive && !active;
    this.progress.set(p);
    this.progressWasActive = active;
    if (libraryChanged || workFinished) this.scheduleProgressSearch();
  }

  private scheduleProgressSearch() {
    if (this.progressSearchTimer != null) return;
    this.progressSearchTimer = setTimeout(() => {
      this.progressSearchTimer = null;
      void this.runSearch();
    }, 150);
  }

  private reconcileSelection(rows: FileRow[]) {
    const visible = new Set(rows.map((row) => row.id));
    this.selectedIds.set(new Set([...this.selectedIds()].filter((id) => visible.has(id))));
    const selected = this.selectedId();
    if (selected != null && !visible.has(selected)) {
      this.selectedId.set(null);
      this.tags.set([]);
      this.detail.set(null);
      this.thumbUri.set(null);
    }
  }

  async runSearch(): Promise<void> {
    const q = this.query().trim();
    const mine = ++this.seq;
    this.loading.set(true);
    this.queryError.set('');
    const t0 = performance.now();
    try {
      if (this.semantic()) {
        if (!q) throw new Error('Enter a description to use semantic search.');
        const res = await this.api.indexer.semantic(q, 200);
        if (mine !== this.seq) return;
        if (!res.available) throw new Error(res.reason || 'Semantic search is not available.');
        const rows: FileRow[] = res.hits.map((h) => ({
          id: h.id, path: h.path, filename: h.filename, folder: folderOf(h.path),
          image_kind: null, width: null, height: null, size_bytes: null, mtime: null,
        }));
        this.results.set(rows);
        this.total.set(rows.length);
        this.reconcileSelection(rows);
      } else {
        const opts: SearchOpts = {
          limit: 1000, sort: this.sort(), dir: this.dir(),
          matchCase: this.matchCase(), wholeWord: this.wholeWord(),
          matchPath: this.matchPath(), matchDiacritics: this.matchDiacritics(),
          minConfidence: this.minConfidence() ?? undefined,
          mediaType: this.mediaType(), regex: this.regexMode(),
        };
        const [rows, total] = await Promise.all([this.api.search(q, opts), this.api.count(q, opts)]);
        if (mine !== this.seq) return; // a newer keystroke superseded this one
        this.results.set(rows);
        this.total.set(total);
        this.reconcileSelection(rows);
      }
    } catch (e) {
      if (mine !== this.seq) return;
      this.queryError.set(e instanceof Error ? e.message : String(e));
      this.results.set([]); this.total.set(0);
      this.reconcileSelection([]);
    } finally {
      if (mine === this.seq) {
        this.queryMs.set(Math.round((performance.now() - t0) * 10) / 10);
        this.loading.set(false);
      }
    }
  }

  toggleSemantic() { this.semantic.set(!this.semantic()); void this.runSearch(); }

  toggleMatch(opt: 'matchCase' | 'wholeWord' | 'matchPath' | 'matchDiacritics') {
    this[opt].set(!this[opt]()); void this.runSearch();
  }

  toggleRegex() {
    this.regexMode.set(!this.regexMode());
    try { localStorage.setItem('imageTagger.regexMode', this.regexMode() ? '1' : '0'); } catch { /* storage unavailable */ }
    void this.runSearch();
  }
  private loadRegexMode(): boolean {
    try { return localStorage.getItem('imageTagger.regexMode') === '1'; } catch { return false; }
  }

  setMinConfidence(v: number | null) {
    this.minConfidence.set(v);
    void this.api.setSetting('min_confidence', v == null ? '' : String(v));
    void this.runSearch();
    void this.reloadTags();
  }

  setMediaType(v: 'image' | 'video' | 'both') {
    this.mediaType.set(v);
    try { localStorage.setItem('imageTagger.mediaType', v); } catch { /* storage unavailable */ }
    void this.runSearch();
  }
  private loadMediaType(): 'image' | 'video' | 'both' {
    try {
      const v = localStorage.getItem('imageTagger.mediaType');
      if (v === 'image' || v === 'video' || v === 'both') return v;
    } catch { /* storage unavailable */ }
    return 'image';
  }

  // The signal updates synchronously — so anything bound to it (the search
  // box's [value], "search this person" jumps, tab-switch persistence) always
  // reflects exactly what's set, with no lag. Only the actual DB round-trip
  // is debounced, so fast typing doesn't fire a query per keystroke (§8.1).
  setQuery(q: string) {
    this.query.set(q);
    if (this.queryDebounceTimer != null) clearTimeout(this.queryDebounceTimer);
    this.queryDebounceTimer = setTimeout(() => {
      this.queryDebounceTimer = null;
      void this.runSearch();
    }, 80);
  }

  toggleSort(col: NonNullable<SearchOpts['sort']>) {
    if (this.sort() === col) this.dir.set(this.dir() === 'asc' ? 'desc' : 'asc');
    else { this.sort.set(col); this.dir.set('asc'); }
    void this.runSearch();
  }

  async select(id: number, opts: { additive?: boolean } = {}) {
    if (opts.additive) {
      const s = new Set(this.selectedIds());
      s.has(id) ? s.delete(id) : s.add(id);
      this.selectedIds.set(s);
      if (!s.has(id)) {
        const fallback = [...s].at(-1) ?? null;
        if (fallback == null) this.clearSelection();
        else await this.loadSelection(fallback);
        return;
      }
    } else {
      this.selectedIds.set(new Set([id]));
    }
    await this.loadSelection(id);
  }

  async selectMany(ids: Iterable<number>, focusId: number | null,
                   opts: { additive?: boolean; load?: boolean } = {}) {
    const next = opts.additive ? new Set(this.selectedIds()) : new Set<number>();
    for (const id of ids) next.add(id);
    this.selectedIds.set(next);
    if (focusId == null || !next.has(focusId)) {
      if (!next.size) this.clearSelection();
      return;
    }
    this.selectedId.set(focusId);
    if (opts.load !== false) await this.loadSelection(focusId);
  }

  clearSelection() {
    this.selectionSeq++;
    this.selectedIds.set(new Set());
    this.selectedId.set(null);
    this.tags.set([]);
    this.detail.set(null);
    this.thumbUri.set(null);
  }

  private async loadSelection(id: number) {
    const mine = ++this.selectionSeq;
    this.selectedId.set(id);
    // Load tags immediately (cheap); detail + thumbnail async so the grid stays
    // responsive (§8.2). Guard against a newer selection superseding this one.
    this.detail.set(null);
    this.thumbUri.set(null);
    const tags = await this.api.tags(id, this.minConfidence() ?? undefined);
    if (this.selectedId() === id && mine === this.selectionSeq) this.tags.set(tags);
    void this.api.fileDetail(id).then((d) => {
      if (this.selectedId() === id && mine === this.selectionSeq) this.detail.set(d);
    }).catch(() => {});
    void this.api.thumb(id).then((t) => {
      if (this.selectedId() === id && mine === this.selectionSeq) this.thumbUri.set(t);
    }).catch(() => {});
  }

  async saveOcr(text: string) {
    const id = this.selectedId(); if (id == null) return;
    this.detail.set(await this.api.setOcr(id, text));
  }

  // Full-resolution image for the lightbox (§8.2 "larger render"); loaded lazily
  // only when the user clicks to enlarge, not part of selection/preview loading.
  fullImage(fileId: number) { return this.api.fullImage(fileId); }

  // Thumbnail for an arbitrary file id, independent of the current selection
  // (§8.2 multi-select grid preview) — unlike loadSelection()'s thumbUri,
  // which only ever tracks the single focused row.
  thumb(fileId: number) { return this.api.thumb(fileId); }

  // reindexFile/recaptionFile/retagFile always bypass Pause Tagger (§12) and
  // touch only the one file. The daemon runs them on its own thread (its RPC
  // loop is single-threaded and must stay answerable — a blocked loop gets the
  // whole daemon killed by the heartbeat watchdog), so the call returns as
  // soon as the work has *started* and the outcome arrives as a file_done
  // event. Same shape as rescan()/waitForScanDone() above.
  private waitForFileDone(path: string, timeoutMs = 10 * 60_000) {
    return new Promise<{ ok: boolean; error?: string }>((resolve) => {
      const timer = setTimeout(() => {
        off();
        resolve({ ok: false, error: 'timed out waiting for the file to finish' });
      }, timeoutMs);
      const off = this.api.onFileDone((e) => {
        if (e.path !== path) return;  // another file's action, keep waiting
        clearTimeout(timer);
        off();
        resolve({ ok: e.ok, error: e.error });
      });
    });
  }

  async reindexSelected(): Promise<{
    ok: boolean; removed?: boolean; error?: string;
  }> {
    const f = this.results().find((x) => x.id === this.selectedId());
    if (!f) return { ok: false };
    const id = f.id;
    // Subscribe before starting so a fast action can't finish first.
    const done = this.waitForFileDone(f.path);
    const started = await this.api.reindexFile(f.path);
    if (!started.ok) return started;
    const result = await done;
    await this.refreshProgress();
    await this.runSearch();
    if (this.results().some((row) => row.id === id)) await this.select(id);
    return result;
  }

  // Narrower sibling of reindexSelected() (§11): regenerates just the caption,
  // so it doesn't need to re-run search or touch OCR/wd14/clip/faces.
  async recaptionSelected(): Promise<{ ok: boolean; error?: string }> {
    const f = this.results().find((x) => x.id === this.selectedId());
    if (!f) return { ok: false };
    const id = f.id;
    const done = this.waitForFileDone(f.path);
    const started = await this.api.recaptionFile(f.path);
    if (!started.ok) return started;
    const result = await done;
    await this.refreshProgress();
    if (result.ok && this.selectedId() === id) this.detail.set(await this.api.fileDetail(id));
    return result;
  }

  // Narrower sibling of reindexSelected() (per-model tag caching): regenerates
  // just the WD14 tags for one file, bypassing the per-model cache -- the
  // "I don't trust the cached result, redo this one now" override.
  async retagSelected(): Promise<{ ok: boolean; error?: string }> {
    const f = this.results().find((x) => x.id === this.selectedId());
    if (!f) return { ok: false };
    const id = f.id;
    const done = this.waitForFileDone(f.path);
    const started = await this.api.retagFile(f.path);
    if (!started.ok) return started;
    const result = await done;
    await this.refreshProgress();
    if (result.ok && this.selectedId() === id) {
      this.detail.set(await this.api.fileDetail(id));
      this.tags.set(await this.api.tags(id, this.minConfidence() ?? undefined));
    }
    return result;
  }

  openSelected() {
    const f = this.results().find((x) => x.id === this.selectedId());
    if (f) void this.api.openFile?.(f.path);
  }

  openFile(file: FileRow) { void this.api.openFile?.(file.path); }
  revealFile(file: FileRow) { void this.api.revealFile?.(file.path); }
  copyPath(file: FileRow) { return this.api.copyText?.(file.path) ?? navigator.clipboard.writeText(file.path).then(() => true); }

  async reindexFiles(ids: Iterable<number>): Promise<number> {
    const wanted = new Set(ids);
    const files = this.results().filter((file) => wanted.has(file.id));
    let queued = 0;
    for (const file of files) {
      const result = await this.api.reindexFile(file.path);
      if (result.ok) queued++;
    }
    await this.refreshProgress();
    return queued;
  }

  listCategories() { return this.api.listCategories(); }
  fileTags(id: number) { return this.api.tags(id); }
  fileDetail(id: number) { return this.api.fileDetail(id); }
  createCategory(name: string, color?: string | null) { return this.api.createCategory(name, color); }
  renameTag(category: string, oldName: string, newName: string) {
    return this.api.indexer.renameTag(category, oldName, newName);
  }

  async addTag(category: string, name: string) {
    const id = this.selectedId();
    if (id == null) return;
    this.tags.set(await this.api.addTag(id, category, name));
  }

  async removeTag(category: string, name: string) {
    const id = this.selectedId();
    if (id == null) return;
    this.tags.set(await this.api.removeTag(id, category, name));
  }

  // For wd14/clip auto-tags specifically (§9): unlike removeTag, this is
  // durable across reindex/rescan (the daemon records the rejection so
  // write_auto_tags never re-adds it) and feeds the removal back into the
  // few-shot learner as a negative example.
  async rejectAutoTag(category: string, name: string, source?: string) {
    const id = this.selectedId();
    if (id == null) return { ok: false };
    const r = await this.api.indexer.rejectAutoTag(category, name, id, source);
    if (r.ok && this.selectedId() === id) {
      this.tags.set(await this.api.tags(id, this.minConfidence() ?? undefined));
    }
    return r;
  }

  async confirmAutoTag(category: string, name: string) {
    const id = this.selectedId();
    if (id == null) return { ok: false };
    const r = await this.api.indexer.confirmAutoTag(category, name, id);
    if (r.ok && this.selectedId() === id) {
      this.tags.set(await this.api.tags(id, this.minConfidence() ?? undefined));
    }
    return r;
  }

  async bulkAdd(category: string, name: string): Promise<number> {
    const ids = [...this.selectedIds()];
    if (!ids.length) return 0;
    const n = await this.api.bulkAddTag(ids, category, name);
    if (this.selectedId() != null) this.tags.set(await this.api.tags(this.selectedId()!));
    return n;
  }

  async bulkTrain(category: string, name: string): Promise<LearnSummary & { tagged: number }> {
    const tagged = await this.bulkAdd(category, name);
    if (!tagged) return { ok: false, error: 'no images selected', count: 0, tagged: 0 };
    const learned = await this.learn(category, name);
    await this.runSearch();
    return { ...learned, tagged };
  }

  async tagFacesSelected(name: string): Promise<{ faces: number; people: number }> {
    const personIds = new Set<number>();
    let faces = 0;
    for (const id of this.selectedIds()) {
      const detail = await this.api.fileDetail(id);
      for (const face of detail?.faces ?? []) {
        faces++;
        if (face.person_id != null) personIds.add(face.person_id);
      }
    }
    for (const personId of personIds) await this.api.indexer.namePerson(personId, name);
    const selected = this.selectedId();
    if (selected != null) await this.loadSelection(selected);
    return { faces, people: personIds.size };
  }

  async pauseIndexing() { const r = await this.api.indexer.pause(); await this.refreshProgress(); return r; }
  async resumeIndexing() { const r = await this.api.indexer.resume(); await this.refreshProgress(); return r; }
  async setMode(mode: 'auto' | 'manual') { const r = await this.api.indexer.setMode(mode); await this.refreshProgress(); return r; }
  // rescan/rescan_root now run on a background thread in the daemon and
  // report back via a "scan_done" event instead of the RPC response, so the
  // command loop stays free to answer other requests (e.g. the Sources
  // page's roots query) while a slow scan is in progress. Wrap that up here
  // so callers still just get a promise that resolves with the real result,
  // same as before.
  private waitForScanDone(timeoutMs = 10 * 60_000) {
    return new Promise<{ added: number; changed: number; removed: number; unchanged: number; revived?: number }>(
      (resolve, reject) => {
        const timer = setTimeout(() => { off(); reject(new Error('scan timed out')); }, timeoutMs);
        const off = this.api.indexer.onScanDone((e) => {
          clearTimeout(timer);
          off();
          if (e.ok) {
            resolve({ added: e.added ?? 0, changed: e.changed ?? 0, removed: e.removed ?? 0,
                      unchanged: e.unchanged ?? 0, revived: e.revived });
          } else {
            reject(new Error(e.error || 'scan failed'));
          }
        });
      });
  }

  async rescan() {
    const started = await this.api.indexer.rescan();
    if (!started.started) throw new Error(started.error || 'could not start scan');
    const r = await this.waitForScanDone();
    await this.refreshProgress();
    await this.runSearch();
    return r;
  }
  async rescanRoot(rootId: number) {
    const started = await this.api.indexer.rescanRoot(rootId);
    if (!started.started) throw new Error(started.error || 'could not start scan');
    const r = await this.waitForScanDone();
    await this.refreshProgress();
    await this.runSearch();
    return { ...r, root_id: rootId };
  }
  async retryErrors(fileId?: number) { const r = await this.api.indexer.retryErrors(fileId); await this.refreshProgress(); return r; }
  async reindexAll() { const r = await this.api.indexer.reindexAll(); await this.refreshProgress(); return r; }
  async reindexRoot(rootId: number) {
    const r = await this.api.indexer.reindexRoot(rootId);
    await this.refreshProgress();
    return r;
  }
  async recaptionRoot(rootId: number) {
    const r = await this.api.indexer.recaptionRoot(rootId);
    if (!r.ok) throw new Error(r.error || 'could not queue descriptions');
    await this.refreshProgress();
    return r;
  }
  // Explicit "↻ Regen all captions" (Sources page): force-redoes every
  // file's caption with whichever model is active. Unlike reindexAll, a
  // plain caption-model switch no longer does this automatically.
  async recaptionAll() {
    const r = await this.api.indexer.recaptionAll();
    if (!r.ok) throw new Error(r.error || 'could not queue captions');
    await this.refreshProgress();
    return r;
  }
  listErrors(rootId?: number) { return this.api.indexer.listErrors(rootId); }
  async refreshProgress() {
    try { this.acceptProgress(await this.api.indexer.progress()); } catch { /* daemon offline */ }
  }

  async reloadTags() {
    const id = this.selectedId();
    if (id != null) this.tags.set(await this.api.tags(id, this.minConfidence() ?? undefined));
  }

  listTags() { return this.api.indexer.listTags(); }
  learnStatus(category: string, name: string) { return this.api.indexer.learnStatus(category, name); }
  learn(category: string, name: string, space?: string) {
    return this.api.indexer.learn(category, name, space);
  }
  learnConfirm(category: string, name: string, fileId: number) {
    return this.api.indexer.learnConfirm(category, name, fileId);
  }
  learnReject(category: string, name: string, fileId: number) {
    return this.api.indexer.learnReject(category, name, fileId);
  }

  getSetting(key: string, fallback: string | null = null) {
    return this.api.getSetting(key, fallback);
  }
  setSetting(key: string, value: string) { return this.api.setSetting(key, value); }

  // Update check (§ new, see api.ts's UpdateCheckResult / updater.js): not an
  // auto-updater — just tells the Settings page whether a newer GitHub
  // Release exists, and opens the official release page for the user to
  // download/run themselves through the same, already-tested install flow.
  getAppVersion() { return this.api.getAppVersion?.() ?? Promise.resolve('0.0.0'); }
  checkForUpdates(force = false) {
    return this.api.checkForUpdates?.(force) ??
      Promise.resolve({ ok: false, error: 'update check is not available' });
  }
  openReleasePage(url: string) { return this.api.openReleasePage?.(url) ?? Promise.resolve(false); }
}
