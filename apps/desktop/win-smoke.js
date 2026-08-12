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
      // IPC progress must repaint without requiring any user click.
      const progressEvent = (done) => ({ files_total: 1412, files_done: done,
        jobs: { queued: 1412 - done, running: 1, done }, paused: false, mode: 'auto' });
      win.webContents.send('indexer:progress', progressEvent(58));
      await new Promise((r) => setTimeout(r, 100));
      const live58 = await win.webContents.executeJavaScript(
        `document.querySelector('[data-testid=progress] .pl')?.textContent || ''`);
      win.webContents.send('indexer:progress', progressEvent(59));
      await new Promise((r) => setTimeout(r, 100));
      const live59 = await win.webContents.executeJavaScript(
        `document.querySelector('[data-testid=progress] .pl')?.textContent || ''`);
      const liveProgress = /58\/1412/.test(live58) && /59\/1412/.test(live59);

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
      const ok = /results/.test(count) && rows > 0 && tags > 0 && scrollOk
        && liveProgress
        && collapsible.controls && collapsible.collapsed && collapsible.expanded
        && feedbackScoped.controls && !feedbackScoped.stale
        && previewZoom.controls && previewZoom.streamed && previewZoom.naturalWidth === 4096
        && previewZoom.renderedWidth > 600 && previewZoom.initial === '100%'
        && previewZoom.buttonZoom === '125%' && previewZoom.wheelZoom === '120%'
        && previewZoom.closed
        && selectionOk && variantsOk && sourceScan && /unchanged/.test(sourceMessage)
        && responsiveOk && rowsAfterRescan === 0;
      console.log(ok ? "RESULT: PASS — Electron window renders the Angular app with live data"
                     : "RESULT: FAIL — window did not render expected content");
      await bridge.stop();
      app.exit(ok ? 0 : 1);
    } catch (e) { console.error("WIN SMOKE ERROR:", e); app.exit(1); }
  });
});
