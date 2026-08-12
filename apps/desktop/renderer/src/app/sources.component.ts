import { Component, effect, inject, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ErrorRow, ExcludeRule, Root, getApi } from './api';
import { LibraryService } from './library.service';

/** Sources / scan scope (spec §7.0): the include/exclude drives & folders that
 * define what gets indexed and searched, plus glob exclude patterns. Add a
 * folder as include or exclude, toggle without deleting, and drive a manual
 * rescan — "most-specific wins", so include D:\Pictures but exclude its \WIP. */
@Component({
  selector: 'app-sources',
  standalone: true,
  imports: [DecimalPipe],
  template: `
    <div class="wrap">
      <div class="top">
        <div>
          <h2>Sources</h2>
          <div class="sub">Which drives &amp; folders get indexed. Excludes always beat includes at equal or deeper depth.</div>
        </div>
        <div class="topbtns">
          <button class="primary" (click)="rescan()" [disabled]="rescanning()" data-testid="rescan">
            {{ rescanning() ? 'rescanning…' : '↻ Rescan now' }}
          </button>
          <button (click)="reindexAll()" [disabled]="reindexingAll()"
                  title="re-run tagging/caption/OCR for every already-indexed file with the current models"
                  data-testid="reindex-all">
            {{ reindexingAll() ? 're-indexing…' : '↻ Reindex' }}
          </button>
        </div>
      </div>

      @if (rescanMsg()) { <div class="msg" data-testid="rescan-msg">{{ rescanMsg() }}</div> }
      @if (reindexAllMsg()) { <div class="msg" data-testid="reindex-all-msg">{{ reindexAllMsg() }}</div> }

      @let ovp = lib.progress();
      @if (ovp) {
        <div class="ovbar" data-testid="overall-progress">
          <div class="ovtrack" [class.paused]="ovp.paused">
            <div class="ovfill" [style.width.%]="ovp.files_total ? (ovp.files_done / ovp.files_total * 100) : 0"></div>
          </div>
          <span class="ovlabel">
            {{ ovp.paused ? '⏸ paused' : '● indexing' }}
            {{ ovp.files_done | number }}/{{ ovp.files_total | number }}
            @if (!ovp.paused && ovp.current && ovp.current !== 'idle') { <span class="ovcurrent">· {{ ovp.current }}</span> }
          </span>
        </div>
      }

      <div class="addbar">
        <input #p placeholder="D:\\Pictures  or  E:\\Anime" [value]="draft()"
               (input)="draft.set($any($event.target).value)" data-testid="root-input" />
        <button (click)="browse()">Browse…</button>
        <button (click)="add(p, 'include')" data-testid="add-include">+ Include</button>
        <button class="excl" (click)="add(p, 'exclude')" data-testid="add-exclude">− Exclude</button>
      </div>

      <div class="table-scroll">
      <table data-testid="roots-table">
        <thead><tr><th>Folder / drive</th><th>Mode</th><th>Files</th><th>Indexed</th><th>Last indexed</th><th>Enabled</th><th>Actions</th></tr></thead>
        <tbody>
          @for (r of roots(); track r.id) {
            <tr data-testid="root-row">
              <td class="path">{{ r.path }}</td>
              <td><span class="mode m-{{ r.mode }}">{{ r.mode }}</span></td>
              <td class="num">{{ r.files | number }}</td>
              <td class="num indexcell">
                @if (r.mode === 'exclude') { <span class="dim">—</span> }
                @else if (rescanningRoot() === r.id) {
                  <div class="rowbar indet"><div class="stripe"></div></div>
                  <span class="rowlabel">scanning…</span>
                } @else if (r.files) {
                  <div class="rowbar">
                    <div class="rowfill" [style.width.%]="r.files ? ((r.done ?? 0) / r.files * 100) : 0"></div>
                  </div>
                  <span class="rowlabel" [class.warn]="(r.pending || 0) > 0" data-testid="root-indexed">{{ r.done | number }}/{{ r.files | number }}</span>
                  <div class="rowchips">
                    @if ((r.pending || 0) > 0) { <span class="chip pend">{{ r.pending }} pending</span> }
                    @if ((r.errors || 0) > 0) {
                      <button class="chip err-btn" (click)="toggleErrors(r)" data-testid="root-errors-toggle">
                        {{ r.errors }} err {{ errorsOpenFor() === r.id ? '▴' : '▾' }}
                      </button>
                    }
                  </div>
                } @else { <span class="dim">not indexed</span> }
              </td>
              <td class="when" data-testid="root-when">{{ r.mode === 'exclude' ? '—' : ago(r.last_indexed) }}</td>
              <td>
                <input type="checkbox" [checked]="r.enabled"
                       (change)="toggle(r, $any($event.target).checked)" data-testid="root-toggle" />
              </td>
              <td class="actions">
                @if (r.mode === 'include') {
                  <button (click)="rescanRoot(r)" [disabled]="!r.enabled || rescanningRoot() === r.id"
                          title="scan only this folder or drive" data-testid="root-rescan">
                    {{ rescanningRoot() === r.id ? 'scanning…' : '↻ Scan' }}
                  </button>
                  <button (click)="reindexRoot(r)" [disabled]="!r.enabled || reindexingRoot() === r.id"
                          title="re-run tagging/caption/OCR for every already-indexed file in this folder"
                          data-testid="root-index">
                    {{ reindexingRoot() === r.id ? 'indexing…' : '↻ Index' }}
                  </button>
                  <button (click)="recaptionRoot(r)" [disabled]="!r.enabled || recaptioningRoot() === r.id"
                          title="regenerate descriptions only for every indexed file in this folder"
                          data-testid="root-desc">
                    {{ recaptioningRoot() === r.id ? 'describing…' : '↻ Desc' }}
                  </button>
                }
                <button class="x" (click)="remove(r)" title="remove" data-testid="root-remove">×</button>
              </td>
            </tr>
            @if (errorsOpenFor() === r.id) {
              <tr class="error-panel">
                <td colspan="7">
                  @if (rootErrorsLoading()) {
                    <div class="dim">loading errors…</div>
                  } @else if (!rootErrors().length) {
                    <div class="dim">no error details found (they may have been retried already)</div>
                  } @else {
                    <div class="errlist">
                      @for (e of rootErrors(); track e.id) {
                        <div class="errrow" data-testid="error-row">
                          <span class="ekind">{{ e.kind }}</span>
                          <span class="efile" [title]="e.path">{{ e.filename }}</span>
                          <span class="emsg">{{ e.error }}</span>
                          <button (click)="retryOne(e)" [disabled]="retrying().has(e.file_id)">
                            {{ retrying().has(e.file_id) ? 'retrying…' : 'retry' }}
                          </button>
                        </div>
                      }
                    </div>
                  }
                </td>
              </tr>
            }
          }
          @if (!roots().length) {
            <tr>
              <td colspan="7" class="empty">
                @if (!hasLoaded()) {
                  <div class="loadbar" data-testid="loading"><div class="stripe"></div></div>
                  <div class="loadingtext">Loading sources…</div>
                } @else if (loadError()) {
                  <div class="errbanner" data-testid="load-error">
                    Couldn't load sources: {{ loadError() }}
                    <button (click)="refresh()">Retry</button>
                  </div>
                } @else {
                  No roots yet — add a folder to start indexing.
                }
              </td>
            </tr>
          }
        </tbody>
      </table>
      </div>

      <div class="logsec">
        <button class="logtoggle" (click)="logOpen.set(!logOpen())" data-testid="log-toggle">
          {{ logOpen() ? '▾' : '▸' }} Indexer log ({{ lib.log().length }})
        </button>
        @if (logOpen()) {
          <div class="logbox" data-testid="log-box">
            @if (!lib.log().length) {
              <div class="dim">No warnings or errors reported yet.</div>
            }
            @for (e of lib.log().slice().reverse(); track $index) {
              <div class="logline" [class.lg-warn]="e.level === 'warn'" [class.lg-error]="e.level === 'error'">
                <span class="lts">{{ logTime(e.ts) }}</span> {{ e.message }}
              </div>
            }
          </div>
        }
      </div>

      <h3>Exclude patterns</h3>
      <div class="sub">Skipped anywhere under an included root — globs like <code>**/node_modules/**</code> or <code>*.tmp</code>.</div>
      <div class="addbar">
        <input #pat placeholder="**/private/**" data-testid="pattern-input" />
        <button (click)="addPattern(pat)" data-testid="add-pattern">+ Add pattern</button>
      </div>
      <div class="pats">
        @for (e of excludes(); track e.id) {
          <span class="pat" [class.off]="!e.enabled" data-testid="pattern-row">
            <input type="checkbox" [checked]="e.enabled"
                   (change)="togglePattern(e, $any($event.target).checked)" />
            <code>{{ e.pattern }}</code>
            <button class="x" (click)="removePattern(e)" title="remove">×</button>
          </span>
        }
      </div>
    </div>
  `,
  styles: [`
    :host { display: block; overflow: auto; height: 100%; min-width: 0; }
    .wrap { padding: clamp(16px, 2vw, 30px); width: 100%; box-sizing: border-box; }
    .top { display: flex; align-items: flex-start; gap: 18px; }
    h2 { margin: 0; } h3 { margin: 22px 0 2px; }
    .sub { color: var(--fg-dim); font-size: 12px; margin-top: 3px; }
    .sub code, .pat code { background: var(--bg-2); padding: 1px 5px; border-radius: 4px; }
    .topbtns { display: flex; align-items: center; gap: 8px; margin-left: auto; }
    .primary { background: var(--accent); color: #fff; border: 0;
               padding: 8px 14px; border-radius: 8px; white-space: nowrap; cursor: pointer; }
    .topbtns button:not(.primary) { padding: 8px 14px; border-radius: 8px; white-space: nowrap;
               border: 1px solid var(--border); background: var(--bg-2); color: var(--fg); cursor: pointer;
               transition: background .12s, border-color .12s; }
    .topbtns button:not(.primary):hover:not(:disabled) { background: var(--sel); border-color: var(--accent); }
    .topbtns button:disabled { opacity: .6; cursor: default; }
    .msg { margin: 12px 0 0; padding: 8px 12px; border-radius: 8px;
           background: var(--tag-manual); font-size: 12px; }
    .addbar { display: flex; gap: 8px; margin: 14px 0 6px; }
    .addbar input { flex: 1; }
    .addbar .excl { color: #b3261e; }
    .table-scroll { width: 100%; overflow-x: auto; }
    table { width: 100%; min-width: 820px; border-collapse: collapse; margin-top: 10px; }
    th { text-align: left; font-size: 11px; text-transform: uppercase; color: var(--fg-dim);
         border-bottom: 1px solid var(--border); padding: 6px 8px; }
    td { padding: 8px; border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent); }
    .path { width: 38%; font-weight: 500; word-break: break-all; }
    .num { text-align: right; color: var(--fg-dim); font-variant-numeric: tabular-nums; white-space: nowrap; }
    .dim { color: var(--fg-dim); }
    .when { color: var(--fg-dim); font-size: 12px; white-space: nowrap; }
    .actions { white-space: nowrap; display: flex; align-items: center; gap: 6px; }
    .actions button { font-size: 12px; padding: 6px 11px; }
    .mode { font-size: 11px; padding: 1px 8px; border-radius: 999px; }
    .m-include { background: var(--tag-manual); } .m-exclude { background: var(--tag-path); }
    .empty { color: var(--fg-dim); text-align: center; padding: 18px; }
    /* Right after launch the daemon may still be starting up, so the very
       first fetch here can take a moment (or, rarely, fail outright) --
       without this the table just showed "No roots yet" indistinguishably
       from a genuinely empty library. */
    .loadbar { position: relative; height: 4px; width: 220px; margin: 0 auto;
               border-radius: 999px; background: var(--bg-2); overflow: hidden; }
    .loadbar .stripe { position: absolute; inset: 0; opacity: .5;
      background: repeating-linear-gradient(45deg, var(--accent) 0 8px, transparent 8px 16px);
      animation: slide 1s linear infinite; }
    .loadingtext { margin-top: 8px; }
    .errbanner { display: inline-flex; align-items: center; gap: 10px; padding: 8px 14px;
                 border-radius: 8px; font-size: 12px;
                 background: color-mix(in srgb, #ef4444 15%, var(--bg-2));
                 border: 1px solid color-mix(in srgb, #ef4444 40%, var(--border)); }
    .errbanner button { font-size: 12px; }
    .x { border: 0; background: none; cursor: pointer; opacity: .6; font-size: 15px; padding: 4px 6px; }
    .x:hover { opacity: 1; }
    .pats { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .pat { display: inline-flex; align-items: center; gap: 5px; padding: 3px 8px;
           border-radius: 999px; background: var(--bg-2); font-size: 12px; }
    .pat.off { opacity: .45; }

    /* Overall progress summary (§12): the same picture the Search header
       shows, so this page never leaves the user guessing what's happening.
       The current-file name is the one part that changes on every job, so it
       gets its own fixed-width, ellipsis-truncated box -- otherwise the bar
       itself visibly stretches and shrinks as filenames of different lengths
       come and go, which reads as the whole thing jittering side to side. */
    .ovbar { display: flex; align-items: center; gap: 10px; margin: 14px 0 2px; }
    .ovtrack { position: relative; flex: 1; height: 10px; border-radius: 999px;
               background: var(--bg-2); overflow: hidden; min-width: 0; }
    .ovfill { position: absolute; inset: 0 auto 0 0; background: var(--accent);
              border-radius: 999px; transition: width .3s; }
    .ovtrack.paused .ovfill { background: #d97706; opacity: .6; }
    .ovlabel { flex: 0 0 auto; display: flex; align-items: center; gap: 6px;
               font-size: 12px; color: var(--fg-dim); white-space: nowrap;
               font-variant-numeric: tabular-nums; }
    .ovcurrent { display: inline-block; max-width: 260px; overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
    @media (max-width: 900px) { .ovcurrent { max-width: 120px; } }

    /* Per-root indexed column: a compact fill bar instead of bare numbers,
       with pending/error state as small chips underneath -- not stacked text. */
    .indexcell { min-width: 150px; }
    .rowbar { position: relative; width: 100%; height: 7px; border-radius: 999px;
              background: var(--bg-2); overflow: hidden; margin-bottom: 5px; }
    .rowfill { position: absolute; inset: 0 auto 0 0; background: var(--accent);
               border-radius: 999px; transition: width .3s; }
    .rowbar.indet { background: var(--bg-2); }
    .rowbar .stripe { position: absolute; inset: 0; opacity: .5;
      background: repeating-linear-gradient(45deg, var(--accent) 0 8px, transparent 8px 16px);
      animation: slide 1s linear infinite; }
    @keyframes slide { from { background-position: 0 0; } to { background-position: 22px 0; } }
    .rowlabel { font-size: 12px; }
    .rowlabel.warn { color: var(--fg); font-weight: 600; }
    .rowchips { display: flex; gap: 6px; margin-top: 4px; }
    .chip { font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 999px;
            white-space: nowrap; font-variant-numeric: tabular-nums; border: 1px solid transparent; }
    .chip.pend { background: color-mix(in srgb, #f59e0b 22%, var(--bg)); color: #fbbf24;
                 border-color: color-mix(in srgb, #f59e0b 55%, transparent); }
    .chip.err-btn { cursor: pointer; background: color-mix(in srgb, #ef4444 22%, var(--bg));
                    color: #fca5a5; border-color: color-mix(in srgb, #ef4444 55%, transparent); }
    .chip.err-btn:hover { background: color-mix(in srgb, #ef4444 34%, var(--bg)); }

    .error-panel td { padding: 10px 8px 14px; background: var(--bg-2); }
    .errlist { display: flex; flex-direction: column; gap: 6px; }
    .errrow { display: grid; grid-template-columns: 60px 1fr 2fr auto; align-items: center;
              gap: 10px; font-size: 12px; }
    .ekind { color: var(--fg-dim); text-transform: uppercase; font-size: 10px; }
    .efile { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .emsg { color: #b3261e; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: monospace; }
    .errrow button { font-size: 12px; padding: 5px 12px; }
    .logsec { margin-top: 22px; }
    .logtoggle { border: 0; background: none; color: var(--fg-dim); cursor: pointer;
                 font-size: 12px; padding: 0; }
    .logtoggle:hover { color: var(--fg); }
    .logbox { margin-top: 8px; max-height: 220px; overflow-y: auto; background: var(--bg-2);
              border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px;
              font-family: monospace; font-size: 11px; }
    .logline { padding: 2px 0; white-space: pre-wrap; word-break: break-word; }
    .logline.lg-warn { color: #b7791f; }
    .logline.lg-error { color: #b3261e; }
    .lts { color: var(--fg-dim); margin-right: 6px; }
    @media (max-width: 760px) {
      .wrap { padding: 14px; }
      .top { flex-wrap: wrap; }
      .primary { margin-left: 0; }
      .addbar { flex-wrap: wrap; }
      .addbar input { flex: 1 1 100%; }
    }
  `],
})
export class SourcesComponent {
  private api = getApi();
  readonly lib = inject(LibraryService);
  readonly roots = signal<Root[]>([]);
  readonly excludes = signal<ExcludeRule[]>([]);
  readonly draft = signal('');
  readonly rescanning = signal(false);
  readonly rescanMsg = signal('');
  readonly rescanningRoot = signal<number | null>(null);
  readonly reindexingAll = signal(false);
  readonly reindexAllMsg = signal('');
  readonly reindexingRoot = signal<number | null>(null);
  readonly recaptioningRoot = signal<number | null>(null);
  readonly errorsOpenFor = signal<number | null>(null);
  readonly rootErrors = signal<ErrorRow[]>([]);
  readonly rootErrorsLoading = signal(false);
  readonly retrying = signal<Set<number>>(new Set());
  readonly logOpen = signal(false);
  readonly hasLoaded = signal(false);
  readonly loadError = signal('');
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    void this.refresh();
    // Per-root file/done/pending/error counts come from their own query, not
    // the generic progress bar — so just sitting on this page never used to
    // refresh them; only clicking Rescan/Reindex (which call refresh() after)
    // did. React to the same live progress stream the rest of the app already
    // gets from the daemon and re-pull roots() a moment later, throttled so a
    // burst of job-completion events doesn't hammer the DB with a full re-scan
    // of every root's counts on every single one.
    effect(() => {
      this.lib.progress();
      if (this.refreshTimer) return;
      this.refreshTimer = setTimeout(() => {
        this.refreshTimer = null;
        void this.refresh();
        // Same staleness bug as the root counts: an open error panel was
        // fetched once at click time and never touched again, so it kept
        // showing e.g. "1 error" long after the badge next to it climbed to
        // 7 — the only way to see the rest was to close and reopen it.
        const openId = this.errorsOpenFor();
        if (openId != null) void this.loadErrors(openId);
      }, 1500);
    });
  }

  logTime(ts: number): string {
    return new Date(ts).toLocaleTimeString();
  }

  // A daemon restart (crash recovery, or the heartbeat watchdog) briefly
  // makes every RPC fail; without a retry, landing on this page during that
  // ~1s window leaves it stuck showing "No roots yet" forever since nothing
  // else re-triggers a fetch. Retry a few times before giving up, and never
  // clear already-loaded data on failure.
  async refresh(attempt = 0) {
    try {
      const r = await this.api.indexer.roots();
      this.roots.set(r.roots);
      this.excludes.set(r.excludes);
      this.loadError.set('');
      this.hasLoaded.set(true);
    } catch (e) {
      if (attempt < 4) {
        setTimeout(() => this.refresh(attempt + 1), 800);
      } else {
        console.error('failed to load sources', e);
        this.loadError.set(e instanceof Error ? e.message : String(e));
        this.hasLoaded.set(true);
      }
    }
  }

  async browse() {
    const paths = (await this.api.pickFolder?.()) ?? [];
    if (paths[0]) this.draft.set(paths[0]);
  }

  async add(_input: HTMLInputElement, mode: 'include' | 'exclude') {
    const path = this.draft().trim();
    if (!path) return;
    if (mode === 'exclude') await this.api.indexer.addExclude(path);
    else await this.api.indexer.addRoot(path, 'include');
    this.draft.set('');
    await this.refresh();
  }

  ago(epoch?: number | null): string {
    if (!epoch) return 'never';
    const s = Math.max(0, Math.floor(Date.now() / 1000) - epoch);
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    if (s < 2592000) return `${Math.floor(s / 86400)}d ago`;
    return new Date(epoch * 1000).toLocaleDateString();
  }

  async remove(r: Root) { await this.api.indexer.removeRoot(r.id); await this.refresh(); }
  async toggle(r: Root, enabled: boolean) { await this.api.indexer.toggleRoot(r.id, enabled); await this.refresh(); }

  async addPattern(input: HTMLInputElement) {
    const p = input.value.trim(); if (!p) return;
    await this.api.indexer.addExcludePattern(p); input.value = '';
    await this.refresh();
  }
  async removePattern(e: ExcludeRule) { await this.api.indexer.removeExclude(e.id); await this.refresh(); }
  async togglePattern(e: ExcludeRule, enabled: boolean) {
    await this.api.indexer.toggleExclude(e.id, enabled); await this.refresh();
  }

  async rescan() {
    this.rescanning.set(true);
    this.rescanMsg.set('');
    try {
      const r = await this.lib.rescan();
      const revived = r.revived ? `, ${r.revived} recovered` : '';
      this.rescanMsg.set(`Rescan done — ${r.added} added, ${r.changed} changed, ${r.removed} removed, ${r.unchanged} unchanged${revived}.`);
      await this.refresh();
    } catch (e) {
      this.rescanMsg.set(`Rescan failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.rescanning.set(false);
    }
  }

  // Unlike Rescan (new/changed/removed files only), this re-runs tagging/
  // caption/OCR for every already-indexed file with whatever models are
  // currently enabled — e.g. after downloading/enabling a facet.
  async reindexAll() {
    if (this.reindexingAll()) return;
    this.reindexingAll.set(true);
    this.reindexAllMsg.set('Queuing every indexed file for re-processing…');
    try {
      const r = await this.lib.reindexAll();
      this.reindexAllMsg.set(`Queued ${r.queued} file(s). Resume/auto-mode indexing will pick them up.`);
    } catch (e) {
      this.reindexAllMsg.set(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.reindexingAll.set(false);
    }
  }

  async rescanRoot(root: Root) {
    if (root.mode !== 'include' || !root.enabled || this.rescanningRoot() != null) return;
    this.rescanningRoot.set(root.id);
    this.rescanMsg.set('');
    try {
      const r = await this.lib.rescanRoot(root.id);
      const revived = r.revived ? `, ${r.revived} recovered` : '';
      this.rescanMsg.set(`${root.path}: ${r.added} added, ${r.changed} changed, ${r.removed} removed, ${r.unchanged} unchanged${revived}.`);
      await this.refresh();
    } catch (e) {
      this.rescanMsg.set(`Scan failed for ${root.path}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.rescanningRoot.set(null);
    }
  }

  // Scoped version of reindexAll: re-process just this folder's already-
  // indexed files (e.g. after switching a model for a subset of the library).
  async reindexRoot(root: Root) {
    if (root.mode !== 'include' || !root.enabled || this.reindexingRoot() != null) return;
    this.reindexingRoot.set(root.id);
    this.rescanMsg.set('');
    try {
      const r = await this.lib.reindexRoot(root.id);
      this.rescanMsg.set(`${root.path}: queued ${r.queued} file(s) for re-processing.`);
    } catch (e) {
      this.rescanMsg.set(`Index failed for ${root.path}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.reindexingRoot.set(null);
    }
  }

  // Scoped, caption-only backfill: unlike Index (which re-runs every facet),
  // this only regenerates descriptions — useful after switching caption
  // models or for a folder that was indexed before captioning was enabled.
  async recaptionRoot(root: Root) {
    if (root.mode !== 'include' || !root.enabled || this.recaptioningRoot() != null) return;
    this.recaptioningRoot.set(root.id);
    this.rescanMsg.set('');
    try {
      const r = await this.lib.recaptionRoot(root.id);
      this.rescanMsg.set(`${root.path}: queued ${r.queued ?? 0} file(s) for new descriptions.`);
    } catch (e) {
      this.rescanMsg.set(`Desc failed for ${root.path}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.recaptioningRoot.set(null);
    }
  }

  async toggleErrors(root: Root) {
    if (this.errorsOpenFor() === root.id) { this.errorsOpenFor.set(null); return; }
    this.errorsOpenFor.set(root.id);
    this.rootErrors.set([]);
    await this.loadErrors(root.id, true);
  }

  private async loadErrors(rootId: number, showSpinner = false) {
    if (showSpinner) this.rootErrorsLoading.set(true);
    try {
      const r = await this.lib.listErrors(rootId);
      if (this.errorsOpenFor() === rootId) this.rootErrors.set(r.errors);
    } finally {
      if (showSpinner) this.rootErrorsLoading.set(false);
    }
  }

  async retryOne(e: ErrorRow) {
    this.retrying.update((s) => new Set(s).add(e.file_id));
    try {
      await this.lib.retryErrors(e.file_id);
      this.rootErrors.update((rows) => rows.filter((r) => r.file_id !== e.file_id));
      await this.refresh();
    } finally {
      this.retrying.update((s) => { const n = new Set(s); n.delete(e.file_id); return n; });
    }
  }
}
