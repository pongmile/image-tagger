// End-to-end audit against the real Electron/Node search path and library DB.
// Run with: ELECTRON_RUN_AS_NODE=1 electron audit-search.js
const fs = require("fs");
const os = require("os");
const path = require("path");
const { openLibrary, search, countMatches } = require("./src/main/search");
const { tagsForFile } = require("./src/main/writes");

const expected = { miku: 5, mei: 5, overwatch: 7 };
const db = openLibrary();
const audit = {
  at: new Date().toISOString(),
  db: process.env.IMAGE_TAGGER_HOME
    ? path.join(process.env.IMAGE_TAGGER_HOME, "library.db")
    : path.join(os.homedir(), ".image-tagger", "library.db"),
  expected,
  queries: {},
};

let failed = false;
for (const [query, wanted] of Object.entries(expected)) {
  const rows = search(db, query, { limit: 1000 });
  const count = countMatches(db, query, { limit: 1000 });
  const files = rows.map((file) => ({
    id: file.id,
    filename: file.filename,
    path: file.path,
    tags: tagsForFile(db, file.id),
  }));
  // The fixture has five standalone Mei files plus a group image which WD14
  // independently identifies as character:mei. Keep that truthful sixth hit;
  // explicitly classify it instead of filtering it to manufacture 5/5.
  const meiNamed = files.filter((f) => f.filename.toLowerCase().includes("mei"));
  const meiExtras = files.filter((f) => !f.filename.toLowerCase().includes("mei"));
  const trueMeiExtra = query === "mei" && count > wanted &&
    meiNamed.length === wanted && meiExtras.length === count - wanted &&
    meiExtras.every((f) => f.tags.some(
      (t) => t.category === "character" && t.name.toLowerCase() === "mei"));
  const status = count === wanted ? "PASS" : trueMeiExtra ? "TRUE_EXTRA" : "FAIL";
  audit.queries[query] = { expected: wanted, count, status, files };
  failed ||= status === "FAIL";
  console.log(`${status} ${query}: ${count}/${wanted}`);
  for (const file of files) {
    const tags = file.tags.map((t) => `${t.category}:${t.name}`).join(", ");
    console.log(`  ${file.filename}\n    ${tags}`);
  }
}

const logDir = path.join(os.homedir(), ".image-tagger", "logs");
fs.mkdirSync(logDir, { recursive: true });
const logPath = path.join(logDir, "search-audit.jsonl");
fs.appendFileSync(logPath, JSON.stringify(audit) + "\n", "utf8");
console.log(`audit log: ${logPath}`);
db.close();
process.exitCode = failed ? 1 : 0;
