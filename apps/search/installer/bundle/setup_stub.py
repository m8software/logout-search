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
    logout-search-setup.exe --rollback --dest BASE   the way back after an update failed its self-test (autoupdate.verify):
                                                   closes the running copy, puts BASE/app.old (the program the update
                                                   replaced) back as BASE/app, starts the tray again
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


def pause_ok(seconds: float = 20.0, poll=None, clock=time.monotonic, sleep=time.sleep) -> bool:
    """After a setup that worked: leave the window up for a moment so the last lines can be read, then close it by itself. A
    window left waiting for Enter forever (a setup started from a script, a task or a terminal nobody watches) kept two
    logout-search-setup.exe processes alive for good (buildbox, 2026-11-16). True when a key ended the wait early."""
    if not (sys.stdin and sys.stdin.isatty()):
        return False
    print(f"\nThis window closes in {int(seconds)} s (press any key to close it now).", flush=True)
    if poll is None:
        try:
            import msvcrt
            poll = msvcrt.kbhit
        except ImportError:
            poll = lambda: False                       # not Windows: nothing to read a key with, the time is the wait
    end = clock() + seconds
    while clock() < end:
        if poll():
            return True
        sleep(0.2)
    return False


def log_to(base: str):
    """--update runs with no console: what it says goes to BASE/data/state/update.log."""
    d = os.path.join(base, "data", "state")
    os.makedirs(d, exist_ok=True)
    f = open(os.path.join(d, "update.log"), "a", encoding="utf-8")
    sys.stdout = sys.stderr = f
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} update", flush=True)


def unpack(payload: str, app: str, tries: int, keep_old: bool = False) -> None:
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
    if not keep_old:                                # an update keeps the program it replaced until the new one passes its self-test
        shutil.rmtree(app + ".old", ignore_errors=True)


def rollback(base: str) -> int:
    """BASE/app.old back as BASE/app (the running copy is closed first); the broken folder is removed."""
    app = os.path.join(base, "app")
    old = app + ".old"
    if not os.path.isdir(old):
        print(f"no previous program ({old}): nothing to go back to", flush=True)
        return 1
    for i in range(15):                            # a process that has just been closed can hold its files a moment longer
        try:
            shutil.rmtree(app + ".bad", ignore_errors=True)
            if os.path.isdir(app):
                os.replace(app, app + ".bad")
            os.replace(old, app)
            break
        except OSError as e:
            if i == 14:
                print(f"could not put the previous program back: {e}", flush=True)
                return 1
            time.sleep(2)
    shutil.rmtree(app + ".bad", ignore_errors=True)
    restart_tray(app, base)
    print("went back to the previous program; the tray is started again", flush=True)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    base = os.path.abspath(argv[argv.index("--dest") + 1]) if "--dest" in argv else default_base()
    app = os.path.join(base, "app")
    update = "--update" in argv or "--rollback" in argv
    if update:
        log_to(base)
    payload = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), PAYLOAD)
    if os.name == "nt" and "--no-run" not in argv:
        # an older copy that is running holds its files open: close it (the tray and the engine) before replacing it.
        # By name only, not /T: this program was started by the running copy and must not close itself with it
        for image in ("logout-search-tray.exe", "logout-search.exe"):
            subprocess.run(["taskkill", "/F", "/IM", image] if update else ["taskkill", "/F", "/T", "/IM", image],
                           capture_output=True, creationflags=0x08000000)
    if "--rollback" in argv:
        return rollback(base)
    print(f"logout search: unpacking the program to {app}", flush=True)
    try:
        os.makedirs(base, exist_ok=True)
        unpack(payload, app, 15 if update else 1, keep_old=update)
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
    if rc == 0:
        pause_ok()                                     # it worked: the window closes by itself
    else:
        pause()                                        # it did not: the window stays until Enter, so the message can be read
    return rc


def restart_tray(app: str, base: str) -> None:
    tray = os.path.join(app, "logout-search-tray.exe")
    if os.path.isfile(tray):
        subprocess.Popen([tray, "--dest", base], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200)


if __name__ == "__main__":
    sys.exit(main())
