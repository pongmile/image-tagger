// Update check — pulls the latest GitHub Release for this repo and compares
// it with the running app's version.
//
// Deliberately NOT a full silent auto-updater: it only informs the user and
// hands back the official release page URL so they can download/run the
// already-tested NSIS installer themselves. Auto-replacing a running .exe's
// files in place is where most Electron "update broke my install" bugs come
// from (file locks, partial extraction, unsigned-binary SmartScreen
// surprises, differential-update metadata drift) — a manual, well-known
// install flow avoids all of that while still saving the user a trip to
// GitHub to check.
"use strict";

const REPO = "pongmile/image-tagger";
const GITHUB_API_URL = `https://api.github.com/repos/${REPO}/releases/latest`;
const FALLBACK_RELEASE_URL = `https://github.com/${REPO}/releases/latest`;
const REQUEST_TIMEOUT_MS = 8000;
const CACHE_TTL_MS = 10 * 60 * 1000; // 10 minutes — avoid hammering the GitHub API

let cache = null; // { result, fetchedAt }

/** Parse "1.2.3" or "v1.2.3" -> [1,2,3], or null if not a plain x.y.z version. */
function parseVersion(raw) {
  const cleaned = String(raw == null ? "" : raw).trim().replace(/^v/i, "");
  const parts = cleaned.split(".");
  if (parts.length !== 3) return null;
  const nums = parts.map((p) => Number(p));
  if (nums.some((n) => !Number.isInteger(n) || n < 0)) return null;
  return nums;
}

/** 1 if a>b, -1 if a<b, 0 if equal. Both must be parseVersion() output. */
function compareVersions(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] > b[i] ? 1 : -1;
  }
  return 0;
}

/** Only ever open a URL GitHub's own API handed back, and only if it's
 * really a github.com URL — defense in depth against a tampered/odd
 * response, on top of the HTTPS request itself. */
function isTrustedGithubUrl(url) {
  try {
    const u = new URL(String(url));
    return u.protocol === "https:" &&
      (u.hostname === "github.com" || u.hostname.endsWith(".github.com"));
  } catch {
    return false;
  }
}

async function fetchLatestRelease(fetchImpl) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetchImpl(GITHUB_API_URL, {
      signal: controller.signal,
      headers: { Accept: "application/vnd.github+json" },
    });
    if (!res.ok) throw new Error(`GitHub API responded ${res.status}`);
    const body = await res.json();
    if (typeof body?.tag_name !== "string" || typeof body?.html_url !== "string") {
      throw new Error("unexpected GitHub API response shape");
    }
    return body;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * @param {object} opts
 * @param {string} opts.currentVersion - app.getVersion() from the caller.
 * @param {(url: string, init?: object) => Promise<Response>} [opts.fetchImpl] -
 *   injectable for tests; defaults to Electron's net.fetch passed in by main.js.
 * @param {boolean} [opts.force] - bypass the in-memory cache.
 */
async function checkForUpdates({ currentVersion, fetchImpl, force = false } = {}) {
  if (typeof fetchImpl !== "function") {
    return { ok: false, error: "no fetch implementation available" };
  }
  if (!force && cache && Date.now() - cache.fetchedAt < CACHE_TTL_MS) {
    return cache.result;
  }

  const current = parseVersion(currentVersion);
  if (!current) {
    return { ok: false, error: `could not parse the running app's version: ${currentVersion}` };
  }

  let release;
  try {
    release = await fetchLatestRelease(fetchImpl);
  } catch (error) {
    // Never cache a failure — a transient network hiccup (offline, GitHub
    // rate limit) shouldn't lock out a real check for the next 10 minutes,
    // and it must never be treated as fatal to the app.
    return { ok: false, error: error && error.message ? error.message : String(error) };
  }

  const latest = parseVersion(release.tag_name);
  if (!latest) {
    return { ok: false, error: `could not parse release tag: ${release.tag_name}` };
  }

  const result = {
    ok: true,
    currentVersion: current.join("."),
    latestVersion: latest.join("."),
    updateAvailable: compareVersions(latest, current) > 0,
    url: isTrustedGithubUrl(release.html_url) ? release.html_url : FALLBACK_RELEASE_URL,
    notes: typeof release.body === "string" ? release.body.slice(0, 4000) : "",
    checkedAt: Date.now(),
  };
  cache = { result, fetchedAt: Date.now() };
  return result;
}

/** Test-only: forget the cached result so the next call re-fetches. */
function clearCache() {
  cache = null;
}

module.exports = {
  checkForUpdates,
  compareVersions,
  parseVersion,
  isTrustedGithubUrl,
  clearCache,
  FALLBACK_RELEASE_URL,
};
