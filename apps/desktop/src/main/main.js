// Electron main — M2 shell.
// Owns the read path (better-sqlite3 over library.db, WAL) plus small manual-tag
// writes (spec §9). The Angular renderer replaces index.html at M9; the search
// IPC contract below is stable. Spec §4.
const { app, BrowserWindow, ipcMain, dialog, shell, clipboard, Menu, protocol, net } = require("electron");
const path = require("path");
const fs = require("fs");
const { openLibrary, search, countMatches } = require("./search");
const writes = require("./writes");
const { IndexerBridge } = require("./indexer");
const { fullImageUrl, registerFullImageProtocol } = require("./image-url");

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

const packageSmoke = process.env.IMAGE_TAGGER_PACKAGE_SMOKE === "1";
if (packageSmoke && process.env.IMAGE_TAGGER_HOME) {
  app.disableHardwareAcceleration();
  app.setPath("userData", path.join(process.env.IMAGE_TAGGER_HOME, "electron-profile"));
}
const packageSmokeLog = packageSmoke && process.env.IMAGE_TAGGER_HOME
  ? path.join(process.env.IMAGE_TAGGER_HOME, "package-smoke.log")
  : null;
function logPackageSmoke(message) {
  if (!packageSmokeLog) return;
  try { fs.appendFileSync(packageSmokeLog, `${message}\n`, "utf8"); } catch { /* diagnostic only */ }
}
logPackageSmoke("main-start");

let db;
let indexer;
let mainWindow;
let packageSmokeImageId = null;

const allowedIndexerCommands = new Set([
  "rescan", "rescan_root", "add_root", "progress", "pause", "resume",
  "set_mode", "retry_errors", "reindex_all", "reindex_root", "recaption_root",
  "list_errors", "roots", "remove_root", "toggle_root", "add_exclude_pattern",
  "remove_exclude", "toggle_exclude", "rename_tag", "list_tags", "learn_status",
  "learn", "learn_confirm", "learn_reject", "reject_tag", "confirm_tag",
  "list_learned_tags", "download", "install_dependency", "download_status",
  "facets", "set_facet_enabled", "models_dir", "variants", "model_state",
  "set_variant", "persons", "person_files", "name_person", "merge_persons",
]);

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function startIndexerAndWaitForDatabase(bridge, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("indexer database did not become ready"));
    }, timeoutMs);
    const cleanup = () => {
      clearTimeout(timer);
      bridge.off("db_ready", onReady);
      bridge.off("exit", onExit);
    };
    const onReady = () => { cleanup(); resolve(); };
    const onExit = (code) => {
      cleanup();
      reject(new Error(`indexer daemon exited during startup (${code})`));
    };
    bridge.once("db_ready", onReady);
    bridge.once("exit", onExit);
    bridge.start();
  });
}
const isSqliteBusy = (error) => {
  const code = String(error?.code || "");
  const message = String(error?.message || error || "");
  return code === "SQLITE_BUSY" || code === "SQLITE_LOCKED" ||
    /database (?:is )?locked|database table is locked/i.test(message);
};

// Retry a complete, rolled-back transaction asynchronously so indexing cannot
// make a manual edit fail merely because both writers reached SQLite together.
async function withSqliteRetry(operation, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let waitMs = 25;
  for (;;) {
    try {
      return operation();
    } catch (error) {
      if (!isSqliteBusy(error) || Date.now() >= deadline) throw error;
      await delay(waitMs);
      waitMs = Math.min(400, Math.round(waitMs * 1.7));
    }
  }
}

// Prefer the built Angular renderer; fall back to the M2 placeholder page if it
// hasn't been built yet (`npm --workspace renderer run build`).
function rendererIndex() {
  const built = path.join(__dirname, "../../renderer/dist/browser/index.html");
  if (fs.existsSync(built)) return built;
  return path.join(__dirname, "../renderer/index.html");
}

function indexedPath(filePath) {
  if (typeof filePath !== "string" || !filePath.trim()) throw new TypeError("Invalid file path");
  const row = db.prepare("SELECT path FROM files WHERE path=?").get(filePath);
  if (!row) throw new Error("The file is not part of the indexed library");
  return row.path;
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();
logPackageSmoke(`single-instance-lock=${hasSingleInstanceLock}`);
if (!hasSingleInstanceLock) app.quit();

app.on("second-instance", () => {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
});

app.whenReady().then(async () => {
  if (!hasSingleInstanceLock) return;

  // Python owns schema creation and migrations. On a fresh/slow machine,
  // opening the renderer first lets its initial IPC reads race the migration
  // and fail with "no such table". Do not expose the Node read connection or
  // load Angular until the daemon confirms database startup is complete.
  indexer = new IndexerBridge({ auto: true });
  await startIndexerAndWaitForDatabase(indexer);
  logPackageSmoke("database-ready");
  db = openLibrary();
  await registerFullImageProtocol(protocol, net, db, logPackageSmoke);
  if (packageSmoke) {
    const packagedSample = path.join(process.resourcesPath, "samples", "beach-sunset-kayak.jpg");
    const samplePath = path.join(process.env.IMAGE_TAGGER_HOME, "Package Smoke Image ü.jpg");
    fs.copyFileSync(packagedSample, samplePath);
    packageSmokeImageId = Number(db.prepare(
      "INSERT INTO files (path,filename,folder,sha256,mime,index_status) " +
      "VALUES (?,?,?,?,?,'done') ON CONFLICT(path) DO UPDATE SET mime=excluded.mime RETURNING id"
    ).get(samplePath, path.basename(samplePath), path.dirname(samplePath),
      "package-smoke-sample", "image/jpeg").id);
  }

  // Read path (hot) --------------------------------------------------------
  ipcMain.handle("search", (_e, query, opts) => search(db, query, opts));
  ipcMain.handle("count", (_e, query, opts) => countMatches(db, query, opts));
  ipcMain.handle("tags", (_e, fileId, minConfidence) =>
    writes.tagsForFile(db, fileId, minConfidence)
  );

  // Manual-tag writes (spec §9) -------------------------------------------
  ipcMain.handle("tag:add", (_e, fileId, category, name) =>
    withSqliteRetry(() => {
      writes.addManualTag(db, fileId, category, name);
      return writes.tagsForFile(db, fileId);
    })
  );
  ipcMain.handle("tag:remove", (_e, fileId, category, name) =>
    withSqliteRetry(() => {
      writes.removeTag(db, fileId, category, name);
      return writes.tagsForFile(db, fileId);
    })
  );
  ipcMain.handle("category:create", (_e, name, color) =>
    withSqliteRetry(() => writes.createCategory(db, name, color))
  );
  ipcMain.handle("category:list", () => writes.listCategories(db));

  // Rich preview data (§8.2): caption, OCR, faces, metadata, thumbnail.
  ipcMain.handle("file:detail", (_e, fileId) => writes.fileDetail(db, fileId));
  // Open the image in the OS default viewer, or reveal it in the file manager.
  ipcMain.handle("file:open", (_e, filePath) => shell.openPath(indexedPath(filePath)));
  ipcMain.handle("file:reveal", (_e, filePath) => shell.showItemInFolder(indexedPath(filePath)));
  ipcMain.handle("clipboard:write", (_e, text) => { clipboard.writeText(String(text)); return true; });
  ipcMain.handle("file:thumb", (_e, fileId) => writes.thumbDataUri(db, fileId));
  // Full-resolution image for the preview-pane lightbox (click-to-enlarge).
  // Loaded on demand only — not part of the search/grid hot path.
  // Return a validated streaming URL. This avoids copying 30-100 MB originals
  // into a much larger base64 IPC payload and works regardless of drive/path.
  ipcMain.handle("file:full", (_e, fileId) => fullImageUrl(db, fileId));
  ipcMain.handle("ocr:set", (_e, fileId, text) =>
    withSqliteRetry(() => {
      writes.setOcrText(db, fileId, text);
      return writes.fileDetail(db, fileId);
    })
  );
  // Re-index a single file (§11 re-caption / refresh): ask the daemon to re-run
  // ingest+infer for just this path.
  ipcMain.handle("file:reindex", (_e, filePath) =>
    indexer.call("reindex_file", { path: indexedPath(filePath) })
  );
  // Regenerate just the caption/description (§11) — narrower than a full
  // reindex, for the preview pane's "↻ re-Description" button.
  ipcMain.handle("file:recaption", (_e, filePath) =>
    indexer.call("recaption_file", { path: indexedPath(filePath) })
  );

  // Settings — e.g. the user-selected models download folder (spec §12).
  ipcMain.handle("settings:get", (_e, key, fallback) =>
    writes.getSetting(db, key, fallback)
  );
  ipcMain.handle("settings:set", (_e, key, value) =>
    withSqliteRetry(() => writes.setSetting(db, key, value))
  );

  // Bulk manual tagging for grid multi-select (spec §9).
  ipcMain.handle("tag:bulkAdd", (_e, fileIds, category, name) =>
    withSqliteRetry(() => writes.bulkAddTag(db, fileIds, category, name))
  );
  ipcMain.handle("tag:bulkRemove", (_e, fileIds, category, name) =>
    withSqliteRetry(() => writes.bulkRemoveTag(db, fileIds, category, name))
  );

  // Python indexer bridge (spec §4): spawn the daemon, forward its progress
  // events to the renderer, and expose a generic control call + semantic search.
  const forward = (channel) => (payload) => {
    if (mainWindow && !mainWindow.isDestroyed())
      mainWindow.webContents.send(channel, payload);
  };
  indexer.on("progress", forward("indexer:progress"));
  indexer.on("download_progress", forward("indexer:downloadProgress"));
  indexer.on("download_done", forward("indexer:downloadDone"));
  indexer.on("exit", (code) => {
    if (mainWindow && !mainWindow.isDestroyed())
      mainWindow.webContents.send("indexer:exit", code);
  });
  // The bridge auto-restarts after an unexpected exit (§7 resilience) — let
  // the renderer show a brief, honest "indexer restarted" notice instead of
  // indexing/search just silently going stale with no explanation.
  indexer.on("restarted", forward("indexer:restarted"));
  // Surface daemon-side trouble that used to vanish silently (§7 resilience):
  // "warning" is a structured {message} event (e.g. a model runtime that
  // failed to preload); "stderr" is the raw stream — Python tracebacks for
  // anything that wasn't caught and turned into a per-job error.
  indexer.on("warning", forward("indexer:warning"));
  indexer.on("stderr", forward("indexer:stderr"));
  // rescan/rescan_root now return immediately ({started}) and run the actual
  // filesystem walk on a background thread in the daemon, so this event
  // carries the real result once it's done (§7 — see _run_scan_async).
  indexer.on("scan_done", forward("indexer:scanDone"));
  ipcMain.handle("indexer:call", (_e, cmd, args) => {
    if (!allowedIndexerCommands.has(cmd)) throw new Error(`Indexer command not allowed: ${cmd}`);
    return indexer.call(cmd, args && typeof args === "object" ? args : {});
  });
  // Native folder picker for adding include/exclude scan roots (§7.0).
  ipcMain.handle("dialog:pickFolder", async () => {
    const r = await dialog.showOpenDialog(mainWindow, {
      properties: ["openDirectory", "multiSelections"],
    });
    return r.canceled ? [] : r.filePaths;
  });
  ipcMain.handle("indexer:semantic", (_e, query, k) =>
    indexer.call("semantic", { query, k: k ?? 20 })
  );

  // This is a packaged single-purpose app with its own in-window nav (Search /
  // Sources / Models / People / Learned tags) — Electron's default File/Edit/
  // View/Window/Help menu bar is just unconfigured boilerplate on top of that,
  // not anything the app uses. F12 still opens DevTools without it.
  Menu.setApplicationMenu(null);

  const iconPath = path.join(__dirname, "../../build/icon.png");
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 720,
    minHeight: 560,
    show: false,
    backgroundColor: "#0b1220",
    ...(fs.existsSync(iconPath) ? { icon: iconPath } : {}),
    webPreferences: {
      preload: path.join(__dirname, "../preload/preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      backgroundThrottling: !packageSmoke,
    },
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  mainWindow.webContents.on("will-navigate", (event) => event.preventDefault());
  mainWindow.once("ready-to-show", () => { if (!packageSmoke) mainWindow?.show(); });
  mainWindow.loadFile(rendererIndex());

  if (packageSmoke) {
    mainWindow.webContents.once("did-finish-load", async () => {
      logPackageSmoke("renderer-finished-load");
      let exitCode = 1;
      try {
        const deadline = Date.now() + 30000;
        let rendererReady = false;
        while (Date.now() < deadline && !rendererReady) {
          rendererReady = await mainWindow.webContents.executeJavaScript(
            "Boolean(document.querySelector('app-root')?.textContent?.includes('Image Tagger'))"
          );
          if (!rendererReady) await delay(100);
        }
        if (!rendererReady) throw new Error("packaged Angular renderer did not become ready");
        logPackageSmoke("renderer-ready");
        const streamedImage = await mainWindow.webContents.executeJavaScript(`(async () => {
          const response = await fetch(${JSON.stringify(fullImageUrl(db, packageSmokeImageId))});
          const bytes = new Uint8Array(await response.arrayBuffer());
          return {
            ok: response.ok,
            contentType: response.headers.get("content-type"),
            byteLength: bytes.byteLength,
            jpeg: bytes[0] === 0xff && bytes[1] === 0xd8 &&
              bytes[bytes.length - 2] === 0xff && bytes[bytes.length - 1] === 0xd9,
          };
        })()`);
        if (!streamedImage.ok || !streamedImage.jpeg || streamedImage.byteLength < 1024) {
          throw new Error("packaged full-image streaming failed");
        }
        logPackageSmoke(`image-stream=${streamedImage.byteLength}:${streamedImage.contentType}`);
        const ping = await indexer.call("ping", {});
        if (ping !== "pong") throw new Error("packaged indexer ping failed");
        logPackageSmoke("indexer-pong");
        fs.writeFileSync(
          path.join(process.env.IMAGE_TAGGER_HOME, "package-smoke-ok"),
          "renderer=ready\nindexer=pong\n",
          "utf8"
        );
        exitCode = 0;
      } catch (error) {
        logPackageSmoke(`failure=${String(error?.stack || error)}`);
        console.error("Packaged app smoke failed:", error);
      } finally {
        if (indexer) await indexer.stop();
        app.exit(exitCode);
      }
    });
  }
}).catch((error) => {
  console.error(error);
  dialog.showErrorBox("Image Tagger could not start", String(error?.stack || error));
  app.quit();
});

app.on("before-quit", async () => {
  if (indexer) await indexer.stop();
});
app.on("window-all-closed", () => app.quit());
