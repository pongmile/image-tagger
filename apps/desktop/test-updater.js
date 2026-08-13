// Standalone Node test for updater.js — no Electron needed (pure logic), and
// no real network calls: a fake fetch stands in for GitHub's API so this is
// deterministic and safe to run in CI (no rate limits, no flakiness).
//
// Run: node apps/desktop/test-updater.js
"use strict";

const updater = require("./src/main/updater");

let failures = 0;
function check(cond, msg) {
  console.log((cond ? "  ok  " : " FAIL ") + msg);
  if (!cond) failures++;
}

function fakeFetch(status, body) {
  return async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
}

async function run() {
  // --- version parsing / comparison ---------------------------------------
  check(JSON.stringify(updater.parseVersion("1.2.3")) === "[1,2,3]", "parses a plain x.y.z version");
  check(JSON.stringify(updater.parseVersion("v1.2.3")) === "[1,2,3]", "strips a leading 'v'");
  check(updater.parseVersion("1.2") === null, "rejects a version missing a segment");
  check(updater.parseVersion("1.2.abc") === null, "rejects a non-numeric segment");
  check(updater.compareVersions([1, 2, 3], [1, 2, 2]) === 1, "1.2.3 > 1.2.2");
  check(updater.compareVersions([1, 2, 3], [1, 3, 0]) === -1, "1.2.3 < 1.3.0");
  check(updater.compareVersions([1, 2, 3], [1, 2, 3]) === 0, "1.2.3 == 1.2.3");

  // --- URL trust check -----------------------------------------------------
  check(updater.isTrustedGithubUrl("https://github.com/pongmile/image-tagger/releases/tag/v1.0.0"),
    "trusts a real github.com release URL");
  check(!updater.isTrustedGithubUrl("https://github.evil.com/phish"), "rejects a look-alike host");
  check(!updater.isTrustedGithubUrl("http://github.com/x"), "rejects non-https");
  check(!updater.isTrustedGithubUrl("not a url"), "rejects garbage input");

  // --- checkForUpdates: update available ----------------------------------
  updater.clearCache();
  {
    const fetchImpl = fakeFetch(200, {
      tag_name: "v9.9.9",
      html_url: "https://github.com/pongmile/image-tagger/releases/tag/v9.9.9",
      body: "release notes here",
    });
    const r = await updater.checkForUpdates({ currentVersion: "0.3.0", fetchImpl, force: true });
    check(r.ok === true, "reports ok:true for a well-formed release");
    check(r.updateAvailable === true, "detects an available update (0.3.0 -> 9.9.9)");
    check(r.latestVersion === "9.9.9", `latestVersion is 9.9.9 (got ${r.latestVersion})`);
    check(r.url === "https://github.com/pongmile/image-tagger/releases/tag/v9.9.9",
      "passes through the trusted release URL");
  }

  // --- checkForUpdates: already up to date --------------------------------
  updater.clearCache();
  {
    const fetchImpl = fakeFetch(200, {
      tag_name: "v0.3.0",
      html_url: "https://github.com/pongmile/image-tagger/releases/tag/v0.3.0",
      body: "",
    });
    const r = await updater.checkForUpdates({ currentVersion: "0.3.0", fetchImpl, force: true });
    check(r.ok === true && r.updateAvailable === false, "reports up-to-date when versions match");
  }

  // --- checkForUpdates: untrusted URL in response falls back --------------
  updater.clearCache();
  {
    const fetchImpl = fakeFetch(200, {
      tag_name: "v9.9.9",
      html_url: "https://not-github.example/tag/v9.9.9",
      body: "",
    });
    const r = await updater.checkForUpdates({ currentVersion: "0.3.0", fetchImpl, force: true });
    check(r.ok === true && r.url === updater.FALLBACK_RELEASE_URL,
      "falls back to the known releases URL when the API response's URL isn't github.com");
  }

  // --- checkForUpdates: network/HTTP failure never throws -----------------
  updater.clearCache();
  {
    const fetchImpl = fakeFetch(500, {});
    const r = await updater.checkForUpdates({ currentVersion: "0.3.0", fetchImpl, force: true });
    check(r.ok === false && typeof r.error === "string", "a non-2xx response resolves with ok:false, not a throw");
  }

  // --- checkForUpdates: malformed response never throws -------------------
  updater.clearCache();
  {
    const fetchImpl = fakeFetch(200, { not_a_release: true });
    const r = await updater.checkForUpdates({ currentVersion: "0.3.0", fetchImpl, force: true });
    check(r.ok === false, "an unexpected response shape resolves with ok:false, not a throw");
  }

  // --- checkForUpdates: rejecting fetch never throws ----------------------
  updater.clearCache();
  {
    const fetchImpl = async () => { throw new Error("offline"); };
    const r = await updater.checkForUpdates({ currentVersion: "0.3.0", fetchImpl, force: true });
    check(r.ok === false && r.error === "offline", "a rejected fetch (offline) resolves with ok:false, not a throw");
  }

  // --- checkForUpdates: invalid current version ---------------------------
  updater.clearCache();
  {
    const fetchImpl = fakeFetch(200, { tag_name: "v1.0.0", html_url: "https://github.com/x", body: "" });
    const r = await updater.checkForUpdates({ currentVersion: "not-a-version", fetchImpl, force: true });
    check(r.ok === false, "an unparseable currentVersion resolves with ok:false, not a throw");
  }

  // --- caching: a second call within the TTL doesn't refetch --------------
  updater.clearCache();
  {
    let calls = 0;
    const fetchImpl = async (...args) => {
      calls++;
      return fakeFetch(200, {
        tag_name: "v0.3.0", html_url: "https://github.com/x", body: "",
      })(...args);
    };
    await updater.checkForUpdates({ currentVersion: "0.3.0", fetchImpl, force: true });
    await updater.checkForUpdates({ currentVersion: "0.3.0", fetchImpl }); // force omitted -> uses cache
    check(calls === 1, `cached result reused instead of refetching (fetch called ${calls} time(s))`);
  }

  console.log();
  if (failures) {
    console.log(`RESULT: FAIL — ${failures} check(s)`);
    process.exit(1);
  }
  console.log("RESULT: PASS — update-check logic verified");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
