"use strict";

const fs = require("fs");
const { pathToFileURL } = require("url");

function fullImageUrl(db, fileId) {
  const id = Number(fileId);
  if (!Number.isSafeInteger(id) || id <= 0) return null;
  const row = db.prepare("SELECT path FROM files WHERE id=?").get(id);
  if (!row?.path || !fs.existsSync(row.path)) return null;
  return `image-tagger://full/${id}`;
}

async function registerFullImageProtocol(protocol, net, db, diagnostic = () => {}) {
  await protocol.handle("image-tagger", (request) => {
    try {
      const url = new URL(request.url);
      const id = url.hostname === "full" ? Number(url.pathname.slice(1)) : NaN;
      if (!Number.isSafeInteger(id) || id <= 0) {
        diagnostic(`image-request-invalid=${request.url}`);
        return new Response(null, { status: 404 });
      }
      const row = db.prepare("SELECT path FROM files WHERE id=?").get(id);
      if (!row?.path || !fs.existsSync(row.path)) {
        diagnostic(`image-request-missing=${id}`);
        return new Response(null, { status: 404 });
      }
      diagnostic(`image-request=${id}`);
      return net.fetch(pathToFileURL(row.path).toString());
    } catch (error) {
      diagnostic(`image-request-error=${String(error?.stack || error)}`);
      return new Response(null, { status: 500 });
    }
  });
}

module.exports = { fullImageUrl, registerFullImageProtocol };
