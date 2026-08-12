import { Component, HostListener, signal } from '@angular/core';
import { Person, getApi } from './api';
import { ZoomableLightboxComponent } from './zoomable-lightbox.component';

/** Faces / persons naming screen (spec §5/§15): review clusters, name one once
 * (new faces then auto-attach), merge duplicates, and preview a cluster's files. */
@Component({
  selector: 'app-faces',
  standalone: true,
  imports: [ZoomableLightboxComponent],
  template: `
    <div class="wrap">
      <div class="head">
        <h2>People</h2>
        <span class="dim">{{ persons().length }} cluster(s) · name one once, new faces auto-attach</span>
        <button (click)="refresh()">Refresh</button>
      </div>

      @if (loading()) {
        <div class="loadbar" data-testid="loading"><div class="stripe"></div></div>
        <p class="dim">Loading people…</p>
      } @else if (loadError()) {
        <div class="errbanner" data-testid="load-error">
          Couldn't load people: {{ loadError() }}
          <button (click)="refresh()">Retry</button>
        </div>
      } @else if (persons().length === 0) {
        <p class="dim">No faces detected yet. This screen is populated by real face
          detection (InsightFace) clustering photos automatically — it's separate from
          adding a <code>person:name</code> tag by hand in the preview pane, which doesn't
          touch this list. Enable the "faces" facet on the Models tab (download the model
          if you haven't, then reindex) to start clustering faces here.</p>
      }

      <div class="grid">
        @for (p of persons(); track p.id) {
          <div class="card" data-testid="person-card" [class.sel]="selected() === p.id">
            <div class="avatar" (click)="openLightbox(p)" title="click to enlarge">
              @if (avatars().get(p.id); as url) {
                <img [src]="url" alt="" />
              } @else {
                <span class="placeholder">🙂</span>
              }
            </div>
            <input class="name" [value]="p.name || ''" placeholder="(unnamed)"
                   (keydown.enter)="rename(p, $any($event.target).value)"
                   (blur)="rename(p, $any($event.target).value)" data-testid="name-input" />
            <div class="meta">{{ p.faces }} face(s)</div>
            <div class="actions">
              <button (click)="view(p)">view</button>
              @if (persons().length > 1) {
                <select (change)="merge(p, +$any($event.target).value); $any($event.target).value=''"
                        data-testid="merge-select">
                  <option value="">merge into…</option>
                  @for (o of others(p); track o.id) {
                    <option [value]="o.id">{{ o.name || '(unnamed #' + o.id + ')' }}</option>
                  }
                </select>
              }
            </div>
          </div>
        }
      </div>

      @if (selected() != null) {
        <div class="overlay" data-testid="person-files-overlay" (click)="close()">
          <div class="panel" data-testid="person-files" (click)="$event.stopPropagation()">
            <div class="panelhead">
              <h3>{{ selectedName() }} — {{ files().length }} file(s)</h3>
              <button class="closebtn" (click)="close()" title="close (Esc)">×</button>
            </div>
            <ul>
              @for (f of files(); track f.id) { <li>{{ f.filename }} <span class="dim">{{ f.path }}</span></li> }
            </ul>
          </div>
        </div>
      }

      @if (lightboxOpen()) {
        <app-zoomable-lightbox [src]="lightboxSrc()" [alt]="selectedName()"
                                (closed)="closeLightbox()" />
      }
    </div>
  `,
  styles: [`
    :host { display: block; overflow: auto; height: 100%; }
    .wrap { padding: 18px 22px; }
    .head { display: flex; align-items: center; gap: 12px; }
    h2 { margin: 0; } .dim { color: var(--fg-dim); font-size: 12px; }
    .head button { margin-left: auto; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 12px; margin-top: 14px; }
    .card { border: 1px solid var(--border); border-radius: 10px; padding: 12px;
            display: flex; flex-direction: column; gap: 8px; background: var(--bg-2); }
    .card.sel { border-color: var(--accent); }
    .avatar { text-align: center; cursor: pointer; background: var(--bg);
              border-radius: 8px; overflow: hidden; aspect-ratio: 1; display: flex;
              align-items: center; justify-content: center; }
    .avatar .placeholder { font-size: 40px; }
    .avatar img { width: 100%; height: 100%; object-fit: cover; }
    .name { font-weight: 600; text-align: center; }
    .meta { color: var(--fg-dim); font-size: 12px; text-align: center; }
    .actions { display: flex; gap: 6px; }
    .actions button, .actions select { flex: 1; font-size: 12px; }
    /* A person's files used to render as a plain block appended after the
       whole grid -- invisible without scrolling past however many of the
       (potentially 600+) cards came after the one just clicked, which read
       as "the button does nothing". A fixed overlay is visible immediately,
       regardless of where in the grid the click happened or how far scrolled. */
    .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 50;
               display: grid; place-items: center; padding: 30px; }
    .panel { background: var(--bg); border: 1px solid var(--border); border-radius: 12px;
             box-shadow: 0 12px 48px rgba(0,0,0,.4); width: min(640px, 100%);
             max-height: 80vh; display: flex; flex-direction: column; overflow: hidden; }
    .panelhead { display: flex; align-items: center; gap: 10px; padding: 14px 16px;
                 border-bottom: 1px solid var(--border); }
    .panelhead h3 { margin: 0; font-size: 14px; flex: 1; min-width: 0;
                     overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .closebtn { border: 0; background: none; font-size: 20px; line-height: 1; cursor: pointer;
                color: var(--fg-dim); padding: 2px 8px; border-radius: 6px; }
    .closebtn:hover { background: var(--bg-2); color: var(--fg); }
    .panel ul { margin: 0; padding: 10px 16px; overflow-y: auto; list-style: none; }
    .panel li { padding: 4px 0; font-size: 13px; border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent); }
    .panel .dim { margin-left: 8px; }
    /* Right after launch the daemon may still be starting up, so the very
       first fetch here can take a moment or transiently fail -- without this,
       the page just sat empty indistinguishably from "no faces detected". */
    .loadbar { position: relative; height: 4px; border-radius: 999px; background: var(--bg-2);
               overflow: hidden; margin-top: 14px; }
    .loadbar .stripe { position: absolute; inset: 0; opacity: .5;
      background: repeating-linear-gradient(45deg, var(--accent) 0 8px, transparent 8px 16px);
      animation: slide 1s linear infinite; }
    @keyframes slide { from { background-position: 0 0; } to { background-position: 22px 0; } }
    .errbanner { margin-top: 14px; padding: 10px 14px; border-radius: 8px; font-size: 12px;
                 background: color-mix(in srgb, #ef4444 15%, var(--bg-2));
                 border: 1px solid color-mix(in srgb, #ef4444 40%, var(--border));
                 display: flex; align-items: center; gap: 10px; }
    .errbanner button { font-size: 12px; }
  `],
})
export class FacesComponent {
  private api = getApi();
  readonly persons = signal<Person[]>([]);
  readonly selected = signal<number | null>(null);
  readonly files = signal<{ id: number; path: string; filename: string }[]>([]);
  // A real thumbnail of the cluster's representative file, keyed by person id
  // — the actual "face" a user recognizes, in place of a generic icon.
  readonly avatars = signal<Map<number, string>>(new Map());
  readonly lightboxOpen = signal(false);
  readonly lightboxSrc = signal<string | null>(null);
  readonly loading = signal(true);
  readonly loadError = signal('');

  constructor() { void this.refresh(); }

  async refresh() {
    this.loadError.set('');
    if (this.persons().length === 0) this.loading.set(true);
    try {
      const persons = await this.api.indexer.persons();
      this.persons.set(persons);
      if (this.selected() != null && !this.persons().some((p) => p.id === this.selected()))
        this.selected.set(null);
      this.loading.set(false);
      // Thumbnail reads (writes.js thumbDataUri) are synchronous disk I/O +
      // base64 encoding on the Electron main thread — with a large library
      // (hundreds of person clusters) firing them all via Promise.all queued
      // hundreds of blocking reads back to back and froze the whole app for
      // a moment, not just this page. A small concurrency cap lets other IPC
      // (search, progress polling) interleave between batches instead of
      // sitting behind one giant wall of reads.
      await this.loadAvatarsThrottled(persons);
    } catch (e) {
      this.loadError.set(e instanceof Error ? e.message : String(e));
      this.loading.set(false);
    }
  }

  private async loadAvatarsThrottled(persons: Person[], concurrency = 6) {
    const pending = persons.filter((p) => p.sample_id != null && !this.avatars().has(p.id));
    let idx = 0;
    const worker = async () => {
      while (idx < pending.length) {
        const p = pending[idx++];
        try {
          const url = await this.api.thumb(p.sample_id!);
          if (url) this.avatars.update((m) => new Map(m).set(p.id, url));
        } catch { /* one bad thumbnail shouldn't stop the rest */ }
      }
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, pending.length) }, worker));
  }

  others(p: Person) { return this.persons().filter((x) => x.id !== p.id); }
  selectedName() {
    const p = this.persons().find((x) => x.id === this.selected());
    return p?.name || `(unnamed #${p?.id})`;
  }

  async view(p: Person) {
    this.selected.set(p.id);
    this.files.set(await this.api.indexer.personFiles(p.id));
  }

  close() {
    this.selected.set(null);
    this.files.set([]);
  }

  async openLightbox(p: Person) {
    this.lightboxOpen.set(true);
    this.lightboxSrc.set(this.avatars().get(p.id) ?? null); // cached thumb immediately…
    if (p.sample_id == null) return;
    const full = await this.api.fullImage(p.sample_id);      // …then swap in full-res.
    if (this.lightboxOpen()) this.lightboxSrc.set(full);
  }

  closeLightbox() {
    this.lightboxOpen.set(false);
    this.lightboxSrc.set(null);
  }

  @HostListener('document:keydown.escape')
  onEscape() {
    if (this.lightboxOpen()) this.closeLightbox();
    else if (this.selected() != null) this.close();
  }

  async rename(p: Person, name: string) {
    name = name.trim();
    if (!name || name === (p.name || '')) return;
    await this.api.indexer.namePerson(p.id, name);
    await this.refresh();
  }

  async merge(src: Person, dstId: number) {
    if (!dstId || dstId === src.id) return;
    await this.api.indexer.mergePersons(src.id, dstId);
    await this.refresh();
  }
}
