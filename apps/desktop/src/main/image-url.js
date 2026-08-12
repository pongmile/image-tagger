"use strict";

const fs = require("fs");

function fullImageUrl(db, fileId) {
  const id = Number(fileId);
  if (!Number.isSafeInteger(id) || id <= 0) return null;
  const row = db.prepare("SELECT path FROM files WHERE id=?").get(id);
  if (!row?.path || !fs.existsSync(row.path)) return null;
  return `image-tagger://full/${id}`;
}

function registerFullImageProtocol(protocol, db, diagnostic = () => {}) {
  const registered = protocol.registerFileProtocol("image-tagger", (request, callback) => {
    try {
      const url = new URL(request.url);
      const id = url.hostname === "full" ? Number(url.pathname.slice(1)) : NaN;
      if (!Number.isSafeInteger(id) || id <= 0) {
        diagnostic(`image-request-invalid=${request.url}`);
        return callback({ error: -6 });
      }
      const row = db.prepare("SELECT path FROM files WHERE id=?").get(id);
      if (!row?.path || !fs.existsSync(row.path)) {
        diagnostic(`image-request-missing=${id}`);
        return callback({ error: -6 });
      }
      diagnostic(`image-request=${id}`);
      callback({ path: row.path });
    } catch (error) {
      diagnostic(`image-request-error=${String(error?.stack || error)}`);
      callback({ error: -2 });
    }
  });
  if (!registered) throw new Error("Unable to register full-image protocol");
}

module.exports = { fullImageUrl, registerFullImageProtocol };
