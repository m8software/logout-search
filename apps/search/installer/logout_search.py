#!/usr/bin/env python3
"""logout search: the one program on a home computer. Installs the data, starts the engine, keeps it updated, starts it
at logon, and connects the browser extension to it (gate T7 of SEARCH-HOME-ENGINE-PLAN.md).

    logout-search setup     [--print]            everything below in one go, with nothing to type: what a double-click runs
    logout-search install   --source hf:ORG/NAME --pubkey KEY --dest BASE [--quick] [--switch]      (install.py's options)
    logout-search start     --dest BASE [--port 8090] [--no-smoke] [--print-env [--format posix|cmd|powershell|json]]
    logout-search status    --dest BASE
    logout-search addon     install|remove wikilinks|se-howto --dest BASE [--source hf:ORG/NAME --pubkey KEY]   |   addon list --dest BASE
    logout-search update    --dest BASE --source hf:ORG/NAME --pubkey KEY.pem [--check]
    logout-search autostart on|off --dest BASE [--port 8090] [--print]
    logout-search host      register|unregister --dest BASE [--port 8090] [--print]
    logout-search host      run --dest BASE [--port 8090]              (what the browser starts; see `host register`)
    logout-search peer      --dest BASE [--port 8090]                  (what `start` runs beside the engine; see below)
    logout-search version   [--dest BASE]

Runs from this checkout (`python3 logout_search.py ...`) or as the bundle `bundle/build.py` makes with PyInstaller, where it
is the only executable: the engine (serve/api), install.py and the update tools are inside it, with Python and the
libraries. Nothing here needs a shell, Docker or a database. Standard library only on the launcher's own path.

autostart   Linux: a systemd user unit (`~/.config/systemd/user/logout-search.service`). macOS: a launchd agent
            (`~/Library/LaunchAgents/cloud.logout.search.plist`). Windows: a Task Scheduler task at logon (`schtasks`).
            `--print` shows what would be written and run, and writes nothing.
host        The native-messaging host the logout browser extension starts (name `cloud.logout.search`). It is a small
            forwarder: the browser talks to it over stdin/stdout, it asks the engine on 127.0.0.1:PORT. It is registered with
            every browser found on this machine (Chrome, Chromium, Brave, Edge, Vivaldi, Firefox): a JSON manifest in each
            browser's NativeMessagingHosts folder on Linux and macOS, a registry key under HKCU on Windows.

setup       Copies this program to its own folder, downloads the search data (checked against the signed list; the source
            and the public key are the ones below), starts the engine now and at logon, and connects the browser
            extension. Everything goes under one folder in your home: BASE/app (the program) and BASE/data.
peer        Lets your other devices use this engine: it keeps an outbound connection to the rendezvous at
            search.logout.cloud, and a phone on the same network that opens that page and taps Pair talks to this
            computer directly, encrypted (serve/api/rtc_peer.py). Searches never pass through the rendezvous. It asks the
            engine on 127.0.0.1:PORT, holds no index itself, and opens no listening port.

Test hooks: LOGOUT_SEARCH_PLATFORM=linux|darwin|win32 makes the file-writing commands behave as on that system (paths,
file contents); HOME, APPDATA and LOCALAPPDATA are read from the environment.
"""
import argparse
import json
import os
import platform
import shlex
import subprocess
import sys

VERSION = "0.1"
HOST_NAME = "cloud.logout.search"
CHROME_ID = "dgnaanjmpoeoecakbcfjmcnoliapkebo"       # fixed by the extension's "key" (apps/search/extension/manifest.json)
FIREFOX_ID = "search@logout.cloud"
SERVICE = "logout-search"
# what `setup` installs: the published data, and the key every file of it is checked against (the dataset page shows both)
DATA_SOURCE = "hf:logoutsearch/logout-search"
DATA_PUBKEY = "ed25519:40502f6841e8644b8f193c10af27e5b148f74297bf6da7067115a60792d138bc"
TASK_NAME = "logout search"

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))
PLATFORM = os.environ.get("LOGOUT_SEARCH_PLATFORM") or ("win32" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux")


def bundle_dir() -> str:
    return getattr(sys, "_MEIPASS", HERE)


def engine_dir() -> str:
    """Where search_api.py and its modules are on disk: the bundle's `engine/`, or serve/api in the checkout."""
    if FROZEN:
        return os.path.join(bundle_dir(), "engine")
    return os.path.normpath(os.path.join(HERE, "..", "serve", "api"))


def release_dir() -> str:
    return os.path.join(bundle_dir(), "release") if FROZEN else os.path.normpath(os.path.join(HERE, "..", "release"))


for _p in (HERE, release_dir()):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import install  # noqa: E402  (installer/install.py: engine_env, wait_ready, the install itself)
import release_ptr  # noqa: E402


def program() -> list:
    """The command that runs this program again (for the service, the task, the host wrapper)."""
    return [sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def home() -> str:
    return os.path.expanduser("~")


# ================================================================================================ engine / start
def run_engine() -> None:
    """Run search_api.py from the engine folder, as its own process does (`python search_api.py`): the libraries come
    from this program, the engine's code and its ranker.json from the folder next to it."""
    import runpy
    d = engine_dir()
    sys.path.insert(0, d)
    runpy.run_path(os.path.join(d, "search_api.py"), run_name="__main__")


def check_imports() -> int:
    """The libraries the engine needs, with versions: proves a bundle carries them."""
    out = {}
    for name in ("numpy", "tantivy", "onnxruntime", "tokenizers", "psycopg"):
        try:
            m = __import__(name)
            out[name] = getattr(m, "__version__", "?")
        except Exception as e:  # noqa: BLE001
            out[name] = f"MISSING ({type(e).__name__})"
    out["python"] = platform.python_version()
    out["engine_dir"] = engine_dir()
    out["engine_files"] = sorted(f for f in os.listdir(engine_dir()) if f.endswith((".py", ".json"))) if os.path.isdir(engine_dir()) else []
    print(json.dumps(out, indent=1, sort_keys=True))
    return 1 if any(str(v).startswith("MISSING") for v in out.values()) else 0


def start(a) -> None:
    base = os.path.abspath(a.dest)
    env = install.engine_env(base, a.port)
    if a.print_env:
        print(install.format_env(env, a.format or ("cmd" if PLATFORM == "win32" else "posix")))
        return
    if "SEARCH_CE_DIR" not in env:
        say("warning: this release ships no reranker: results will rank noticeably worse")
    say(f"starting the engine on http://127.0.0.1:{a.port} from {os.path.realpath(os.path.join(base, 'current'))}")
    proc = subprocess.Popen(program() + ["engine"], env={**os.environ, **env})
    peer = None
    try:
        state = install.wait_ready(a.port, lambda: proc.poll() is None)
        if state == "ready" and not a.no_smoke and install.run_smoke(a.port, "SEARCH_SE_INDEX" in env):
            proc.terminate()
            proc.wait()
            sys.exit(1)
        if state == "ready" and not getattr(a, "no_peer", False):
            # the other devices' way in (see `peer`); it ends with the engine, and the engine runs on without it
            peer = subprocess.Popen(program() + ["peer", "--dest", base, "--port", str(a.port)])
        sys.exit(proc.wait())
    except KeyboardInterrupt:
        proc.terminate()
        sys.exit(proc.wait())
    finally:
        if peer is not None and peer.poll() is None:
            peer.terminate()


# ================================================================================================ peer (other devices)
def load_peer():
    """rtc_peer, with the stand-in for PyAV in place: aiortc imports `av` (FFmpeg) for audio and video this peer never
    opens, and the bundle does not carry it (serve/api/av_absent.py says why)."""
    d = engine_dir()
    if d not in sys.path:
        sys.path.insert(0, d)
    if "av" not in sys.modules:
        import av_absent
        sys.modules["av"] = av_absent
    import rtc_peer
    return rtc_peer


def peer_check() -> int:
    """The libraries the peer needs: proves a bundle carries them (the build's smoke test runs this)."""
    out = {}
    try:
        rp = load_peer()
        import aiortc
        out = {"aiortc": aiortc.__version__, "qr": rp.qr_rows("https://search.logout.cloud/") is not None, "rendezvous": rp.RV_URL}
    except Exception as e:  # noqa: BLE001
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, indent=1, sort_keys=True))
    return 1 if "error" in out else 0


def data_gb(base: str) -> float | None:
    """The installed release's size from its manifest, for the engine pill's details."""
    try:
        with open(os.path.join(base, "current", "manifest.json"), encoding="utf-8") as f:
            return round(json.load(f).get("total_bytes", 0) / 1e9, 1) or None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def peer_run(a) -> int:
    import asyncio
    base = os.path.abspath(a.dest)
    state = os.path.join(base, "state")
    os.makedirs(state, exist_ok=True)
    # read by rtc_peer and native_host when they are imported: this computer's pairings and the page's settings live with the data
    os.environ.setdefault("RTC_STATE", os.path.join(state, "engine.json"))
    os.environ.setdefault("LOGOUT_SETTINGS", os.path.join(state, "settings.json"))
    os.environ.setdefault("LOGOUT_FEEDS", "0")          # no feed fetcher runs beside this engine: the page offers no sources
    try:
        rp = load_peer()
    except ImportError as e:
        say(f"peer: not started ({e}); other devices cannot pair with this engine, searches on this computer are unaffected")
        return 1
    import native_host as nh

    def details() -> dict:
        return {"engine": nh.friendly_name(), "ip": nh.lan_ip(), "via": "",
                "host_version": nh.VERSION, "release": release_ptr.current_id(base), "data_gb": data_gb(base),
                "engine_up": install._readyz(a.port) == 200}

    say("other devices on this network can now pair: open https://search.logout.cloud on one and tap Pair")
    try:
        asyncio.run(rp.main(answer=http_answer(a.port, nh), info=details))
    except KeyboardInterrupt:
        pass
    return 0


# ================================================================================================ setup (a double-click)
def default_base() -> str:
    """The one folder everything goes under, per system."""
    if PLATFORM == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(home(), "AppData", "Local"), "logout-search")
    if PLATFORM == "darwin":
        return os.path.join(home(), "Library", "Application Support", "logout-search")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home(), ".local", "share"), "logout-search")


def exe_name() -> str:
    return "logout-search.exe" if PLATFORM == "win32" else "logout-search"


def setup_plan(base: str, port: int) -> list:
    """The steps of `setup`, as lines to show: [(what, detail)]."""
    app, data = os.path.join(base, "app"), os.path.join(base, "data")
    here = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else HERE
    steps = []
    if FROZEN and os.path.normcase(os.path.realpath(here)) != os.path.normcase(os.path.realpath(app)):
        steps.append(("copy", f"this program -> {app}"))
    steps += [("data", f"{DATA_SOURCE} -> {data}  (about 11 GB to download, 18 GB on disk; checked against {DATA_PUBKEY})"),
              ("autostart", ("a tray icon runs the engine now and at each sign-in; Start menu entries to open and uninstall it"
                             if PLATFORM == "win32" else f"the engine starts now and at logon, answering only on http://127.0.0.1:{port}")),
              ("devices", "phones and other computers on this network can pair at https://search.logout.cloud")]
    return steps


def pause_if_double_clicked() -> None:
    """On Windows a double-click opens a console that closes with the program: wait, so the last lines can be read."""
    if PLATFORM == "win32" and FROZEN and sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass


def setup(a) -> int:
    import shutil
    base = os.path.abspath(a.dest) if a.dest else default_base()
    app, data = os.path.join(base, "app"), os.path.join(base, "data")
    plan = setup_plan(base, a.port)
    if a.print:
        for what, detail in plan:
            print(f"{what:10} {detail}")
        return 0
    say("logout search: setting up. Nothing needs typing; this window shows what is happening.\n")
    if plan[0][0] == "copy":
        # run from where it was unpacked (Downloads): the copy under BASE is the one that starts at logon, so the
        # unpacked folder can be deleted afterwards
        src = os.path.dirname(os.path.abspath(sys.executable))
        say(f"1/5  copying the program to {app}")
        os.makedirs(base, exist_ok=True)
        try:
            shutil.rmtree(app + ".new", ignore_errors=True)
            shutil.copytree(src, app + ".new", symlinks=True)
            if os.path.isdir(app):
                shutil.rmtree(app + ".old", ignore_errors=True)
                os.replace(app, app + ".old")
            os.replace(app + ".new", app)
            shutil.rmtree(app + ".old", ignore_errors=True)
        except OSError as e:
            say(f"could not copy the program to {app}: {e}\nIf logout search is already running, stop it (or sign out and in) and run this again.")
            return 1
        rc = subprocess.call([os.path.join(app, exe_name()), "setup", "--dest", base, "--port", str(a.port), "--copied"])
        return rc
    say(f"2/5  downloading the search data to {data}\n     about 11 GB; it is safe to stop and run this again, it carries on where it stopped")
    os.environ["LOGOUT_INSTALL_QUIET"] = "1"
    try:
        run_install(["install", "--source", DATA_SOURCE, "--pubkey", DATA_PUBKEY, "--dest", data])
    except SystemExit as e:
        if e.code not in (0, None):
            say("the data was not installed; run this again to carry on")
            return int(e.code) if isinstance(e.code, int) else 1
    say("3/5  starting the engine now and at each sign-in")
    if PLATFORM == "win32":
        # a tray icon that runs the engine with no window and restarts it; started at sign-in from the user's own Run
        # key, with a Start menu folder (open, uninstall) and an entry in Settings > Apps. Per user: no administrator
        # (Task Scheduler needed one: the owner's first install got "Access is denied")
        import tray_win
        for line in tray_win.register(base, a.port):
            say(f"     could not set up {line}")
        tray = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "logout-search-tray.exe")
        subprocess.Popen([tray, "--dest", base, "--port", str(a.port)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=0x00000008 | 0x00000200)   # detached: it outlives this window
        say("     the logout search icon is in the taskbar's corner (it may be under the ^ arrow); the Start menu has it too")
    else:
        auto = argparse.Namespace(state="on", dest=data, port=a.port, print=False)
        if autostart(auto) != 0:
            # no systemd user session: start it for this session at least
            say("     it could not be set to start by itself here; starting it for now")
            os.makedirs(os.path.join(data, "state"), exist_ok=True)
            log = open(os.path.join(data, "state", "engine.log"), "ab")
            subprocess.Popen(start_cmd(data, a.port), stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    # no browser extension any more: the page reaches this engine the way a phone does, through the pairing peer
    say("4/5  letting this computer's browser and your other devices use it")
    say("5/5  waiting for the engine to answer (the first start reads the index into memory)")
    ready = install.wait_ready(a.port, lambda: True, timeout=600.0) == "ready"
    say("")
    if ready:
        say("Done. Open https://search.logout.cloud\n"
            "  on this computer: it is answered here, by your own engine\n"
            "  on your phone, on the same Wi-Fi: tap Pair, and it uses this computer from anywhere")
        try:
            import webbrowser
            webbrowser.open("https://search.logout.cloud/")
        except Exception:  # noqa: BLE001
            pass
    else:
        say(f"Installed, but the engine has not answered yet. Its log: {os.path.join(data, 'state', 'engine.log')}")
    return 0 if ready else 1


# ================================================================================================ autostart
def start_cmd(base: str, port: int) -> list:
    return program() + ["start", "--dest", base, "--port", str(port)]


def win_quote(parts: list) -> str:
    return " ".join(f'"{p}"' if (" " in p or not p) else p for p in parts)


def autostart_plan(on: bool, base: str, port: int) -> dict:
    """What `autostart` writes and runs on this platform: {"files": {path: content}, "remove": [path], "run": [[cmd]]}."""
    cmd = start_cmd(base, port)
    if PLATFORM == "linux":
        unit = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home(), ".config"), "systemd", "user", f"{SERVICE}.service")
        if on:
            text = ("[Unit]\nDescription=logout search engine\nAfter=network-online.target\n\n[Service]\n"
                    f"ExecStart={' '.join(shlex.quote(p) for p in cmd)}\nRestart=on-failure\nRestartSec=10\n\n"
                    "[Install]\nWantedBy=default.target\n")
            return {"files": {unit: text}, "remove": [],
                    "run": [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", f"{SERVICE}.service"]]}
        return {"files": {}, "remove": [unit],
                "run": [["systemctl", "--user", "disable", "--now", f"{SERVICE}.service"], ["systemctl", "--user", "daemon-reload"]]}
    if PLATFORM == "darwin":
        import plistlib
        plist = os.path.join(home(), "Library", "LaunchAgents", f"{HOST_NAME}.plist")
        uid = os.getuid() if hasattr(os, "getuid") else 501
        if on:
            log = os.path.join(base, "state", "engine.log")
            d = {"Label": HOST_NAME, "ProgramArguments": cmd, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
                 "StandardOutPath": log, "StandardErrorPath": log}
            return {"files": {plist: plistlib.dumps(d, sort_keys=True).decode()}, "remove": [],
                    "run": [["launchctl", "bootout", f"gui/{uid}/{HOST_NAME}"], ["launchctl", "bootstrap", f"gui/{uid}", plist]]}
        return {"files": {}, "remove": [plist], "run": [["launchctl", "bootout", f"gui/{uid}/{HOST_NAME}"]]}
    if PLATFORM == "win32":
        if on:
            return {"files": {}, "remove": [], "run": [["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", win_quote(cmd)]]}
        return {"files": {}, "remove": [], "run": [["schtasks", "/Delete", "/F", "/TN", TASK_NAME]]}
    raise SystemExit(f"autostart: unknown platform {PLATFORM}")


def apply_plan(p: dict, dry: bool, optional_ok=()) -> int:
    """Write the files, remove what is to go, run the commands. `optional_ok`: commands (by first two words) whose failure is
    not an error (bootout before bootstrap when nothing was loaded)."""
    for path, text in p["files"].items():
        print(f"write  {path}")
        if dry:
            print("\n".join("    " + line for line in text.rstrip("\n").split("\n")))
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    for path in p["remove"]:
        print(f"remove {path}")
        if not dry and os.path.lexists(path):
            os.remove(path)
    rc = 0
    for cmd in p["run"]:
        print("run    " + " ".join(shlex.quote(c) for c in cmd))
        if dry:
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as e:
            say(f"  {cmd[0]}: {e}")
            rc = 1
            continue
        if r.returncode and tuple(cmd[:2]) not in optional_ok:
            say(f"  exited {r.returncode}: {(r.stderr or r.stdout).strip()[:400]}")
            rc = 1
    return rc


def autostart(a) -> int:
    base = os.path.abspath(a.dest)
    on = a.state == "on"
    if on and not a.print and release_ptr.current_id(base) is None:
        say(f"refused: {base} holds no installed release (run `install` first)")
        return 2
    plan = autostart_plan(on, base, a.port)
    rc = apply_plan(plan, a.print, optional_ok=(("launchctl", "bootout"), ("systemctl", "--user")) if not on or PLATFORM == "darwin" else (("launchctl", "bootout"),))
    if not a.print and rc == 0:
        say("the engine starts at logon" if on else "the engine no longer starts at logon")
    return rc


# ================================================================================================ host (native messaging)
def host_dir(base: str) -> str:
    return os.path.join(base, "host")


def host_wrapper(base: str, port: int) -> tuple:
    """(path, content) of the script the browser starts. Native messaging allows no arguments in the manifest, so a
    one-line script carries them."""
    cmd = program() + ["host", "run", "--dest", base, "--port", str(port)]
    if PLATFORM == "win32":
        return os.path.join(host_dir(base), f"{HOST_NAME}.cmd"), "@echo off\r\n" + win_quote(cmd) + "\r\n"
    return os.path.join(host_dir(base), f"{HOST_NAME}.sh"), "#!/bin/sh\nexec " + " ".join(shlex.quote(p) for p in cmd) + "\n"


def manifest(path: str, firefox: bool) -> str:
    m = {"name": HOST_NAME, "description": "logout search: the engine on this computer", "path": path, "type": "stdio"}
    if firefox:
        m["allowed_extensions"] = [FIREFOX_ID]
    else:
        m["allowed_origins"] = [f"chrome-extension://{CHROME_ID}/"]
    return json.dumps(m, indent=2) + "\n"


def browser_dirs() -> list:
    """[(label, NativeMessagingHosts folder or registry key, firefox?)] for the browsers present on this machine."""
    import glob
    found = []
    if PLATFORM == "linux":
        cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home(), ".config")
        pats = ["google-chrome*", "chromium", "BraveSoftware/*", "microsoft-edge*", "vivaldi*"]
        for pat in pats:
            for d in sorted(glob.glob(os.path.join(cfg, pat))):
                if os.path.isdir(d):
                    found.append((os.path.relpath(d, cfg), os.path.join(d, "NativeMessagingHosts"), False))
        moz = os.path.join(home(), ".mozilla")
        if os.path.isdir(moz):
            found.append(("firefox", os.path.join(moz, "native-messaging-hosts"), True))
    elif PLATFORM == "darwin":
        sup = os.path.join(home(), "Library", "Application Support")
        for d in ("Google/Chrome", "Google/Chrome Beta", "Google/Chrome Canary", "Chromium", "BraveSoftware/Brave-Browser",
                  "BraveSoftware/Brave-Browser-Beta", "BraveSoftware/Brave-Browser-Nightly", "Microsoft Edge", "Vivaldi"):
            if os.path.isdir(os.path.join(sup, d)):
                found.append((d, os.path.join(sup, d, "NativeMessagingHosts"), False))
        if os.path.isdir(os.path.join(sup, "Firefox")):
            found.append(("firefox", os.path.join(sup, "Mozilla", "NativeMessagingHosts"), True))
    elif PLATFORM == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home(), "AppData", "Local")
        roaming = os.environ.get("APPDATA") or os.path.join(home(), "AppData", "Roaming")
        for parts in (("Google", "Chrome"), ("Chromium",), ("BraveSoftware", "Brave-Browser"), ("Microsoft", "Edge"), ("Vivaldi",)):
            if os.path.isdir(os.path.join(local, *parts)):      # the browser's profile folder under %LOCALAPPDATA%
                found.append(("\\".join(parts), f"HKCU\\Software\\{chr(92).join(parts)}\\NativeMessagingHosts\\{HOST_NAME}", False))
        if os.path.isdir(os.path.join(roaming, "Mozilla", "Firefox")):
            found.append(("firefox", f"HKCU\\Software\\Mozilla\\NativeMessagingHosts\\{HOST_NAME}", True))
    return found


def host_plan(on: bool, base: str, port: int) -> dict:
    """{"files": {path: content}, "remove": [path], "registry": {key: value}, "unregister": [key], "browsers": [label]}"""
    wrapper, content = host_wrapper(base, port)
    plan = {"files": {}, "remove": [], "registry": {}, "unregister": [], "browsers": []}
    dirs = browser_dirs()
    for label, where, firefox in dirs:
        plan["browsers"].append(label)
        if PLATFORM == "win32":
            jpath = os.path.join(host_dir(base), f"{HOST_NAME}.{'firefox' if firefox else 'chromium'}.json")
            if on:
                plan["files"][jpath] = manifest(wrapper, firefox)
                plan["registry"][where] = jpath
            else:
                plan["unregister"].append(where)
                plan["remove"].append(jpath)
        else:
            jpath = os.path.join(where, f"{HOST_NAME}.json")
            if on:
                plan["files"][jpath] = manifest(wrapper, firefox)
            else:
                plan["remove"].append(jpath)
    if on:
        plan["files"][wrapper] = content
    else:
        plan["remove"].append(wrapper)
    return plan


def registry_set(key: str, value: str, dry: bool) -> None:
    print(f"{'set' if value is not None else 'delete'}    {key}" + (f" = {value}" if value is not None else ""))
    if dry or os.name != "nt":
        return
    import winreg
    sub = key.split("HKCU\\", 1)[1]
    if value is None:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except FileNotFoundError:
            pass
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, value)


def host_register(a) -> int:
    base = os.path.abspath(a.dest)
    on = a.action == "register"
    plan = host_plan(on, base, a.port)
    if not plan["browsers"]:
        say("no supported browser profile found on this machine (Chrome, Chromium, Brave, Edge, Vivaldi, Firefox)")
        return 1
    rc = apply_plan({"files": plan["files"], "remove": plan["remove"], "run": []}, a.print)
    for key, value in plan["registry"].items():
        registry_set(key, value, a.print)
    for key in plan["unregister"]:
        registry_set(key, None, a.print)
    if on and not a.print:
        for path in plan["files"]:
            if path.endswith(".sh"):
                os.chmod(path, 0o755)
    say(("registered with: " if on else "removed from: ") + ", ".join(plan["browsers"]))
    if on and not a.print:
        say("now install the logout extension and open https://search.logout.cloud: the badge next to the logo reads 'local'")
    return rc


def http_answer(port: int, nh):
    """native_host's `answer` over HTTP to the running engine (one process keeps the indexes and models warm)."""
    import urllib.parse
    import urllib.request

    def get(path: str, qs: dict) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}?{urllib.parse.urlencode(qs)}", timeout=30) as r:
            return json.load(r)

    def answer(q: str, results: bool, full: bool = False, near=None) -> dict:
        qs = {"q": q}
        if near:
            qs["near"] = near
        if not results:
            qs["results"] = "0"
        elif full:
            qs["full"] = "1"
        return nh.trim(get("/instant", qs))

    def more_news(q: str, offset: int) -> dict:
        d = get("/more", {"q": q, "what": "news", "offset": str(offset)})
        return {"news": d.get("news") or [], "more": d.get("more") is True}

    def explain(q: str, near=None) -> dict:
        return get("/explain", {"q": q, **({"near": near} if near else {})})

    answer.more_news = more_news
    answer.explain = explain
    return answer


def host_run(a) -> None:
    base = os.path.abspath(a.dest)
    sys.path.insert(0, engine_dir())
    import native_host as nh
    os.environ.setdefault("LOGOUT_SETTINGS", os.path.join(base, "state", "settings.json"))   # the page's settings, shared with the engine
    out = nh.private_stdout()

    def details() -> dict:
        return {"engine": nh.friendly_name(), "ip": nh.lan_ip(), "via": "",
                "host_version": nh.VERSION, "release": release_ptr.current_id(base), "data_gb": data_gb(base),
                "engine_up": install._readyz(a.port) == 200}

    nh.serve(sys.stdin.buffer, out, http_answer(a.port, nh), info=details)


# ================================================================================================ passthroughs
def run_install(argv: list) -> None:
    sys.argv = ["install.py"] + argv
    install.main()


def run_update(a, rest: list) -> int:
    import update_fetch
    cmd = program() + ["_update-apply"]
    os.environ["LOGOUT_UPDATE_APPLY_CMD"] = json.dumps(cmd)      # update_fetch runs update_apply through this program
    os.environ["LOGOUT_UPDATE_SELF_CMD"] = json.dumps(cmd)       # and update_apply's row-diff workers too
    sys.argv = ["update_fetch.py", "check" if a.check else "fetch", "--base", a.dest, "--source", a.source, "--pubkey", a.pubkey] + \
        ([] if a.check else ["--apply"]) + rest
    return update_fetch.main()


def run_update_apply(argv: list) -> None:
    import update_apply
    os.environ.setdefault("LOGOUT_UPDATE_SELF_CMD", json.dumps(program() + ["_update-apply"]))
    sys.argv = ["update_apply.py"] + argv
    update_apply.main()


def version(a) -> None:
    info = {"launcher": VERSION, "platform": PLATFORM, "frozen": FROZEN, "python": platform.python_version(),
            "engine_dir": engine_dir(), "program": program()}
    if a.dest:
        b = os.path.abspath(a.dest)
        info["release"] = release_ptr.current_id(b)
        info["previous"] = release_ptr.current_id(b, "previous")
    print(json.dumps(info, indent=1, sort_keys=True))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="logout-search", description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("install", add_help=False)
    sp.add_parser("status", add_help=False)
    sp.add_parser("addon", add_help=False)
    s = sp.add_parser("start")
    s.add_argument("--dest", required=True)
    s.add_argument("--port", type=int, default=8090)
    s.add_argument("--print-env", action="store_true")
    s.add_argument("--format", choices=["posix", "cmd", "powershell", "json"])
    s.add_argument("--no-smoke", action="store_true", help="skip the three fixed searches run once the engine reports ready")
    s.add_argument("--no-peer", action="store_true", help="do not let other devices pair with this engine")
    u = sp.add_parser("update")
    u.add_argument("--dest", required=True)
    u.add_argument("--source", required=True)
    u.add_argument("--pubkey", required=True, help="the PEM file of the pinned public key")
    u.add_argument("--check", action="store_true", help="say whether an update exists; download nothing")
    au = sp.add_parser("autostart")
    au.add_argument("state", choices=["on", "off"])
    au.add_argument("--dest", required=True)
    au.add_argument("--port", type=int, default=8090)
    au.add_argument("--print", action="store_true", help="show what would be written and run; change nothing")
    h = sp.add_parser("host")
    h.add_argument("action", choices=["register", "unregister", "run"])
    h.add_argument("--dest", required=True)
    h.add_argument("--port", type=int, default=8090)
    h.add_argument("--print", action="store_true")
    v = sp.add_parser("version")
    v.add_argument("--dest")
    pe = sp.add_parser("peer")
    pe.add_argument("--dest")
    pe.add_argument("--port", type=int, default=8090)
    pe.add_argument("--check", action="store_true", help="say whether this program carries what the peer needs; connect to nothing")
    se = sp.add_parser("setup")
    se.add_argument("--dest", help="the one folder everything goes under (default: a logout-search folder in your home's application data)")
    se.add_argument("--port", type=int, default=8090)
    se.add_argument("--print", action="store_true", help="show the steps; change nothing")
    se.add_argument("--copied", action="store_true", help=argparse.SUPPRESS)
    sp.add_parser("engine", add_help=False)             # internal: run search_api.py (what `start` spawns)
    sp.add_parser("_update-apply", add_help=False)      # internal: update_apply.py's main (what `update` spawns)
    argv = sys.argv[1:] if argv is None else argv
    if not argv and FROZEN:
        # a double-click: no terminal, nothing typed. Set everything up, and keep the window open at the end
        rc = main(["setup"])
        pause_if_double_clicked()
        return rc
    if argv and argv[0] in ("install", "status", "addon", "engine", "_update-apply"):
        if argv[0] == "engine":
            if argv[1:] == ["--check-imports"]:
                return check_imports()
            run_engine()
            return 0
        if argv[0] == "_update-apply":
            run_update_apply(argv[1:])
            return 0
        run_install(argv)
        return 0
    a, rest = ap.parse_known_args(argv)
    if a.cmd == "start":
        start(a)
    elif a.cmd == "update":
        return run_update(a, rest)
    elif a.cmd == "autostart":
        return autostart(a)
    elif a.cmd == "host":
        if a.action == "run":
            host_run(a)
        else:
            return host_register(a)
    elif a.cmd == "version":
        version(a)
    elif a.cmd == "peer":
        if a.check:
            return peer_check()
        if not a.dest:
            ap.error("peer: --dest is needed")
        return peer_run(a)
    elif a.cmd == "setup":
        return setup(a)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except install.InstallError as e:
        print(f"logout-search: REFUSED: {e}", file=sys.stderr)
        sys.exit(2)
