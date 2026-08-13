import { Component, inject, signal } from '@angular/core';
import { LibraryService } from './library.service';
import { Category, UpdateCheckResult } from './api';

/** Full Settings page (moved out of the old narrow slide-out drawer, per
 * request — a real menu of its own instead of a cramped 320px panel):
 * models folder, engine tier, confidence filter, auto-tag sources, and tag
 * categories (spec §5.2/§12). */
@Component({
  selector: 'app-settings',
  standalone: true,
  template: `
    <div class="wrap">
      <h2>Settings</h2>

      <section class="card">
        <h3>Engine</h3>
        <label>Models folder
          <input [value]="modelsDir()" (change)="saveModelsDir($any($event.target).value)" data-testid="settings-models-dir" />
        </label>
        <label>Engine tier
          <select [value]="tier()" (change)="saveTier($any($event.target).value)" data-testid="settings-tier">
            <option value="">auto-detect</option>
            <option value="low">low (CPU / &lt;6GB)</option>
            <option value="low-mid">low-mid (6-8GB)</option>
            <option value="mid">mid (8-12GB)</option>
            <option value="high">high (16GB+)</option>
          </select>
        </label>
      </section>

      <section class="card">
        <h3>Confidence filter</h3>
        <div class="confopts">
          @for (opt of confOptions; track opt.value) {
            <button [class.on]="lib.minConfidence() === opt.value"
                    (click)="lib.setMinConfidence(opt.value)">{{ opt.label }}</button>
          }
        </div>
        <div class="hint">Hides low-confidence auto tags from search and the preview pane. Manual tags always show. New lower-confidence tags only appear on files re-indexed after this build (0.25 storage floor) — see "Re-index library" below.</div>
        <button (click)="reindexAll()" [disabled]="reindexingAll()" data-testid="reindex-all">
          {{ reindexingAll() ? 're-indexing…' : '↻ Re-index library with current models' }}
        </button>
        @if (reindexAllMessage()) { <div class="hint">{{ reindexAllMessage() }}</div> }
      </section>

      <section class="card">
        <h3>Auto-tag sources</h3>
        <label class="chk"><input type="checkbox" [checked]="tagFromPath()"
               (change)="saveSource('tag_from_path', $any($event.target).checked)" data-testid="opt-path" />
          Use folder names as tags</label>
        <label class="chk"><input type="checkbox" [checked]="tagFromMeta()"
               (change)="saveSource('tag_from_metadata', $any($event.target).checked)" data-testid="opt-meta" />
          Index embedded metadata (EXIF / SD params)</label>
        <div class="hint">Applies to files indexed from now on — run a Rescan to re-apply to existing files.</div>
      </section>

      <section class="card">
        <h3>Tag categories</h3>
        <div class="catlist" data-testid="cat-list">
          @for (c of categories(); track c.id) {
            <span class="catchip" [style.background]="c.color || 'var(--bg-2)'">
              {{ c.name }}@if (!c.is_builtin) { <span class="custom">custom</span> }
            </span>
          }
        </div>
        <div class="catadd">
          <input #cn placeholder="new category name" data-testid="cat-name" />
          <input #cc type="color" value="#8b5cf6" data-testid="cat-color" />
          <button (click)="addCategory(cn, cc)" data-testid="cat-add">Add</button>
        </div>
      </section>

      <section class="card">
        <h3>Updates</h3>
        <div class="hint">Version {{ appVersion() }}</div>
        <button (click)="checkForUpdates()" [disabled]="checkingUpdate()" data-testid="check-updates">
          {{ checkingUpdate() ? 'checking…' : 'Check for updates' }}
        </button>
        @if (updateStatus()) { <div class="hint" data-testid="update-status">{{ updateStatus() }}</div> }
        @if (updateInfo()?.updateAvailable) {
          <button (click)="openReleasePage()" data-testid="open-release">
            &#8595; Download v{{ updateInfo()!.latestVersion }}
          </button>
        }
        <div class="hint">Downloads open the official GitHub release page — this app never auto-installs anything for you, so a bad update can't silently break your install.</div>
      </section>
    </div>
  `,
  styles: [`
    :host { display: block; overflow: auto; height: 100%; }
    .wrap { padding: 18px 22px; max-width: 720px; }
    h2 { margin: 0 0 16px; }
    .card { border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px;
            background: var(--bg-2); margin-bottom: 16px; display: flex; flex-direction: column; gap: 10px; }
    .card h3 { margin: 0 0 2px; font-size: 14px; }
    label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--fg-dim); }
    label input, label select { font-size: 13px; padding: 7px 10px; }
    .confopts { display: flex; flex-wrap: wrap; gap: 6px; }
    .confopts button { font-size: 12px; padding: 6px 13px; border-radius: 7px; font-weight: 500;
                       background: var(--bg); color: var(--fg-dim); border: 1px solid var(--border); }
    .confopts button.on { background: var(--tag-learned); color: var(--fg); font-weight: 600; border-color: var(--accent); }
    .catlist { display: flex; flex-wrap: wrap; gap: 6px; }
    .catchip { font-size: 12px; padding: 3px 10px; border-radius: 999px; }
    .catchip .custom { font-size: 9px; opacity: .7; margin-left: 4px; text-transform: uppercase; }
    .catadd { display: flex; gap: 8px; }
    .catadd input:not([type=color]) { flex: 1; min-width: 0; font-size: 13px; padding: 7px 10px; }
    .catadd input[type=color] { width: 38px; padding: 0; }
    .chk { flex-direction: row !important; align-items: center; gap: 8px; color: var(--fg); font-size: 13px; }
    .chk input { width: auto; }
    .hint { font-size: 11px; color: var(--fg-dim); }
  `],
})
export class SettingsComponent {
  readonly lib = inject(LibraryService);
  readonly modelsDir = signal('');
  readonly tier = signal('');
  readonly categories = signal<Category[]>([]);
  readonly tagFromPath = signal(true);
  readonly tagFromMeta = signal(true);
  readonly reindexingAll = signal(false);
  readonly reindexAllMessage = signal('');
  readonly appVersion = signal('');
  readonly checkingUpdate = signal(false);
  readonly updateStatus = signal('');
  readonly updateInfo = signal<UpdateCheckResult | null>(null);

  // Same "All" default rationale as the search bar's confidence filter
  // (app.component.ts): 0.5 would silently hide previously-visible general
  // tags between 0.35-0.5 relative to the old per-facet floors.
  readonly confOptions: { value: number | null; label: string }[] = [
    { value: null, label: 'All' },
    { value: 0.5, label: '>0.5' },
    { value: 0.6, label: '>0.6' },
    { value: 0.7, label: '>0.7' },
    { value: 0.8, label: '>0.8' },
    { value: 0.9, label: '>0.9' },
  ];

  constructor() {
    void this.lib.getSetting('models_dir', '').then((v) => this.modelsDir.set(v ?? ''));
    void this.lib.getSetting('tier', '').then((v) => this.tier.set(v ?? ''));
    void this.lib.listCategories().then((c) => this.categories.set(c));
    void this.lib.getSetting('tag_from_path', '1').then((v) => this.tagFromPath.set(v !== '0'));
    void this.lib.getSetting('tag_from_metadata', '1').then((v) => this.tagFromMeta.set(v !== '0'));
    void this.lib.getAppVersion().then((v) => this.appVersion.set(v));
    // A cheap, cached read (updater.js keeps a 10-minute in-memory result, and
    // main.js already warms it ~5s after launch) — opening Settings usually
    // shows a result instantly instead of a spinner.
    void this.checkForUpdates(false);
  }

  saveModelsDir(v: string) { this.modelsDir.set(v); void this.lib.setSetting('models_dir', v); }
  saveTier(v: string) { this.tier.set(v); void this.lib.setSetting('tier', v); }

  saveSource(key: 'tag_from_path' | 'tag_from_metadata', on: boolean) {
    (key === 'tag_from_path' ? this.tagFromPath : this.tagFromMeta).set(on);
    void this.lib.setSetting(key, on ? '1' : '0');
  }

  async addCategory(nameEl: HTMLInputElement, colorEl: HTMLInputElement) {
    const name = nameEl.value.trim();
    if (!name) return;
    await this.lib.createCategory(name, colorEl.value);
    nameEl.value = '';
    this.categories.set(await this.lib.listCategories());
  }

  async reindexAll() {
    if (this.reindexingAll()) return;
    this.reindexingAll.set(true);
    this.reindexAllMessage.set('Queuing every indexed file for re-processing…');
    try {
      const r = await this.lib.reindexAll();
      this.reindexAllMessage.set(`Queued ${r.queued} file(s). Resume/auto-mode indexing will pick them up.`);
    } catch (e) {
      this.reindexAllMessage.set(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.reindexingAll.set(false);
    }
  }

  async checkForUpdates(force = true) {
    this.checkingUpdate.set(true);
    this.updateStatus.set(force ? 'Checking…' : '');
    try {
      const r = await this.lib.checkForUpdates(force);
      this.updateInfo.set(r);
      if (!r.ok) {
        this.updateStatus.set(force ? `Couldn't check for updates: ${r.error ?? 'unknown error'}` : '');
      } else if (r.updateAvailable) {
        this.updateStatus.set(`A new version (v${r.latestVersion}) is available.`);
      } else if (force) {
        this.updateStatus.set(`You're up to date (v${r.currentVersion}).`);
      } else {
        this.updateStatus.set('');
      }
    } catch (e) {
      if (force) this.updateStatus.set(`Couldn't check for updates: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.checkingUpdate.set(false);
    }
  }

  openReleasePage() {
    const url = this.updateInfo()?.url;
    if (url) void this.lib.openReleasePage(url);
  }
}
