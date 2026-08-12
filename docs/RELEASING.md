# Releasing Windows builds

## Local build

Requirements: Windows x64, Node.js 24 LTS, Python 3.12, npm 10+, `uv`. Native compilation additionally needs Visual Studio Build Tools with Desktop development with C++.

```powershell
npm ci
npm ci --prefix apps/desktop/renderer
npm test
npm run test:ui
npm run bench
npm run dist:win
```

`npm run dist:win` performs these operations:

1. builds the Angular renderer;
2. copies a private Python 3.12 runtime and installs base dependencies;
3. builds NSIS and portable ZIP for Windows x64;
4. verifies the packaged Python imports, daemon RPC, model catalog and bundled samples.

Expected artifacts:

```text
apps/desktop/dist/Image-Tagger-<version>-win-x64.exe
apps/desktop/dist/Image-Tagger-<version>-win-x64.zip
```

Test both on a clean Windows user account. Validate install/uninstall, portable launch, a path containing Thai/space characters, Source/Rescan, OCR, search, preview, and model download cancellation/retry.

## GitHub release

1. Update versions in the root, desktop and renderer `package.json` files.
2. Update `CHANGELOG.md`.
3. Merge only after CI passes.
4. Create and push a matching tag, for example `v0.1.0`.
5. `.github/workflows/release.yml` builds, verifies, hashes and publishes the `.exe`, `.zip` and `SHA256SUMS.txt`.

Optional Authenticode signing uses repository secrets `CSC_LINK` and `CSC_KEY_PASSWORD`. Without them the build works but Windows may show SmartScreen. Never store a certificate/password in the repository.

## Cross-platform note

electron-builder cannot reliably produce every native target from one OS, and `better-sqlite3` is architecture-specific. Build macOS on macOS and Linux on Linux, prepare a platform-native bundled Python runtime, then add per-platform smoke tests before publishing those targets.
