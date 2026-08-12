#!/usr/bin/env node
"use strict";

const { spawnSync } = require("child_process");
const path = require("path");

const root = path.resolve(__dirname, "..");
const desktop = path.join(root, "apps", "desktop");
const electronPackage = require.resolve("electron", { paths: [desktop, root] });
const electron = require(electronPackage);
const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const target = process.argv[2] || ".";
const args = [target, ...process.argv.slice(3)];
if (process.platform === "linux" && env.CI === "true") {
  args.unshift("--no-sandbox");
}
const result = spawnSync(electron, args, { cwd: desktop, env, stdio: "inherit" });
if (result.error) throw result.error;
process.exit(result.status ?? 1);
