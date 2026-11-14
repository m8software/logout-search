#!/usr/bin/env python3
"""logout-search-setup.exe: the one file a Windows user downloads and double-clicks.

It holds the program's folder as a zip. Run, it unpacks that folder to %LOCALAPPDATA%\\logout-search\\app and starts the
program's own setup there (`logout-search.exe setup`): the search data, start at logon, the browser extension, pairing for
other devices. Nothing to type. It is unpacked once, so the engine does not unpack itself at every start, which a
single-file build of the whole program would do.

    logout-search-setup.exe                  everything
    logout-search-setup.exe --dest FOLDER    another folder than %LOCALAPPDATA%\\logout-search
    logout-search-setup.exe --no-run         unpack only (the build's own test)
    logout-search-setup.exe --pin 1234       also set the admin PIN (a paired device then changes settings with it)
    logout-search-setup.exe --update --dest BASE   the daily automatic update (installer/autoupdate.py): no window, no
                                                   questions; closes the running copy, replaces BASE/app, starts the
                                                   tray again. The data and every setting stay. Its log is
                                                   BASE/data/state/update.log.

Standard library only. build.py makes it on Windows, with PyInstaller --onefile.
"""
import os
import shutil
import subprocess
import sys
import time
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


def log_to(base: str):
    """--update runs with no console: what it says goes to BASE/data/state/update.log."""
    d = os.path.join(base, "data", "state")
    os.makedirs(d, exist_ok=True)
    f = open(os.path.join(d, "update.log"), "a", encoding="utf-8")
    sys.stdout = sys.stderr = f
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} update", flush=True)


def unpack(payload: str, app: str, tries: int) -> None:
    new = app + ".new"
    shutil.rmtree(new, ignore_errors=True)
    with zipfile.ZipFile(payload) as z:
        z.extractall(new)
    inside = os.listdir(new)
    inner = os.path.join(new, inside[0]) if len(inside) == 1 and os.path.isdir(os.path.join(new, inside[0])) else new
    for i in range(tries):                         # a process that has just been closed can hold its files a moment longer
        try:
            if os.path.isdir(app):
                shutil.rmtree(app + ".old", ignore_errors=True)
                os.replace(app, app + ".old")
            break
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(2)
    os.replace(inner, app)
    shutil.rmtree(new, ignore_errors=True)
    shutil.rmtree(app + ".old", ignore_errors=True)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    base = os.path.abspath(argv[argv.index("--dest") + 1]) if "--dest" in argv else default_base()
    app = os.path.join(base, "app")
    update = "--update" in argv
    if update:
        log_to(base)
    payload = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), PAYLOAD)
    if os.name == "nt" and "--no-run" not in argv:
        # an older copy that is running holds its files open: close it (the tray and the engine) before replacing it.
        # By name only, not /T: this program was started by the running copy and must not close itself with it
        for image in ("logout-search-tray.exe", "logout-search.exe"):
            subprocess.run(["taskkill", "/F", "/IM", image] if update else ["taskkill", "/F", "/T", "/IM", image],
                           capture_output=True, creationflags=0x08000000)
    print(f"logout search: unpacking the program to {app}", flush=True)
    try:
        os.makedirs(base, exist_ok=True)
        unpack(payload, app, 15 if update else 1)
    except OSError as e:
        print(f"could not unpack to {app}: {e}\nIf logout search is already running, sign out and in (or stop it) and run this again.", flush=True)
        if update and os.path.isdir(app + ".old") and not os.path.isdir(app):
            os.replace(app + ".old", app)              # put the old program back rather than leave none
        if update:
            restart_tray(app, base)
            return 1
        pause()
        return 1
    exe = os.path.join(app, "logout-search.exe" if os.name == "nt" else "logout-search")
    if "--no-run" in argv:
        print(f"unpacked: {exe}")
        return 0
    if update:
        restart_tray(app, base)
        print("updated; the tray is started again", flush=True)
        return 0
    pin = argv[argv.index("--pin") + 1] if "--pin" in argv and argv.index("--pin") + 1 < len(argv) else ""
    rc = subprocess.call([exe, "setup", "--dest", base, "--copied"] + (["--pin", pin] if pin else []))
    pause()
    return rc


def restart_tray(app: str, base: str) -> None:
    tray = os.path.join(app, "logout-search-tray.exe")
    if os.path.isfile(tray):
        subprocess.Popen([tray, "--dest", base], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200)


if __name__ == "__main__":
    sys.exit(main())
