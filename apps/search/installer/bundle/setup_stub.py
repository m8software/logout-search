#!/usr/bin/env python3
"""logout-search-setup.exe: the one file a Windows user downloads and double-clicks.

It holds the program's folder as a zip. Run, it unpacks that folder to %LOCALAPPDATA%\\logout-search\\app and starts the
program's own setup there (`logout-search.exe setup`): the search data, start at logon, the browser extension, pairing for
other devices. Nothing to type. It is unpacked once, so the engine does not unpack itself at every start, which a
single-file build of the whole program would do.

    logout-search-setup.exe                  everything
    logout-search-setup.exe --dest FOLDER    another folder than %LOCALAPPDATA%\\logout-search
    logout-search-setup.exe --no-run         unpack only (the build's own test)

Standard library only. build.py makes it on Windows, with PyInstaller --onefile.
"""
import os
import shutil
import subprocess
import sys
import zipfile

PAYLOAD = "logout-search.zip"


def default_base() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"), "logout-search")


def pause() -> None:
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    base = os.path.abspath(argv[argv.index("--dest") + 1]) if "--dest" in argv else default_base()
    app = os.path.join(base, "app")
    payload = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), PAYLOAD)
    if os.name == "nt" and "--no-run" not in argv:
        # an older copy that is running holds its files open: close it (the tray and the engine) before replacing it
        for image in ("logout-search-tray.exe", "logout-search.exe"):
            subprocess.run(["taskkill", "/F", "/T", "/IM", image], capture_output=True, creationflags=0x08000000)
    print(f"logout search: unpacking the program to {app}", flush=True)
    try:
        os.makedirs(base, exist_ok=True)
        new = app + ".new"
        shutil.rmtree(new, ignore_errors=True)
        with zipfile.ZipFile(payload) as z:
            z.extractall(new)
        inside = os.listdir(new)
        inner = os.path.join(new, inside[0]) if len(inside) == 1 and os.path.isdir(os.path.join(new, inside[0])) else new
        if os.path.isdir(app):
            shutil.rmtree(app + ".old", ignore_errors=True)
            os.replace(app, app + ".old")
        os.replace(inner, app)
        shutil.rmtree(new, ignore_errors=True)
        shutil.rmtree(app + ".old", ignore_errors=True)
    except OSError as e:
        print(f"could not unpack to {app}: {e}\nIf logout search is already running, sign out and in (or stop it) and run this again.", flush=True)
        pause()
        return 1
    exe = os.path.join(app, "logout-search.exe" if os.name == "nt" else "logout-search")
    if "--no-run" in argv:
        print(f"unpacked: {exe}")
        return 0
    rc = subprocess.call([exe, "setup", "--dest", base, "--copied"])
    pause()
    return rc


if __name__ == "__main__":
    sys.exit(main())
