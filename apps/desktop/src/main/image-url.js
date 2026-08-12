"use strict";

const fs = require("fs");

function fullImageUrl(db, fileId) {
  const id = Number(fileId);
  if (!Number.isSafeInteger(id) || id <= 0) return null;
  const row = db.prepare("SELECT path FROM files WHERE id=?").get(id);
  if (!row?.path || !fs.existsSync(row.path)) return null;
  return `image-tagger://full/${id}`;
}

function registerFullImageProtocol(protocol, db) {
  protocol.registerFileProtocol("image-tagger", (request, callback) => {
    try {
      const url = new URL(request.url);
      const id = url.hostname === "full" ? Number(url.pathname.slice(1)) : NaN;
      if (!Number.isSafeInteger(id) || id <= 0) return callback({ error: -6 });
      const row = db.prepare("SELECT path FROM files WHERE id=?").get(id);
      if (!row?.path || !fs.existsSync(row.path)) return callback({ error: -6 });
      callback({ path: row.path });
    } catch {
      callback({ error: -2 });
    }
  });
}

module.exports = { fullImageUrl, registerFullImageProtocol };
