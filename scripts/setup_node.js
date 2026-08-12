// Install JavaScript dependencies with bounded retries. Native prebuilds are
// hosted on GitHub Releases; a transient socket reset otherwise makes npm fall
// back to node-gyp and misleadingly ask ordinary contributors for Visual Studio.
const { spawnSync } = require("child_process");
const path = require("path");

const root = path.resolve(__dirname, "..");
const npmCli = process.env.npm_execpath;
if (!npmCli) throw new Error("npm_execpath is unavailable; run this via npm run setup");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function install(cwd, label, attempts = 4) {
  for (let attempt = 1; attempt <= attempts; attempt++) {
    console.log(`\nInstalling ${label} dependencies (attempt ${attempt}/${attempts})…`);
    const result = spawnSync(process.execPath, [npmCli, "install"], {
      cwd,
      stdio: "inherit",
      env: process.env,
    });
    if (result.status === 0) return;
    if (attempt < attempts) await sleep(attempt * 1500);
  }
  throw new Error(`Could not install ${label} dependencies after ${attempts} attempts`);
}

(async () => {
  await install(root, "desktop");
  await install(path.join(root, "apps", "desktop", "renderer"), "renderer");
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
