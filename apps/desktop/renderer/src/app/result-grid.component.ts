import { AfterViewChecked, Component, ElementRef, HostListener, OnDestroy, ViewChild, computed, inject, signal } from '@angular/core';
import { CdkVirtualScrollViewport, ScrollingModule } from '@angular/cdk/scrolling';
import { LibraryService } from './library.service';
import { FileDetail, FileRow, SearchOpts, TagRow, getApi } from './api';

/** Virtualized, sortable result list (§8.1): renders only visible rows via CDK
 * virtual scroll so hundreds of thousands of results scroll with zero lag.
 * Columns sort in SQL (the service re-queries with ORDER BY), never in JS.
 *
 * Three view modes (list / small thumbnails / large thumbnails): the grid
 * modes chunk results into rows of N files (N computed from the panel's
 * actual width via ResizeObserver) so the same fixed-size CDK virtual
 * scroller that makes the list fast also works for thumbnails — only the
 * chunk size and cell template differ. */
@Component({
  selector: 'app-result-grid',
  standalone: true,
  imports: [ScrollingModule],
  template: `
    <div class="viewbar">
      <button [class.on]="viewMode() === 'list'" (click)="setViewMode('list')"
              title="Detail list" data-testid="view-list">☰ List</button>
      <button [class.on]="viewMode() === 'small'" (click)="setViewMode('small')"
              title="Small thumbnails" data-testid="view-small">▦ Small</button>
      <button [class.on]="viewMode() === 'large'" (click)="setViewMode('large')"
              title="Extra large thumbnails" data-testid="view-large">⬛ Large</button>
    </div>

    @if (!lib.loading() && lib.results().length === 0) {
      <div class="empty" data-testid="empty-state">
        <div class="emptyicon">⌕</div>
        <h2>{{ lib.query() ? 'No matching images' : 'Your library is empty' }}</h2>
        <p>{{ lib.query() ? 'Try fewer terms or turn off a match filter.' : 'Open Sources, add an image folder, then run Rescan.' }}</p>
      </div>
    } @else if (viewMode() === 'list') {
      <div class="header">
        @for (c of columns; track c.key) {
          <div class="cell {{ c.cls }}" [style.flex]="colFlex(c.cls)" (click)="sort(c.key)">
            <span class="label">{{ c.label }}</span>
            @if (lib.sort() === c.key) { <span class="arrow">{{ lib.dir() === 'asc' ? '▲' : '▼' }}</span> }
            <span class="resizer" title="drag to resize" (pointerdown)="startResize($event, c.cls)"
                  (pointermove)="doResize($event)" (pointerup)="endResize($event)"
                  (pointercancel)="endResize($event)" (dblclick)="resetWidth($event, c.cls)"
                  (click)="$event.stopPropagation()"></span>
          </div>
        }
        <div class="filler"></div>
      </div>

      <div class="gridbody" #bodyEl (pointerdown)="startMarquee($event)"
           (pointermove)="moveMarquee($event)" (pointerup)="endMarquee($event)"
           (pointercancel)="endMarquee($event)">
        <cdk-virtual-scroll-viewport itemSize="26" class="viewport" data-testid="viewport">
          <div *cdkVirtualFor="let f of lib.results(); trackBy: trackId"
               class="row" [class.sel]="selected(f.id)" [attr.data-file-id]="f.id"
               (click)="click(f, $event)" (dblclick)="open(f)"
               (contextmenu)="showMenu(f, $event)" data-testid="row">
            <div class="cell name" [style.flex]="colFlex('name')" [title]="f.path">{{ f.filename }}</div>
            <div class="cell folder" [style.flex]="colFlex('folder')" [title]="f.folder">{{ f.folder }}</div>
            <div class="cell kind" [style.flex]="colFlex('kind')" [title]="f.image_kind || ''">{{ isVideo(f) ? '▶ video' : (f.image_kind || '—') }}</div>
            <div class="cell dim" [style.flex]="colFlex('dim')">{{ f.width }}×{{ f.height }}</div>
            <div class="cell size" [style.flex]="colFlex('size')">{{ sizeKb(f) }}</div>
            <div class="filler"></div>
          </div>
        </cdk-virtual-scroll-viewport>
        @if (marquee()) {
          <div class="marquee" [style.left.px]="marquee()!.left" [style.top.px]="marquee()!.top"
               [style.width.px]="marquee()!.width" [style.height.px]="marquee()!.height"></div>
        }
      </div>
    } @else {
      <div class="gridbody" #bodyEl (pointerdown)="startMarquee($event)"
           (pointermove)="moveMarquee($event)" (pointerup)="endMarquee($event)"
           (pointercancel)="endMarquee($event)">
        <cdk-virtual-scroll-viewport [itemSize]="rowHeight()" class="viewport thumbview" data-testid="viewport">
          <div *cdkVirtualFor="let chunk of rows(); trackBy: trackRow"
               class="thumbrow" [style.height.px]="rowHeight()">
            @for (f of chunk; track f.id) {
              <div class="thumbcell" [class.sel]="selected(f.id)" [attr.data-file-id]="f.id"
                   [style.width.px]="cellWidth()"
                   (click)="click(f, $event)" (dblclick)="open(f)"
                   (contextmenu)="showMenu(f, $event)" data-testid="row">
                <div class="thumbimg" [style.width.px]="thumbSize()" [style.height.px]="thumbSize()">
                  @if (thumbOf(f.id); as url) {
                    <img [src]="url" [alt]="f.filename" loading="lazy" />
                  } @else {
                    <div class="ph"></div>
                  }
                  @if (isVideo(f)) { <span class="playbadge">▶</span> }
                </div>
                <div class="thumblabel" [title]="f.path">{{ f.filename }}</div>
              </div>
            }
          </div>
        </cdk-virtual-scroll-viewport>
        @if (marquee()) {
          <div class="marquee" [style.left.px]="marquee()!.left" [style.top.px]="marquee()!.top"
               [style.width.px]="marquee()!.width" [style.height.px]="marquee()!.height"></div>
        }
      </div>
    }

    @if (menu(); as m) {
      <div class="ctx" [style.left.px]="m.x" [style.top.px]="m.y"
           (pointerdown)="$event.stopPropagation()" data-testid="picture-menu">
        <button (click)="menuAction('open')">Open image</button>
        <button (click)="menuAction('reveal')">Open folder path</button>
        <button (click)="menuAction('copy')">Copy full path</button>
        <div class="sep"></div>
        <button (click)="menuAction('tag')">Add tag to selected…</button>
        <button (click)="menuAction('train')">Train tag from selected…</button>
        <button (click)="menuAction('face')">Tag face…</button>
        <button (click)="menuAction('reindex')">Re-index selected</button>
        <div class="sep"></div>
        <button (click)="menuAction('properties')">Properties</button>
        <button (click)="menuAction('select-all')">Select all results</button>
        <button (click)="lib.clearSelection(); menu.set(null)">Clear selection</button>
      </div>
    }

    @if (propertiesFile(); as p) {
      <div class="modalback" (click)="propertiesFile.set(null)">
        <section class="properties" (click)="$event.stopPropagation()" data-testid="properties-dialog">
          <h3>Properties</h3>
          <div><b>Name</b><span>{{ p.filename }}</span></div>
          <div><b>Path</b><span>{{ p.path }}</span></div>
          <div><b>Type</b><span>{{ p.image_kind || 'unknown' }}</span></div>
          <div><b>Dimensions</b><span>{{ p.width || '—' }} × {{ p.height || '—' }}</span></div>
          <div><b>Size</b><span>{{ sizeKb(p) }}</span></div>
          <div><b>Modified</b><span>{{ modified(p) }}</span></div>
          <div><b>Tags</b><span>{{ propertiesTags().length }}</span></div>
          <div><b>Faces</b><span>{{ propertiesDetail()?.faces?.length || 0 }}</span></div>
          <button (click)="propertiesFile.set(null)">Close</button>
        </section>
      </div>
    }
  `,
  styles: [`
    :host { display: flex; flex-direction: column; height: 100%; min-width: 0; }
    .empty { flex: 1; display: grid; place-content: center; justify-items: center; text-align: center;
             padding: 32px; color: var(--fg-dim); }
    .emptyicon { display: grid; place-items: center; width: 60px; height: 60px; margin-bottom: 14px;
                 border-radius: 20px; font-size: 34px; color: var(--accent);
                 background: color-mix(in srgb, var(--accent) 12%, var(--surface));
                 border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border)); }
    .empty h2 { margin: 0 0 7px; color: var(--fg); font-size: 18px; }
    .empty p { margin: 0; max-width: 420px; }
    .viewbar { display: flex; gap: 6px; padding: 6px 8px; border-bottom: 1px solid var(--border);
               background: var(--bg-2); flex: 0 0 auto; }
    .viewbar button { font-size: 12px; padding: 5px 11px; }
    .viewbar button.on { background: var(--accent); color: #fff; border-color: transparent; font-weight: 600; }
    .header { display: flex; border-bottom: 1px solid var(--border);
              background: var(--bg-2); font-size: 12px; color: var(--fg-dim);
              position: sticky; top: 0; user-select: none; }
    .header .cell { cursor: pointer; position: relative; }
    .header .cell:hover { color: var(--fg); }
    .header .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
    .resizer { position: absolute; top: 0; right: 0; bottom: 0; width: 7px;
               cursor: col-resize; touch-action: none; z-index: 2; }
    .resizer:hover, .resizer:active { background: var(--accent); opacity: .45; }
    .gridbody { position: relative; flex: 1; min-height: 0; overflow: hidden; }
    .viewport { height: 100%; user-select: none; }
    .row { display: flex; align-items: center; height: 26px; cursor: pointer;
           border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent); }
    .row:hover { background: var(--bg-2); }
    .row.sel { background: var(--sel); }
    /* A small explicit min-width (not 0) is required on every column: it's
       what lets text-overflow: ellipsis actually shrink a nowrap cell down to
       its allotted width instead of overflowing it, while still guaranteeing
       every column stays visible — a bare "min-width: 0" here let columns
       shrink all the way to nothing under space pressure (e.g. a widened
       preview pane), which is what made Size silently disappear. */
    .cell { padding: 0 8px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
            box-sizing: border-box; }
    .name { font-weight: 500; min-width: 60px; }
    .folder { color: var(--fg-dim); min-width: 60px; }
    .kind { min-width: 40px; }
    .dim  { color: var(--fg-dim); min-width: 40px; }
    .size { min-width: 50px; color: var(--fg-dim); text-align: right; }
    /* Absorbs whatever width is left after the 5 real (now all independently
       resizable, fixed-width) columns, so the row still fills the panel with
       no dead space or horizontal scroll — without forcing Size itself to
       stretch to fill it, which used to make it balloon to hundreds of
       pixels wider than its content needs. */
    .filler { flex: 1 1 auto; }
    .arrow { font-size: 9px; margin-left: 3px; }
    /* Thumbnail grid modes: same virtualized viewport as the list, but each
       virtual "item" is a whole row of thumbnail cells (chunk size computed
       from the panel's real width) rather than one file — the fixed-size
       strategy CDK needs stays satisfied either way. */
    .thumbview { user-select: none; }
    .thumbrow { display: flex; gap: 10px; padding: 0 10px; box-sizing: border-box; }
    .thumbcell { display: flex; flex-direction: column; gap: 4px; padding: 6px; cursor: pointer;
                 border-radius: 8px; border: 1px solid transparent; box-sizing: border-box; }
    .thumbcell:hover { background: var(--bg-2); }
    .thumbcell.sel { background: var(--sel); border-color: var(--accent); }
    .thumbimg { position: relative; background: var(--bg-2); border-radius: 6px; overflow: hidden;
                display: flex; align-items: center; justify-content: center; }
    .thumbimg img { width: 100%; height: 100%; object-fit: cover; }
    .playbadge { position: absolute; bottom: 5px; right: 5px; width: 20px; height: 20px;
                 border-radius: 50%; background: rgba(0,0,0,.65); color: #fff;
                 display: flex; align-items: center; justify-content: center;
                 font-size: 10px; padding-left: 2px; }
    .thumbimg .ph { width: 100%; height: 100%; background:
      linear-gradient(90deg, var(--bg-2) 25%, color-mix(in srgb, var(--bg-2) 60%, var(--border)) 37%, var(--bg-2) 63%);
      background-size: 400% 100%; animation: shimmer 1.4s ease-in-out infinite; }
    @keyframes shimmer { 0% { background-position: 100% 0; } 100% { background-position: 0 0; } }
    .thumblabel { font-size: 11px; text-align: center; overflow: hidden; text-overflow: ellipsis;
                  white-space: nowrap; color: var(--fg-dim); }
    .thumbcell.sel .thumblabel { color: var(--fg); }
    .marquee { position: fixed; z-index: 30; pointer-events: none;
               border: 1px solid var(--accent); background: color-mix(in srgb, var(--accent) 20%, transparent); }
    .ctx { position: fixed; z-index: 100; min-width: 210px; display: flex; flex-direction: column;
           padding: 5px; border: 1px solid var(--border); border-radius: 8px;
           background: var(--bg-2); box-shadow: var(--shadow); }
    .ctx button { text-align: left; border: 0; background: transparent; padding: 6px 10px;
                  border-radius: 5px; color: var(--fg); }
    .ctx button:hover { background: var(--sel); }
    .ctx .sep { height: 1px; background: var(--border); margin: 4px 2px; }
    .modalback { position: fixed; inset: 0; z-index: 110; display: grid; place-items: center;
                 background: #0008; }
    .properties { width: min(620px, 85vw); max-height: 80vh; overflow: auto; padding: 18px;
                  border: 1px solid var(--border); border-radius: 10px; background: var(--bg); box-shadow: var(--shadow); }
    .properties h3 { margin: 0 0 14px; }
    .properties div { display: grid; grid-template-columns: 100px 1fr; gap: 10px; margin: 7px 0; }
    .properties span { word-break: break-all; color: var(--fg-dim); }
    .properties > button { float: right; margin-top: 12px; }
  `],
})
export class ResultGridComponent implements AfterViewChecked, OnDestroy {
  readonly lib = inject(LibraryService);
  private api = getApi();

  // --- View mode (§8.1): list / small thumbnails / large thumbnails --------
  private static readonly VIEW_KEY = 'imageTagger.resultGrid.viewMode';
  private static readonly THUMB_GAP = 10;
  // Must match .thumbcell's own CSS padding: the cell's *outer* width has to
  // include it, or the fixed-size .thumbimg inside (bound to the same
  // thumbSize) overflows the cell's padded content box on the right/bottom —
  // pushing the image past the 'selected' highlight border drawn on the cell
  // itself, instead of the border cleanly framing it.
  private static readonly THUMB_PADDING = 6;
  private static readonly THUMB_SIZES: Record<'small' | 'large', number> = { small: 120, large: 280 };

  readonly viewMode = signal<'list' | 'small' | 'large'>(this.loadViewMode());
  readonly thumbSize = computed(() => ResultGridComponent.THUMB_SIZES[this.viewMode() === 'large' ? 'large' : 'small']);
  readonly cellWidth = computed(() => this.thumbSize() + ResultGridComponent.THUMB_PADDING * 2);
  // Label + padding above the square thumbnail.
  readonly rowHeight = computed(() => this.thumbSize() + 30);
  private readonly bodyWidth = signal(800);
  readonly itemsPerRow = computed(() => Math.max(1,
    Math.floor((this.bodyWidth() - 20) / (this.cellWidth() + ResultGridComponent.THUMB_GAP))));
  readonly rows = computed(() => {
    const n = this.itemsPerRow();
    const results = this.lib.results();
    const out: FileRow[][] = [];
    for (let i = 0; i < results.length; i += n) out.push(results.slice(i, i + n));
    return out;
  });
  trackRow(_i: number, row: FileRow[]) { return row.length ? row[0].id : _i; }

  private thumbs = signal<Map<number, string | null>>(new Map());
  private pendingThumbs = new Set<number>();
  thumbOf(id: number): string | null {
    const cached = this.thumbs().get(id);
    if (cached !== undefined) return cached;
    if (!this.pendingThumbs.has(id)) {
      this.pendingThumbs.add(id);
      void this.api.thumb(id).then((url) => {
        this.pendingThumbs.delete(id);
        this.thumbs.update((m) => { const n = new Map(m); n.set(id, url); return n; });
      });
    }
    return null;
  }

  setViewMode(mode: 'list' | 'small' | 'large') {
    this.viewMode.set(mode);
    try { localStorage.setItem(ResultGridComponent.VIEW_KEY, mode); } catch { /* storage unavailable */ }
  }
  private loadViewMode(): 'list' | 'small' | 'large' {
    try {
      const v = localStorage.getItem(ResultGridComponent.VIEW_KEY);
      if (v === 'list' || v === 'small' || v === 'large') return v;
    } catch { /* storage unavailable */ }
    return 'list';
  }

  @ViewChild('bodyEl') bodyEl?: ElementRef<HTMLElement>;
  private bodyObserver?: ResizeObserver;
  private observedEl?: HTMLElement;

  ngAfterViewChecked() {
    // .gridbody is inside an @if/@else on viewMode, so switching list <->
    // grid destroys and recreates the element -- re-attach the observer
    // whenever the underlying DOM node actually changes (cheap no-op
    // otherwise). Recomputing itemsPerRow on resize (window resize, or
    // dragging the preview-pane splitter) keeps thumbnail rows from going
    // stale after either.
    const el = this.bodyEl?.nativeElement;
    if (el && el !== this.observedEl) {
      this.bodyObserver?.disconnect();
      this.bodyObserver = new ResizeObserver((entries) => {
        const w = entries[0]?.contentRect.width;
        if (w) this.bodyWidth.set(w);
      });
      this.bodyObserver.observe(el);
      this.observedEl = el;
    }
  }

  ngOnDestroy() {
    this.bodyObserver?.disconnect();
  }

  readonly columns: { key: NonNullable<SearchOpts['sort']>; label: string; cls: string }[] = [
    { key: 'name', label: 'Name', cls: 'name' },
    { key: 'path', label: 'Folder', cls: 'folder' },
    { key: 'kind', label: 'Kind', cls: 'kind' },
    { key: 'dim', label: 'Dimensions', cls: 'dim' },
    { key: 'size', label: 'Size', cls: 'size' },
  ];

  // All 5 columns are independently resizable, fixed-width; a trailing
  // invisible .filler div (flex:1) soaks up whatever width is left so the
  // row still fills the panel with no dead space — instead of forcing one
  // real column (previously Size) to stretch and absorb it, which made that
  // column balloon to hundreds of pixels wider than its content ever needed.
  private static readonly DEFAULT_WIDTHS: Record<string, number> = {
    name: 260, folder: 340, kind: 80, dim: 100, size: 90,
  };
  private static readonly MIN_COL_WIDTH = 40;
  private static readonly STORAGE_KEY = 'imageTagger.resultGrid.colWidths';

  readonly colWidths = signal<Record<string, number>>(this.loadColWidths());
  private resize?: { key: string; startX: number; startWidth: number };

  colFlex(cls: string): string {
    // shrink:1 (not 0) so columns compress together instead of overflowing
    // the panel — and staying rigid — when the window/panel is narrower than
    // the sum of the user's chosen widths.
    return `0 1 ${this.colWidths()[cls] ?? ResultGridComponent.DEFAULT_WIDTHS[cls] ?? 100}px`;
  }

  startResize(ev: PointerEvent, key: string) {
    ev.preventDefault();
    ev.stopPropagation();
    this.resize = {
      key, startX: ev.clientX, startWidth: this.colWidths()[key] ?? ResultGridComponent.DEFAULT_WIDTHS[key],
    };
    try { (ev.currentTarget as HTMLElement).setPointerCapture(ev.pointerId); } catch { /* synthetic/test pointer */ }
  }

  doResize(ev: PointerEvent) {
    const r = this.resize;
    if (!r) return;
    ev.preventDefault();
    const w = Math.max(ResultGridComponent.MIN_COL_WIDTH, r.startWidth + (ev.clientX - r.startX));
    this.colWidths.update((prev) => ({ ...prev, [r.key]: w }));
  }

  endResize(ev: PointerEvent) {
    if (!this.resize) return;
    try { (ev.currentTarget as HTMLElement).releasePointerCapture(ev.pointerId); } catch { /* already released */ }
    this.resize = undefined;
    this.persistColWidths();
  }

  resetWidth(ev: MouseEvent, key: string) {
    ev.stopPropagation();
    this.colWidths.update((prev) => ({ ...prev, [key]: ResultGridComponent.DEFAULT_WIDTHS[key] }));
    this.persistColWidths();
  }

  private loadColWidths(): Record<string, number> {
    try {
      const raw = localStorage.getItem(ResultGridComponent.STORAGE_KEY);
      if (raw) return { ...ResultGridComponent.DEFAULT_WIDTHS, ...JSON.parse(raw) };
    } catch { /* corrupt/unavailable storage — fall back to defaults */ }
    return { ...ResultGridComponent.DEFAULT_WIDTHS };
  }

  private persistColWidths() {
    try { localStorage.setItem(ResultGridComponent.STORAGE_KEY, JSON.stringify(this.colWidths())); } catch { /* storage unavailable */ }
  }

  @ViewChild(CdkVirtualScrollViewport) viewport?: CdkVirtualScrollViewport;

  readonly sel = computed(() => this.lib.selectedIds());
  readonly marquee = signal<{ left: number; top: number; width: number; height: number } | null>(null);
  readonly menu = signal<{ x: number; y: number; file: FileRow } | null>(null);
  readonly propertiesFile = signal<FileRow | null>(null);
  readonly propertiesTags = signal<TagRow[]>([]);
  readonly propertiesDetail = signal<FileDetail | null>(null);
  private anchorId: number | null = null;
  private drag?: { x: number; y: number; pointerId: number; base: Set<number> };
  private dragged = false;
  private suppressClick = false;
  selected(id: number) { return this.sel().has(id); }

  trackId(_i: number, f: FileRow) { return f.id; }
  isVideo(f: FileRow): boolean { return !!f.mime && f.mime.startsWith('video/'); }
  sort(key: NonNullable<SearchOpts['sort']>) { this.lib.toggleSort(key); }

  click(f: FileRow, ev: MouseEvent) {
    if (this.suppressClick) return;
    const rows = this.lib.results();
    if (ev.shiftKey && this.anchorId != null) {
      const a = rows.findIndex((row) => row.id === this.anchorId);
      const b = rows.findIndex((row) => row.id === f.id);
      if (a >= 0 && b >= 0) {
        const ids = rows.slice(Math.min(a, b), Math.max(a, b) + 1).map((row) => row.id);
        void this.lib.selectMany(ids, f.id, { additive: ev.ctrlKey || ev.metaKey });
        return;
      }
    }
    this.anchorId = f.id;
    void this.lib.select(f.id, { additive: ev.ctrlKey || ev.metaKey });
  }

  open(f: FileRow) { void this.lib.select(f.id); this.lib.openSelected(); }

  startMarquee(ev: PointerEvent) {
    if (ev.button !== 0 || !(ev.target as HTMLElement).closest('.viewport')) return;
    this.menu.set(null);
    this.drag = {
      x: ev.clientX, y: ev.clientY, pointerId: ev.pointerId,
      base: ev.ctrlKey || ev.metaKey ? new Set(this.lib.selectedIds()) : new Set<number>(),
    };
    this.dragged = false;
  }

  moveMarquee(ev: PointerEvent) {
    const start = this.drag;
    if (!start || start.pointerId !== ev.pointerId) return;
    const dx = ev.clientX - start.x;
    const dy = ev.clientY - start.y;
    if (!this.dragged && Math.hypot(dx, dy) < 5) return;
    if (!this.dragged) {
      this.dragged = true;
      // Capturing on pointerdown retargets an ordinary mouse click away from
      // the row, so selecting/opening a picture stops working. Capture only
      // after the pointer has actually crossed the marquee drag threshold.
      try { (ev.currentTarget as HTMLElement).setPointerCapture?.(ev.pointerId); } catch { /* synthetic/test pointer */ }
    }
    ev.preventDefault();
    const rect = {
      left: Math.min(start.x, ev.clientX), top: Math.min(start.y, ev.clientY),
      width: Math.abs(dx), height: Math.abs(dy),
    };
    this.marquee.set(rect);
    const right = rect.left + rect.width;
    const bottom = rect.top + rect.height;
    const hits: number[] = [];
    const root = this.viewport?.elementRef.nativeElement as HTMLElement | undefined;
    for (const row of Array.from(root?.querySelectorAll<HTMLElement>('[data-file-id]') ?? [])) {
      const r = row.getBoundingClientRect();
      if (r.right >= rect.left && r.left <= right && r.bottom >= rect.top && r.top <= bottom) {
        hits.push(Number(row.dataset['fileId']));
      }
    }
    const selected = new Set(start.base);
    for (const id of hits) selected.add(id);
    const focus = hits.at(-1) ?? [...selected].at(-1) ?? null;
    void this.lib.selectMany(selected, focus, { load: false });
  }

  endMarquee(ev: PointerEvent) {
    if (!this.drag || this.drag.pointerId !== ev.pointerId) return;
    try { (ev.currentTarget as HTMLElement).releasePointerCapture?.(ev.pointerId); } catch { /* already released */ }
    this.drag = undefined;
    this.marquee.set(null);
    if (!this.dragged) return;
    this.suppressClick = true;
    const focus = this.lib.selectedId();
    void this.lib.selectMany(this.lib.selectedIds(), focus);
    this.anchorId = focus;
    setTimeout(() => { this.suppressClick = false; }, 0);
  }

  showMenu(file: FileRow, ev: MouseEvent) {
    ev.preventDefault();
    ev.stopPropagation();
    if (!this.lib.selectedIds().has(file.id)) {
      this.anchorId = file.id;
      void this.lib.select(file.id);
    }
    this.menu.set({
      x: Math.min(ev.clientX, window.innerWidth - 230),
      y: Math.min(ev.clientY, window.innerHeight - 330), file,
    });
  }

  async menuAction(action: 'open' | 'reveal' | 'copy' | 'tag' | 'train' | 'face' |
                   'reindex' | 'properties' | 'select-all') {
    const entry = this.menu();
    if (!entry) return;
    this.menu.set(null);
    if (action === 'open') this.lib.openFile(entry.file);
    else if (action === 'reveal') this.lib.revealFile(entry.file);
    else if (action === 'copy') await this.lib.copyPath(entry.file);
    else if (action === 'select-all') {
      const rows = this.lib.results();
      await this.lib.selectMany(rows.map((row) => row.id), entry.file.id);
    } else if (action === 'reindex') {
      const count = await this.lib.reindexFiles(this.lib.selectedIds());
      window.alert(`Queued ${count} image(s) for re-indexing.`);
    } else if (action === 'properties') {
      this.propertiesFile.set(entry.file);
      this.propertiesTags.set(await this.lib.fileTags(entry.file.id));
      this.propertiesDetail.set(await this.lib.fileDetail(entry.file.id));
    } else if (action === 'tag' || action === 'train') {
      const value = window.prompt(action === 'train'
        ? 'Train tag from selected images (category:name)'
        : 'Add tag to selected images (category:name)', 'general:');
      const parsed = this.parseTag(value);
      if (!parsed) return;
      if (action === 'tag') {
        const n = await this.lib.bulkAdd(parsed.category, parsed.name);
        window.alert(`Added ${parsed.category}:${parsed.name} to ${n} image(s).`);
      } else {
        const result = await this.lib.bulkTrain(parsed.category, parsed.name);
        window.alert(result.ok
          ? `Training complete from ${result.tagged} image(s); applied to ${result.applied ?? 0} similar image(s).`
          : `Training failed: ${result.error ?? 'not enough examples'}`);
      }
    } else if (action === 'face') {
      const name = window.prompt('Name all detected face clusters in the selected images:');
      if (!name?.trim()) return;
      const result = await this.lib.tagFacesSelected(name.trim());
      window.alert(result.faces
        ? `Named ${result.people} person cluster(s) across ${result.faces} detected face(s).`
        : 'No detected faces in the selected images. Enable the face model and re-index them first.');
    }
  }

  private parseTag(value: string | null): { category: string; name: string } | null {
    if (!value) return null;
    const [category, ...rest] = value.split(':');
    const name = rest.join(':').trim();
    return category.trim() && name ? { category: category.trim(), name } : null;
  }

  modified(file: FileRow): string {
    return file.mtime ? new Date(file.mtime * 1000).toLocaleString() : '—';
  }

  // Keyboard-first navigation (§8.1): ↑/↓ move the selection, Enter opens the
  // file in the OS viewer. Ignored while typing in the search bar / editors.
  @HostListener('document:pointerdown')
  closeMenu() { this.menu.set(null); }

  @HostListener('document:keydown', ['$event'])
  onKey(e: KeyboardEvent) {
    const tag = (document.activeElement?.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    if (e.key === 'Escape') {
      this.menu.set(null); this.propertiesFile.set(null); return;
    }
    const rows = this.lib.results();
    if (!rows.length) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      const focus = this.lib.selectedId() ?? rows[0].id;
      void this.lib.selectMany(rows.map((row) => row.id), focus);
      return;
    }
    const cur = rows.findIndex((r) => r.id === this.lib.selectedId());
    // In the thumbnail grid modes, one virtual scroll "item" is a whole row
    // of N cells, not one file -- Down/Up step by a full row (N files) and
    // Left/Right step by one file, instead of Up/Down stepping by one file
    // as in the list.
    const step = this.viewMode() === 'list' ? 1 : this.itemsPerRow();
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      const idx = cur < 0 ? 0 : Math.min(rows.length - 1, cur + step);
      void this.lib.select(rows[idx].id); this.reveal(idx);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      const idx = cur < 0 ? 0 : Math.max(0, cur - step);
      void this.lib.select(rows[idx].id); this.reveal(idx);
    } else if (e.key === 'ArrowRight' && this.viewMode() !== 'list') {
      e.preventDefault();
      const idx = cur < 0 ? 0 : Math.min(rows.length - 1, cur + 1);
      void this.lib.select(rows[idx].id); this.reveal(idx);
    } else if (e.key === 'ArrowLeft' && this.viewMode() !== 'list') {
      e.preventDefault();
      const idx = cur < 0 ? 0 : Math.max(0, cur - 1);
      void this.lib.select(rows[idx].id); this.reveal(idx);
    } else if (e.key === 'Enter' && this.lib.selectedId() != null) {
      e.preventDefault(); this.lib.openSelected();
    }
  }

  private reveal(index: number) {
    const vp = this.viewport;
    if (!vp) return;
    // Convert a flat file index to a virtual-scroll item index: 1:1 in list
    // mode, but one item per row of N files in the thumbnail grid modes.
    const itemIndex = this.viewMode() === 'list' ? index : Math.floor(index / this.itemsPerRow());
    const range = vp.getRenderedRange();
    if (itemIndex < range.start + 1 || itemIndex > range.end - 2) vp.scrollToIndex(Math.max(0, itemIndex - 3), 'smooth');
  }

  sizeKb(f: FileRow) {
    const b = f.size_bytes ?? 0;
    return b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round(b / 1e3)} KB`;
  }
}
