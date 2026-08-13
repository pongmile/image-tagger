"""Smoke-test the packaged Windows runtime without downloading optional models.

The NSIS stage installs and uninstalls the real downloadable artifact, which is
destructive to any copy already installed on this machine (see
`installed_locations`). It is therefore skipped automatically when one is
detected. Flags:

    --installer      run it anyway, uninstalling the existing copy
    --no-installer   never run it
"""
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
    _PACKAGE = json.load(package_file)
VERSION = _PACKAGE["version"]
PRODUCT_NAME = _PACKAGE.get("build", {}).get("productName", "Image Tagger")
INSTALLER = DIST / f"Image-Tagger-{VERSION}-win-x64.exe"
PORTABLE = DIST / f"Image-Tagger-{VERSION}-win-x64.zip"


def _uninstaller_dir(uninstall_string: str) -> str:
    """Directory holding the uninstaller named by an UninstallString.

    The value is a command line, so the executable may be quoted and is
    normally followed by switches ("...\\Uninstall Image Tagger.exe" /currentuser).
    """
    command = uninstall_string.strip()
    if not command:
        return ""
    if command.startswith('"'):
        executable = command[1:].split('"', 1)[0]
    else:
        executable = command.split(" ", 1)[0]
    return str(Path(executable).parent) if executable else ""


def installed_locations() -> list[str]:
    """Where Windows currently has this app registered as installed.

    electron-builder's NSIS target is per-user and keyed by appId, so running
    the installer -- even silently, even with /D= pointing at a scratch
    directory -- *first uninstalls whatever copy is already registered*. On a
    developer machine that copy is their working install, so this verification
    used to delete the app and its shortcuts as a side effect of `npm run dist`
    (user data under IMAGE_TAGGER_HOME is separate and survives, but the
    install itself does not). Detecting it lets the destructive stage be
    skipped by default and stay opt-in.

    CI runners have nothing installed, so the stage still runs there — which is
    where verifying the downloadable artifact actually matters.
    """
    try:
        import winreg
    except ImportError:      # not Windows: nothing can be registered
        return []
    uninstall_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    views = [
        (winreg.HKEY_CURRENT_USER, 0),
        (winreg.HKEY_LOCAL_MACHINE, 0),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
    ]
    found: list[str] = []
    for root, extra_flags in views:
        try:
            uninstall = winreg.OpenKey(
                root, uninstall_path, 0, winreg.KEY_READ | extra_flags)
        except OSError:
            continue
        with uninstall:
            for index in range(winreg.QueryInfoKey(uninstall)[0]):
                try:
                    entry_name = winreg.EnumKey(uninstall, index)
                    with winreg.OpenKey(uninstall, entry_name, 0,
                                        winreg.KEY_READ | extra_flags) as entry:
                        def value(name: str) -> str:
                            try:
                                return str(winreg.QueryValueEx(entry, name)[0] or "")
                            except OSError:
                                return ""
                        # NSIS registers the DisplayName with the version
                        # appended ("Image Tagger 0.7.0"), so match the prefix
                        # rather than the bare product name.
                        if not value("DisplayName").startswith(PRODUCT_NAME):
                            continue
                        # electron-builder's NSIS writes no InstallLocation, so
                        # the uninstaller's own path is the only record of where
                        # the installed copy actually lives.
                        location = value("InstallLocation") or _uninstaller_dir(
                            value("UninstallString"))
                except OSError:
                    continue        # entry vanished mid-enumeration
                # A leftover registry entry pointing at a deleted directory is
                # not an install worth protecting.
                if location and Path(location).is_dir() and location not in found:
                    found.append(location)
    return found


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
        app_home = Path(temp_root) / "User Data - portable path \u00fc"
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
            smoke_log = Path(app_home) / "package-smoke.log"
            diagnostics = smoke_log.read_text(encoding="utf-8") if smoke_log.is_file() else "(no smoke log)"
            raise RuntimeError(
                f"packaged app {executable} exited with {launched.returncode}\n"
                f"smoke log:\n{diagnostics[-4000:]}\n"
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


def main(mode: str = "auto") -> int:
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
        portable_dir = Path(portable_root) / "Portable App - path test \u00fc"
        portable_dir.mkdir()
        with zipfile.ZipFile(PORTABLE) as archive:
            archive.extractall(portable_dir)
        portable_exe = portable_dir / "Image Tagger.exe"
        if not portable_exe.is_file():
            raise FileNotFoundError(f"portable ZIP did not contain {portable_exe}")
        smoke_electron_app(portable_exe, portable_dir)
    print("portable ZIP extraction and launch: ok")

    installer_verified = verify_installer(mode)

    # Name only what actually ran: a summary claiming the installer was checked
    # when the stage was skipped is exactly the kind of thing a release is
    # signed off on.
    print("PASS: packaged app, runtime, daemon, samples"
          + (", installer and portable ZIP" if installer_verified
             else " and portable ZIP (installer stage skipped)"))
    return 0


def verify_installer(mode: str) -> bool:
    """Exercise the downloadable NSIS artifact: silent install, launch, uninstall.

    Destructive to an existing installation (see `installed_locations`), so by
    default it yields to one rather than removing it silently. Returns whether
    the stage actually ran.
    """
    if mode == "skip":
        print("NSIS silent install, launch, and uninstall: skipped (--no-installer)")
        return False
    existing = installed_locations() if mode == "auto" else []
    if existing:
        print("NSIS silent install, launch, and uninstall: SKIPPED — "
              f"{PRODUCT_NAME} is already installed at " + ", ".join(existing))
        print("  The NSIS artifact uninstalls the registered copy before "
              "installing, so verifying it here would remove that install and "
              "its shortcuts. Your library, thumbnails and models are stored "
              "separately and are not affected either way.")
        print("  Re-run with `npm run verify:package -- --installer` to verify "
              "it anyway (you will need to reinstall afterwards).")
        return False

    with tempfile.TemporaryDirectory(
        prefix="image-tagger-installer-", ignore_cleanup_errors=True
    ) as install_root:
        install_dir = Path(install_root) / "Image Tagger Installed \u00fc"
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
            deadline = time.time() + 60
            while install_dir.exists() and time.time() < deadline:
                time.sleep(0.1)
            if install_dir.exists():
                raise TimeoutError("NSIS uninstaller did not fully remove the installed app")
    print("NSIS silent install, launch, and uninstall: ok")
    return True


def installer_mode(argv: list[str]) -> str:
    if "--no-installer" in argv:
        return "skip"
    if "--installer" in argv:
        return "force"
    return "auto"


if __name__ == "__main__":
    raise SystemExit(main(installer_mode(sys.argv[1:])))
