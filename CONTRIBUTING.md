# Contributing

Use Node.js 24 LTS and Python 3.12. Run `npm run setup` once, then `npm test` before opening a pull request. UI changes also require `npm run test:ui`; search changes require `npm run bench`; packaging changes require a Windows `npm run dist:win`.

Keep changes focused. Add regression coverage for bugs. Do not commit user images, databases, downloaded models, logs, credentials, `.venv`, `node_modules`, bundled runtimes or generated `dist` files.

Native dependency changes must document the Electron ABI and verify that a prebuilt binary exists for every published platform/architecture, or document the required compiler toolchain.
