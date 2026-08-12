"use strict";

const fs = require("fs");
const { pathToFileURL } = require("url");

function fullImageUrl(db, fileId) {
  const id = Number(fileId);
  if (!Number.isSafeInteger(id) || id <= 0) return null;
  const row = db.prepare("SELECT path FROM files WHERE id=?").get(id);
  if (!row?.path || !fs.existsSync(row.path)) return null;
  return pathToFileURL(row.path).toString();
}

module.exports = { fullImageUrl };
