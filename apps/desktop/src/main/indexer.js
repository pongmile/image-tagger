// Electron↔Python bridge (spec §4 control channel). Spawns the indexer daemon
// once and speaks line-delimited JSON over stdio: each call gets an id and
// resolves when the matching response line arrives. Unsolicited {event:...}
// lines (progress in --auto mode) are re-emitted as events.
const { spawn } = require("child_process");
const { EventEmitter } = require("events");
const os = require("os");
const path = require("path");
const fs = require("fs");

function venvPython(indexerDir) {
  const win = path.join(indexerDir, ".venv", "Scripts", "python.exe");
  const nix = path.join(indexerDir, ".venv", "bin", "python");
  const bundledWin = process.resourcesPath
    ? path.join(process.resourcesPath, "python", "python.exe")
    : null;
  const bundledNix = process.resourcesPath
    ? path.join(process.resourcesPath, "python", "bin", "python3")
    : null;
  if (process.env.IMAGE_TAGGER_PYTHON) return process.env.IMAGE_TAGGER_PYTHON;
  if (fs.existsSync(win)) return win;
  if (fs.existsSync(nix)) return nix;
  if (bundledWin && fs.existsSync(bundledWin)) return bundledWin;
  if (bundledNix && fs.existsSync(bundledNix)) return bundledNix;
  return os.platform() === "win32" ? "python" : "python3"; // last resort
}

// Resilience tuning (§7 "survive crashes and recover"): a hung worker loop
// (e.g. a corrupt image wedging a native decode/inference call) keeps
// answering RPCs on the main thread fine — only its own heartbeat timestamp
// stalls — so periodic RPCs alone can't detect it. HEARTBEAT_STALE_MS must
// stay well above worst-case legitimate per-file time (cold model load +
// 5 facets); worker.py also pets the clock at each facet boundary, not just
// once per file, so this only needs to cover one facet's worst case.
const HEARTBEAT_POLL_MS = 20_000;
const HEARTBEAT_STALE_MS = 120_000;
const RESTART_DELAY_MS = 1_000;
// The daemon answers *no* RPCs — not even "heartbeat" — until it's done its
// own synchronous startup (preloading every enabled model's runtime, which
// can legitimately take well over a poll interval). A single missed poll can
// also just mean a slow-but-legitimate synchronous command is in flight
// (e.g. rescanning thousands of files on a slow/network drive) — the daemon
// has one stdin-reader thread, so it can't answer "heartbeat" concurrently.
// Require several consecutive misses before concluding it's truly wedged.
const HEARTBEAT_MAX_FAILURES = 3;

class IndexerBridge extends EventEmitter {
  constructor(opts = {}) {
    super();
    this.indexerDir =
      opts.indexerDir || path.join(__dirname, "../../../indexer");
    this.auto = opts.auto !== false;
    this.seq = 0;
    this.pending = new Map();
    this.buf = "";
    this.proc = null;
    this.stopping = false;
    this.heartbeatTimer = null;
  }

  start() {
    this.stopping = false;
    const py = venvPython(this.indexerDir);
    const args = ["-m", "indexer.daemon", ...(this.auto ? ["--auto"] : [])];
    const child = spawn(py, args, {
      cwd: this.indexerDir,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc = child;
    child.stdout.on("data", (d) => this._onData(d));
    child.stderr.on("data", (d) =>
      this.emit("stderr", d.toString())
    );
    child.on("error", (error) => this.emit("stderr", `indexer spawn failed: ${error.message}\n`));
    child.stdin.on("error", (error) => {
      if (error.code !== "EPIPE") this.emit("stderr", `indexer stdin failed: ${error.message}\n`);
    });
    child.on("exit", (code) => {
      if (this.proc === child) this.proc = null;
      this._stopHeartbeat();
      for (const { reject } of this.pending.values())
        reject(new Error(`indexer daemon exited (${code})`));
      this.pending.clear();
      this.emit("exit", code);
      // A deliberate stop() never auto-restarts. Anything else — a crash, an
      // OOM-kill, or this same class killing a hung process below — does,
      // so a dead/wedged daemon recovers within the session instead of
      // silently leaving search/indexing broken until the user relaunches
      // the whole app. The daemon's own recover_interrupted_jobs() (run at
      // its startup) then requeues whatever job was stuck mid-flight.
      if (!this.stopping) {
        setTimeout(() => {
          if (this.stopping) return;
          this.start();
          this.emit("restarted", { previousExitCode: code });
        }, RESTART_DELAY_MS);
      }
    });
    if (this.auto) {
      // Don't poll for liveness until the daemon says it's actually up —
      // otherwise its own (possibly slow) startup looks identical to a hang
      // and gets killed before it ever gets a chance to run.
      this.once("ready", () => {
        if (this.proc && !this.stopping) this._startHeartbeat();
      });
    }
    return this;
  }

  _startHeartbeat() {
    this._stopHeartbeat();
    this._heartbeatFailures = 0;
    this.heartbeatTimer = setInterval(async () => {
      if (!this.proc) return;
      try {
        const r = await this.call("heartbeat", {}, { timeout: 10_000 });
        this._heartbeatFailures = 0;
        if (r && typeof r.worker_age === "number" && r.worker_age * 1000 > HEARTBEAT_STALE_MS) {
          this.emit("stderr", `indexer worker loop stalled for ${Math.round(r.worker_age)}s — restarting\n`);
          this.proc.kill(); // triggers the "exit" handler's auto-restart above
        }
      } catch {
        // RPC itself timed out/failed. This alone doesn't mean the process is
        // wedged — it could just be busy on a slow synchronous command — so
        // only act after several consecutive misses (~1 minute of total
        // unresponsiveness), which a merely-busy daemon should never hit.
        this._heartbeatFailures++;
        if (this._heartbeatFailures >= HEARTBEAT_MAX_FAILURES && this.proc) {
          this.emit("stderr", `indexer unresponsive for ${this._heartbeatFailures} heartbeat checks — restarting\n`);
          this.proc.kill();
        }
      }
    }, HEARTBEAT_POLL_MS);
  }

  _stopHeartbeat() {
    if (this.heartbeatTimer) { clearInterval(this.heartbeatTimer); this.heartbeatTimer = null; }
  }

  _onData(chunk) {
    this.buf += chunk.toString();
    let nl;
    while ((nl = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, nl).trim();
      this.buf = this.buf.slice(nl + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      if (msg.event) { this.emit(msg.event, msg); this.emit("event", msg); continue; }
      const p = this.pending.get(msg.id);
      if (p) {
        this.pending.delete(msg.id);
        msg.ok ? p.resolve(msg.result) : p.reject(new Error(msg.error));
      }
    }
  }

  call(cmd, args = {}, { timeout = 120000 } = {}) {
    const child = this.proc;
    if (!child || child.killed || child.exitCode != null || !child.stdin.writable) {
      throw new Error("indexer daemon not started");
    }
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`indexer call '${cmd}' timed out`));
      }, timeout);
      this.pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      child.stdin.write(JSON.stringify({ id, cmd, ...args }) + "\n", (error) => {
        if (!error) return;
        this.pending.delete(id);
        clearTimeout(timer);
        reject(new Error(`indexer call '${cmd}' could not be sent: ${error.message}`));
      });
    });
  }

  async stop() {
    this.stopping = true;
    this._stopHeartbeat();
    if (!this.proc) return;
    try { await this.call("stop", {}, { timeout: 3000 }); } catch { /* ignore */ }
    const child = this.proc;
    if (child) {
      child.stdin.end();
      child.kill();
      if (this.proc === child) this.proc = null;
    }
  }
}

module.exports = { IndexerBridge, venvPython };
