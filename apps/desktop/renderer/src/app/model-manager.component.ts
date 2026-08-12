import { Component, NgZone, OnDestroy, computed, inject, signal } from '@angular/core';
import { LibraryService } from './library.service';
import { DownloadProgress, Facet, FacetVariants, Variant, getApi } from './api';

/** Model-download manager (spec §12): the catalog of every model each AI feature
 * uses — name, source, size, save location — with one-click downloads and live
 * progress, so the user never has to guess which models are needed. */
@Component({
  selector: 'app-model-manager',
  standalone: true,
  template: `
    <div class="wrap">
      <div class="top">
        <div>
          <h2>Models</h2>
          <div class="saved">Saved to <code data-testid="models-dir">{{ dir() }}</code></div>
        </div>
        <div class="dir">
          <label>Change folder</label>
          <input [value]="modelsDir()" (change)="saveDir($any($event.target).value)" />
        </div>
        <button (click)="refresh()" [disabled]="refreshing()">{{ refreshing() ? 'Refreshing…' : 'Refresh' }}</button>
      </div>

      @if (!hasLoaded()) {
        <div class="loadbar" data-testid="loading"><div class="stripe"></div></div>
        <p class="dim loadingtext">Loading model status…</p>
      } @else {
        @if (loadError()) {
          <div class="errbanner" data-testid="load-error">
            Couldn't load model status: {{ loadError() }}
            <button (click)="refresh()">Retry</button>
          </div>
        }
        @if (notice()) { <div class="notice" data-testid="model-notice">{{ notice() }}</div> }

        @if (missing().length) {
          <div class="banner" data-testid="firstrun">
            <div>
              <b>{{ missing().length }} model(s) needed</b> for the AI features you haven't set up yet
              (~{{ missingGb() }}). They download once into the folder above — nothing leaves your machine.
            </div>
            <button class="primary" (click)="downloadAll()" data-testid="download-all"
                    [disabled]="downloadingAll()">
              {{ downloadingAll() ? 'downloading…' : 'Download all missing' }}
            </button>
          </div>
        } @else if (!loadError()) {
          <div class="banner ok">All models for your enabled features are ready. 🎉</div>
        }
        @if (tier()) {
          <div class="tierline">Detected tier: <b>{{ tier() }}</b> — variants marked ★ are the best fit for your GPU/NPU. Pick a bigger one for higher quality, a smaller one for speed.</div>
        }
        @if (reindexNote()) {
          <div class="reindex" data-testid="reindex-note">Switched CLIP model — its embeddings differ, so a re-index is needed for semantic search &amp; learned tags. Run a rescan when convenient.</div>
        }

      <div class="table-scroll">
      <table data-testid="facet-table">
        <thead>
          <tr><th>Feature</th><th>Model</th><th>Size</th><th>Status</th><th></th></tr>
        </thead>
        <tbody>
          @for (f of facets(); track f.label) {
            <tr data-testid="facet-row">
              <td class="feat">
                <div class="lbl">{{ f.label }}</div>
                <div class="ms">{{ f.milestone }}</div>
                @if (f.facet) {
                  <label class="toggle" [title]="(!f.dep_ok || !f.model_ok) ? 'Install the dependency and model first' : 'Enable this facet for new/re-indexed images'">
                    <input type="checkbox" [checked]="f.enabled"
                           [disabled]="toggling() === f.facet || !f.dep_ok || !f.model_ok"
                           (change)="toggle(f, $any($event.target).checked)" />
                    <span>{{ f.enabled ? 'enabled' : 'disabled' }}</span>
                  </label>
                }
              </td>
              <td class="model">
                <div class="mn">{{ f.model_name || '—' }}</div>
                <div class="src">
                  {{ f.source }}
                  @if (f.url) { · <a [href]="f.url" target="_blank" rel="noopener">page ↗</a> }
                  <span class="kind k-{{ f.kind }}">{{ f.kind }}</span>
                </div>
                @if (f.has_variants && f.download && vlist(f.download).length) {
                  <select class="vsel" data-testid="variant-select"
                          [value]="pendingOf(f.download)"
                          (change)="pick(f.download, $any($event.target).value)">
                    @for (v of vlist(f.download); track v.id) {
                      <option [value]="v.id" [selected]="v.id === pendingOf(f.download)">{{ v.label }} · {{ v.size_mb }} MB · {{ v.tier }}{{ v.id === vrec(f.download) ? '  ★ best for your GPU' : '' }}</option>
                    }
                  </select>
                  @if (isPending(f.download)) {
                    <div class="pendingrow" data-testid="pending-variant">
                      <span class="pendingtag">not applied yet</span>
                      <button class="apply" (click)="apply(f.download)" [disabled]="applying() === f.download" data-testid="apply-btn">
                        {{ applying() === f.download ? 'applying…' : '✓ Apply' }}
                      </button>
                    </div>
                  }
                }
              </td>
              <td class="size">{{ sizeLabel(f) }}</td>
              <td><span class="badge s-{{ cls(f) }}">{{ f.state }}</span></td>
              <td class="act">
                @if (isBusy(jobKey(f))) {
                  <div class="prog" data-testid="download-prog" [class.indet]="pctOf(jobKey(f)) === null">
                    @if (pctOf(jobKey(f)) !== null) {
                      <div class="fill" [style.width.%]="pctOf(jobKey(f))"></div>
                    } @else { <div class="stripe"></div> }
                  </div>
                  <span class="pl">{{ pctOf(jobKey(f)) !== null ? pctOf(jobKey(f)) + '%' : 'downloading…' }}</span>
                  <div class="dlstatus">{{ statusText(jobKey(f)) }}</div>
                } @else if (!f.dep_ok) {
                  <button (click)="install(f)" [disabled]="!installKey(f) || isBusy(depKey(f))">
                    {{ isBusy(depKey(f)) ? 'installing…' : 'install dependency' }}
                  </button>
                } @else if (!f.download) {
                  <span class="dim">included with app</span>
                } @else {
                  <button (click)="download(f)" data-testid="download-btn"
                          [title]="isPending(f.download) ? 'Download this variant only — it will not become active until you Apply it' : ''">
                    {{ downloadLabel(f) }}
                  </button>
                }
                @if (errorOf(jobKey(f))) {
                  <div class="dlerror" data-testid="download-error">{{ errorOf(jobKey(f)) }}</div>
                }
              </td>
            </tr>
          }
        </tbody>
      </table>
      </div>
      }
    </div>
  `,
  styles: [`
    :host { display: block; overflow: auto; height: 100%; min-width: 0; }
    .wrap { padding: clamp(16px, 2vw, 30px); width: 100%; box-sizing: border-box; }
    .top { display: flex; align-items: flex-start; gap: 18px; }
    h2 { margin: 0; }
    .saved { color: var(--fg-dim); font-size: 12px; margin-top: 3px; }
    .saved code { background: var(--bg-2); padding: 1px 6px; border-radius: 4px; }
    .dir { margin-left: auto; display: flex; flex-direction: column; gap: 3px; }
    .dir label { color: var(--fg-dim); font-size: 11px; } .dir input { width: 260px; }
    .banner { display: flex; align-items: center; gap: 14px; margin: 14px 0 4px;
              padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px;
              background: var(--bg-2); font-size: 13px; }
    .banner.ok { color: var(--fg-dim); }
    .notice { margin: 10px 0 0; padding: 8px 12px; border-radius: 8px;
              background: var(--bg-2); font-size: 12px; white-space: pre-wrap; }
    .banner .primary { margin-left: auto; background: var(--accent); color: #fff; border: 0;
                       padding: 8px 14px; border-radius: 8px; white-space: nowrap; }
    .table-scroll { width: 100%; overflow-x: auto; }
    table { width: 100%; min-width: 900px; border-collapse: collapse; margin-top: 12px; }
    th { text-align: left; font-size: 11px; text-transform: uppercase; color: var(--fg-dim);
         border-bottom: 1px solid var(--border); padding: 6px 8px; }
    td { padding: 10px 8px; border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
         vertical-align: top; }
    .lbl { font-weight: 600; } .ms { color: var(--fg-dim); font-size: 11px; }
    .toggle { display: inline-flex; align-items: center; gap: 5px; margin-top: 5px;
              color: var(--fg-dim); font-size: 11px; cursor: pointer; }
    .toggle input { accent-color: var(--accent); }
    .toggle:has(input:checked) { color: var(--fg); font-weight: 600; }
    .toggle:has(input:disabled) { cursor: not-allowed; opacity: .6; }
    .tierline { font-size: 12px; color: var(--fg-dim); margin: 8px 2px 0; }
    .reindex { font-size: 12px; margin: 8px 0 0; padding: 8px 12px; border-radius: 8px;
               background: var(--tag-path); }
    .vsel { margin-top: 6px; max-width: 320px; font-size: 12px; padding: 5px 7px; }
    .pendingrow { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
    .pendingtag { font-size: 10px; text-transform: uppercase; letter-spacing: .03em;
                  color: #92400e; background: var(--tag-path); padding: 2px 8px; border-radius: 999px; }
    .apply { font-size: 12px; font-weight: 600; padding: 5px 12px; border-radius: 7px;
             background: var(--accent); color: #fff; border: 1px solid transparent; }
    .apply:hover:not(:disabled) { filter: brightness(1.08); background: var(--accent); }
    .mn { font-weight: 500; } .src { color: var(--fg-dim); font-size: 12px; margin-top: 2px; }
    .src a { color: var(--accent); }
    .kind { font-size: 10px; padding: 0 5px; border-radius: 4px; margin-left: 6px; }
    .k-direct { background: var(--tag-wd14); } .k-library { background: var(--tag-clip); }
    .k-pip { background: var(--bg); border: 1px solid var(--border); }
    .size { white-space: nowrap; color: var(--fg-dim); }
    .badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--bg-2); }
    .s-ready { background: var(--tag-manual); } .s-model { background: var(--tag-path); }
    .s-dep { background: var(--tag-clip); }
    .act { min-width: 170px; } .dim { color: var(--fg-dim); font-size: 12px; }
    .prog { position: relative; height: 8px; width: 150px; border-radius: 999px;
            background: var(--bg-2); overflow: hidden; }
    .prog .fill { position: absolute; inset: 0 auto 0 0; background: var(--accent);
                  border-radius: 999px; transition: width .2s; }
    .prog .pl { display: block; font-size: 11px; margin-top: 4px; color: var(--fg-dim);
                font-variant-numeric: tabular-nums; }
    .dlstatus { width: 190px; color: var(--fg-dim); font-size: 10px; margin-top: 4px; }
    .dlerror { width: 260px; max-height: 90px; overflow: auto; color: #ff8a80;
               font-size: 10px; white-space: pre-wrap; margin-top: 5px; }
    .prog.indet .fill { display: none; }
    .prog .stripe { position: absolute; inset: 0; opacity: .5;
      background: repeating-linear-gradient(45deg, var(--accent) 0 8px, transparent 8px 16px);
      animation: slide 1s linear infinite; }
    @keyframes slide { from { background-position: 0 0; } to { background-position: 22px 0; } }
    /* Right after launch the daemon may still be starting up, so the very
       first fetch here can take a moment or transiently fail. Without this,
       the page rendered an empty table plus a false "All models ready" banner
       (missing() is trivially empty when facets() hasn't loaded yet at all),
       which is actively misleading, not just blank. */
    .loadbar { position: relative; height: 4px; border-radius: 999px; background: var(--bg-2);
               overflow: hidden; margin-top: 16px; }
    .loadbar .stripe { position: absolute; inset: 0; opacity: .5;
      background: repeating-linear-gradient(45deg, var(--accent) 0 8px, transparent 8px 16px);
      animation: slide 1s linear infinite; }
    .loadingtext { margin-top: 8px; }
    .errbanner { margin-top: 14px; padding: 10px 14px; border-radius: 8px; font-size: 12px;
                 background: color-mix(in srgb, #ef4444 15%, var(--bg-2));
                 border: 1px solid color-mix(in srgb, #ef4444 40%, var(--border));
                 display: flex; align-items: center; gap: 10px; }
    .errbanner button { font-size: 12px; }
    @media (max-width: 760px) {
      .wrap { padding: 14px; }
      .top { flex-wrap: wrap; }
      .dir { margin-left: 0; flex: 1 1 100%; }
      .dir input { width: 100%; box-sizing: border-box; }
      .banner { align-items: flex-start; flex-wrap: wrap; }
      .banner .primary { margin-left: 0; }
    }
  `],
})
export class ModelManagerComponent implements OnDestroy {
  private lib = inject(LibraryService);
  private api = getApi();
  private readonly zone = inject(NgZone);
  readonly facets = signal<Facet[]>([]);
  readonly dl = signal<Record<string, DownloadProgress>>({});
  readonly modelsDir = signal('');
  readonly dir = signal('…');
  readonly downloadingAll = signal(false);
  readonly variants = signal<Record<string, FacetVariants>>({});
  readonly tier = signal('');
  readonly reindexNote = signal(false);
  readonly toggling = signal<string | null>(null);
  readonly notice = signal('');
  readonly refreshing = signal(false);
  readonly hasLoaded = signal(false);
  readonly loadError = signal('');
  // Locally-picked-but-not-yet-applied variant per facet (§12 Models UX):
  // browsing the dropdown must never itself change what's active. Cleared to
  // "follow the applied value" whenever it's undefined for a facet.
  readonly pending = signal<Record<string, string>>({});
  readonly applying = signal<string | null>(null);
  private refreshSeq = 0;
  private pollTimer?: ReturnType<typeof setInterval>;
  private offProgress?: () => void;
  private offDone?: () => void;

  readonly missing = computed(() => this.facets().filter(
    (f) => !f.dep_ok || (!!f.download && !f.model_ok)));
  readonly missingGb = computed(() => {
    const mb = this.missing().reduce((s, f) => s + (f.size_mb ?? 0), 0);
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
  });

  constructor() {
    void this.refresh();
    void this.lib.getSetting('models_dir', '').then((v) => this.modelsDir.set(v ?? ''));
    this.offProgress = this.api.indexer.onDownloadProgress((e) =>
      this.zone.run(() => this.applyStatus(e)));
    this.offDone = this.api.indexer.onDownloadDone(async (e) => {
      this.zone.run(() => {
        this.applyStatus({ ...e, pct: e.ok ? 100 : null,
          state: e.ok ? 'done' : 'error', indeterminate: false });
        this.notice.set(e.ok ? `${e.model} is ready.` : `${e.model} failed: ${e.error || 'unknown error'}`);
      });
      await this.refresh();
      if (e.ok && e.model.startsWith('dep:') && this.downloadingAll()) {
        const facet = e.model.slice(4);
        const f = this.facets().find((x) => this.installKey(x) === facet);
        if (f?.dep_ok && !f.model_ok && f.download) void this.download(f);
      }
      if (!this.missing().length || !e.ok) this.downloadingAll.set(false);
    });
    this.pollTimer = setInterval(() => void this.syncDownloads(), 1000);
  }

  async refresh() {
    const mine = ++this.refreshSeq;
    this.refreshing.set(true);
    try {
      const state = await this.api.indexer.modelState();
      if (mine !== this.refreshSeq) return;
      const map: Record<string, FacetVariants> = {};
      for (const v of state.variants) map[v.facet] = v;
      this.facets.set(state.facets);
      this.variants.set(map);
      this.dir.set(state.models_dir);
      if (state.variants[0]) this.tier.set(state.variants[0].tier);
      this.mergeStatuses(state.downloads);
      this.loadError.set('');
    } catch (e) {
      if (mine === this.refreshSeq)
        this.loadError.set(e instanceof Error ? e.message : String(e));
    } finally {
      if (mine === this.refreshSeq) { this.refreshing.set(false); this.hasLoaded.set(true); }
    }
  }

  ngOnDestroy() {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.offProgress?.();
    this.offDone?.();
  }

  private applyStatus(status: DownloadProgress) {
    this.dl.update((current) => ({ ...current, [status.model]: { ...current[status.model], ...status } }));
  }

  private mergeStatuses(statuses: DownloadProgress[]) {
    for (const status of statuses) this.applyStatus(status);
  }

  private async syncDownloads() {
    try {
      const state = await this.api.indexer.downloadStatus();
      this.mergeStatuses(state.downloads);
    } catch { /* daemon may be restarting */ }
  }

  vlist(facet: string): Variant[] { return this.variants()[facet]?.variants ?? []; }
  vsel(facet: string): string { return this.variants()[facet]?.selected ?? ''; }
  vrec(facet: string): string { return this.variants()[facet]?.recommended ?? ''; }

  // Just stages the pick locally -- no RPC, no side effects. Browsing the
  // dropdown to compare variants must be free; only apply() below commits it.
  pick(facet: string, id: string) {
    this.pending.update((p) => ({ ...p, [facet]: id }));
  }

  pendingOf(facet: string): string { return this.pending()[facet] ?? this.vsel(facet); }
  isPending(facet: string): boolean {
    const p = this.pending()[facet];
    return p != null && p !== this.vsel(facet);
  }

  downloadLabel(f: Facet): string {
    if (!f.download) return 'download';
    if (this.isPending(f.download)) {
      const v = this.vlist(f.download).find((x) => x.id === this.pendingOf(f.download!));
      return `download ${v?.label ?? 'variant'} only`;
    }
    return f.model_ok ? 'redownload' : 'download';
  }

  async apply(facet: string) {
    const id = this.pendingOf(facet);
    const current = this.variants()[facet];
    if (!current || current.selected === id) { this.pending.update((p) => { const n = { ...p }; delete n[facet]; return n; }); return; }
    this.applying.set(facet);
    try {
      const r = await this.api.indexer.setVariant(facet, id);
      if (!r.ok) throw new Error('Could not apply model selection');
      if (r.reindex_needed) this.reindexNote.set(true);
      this.notice.set(`Applied ${facet} model: ${this.vlist(facet).find((v) => v.id === id)?.label ?? id}`
        + (r.recaptioning ? ` — ${r.recaptioning} already-captioned file(s) queued for a new description.` : ''));
      this.pending.update((p) => { const n = { ...p }; delete n[facet]; return n; });
      await this.refresh();
    } catch (e) {
      this.notice.set(`Could not apply ${facet}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.applying.set(null);
    }
  }

  async toggle(f: Facet, enabled: boolean) {
    if (!f.facet) return;
    this.toggling.set(f.facet);
    try {
      const r = await this.api.indexer.setFacetEnabled(f.facet, enabled);
      if (!r.ok) throw new Error(r.error || 'Could not change facet state');
      await this.refresh();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e));
      await this.refresh();
    } finally {
      this.toggling.set(null);
    }
  }

  cls(f: Facet) { return f.state === 'ready' ? 'ready' : f.state.includes('dep') ? 'dep' : 'model'; }
  sizeLabel(f: Facet) {
    const mb = f.size_mb ?? 0;
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : mb > 0 ? `${mb} MB` : '—';
  }
  isBusy(model: string) {
    const state = this.dl()[model]?.state;
    return state === 'queued' || state === 'running';
  }
  pctOf(model: string): number | null { return this.dl()[model]?.pct ?? null; }
  statusText(model: string): string {
    const s = this.dl()[model];
    if (!s) return '';
    const elapsedSeconds = s.elapsed_s ?? (s.started_at ? Math.max(0, Math.floor(Date.now() / 1000 - s.started_at)) : null);
    const elapsed = elapsedSeconds != null ? ` · ${this.duration(elapsedSeconds)}` : '';
    return `${s.message || s.phase || s.state || ''}${elapsed}`;
  }
  errorOf(model: string): string { return this.dl()[model]?.state === 'error' ? (this.dl()[model]?.error || 'Unknown error') : ''; }
  private duration(seconds: number): string {
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    return seconds < 3600 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }
  installKey(f: Facet) { return f.install || f.facet || ''; }
  depKey(f: Facet) { return `dep:${this.installKey(f)}`; }
  jobKey(f: Facet): string {
    const dependency = this.dl()[this.depKey(f)];
    if (dependency && dependency.state !== 'done') return this.depKey(f);
    return f.download || this.depKey(f);
  }

  async install(f: Facet) {
    const install = this.installKey(f);
    if (!install || this.isBusy(this.depKey(f))) return;
    const key = this.depKey(f);
    this.applyStatus({ model: key, pct: null, state: 'queued', indeterminate: true, message: 'Starting installer' });
    try {
      const r = await this.api.indexer.installDependency(install);
      if (r.error) throw new Error(r.error);
      await this.syncDownloads();
    } catch (e) {
      this.applyStatus({ model: key, pct: null, state: 'error', error: e instanceof Error ? e.message : String(e) });
    }
  }

  async download(f: Facet) {
    if (!f.download || this.isBusy(f.download)) return;
    const targetingPending = this.isPending(f.download);
    const variantId = targetingPending ? this.pendingOf(f.download) : undefined;
    this.applyStatus({ model: f.download, pct: 0, state: 'queued', indeterminate: true, message: 'Starting download' });
    try {
      const r = await this.api.indexer.download(f.download, variantId);
      if (r.error) throw new Error(r.error);
      await this.syncDownloads();
      if (targetingPending) {
        const label = this.vlist(f.download).find((v) => v.id === variantId)?.label ?? variantId;
        this.notice.set(`Downloaded ${label} — it is not active yet; click Apply to use it.`);
      }
    } catch (e) {
      this.applyStatus({ model: f.download, pct: null, state: 'error', error: e instanceof Error ? e.message : String(e) });
    }
  }

  downloadAll() {
    this.downloadingAll.set(true);
    for (const f of this.missing()) {
      if (f.dep_ok) void this.download(f);
      else void this.install(f);
    }
  }

  saveDir(v: string) {
    this.modelsDir.set(v);
    void this.lib.setSetting('models_dir', v).then(() => this.refresh());
    if (v) this.dir.set(v);
  }
}
