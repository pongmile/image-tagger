import { Component, computed, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { LearnedTagRow, TagProgressRow, getApi } from './api';

/** Learned-tags transparency view (spec §5.3): a read-only dashboard listing
 * every few-shot learned tag and its live training state — makes the
 * self-training loop (tag 5-10 examples → auto-apply as suggestions → confirm/
 * reject to refine) visible instead of an implicit backend detail. Confirm/
 * reject itself still happens per-file in the preview pane (§9); this view
 * also surfaces tags that are still accumulating examples (below the
 * training floor) and a per-tag Refresh to retrain+reapply on demand. */
@Component({
  selector: 'app-learned',
  standalone: true,
  imports: [DecimalPipe],
  template: `
    <div class="wrap">
      <div class="head">
        <h2>Learned tags</h2>
        <span class="dim">few-shot self-training (§5.3) — tag 5-10 examples of anything the model doesn't know, and it starts auto-suggesting the rest. Confirming a suggestion elsewhere in the app counts as an example too.</span>
        <button (click)="refresh()">Refresh list</button>
      </div>

      @if (resetMsg()) {
        <div class="resetbanner" data-testid="reset-msg">
          {{ resetMsg() }}
          <button (click)="resetMsg.set('')">Dismiss</button>
        </div>
      }

      @if (loading()) {
        <div class="loadbar" data-testid="loading"><div class="stripe"></div></div>
        <p class="dim">Loading learned tags…</p>
      } @else if (loadError()) {
        <div class="errbanner" data-testid="load-error">
          Couldn't load learned tags: {{ loadError() }}
          <button (click)="refresh()">Retry</button>
        </div>
      } @else if (tags().length === 0 && progress().length === 0) {
        <p class="dim">Nothing learned yet. Use the "tag &amp; teach" panel in the preview pane, the 🎓 icon on a manual tag, or the ✓ confirm button on an auto tag to start teaching a character, person, or concept.</p>
      }

      @if (progress().length > 0) {
        <h3 class="sub">In progress <span class="dim">— not enough examples yet to start suggesting</span></h3>
        <div class="grid">
          @for (p of progress(); track p.tag_id) {
            <div class="card inprogress" data-testid="progress-card">
              <div class="name">{{ p.name }} <span class="cat">{{ p.category }}</span></div>
              <div class="bar"><div class="fill" [style.width.%]="(p.n_pos / minPositives()) * 100"></div></div>
              <div class="row">{{ p.n_pos }} / {{ minPositives() }} positive example(s) needed
                @if (p.n_neg > 0) { <span class="dim">· {{ p.n_neg }} negative</span> }</div>
              <div class="cardbtns">
                @if (confirmingId() === p.tag_id) {
                  <button class="danger" (click)="forgetTag(p)" [disabled]="busy()"
                          data-testid="confirm-delete-tag">
                    {{ forgettingId() === p.tag_id ? 'resetting…' : 'Yes, reset' }}
                  </button>
                  <button (click)="confirmingId.set(null)" [disabled]="busy()"
                          data-testid="cancel-delete-tag">Cancel</button>
                } @else {
                  <button class="danger" (click)="confirmingId.set(p.tag_id)" [disabled]="busy()"
                          title="discard the examples taught so far for this tag"
                          data-testid="delete-tag">Delete</button>
                }
              </div>
              @if (confirmingId() === p.tag_id) {
                <div class="warn">Discards all {{ p.n_pos + p.n_neg }} example(s) taught so far.
                  Your own manual tags keep this tag as-is.</div>
              }
              @if (cardMsg().tagId === p.tag_id) { <div class="hint">{{ cardMsg().text }}</div> }
            </div>
          }
        </div>
      }

      @if (tags().length > 0) {
        @if (progress().length > 0) { <h3 class="sub">Active</h3> }
        <div class="grid">
          @for (t of tags(); track t.tag_id) {
            <div class="card" data-testid="learned-card">
              <div class="name">{{ t.name }} <span class="cat">{{ t.category }}</span></div>
              <div class="method" [class.linear]="t.method === 'linear'">{{ t.method }}</div>
              <div class="row">{{ t.n_pos }} positive / {{ t.n_neg }} negative example(s)</div>
              <div class="row">threshold {{ t.threshold | number:'1.2-2' }} · applied to {{ t.applied }} file(s)</div>
              <div class="cardbtns">
                <button (click)="refreshTag(t)" [disabled]="busy()" data-testid="refresh-tag">
                  {{ refreshingId() === t.tag_id ? 'refreshing…' : '↻ Refresh' }}
                </button>
                @if (confirmingId() === t.tag_id) {
                  <button class="danger" (click)="forgetTag(t)" [disabled]="busy()"
                          data-testid="confirm-delete-tag">
                    {{ forgettingId() === t.tag_id ? 'resetting…' : 'Yes, reset' }}
                  </button>
                  <button (click)="confirmingId.set(null)" [disabled]="busy()"
                          data-testid="cancel-delete-tag">Cancel</button>
                } @else {
                  <button class="danger" (click)="confirmingId.set(t.tag_id)" [disabled]="busy()"
                          title="undo this tag's auto-tagging and forget what it learned"
                          data-testid="delete-tag">Delete</button>
                }
              </div>
              @if (confirmingId() === t.tag_id) {
                <div class="warn">Removes this tag from the {{ t.applied }} file(s) it was
                  auto-applied to and forgets all {{ t.n_pos + t.n_neg }} example(s). Files you
                  tagged by hand keep it, and nothing gets re-suggested until you teach it again.</div>
              }
              @if (cardMsg().tagId === t.tag_id) { <div class="hint">{{ cardMsg().text }}</div> }
            </div>
          }
        </div>
      }
    </div>
  `,
  styles: [`
    :host { display: block; overflow: auto; height: 100%; }
    .wrap { padding: 18px 22px; }
    .head { display: flex; align-items: center; gap: 12px; }
    h2 { margin: 0; } .dim { color: var(--fg-dim); font-size: 12px; }
    .head button { margin-left: auto; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 12px; margin-top: 14px; }
    .card { border: 1px solid var(--border); border-radius: 10px; padding: 12px;
            display: flex; flex-direction: column; gap: 6px; background: var(--bg-2); }
    .name { font-weight: 600; }
    .name .cat { font-weight: 400; font-size: 11px; color: var(--fg-dim); margin-left: 6px; }
    .method { align-self: flex-start; font-size: 10px; text-transform: uppercase; letter-spacing: .04em;
              padding: 1px 8px; border-radius: 999px; background: var(--tag-learned); }
    .method.linear { background: var(--tag-clip); }
    .row { font-size: 12px; color: var(--fg-dim); }
    .sub { margin: 18px 0 0; font-size: 13px; font-weight: 600; }
    .sub .dim { font-weight: 400; margin-left: 4px; }
    .card.inprogress { border-style: dashed; }
    .bar { height: 6px; border-radius: 999px; background: var(--bg); overflow: hidden; }
    .bar .fill { height: 100%; background: var(--tag-learned); transition: width .3s; }
    .cardbtns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 2px; }
    .cardbtns button { font-size: 12px; padding: 5px 11px; }
    .cardbtns .danger { margin-left: auto; color: #fca5a5;
                        border-color: color-mix(in srgb, #ef4444 40%, var(--border)); }
    .cardbtns .danger:hover:not(:disabled) { background: color-mix(in srgb, #ef4444 22%, var(--bg-2)); }
    /* Once the confirm step is showing, "Yes, reset" sits next to Cancel
       rather than pushed to the far edge away from it. */
    .cardbtns [data-testid=confirm-delete-tag] { margin-left: auto; }
    .cardbtns [data-testid=cancel-delete-tag] { margin-left: 0; }
    .hint { font-size: 11px; color: var(--fg-dim); }
    .warn { font-size: 11px; line-height: 1.45;
            color: color-mix(in srgb, #ef4444 55%, var(--fg)); }
    .resetbanner { margin-top: 14px; padding: 10px 14px; border-radius: 8px; font-size: 12px;
                   background: color-mix(in srgb, var(--accent) 12%, var(--bg-2));
                   border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
                   display: flex; align-items: center; gap: 10px; }
    .resetbanner button { margin-left: auto; font-size: 12px; }
    /* Right after launch the daemon may still be starting up, so the very
       first fetch here can take a moment or transiently fail -- without this,
       the page just sat empty indistinguishably from "you have no learned
       tags yet", which reads as broken/no data rather than "still loading". */
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
export class LearnedComponent {
  private api = getApi();
  readonly tags = signal<LearnedTagRow[]>([]);
  readonly progress = signal<TagProgressRow[]>([]);
  readonly minPositives = signal(5);
  readonly loading = signal(true);
  readonly loadError = signal('');
  readonly refreshingId = signal<number | null>(null);
  readonly forgettingId = signal<number | null>(null);
  // Which card is showing its "Yes, reset / Cancel" step. Delete is
  // irreversible and silently changes tags across the whole library, so it
  // never fires on a single click.
  readonly confirmingId = signal<number | null>(null);
  readonly cardMsg = signal<{ tagId: number | null; text: string }>({ tagId: null, text: '' });
  // Result of the last reset. Lives on the page, not the card: a successful
  // reset removes the card it was triggered from.
  readonly resetMsg = signal('');

  /** One card's action at a time — both operations retrain/rewrite the same
   *  file_tags rows, and the list is reloaded afterwards either way. */
  readonly busy = computed(() => this.refreshingId() != null || this.forgettingId() != null);

  constructor() { void this.refresh(); }

  async refresh() {
    this.loadError.set('');
    if (this.tags().length === 0) this.loading.set(true);
    try {
      const r = await this.api.indexer.listLearnedTags();
      this.tags.set(r.tags);
      this.progress.set(r.in_progress);
      this.minPositives.set(r.min_positives);
    } catch (e) {
      this.loadError.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loading.set(false);
    }
  }

  // Re-runs train()+apply() on demand (§5.3) — useful after tagging a batch
  // of new examples elsewhere without wanting to wait for the next confirm/
  // reject to trigger a retrain, or just to re-score the library against
  // recently-added files.
  async refreshTag(t: LearnedTagRow) {
    if (this.busy()) return;
    this.refreshingId.set(t.tag_id);
    this.cardMsg.set({ tagId: null, text: '' });
    try {
      const r = await this.api.indexer.learn(t.category, t.name, t.space);
      this.cardMsg.set({
        tagId: t.tag_id,
        text: r.ok
          ? `Retrained from ${r.n_pos ?? t.n_pos} example(s) — now applied to ${r.applied ?? 0} file(s).`
          : `Refresh failed: ${r.error ?? 'unknown error'}`,
      });
      if (r.ok) await this.refresh();
    } finally {
      this.refreshingId.set(null);
    }
  }

  // Reset a tag that has learned the wrong thing (§5.3): every auto-applied
  // instance is withdrawn and the training state is dropped, so the tag falls
  // back to plain manual/base-model tagging. Retraining can't do this — it only
  // ever adds to the same examples that produced the bad behaviour.
  async forgetTag(t: { tag_id: number; category: string; name: string }) {
    if (this.busy()) return;
    this.forgettingId.set(t.tag_id);
    this.cardMsg.set({ tagId: null, text: '' });
    try {
      const r = await this.api.indexer.learnForget(t.category, t.name);
      if (r.ok) {
        // The card itself disappears on reload, so report on the list instead.
        this.resetMsg.set(
          `Reset "${t.name}" — removed it from ${r.unapplied ?? 0} auto-tagged file(s)` +
          ` and forgot ${r.examples_cleared ?? 0} example(s). Manual tags kept.`);
        this.confirmingId.set(null);
        await this.refresh();
      } else {
        this.cardMsg.set({ tagId: t.tag_id, text: `Reset failed: ${r.error ?? 'unknown error'}` });
      }
    } finally {
      this.forgettingId.set(null);
    }
  }
}
