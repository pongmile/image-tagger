// Read-side search over library.db — spec §8 (the "Everything" bar).
// Synchronous better-sqlite3 = the Everything-like hot path. Verified: 500k
// rows, p95 < 1ms (scripts/bench_search.py). buildQuery() is a pure function
// (driver-independent) so the query grammar is unit-testable on its own.
//
// Grammar (mirrors voidtools' Everything, §8.1):
//   space          AND (default; implicit between adjacent terms)
//   |              OR
//   ! or -         NOT (prefix, no space before the term)
//   < >            grouping, e.g. <cat|dog> outdoor
//   "exact phrase" literal phrase, no wildcard/regex expansion
//   * ?            wildcards (any-chars / one-char) in a bare term
//   size:N[kb|mb|gb], size:>N.., size:<N.., size:A-B  file-size filter
//   tag:/folder:/person:/<category>:  structured filters, unchanged from v1
// A `regex: true` option switches every free-text term to a JS-RegExp test
// (§8.1's regex checkbox) instead of FTS/wildcard matching.
const Database = require("better-sqlite3");
const os = require("os");
const path = require("path");

function openLibrary(dbPath) {
  const home = process.env.IMAGE_TAGGER_HOME;
  const p =
    dbPath ||
    (home
      ? path.join(home, "library.db")
      : path.join(os.homedir(), ".image-tagger", "library.db"));
  const db = new Database(p, { readonly: false, fileMustExist: false });
  db.pragma("journal_mode = WAL");
  // The Python indexer is a second WAL writer. A large model can finish and
  // commit at the same moment as a manual edit, so give user-driven writes a
  // short acquisition window; main.js retries SQLITE_BUSY asynchronously for
  // up to 30 seconds so the Electron event loop gets a chance to breathe.
  db.pragma("busy_timeout = 2000");
  // Everything-style match modes (§8.1). FTS gives the fast candidate set
  // (substring, case/diacritic-insensitive); imatch() is the precise post-filter
  // the UI toggles turn on: case-sensitive, whole-word, diacritic-sensitive.
  db.function(
    "imatch",
    { deterministic: true },
    (text, needle, mcase, mword, mdia) => {
      if (text == null) return 0;
      let t = String(text);
      let n = String(needle);
      if (!mcase) { t = t.toLowerCase(); n = n.toLowerCase(); }
      if (!mdia) { t = foldDiacritics(t); n = foldDiacritics(n); }
      if (!n) return 1;
      if (mword) {
        const esc = n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        try {
          return new RegExp(`(^|[^\\p{L}\\p{N}])${esc}([^\\p{L}\\p{N}]|$)`, "u").test(t) ? 1 : 0;
        } catch {
          return t.includes(n) ? 1 : 0;
        }
      }
      return t.includes(n) ? 1 : 0;
    }
  );
  // Regex mode (§8.1 checkbox, default off): the user's term is a JS regex
  // tested against the same haystack strict-mode uses. Deliberately returns 0
  // instead of throwing on a bad pattern here — buildQuery() validates every
  // pattern up front and throws a clear error before this ever runs, so a 0
  // here should be unreachable in practice, not a silent swallow.
  db.function("rmatch", { deterministic: true }, (text, pattern, mcase) => {
    if (text == null) return 0;
    try {
      return new RegExp(pattern, mcase ? "u" : "iu").test(String(text)) ? 1 : 0;
    } catch {
      return 0;
    }
  });
  return db;
}

// Strip combining marks (Latin accents) + Thai tone/vowel marks so "cafe"
// matches "café" and Thai text matches regardless of diacritics, unless the
// user turns Match Diacritics on.
function foldDiacritics(s) {
  return s
    .normalize("NFD")
    .replace(/[̀-ͯัิ-ฺ็-๎]/g, "");
}

// --- tokenizer ---------------------------------------------------------
// Scans left to right; quoted phrases absorb embedded spaces/operators so
// `tag:"hatsune miku"` and `"a > b"` stay intact. `<`/`>`/`|` are only
// operators when they appear as a token boundary (start/end of a bare
// term) — a field:value token (folder:, size:, ...) runs to the next
// whitespace so `size:>1mb` stays one token, not grouping-close + `1mb`.
function scanTokens(query) {
  const toks = [];
  let i = 0;
  const n = query.length;
  const isWs = (c) => c === " " || c === "\t" || c === "\n" || c === "\r";
  while (i < n) {
    const c = query[i];
    if (isWs(c)) { i++; continue; }
    if (c === "<") { toks.push({ type: "gopen" }); i++; continue; }
    if (c === ">") { toks.push({ type: "gclose" }); i++; continue; }
    if (c === "|") { toks.push({ type: "or" }); i++; continue; }
    if (c === "!" || c === "-") {
      // A standalone NOT token (not merged into the term token) so it can
      // negate either a single term OR a whole <group> that follows it.
      const nextC = query[i + 1];
      if (nextC === undefined || isWs(nextC) || nextC === ">" || nextC === "|") { i++; continue; } // dangling
      toks.push({ type: "not" });
      i++;
      continue;
    }
    // field: prefix (letters/digits/dashes, then a colon)
    let field = null;
    const rest = query.slice(i);
    const identMatch = /^[a-zA-Z][\w-]*/.exec(rest);
    if (identMatch && query[i + identMatch[0].length] === ":") {
      field = identMatch[0].toLowerCase();
      i += identMatch[0].length + 1;
    }
    if (i < n && query[i] === '"') {
      i++;
      const s = i;
      while (i < n && query[i] !== '"') i++;
      const value = query.slice(s, i);
      if (i < n) i++; // closing quote
      toks.push({ type: "term", field, value, phrase: true });
    } else if (field) {
      // Field values run to the next whitespace only — <>| inside are literal
      // (size:>1mb, folder:<weird> would be pathological on Windows anyway).
      const s = i;
      while (i < n && !isWs(query[i])) i++;
      const value = query.slice(s, i);
      if (value) toks.push({ type: "term", field, value, phrase: false });
    } else {
      const s = i;
      while (i < n && !isWs(query[i]) && query[i] !== "<" && query[i] !== ">" && query[i] !== "|") i++;
      const value = query.slice(s, i);
      if (value) toks.push({ type: "term", field: null, value, phrase: false });
    }
  }
  return toks;
}

// --- parser: tokens -> boolean AST ---------------------------------------
// or := and ('|' and)*        and := atom+ (implicit AND)
// atom := '<' or '>' | term   term := ('!'|'-')? leaf
class Parser {
  constructor(tokens) { this.toks = tokens; this.pos = 0; }
  peek() { return this.toks[this.pos]; }
  next() { return this.toks[this.pos++]; }
  parseOr() {
    const parts = [this.parseAnd()].filter(Boolean);
    while (this.peek() && this.peek().type === "or") {
      this.next();
      const n = this.parseAnd();
      if (n) parts.push(n);
    }
    if (parts.length === 0) return null;
    return parts.length === 1 ? parts[0] : { type: "or", nodes: parts };
  }
  parseAnd() {
    const parts = [];
    while (this.peek() && this.peek().type !== "or" && this.peek().type !== "gclose") {
      const before = this.pos;
      const n = this.parseAtom();
      if (n) parts.push(n);
      if (this.pos === before) this.pos++; // safety: always make progress
    }
    if (parts.length === 0) return null;
    return parts.length === 1 ? parts[0] : { type: "and", nodes: parts };
  }
  parseAtom() {
    const t = this.peek();
    if (!t) return null;
    if (t.type === "not") {
      this.next();
      const inner = this.parseAtom(); // may itself be a <group> or a plain term
      return inner ? { type: "not", node: inner } : null;
    }
    if (t.type === "gopen") {
      this.next();
      const inner = this.parseOr();
      if (this.peek() && this.peek().type === "gclose") this.next();
      return inner;
    }
    if (t.type === "gclose") { this.next(); return null; } // stray '>' — drop
    if (t.type === "or") { this.next(); return null; } // stray '|' — drop
    this.next(); // 'term'
    return { type: "leaf", field: t.field, value: t.value, phrase: t.phrase };
  }
}

function parseQuery(query) {
  return new Parser(scanTokens(query || "")).parseOr();
}

const SORTS = {
  name: "f.filename",
  path: "f.path",
  size: "f.size_bytes",
  dim: "f.width * f.height",
  date: "f.mtime",
  mtime: "f.mtime",
  kind: "f.image_kind",
};

function normFolder(p) {
  return p.replace(/\\/g, "/").replace(/\/+$/, "");
}

const SIZE_UNIT = { b: 1, kb: 1024, mb: 1024 ** 2, gb: 1024 ** 3 };

// size:N[kb|mb|gb] (default >=), size:>N.., size:<N.., size:<=N.., size:A-B
function compileSize(raw) {
  const v = raw.trim();
  const range = /^([\d.]+)\s*(b|kb|mb|gb)?\s*(?:-|\.\.)\s*([\d.]+)\s*(b|kb|mb|gb)?$/i.exec(v);
  if (range) {
    const loUnit = (range[2] || "b").toLowerCase();
    const hiUnit = (range[4] || range[2] || "b").toLowerCase();
    const lo = parseFloat(range[1]) * SIZE_UNIT[loUnit];
    const hi = parseFloat(range[3]) * SIZE_UNIT[hiUnit];
    return { sql: "f.size_bytes BETWEEN ? AND ?", params: [Math.min(lo, hi), Math.max(lo, hi)] };
  }
  const cmp = /^(<=|>=|<|>)?\s*([\d.]+)\s*(b|kb|mb|gb)?$/i.exec(v);
  if (cmp) {
    const op = cmp[1] || ">=";
    const bytes = parseFloat(cmp[2]) * SIZE_UNIT[(cmp[3] || "b").toLowerCase()];
    return { sql: `f.size_bytes ${op} ?`, params: [bytes] };
  }
  // Unparseable size filter: match nothing rather than silently ignoring it
  // (a typo'd size: term should not quietly widen the search to "everything").
  return { sql: "0=1", params: [] };
}

function ftsPhrase(value) {
  return `"${value.replace(/"/g, '""')}"`;
}

// Compiles one leaf token into a {sql, params} WHERE fragment. `ctx` carries
// shared query options plus a mutable `usesScope` flag: wildcard/regex/strict
// leaves all read the concatenated-columns `scope` expression, which requires
// LEFT JOIN files_fts ff — the caller only pays for that join when needed.
function compileLeaf(tok, ctx) {
  const { field, value, phrase } = tok;
  const { flags, scope, strict, hasConfFloor, minConfidence, matchCase, regex } = ctx;

  if (field === "size") return compileSize(value);
  if (field === "folder") {
    const like = normFolder(value) + "/%";
    const eq = normFolder(value);
    return {
      sql: "(REPLACE(f.folder,'\\','/') = ? OR REPLACE(f.folder,'\\','/') LIKE ? )",
      params: [eq, like],
    };
  }
  if (field === "person") {
    return {
      sql: `EXISTS (SELECT 1 FROM faces fa JOIN persons p ON p.id=fa.person_id
                     WHERE fa.file_id=f.id AND p.name=? COLLATE NOCASE)`,
      params: [value],
    };
  }
  if (field) {
    // 'tag' = any category; any other field name = that category (character:, general:, ...)
    const isTagField = field === "tag";
    const sql =
      `EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id=ft.tag_id` +
      (isTagField ? "" : " JOIN categories cc ON cc.id=t.category_id") +
      ` WHERE ft.file_id=f.id` +
      (isTagField ? "" : " AND lower(cc.name)=lower(?)") +
      ` AND instr(lower(t.name), lower(?)) > 0` +
      (hasConfFloor ? ` AND (ft.confidence IS NULL OR ft.confidence >= ?)` : ``) +
      `)`;
    const params = [];
    if (!isTagField) params.push(field);
    params.push(value);
    if (hasConfFloor) params.push(Number(minConfidence));
    return { sql, params };
  }

  // Free text (bareword or "phrase").
  if (regex) {
    ctx.usesScope = true;
    return { sql: `rmatch(${scope}, ?, ${matchCase ? 1 : 0}) = 1`, params: [value] };
  }
  if (!phrase && (value.includes("*") || value.includes("?"))) {
    ctx.usesScope = true;
    // SQLite GLOB already uses '*'/'?' verbatim — the same wildcard syntax
    // Everything uses — so no character translation is needed either way.
    // GLOB matches the *whole* string though, and `scope` concatenates
    // several columns together, so an unanchored pattern (no leading/
    // trailing '*') is auto-wrapped to "found anywhere in scope" — matching
    // every other free-text term in this grammar, which is substring-based.
    let pat = value;
    if (!pat.startsWith("*")) pat = "*" + pat;
    if (!pat.endsWith("*")) pat = pat + "*";
    return matchCase
      ? { sql: `${scope} GLOB ?`, params: [pat] }
      : { sql: `LOWER(${scope}) GLOB LOWER(?)`, params: [pat] };
  }
  const ftsCond = `f.id IN (SELECT rowid FROM files_fts WHERE files_fts MATCH ?)`;
  if (strict) {
    ctx.usesScope = true;
    return {
      sql: `(${ftsCond} AND imatch(${scope}, ?, ${flags}) = 1)`,
      params: [ftsPhrase(value), value],
    };
  }
  return { sql: ftsCond, params: [ftsPhrase(value)] };
}

// Recursively compiles an AST node. NOT is a true logical negation of its
// child (via SQL NOT (...)), which composes correctly once NOT can nest
// inside OR/grouping — unlike a hand-rolled asymmetric exclude rule, this
// stays correct for arbitrarily nested `!<a|b> c` expressions.
function compileNode(node, ctx) {
  if (node.type === "leaf") return compileLeaf(node, ctx);
  if (node.type === "not") {
    const inner = compileNode(node.node, ctx);
    return { sql: `NOT (${inner.sql})`, params: inner.params };
  }
  const parts = node.nodes.map((n) => compileNode(n, ctx));
  const joiner = node.type === "and" ? " AND " : " OR ";
  return {
    sql: "(" + parts.map((p) => p.sql).join(joiner) + ")",
    params: [].concat(...parts.map((p) => p.params)),
  };
}

// Walks the AST collecting every free-text/regex/wildcard leaf's raw pattern
// so buildQuery() can validate regex syntax up front (§8.1: a bad pattern
// should surface as one clear error, not silently match nothing).
function collectRegexPatterns(node, out) {
  if (!node) return;
  if (node.type === "leaf") { if (!node.field) out.push(node.value); return; }
  if (node.type === "not") return collectRegexPatterns(node.node, out);
  for (const n of node.nodes) collectRegexPatterns(n, out);
}

function buildQuery(
  query,
  {
    limit = 200, sort, dir = "asc",
    matchCase = false, wholeWord = true, matchPath = false, matchDiacritics = false,
    minConfidence = null, mediaType = "image", regex = false,
  } = {}
) {
  const ast = parseQuery(query || "");
  const where = [];
  const params = [];

  if (regex) {
    const patterns = [];
    collectRegexPatterns(ast, patterns);
    for (const p of patterns) {
      try { new RegExp(p, matchCase ? "u" : "iu"); }
      catch (e) { throw new Error(`Invalid regex "${p}": ${e.message}`); }
    }
  }

  const strict = matchCase || wholeWord || matchPath || matchDiacritics;
  const flags = `${matchCase ? 1 : 0}, ${wholeWord ? 1 : 0}, ${matchDiacritics ? 1 : 0}`;
  const scope = matchPath
    ? "ff.path"
    : "(COALESCE(ff.filename,'')||' '||COALESCE(ff.folder,'')||' '||" +
      "COALESCE(ff.tags_text,'')||' '||COALESCE(ff.ocr_text,'')||' '||" +
      "COALESCE(ff.caption,'')||' '||COALESCE(ff.path,''))";
  // minConfidence (§5.3 UX): hide auto-tag matches below this floor from
  // structured cat: filters. Manual tags (confidence=NULL) are never gated —
  // matches the same rule applied to the preview pane's tag list (writes.js
  // tagsForFile). Free-text/FTS matching is untouched (tags_text has no
  // per-token confidence; keeps the fast path unchanged).
  const hasConfFloor = minConfidence != null && minConfidence !== "";
  const ctx = { flags, scope, strict, hasConfFloor, minConfidence, matchCase, regex, usesScope: false };

  if (ast) {
    const compiled = compileNode(ast, ctx);
    where.push(compiled.sql);
    params.push(...compiled.params);
  }

  // Media-type filter (§12): videos are browse/search-only (no AI facets), so
  // they default OFF rather than silently mixing into an image search the
  // user didn't ask to widen. mime is NULL-tolerant on the image side so
  // rows ingested before this column was populated don't vanish from the
  // default view.
  if (mediaType === "image") {
    where.push("(f.mime IS NULL OR f.mime LIKE 'image/%')");
  } else if (mediaType === "video") {
    where.push("f.mime LIKE 'video/%'");
  }

  const needsFtsJoin = strict || ctx.usesScope;
  const sql =
    `SELECT f.id, f.path, f.filename, f.folder, f.image_kind, f.sha256,
            f.width, f.height, f.size_bytes, f.mtime, f.mime
       FROM files f` +
    (needsFtsJoin ? `\n       LEFT JOIN files_fts ff ON ff.rowid = f.id` : "") +
    (where.length ? `\n      WHERE ${where.join("\n        AND ")}` : "") +
    (SORTS[sort] ? `\n      ORDER BY ${SORTS[sort]} ${dir === "desc" ? "DESC" : "ASC"}` : "") +
    `\n      LIMIT ?`;
  params.push(limit);
  return { sql, params };
}

function search(db, query, opts = {}) {
  const { sql, params } = buildQuery(query, opts);
  return db.prepare(sql).all(...params);
}

function countMatches(db, query, opts = {}) {
  const { sql, params } = buildQuery(query, { ...opts, limit: -1 });
  const inner = sql.replace(/LIMIT \?$/, "");
  params.pop(); // drop the LIMIT param
  return db.prepare(`SELECT count(*) c FROM (${inner})`).get(...params).c;
}

module.exports = { openLibrary, search, countMatches, buildQuery, parseQuery };
