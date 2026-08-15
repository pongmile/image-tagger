// Windowed Electron smoke: launches a real BrowserWindow rendering the built
// Angular app against the real preload IPC + better-sqlite3, drives a search,
// and captures the window to a PNG. Proves the actual GUI runs. Not shipped.
const { app, BrowserWindow, ipcMain, protocol, net } = require("electron");
const path = require("path");
const os = require("os");
const fs = require("fs");

protocol.registerSchemesAsPrivileged([{
  scheme: "image-tagger",
  privileges: {
    secure: true,
    standard: true,
    stream: true,
    supportFetchAPI: true,
    corsEnabled: true,
  },
}]);

// Electron can outlive the shell process that launched this smoke test on
// Windows.  Ignore only a closed output pipe; otherwise Node turns a harmless
// late diagnostic write into a main-process error dialog.
for (const stream of [process.stdout, process.stderr]) {
  stream?.on("error", (error) => {
    if (error.code !== "EPIPE") throw error;
  });
}

app.disableHardwareAcceleration();

const HOME = path.join(os.tmpdir(), "imgtag_win_smoke");
fs.rmSync(HOME, { recursive: true, force: true });
process.env.IMAGE_TAGGER_HOME = HOME;
app.setPath("userData", path.join(HOME, "electron-profile"));
const SHOT = process.env.SMOKE_SHOT || path.join(HOME, "window.png");
const MODELS_SHOT = path.join(path.dirname(SHOT), `${path.parse(SHOT).name}-models.png`);

const { openLibrary, search, countMatches } = require("./src/main/search");
const writes = require("./src/main/writes");
const { IndexerBridge } = require("./src/main/indexer");
const { fullImageUrl, registerFullImageProtocol } = require("./src/main/image-url");

function createLargeBmp(filePath, width = 4096, height = 3072) {
  const rowBytes = (width * 3 + 3) & ~3;
  const fileSize = 54 + rowBytes * height;
  const header = Buffer.alloc(54);
  header.write("BM", 0, "ascii");
  header.writeUInt32LE(fileSize, 2);
  header.writeUInt32LE(54, 10);
  header.writeUInt32LE(40, 14);
  header.writeInt32LE(width, 18);
  header.writeInt32LE(height, 22);
  header.writeUInt16LE(1, 26);
  header.writeUInt16LE(24, 28);
  header.writeUInt32LE(rowBytes * height, 34);
  const fd = fs.openSync(filePath, "w");
  try {
    fs.ftruncateSync(fd, fileSize);
    fs.writeSync(fd, header, 0, header.length, 0);
  } finally {
    fs.closeSync(fd);
  }
  return fileSize;
}

app.whenReady().then(async () => {
  // Bring up the daemon (creates schema) then seed a few rows via better-sqlite3.
  const bridge = new IndexerBridge({ indexerDir: path.join(__dirname, "../indexer"), auto: true });
  const ready = new Promise((r) => bridge.once("ready", r));
  bridge.start();
  await ready;

  const db = openLibrary();
  await bridge.call("add_root", { path: "D:/Pictures", mode: "include" });
  await bridge.call("set_variant", { facet: "wd14", variant: "eva02-large-v3" });
  await bridge.call("set_variant", { facet: "clip", variant: "vith14" });
  await bridge.call("set_variant", { facet: "insightface", variant: "buffalo_l" });
  await bridge.call("set_variant", { facet: "caption", variant: "blip-large" });
  const chars = ["hatsune miku", "frieren", "rem"];
  for (let i = 1; i <= 12; i++) {
    const c = chars[i % 3];
    const fn = `${c.replace(" ", "_")}_${String(i).padStart(3, "0")}.png`;
    const fid = db.prepare(
      "INSERT INTO files (path,filename,folder,sha256,width,height,size_bytes,image_kind,index_status)"
      + " VALUES (?,?,?,?,?,?,?,?, 'done')"
    ).run(`D:/Pictures/${c}/${fn}`, fn, `D:/Pictures/${c}`, "sha" + i, 512, 768, 200000 + i * 999,
          ["anime", "real", "other"][i % 3]).lastInsertRowid;
    writes.addManualTag(db, fid, "character", c);
    writes.addManualTag(db, fid, "scene", ["beach", "forest", "city"][i % 3]);
  }
  const tallPreviewId = db.prepare(
    "SELECT id FROM files WHERE filename LIKE 'hatsune_miku%' ORDER BY id LIMIT 1"
  ).get().id;
  const sample = path.resolve(__dirname, "../../samples/anime-neon-city-heroine.webp");
  const largeSampleDir = path.join(HOME, "Large Preview Path \u00fc");
  fs.mkdirSync(largeSampleDir, { recursive: true });
  const largeSample = path.join(largeSampleDir, "large preview over 30mb.bmp");
  const largeSampleSize = createLargeBmp(largeSample);
  const sampleSha = "sample-anime";
  const thumbDir = path.join(HOME, "thumbs", sampleSha.slice(0, 2));
  fs.mkdirSync(thumbDir, { recursive: true });
  fs.copyFileSync(sample, path.join(thumbDir, `${sampleSha}.webp`));
  db.prepare(
    "UPDATE files SET path=?,filename=?,folder=?,sha256=?,mime='image/bmp',width=4096,height=3072,size_bytes=? WHERE id=?"
  ).run(largeSample, path.basename(largeSample), path.dirname(largeSample), sampleSha, largeSampleSize, tallPreviewId);
  writes.refreshFts(db, tallPreviewId);
  for (let i = 1; i <= 48; i++) {
    writes.addManualTag(db, tallPreviewId, "detail", `visible-detail-${i}`);
  }
  writes.addManualTag(db, tallPreviewId, "general", "visible-general");
  // A learned suggestion exercises confirm/reject feedback in the preview.
  // Keep it on the first visible miku row so selection changes can verify that
  // its status message never leaks onto another image.
  writes.addManualTag(db, tallPreviewId, "character", "ashe");
  db.prepare(
    "UPDATE file_tags SET source='learned', confidence=.91 WHERE file_id=? AND tag_id=(SELECT id FROM tags WHERE name='ashe')"
  ).run(tallPreviewId);
  db.prepare("INSERT INTO file_metadata (file_id,key,value) VALUES (?,?,?)")
    .run(tallPreviewId, "exif:Model", "Smoke Camera");

  // Real IPC surface (same as main.js) so the renderer's window.api works.
  ipcMain.handle("search", (_e, q, opts) => search(db, q, opts));
  ipcMain.handle("count", (_e, q) => countMatches(db, q));
  ipcMain.handle("tags", (_e, id) => writes.tagsForFile(db, id));
  ipcMain.handle("file:detail", (_e, id) => writes.fileDetail(db, id));
  ipcMain.handle("file:thumb", (_e, id) => writes.thumbDataUri(db, id));
  ipcMain.handle("tag:add", (_e, id, c, n) => { writes.addManualTag(db, id, c, n); return writes.tagsForFile(db, id); });
  ipcMain.handle("tag:remove", (_e, id, c, n) => { writes.removeTag(db, id, c, n); return writes.tagsForFile(db, id); });
  ipcMain.handle("tag:bulkAdd", (_e, ids, c, n) => writes.bulkAddTag(db, ids, c, n));
  ipcMain.handle("tag:bulkRemove", (_e, ids, c, n) => writes.bulkRemoveTag(db, ids, c, n));
  ipcMain.handle("category:create", (_e, name, color) => writes.createCategory(db, name, color));
  ipcMain.handle("category:list", () => writes.listCategories(db));
  await registerFullImageProtocol(protocol, net, db);
  ipcMain.handle("file:full", (_e, id) => fullImageUrl(db, id));
  ipcMain.handle("ocr:set", (_e, id, text) => writes.setOcrText(db, id, text));
  ipcMain.handle("file:open", () => "");
  ipcMain.handle("file:reveal", () => undefined);
  ipcMain.handle("clipboard:write", () => true);
  ipcMain.handle("settings:get", (_e, k, f) => writes.getSetting(db, k, f));
  ipcMain.handle("settings:set", (_e, k, v) => writes.setSetting(db, k, v));
  // Same behavior as main.js under an isolated IMAGE_TAGGER_HOME profile
  // (always true for this smoke test): no real network call.
  ipcMain.handle("app:getVersion", () => app.getVersion());
  ipcMain.handle("app:checkForUpdates", () =>
    ({ ok: false, error: "update checks are disabled in a dev/test profile" }));
  ipcMain.handle("app:openReleasePage", () => false);
  ipcMain.handle("indexer:call", (_e, cmd, args) => {
    // Renderer regression: a rescan that removes indexed files must also
    // refresh an already-visible search result list.
    if (cmd === "rescan") {
      const removed = db.prepare(
        "DELETE FROM files WHERE id IN (SELECT ft.file_id FROM file_tags ft JOIN tags t ON t.id=ft.tag_id WHERE t.name='hatsune miku')"
      ).run().changes;
      setTimeout(() => BrowserWindow.getAllWindows()[0]?.webContents.send("indexer:scanDone",
        { event: "scan_done", ok: true, added: 0, changed: 0, removed, unchanged: 8 }), 20);
      return { started: true };
    }
    if (cmd === "rescan_root") {
      setTimeout(() => BrowserWindow.getAllWindows()[0]?.webContents.send("indexer:scanDone",
        { event: "scan_done", ok: true, added: 0, changed: 0, removed: 0,
          unchanged: 12, root_id: args.root_id }), 20);
      return { started: true };
    }
    if (cmd === "learn_confirm") {
      return new Promise((resolve) => setTimeout(() => resolve({ ok: true }), 150));
    }
    return bridge.call(cmd, args);
  });
  ipcMain.handle("indexer:semantic", (_e, q, k) => bridge.call("semantic", { query: q, k }));

  const showSmoke = process.env.SMOKE_SHOW === "1";
  const win = new BrowserWindow({
    width: 1200, height: 760, show: showSmoke,
    webPreferences: {
      preload: path.join(__dirname, "src/preload/preload.js"),
      backgroundThrottling: false,
      offscreen: !showSmoke,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });
  win.loadFile(path.join(__dirname, "renderer/dist/browser/index.html"));

  win.webContents.on("did-finish-load", async () => {
    try {
      // IPC progress must repaint without requiring any user click. Shaped
      // like db.progress()'s real payload, including the per-stage counts the
      // split Scan/Tags/Caption bars read.
      // 1,412 rows of which 212 are video: videos are scan-only, so Scan
      // measures against all 1,412 while Tags/Caption measure against the
      // 1,200 files a model can actually reach. Shaped this way deliberately —
      // the two denominators differing is the fix being verified below.
      const TOTAL = 1412, VIDEOS = 212, ANALYZABLE = TOTAL - VIDEOS;
      const progressEvent = (done) => ({ files_total: TOTAL, files_done: done,
        files_pending: TOTAL - done,
        jobs: { queued: TOTAL - done, running: 1, done },
        jobs_pending: TOTAL - done + 1,
        scan_done: TOTAL, scan_total: TOTAL,
        tag_done: 1000, tag_total: ANALYZABLE,
        caption_done: 780, caption_total: ANALYZABLE,
        videos_total: VIDEOS, analyzable_total: ANALYZABLE,
        // Per-media breakdown behind the "details" panel. Videos are scanned
        // but never tagged/described, so those halves are null — an explicit
        // "does not apply", not a bar sitting at 0%.
        media: {
          images: { total: ANALYZABLE, scanned: ANALYZABLE, indexed: done,
                    tagged: 1000, captioned: 780, errors: 0 },
          videos: { total: VIDEOS, scanned: VIDEOS, indexed: 0,
                    tagged: null, captioned: null, errors: 0 },
        },
        device: 'tagging/OCR CUDA · CLIP/caption CUDA',
        facets: { wd14: true, caption: true, ocr: true, clip: true, faces: false },
        paused: false, mode: 'auto' });
      win.webContents.send('indexer:progress', progressEvent(58));
      await new Promise((r) => setTimeout(r, 100));
      const live58 = await win.webContents.executeJavaScript(
        `document.querySelector('[data-testid=progress] .pl')?.textContent || ''`);
      win.webContents.send('indexer:progress', progressEvent(59));
      await new Promise((r) => setTimeout(r, 100));
      const live59 = await win.webContents.executeJavaScript(
        `document.querySelector('[data-testid=progress] .pl')?.textContent || ''`);
      // Outstanding work is shown as a count of *jobs*, not a done/total
      // ratio and not a count of files: index_status measures freshness
      // against the queue, so one "Reindex all" collapses files_done to ~0
      // while the coverage bars below still read 80%+ — as a ratio the two
      // looked contradictory — and it counts stale rows rather than work,
      // which is a much larger number than the queue actually holds.
      const liveProgress = /1,355 jobs left/.test(live58) && /1,354 jobs left/.test(live59);
      // Per-stage bars (§12): each stage must render a labelled, non-zero-width
      // track — a bar whose track is invisible or whose fill never moves is the
      // exact failure this split was meant to remove.
      const stages = await win.webContents.executeJavaScript(`
        (() => Array.from(document.querySelectorAll('[data-testid=stagebar] .stage'))
          .map((s) => ({
            label: s.querySelector('.stagelabel')?.textContent?.trim(),
            num: s.querySelector('.stagenum')?.textContent?.trim(),
            trackW: s.querySelector('.stagetrack')?.getBoundingClientRect().width || 0,
            fillPct: Math.round(((s.querySelector('.stagefill')?.getBoundingClientRect().width || 0)
              / (s.querySelector('.stagetrack')?.getBoundingClientRect().width || 1)) * 100),
          })))();
      `);
      // fillPct is measured against the track's border-box, so a fully-filled
      // bar reads a couple of percent short of 100 (the track carries a 1px
      // border each side). Assert "visually full" rather than an exact 100.
      //
      // Each stage must also print *its own* denominator. Tags and Caption
      // reading "/1,412" instead of "/1,200" is the specific bug this guards:
      // videos can never be tagged or described, so counting them in those
      // denominators pinned both bars below 100% permanently, and a fully
      // caught-up library looked stuck at 82% with no way to tell that from a
      // real stall.
      // Four stages, not three: Index measures queue freshness and is a
      // genuinely different question from the three coverage rows — it is the
      // one that collapses after a reindex while the others stay high, and
      // conflating it with them is what made the old display contradict
      // itself.
      const stagesOk = stages.length === 4
        && stages.every((s) => s.trackW > 10 && s.num)
        && stages[0].label === 'Scan' && stages[0].fillPct >= 95
        && stages[0].num === '1,412/1,412'
        && stages[1].label === 'Index' && stages[1].num === '59/1,412'
        && stages[2].label === 'Tags' && stages[2].fillPct > 80 && stages[2].fillPct < 90
        && stages[2].num === '1,000/1,200'
        && stages[3].label === 'Description' && stages[3].fillPct > 60 && stages[3].fillPct < 70
        && stages[3].num === '780/1,200';

      // The two things the user could not previously find out at all: what
      // hardware is running the models, and why Tags/Caption count fewer
      // files than Scan.
      const statusBadges = await win.webContents.executeJavaScript(`
        (() => ({
          device: document.querySelector('[data-testid=device]')?.textContent?.trim() || '',
          deviceIsCpu: !!document.querySelector('[data-testid=device].cpu'),
          videoNote: document.querySelector('[data-testid=video-note]')?.textContent?.trim() || '',
          current: document.querySelector('[data-testid=current-file]')?.textContent?.trim() || '',
        }))();
      `);
      const badgesOk = /CUDA/.test(statusBadges.device)
        && !statusBadges.deviceIsCpu
        && /212 videos/.test(statusBadges.videoNote);

      // The per-step breakdown: every stage split into images and videos, and
      // the enabled-but-unmeasurable steps listed rather than left invisible.
      // A video row for Tags must read as "does not apply", not as 0%.
      await win.webContents.executeJavaScript(
        `document.querySelector('[data-testid=status-details-toggle]').click()`);
      await new Promise((r) => setTimeout(r, 300));
      const detail = await win.webContents.executeJavaScript(`
        (() => {
          const rows = Array.from(document.querySelectorAll('[data-testid=status-detail] tbody tr'))
            .map((tr) => ({
              step: tr.getAttribute('data-step'),
              cells: Array.from(tr.querySelectorAll('td')).slice(1).map((td) => {
                const num = td.querySelector('.mininum')?.textContent?.trim();
                return num || td.querySelector('.na')?.textContent?.trim() || '';
              }),
            }));
          return {
            rows,
            headers: Array.from(document.querySelectorAll('[data-testid=status-detail] thead th'))
              .map((th) => th.textContent.trim()),
            facets: Array.from(document.querySelectorAll('[data-testid=status-detail] .facet'))
              .map((f) => f.textContent.trim()),
          };
        })();
      `);
      const byStep = Object.fromEntries(detail.rows.map((r) => [r.step, r.cells]));
      const detailOk = detail.headers.join('|') === 'Step|Images|Videos'
        && detail.rows.length === 4
        // Scan covers both media types; Tags/Description cover only images.
        && byStep.scan?.[0] === '1,200/1,200' && byStep.scan?.[1] === '212/212'
        && byStep.index?.[0] === '59/1,200'
        && byStep.tag?.[0] === '1,000/1,200' && /not tagged/.test(byStep.tag?.[1] || '')
        && byStep.caption?.[0] === '780/1,200' && /not described/.test(byStep.caption?.[1] || '')
        // OCR and CLIP are on and cost time per image, but store nothing
        // countable — listed by name instead of silently invisible.
        && detail.facets.includes('OCR') && detail.facets.includes('CLIP')
        && !detail.facets.includes('Faces');
      await win.webContents.executeJavaScript(
        `document.querySelector('[data-testid=status-details-toggle]').click()`);

      // Drive a search in the real renderer, then capture the window.
      await win.webContents.executeJavaScript(`
        (() => { const i = document.querySelector('[data-testid=search]');
          i.value = 'miku'; i.dispatchEvent(new Event('input', { bubbles: true })); })();
      `);
      await new Promise((r) => setTimeout(r, 700));
      // Use native mouse input, not HTMLElement.click(): pointer capture bugs
      // can otherwise make real row clicks fail while a synthetic click passes.
      const firstRow = await win.webContents.executeJavaScript(`(() => {
        const r = document.querySelector('[data-testid=row]')?.getBoundingClientRect();
        return r ? { x: Math.round(r.left + 30), y: Math.round(r.top + r.height / 2) } : null;
      })()`);
      if (!firstRow) throw new Error('first result row was not rendered');
      win.webContents.sendInputEvent({ type: 'mouseDown', x: firstRow.x, y: firstRow.y, button: 'left', clickCount: 1 });
      win.webContents.sendInputEvent({ type: 'mouseUp', x: firstRow.x, y: firstRow.y, button: 'left', clickCount: 1 });
      await new Promise((r) => setTimeout(r, 400));
      await new Promise((r) => setTimeout(r, 300)); // let the hidden window paint
      const count = await win.webContents.executeJavaScript(
        `document.querySelector('[data-testid=count]')?.textContent || ''`);
      const rows = await win.webContents.executeJavaScript(
        `document.querySelectorAll('[data-testid=row]').length`);
      const tags = await win.webContents.executeJavaScript(
        `document.querySelectorAll('app-preview .tag').length`);
      const collapsible = await win.webContents.executeJavaScript(`(async () => {
        const metadata = document.querySelector('[data-testid=metadata-toggle]');
        const general = document.querySelector('[data-testid=general-toggle]');
        if (metadata?.getAttribute('aria-expanded') === 'true') metadata.click();
        if (general?.getAttribute('aria-expanded') === 'true') general.click();
        await new Promise(r => setTimeout(r, 100));
        const collapsed = !document.querySelector('[data-testid=metadata]')
          && !document.querySelector('[data-testid=general-tags]');
        metadata?.click(); general?.click();
        await new Promise(r => setTimeout(r, 100));
        return { controls: !!metadata && !!general, collapsed,
          expanded: !!document.querySelector('[data-testid=metadata]')
            && !!document.querySelector('[data-testid=general-tags]') };
      })()`);

      // Regression: feedback is scoped to the image that initiated it.  If
      // selection changes while confirmation is in flight, neither the old
      // message nor its eventual async result may appear on the new preview.
      const feedbackScoped = await win.webContents.executeJavaScript(`(async () => {
        const confirm = document.querySelector('[data-testid=learn-confirm]');
        const rows = [...document.querySelectorAll('[data-testid=row]')];
        if (!confirm || rows.length < 2) return { controls: false, stale: true };
        confirm.click();
        rows[1].dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await new Promise(r => setTimeout(r, 350));
        const stale = !!document.querySelector('[data-testid=learn-msg]');
        rows[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await new Promise(r => setTimeout(r, 100));
        return { controls: true, stale };
      })()`);
      // Every full-size preview uses the shared zoom/pan viewer. Verify the
      // control path and wheel path, then close it so the UI screenshot below
      // remains a useful review of the main layout.
      const previewZoom = await win.webContents.executeJavaScript(`(async () => {
        document.querySelector('[data-testid=thumb-img]')?.click();
        const deadline = Date.now() + 5000;
        while (Date.now() < deadline) {
          const image = document.querySelector('[data-testid=lightbox] img');
          if (image?.src.startsWith('image-tagger:') && image.naturalWidth > 0) break;
          await new Promise(r => setTimeout(r, 50));
        }
        const lightbox = document.querySelector('[data-testid=lightbox]');
        const zoomIn = document.querySelector('[data-testid=zoom-in]');
        const reset = document.querySelector('[data-testid=zoom-reset]');
        if (!lightbox || !zoomIn || !reset) return { controls: false };
        const fullImage = lightbox.querySelector('img');
        const initial = reset.textContent.trim();
        const streamed = fullImage?.src.startsWith('image-tagger:') ?? false;
        const naturalWidth = fullImage?.naturalWidth ?? 0;
        const renderedWidth = fullImage?.getBoundingClientRect().width ?? 0;
        zoomIn.click();
        await new Promise(r => setTimeout(r, 50));
        const buttonZoom = reset.textContent.trim();
        reset.click();
        const viewport = lightbox.querySelector('.viewport');
        viewport?.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -100,
          clientX: innerWidth / 2, clientY: innerHeight / 2 }));
        await new Promise(r => setTimeout(r, 50));
        const wheelZoom = reset.textContent.trim();
        document.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Escape' }));
        await new Promise(r => setTimeout(r, 50));
        return { controls: true, streamed, naturalWidth, renderedWidth, initial, buttonZoom, wheelZoom,
          closed: !document.querySelector('[data-testid=lightbox]') };
      })()`);
      // Lightbox prev/next browsing (§8.2): the ⟨/⟩ buttons and ArrowLeft/
      // ArrowRight keys must step through the exact list currently being
      // searched ("miku"), swapping the loaded full-res image each time.
      const lightboxNav = await win.webContents.executeJavaScript(`(async () => {
        const waitLoaded = async () => {
          const deadline = Date.now() + 5000;
          while (Date.now() < deadline) {
            const image = document.querySelector('[data-testid=lightbox] img');
            if (image?.src.startsWith('image-tagger:') && image.naturalWidth > 0) break;
            await new Promise(r => setTimeout(r, 50));
          }
          await new Promise(r => setTimeout(r, 400)); // let the filename label re-render too
        };
        document.querySelector('[data-testid=thumb-img]')?.click();
        await waitLoaded();
        const startName = document.querySelector('.head .fn')?.textContent || '';
        const prevBtn = document.querySelector('[data-testid=lightbox-prev]');
        const nextBtn = document.querySelector('[data-testid=lightbox-next]');
        const prevDisabledAtStart = prevBtn?.disabled ?? true;
        const nextDisabledAtStart = nextBtn?.disabled ?? true;
        nextBtn?.click();
        await waitLoaded();
        const afterClickName = document.querySelector('.head .fn')?.textContent || '';
        document.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'ArrowRight' }));
        await waitLoaded();
        const afterKeyName = document.querySelector('.head .fn')?.textContent || '';
        document.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'ArrowLeft' }));
        await waitLoaded();
        const afterBackName = document.querySelector('.head .fn')?.textContent || '';
        document.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Escape' }));
        await new Promise(r => setTimeout(r, 50));
        return { hasButtons: !!prevBtn && !!nextBtn, prevDisabledAtStart, nextDisabledAtStart, startName, afterClickName,
          afterKeyName, afterBackName, closed: !document.querySelector('[data-testid=lightbox]') };
      })()`);
      // Grid list ArrowLeft/ArrowRight key navigation: pressing ArrowRight/ArrowLeft
      // when viewing search results in list mode moves selection between files.
      const gridNav = await win.webContents.executeJavaScript(`(async () => {
        const rs = [...document.querySelectorAll('[data-testid=row]')];
        if (rs.length < 2) return { ok: false };
        rs[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await new Promise(r => setTimeout(r, 50));
        const firstSel = document.querySelector('[data-testid=row].sel')?.dataset['fileId'];
        document.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'ArrowRight' }));
        await new Promise(r => setTimeout(r, 50));
        const secondSel = document.querySelector('[data-testid=row].sel')?.dataset['fileId'];
        document.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'ArrowLeft' }));
        await new Promise(r => setTimeout(r, 50));
        const backSel = document.querySelector('[data-testid=row].sel')?.dataset['fileId'];
        return {
          ok: firstSel != null && secondSel != null && backSel != null
            && firstSel !== secondSel && backSel === firstSel
        };
      })()`);
      console.log(`  gridNav=${JSON.stringify(gridNav)}`);
      // Multi-select grid preview (§8.2): picking >1 row must swap the right
      // pane to a plain thumbnail grid + filenames — no tags/metadata clutter.
      const multiSelect = await win.webContents.executeJavaScript(`(async () => {
        const rs = [...document.querySelectorAll('[data-testid=row]')];
        rs[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await new Promise(r => setTimeout(r, 100));
        rs[1].dispatchEvent(new MouseEvent('click', { bubbles: true, ctrlKey: true }));
        rs[2].dispatchEvent(new MouseEvent('click', { bubbles: true, ctrlKey: true }));
        await new Promise(r => setTimeout(r, 300));
        const head = document.querySelector('[data-testid=multi-head]')?.textContent || '';
        const cells = document.querySelectorAll('[data-testid=multi-cell]').length;
        const names = [...document.querySelectorAll('[data-testid=multi-cell] .multi-name')]
          .map(x => x.textContent.trim()).filter(Boolean).length;
        const tagsInGrid = document.querySelectorAll('app-preview .tag').length;
        rs[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await new Promise(r => setTimeout(r, 100));
        return { head, cells, names, tagsInGrid };
      })()`);
      console.log(`  lightboxNav=${JSON.stringify(lightboxNav)}`);
      console.log(`  multiSelect=${JSON.stringify(multiSelect)}`);
      // Capture before deliberately scrolling the preview so the artifact is a
      // useful full-window visual review, not a scrolled implementation detail.
      const img = await win.webContents.capturePage();
      fs.writeFileSync(SHOT, img.toPNG());
      const scroll = await win.webContents.executeJavaScript(`(() => {
        const p = document.querySelector('app-preview');
        const before = { clientHeight: p.clientHeight, scrollHeight: p.scrollHeight };
        p.scrollTop = p.scrollHeight;
        return { ...before, scrollTop: p.scrollTop };
      })()`);
      console.log(`  count="${count.trim()}"  rows=${rows}  previewTags=${tags}  scroll=${JSON.stringify(scroll)}`);
      console.log(`  screenshot: ${SHOT}`);
      const scrollOk = scroll.scrollHeight > scroll.clientHeight && scroll.scrollTop > 0;

      // Shift range + marquee selection, then the per-picture context menu.
      const selection = await win.webContents.executeJavaScript(`(async () => {
        const rs = [...document.querySelectorAll('[data-testid=row]')];
        rs[3].dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));
        await new Promise(r => setTimeout(r, 100));
        const shift = document.querySelectorAll('[data-testid=row].sel').length;
        const a = rs[0].getBoundingClientRect(), b = rs[2].getBoundingClientRect();
        const opts = (type, y) => ({ type, bubbles: true, button: 0, buttons: type === 'pointerup' ? 0 : 1,
          pointerId: 71, clientX: a.left + 30, clientY: y });
        rs[0].dispatchEvent(new PointerEvent('pointerdown', opts('pointerdown', a.top + 5)));
        rs[0].dispatchEvent(new PointerEvent('pointermove', opts('pointermove', b.bottom - 5)));
        rs[0].dispatchEvent(new PointerEvent('pointerup', opts('pointerup', b.bottom - 5)));
        await new Promise(r => setTimeout(r, 100));
        const marquee = document.querySelectorAll('[data-testid=row].sel').length;
        rs[1].dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 220, clientY: 260 }));
        await new Promise(r => setTimeout(r, 100));
        const menu = !!document.querySelector('[data-testid=picture-menu]');
        const prop = [...document.querySelectorAll('[data-testid=picture-menu] button')]
          .find(x => x.textContent.trim() === 'Properties');
        prop?.click();
        await new Promise(r => setTimeout(r, 100));
        return { shift, marquee, menu, bulk: !!document.querySelector('[data-testid=bulk]'),
          properties: !!document.querySelector('[data-testid=properties-dialog]') };
      })()`);
      console.log(`  selection=${JSON.stringify(selection)}`);
      console.log(`  collapsible=${JSON.stringify(collapsible)}`);
      console.log(`  feedbackScoped=${JSON.stringify(feedbackScoped)}`);
      console.log(`  previewZoom=${JSON.stringify(previewZoom)}`);
      console.log(`  liveProgress=${JSON.stringify({ live58, live59, ok: liveProgress })}`);
      console.log(`  stages=${JSON.stringify({ ok: stagesOk, stages })}`);
      console.log(`  statusBadges=${JSON.stringify({ ok: badgesOk, ...statusBadges })}`);
      console.log(`  statusDetail=${JSON.stringify({ ok: detailOk, ...detail })}`);

      // Re-enter Models: selected best/accurate variants must remain selected.
      await win.webContents.executeJavaScript(`document.querySelector('[data-testid=properties-dialog] button').click(); document.querySelector('[data-testid=tab-models]').click()`);
      await new Promise((r) => setTimeout(r, 1200));
      const variants = await win.webContents.executeJavaScript(
        `[...document.querySelectorAll('[data-testid=variant-select]')].map(x => x.value)`);
      await win.webContents.executeJavaScript(`document.querySelector('[data-testid=tab-search]').click(); document.querySelector('[data-testid=tab-models]').click()`);
      await new Promise((r) => setTimeout(r, 1200));
      const variantsAfterReopen = await win.webContents.executeJavaScript(
        `[...document.querySelectorAll('[data-testid=variant-select]')].map(x => x.value)`);
      const modelControlStyles = await win.webContents.executeJavaScript(`(() => {
        const select = document.querySelector('[data-testid=variant-select]');
        const input = document.querySelector('app-model-manager .dir input');
        const button = document.querySelector('app-model-manager .top button');
        const pick = (el) => { const s = getComputedStyle(el); return {
          radius: s.borderRadius, height: s.height, appearance: s.appearance } };
        return { select: pick(select), input: pick(input), button: pick(button) };
      })()`);
      const modelsImage = await win.webContents.capturePage();
      fs.writeFileSync(MODELS_SHOT, modelsImage.toPNG());
      console.log(`  variants=${JSON.stringify(variantsAfterReopen)}`);
      console.log(`  models screenshot: ${MODELS_SHOT}`);
      console.log(`  modelControlStyles=${JSON.stringify(modelControlStyles)}`);

      // Sources exposes a scan button for each enabled include root.
      await win.webContents.executeJavaScript(`document.querySelector('[data-testid=tab-sources]').click()`);
      await new Promise((r) => setTimeout(r, 500));
      const sourceScan = await win.webContents.executeJavaScript(`(() => {
        const b = document.querySelector('[data-testid=root-rescan]'); b?.click(); return !!b;
      })()`);
      await new Promise((r) => setTimeout(r, 400));
      const sourceMessage = await win.webContents.executeJavaScript(
        `document.querySelector('[data-testid=rescan-msg]')?.textContent || ''`);

      // Responsive layout regression: content must use the available width on
      // a large window, while narrow windows scroll only the data table (not
      // the whole document).
      win.setSize(1600, 900);
      await new Promise((r) => setTimeout(r, 300));
      const sourcesWide = await win.webContents.executeJavaScript(`(() => {
        const wrap = document.querySelector('app-sources .wrap');
        return { viewport: innerWidth, width: wrap?.getBoundingClientRect().width || 0 };
      })()`);
      win.setSize(760, 760);
      await new Promise((r) => setTimeout(r, 300));
      const sourcesNarrow = await win.webContents.executeJavaScript(`(() => {
        const scroller = document.querySelector('app-sources .table-scroll');
        return { viewport: innerWidth, documentWidth: document.documentElement.scrollWidth,
          scrollerClient: scroller?.clientWidth || 0, scrollerScroll: scroller?.scrollWidth || 0 };
      })()`);
      await win.webContents.executeJavaScript(`document.querySelector('[data-testid=tab-models]').click()`);
      await new Promise((r) => setTimeout(r, 300));
      const modelsNarrow = await win.webContents.executeJavaScript(`(() => ({
        viewport: innerWidth, documentWidth: document.documentElement.scrollWidth,
        scroller: !!document.querySelector('app-model-manager .table-scroll')
      }))()`);
      win.setSize(1200, 760);
      await new Promise((r) => setTimeout(r, 200));
      console.log(`  responsive=${JSON.stringify({ sourcesWide, sourcesNarrow, modelsNarrow })}`);
      const responsiveOk = sourcesWide.width >= sourcesWide.viewport * 0.9
        && sourcesNarrow.documentWidth <= sourcesNarrow.viewport + 1
        && sourcesNarrow.scrollerScroll >= sourcesNarrow.scrollerClient
        && modelsNarrow.documentWidth <= modelsNarrow.viewport + 1
        && modelsNarrow.scroller;

      // Settings > Updates (§ new): version shows, the check button exists
      // and is clickable, and — since this smoke test runs under an isolated
      // IMAGE_TAGGER_HOME profile — main.js must refuse a real network call
      // rather than silently attempting one.
      await win.webContents.executeJavaScript(`document.querySelector('[data-testid=tab-settings]').click()`);
      await new Promise((r) => setTimeout(r, 300));
      await win.webContents.executeJavaScript(`document.querySelector('[data-testid=check-updates]').click()`);
      await new Promise((r) => setTimeout(r, 300));
      const updates = await win.webContents.executeJavaScript(`(() => ({
        version: document.body.textContent.includes('Version') &&
          !!document.querySelector('[data-testid=check-updates]'),
        disabledMessage: (document.querySelector('[data-testid=update-status]')?.textContent || '')
      }))()`);
      console.log(`  settingsUpdates=${JSON.stringify(updates)}`);
      const updatesOk = updates.version && /disabled in a dev\/test profile/.test(updates.disabledMessage);
      await win.webContents.executeJavaScript(`document.querySelector('[data-testid=tab-search]').click()`);

      await win.webContents.executeJavaScript(
        `document.querySelector('[data-testid=rescan-btn]').click()`);
      await new Promise((r) => setTimeout(r, 500));
      const rowsAfterRescan = await win.webContents.executeJavaScript(
        `document.querySelectorAll('[data-testid=row]').length`);
      console.log(`  rowsAfterRescan=${rowsAfterRescan}`);
      const expectedVariants = ['eva02-large-v3', 'vith14', 'buffalo_l', 'blip-large'];
      const variantsOk = JSON.stringify(variants) === JSON.stringify(expectedVariants)
        && JSON.stringify(variantsAfterReopen) === JSON.stringify(expectedVariants);
      const selectionOk = selection.shift === 4 && selection.marquee === 3 && selection.menu
        && selection.bulk && selection.properties;
      const lightboxNavOk = lightboxNav.hasButtons && lightboxNav.prevDisabledAtStart
        && lightboxNav.afterClickName !== lightboxNav.startName
        && lightboxNav.afterKeyName !== lightboxNav.afterClickName
        && lightboxNav.afterBackName === lightboxNav.afterClickName
        && lightboxNav.closed;
      const multiSelectOk = /3 images selected/.test(multiSelect.head) && multiSelect.cells === 3
        && multiSelect.names === 3 && multiSelect.tagsInGrid === 0;
      const ok = /results/.test(count) && rows > 0 && tags > 0 && scrollOk
        && liveProgress && stagesOk && badgesOk && detailOk && gridNav.ok
        && collapsible.controls && collapsible.collapsed && collapsible.expanded
        && feedbackScoped.controls && !feedbackScoped.stale
        && previewZoom.controls && previewZoom.streamed && previewZoom.naturalWidth === 4096
        && previewZoom.renderedWidth > 600 && previewZoom.initial === '100%'
        && previewZoom.buttonZoom === '125%' && previewZoom.wheelZoom === '120%'
        && previewZoom.closed
        && lightboxNavOk && multiSelectOk
        && selectionOk && variantsOk && sourceScan && /unchanged/.test(sourceMessage)
        && responsiveOk && rowsAfterRescan === 0 && updatesOk;
      console.log(ok ? "RESULT: PASS — Electron window renders the Angular app with live data"
                     : "RESULT: FAIL — window did not render expected content");
      await bridge.stop();
      app.exit(ok ? 0 : 1);
    } catch (e) { console.error("WIN SMOKE ERROR:", e); app.exit(1); }
  });
});
