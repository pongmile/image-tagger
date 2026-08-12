# Development

## Runtime architecture

```text
Angular renderer -- typed preload/IPC --> Electron main -- better-sqlite3 --> library.db
                                             |
                                             +-- JSON-lines RPC --> Python daemon
                                                                      |
                                                                      +-- scan/watch/inference writes
```

Search stays in-process in Electron and reads SQLite directly. Python owns filesystem indexing and inference. SQLite WAL plus busy retry coordinates the writers.

## Supported development toolchain

- Node.js 24 LTS; Angular 21 requires Node `^20.19`, `^22.12`, or `^24`
- npm 10+
- Python 3.10–3.12; 3.12 is the release runtime
- Windows release build: PowerShell, `uv`, and optionally Visual Studio Build Tools with Desktop C++

`better-sqlite3` is a native module and must match Electron's ABI. The pinned pair is Electron 41.10.3 + better-sqlite3 12.10.0, for which upstream publishes ABI 145 prebuilds. Change them together and run the Electron smoke test before committing.

## Setup

```powershell
npm run setup
npm test
npm run dev
```

Optional model dependencies are deliberately not installed by `npm run setup`; use the Models screen or `apps/indexer/requirements-models.txt`.

## Verification matrix

| Changed area | Required command |
|---|---|
| Renderer | `npm run build:renderer` and `npm run test:ui` |
| Search grammar/SQL | `npm run test:electron` and `npm run bench` |
| Python/indexing/model routing | `npm run test:python` |
| Packaging/runtime | `npm run dist:win` |
| Dependencies | `npm audit --omit=dev` and `npm audit --prefix apps/desktop/renderer` |

`npm run test:ui` uses a hidden BrowserWindow by default. Set `SMOKE_SHOW=1` only when interactive visual debugging is required. The output screenshot path can be changed with `SMOKE_SHOT`.

## Application data

Set `IMAGE_TAGGER_HOME` to isolate development data:

```powershell
$env:IMAGE_TAGGER_HOME = "$pwd/.dev-data"
npm run dev
```

Never commit databases, thumbnails, logs, downloaded models, `.venv`, bundled Python, or user image libraries.

## Security boundaries

- Renderer has `contextIsolation`, sandbox, no Node integration, CSP, no popup windows and no navigation
- Preload exposes narrow IPC methods; main process whitelists daemon commands
- Open/reveal/re-index accepts only paths already present in the library DB
- User content is treated as data and never interpolated into SQL identifiers or shell commands
- Optional dependencies/models are network downloads; pin and review them before release
