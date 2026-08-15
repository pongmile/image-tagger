import { Component, computed, inject, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { LibraryService, formatDuration } from './library.service';

/** The "what is the program doing right now" strip (spec §12 visibility).
 *
 * Rendered once, above the tab switch, so it is on *every* page. It used to
 * exist only inside the Search view's header, with the Sources page keeping a
 * near-copy of it and every other page (Models, People, Learned tags,
 * Settings) showing nothing at all — so the answer to "is it still working?"
 * depended on which tab you happened to be on, and the two pages that did
 * answer it disagreed with each other.
 *
 * Three levels of detail, because three different questions get asked:
 *
 *   1. The summary line — "is it running, how much is left, how long, on what
 *      hardware". Always visible.
 *   2. The stage bars — which step is behind. Always visible.
 *   3. The breakdown — each stage split into images and videos, plus the
 *      inference steps that have no coverage metric but still cost time.
 *      Behind a toggle: it is the answer to "why is that number what it is",
 *      which is not a question being asked continuously.
 *
 * The image/video split is not decoration. Scan covers both, Tags and
 * Description cover only images, and Index measures something else entirely
 * (queue freshness) — four ratios that legitimately do not add up to each
 * other. Showing them as four bare totals left the user to infer that
 * structure, and the most natural inference ("Tags is stuck at 82%") was
 * wrong.
 */
@Component({
  selector: 'app-index-status',
  standalone: true,
  imports: [DecimalPipe],
  template: `
    @let p = lib.progress();
    @if (p) {
      <div class="strip" data-testid="index-status">
        <div class="summary">
          <span class="state" [class.paused]="lib.paused()" [class.idle]="!lib.indexing()">
            {{ stateLabel() }}
          </span>
          @if (lib.indexing()) {
            <span class="queue" [title]="queueTitle()" data-testid="queue-depth">
              {{ lib.queuedJobs() | number }} jobs left
            </span>
            @if (etaLabel()) {
              <span class="eta" data-testid="eta"
                    title="measured from real throughput over the last minute">{{ etaLabel() }}</span>
            }
          }
          @if (lib.deviceLabel()) {
            <span class="device" [class.cpu]="deviceIsCpu()" data-testid="device"
                  [title]="deviceTitle()">{{ deviceIsCpu() ? '🐌' : '⚡' }} {{ lib.deviceLabel() }}</span>
          }
          @if (lib.workingLabel()) {
            <span class="current" [title]="lib.workingLabel()" data-testid="current-file">{{ lib.workingLabel() }}</span>
          }
          <span class="spacer"></span>
          @if (ramLabel()) { <span class="ram" title="memory held by the background indexer — mostly the loaded models">{{ ramLabel() }}</span> }
          <button class="toggle" (click)="open.set(!open())" data-testid="status-details-toggle"
                  [title]="open() ? 'hide the per-step breakdown' : 'show what each step has finished, images vs videos'">
            {{ open() ? '▴ less' : '▾ details' }}
          </button>
        </div>

        <div class="stagebar" data-testid="stagebar">
          @for (s of lib.stages(); track s.label) {
            <span class="stage" [title]="s.title" [attr.data-stage]="s.label">
              <span class="stagelabel">{{ s.label }}</span>
              <span class="stagetrack"><span class="stagefill" [style.width.%]="s.pct"></span></span>
              <span class="stagenum">{{ s.done | number }}/{{ s.total | number }}</span>
            </span>
          }
          @if (videoNote()) {
            <span class="videonote" data-testid="video-note"
                  title="Videos get a thumbnail and filename/folder search, but no tagging or description — so they count under Scan and are excluded from Tags/Description.">{{ videoNote() }}</span>
          }
        </div>

        @if (open()) {
          <div class="detail" data-testid="status-detail">
            <table>
              <thead>
                <tr>
                  <th class="stepcol">Step</th>
                  <th>Images</th>
                  @if (lib.hasVideos()) { <th>Videos</th> }
                </tr>
              </thead>
              <tbody>
                @for (row of lib.stageDetail(); track row.key) {
                  <tr [attr.data-step]="row.key">
                    <td class="stepcol">
                      <span class="steplabel">{{ row.label }}</span>
                      <span class="stepwhat">{{ row.what }}</span>
                    </td>
                    <td>
                      @if (row.images; as c) {
                        <div class="cell">
                          <div class="minitrack"><div class="minifill" [style.width.%]="c.pct"></div></div>
                          <span class="mininum">{{ c.done | number }}/{{ c.total | number }}</span>
                          <span class="minipct">{{ c.pct | number:'1.0-0' }}%</span>
                        </div>
                      } @else { <span class="na">—</span> }
                    </td>
                    @if (lib.hasVideos()) {
                      <td>
                        @if (row.videos; as c) {
                          <div class="cell">
                            <div class="minitrack"><div class="minifill" [style.width.%]="c.pct"></div></div>
                            <span class="mininum">{{ c.done | number }}/{{ c.total | number }}</span>
                            <span class="minipct">{{ c.pct | number:'1.0-0' }}%</span>
                          </div>
                        } @else {
                          <span class="na" [title]="'Videos are browse/search-only — ' + row.videoNote">— {{ row.videoNote }}</span>
                        }
                      </td>
                    }
                  </tr>
                }
              </tbody>
            </table>

            @if (lib.runningFacets().length) {
              <div class="alsorunning">
                <span class="alsolabel">also running per image:</span>
                @for (f of lib.runningFacets(); track f.key) {
                  <span class="facet" [title]="f.what" [attr.data-facet]="f.key">{{ f.label }}</span>
                }
                <span class="alsonote">these have no percentage — an image with no text and an image never scanned for text both store nothing, so there is nothing to count</span>
              </div>
            }
          </div>
        }
      </div>
    }
  `,
  styles: [`
    .strip { display: flex; flex-direction: column; gap: 4px; padding: 6px 12px;
             border-bottom: 1px solid var(--border); background: var(--bg-2); }
    .summary { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
               font-size: 11px; color: var(--fg-dim); font-variant-numeric: tabular-nums; }
    .spacer { flex: 1 1 auto; }
    .state { font-weight: 600; color: var(--fg); }
    .state.paused { color: #d97706; }
    .state.idle { color: var(--fg-dim); font-weight: 500; }
    .queue, .eta, .ram { white-space: nowrap; }
    /* The filename is the only part that changes length constantly; letting it
       size the row makes the whole strip jitter sideways as files go by. */
    .current { flex: 0 1 auto; min-width: 0; max-width: 280px; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; opacity: .85; }
    .device { font-size: 10px; padding: 1px 6px; border-radius: 999px; white-space: nowrap;
              border: 1px solid var(--border); color: var(--fg-dim); background: var(--bg); }
    .device.cpu { color: #d97706; border-color: currentColor; }
    .toggle { font-size: 10px; padding: 2px 8px; border-radius: 999px; cursor: pointer;
              border: 1px solid var(--border); background: var(--bg); color: var(--fg-dim); }
    .toggle:hover { color: var(--fg); border-color: var(--accent); }

    .stagebar { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
    .stage { display: inline-flex; align-items: center; gap: 6px; }
    .stagelabel { font-size: 11px; color: var(--fg-dim); white-space: nowrap; }
    /* Explicit border, not just a fill colour: an empty track on var(--bg-2)
       is invisible against the strip on the dark theme. */
    .stagetrack { position: relative; display: inline-block; width: 88px; height: 8px;
                  border-radius: 999px; background: var(--bg); overflow: hidden;
                  border: 1px solid var(--border); }
    .stagefill { position: absolute; inset: 0 auto 0 0; background: var(--accent);
                 border-radius: 999px; transition: width .3s; }
    .stagenum { font-size: 10px; color: var(--fg-dim); white-space: nowrap;
                font-variant-numeric: tabular-nums; }
    .videonote { font-size: 10px; color: var(--fg-dim); opacity: .8; white-space: nowrap; }

    .detail { padding: 6px 0 2px; overflow-x: auto; }
    .detail table { border-collapse: collapse; width: 100%; }
    .detail th { text-align: left; font-size: 10px; font-weight: 600; text-transform: uppercase;
                 letter-spacing: .04em; color: var(--fg-dim); padding: 2px 14px 4px 0;
                 border-bottom: 1px solid var(--border); }
    .detail td { padding: 5px 14px 5px 0; vertical-align: top; }
    .stepcol { min-width: 210px; }
    .steplabel { display: block; font-size: 12px; font-weight: 600; color: var(--fg); }
    .stepwhat { display: block; font-size: 10px; color: var(--fg-dim); max-width: 340px; }
    .cell { display: flex; align-items: center; gap: 8px; }
    .minitrack { position: relative; width: 110px; height: 8px; border-radius: 999px;
                 background: var(--bg); overflow: hidden; border: 1px solid var(--border);
                 flex: 0 0 auto; }
    .minifill { position: absolute; inset: 0 auto 0 0; background: var(--accent);
                border-radius: 999px; transition: width .3s; }
    .mininum { font-size: 11px; color: var(--fg); font-variant-numeric: tabular-nums;
               white-space: nowrap; }
    .minipct { font-size: 10px; color: var(--fg-dim); font-variant-numeric: tabular-nums; }
    .na { font-size: 11px; color: var(--fg-dim); opacity: .75; white-space: nowrap; }

    .alsorunning { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
                   padding-top: 8px; margin-top: 6px; border-top: 1px solid var(--border); }
    .alsolabel { font-size: 10px; color: var(--fg-dim); text-transform: uppercase;
                 letter-spacing: .04em; }
    .facet { font-size: 11px; font-weight: 600; padding: 1px 8px; border-radius: 999px;
             background: var(--bg); border: 1px solid var(--border); color: var(--fg); }
    .alsonote { font-size: 10px; color: var(--fg-dim); opacity: .8; }
  `],
})
export class IndexStatusComponent {
  readonly lib = inject(LibraryService);
  readonly open = signal(false);

  readonly stateLabel = computed(() => {
    if (this.lib.paused()) return '⏸ paused';
    if (this.lib.indexing()) return '● indexing';
    const n = this.lib.progress()?.files_total ?? 0;
    return `✓ up to date · ${n.toLocaleString()} files`;
  });

  readonly deviceIsCpu = computed(() => /\bCPU\b/.test(this.lib.deviceLabel()));

  readonly deviceTitle = computed(() => this.deviceIsCpu()
    ? 'At least one model is running on the CPU. If you expected GPU acceleration, '
      + 'check Models — the installed onnxruntime/torch build must be able to bind your GPU.'
    : 'Hardware actually running the models right now, read back from the loaded '
      + 'models themselves rather than from what was requested.');

  readonly etaLabel = computed(() => {
    const t = this.lib.throughput();
    if (!t) return '';
    const rate = t.perMin >= 60 ? `${Math.round(t.perMin)}/min` : `${t.perMin.toFixed(1)}/min`;
    return t.etaMs == null ? rate : `${rate} · ~${formatDuration(t.etaMs)} left`;
  });

  readonly ramLabel = computed(() => {
    const mb = this.lib.progress()?.rss_mb;
    if (mb == null) return '';
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB RAM` : `${Math.round(mb)} MB RAM`;
  });

  readonly videoNote = computed(() => {
    const v = this.lib.progress()?.videos_total ?? 0;
    return v > 0 ? `${v.toLocaleString()} videos: scan only` : '';
  });

  queueTitle(): string {
    const p = this.lib.progress();
    const jobs = p?.jobs ?? {};
    const parts = Object.entries(jobs).map(([state, n]) => `${n.toLocaleString()} ${state}`);
    return 'Jobs still queued or running. Each file can produce several jobs '
      + '(scan, tagging, description), so this counts work, not files.\n'
      + parts.join(' · ');
  }
}
