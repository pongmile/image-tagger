import { Component, HostListener, computed, inject, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { LibraryService } from './library.service';
import { ResultGridComponent } from './result-grid.component';
import { PreviewComponent } from './preview.component';
import { ModelManagerComponent } from './model-manager.component';
import { FacesComponent } from './faces.component';
import { LearnedComponent } from './learned.component';
import { SourcesComponent } from './sources.component';
import { SettingsComponent } from './settings.component';
import { IS_ELECTRON } from './api';

type View = 'search' | 'sources' | 'models' | 'people' | 'learned' | 'settings';

/** App shell (spec §8): the Everything-like search bar (no submit, live count),
 * the virtualized result grid, and the preview/tag-editor pane. A bulk bar
 * appears on multi-select (§9) and a settings drawer exposes the models folder
 * and engine tier (§5.2/§12). */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [ResultGridComponent, PreviewComponent, ModelManagerComponent, FacesComponent, LearnedComponent, SourcesComponent, SettingsComponent, DecimalPipe],
  template: `
    <nav class="tabs">
      <span class="brand"><span class="mark">IT</span><span>Image Tagger</span></span>
      <button [class.on]="view() === 'search'" (click)="view.set('search')" data-testid="tab-search">Search</button>
      <button [class.on]="view() === 'sources'" (click)="view.set('sources')" data-testid="tab-sources">Sources</button>
      <button [class.on]="view() === 'models'" (click)="view.set('models')" data-testid="tab-models">Models</button>
      <button [class.on]="view() === 'people'" (click)="view.set('people')" data-testid="tab-people">People</button>
      <button [class.on]="view() === 'learned'" (click)="view.set('learned')" data-testid="tab-learned">Learned tags</button>
      <button [class.on]="view() === 'settings'" (click)="view.set('settings')" data-testid="tab-settings">Settings</button>
      <span class="env" [class.mock]="!electron">{{ electron ? 'live' : 'demo data' }}</span>
    </nav>

    @if (view() === 'models') { <app-model-manager></app-model-manager> }
    @else if (view() === 'people') { <app-faces></app-faces> }
    @else if (view() === 'learned') { <app-learned></app-learned> }
    @else if (view() === 'sources') { <app-sources></app-sources> }
    @else if (view() === 'settings') { <app-settings></app-settings> }
    @else {
    <div class="searchview">
    <header>
      <div class="bar">
        <input #q class="search" data-testid="search"
               [placeholder]="lib.semantic()
                 ? 'semantic search…  girl on a beach at sunset'
                 : 'search…  cat|dog   character:&quot;hatsune miku&quot;   *.png   size:&gt;10mb   !draft'"
               [value]="lib.query()"
               (input)="onInput(q.value)" autofocus />
        @if (!lib.semantic()) {
          <button class="synhelp" data-testid="syntax-help"
                  [class.on]="showSyntaxHelp()" (click)="showSyntaxHelp.set(!showSyntaxHelp())"
                  title="search syntax reference">? syntax</button>
        }
        <button class="sem" data-testid="semantic-toggle"
                [class.on]="lib.semantic()" (click)="lib.toggleSemantic()"
                title="semantic search (CLIP + sqlite-vec)">✨ semantic</button>
        <button class="gear" (click)="view.set('settings')" title="settings" data-testid="gear">⚙</button>
      </div>

      @if (showSyntaxHelp() && !lib.semantic()) {
        <div class="synpanel" data-testid="syntax-panel">
          <table>
            <tr><td><code>space</code></td><td>AND — every term must match (default)</td></tr>
            <tr><td><code>a | b</code></td><td>OR — either term matches</td></tr>
            <tr><td><code>!a</code> or <code>-a</code></td><td>NOT — exclude a term</td></tr>
            <tr><td><code>&lt;a|b&gt; c</code></td><td>grouping — (a OR b) AND c</td></tr>
            <tr><td><code>"a b"</code></td><td>exact phrase</td></tr>
            <tr><td><code>*</code></td><td>wildcard — any characters</td></tr>
            <tr><td><code>?</code></td><td>wildcard — exactly one character</td></tr>
            <tr><td><code>size:100mb</code></td><td>at least 100mb — also <code>size:&lt;10mb</code>, <code>size:&gt;1gb</code>, <code>size:1mb-10mb</code></td></tr>
            <tr><td><code>tag:x</code> / <code>character:x</code></td><td>partial match on any tag / a specific category</td></tr>
            <tr><td><code>person:name</code></td><td>match a named face cluster</td></tr>
            <tr><td><code>folder:C:/Pictures</code></td><td>restrict to a folder (and its subfolders)</td></tr>
          </table>
          <div class="synfoot">Works out of the box, nothing to enable. Turn on <b>Regex</b> below to treat free-text terms as regular expressions instead.</div>
        </div>
      }

      @if (!lib.semantic()) {
        <div class="filters">
        <div class="matchopts" data-testid="matchopts">
          <button [class.on]="lib.matchCase()" (click)="lib.toggleMatch('matchCase')"
                  title="Match Case (Ctrl+I)" data-testid="m-case">Aa Match Case</button>
          <button [class.on]="lib.wholeWord()" (click)="lib.toggleMatch('wholeWord')"
                  title="Match Whole Word (Ctrl+B)" data-testid="m-word">⌈ab⌋ Whole Word</button>
          <button [class.on]="lib.matchPath()" (click)="lib.toggleMatch('matchPath')"
                  title="Match Path — search the full path, not tags (Ctrl+U)" data-testid="m-path">/ Match Path</button>
          <button [class.on]="lib.matchDiacritics()" (click)="lib.toggleMatch('matchDiacritics')"
                  title="Match Diacritics (Ctrl+M)" data-testid="m-dia">á Diacritics</button>
          <button [class.on]="lib.regexMode()" (click)="lib.toggleRegex()"
                  title="Treat free-text terms as regular expressions" data-testid="m-regex">.* Regex</button>
        </div>

        <div class="confopts" data-testid="confopts" title="hide auto tags below this confidence — a lower-confidence guess (e.g. a 5th character the model wasn't fully sure of) may still be in the library above 0.25">
          <span class="conflabel">confidence</span>
          @for (opt of confOptions; track opt.value) {
            <button [class.on]="lib.minConfidence() === opt.value"
                    (click)="lib.setMinConfidence(opt.value)"
                    [attr.data-testid]="'conf-' + (opt.value ?? 'all')">{{ opt.label }}</button>
          }
        </div>

        <div class="mediaopt" data-testid="media-type" title="videos are browse/search-only — no AI tagging/captioning runs on them">
          <span class="conflabel">Search in</span>
          <div class="segmented">
            <button [class.on]="lib.mediaType()==='image'" (click)="lib.setMediaType('image')" data-testid="media-image">🖼 Images</button>
            <button [class.on]="lib.mediaType()==='video'" (click)="lib.setMediaType('video')" data-testid="media-video">🎞 Videos</button>
            <button [class.on]="lib.mediaType()==='both'" (click)="lib.setMediaType('both')" data-testid="media-both">🖼🎞 Both</button>
          </div>
        </div>
        </div>
      }

      @if (lib.queryError()) {
        <div class="qerr" data-testid="query-error">⚠ {{ lib.queryError() }}</div>
      }

      <div class="status">
        <span data-testid="count">{{ lib.total() | number }} results</span>
        @if (lib.results().length < lib.total()) { <span class="dim">(showing {{ lib.results().length }})</span> }
        @if (lib.queryMs() > 0) { <span class="dim">· {{ lib.queryMs() }} ms</span> }
        @if (!lib.semantic()) {
          <span class="grammar">bare=substring · a|b OR · !a NOT · &lt;a|b&gt; group · "phrase" · wild* c?rd · size:100mb · tag:partial · person:name · folder:path</span>
        } @else {
          <span class="grammar">semantic: ranked by meaning (CLIP embeddings)</span>
        }
      </div>

      <div class="indexbar" data-testid="indexbar">
        <span class="modeswitch">
          <button [class.on]="mode() === 'auto'" (click)="setMode('auto')"
                  title="watch folders and index changes automatically" data-testid="mode-auto">auto</button>
          <button [class.on]="mode() === 'manual'" (click)="setMode('manual')"
                  title="index only when you press Rescan" data-testid="mode-manual">manual</button>
        </span>
        @if (paused()) {
          <button class="pauseresume paused" (click)="resume()" title="Indexing is paused — nothing is being processed right now" data-testid="resume">▶ Resume indexing</button>
        } @else {
          <button class="pauseresume running" (click)="pause()" title="Pause background indexing" data-testid="pause">⏸ Pause</button>
        }
        <button class="ctl" (click)="rescan()" [disabled]="rescanning()" data-testid="rescan-btn">
          {{ rescanning() ? 'rescanning…' : '↻ Rescan' }}
        </button>
        @if (scanMessage()) {
          <span class="scanmsg" data-testid="scan-message">{{ scanMessage() }}</span>
        }
        @if (errorCount() > 0) {
          <button class="ctl err" (click)="retry()" data-testid="retry">↺ Retry {{ errorCount() }} error(s)</button>
        }
        @if (indexing()) {
          <div class="progress" [class.paused]="paused()" data-testid="progress" [title]="jobsLabel()">
            <div class="fill" [style.width.%]="pct()"></div>
            <span class="pl">{{ paused() ? 'paused' : 'indexing' }} {{ prog()!.files_done }}/{{ prog()!.files_total }}</span>
          </div>
        } @else {
          <span class="idle" data-testid="idle">idle · {{ prog()?.files_total || 0 }} files indexed</span>
        }
      </div>
      @if (workingLabel() || ramLabel()) {
        <div class="workingnotice" data-testid="working-status">
          @if (workingLabel()) { <span class="wl">⚙ {{ workingLabel() }}</span> }
          @if (ramLabel()) { <span class="ram">{{ workingLabel() ? ' · ' : '' }}indexer using {{ ramLabel() }}</span> }
        </div>
      }
      @if (lib.restartNotice()) {
        <div class="restartnotice" data-testid="restart-notice">⚠ {{ lib.restartNotice() }}</div>
      }
    </header>

    @if (selectedCount() > 1) {
      <div class="bulk" data-testid="bulk">
        {{ selectedCount() }} selected
        <input #b placeholder="category:name" (keydown.enter)="bulkAdd(b)" />
        <button (click)="bulkAdd(b)">Add tag to all</button>
        <button (click)="bulkTrain(b)" data-testid="bulk-train">Add &amp; train tag</button>
        @if (bulkMessage()) { <span class="dim">{{ bulkMessage() }}</span> }
      </div>
    }

    <main [style.--preview-w]="previewWidth() + 'px'">
      <app-result-grid></app-result-grid>
      <div class="resizer" (mousedown)="startResize($event)" title="drag to resize the preview pane"></div>
      <aside><app-preview></app-preview></aside>
    </main>

    </div>
    }
  `,
  styles: [`
    :host { display: flex; flex-direction: column; height: 100vh; min-height: 0; }
    app-model-manager, app-faces, app-learned, app-settings { flex: 1; min-height: 0; }
    .searchview { flex: 1; min-height: 0; display: flex; flex-direction: column; }
    .searchview header, .searchview .bulk { flex: 0 0 auto; }
    .tabs { display: flex; align-items: center; gap: 5px; padding: 8px 14px; flex: 0 0 auto;
            border-bottom: 1px solid var(--border); background: color-mix(in srgb, var(--bg-2) 92%, transparent);
            box-shadow: 0 1px 0 #00000008; }
    .tabs button { background: none; border: 0; padding: 7px 12px; border-radius: 8px;
                   color: var(--fg-dim); }
    .tabs button.on { background: var(--surface); color: var(--fg); box-shadow: var(--shadow); }
    .brand { display: inline-flex; align-items: center; gap: 8px; margin-right: 10px; font-weight: 750;
             letter-spacing: -.02em; white-space: nowrap; }
    .mark { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 9px;
            color: #fff; font-size: 11px; letter-spacing: -.04em;
            background: linear-gradient(135deg, var(--accent), var(--accent-2));
            box-shadow: 0 7px 18px color-mix(in srgb, var(--accent) 30%, transparent); }
    .tabs .env { margin-left: auto; padding: 1px 8px; border-radius: 999px;
                 background: var(--tag-manual); font-size: 11px; }
    .tabs .env.mock { background: var(--tag-path); }
    header { border-bottom: 1px solid var(--border); padding: 12px 14px 10px;
             background: color-mix(in srgb, var(--bg) 94%, var(--accent) 6%); }
    .bar { display: flex; gap: 8px; }
    .search { flex: 1; min-height: 42px; font-size: 15px; padding: 9px 13px;
              border-radius: 10px; box-shadow: inset 0 1px 2px #00000008; }
    .gear { flex: 0 0 auto; }
    .status { display: flex; gap: 10px; align-items: center; margin-top: 6px;
              font-size: 12px; color: var(--fg); }
    .status .dim, .grammar { color: var(--fg-dim); }
    .grammar { margin-left: auto; }
    .env { padding: 1px 7px; border-radius: 999px; background: var(--tag-manual); font-size: 11px; }
    .env.mock { background: var(--tag-path); }
    .sem { flex: 0 0 auto; }
    .sem.on { background: var(--tag-clip); border-color: var(--accent); }
    .filters { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-top: 9px; }
    .matchopts { display: flex; gap: 6px; flex-wrap: wrap; }
    .matchopts button { font-size: 12px; padding: 5px 11px; border-radius: 7px; font-weight: 500;
                        background: var(--bg-2); color: var(--fg-dim); border: 1px solid var(--border); }
    .matchopts button.on { background: var(--tag-clip); color: var(--fg); font-weight: 600;
                           border-color: var(--accent); }
    .confopts { display: flex; align-items: center; gap: 6px; }
    .confopts .conflabel { font-size: 11px; color: var(--fg-dim); margin-right: 2px; }
    .confopts button { font-size: 12px; padding: 5px 11px; border-radius: 7px; font-weight: 500;
                       background: var(--bg-2); color: var(--fg-dim); border: 1px solid var(--border); }
    .confopts button.on { background: var(--tag-learned); color: var(--fg); font-weight: 600;
                          border-color: var(--accent); }
    .mediaopt { display: flex; align-items: center; gap: 8px; }
    .mediaopt .conflabel { font-size: 12px; color: var(--fg-dim); font-weight: 500; }
    /* A native <select> here used to be tiny, low-contrast, and unclear at a
       glance (just emoji). A segmented button group matches .modeswitch's
       existing pattern -- bigger hit targets, the active choice is obvious,
       and the wording is spelled out instead of relying on emoji alone. */
    .segmented { display: inline-flex; border: 1px solid var(--border); border-radius: 8px;
                 overflow: hidden; }
    .segmented button { border: 0; border-radius: 0; background: var(--bg-2); color: var(--fg-dim);
                        padding: 7px 14px; font-size: 13px; font-weight: 500;
                        border-right: 1px solid var(--border); }
    .segmented button:last-child { border-right: 0; }
    .segmented button.on { background: var(--accent); color: #fff; font-weight: 600; }
    .synhelp { flex: 0 0 auto; font-size: 12px; padding: 7px 12px; }
    .synhelp.on { background: var(--tag-clip); border-color: var(--accent); }
    .synpanel { margin-top: 8px; padding: 10px 14px; border-radius: 10px; background: var(--bg-2);
                border: 1px solid var(--border); font-size: 12px; }
    .synpanel table { border-collapse: collapse; width: 100%; }
    .synpanel td { padding: 3px 10px 3px 0; vertical-align: top; }
    .synpanel td:first-child { white-space: nowrap; }
    .synpanel code { background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
                      padding: 1px 5px; font-size: 11px; }
    .synfoot { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); color: var(--fg-dim); }
    .qerr { margin-top: 6px; padding: 7px 12px; border-radius: 8px; font-size: 12px;
            background: color-mix(in srgb, #ef4444 15%, var(--bg-2));
            border: 1px solid color-mix(in srgb, #ef4444 40%, var(--border)); color: var(--fg); }
    .indexbar { display: flex; align-items: center; gap: 8px; margin-top: 9px; }
    .modeswitch { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
    .modeswitch button { border: 0; border-radius: 0; background: var(--bg-2); color: var(--fg-dim);
                         padding: 7px 13px; font-size: 13px; font-weight: 500; }
    .modeswitch button.on { background: var(--accent); color: #fff; font-weight: 600; }
    .ctl { font-size: 13px; padding: 7px 14px; border-radius: 8px;
           background: var(--bg-2); border: 1px solid var(--border); font-weight: 500; }
    .ctl.err { background: var(--tag-path); font-weight: 600; }
    /* Pause/Resume drives whether indexing runs at all -- the single most
       consequential control on this screen -- so it gets real visual weight
       instead of blending into the row like a minor toggle. */
    .pauseresume { font-size: 14px; font-weight: 700; padding: 7px 18px; border-radius: 999px;
                   border: 1px solid transparent; cursor: pointer; letter-spacing: .01em;
                   transition: filter .12s, transform .05s; }
    .pauseresume:active { transform: scale(.97); }
    .pauseresume.running { background: var(--accent); color: #fff; }
    .pauseresume.running:hover { filter: brightness(1.08); }
    .pauseresume.paused { background: #d97706; color: #fff;
                          box-shadow: 0 0 0 3px color-mix(in srgb, #d97706 25%, transparent);
                          animation: pausepulse 2s ease-in-out infinite; }
    .pauseresume.paused:hover { filter: brightness(1.08); }
    @keyframes pausepulse {
      0%, 100% { box-shadow: 0 0 0 3px color-mix(in srgb, #d97706 25%, transparent); }
      50% { box-shadow: 0 0 0 6px color-mix(in srgb, #d97706 12%, transparent); }
    }
    .idle { font-size: 12px; color: var(--fg-dim); margin-left: 4px; }
    .progress { position: relative; flex: 1; height: 18px; border-radius: 4px;
                background: var(--bg-2); overflow: hidden; }
    .progress.paused .fill { opacity: .2; }
    .progress .fill { position: absolute; inset: 0 auto 0 0; background: var(--accent);
                      opacity: .35; transition: width .3s; }
    .progress .pl { position: relative; font-size: 11px; padding: 0 6px; line-height: 16px;
                    color: var(--fg-dim); }
    .bulk { display: flex; gap: 8px; align-items: center; padding: 6px 12px;
            background: var(--sel); border-bottom: 1px solid var(--border); font-size: 13px; }
    .bulk input { flex: 0 0 240px; }
    main { display: grid; grid-template-columns: minmax(0, 1fr) 6px var(--preview-w, 420px);
           overflow: hidden; flex: 1; min-height: 0; }
    aside { min-width: 0; min-height: 0; height: 100%; overflow: hidden; }
    .resizer { cursor: col-resize; background: var(--border); }
    .resizer:hover { background: var(--accent); }
    .scanmsg { color: var(--fg-dim); font-size: 11px; white-space: nowrap; }
    .workingnotice { margin-top: 6px; font-size: 11px; color: var(--fg-dim);
                     font-variant-numeric: tabular-nums; display: flex; gap: 6px; }
    /* Fixed width (not max-width): the filename changes on every job, and a
       shrink-to-fit box would let the RAM figure after it visibly slide left
       and right as names of different lengths come and go. */
    .workingnotice .wl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 46ch; }
    .restartnotice { margin-top: 6px; padding: 6px 10px; border-radius: 8px; font-size: 12px;
                     background: color-mix(in srgb, #d97706 18%, var(--bg-2));
                     border: 1px solid color-mix(in srgb, #d97706 40%, var(--border)); color: var(--fg); }
    @media (max-width: 820px) {
      .tabs { overflow-x: auto; }
      .brand span:last-child { display: none; }
      .bar { flex-wrap: wrap; }
      .search { flex-basis: calc(100% - 120px); }
      .indexbar { flex-wrap: wrap; height: auto; min-height: 28px; }
      main { grid-template-columns: 1fr; grid-template-rows: minmax(260px, 1fr) minmax(240px, 42%); }
      aside { border-top: 1px solid var(--border); }
      .resizer { display: none; }
      .bulk { flex-wrap: wrap; }
      .bulk input { flex: 1 1 220px; }
    }
  `],
})
export class AppComponent {
  readonly lib = inject(LibraryService);
  readonly electron = IS_ELECTRON;
  readonly view = signal<View>('search');
  readonly showSyntaxHelp = signal(false);
  readonly previewWidth = signal(420);
  private resizingPreview = false;
  // Default "All" (null), not 0.5: the old per-facet storage floors (general
  // 0.35, character 0.75) are both above the new unified 0.25 storage floor but
  // straddle the 0.5 bottom of this list, so defaulting to 0.5 would silently
  // hide previously-visible general tags between 0.35-0.5. "All" preserves what
  // was visible before and makes the filter a pure "raise the bar" control.
  readonly confOptions: { value: number | null; label: string }[] = [
    { value: null, label: 'All' },
    { value: 0.5, label: '>0.5' },
    { value: 0.6, label: '>0.6' },
    { value: 0.7, label: '>0.7' },
    { value: 0.8, label: '>0.8' },
    { value: 0.9, label: '>0.9' },
  ];

  readonly selectedCount = computed(() => this.lib.selectedIds().size);
  readonly prog = this.lib.progress;
  readonly rescanning = signal(false);
  readonly scanMessage = signal('');
  readonly bulkMessage = signal('');
  readonly paused = computed(() => this.prog()?.paused ?? false);
  readonly mode = computed(() => this.prog()?.mode ?? 'auto');
  readonly errorCount = computed(() => this.prog()?.jobs['error'] ?? 0);
  readonly indexing = computed(() => {
    const p = this.prog();
    if (!p) return false;
    const active = (p.jobs['queued'] ?? 0) + (p.jobs['running'] ?? 0);
    return active > 0 || p.files_done < p.files_total;
  });
  readonly pct = computed(() => {
    const p = this.prog();
    return p && p.files_total ? Math.round((p.files_done / p.files_total) * 100) : 0;
  });
  // §12 visibility: what the background process is doing and how much RAM it
  // holds, so a large number in Task Manager has a visible cause in-app
  // instead of leaving the user to guess.
  readonly workingLabel = computed(() => {
    const c = this.prog()?.current;
    return c && c !== 'idle' ? c : '';
  });
  readonly ramLabel = computed(() => {
    const mb = this.prog()?.rss_mb;
    if (mb == null) return '';
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB RAM` : `${Math.round(mb)} MB RAM`;
  });
  jobsLabel() {
    const j = this.prog()?.jobs ?? {};
    return Object.entries(j).map(([k, v]) => `${k}:${v}`).join('  ');
  }
  constructor() {
    void this.lib.getSetting('preview_width', '420').then((v) => {
      const n = Number(v);
      if (Number.isFinite(n)) this.previewWidth.set(this.clampPreviewWidth(n));
    });
  }

  // The result grid's columns need a minimum ~300px to stay readable (name +
  // folder + kind + dim + size at their own CSS min-widths) — cap the preview
  // pane so it can never squeeze the grid narrower than that. Without this,
  // dragging the preview wide enough silently clipped trailing columns (Size
  // most visibly) instead of just refusing to go any wider.
  private static readonly MIN_GRID_WIDTH = 300;

  private clampPreviewWidth(w: number): number {
    const maxByWindow = Math.max(280, window.innerWidth - AppComponent.MIN_GRID_WIDTH - 6);
    return Math.min(900, maxByWindow, Math.max(280, w));
  }

  // Drag-to-resize the preview pane (§8.2 "larger render" — user-adjustable width).
  startResize(ev: MouseEvent) {
    ev.preventDefault();
    this.resizingPreview = true;
  }

  @HostListener('document:mousemove', ['$event'])
  onResizeMove(ev: MouseEvent) {
    if (!this.resizingPreview) return;
    this.previewWidth.set(this.clampPreviewWidth(window.innerWidth - ev.clientX));
  }

  @HostListener('document:mouseup')
  onResizeEnd() {
    if (!this.resizingPreview) return;
    this.resizingPreview = false;
    void this.lib.setSetting('preview_width', String(this.previewWidth()));
  }

  // Re-clamp if the OS window itself is resized smaller (not just the drag
  // handle) — otherwise shrinking the window could leave a stale, now-too-wide
  // preview width squeezing the grid the same way.
  @HostListener('window:resize')
  onWindowResize() {
    this.previewWidth.set(this.clampPreviewWidth(this.previewWidth()));
  }

  onInput(v: string) {
    // The query signal updates synchronously (so [value] stays in lockstep
    // with what was just typed — no lag to fight the caret) — only the
    // actual DB search is debounced, inside LibraryService.setQuery().
    this.lib.setQuery(v);
  }

  bulkAdd(input: HTMLInputElement) {
    const [cat, ...rest] = input.value.split(':');
    const name = rest.join(':').trim();
    if (cat?.trim() && name) { void this.lib.bulkAdd(cat.trim(), name); input.value = ''; }
  }

  async bulkTrain(input: HTMLInputElement) {
    const [cat, ...rest] = input.value.split(':');
    const name = rest.join(':').trim();
    if (!cat?.trim() || !name) {
      this.bulkMessage.set('Use category:name');
      return;
    }
    this.bulkMessage.set('Training…');
    const result = await this.lib.bulkTrain(cat.trim(), name);
    this.bulkMessage.set(result.ok
      ? `Trained from ${result.tagged} selected image(s); applied to ${result.applied ?? 0} similar image(s).`
      : `Training failed: ${result.error ?? 'not enough examples'}`);
    input.value = '';
  }

  // Everything-style match-mode shortcuts (mirrors the reference: Ctrl+I/B/U/M).
  @HostListener('document:keydown', ['$event'])
  onKey(e: KeyboardEvent) {
    if (!e.ctrlKey || e.altKey || this.view() !== 'search' || this.lib.semantic()) return;
    const map: Record<string, 'matchCase' | 'wholeWord' | 'matchPath' | 'matchDiacritics'> = {
      i: 'matchCase', b: 'wholeWord', u: 'matchPath', m: 'matchDiacritics',
    };
    const opt = map[e.key.toLowerCase()];
    if (opt) { e.preventDefault(); this.lib.toggleMatch(opt); }
  }

  pause() { void this.lib.pauseIndexing(); }
  resume() { void this.lib.resumeIndexing(); }
  setMode(m: 'auto' | 'manual') { void this.lib.setMode(m); }
  retry() { void this.lib.retryErrors(); }
  async rescan() {
    this.rescanning.set(true);
    this.scanMessage.set('');
    try {
      const r = await this.lib.rescan();
      this.scanMessage.set(`${r.added} added, ${r.changed} changed, ${r.removed} removed`);
    } catch (e) {
      this.scanMessage.set(`Rescan failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.rescanning.set(false);
    }
  }
}
