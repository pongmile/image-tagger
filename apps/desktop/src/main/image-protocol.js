"use strict";

const fs = require("fs");
const { net } = require("electron");
const { pathToFileURL } = require("url");

function fullImageUrl(db, fileId) {
  const id = Number(fileId);
  if (!Number.isSafeInteger(id) || id <= 0) return null;
  return db.prepare("SELECT id FROM files WHERE id=?").get(id)
    ? `image-tagger://full/${id}`
    : null;
}

function createFullImageProtocolHandler(db, onError = () => {}) {
  return (request) => {
    try {
      const url = new URL(request.url);
      if (url.hostname !== "full") return new Response("Not found", { status: 404 });
      const id = Number(url.pathname.slice(1));
      if (!Number.isSafeInteger(id) || id <= 0) return new Response("Bad image id", { status: 400 });
      const row = db.prepare("SELECT path FROM files WHERE id=?").get(id);
      if (!row?.path || !fs.existsSync(row.path)) return new Response("Image not found", { status: 404 });
      return net.fetch(pathToFileURL(row.path).toString());
    } catch (error) {
      onError(error);
      return new Response("Unable to read image", { status: 500 });
    }
  };
}

module.exports = { createFullImageProtocolHandler, fullImageUrl };
