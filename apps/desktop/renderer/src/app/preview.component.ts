import { Component, computed, effect, inject, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { LibraryService } from './library.service';
import { TagRow } from './api';
import { ZoomableLightboxComponent } from './zoomable-lightbox.component';

/** Preview pane (§8.2): larger render placeholder + all tags grouped by
 * category and colored by source, with inline add/remove. Faces/persons and
 * caption surface here too as the backend populates them. */
@Component({
  selector: 'app-preview',
  standalone: true,
  imports: [DecimalPipe, ZoomableLightboxComponent],
  template: `
    @let id = lib.selectedId();
    @if (id == null) {
      <div class="empty">Select a result to see its tags, caption, and faces.</div>
    } @else {
      <div class="head">
        <div class="fn">{{ file()?.filename }}</div>
        <div class="meta">{{ file()?.image_kind }} · {{ dims() }} · {{ size() }}
          <button class="reidx" (click)="reindex()" [disabled]="isReindexing()" title="re-run tagging / caption / OCR for this file" data-testid="reindex-file">{{ isReindexing() ? 're-indexing…' : '↻ re-index' }}</button>
          <button class="reidx" (click)="recaption()" [disabled]="isRecaptioning()" title="regenerate just the description — after changing tags or the caption model" data-testid="recaption-file">{{ isRecaptioning() ? 're-describing…' : '↻ re-Description' }}</button>
        </div>
      </div>
      @if (thumb()) {
        <img class="thumb" [src]="thumb()!" [alt]="file()?.filename" data-testid="thumb-img"
             title="click to enlarge" (click)="openLightbox()" />
      } @else {
        <div class="thumb ph">{{ file()?.filename }}<br /><span class="dim">thumbnail not generated yet</span></div>
      }

      @if (lightboxOpen()) {
        <app-zoomable-lightbox [src]="lightboxSrc()" [alt]="file()?.filename || ''"
                                (closed)="closeLightbox()" />
      }

      @let d = lib.detail();
      <div class="cat">description</div>
      <div class="box caption" [class.no-caption]="!d?.caption" data-testid="caption">
        @if (isRecaptioning()) {
          <span class="genwait">✨ generating…</span>
        } @else if (d?.caption) {
          {{ d!.caption }}
        } @else {
          <span class="ph">no description yet — click ↻ re-Description above to generate one</span>
        }
      </div>

      @if (d?.ocr_text) {
        <div class="cat">text in image (OCR)</div>
        <textarea class="box ocr" data-testid="ocr" rows="2"
                  placeholder="no text detected — you can type/correct it here"
                  [value]="d?.ocr_text || ''"
                  (change)="saveOcr($any($event.target).value)"></textarea>
      } @else {
        <button class="cat section-toggle" type="button" (click)="ocrOpen.update(v => !v)"
                [attr.aria-expanded]="ocrOpen()" data-testid="ocr-toggle">
          <span class="chevron">{{ ocrOpen() ? '&#9662;' : '&#9656;' }}</span> text in image (OCR)
        </button>
        @if (ocrOpen()) {
          <textarea class="box ocr" data-testid="ocr" rows="2"
                    placeholder="no text detected — you can type/correct it here"
                    [value]="d?.ocr_text || ''"
                    (change)="saveOcr($any($event.target).value)"></textarea>
        }
      }

      @if (d?.faces?.length) {
        <div class="cat">faces</div>
        <div class="faces" data-testid="faces">
          @for (fa of d!.faces; track fa.id) {
            <span class="face" [class.named]="fa.name" (click)="findPerson(fa)"
                  [title]="fa.name ? 'search this person' : 'unnamed — name in People tab'">
              🙂 {{ fa.name || '(unnamed #' + fa.person_id + ')' }}
            </span>
          }
        </div>
      }

      @if (d?.metadata?.length) {
        <button class="cat section-toggle" type="button" (click)="metadataOpen.update(v => !v)"
                [attr.aria-expanded]="metadataOpen()" data-testid="metadata-toggle">
          <span class="chevron">{{ metadataOpen() ? '&#9662;' : '&#9656;' }}</span> metadata
        </button>
        @if (metadataOpen()) {
          <div class="metas" data-testid="metadata">
            @for (m of d!.metadata; track m.key) {
              <div class="mrow"><span class="mk">{{ m.key }}</span><span class="mv">{{ m.value }}</span></div>
            }
          </div>
        }
      }

      @for (group of grouped(); track group.category) {
        @if (group.category === 'general') {
          <button class="cat section-toggle" type="button" (click)="generalOpen.update(v => !v)"
                  [attr.aria-expanded]="generalOpen()" data-testid="general-toggle">
            <span class="chevron">{{ generalOpen() ? '&#9662;' : '&#9656;' }}</span> {{ group.category }}
          </button>
        } @else {
          <div class="cat">{{ group.category }}</div>
        }
        @if (group.category !== 'general' || generalOpen()) {
        <div class="tags" [attr.data-testid]="group.category === 'general' ? 'general-tags' : null">
          @for (t of group.tags; track t.name) {
            <span class="tag" [class]="'src-' + t.source"
                  [class.just-confirmed]="t.confirmed && isJustConfirmed(t)"
                  [title]="t.source + (t.confidence != null ? ' · ' + (t.confidence | number:'1.2-2') : '')">
              @if (isEditing(t)) {
                <input class="rn" [value]="t.name" data-testid="rename-input"
                       (keydown.enter)="doRename(t, $any($event.target).value)"
                       (keydown.escape)="editing.set(null)" (blur)="editing.set(null)" />
              } @else {
                {{ t.name }}
                @if (t.source === 'learned' && t.confirmed) {
                  <span class="confirmed" [class.pop]="isJustConfirmed(t)" data-testid="confirmed-badge">✓ confirmed</span>
                  <button class="x" (click)="reject(t)" title="reject — not this after all" data-testid="learn-reject">✗</button>
                } @else if (t.source === 'learned') {
                  <span class="sug">suggested</span>
                  <button class="ok" (click)="confirm(t)" title="confirm — this is correct" data-testid="learn-confirm">✓</button>
                  <button class="x" (click)="reject(t)" title="reject — not this" data-testid="learn-reject">✗</button>
                } @else if (t.source === 'wd14' || t.source === 'clip') {
                  @if (t.confirmed) {
                    <span class="confirmed" [class.pop]="isJustConfirmed(t)" data-testid="confirmed-badge">✓ confirmed</span>
                  } @else {
                    <button class="ok" (click)="confirmAuto(t)" title="confirm — this is correct, and helps the model learn it" data-testid="confirm-auto">✓</button>
                  }
                  <button class="ren" (mousedown)="startRename(t, $event)" title="rename / merge this tag everywhere" data-testid="rename">✎</button>
                  <button class="x" (click)="rejectAuto(t)" title="remove — and stop suggesting this tag for this image" data-testid="reject-auto">×</button>
                } @else {
                  <button class="ren" (mousedown)="startRename(t, $event)" title="rename / merge this tag everywhere" data-testid="rename">✎</button>
                  @if (t.source === 'manual' || t.source === 'path') {
                    <button class="teach" (click)="teach(t)" title="teach the app this tag from your examples" data-testid="teach">🎓</button>
                  }
                  <button class="x" (click)="remove(t)" title="remove">×</button>
                }
              }
            </span>
          }
        </div>
        }
      }

      @if (learnMsg()) { <div class="learnmsg" data-testid="learn-msg">{{ learnMsg() }}</div> }

      <div class="cat">tag &amp; teach a character or person</div>
      <div class="teach-panel" data-testid="teach-panel">
        <div class="teach-row">
          <select #teachCat data-testid="teach-cat">
            @for (c of teachCategories(); track c.name) {
              <option [value]="c.name" [selected]="c.name === 'character'">{{ c.name }}</option>
            }
          </select>
          <input #teachName class="teach-name" list="teach-names" placeholder="name (e.g. a new VTuber)"
                 (change)="refreshTeachStatus(teachCat.value, teachName.value)" data-testid="teach-name" />
          <datalist id="teach-names">
            @for (n of teachNames(teachCat.value); track n) { <option [value]="n"></option> }
          </datalist>
        </div>
        <label class="teach-space" data-testid="teach-space-face"
               title="Uses facial features (InsightFace) instead of overall image similarity — keeps recognizing the same real person across different photos, angles, and outfits.">
          <input type="checkbox" [checked]="teachSpace() === 'face'"
                 (change)="teachSpace.set($any($event.target).checked ? 'face' : 'clip')"
                 data-testid="teach-space-face-checkbox" />
          recognize by face (real person)
        </label>
        @if (teachStatus()) { <div class="teach-status">{{ teachStatus() }}</div> }
        <button class="teach-btn" (click)="tagAndTrain(teachCat.value, teachName.value)"
                [disabled]="teaching()" data-testid="teach-submit">
          {{ teaching() ? 'tagging & training…' : 'Tag this image & train' }}
        </button>
      </div>

      <input class="add" placeholder="add tag as  category:name  (Enter)"
             (keydown.enter)="add($any($event.target))" />
    }
  `,
  styles: [`
    :host { display: block; padding: 14px; overflow-y: auto; overflow-x: hidden;
            height: 100%; max-height: 100%; min-height: 0; box-sizing: border-box;
            scrollbar-gutter: stable; }
    .empty { color: var(--fg-dim); font-size: 13px; }
    .head .fn { font-weight: 600; word-break: break-all; }
    .head .meta { color: var(--fg-dim); font-size: 12px; margin-top: 2px; }
    .head .meta .reidx { margin-left: 8px; font-size: 11px; padding: 3px 9px;
                         border-radius: 999px; background: var(--bg-2); border: 1px solid var(--border);
                         color: var(--fg-dim); cursor: pointer; transition: background .12s, color .12s, border-color .12s; }
    .head .meta .reidx:hover:not(:disabled) { background: var(--sel); color: var(--fg); border-color: var(--accent); }
    .head .meta .reidx:disabled { opacity: .6; cursor: default; }
    .thumb { margin: 12px 0; width: 100%; max-height: 240px; object-fit: contain;
             background: var(--bg-2); border: 1px solid var(--border); border-radius: 16px;
             box-shadow: var(--shadow); }
    .thumb:not(.ph) { cursor: zoom-in; }
    .thumb.ph { height: 150px; display: grid; place-items: center; border-style: dashed;
                color: var(--fg-dim); font-size: 12px; text-align: center; padding: 8px; }
    .box { width: 100%; box-sizing: border-box; border-radius: 14px; padding: 11px 13px;
           background: linear-gradient(145deg, var(--surface), var(--bg-2)); border: 1px solid var(--border); font-family: inherit;
           box-shadow: var(--shadow-control);
           transition: border-color .12s, box-shadow .12s; }
    .box:focus-within, textarea.box:hover { border-color: var(--accent); }
    .box:focus-within { box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent); }
    .caption { font-size: 13px; line-height: 1.5; min-height: 1.5em; }
    .caption .genwait { color: var(--fg-dim); font-style: italic; }
    .caption .ph, .caption.no-caption { color: var(--fg-dim); font-size: 12px; font-style: italic; }
    textarea.ocr { width: 100%; font-size: 12px; line-height: 1.5; resize: vertical; color: var(--fg); }
    textarea.ocr::placeholder { color: var(--fg-dim); }
    textarea.ocr:focus { outline: none; }
    .faces { display: flex; flex-wrap: wrap; gap: 6px; }
    .face { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: var(--bg-2);
            border: 1px solid var(--border); cursor: pointer; }
    .face.named { background: var(--tag-manual); }
    .metas { font-size: 11px; display: flex; flex-direction: column; gap: 3px; }
    .mrow { display: flex; gap: 8px; }
    .mk { flex: 0 0 130px; color: var(--fg-dim); word-break: break-all; }
    .mv { flex: 1; white-space: pre-wrap; word-break: break-word; }
    .cat { font-size: 11px; text-transform: uppercase; color: var(--fg-dim);
           margin: 10px 0 4px; letter-spacing: .04em; }
    .section-toggle { width: 100%; padding: 2px 0; border: 0; background: none;
                      display: flex; align-items: center; gap: 4px; text-align: left;
                      cursor: pointer; font-family: inherit; }
    .section-toggle:hover { color: var(--fg); }
    .section-toggle .chevron { width: 12px; text-align: center; font-size: 12px; }
    .tags { display: flex; flex-wrap: wrap; gap: 5px; }
    .tag { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px;
           border-radius: 999px; background: var(--tag); font-size: 12px; }
    .tag.src-manual { background: var(--tag-manual); }
    .tag.src-path { background: var(--tag-path); }
    .tag.src-learned { background: var(--tag-learned); }
    .tag.src-wd14 { background: var(--tag-wd14); }
    .tag.src-clip { background: var(--tag-clip); }
    .tag .x, .tag .ok, .tag .teach, .tag .ren { border: 0; background: none; padding: 0 2px; opacity: .6; cursor: pointer; }
    .tag .ren:hover { opacity: 1; }
    .tag .rn { font-size: 12px; width: 90px; padding: 0 4px; }
    .tag .ok { color: #1a7f37; opacity: .9; } .tag .ok:hover { opacity: 1; }
    .tag .teach:hover, .tag .x:hover { opacity: 1; }
    .tag .sug { font-size: 9px; text-transform: uppercase; letter-spacing: .04em;
                opacity: .6; margin-left: 2px; }
    .tag.just-confirmed { outline: 1.5px solid #1a7f37; outline-offset: 0;
                          background: color-mix(in srgb, #1a7f37 22%, var(--tag)); }
    .tag .confirmed { display: inline-flex; align-items: center; gap: 2px; margin-left: 2px;
                       padding: 1px 7px; border-radius: 999px; background: #1a7f37; color: #fff;
                       font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; }
    .tag .confirmed.pop { animation: confirmPop .18s ease-out; }
    @keyframes confirmPop { from { opacity: 0; transform: scale(.75); } to { opacity: 1; transform: scale(1); } }
    .learnmsg { margin-top: 10px; padding: 8px 10px; border-radius: 10px;
                background: var(--tag-learned); font-size: 12px; }
    .teach-panel { border: 1px solid var(--border); border-radius: 16px; padding: 12px;
                   background: linear-gradient(145deg, var(--surface), var(--bg-2));
                   box-shadow: var(--shadow-control); }
    .teach-row { display: flex; gap: 6px; }
    .teach-row select, .teach-row .teach-name {
      border-radius: 7px; border: 1px solid var(--border); background: var(--bg);
      transition: border-color .12s;
    }
    .teach-row select:focus, .teach-row .teach-name:focus { outline: none; border-color: var(--accent); }
    .teach-row select { flex: 0 0 auto; font-size: 12px; }
    .teach-row .teach-name { flex: 1; min-width: 0; font-size: 12px; }
    .teach-space { display: flex; align-items: center; gap: 6px; margin-top: 8px;
                   font-size: 11px; color: var(--fg-dim); cursor: pointer; user-select: none; }
    .teach-space input { cursor: pointer; }
    .teach-status { font-size: 11px; color: var(--fg-dim); margin-top: 6px; }
    .teach-btn { width: 100%; margin-top: 8px; font-size: 12px; padding: 7px; border-radius: 8px;
                border: 1px solid var(--border); background: var(--accent); color: #fff; cursor: pointer;
                transition: opacity .12s; }
    .teach-btn:hover:not(:disabled) { opacity: .9; }
    .teach-btn:disabled { opacity: .5; cursor: default; }
    .add { width: 100%; margin-top: 12px; border-radius: 8px; border: 1px solid var(--border);
           background: var(--bg-2); padding: 7px 10px; box-sizing: border-box; transition: border-color .12s; }
    .add:focus { outline: none; border-color: var(--accent); }
  `],
})
export class PreviewComponent {
  readonly lib = inject(LibraryService);
  readonly learnMsg = signal('');
  // Keyed by file id, not a bare boolean: the preview pane is a single shared
  // component instance reused across every selected file, so a flag with no
  // id attached stays stuck "on" for whichever file happens to be selected
  // next while a previous file's request is still in flight.
  readonly reindexingId = signal<number | null>(null);
  readonly recaptioningId = signal<number | null>(null);
  readonly isReindexing = computed(() => this.reindexingId() === this.lib.selectedId());
  readonly isRecaptioning = computed(() => this.recaptioningId() === this.lib.selectedId());
  readonly editing = signal<string | null>(null);
  readonly thumb = this.lib.thumbUri;
  readonly lightboxOpen = signal(false);
  readonly lightboxSrc = signal<string | null>(null);
  readonly metadataOpen = signal(false);
  readonly ocrOpen = signal(false);
  readonly generalOpen = signal(true);
  // Transient inline feedback (§5.3 UX) for the tag just confirmed, keyed as
  // "category:name" — shown right on the chip itself since after confirm()
  // reloads tags, a promoted tag can leave the 'learned' branch entirely and
  // the far-away .learnmsg banner is easy to miss next to a wall of chips.
  readonly justConfirmed = signal<string | null>(null);
  private justConfirmedTimer?: ReturnType<typeof setTimeout>;

  // "Tag & teach" panel (§5.3 discoverability): a dedicated, always-visible way
  // to tag a character/person and immediately train the few-shot learner, instead
  // of requiring "add a plain tag, then find the tiny 🎓 icon on it".
  readonly teachCategories = signal<{ name: string }[]>([]);
  private allTagNames = signal<{ name: string; category: string }[]>([]);
  readonly teaching = signal(false);
  readonly teachStatus = signal('');
  // Which embedding space to train in (§5.3): 'clip' generalizes on overall
  // image similarity (character/concept/outfit/pose/object/scene); 'face'
  // generalizes on InsightFace embeddings instead, so it keeps recognizing the
  // same real person across very different photos, angles, and outfits where
  // CLIP similarity would drift. Defaults to 'clip' to preserve prior behavior.
  readonly teachSpace = signal<'clip' | 'face'>('clip');
  private selectionContextId: number | null = null;

  constructor() {
    // Feedback belongs to the image on which the action was performed.  Clear
    // it immediately when the focused row changes so a confirmation made on
    // Ashe can never appear to describe the subsequently selected Kiriko row.
    effect(() => {
      const id = this.lib.selectedId();
      if (id === this.selectionContextId) return;
      this.selectionContextId = id;
      this.learnMsg.set('');
      this.teachStatus.set('');
      this.editing.set(null);
      this.ocrOpen.set(false);
      this.closeLightbox();
      if (this.justConfirmedTimer) clearTimeout(this.justConfirmedTimer);
      this.justConfirmed.set(null);
    });
    void this.lib.listCategories().then((c) => this.teachCategories.set(c));
    void this.lib.listTags().then((r) => this.allTagNames.set(r.tags));
  }

  teachNames(category: string): string[] {
    return this.allTagNames().filter((t) => t.category === category).map((t) => t.name);
  }

  async refreshTeachStatus(category: string, name: string) {
    name = name.trim();
    if (!category || !name) { this.teachStatus.set(''); return; }
    const s = await this.lib.learnStatus(category, name);
    this.teachStatus.set(s.count > 0
      ? `${s.count} example(s) tagged so far — trains automatically at 5.`
      : `No examples yet for “${name}”.`);
  }

  async tagAndTrain(category: string, name: string) {
    const id = this.lib.selectedId();
    category = category.trim(); name = name.trim();
    if (id == null || !category || !name) return;
    const space = this.teachSpace();
    this.teaching.set(true);
    this.teachStatus.set(space === 'face'
      ? `Tagging ${category}:${name}, preparing face examples, and training…`
      : `Tagging ${category}:${name}, preparing CLIP examples, and training…`);
    try {
      await this.lib.addTag(category, name);
      const r = await this.lib.learn(category, name, space);
      this.teachStatus.set(r.ok
        ? `Trained “${name}” (${r.method}, ${r.n_pos} positive / ${r.n_neg ?? 0} negative example(s)) — applied to ${r.applied} image(s) now; ${r.queued ?? 0} remaining image(s) queued for matching.`
        : `Tagged, but training is not ready: ${r.error ?? 'unknown error'} (${r.usable ?? 0} usable embedding(s), ${r.count ?? 0} manual example(s)).`);
      void this.lib.reloadTags();
      void this.lib.listTags().then((res) => this.allTagNames.set(res.tags));
    } catch (e) {
      this.teachStatus.set(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.teaching.set(false);
    }
  }

  async openLightbox() {
    this.lightboxOpen.set(true);
    this.lightboxSrc.set(this.thumb()); // show the cached thumb immediately…
    const id = this.lib.selectedId();
    if (id == null) return;
    const full = await this.lib.fullImage(id);           // …then swap in full-res.
    if (this.lightboxOpen() && this.lib.selectedId() === id && full) this.lightboxSrc.set(full);
  }

  closeLightbox() {
    this.lightboxOpen.set(false);
    this.lightboxSrc.set(null);
  }

  isEditing(t: TagRow) { return this.editing() === t.category + ' ' + t.name; }
  startRename(t: TagRow, ev: Event) { ev.preventDefault(); this.editing.set(t.category + ' ' + t.name); }

  async doRename(t: TagRow, newName: string) {
    newName = newName.trim();
    this.editing.set(null);
    if (!newName || newName === t.name) return;
    const r = await this.lib.renameTag(t.category, t.name, newName);
    this.learnMsg.set(r.ok
      ? (r.merged
          ? `Merged “${t.name}” → “${newName}” across ${r.files} file(s).`
          : `Renamed “${t.name}” → “${newName}” across ${r.files} file(s).`)
      : `Rename failed: ${r.error}`);
    await this.lib.reloadTags();
    void this.lib.runSearch();
  }

  readonly file = computed(() =>
    this.lib.results().find((f) => f.id === this.lib.selectedId()) ?? null);

  readonly grouped = computed(() => {
    const by: Record<string, TagRow[]> = {};
    for (const t of this.lib.tags()) (by[t.category] ??= []).push(t);
    return Object.keys(by).sort().map((category) => ({ category, tags: by[category] }));
  });

  dims = computed(() => {
    const f = this.file();
    return f?.width && f?.height ? `${f.width}×${f.height}` : '—';
  });
  size = computed(() => {
    const b = this.file()?.size_bytes ?? 0;
    return b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round(b / 1e3)} KB`;
  });

  remove(t: TagRow) { void this.lib.removeTag(t.category, t.name); }

  async rejectAuto(t: TagRow) {
    const id = this.lib.selectedId(); if (id == null) return;
    const filename = this.file()?.filename ?? `file #${id}`;
    const r = await this.lib.rejectAutoTag(t.category, t.name, t.source);
    if (this.lib.selectedId() !== id) return;
    this.learnMsg.set(r.ok
      ? `Removed “${t.name}” for “${filename}” — won't come back on rescan/reindex, and similar images are less likely to get it too.`
      : `Couldn't remove “${t.name}”: ${r.error ?? 'unknown error'}`);
  }

  async confirmAuto(t: TagRow) {
    const id = this.lib.selectedId(); if (id == null) return;
    const filename = this.file()?.filename ?? `file #${id}`;
    const key = t.category + ':' + t.name;
    const r = await this.lib.confirmAutoTag(t.category, t.name);
    await this.lib.reloadTags();
    if (this.lib.selectedId() !== id) return;
    // Honest about *when* this actually starts reinforcing recognition: a
    // single confirm just records one example. The learner only kicks in
    // once enough accumulate — say so explicitly instead of implying an
    // immediate effect (see the Learned tags page for the live count).
    let msg: string;
    if (!r.ok) {
      msg = `Couldn't confirm “${t.name}”: ${r.error ?? 'unknown error'}`;
    } else if (!r.reinforces) {
      msg = `Confirmed “${t.name}” for “${filename}” — marked as correct for good.`;
    } else if (r.trained) {
      msg = `Confirmed “${t.name}” for “${filename}” — trained from ${r.n_pos} example(s), now suggested on ${r.applied} similar file(s). See the Learned tags page.`;
    } else {
      msg = `Confirmed “${t.name}” for “${filename}” — ${r.n_pos}/${r.needed} confirmations so far. Once it reaches ${r.needed}, it'll start auto-suggesting this tag on similar images (see the Learned tags page).`;
    }
    this.learnMsg.set(msg);
    if (r.ok) {
      if (this.justConfirmedTimer) clearTimeout(this.justConfirmedTimer);
      this.justConfirmed.set(key);
      this.justConfirmedTimer = setTimeout(() => this.justConfirmed.set(null), 2500);
    }
  }

  saveOcr(text: string) { void this.lib.saveOcr(text); }

  async reindex() {
    const id = this.lib.selectedId();
    if (id == null || this.reindexingId() != null) return;
    this.reindexingId.set(id);
    this.learnMsg.set('Re-indexing this file...');
    try {
      const r = await this.lib.reindexSelected();
      if (this.lib.selectedId() !== id) return;
      if (r.removed) this.learnMsg.set('The file no longer exists and was removed from the library.');
      else if (!r.ok) this.learnMsg.set(`Re-index failed: ${r.error ?? 'unknown error'}`);
      else if (r.completed) this.learnMsg.set('Re-index complete. Tags, caption and OCR are refreshed.');
      else this.learnMsg.set('Re-index queued. Resume indexing to process it.');
    } catch (e) {
      if (this.lib.selectedId() === id) this.learnMsg.set(`Re-index failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      if (this.reindexingId() === id) this.reindexingId.set(null);
    }
  }

  async recaption() {
    const id = this.lib.selectedId();
    if (id == null || this.recaptioningId() != null) return;
    this.recaptioningId.set(id);
    this.learnMsg.set('Generating a new description...');
    try {
      const r = await this.lib.recaptionSelected();
      if (this.lib.selectedId() !== id) return;
      if (!r.ok) this.learnMsg.set(`Couldn't regenerate the description: ${r.error ?? 'unknown error'}`);
      else if (r.completed) this.learnMsg.set('Description regenerated.');
      else this.learnMsg.set('Description queued. Resume indexing to process it.');
    } catch (e) {
      if (this.lib.selectedId() === id) this.learnMsg.set(`Couldn't regenerate the description: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      if (this.recaptioningId() === id) this.recaptioningId.set(null);
    }
  }

  findPerson(fa: { name: string | null }) {
    if (fa.name) this.lib.setQuery(`person:"${fa.name}"`);
  }

  async teach(t: TagRow) {
    this.learnMsg.set(`Preparing CLIP examples and teaching “${t.name}”…`);
    const r = await this.lib.learn(t.category, t.name);
    this.learnMsg.set(r.ok
      ? `Learned “${t.name}” from ${r.n_pos} example(s) — applied to ${r.applied} similar image(s), with ${r.queued ?? 0} image(s) queued for matching. It’ll keep improving as you confirm/reject.`
      : `Can’t teach yet: ${r.error} (${r.usable ?? 0} usable embedding(s), ${r.count ?? 0} manual example(s)).`);
    const id = this.lib.selectedId();
    if (id != null) await this.lib.reloadTags();
  }

  isJustConfirmed(t: TagRow): boolean {
    return this.justConfirmed() === t.category + ':' + t.name;
  }

  async confirm(t: TagRow) {
    const id = this.lib.selectedId(); if (id == null) return;
    const filename = this.file()?.filename ?? `file #${id}`;
    const key = t.category + ':' + t.name;
    await this.lib.learnConfirm(t.category, t.name, id);
    await this.lib.reloadTags();
    if (this.lib.selectedId() !== id) return;
    this.learnMsg.set(`Confirmed “${t.name}” for “${filename}” — added as a positive example.`);
    if (this.justConfirmedTimer) clearTimeout(this.justConfirmedTimer);
    this.justConfirmed.set(key);
    this.justConfirmedTimer = setTimeout(() => this.justConfirmed.set(null), 2500);
  }

  async reject(t: TagRow) {
    const id = this.lib.selectedId(); if (id == null) return;
    const filename = this.file()?.filename ?? `file #${id}`;
    await this.lib.learnReject(t.category, t.name, id);
    await this.lib.reloadTags();
    if (this.lib.selectedId() !== id) return;
    this.learnMsg.set(`Rejected “${t.name}” for “${filename}” — the app will stop suggesting it there.`);
  }

  add(input: HTMLInputElement) {
    const [cat, ...rest] = input.value.split(':');
    const name = rest.join(':').trim();
    if (cat?.trim() && name) {
      void this.lib.addTag(cat.trim(), name);
      input.value = '';
    }
  }
}
