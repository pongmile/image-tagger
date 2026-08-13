import { Component, EventEmitter, HostListener, Input, OnChanges, Output, SimpleChanges } from '@angular/core';

@Component({
  selector: 'app-zoomable-lightbox',
  standalone: true,
  template: `
    <div class="backdrop" data-testid="lightbox" (click)="onBackdrop($event)">
      <div class="toolbar" (click)="$event.stopPropagation()" aria-label="Image zoom controls">
        <button type="button" (click)="zoomBy(1 / 1.25)" [disabled]="zoom <= 1"
                title="Zoom out" data-testid="zoom-out">−</button>
        <button type="button" class="level" (click)="reset()" title="Reset zoom"
                data-testid="zoom-reset">{{ zoomPercent }}%</button>
        <button type="button" (click)="zoomBy(1.25)" [disabled]="zoom >= maxZoom"
                title="Zoom in" data-testid="zoom-in">+</button>
      </div>
      <button class="close" type="button" (click)="closed.emit()" title="Close (Esc)"
              aria-label="Close image preview">×</button>
      <div #viewport class="viewport" [class.dragging]="dragging" [class.zoomed]="zoom > 1"
           (wheel)="onWheel($event, viewport)" (dblclick)="toggleZoom($event)"
           (pointerdown)="startPan($event)" (pointermove)="movePan($event)"
           (pointerup)="endPan($event)" (pointercancel)="endPan($event)">
        @if (displaySrc) {
          <img [src]="displaySrc" [alt]="alt" draggable="false" decoding="async"
               (error)="retryImage()" (load)="onImageLoad($event, viewport)"
               [style.transform]="imageTransform" (click)="$event.stopPropagation()" />
        } @else {
          <div class="loading">Loading full image…</div>
        }
      </div>
      <div class="hint">Mouse wheel or +/− to zoom · drag to pan · double-click to reset</div>
    </div>
  `,
  styles: [`
    :host { position: fixed; inset: 0; z-index: 1000; }
    .backdrop { position: absolute; inset: 0; overflow: hidden; background: rgba(3, 7, 18, .94);
                backdrop-filter: blur(8px); display: grid; place-items: center; }
    .viewport { position: absolute; inset: 62px 24px 46px; display: grid; place-items: center;
                place-content: center; overflow: hidden; touch-action: none; cursor: default; }
    .viewport.zoomed { cursor: grab; }
    .viewport.dragging { cursor: grabbing; }
    /* The image is laid out at its native intrinsic size (no object-fit/
       max-width shrink) so Blink always rasterizes it at full resolution;
       ALL display scaling -- both the initial fit-to-viewport and any
       zoom-in -- happens via the single scale() in imageTransform
       (zoom * baseScale), computed in JS once natural size is known. Fitting
       via object-fit plus a separate transform scale() zoom on top of it
       (the previous approach) meant zooming magnified an already
       down-rasterized layer, visibly softer than the source at high zoom.
       A large photo's native size is almost always bigger than .viewport, so
       its single implicit grid track overflows the container -- place-items:
       center only centers an item *within* its own track/area and does
       nothing once the track itself is larger than the container. Without
       place-content: center too, the oversized track (and so the
       unscaled image) pins to the top-left, and transform-origin's center
       point ends up far outside the visible viewport -- only a corner of
       the shrunk image peeks in near the bottom-right, the bug reported
       here. place-content: center centers the overflowing track itself so
       its excess is clipped equally on every side, keeping the
       transform-origin center aligned with the viewport's actual center. */
    img { display: block; user-select: none; pointer-events: auto;
          transform-origin: center center; will-change: transform;
          border-radius: 8px; box-shadow: 0 24px 80px #000b; }
    .toolbar { position: fixed; z-index: 2; top: 16px; left: 50%; transform: translateX(-50%);
               display: flex; align-items: center; gap: 4px; padding: 5px;
               border: 1px solid #ffffff2e; border-radius: 14px; background: #101827e8;
               box-shadow: 0 12px 36px #0008; }
    .toolbar button, .close { min-width: 38px; min-height: 36px; padding: 6px 10px;
                              border: 0; border-radius: 10px; background: #ffffff14;
                              color: #f8fafc; font-size: 18px; }
    .toolbar button:hover:not(:disabled), .close:hover { background: #ffffff2b; }
    .toolbar .level { min-width: 76px; font-size: 12px; font-variant-numeric: tabular-nums; }
    .toolbar button:disabled { opacity: .35; }
    .close { position: fixed; z-index: 2; top: 16px; right: 20px; font-size: 26px; line-height: 1; }
    .loading { color: #f8fafc; font-size: 14px; }
    .hint { position: fixed; z-index: 2; bottom: 16px; left: 50%; transform: translateX(-50%);
            color: #cbd5e1; font-size: 11px; white-space: nowrap; pointer-events: none; }
    @media (max-width: 560px) {
      .viewport { inset-inline: 8px; }
      .hint { display: none; }
      .close { right: 8px; }
    }
  `],
})
export class ZoomableLightboxComponent implements OnChanges {
  @Input() src: string | null = null;
  @Input() alt = '';
  @Output() readonly closed = new EventEmitter<void>();
  displaySrc: string | null = null;

  readonly maxZoom = 8;
  zoom = 1;
  panX = 0;
  panY = 0;
  dragging = false;
  // Scale that fits the image's native size to the viewport (what
  // object-fit:contain used to compute for us). Defaults to 1 (native size,
  // same as a plain unstyled <img>) until onImageLoad reports real
  // dimensions and corrects it -- naturalWidth/Height (and so layout/paint)
  // can be available before the `load` event fires, so this must never be 0
  // or the image is invisible (scale(0)) for that window.
  baseScale = 1;
  private naturalWidth = 0;
  private naturalHeight = 0;
  private viewportEl: HTMLElement | null = null;
  private pointerId: number | null = null;
  private dragX = 0;
  private dragY = 0;
  private imageRetries = 0;

  ngOnChanges(changes: SimpleChanges) {
    if (!changes['src']) return;
    this.displaySrc = this.src;
    this.imageRetries = 0;
    this.baseScale = 1;
    this.naturalWidth = 0;
    this.naturalHeight = 0;
  }

  onImageLoad(event: Event, viewportEl: HTMLElement) {
    const img = event.target as HTMLImageElement;
    this.naturalWidth = img.naturalWidth;
    this.naturalHeight = img.naturalHeight;
    this.viewportEl = viewportEl;
    this.recomputeBaseScale();
  }

  @HostListener('window:resize')
  onResize() { this.recomputeBaseScale(); }

  private recomputeBaseScale() {
    if (!this.viewportEl || !this.naturalWidth || !this.naturalHeight) return;
    const { clientWidth, clientHeight } = this.viewportEl;
    this.baseScale = Math.min(clientWidth / this.naturalWidth, clientHeight / this.naturalHeight, 1) || 1;
  }

  retryImage() {
    if (!this.src || this.imageRetries >= 10) return;
    const retry = ++this.imageRetries;
    setTimeout(() => {
      if (!this.src || retry !== this.imageRetries) return;
      const separator = this.src.includes('?') ? '&' : '?';
      this.displaySrc = `${this.src}${separator}retry=${retry}`;
    }, 200);
  }

  get zoomPercent(): number { return Math.round(this.zoom * 100); }
  get imageTransform(): string {
    return `translate3d(${this.panX}px, ${this.panY}px, 0) scale(${this.zoom * this.baseScale})`;
  }

  zoomBy(factor: number) { this.setZoom(this.zoom * factor); }

  private setZoom(value: number, x?: number, y?: number) {
    const oldZoom = this.zoom;
    const next = Math.min(this.maxZoom, Math.max(1, value));
    if (x != null && y != null && oldZoom !== next) {
      this.panX = x - (x - this.panX) * (next / oldZoom);
      this.panY = y - (y - this.panY) * (next / oldZoom);
    }
    this.zoom = next;
    if (next === 1) this.resetPan();
  }

  onWheel(event: WheelEvent, viewport: HTMLElement) {
    event.preventDefault();
    event.stopPropagation();
    const rect = viewport.getBoundingClientRect();
    this.setZoom(this.zoom * (event.deltaY < 0 ? 1.2 : 1 / 1.2),
      event.clientX - rect.left - rect.width / 2,
      event.clientY - rect.top - rect.height / 2);
  }

  toggleZoom(event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    if (this.zoom > 1) this.reset();
    else this.setZoom(2);
  }

  startPan(event: PointerEvent) {
    if (this.zoom <= 1 || event.button !== 0) return;
    this.dragging = true;
    this.pointerId = event.pointerId;
    this.dragX = event.clientX - this.panX;
    this.dragY = event.clientY - this.panY;
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }

  movePan(event: PointerEvent) {
    if (!this.dragging || event.pointerId !== this.pointerId) return;
    this.panX = event.clientX - this.dragX;
    this.panY = event.clientY - this.dragY;
  }

  endPan(event: PointerEvent) {
    if (event.pointerId !== this.pointerId) return;
    this.dragging = false;
    this.pointerId = null;
  }

  reset() { this.zoom = 1; this.resetPan(); }
  private resetPan() { this.panX = 0; this.panY = 0; }

  onBackdrop(event: MouseEvent) {
    if (event.target === event.currentTarget) this.closed.emit();
  }

  @HostListener('document:keydown.escape')
  onEscape() { this.closed.emit(); }
}
