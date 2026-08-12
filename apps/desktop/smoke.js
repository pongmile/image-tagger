// Electron main-process smoke test (no BrowserWindow, so it runs without a
// display). Proves the real GUI stack loads under Electron's runtime:
//   - better-sqlite3 (Electron-ABI prebuilt) opens the DB + FTS read/write path
//   - the Python indexer daemon spawns and answers over stdio
// Exits 0 on success, 1 on failure. Not shipped — a CI/dev gate.
const { app } = require("electron");
const path = require("path");
const os = require("os");
const fs = require("fs");

app.disableHardwareAcceleration();

const HOME = path.join(os.tmpdir(), "imgtag_electron_smoke");
fs.rmSync(HOME, { recursive: true, force: true });
process.env.IMAGE_TAGGER_HOME = HOME;
app.setPath("userData", path.join(HOME, "electron-profile"));

const { openLibrary, search } = require("./src/main/search");
const writes = require("./src/main/writes");
const { IndexerBridge } = require("./src/main/indexer");

let failed = 0;
const check = (c, m) => { console.log((c ? "  ok  " : " FAIL ") + m); if (!c) failed++; };

app.whenReady().then(async () => {
  try {
    // 1) Spawn the daemon first so it creates the schema (db.connect runs it).
    const bridge = new IndexerBridge({
      indexerDir: path.join(__dirname, "../indexer"), auto: false,
    });
    const ready = new Promise((r) => bridge.once("ready", r));
    bridge.start();
    await ready;
    check(true, "daemon spawned under Electron (ready)");
    check((await bridge.call("ping")) === "pong", "bridge ping -> pong");
    const prog = await bridge.call("progress");
    check(typeof prog.files_total === "number", "bridge progress query works");

    // 2) better-sqlite3 (Electron prebuilt) — write + FTS read on the same DB.
    const db = openLibrary();
    const fid = db.prepare(
      "INSERT INTO files (path,filename,folder,sha256,index_status) VALUES (?,?,?,?, 'done')"
    ).run("D:/pics/miku.png", "miku.png", "D:/pics", "sha").lastInsertRowid;
    writes.refreshFts(db, fid);
    writes.addManualTag(db, fid, "character", "hatsune miku");
    const rows = search(db, "miku");
    check(rows.length === 1 && rows[0].filename === "miku.png",
      `better-sqlite3 FTS search under Electron returns the row (${rows.length})`);
    check(search(db, "").length === 1,
      "empty search browses the indexed library");
    check((await bridge.call("progress")).files_total === 1,
      "daemon sees the row Node wrote (shared DB / WAL)");

    await bridge.stop();
    console.log("");
    console.log(failed ? `RESULT: FAIL — ${failed}` : "RESULT: PASS — Electron GUI stack (better-sqlite3 + daemon) verified");
  } catch (e) {
    console.error("SMOKE ERROR:", e);
    failed = 1;
  } finally {
    app.exit(failed ? 1 : 0);
  }
});
