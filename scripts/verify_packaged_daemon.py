"""Smoke-test the packaged Windows runtime without downloading optional models."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "apps" / "desktop" / "dist"
UNPACKED = DIST / "win-unpacked"
PYTHON = UNPACKED / "resources" / "python" / "python.exe"
INDEXER = UNPACKED / "resources" / "indexer"
with (ROOT / "apps" / "desktop" / "package.json").open(encoding="utf-8") as package_file:
    VERSION = json.load(package_file)["version"]
INSTALLER = DIST / f"Image-Tagger-{VERSION}-win-x64.exe"
PORTABLE = DIST / f"Image-Tagger-{VERSION}-win-x64.zip"


def require_release_files() -> None:
    required = [
        UNPACKED / "Image Tagger.exe",
        PYTHON,
        INDEXER / "indexer" / "daemon.py",
        UNPACKED / "resources" / "samples" / "beach-sunset-kayak.jpg",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if not INSTALLER.is_file():
        missing.append(str(INSTALLER))
    if not PORTABLE.is_file():
        missing.append(str(PORTABLE))
    if missing:
        raise FileNotFoundError("Missing packaged files: " + ", ".join(missing))


def smoke_electron_app(executable: Path, cwd: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="image-tagger-app-release-") as temp_root:
        app_home = Path(temp_root) / "User Data - portable path ü"
        app_home.mkdir()
        marker = Path(app_home) / "package-smoke-ok"
        app_env = os.environ.copy()
        # Codex/VS Code terminals can run Electron as a Node binary for their
        # own tooling. A downloaded app does not inherit this flag, and leaving
        # it set makes the packaged executable exit before Electron starts.
        app_env.pop("ELECTRON_RUN_AS_NODE", None)
        app_env["IMAGE_TAGGER_HOME"] = str(app_home)
        app_env["IMAGE_TAGGER_PACKAGE_SMOKE"] = "1"
        launched = subprocess.run(
            [str(executable)], cwd=cwd, env=app_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=60,
        )
        if launched.returncode:
            raise RuntimeError(
                f"packaged app {executable} exited with {launched.returncode}\n"
                f"stdout:\n{launched.stdout[-4000:]}\n"
                f"stderr:\n{launched.stderr[-4000:]}"
            )
        marker_deadline = time.time() + 60
        while not marker.is_file() and time.time() < marker_deadline:
            time.sleep(0.1)
        if not marker.is_file():
            smoke_log = Path(app_home) / "package-smoke.log"
            diagnostics = smoke_log.read_text(encoding="utf-8") if smoke_log.is_file() else "(no smoke log)"
            raise RuntimeError(
                f"packaged app {executable} exited without completing its "
                "renderer/indexer smoke test\n"
                f"smoke log:\n{diagnostics[-4000:]}\n"
                f"stdout:\n{launched.stdout[-4000:]}\n"
                f"stderr:\n{launched.stderr[-4000:]}"
            )
        # Electron's Windows launcher can return just before the last renderer
        # process releases its executable mapping. Wait for that handle instead
        # of racing TemporaryDirectory cleanup or the NSIS uninstaller.
        deadline = time.time() + 20
        while True:
            try:
                descriptor = os.open(executable, os.O_RDWR)
                os.close(descriptor)
                break
            except PermissionError:
                if time.time() >= deadline:
                    raise TimeoutError(f"packaged app did not release {executable}")
                time.sleep(0.1)


def main() -> int:
    require_release_files()
    subprocess.run(
        [str(PYTHON), "-c", "import PIL, watchdog, piexif, onnxruntime, rapidocr_onnxruntime, sqlite_vec; print('packaged Python dependencies: ok')"],
        check=True,
        cwd=INDEXER,
    )

    with tempfile.TemporaryDirectory(prefix="image-tagger-release-") as test_home:
        env = os.environ.copy()
        env["IMAGE_TAGGER_HOME"] = test_home
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            [str(PYTHON), "-m", "indexer.daemon"],
            cwd=INDEXER,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        output: queue.Queue[dict] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    output.put(json.loads(line))
                except ValueError:
                    output.put({"raw": line.rstrip()})

        threading.Thread(target=read_output, daemon=True).start()

        def until(predicate, timeout: float = 60) -> dict:
            deadline = time.time() + timeout
            while time.time() < deadline:
                item = output.get(timeout=max(0.1, deadline - time.time()))
                if predicate(item):
                    return item
            raise TimeoutError("packaged daemon event timed out")

        request_id = 0

        def call(command: str, **arguments) -> dict:
            nonlocal request_id
            request_id += 1
            assert process.stdin is not None
            process.stdin.write(json.dumps({"id": request_id, "cmd": command, **arguments}) + "\n")
            process.stdin.flush()
            return until(lambda item: item.get("id") == request_id, 90)

        try:
            ready = until(lambda item: item.get("event") == "ready", 30)
            ping = call("ping")
            state = call("model_state")
            doctor = call("doctor")
            assert ping.get("ok") and ping.get("result") == "pong", ping
            assert state.get("ok") and len(state["result"]["facets"]) >= 5, state
            assert doctor.get("ok"), doctor
            print(json.dumps({
                "ready": ready.get("event"),
                "ping": ping["result"],
                "facets": len(state["result"]["facets"]),
                "tier": doctor["result"].get("tier"),
            }, ensure_ascii=False))
        finally:
            if process.poll() is None:
                try:
                    call("stop")
                finally:
                    process.wait(timeout=10)

    # Launch the actual packaged Electron executable with its bundled renderer,
    # preload, native better-sqlite3 module, and Python daemon. The opt-in smoke
    # flag keeps the window hidden and exits after Angular + daemon ping.
    smoke_electron_app(UNPACKED / "Image Tagger.exe", UNPACKED)
    print("packaged Electron executable: ok")

    with tempfile.TemporaryDirectory(prefix="image-tagger-portable-") as portable_root:
        portable_dir = Path(portable_root) / "Portable App - path test ü"
        portable_dir.mkdir()
        with zipfile.ZipFile(PORTABLE) as archive:
            archive.extractall(portable_dir)
        portable_exe = portable_dir / "Image Tagger.exe"
        if not portable_exe.is_file():
            raise FileNotFoundError(f"portable ZIP did not contain {portable_exe}")
        smoke_electron_app(portable_exe, portable_dir)
    print("portable ZIP extraction and launch: ok")

    # Exercise the downloadable NSIS artifact itself, including silent install,
    # first launch, and uninstall in an isolated temporary destination.
    with tempfile.TemporaryDirectory(
        prefix="image-tagger-installer-", ignore_cleanup_errors=True
    ) as install_root:
        install_dir = Path(install_root) / "Image Tagger Installed ü"
        installed = subprocess.run(
            [str(INSTALLER), "/S", f"/D={install_dir}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=300,
        )
        if installed.returncode:
            raise RuntimeError(f"NSIS installer exited with {installed.returncode}")
        installed_exe = install_dir / "Image Tagger.exe"
        if not installed_exe.is_file():
            raise FileNotFoundError(f"NSIS did not install {installed_exe}")
        smoke_electron_app(installed_exe, install_dir)
        uninstaller = install_dir / "Uninstall Image Tagger.exe"
        if uninstaller.is_file():
            removed = subprocess.run(
                [str(uninstaller), "/S"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
            )
            if removed.returncode:
                raise RuntimeError(f"NSIS uninstaller exited with {removed.returncode}")
            deadline = time.time() + 30
            while installed_exe.exists() and time.time() < deadline:
                time.sleep(0.1)
            if installed_exe.exists():
                raise TimeoutError("NSIS uninstaller did not remove the installed app")
    print("NSIS silent install, launch, and uninstall: ok")

    print("PASS: packaged app, runtime, daemon, samples, installer and portable ZIP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
