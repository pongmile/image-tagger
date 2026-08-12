// Audit the real Electron/Node search fast path and print the tags per sample.
const path = require("path");
const { openLibrary, search } = require("../apps/desktop/src/main/search");
const writes = require("../apps/desktop/src/main/writes");

const db = openLibrary();
for (const query of ["miku", "mei", "overwatch"]) {
  const rows = search(db, query, { limit: 1000 });
  console.log(JSON.stringify({
    query,
    count: rows.length,
    files: rows.map((row) => row.filename),
  }));
}

const sampleDir = path.join(process.env.USERPROFILE, "Documents", "test_search");
const files = db.prepare(
  "SELECT id, filename, path FROM files WHERE folder=? ORDER BY filename"
).all(sampleDir);
for (const file of files) {
  console.log(JSON.stringify({
    filename: file.filename,
    tags: writes.tagsForFile(db, file.id).map((tag) => `${tag.category}:${tag.name}`),
  }));
}
