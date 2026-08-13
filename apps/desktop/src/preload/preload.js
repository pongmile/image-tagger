const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("api", {
  pickFolder: () => ipcRenderer.invoke("dialog:pickFolder"),
  search: (q, opts) => ipcRenderer.invoke("search", q, opts),
  count: (q, opts) => ipcRenderer.invoke("count", q, opts),
  tags: (fileId, minConfidence) => ipcRenderer.invoke("tags", fileId, minConfidence),
  addTag: (fileId, category, name) =>
    ipcRenderer.invoke("tag:add", fileId, category, name),
  removeTag: (fileId, category, name) =>
    ipcRenderer.invoke("tag:remove", fileId, category, name),
  createCategory: (name, color) =>
    ipcRenderer.invoke("category:create", name, color),
  listCategories: () => ipcRenderer.invoke("category:list"),
  fileDetail: (fileId) => ipcRenderer.invoke("file:detail", fileId),
  thumb: (fileId) => ipcRenderer.invoke("file:thumb", fileId),
  fullImage: (fileId) => ipcRenderer.invoke("file:full", fileId),
  setOcr: (fileId, text) => ipcRenderer.invoke("ocr:set", fileId, text),
  reindexFile: (filePath) => ipcRenderer.invoke("file:reindex", filePath),
  recaptionFile: (filePath) => ipcRenderer.invoke("file:recaption", filePath),
  retagFile: (filePath) => ipcRenderer.invoke("file:retag", filePath),
  onFileDone: (cb) => {
    const h = (_e, p) => cb(p);
    ipcRenderer.on("indexer:fileDone", h);
    return () => ipcRenderer.removeListener("indexer:fileDone", h);
  },
  openFile: (filePath) => ipcRenderer.invoke("file:open", filePath),
  revealFile: (filePath) => ipcRenderer.invoke("file:reveal", filePath),
  copyText: (text) => ipcRenderer.invoke("clipboard:write", text),
  getSetting: (key, fallback) => ipcRenderer.invoke("settings:get", key, fallback),
  setSetting: (key, value) => ipcRenderer.invoke("settings:set", key, value),
  bulkAddTag: (fileIds, category, name) =>
    ipcRenderer.invoke("tag:bulkAdd", fileIds, category, name),
  bulkRemoveTag: (fileIds, category, name) =>
    ipcRenderer.invoke("tag:bulkRemove", fileIds, category, name),

  // Update check (convenience + install/update-bug avoidance, see updater.js):
  // reports whether the latest GitHub Release is newer than this build, and
  // opens the official release page for the user to download/run themselves.
  getAppVersion: () => ipcRenderer.invoke("app:getVersion"),
  checkForUpdates: (force) => ipcRenderer.invoke("app:checkForUpdates", { force }),
  openReleasePage: (url) => ipcRenderer.invoke("app:openReleasePage", url),

  // Python indexer control (spec §4) + semantic search (§8) + live progress.
  indexer: {
    semantic: (query, k) => ipcRenderer.invoke("indexer:semantic", query, k),
    rescan: () => ipcRenderer.invoke("indexer:call", "rescan", {}),
    rescanRoot: (rootId) =>
      ipcRenderer.invoke("indexer:call", "rescan_root", { root_id: rootId }),
    addRoot: (p, mode) =>
      ipcRenderer.invoke("indexer:call", "add_root", { path: p, mode }),
    progress: () => ipcRenderer.invoke("indexer:call", "progress", {}),
    pause: () => ipcRenderer.invoke("indexer:call", "pause", {}),
    resume: () => ipcRenderer.invoke("indexer:call", "resume", {}),
    setMode: (mode) => ipcRenderer.invoke("indexer:call", "set_mode", { mode }),
    retryErrors: (fileId) =>
      ipcRenderer.invoke("indexer:call", "retry_errors", { file_id: fileId }),
    reindexAll: () => ipcRenderer.invoke("indexer:call", "reindex_all", {}),
    reindexRoot: (rootId) =>
      ipcRenderer.invoke("indexer:call", "reindex_root", { root_id: rootId }),
    recaptionRoot: (rootId) =>
      ipcRenderer.invoke("indexer:call", "recaption_root", { root_id: rootId }),
    recaptionAll: () => ipcRenderer.invoke("indexer:call", "recaption_all", {}),
    listErrors: (rootId, limit) =>
      ipcRenderer.invoke("indexer:call", "list_errors", { root_id: rootId, limit }),
    // Scan scope: include/exclude roots + exclude patterns (§7.0)
    roots: () => ipcRenderer.invoke("indexer:call", "roots", {}),
    addExclude: (p) =>
      ipcRenderer.invoke("indexer:call", "add_root", { path: p, mode: "exclude" }),
    removeRoot: (rootId) =>
      ipcRenderer.invoke("indexer:call", "remove_root", { root_id: rootId }),
    toggleRoot: (rootId, enabled) =>
      ipcRenderer.invoke("indexer:call", "toggle_root", { root_id: rootId, enabled }),
    addExcludePattern: (pattern) =>
      ipcRenderer.invoke("indexer:call", "add_exclude_pattern", { pattern }),
    removeExclude: (ruleId) =>
      ipcRenderer.invoke("indexer:call", "remove_exclude", { rule_id: ruleId }),
    toggleExclude: (ruleId, enabled) =>
      ipcRenderer.invoke("indexer:call", "toggle_exclude", { rule_id: ruleId, enabled }),
    // Few-shot learned tags (§5.3)
    renameTag: (category, oldName, newName) =>
      ipcRenderer.invoke("indexer:call", "rename_tag", { category, old: oldName, new: newName }),
    listTags: () => ipcRenderer.invoke("indexer:call", "list_tags", {}),
    learnStatus: (category, name) =>
      ipcRenderer.invoke("indexer:call", "learn_status", { category, name }),
    learn: (category, name, space) =>
      ipcRenderer.invoke("indexer:call", "learn", { category, name, space }),
    learnConfirm: (category, name, fileId) =>
      ipcRenderer.invoke("indexer:call", "learn_confirm", { category, name, file_id: fileId }),
    learnReject: (category, name, fileId) =>
      ipcRenderer.invoke("indexer:call", "learn_reject", { category, name, file_id: fileId }),
    learnForget: (category, name) =>
      ipcRenderer.invoke("indexer:call", "learn_forget", { category, name }),
    rejectAutoTag: (category, name, fileId, source) =>
      ipcRenderer.invoke("indexer:call", "reject_tag", { category, name, file_id: fileId, source }),
    confirmAutoTag: (category, name, fileId) =>
      ipcRenderer.invoke("indexer:call", "confirm_tag", { category, name, file_id: fileId }),
    listLearnedTags: () => ipcRenderer.invoke("indexer:call", "list_learned_tags", {}),
    download: (model, variant) => ipcRenderer.invoke("indexer:call", "download", { model, variant }),
    installDependency: (facet) =>
      ipcRenderer.invoke("indexer:call", "install_dependency", { facet }),
    downloadStatus: () => ipcRenderer.invoke("indexer:call", "download_status", {}),
    facets: () => ipcRenderer.invoke("indexer:call", "facets", {}),
    setFacetEnabled: (facet, enabled) =>
      ipcRenderer.invoke("indexer:call", "set_facet_enabled", { facet, enabled }),
    modelsDir: () => ipcRenderer.invoke("indexer:call", "models_dir", {}),
    variants: () => ipcRenderer.invoke("indexer:call", "variants", {}),
    modelState: () => ipcRenderer.invoke("indexer:call", "model_state", {}),
    setVariant: (facet, variant) =>
      ipcRenderer.invoke("indexer:call", "set_variant", { facet, variant }),
    persons: () => ipcRenderer.invoke("indexer:call", "persons", {}),
    personFiles: (id) =>
      ipcRenderer.invoke("indexer:call", "person_files", { person_id: id }),
    namePerson: (id, name) =>
      ipcRenderer.invoke("indexer:call", "name_person", { person_id: id, name }),
    mergePersons: (src, dst) =>
      ipcRenderer.invoke("indexer:call", "merge_persons", { src, dst }),
    onProgress: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("indexer:progress", h);
      return () => ipcRenderer.removeListener("indexer:progress", h);
    },
    onDownloadProgress: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("indexer:downloadProgress", h);
      return () => ipcRenderer.removeListener("indexer:downloadProgress", h);
    },
    onDownloadDone: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("indexer:downloadDone", h);
      return () => ipcRenderer.removeListener("indexer:downloadDone", h);
    },
    onRestarted: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("indexer:restarted", h);
      return () => ipcRenderer.removeListener("indexer:restarted", h);
    },
    onWarning: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("indexer:warning", h);
      return () => ipcRenderer.removeListener("indexer:warning", h);
    },
    onStderr: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("indexer:stderr", h);
      return () => ipcRenderer.removeListener("indexer:stderr", h);
    },
    onScanDone: (cb) => {
      const h = (_e, p) => cb(p);
      ipcRenderer.on("indexer:scanDone", h);
      return () => ipcRenderer.removeListener("indexer:scanDone", h);
    },
  },
});
