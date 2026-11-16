#!/usr/bin/env python3
"""logout-search-tray.exe: logout search on Windows as a normal program (owner 2026-11-14: "it should just run
automatically, and restart on startup, and be a tray icon, and be something they can click on in the start menu to run
or uninstall").

    logout-search-tray.exe [--dest BASE] [--port 8090]   the tray icon; it runs the engine hidden and restarts it if it stops
    logout-search-tray.exe --uninstall [--dest BASE]     asks, then removes everything (Settings > Apps runs this)
    logout-search-tray.exe --selftest                    shows the icon for a moment and exits 0 (the build's own test)

It is built windowed (no console), so starting at logon shows nothing but the icon. Everything it registers is per user
(HKCU and the user's Start menu): no administrator rights, which Task Scheduler needed and a normal account lacks
(owner's first install: `schtasks /Create` -> "Access is denied").

Standard library only (ctypes): no third-party tray package, so nothing more to license or ship.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from ctypes import wintypes

import autoupdate as au                                   # imported here so the build's --selftest proves it is bundled
import adminpin
import homesettings as hs

APP = "logout search"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\logout-search"
NATIVE_KEYS = [r"Software\Google\Chrome\NativeMessagingHosts\cloud.logout.search",
               r"Software\Chromium\NativeMessagingHosts\cloud.logout.search",
               r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\cloud.logout.search",
               r"Software\Microsoft\Edge\NativeMessagingHosts\cloud.logout.search",
               r"Software\Vivaldi\NativeMessagingHosts\cloud.logout.search",
               r"Software\Mozilla\NativeMessagingHosts\cloud.logout.search"]
SEARCH_URL = "https://search.logout.cloud/"
CREATE_NO_WINDOW = 0x08000000


def out(text: str) -> None:
    """A windowed build has no console: sys.stdout is None there, and printing would raise."""
    if sys.stdout is not None:
        print(text, flush=True)


def default_base() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"), "logout-search")


def here() -> str:
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def engine_exe() -> str:
    return os.path.join(here(), "logout-search.exe")


def start_menu_dir() -> str:
    return os.path.join(os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming"),
                        "Microsoft", "Windows", "Start Menu", "Programs", APP)


def arg(name: str, default=None):
    a = sys.argv[1:]
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


# ================================================================================================ registration
def register(base: str, port: int) -> list:
    """Start at logon, the Start menu folder, and the entry in Settings > Apps. Per user, no administrator. -> what failed."""
    import winreg
    tray = os.path.join(here(), "logout-search-tray.exe")
    run = f'"{tray}" --dest "{base}" --port {port}'
    bad = []
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, run)
    except OSError as e:
        bad.append(f"start at logon: {e}")
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            for name, val in (("DisplayName", APP), ("Publisher", "M8 Software LLC"), ("DisplayIcon", tray),
                              ("InstallLocation", base), ("UninstallString", f'"{tray}" --uninstall --dest "{base}"'),
                              ("URLInfoAbout", "https://logout.cloud/download")):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
    except OSError as e:
        bad.append(f"Settings > Apps entry: {e}")
    d = start_menu_dir()
    os.makedirs(d, exist_ok=True)
    for name, target, args in ((APP, tray, f'--dest "{base}" --port {port}'),
                               (f"Uninstall {APP}", tray, f'--uninstall --dest "{base}"')):
        if not shortcut(os.path.join(d, name + ".lnk"), target, args):
            bad.append(f"Start menu: {name}")
    return bad


class GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, s: str):
        import uuid
        u = uuid.UUID(s)
        super().__init__(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))


def _com(ptr, index: int, *args) -> int:
    """Call method `index` of a COM interface pointer (its vtable), the arguments given as ctypes values."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *[type(a) for a in args])(vtbl[index])
    return fn(ptr, *args)


def shortcut(path: str, target: str, args: str) -> bool:
    """A .lnk made through Windows' own shell interface (IShellLinkW, IPersistFile), called with ctypes. No PowerShell
    (2026-11-14: Defender took the tray for a trojan, Bearfoos.A!ml; a hidden PowerShell with its policy bypassed is one
    of the things that model weighs)."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 2)                              # apartment-threaded; already done is fine
    clsid, iid_link, iid_persist = (GUID(g) for g in ("00021401-0000-0000-C000-000000000046",
                                                      "000214F9-0000-0000-C000-000000000046", "0000010B-0000-0000-C000-000000000046"))
    addr = lambda o: ctypes.c_void_p(ctypes.addressof(o))       # a pointer argument, as a plain address
    link, persist = ctypes.c_void_p(), ctypes.c_void_p()
    if ole32.CoCreateInstance(addr(clsid), None, 1, addr(iid_link), addr(link)) != 0:
        return False
    try:
        W = ctypes.c_wchar_p
        for index, value in ((20, target), (11, args), (9, os.path.dirname(target))):   # SetPath, SetArguments, SetWorkingDirectory
            if _com(link, index, W(value)) != 0:
                return False
        _com(link, 17, W(target), ctypes.c_int(0))            # SetIconLocation
        if _com(link, 0, addr(iid_persist), addr(persist)) != 0:   # QueryInterface: IPersistFile
            return False
        return _com(persist, 6, W(path), ctypes.c_int(1)) == 0 and os.path.isfile(path)   # Save
    finally:
        for p in (persist, link):
            if p.value:
                _com(p, 2)                                     # Release


def unregister() -> None:
    import shutil
    import winreg
    for root_key, sub, value in [(winreg.HKEY_CURRENT_USER, RUN_KEY, APP)]:
        try:
            with winreg.OpenKey(root_key, sub, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, value)
        except OSError:
            pass
    for sub in [UNINSTALL_KEY] + NATIVE_KEYS:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass
    shutil.rmtree(start_menu_dir(), ignore_errors=True)


# ================================================================================================ the engine
class Engine:
    """Runs `logout-search.exe start` with no window, and starts it again if it stops (not when we stop it)."""

    def __init__(self, data: str, port: int):
        self.data, self.port, self.proc, self.stopping, self.lock = data, port, None, False, threading.Lock()
        self.log = os.path.join(data, "state", "engine.log")

    def start(self) -> None:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            os.makedirs(os.path.dirname(self.log), exist_ok=True)
            out = open(self.log, "ab")
            self.proc = subprocess.Popen([engine_exe(), "start", "--dest", self.data, "--port", str(self.port), "--no-smoke"],
                                         stdout=out, stderr=out, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)

    def stop(self) -> None:
        with self.lock:
            self.stopping = True
            p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            # the whole tree: `start` runs the engine and the peer as its children
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, creationflags=CREATE_NO_WINDOW)

    def restart(self) -> None:
        self.stop()
        self.stopping = False
        self.start()

    def watch(self) -> None:
        delay = 5
        while True:
            time.sleep(delay)
            p = self.proc
            if not self.stopping and (p is None or p.poll() is not None):
                self.start()
                delay = min(delay * 2, 300)          # one that keeps failing is retried less and less often
            elif p is not None and p.poll() is None:
                delay = 5

    def state(self) -> str:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/readyz", timeout=2) as r:
                return "ready" if r.status == 200 else "starting"
        except Exception:  # noqa: BLE001
            p = self.proc
            return "starting" if p is not None and p.poll() is None else "stopped"


# ================================================================================================ the tray icon (Win32)
user32, shell32, kernel32 = (ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32) if os.name == "nt" else (None,) * 3
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM) if os.name == "nt" else None
WM_USER, WM_COMMAND, WM_DESTROY, WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0400, 0x0111, 0x0002, 0x0202, 0x0205, 0x0203
WM_TRAY, WM_TIMER = WM_USER + 1, 0x0113
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_GRAYED, MF_SEPARATOR, MF_CHECKED, MF_POPUP = 0, 1, 0x800, 0x8, 0x10
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
ID_OPEN, ID_RESTART, ID_UNINSTALL, ID_QUIT, ID_UPDATE_NOW, ID_UPDATE_AUTO = 1, 2, 3, 4, 5, 6
# the settings menus (owner 2026-11-14: "should all be configurable in the tray settings, set update times or check if
# you want them at all"): what each choice writes is in choose()
ID_NEWS, ID_WEATHER, ID_SPORTS, ID_ADDONS, ID_CHANNEL = 10, 20, 30, 42, 43
NEWS_IDS = {11: 1, 12: 2, 13: 4}                           # news_every
WEATHER_IDS = {21: 1, 22: 3, 23: 6}                        # weather_every_h
WINDOW_IDS = {50: 1, 51: 3, 52: 5, 53: 12, 54: 19, 55: 23}  # at_hour: the start of the daily check's two hours
ID_PIN_SET, ID_PIN_CLEAR = 61, 62                          # the admin PIN (adminpin.py): set or remove, no PIN asked here
PROFILE_IDS = {70: "auto", 71: "full", 72: "balanced", 73: "light", 74: "search"}   # Data use (homesettings.PROFILES)
WINDOW_TEXT = {1: "1 to 3 a.m.", 3: "3 to 5 a.m.", 5: "5 to 7 a.m.", 12: "noon to 2 p.m.", 19: "7 to 9 p.m.", 23: "11 p.m. to 1 a.m."}
MB_YESNO, MB_ICONQUESTION, MB_OK, MB_ICONINFO, IDYES = 0x4, 0x20, 0x0, 0x40, 6


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC if WNDPROC else ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


def _menu_types() -> None:
    """HMENU is a pointer: without these, ctypes passes and returns it as a 32-bit int."""
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
    user32.DestroyMenu.argtypes = [wintypes.HMENU]


class Tray:
    def __init__(self, engine, selftest: bool = False):
        self.engine, self.selftest = engine, selftest
        _menu_types()
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        self._proc = WNDPROC(self.wndproc)                # kept: a collected callback would crash the window
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName="logout-search-tray")
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, "logout-search-tray", APP, 0, 0, 0, 0, 0, None, None, hinst, None)
        icon = user32.LoadImageW(None, os.path.join(here(), "logout-search.ico"), 1, 0, 0, 0x10 | 0x40)   # LR_LOADFROMFILE|LR_DEFAULTSIZE
        if not icon:
            icon = shell32.ExtractIconW(hinst, sys.executable, 0) or user32.LoadIconW(None, ctypes.c_void_p(32512))
        self.nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                                   uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY, hIcon=icon)
        self.nid.szTip = f"{APP}: starting"
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid))
        user32.SetTimer(self.hwnd, 1, 5000, None)
        self.last = None

    def tip(self) -> None:
        st = self.engine.state() if self.engine else "ready"
        if st != self.last:
            self.last = st
            self.nid.szTip = {"ready": f"{APP}: ready", "starting": f"{APP}: starting (loading the index)",
                              "stopped": f"{APP}: stopped"}[st]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def menu(self) -> None:
        m = self.build_menu(self.engine.data if self.engine else None)
        self.show_menu(m)

    def build_menu(self, data):
        """The whole menu, its submenus included (the build's --selftest builds it too, without showing it)."""
        m = user32.CreatePopupMenu()
        st = self.engine.state() if self.engine else "ready"
        add = user32.AppendMenuW
        add(m, MF_STRING | MF_GRAYED, 0, {"ready": "Ready", "starting": "Starting…", "stopped": "Stopped"}[st])
        add(m, MF_SEPARATOR, 0, None)
        add(m, MF_STRING, ID_OPEN, "Open search")
        add(m, MF_STRING, ID_RESTART, "Restart the engine")
        if data:
            add(m, MF_SEPARATOR, 0, None)
            for title, items in settings_menus(data):
                sub = user32.CreatePopupMenu()              # destroyed with m (DestroyMenu takes its submenus)
                for flags, cid, text in items:
                    add(sub, flags, cid, text)
                add(m, MF_POPUP, sub, title)
            add(m, MF_SEPARATOR, 0, None)
        add(m, MF_STRING, ID_UNINSTALL, "Uninstall…")
        user32.AppendMenuW(m, MF_STRING, ID_QUIT, "Quit (until the next sign-in)")
        return m

    def show_menu(self, m) -> None:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        user32.DestroyMenu(m)
        self.command(cmd)

    def command(self, cmd: int) -> None:
        if cmd == ID_OPEN:
            webbrowser.open(SEARCH_URL)
        elif cmd == ID_RESTART and self.engine:
            threading.Thread(target=self.engine.restart, daemon=True).start()
        elif cmd == ID_UNINSTALL:
            if uninstall(self.engine.data if self.engine else None, True, self.engine):
                self.quit(stop_engine=False)
        elif cmd == ID_QUIT:
            self.quit()
        elif self.engine and cmd:                          # a settings choice (settings_menus)
            choose(self.engine.data, cmd)

    def quit(self, stop_engine: bool = True) -> None:
        if stop_engine and self.engine:
            self.engine.stop()
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
        user32.DestroyWindow(self.hwnd)

    def wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY and lparam in (WM_RBUTTONUP, WM_LBUTTONUP):
            self.menu()
            return 0
        if msg == WM_TRAY and lparam == WM_LBUTTONDBLCLK:
            webbrowser.open(SEARCH_URL)
            return 0
        if msg == WM_TIMER:
            if self.selftest:
                self.quit(stop_engine=False)
            else:
                self.tip()
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def loop(self) -> None:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def settings_menus(data: str) -> list:
    """[(submenu title, [(flags, id, text)])]: what the collectors fetch and how often (homesettings.py), and the daily
    update (autoupdate.py). The figures are what one install downloads a day: measured on testbox on 2026-11-14 with
    v0.4.6 (gzip on), 470 MB a day for everything but NOAA's forecast bulletins, which are 28 MB an hourly run and 21 MB
    a six-hourly one."""
    c, u = hs.load(data), au.load(data)
    eff = hs.effective(data)
    if eff["profile"] != "custom":                         # Customize shows what the preset in force does
        c = dict(c, **({"news": False, "sports": False, "weather": False} if eff["applied"] == "search" else hs._custom_from(eff["applied"])))
    tick = lambda on: MF_CHECKED if on else 0
    off_grey = lambda on: 0 if on else MF_GRAYED
    use = [(MF_STRING | tick(eff["profile"] == p), i, f"{hs.PROFILE_TEXT[p][0]}  ({hs.PROFILE_TEXT[p][1]})") for i, p in PROFILE_IDS.items()]
    if eff["profile"] == "custom":
        use.append((MF_STRING | MF_CHECKED | MF_GRAYED, 0, "Custom (Customize, below)"))
    if eff["profile"] == "auto":
        use += [(MF_SEPARATOR, 0, None), (MF_STRING | MF_GRAYED, 0, "Now: " + hs.PROFILE_TEXT[eff["applied"]][0]
                 + (", this connection is metered" if eff["metered"] else ""))]
    news = [(MF_STRING | tick(c["news"]), ID_NEWS, "Headlines and your own feeds"), (MF_SEPARATOR, 0, None)]
    news += [(MF_STRING | tick(c["news_every"] == v) | off_grey(c["news"]), i, t) for i, v, t in (
        (11, 1, "Every feed at its own pace (about 330 MB a day)"), (12, 2, "Half as often (about 170 MB a day)"),
        (13, 4, "A quarter as often (about 85 MB a day)"))]
    weather = [(MF_STRING | tick(c["weather"]), ID_WEATHER, "US weather from NOAA"), (MF_SEPARATOR, 0, None)]
    weather += [(MF_STRING | tick(c["weather_every_h"] == v) | off_grey(c["weather"]), i, t) for i, v, t in (
        (21, 1, "Forecast every hour (about 830 MB a day)"), (22, 3, "Forecast every 3 hours (about 390 MB a day)"),
        (23, 6, "Forecast every 6 hours (about 270 MB a day)"))]
    sports = [(MF_STRING | tick(c["sports"]), ID_SPORTS, "Scores and standings (about 55 MB a day)")]
    upd = [(MF_STRING | MF_GRAYED, 0, "Last check: " + ("checking\u2026" if u.get("requested") else (u.get("last_result") or "not yet"))),
           (MF_STRING | MF_GRAYED, 0, "Channel: " + au.channel_of(u) + (" (new versions arrive a day early)" if au.channel_of(u) == "beta" else "")),
           (MF_STRING | tick(au.channel_of(u) == "beta"), ID_CHANNEL, "Get new versions early (beta channel)"),
           (MF_STRING, ID_UPDATE_NOW, "Check for updates now"), (MF_SEPARATOR, 0, None),
           (MF_STRING | tick(au.enabled(u)), ID_UPDATE_AUTO, "Update automatically"),
           (MF_STRING | tick(u.get("addons", True) is not False) | off_grey(au.enabled(u)), ID_ADDONS,
            "Add-ons: Wikipedia links and how-to answers (6 GB)"),
           (MF_SEPARATOR, 0, None), (MF_STRING | MF_GRAYED, 0, "Check once a day between")]
    upd += [(MF_STRING | tick(au.at_hour(u) == h) | off_grey(au.enabled(u)), i, WINDOW_TEXT[h]) for i, h in WINDOW_IDS.items()]
    pin = adminpin.get(data)
    admin = [(MF_STRING | MF_GRAYED, 0, f"PIN: {pin}" if pin else "No PIN set: settings change only on this computer"),
             (MF_STRING | MF_GRAYED, 0, "With it, a paired device changes these settings"),
             (MF_SEPARATOR, 0, None), (MF_STRING, ID_PIN_SET, "Set a new PIN\u2026"),
             (MF_STRING | (0 if pin else MF_GRAYED), ID_PIN_CLEAR, "Remove the PIN")]
    custom = [(MF_STRING | MF_GRAYED, 0, "News")] + news + [(MF_SEPARATOR, 0, None), (MF_STRING | MF_GRAYED, 0, "Weather")] + weather \
        + [(MF_SEPARATOR, 0, None)] + sports
    return [("Data use", use), ("Customize", custom), ("Updates", upd), ("Admin PIN", admin)]


def set_pin_from_tray(data: str) -> None:
    """A small window of our own program asks for the new PIN (`logout-search admin pin ask`): no PowerShell."""
    subprocess.Popen([engine_exe(), "admin", "pin", "ask", "--dest", data], creationflags=0x00000010)   # CREATE_NEW_CONSOLE


def choose(data: str, cmd: int) -> None:
    """A settings choice from the tray: written at once; the engine's process applies it within a minute."""
    c, u = hs.load(data), au.load(data)
    if cmd in PROFILE_IDS:
        hs.set_value(data, "profile", PROFILE_IDS[cmd])
    elif cmd in (ID_NEWS, ID_WEATHER, ID_SPORTS):
        key = {ID_NEWS: "news", ID_WEATHER: "weather", ID_SPORTS: "sports"}[cmd]
        hs.set_value(data, key, not hs.effective(data)["on"][key])     # what runs now, not what a preset left in the file
    elif cmd in NEWS_IDS:
        hs.set_value(data, "news_every", NEWS_IDS[cmd])
    elif cmd in WEATHER_IDS:
        hs.set_value(data, "weather_every_h", WEATHER_IDS[cmd])
    elif cmd == ID_UPDATE_NOW:
        au.command("now", data)
    elif cmd == ID_UPDATE_AUTO:
        au.command("off" if au.enabled(u) else "on", data)
    elif cmd == ID_CHANNEL:
        au.command("channel", data, "stable" if au.channel_of(u) == "beta" else "beta")
    elif cmd == ID_ADDONS:
        au.command("addons", data, "off" if u.get("addons", True) is not False else "on")
    elif cmd in WINDOW_IDS:
        au.command("at", data, WINDOW_IDS[cmd])
    elif cmd == ID_PIN_SET:
        threading.Thread(target=set_pin_from_tray, args=(data,), daemon=True).start()   # the box must not hold the icon
    elif cmd == ID_PIN_CLEAR:
        adminpin.clear(data)


def confirm(text: str) -> bool:
    return user32.MessageBoxW(None, text, APP, MB_YESNO | MB_ICONQUESTION) == IDYES


def tell(text: str) -> None:
    user32.MessageBoxW(None, text, APP, MB_OK | MB_ICONINFO)


def uninstall(data, ask_first: bool = True, engine=None) -> bool:
    """Stop everything, remove what was registered, then the folder (after this program has exited)."""
    base = os.path.dirname(data) if data and os.path.basename(data) == "data" else (data or default_base())
    if ask_first and not confirm(
            f"Remove logout search from this computer?\n\nThis deletes the program and its search data ({base}).\n"
            "Your other devices will no longer be able to use it."):
        return False
    if engine:
        engine.stop()
    # anything else of ours still running (a second tray, a `start` from a terminal)
    for image in ("logout-search.exe",):
        subprocess.run(["taskkill", "/F", "/T", "/IM", image], capture_output=True, creationflags=CREATE_NO_WINDOW)
    unregister()
    # the folder holds this very program: a hidden shell removes it a few seconds after we exit
    subprocess.Popen(f'cmd /c ping -n 4 127.0.0.1 >nul & rmdir /s /q "{base}"', creationflags=CREATE_NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tell("logout search has been removed.")
    return True


def single_instance() -> bool:
    kernel32.CreateMutexW(None, False, "Local\\logout-search-tray")
    return kernel32.GetLastError() != 183                 # ERROR_ALREADY_EXISTS


def main() -> int:
    if os.name != "nt":
        out("tray_win: Windows only")
        return 2
    if "--selftest" in sys.argv:
        t = Tray(None, selftest=True)
        import tempfile                                    # the full menu with its submenus, built and destroyed unseen
        with tempfile.TemporaryDirectory() as d:
            user32.DestroyMenu(t.build_menu(d))
            lnk = os.path.join(d, "selftest.lnk")              # a Start menu shortcut, made the way register() makes them
            if not shortcut(lnk, sys.executable, "--selftest"):
                out("tray selftest: the shortcut was not made")
                return 1
        user32.SetTimer(t.hwnd, 2, 1500, None)
        t.loop()
        out("tray ok")
        return 0
    base = os.path.abspath(arg("--dest") or default_base())
    data = os.path.join(base, "data")
    if "--uninstall" in sys.argv:
        uninstall(data)
        return 0
    if "--register" in sys.argv:
        bad = register(base, int(arg("--port", "8090")))
        out("\n".join(bad) if bad else "registered")
        return 1 if bad else 0
    if not single_instance():
        webbrowser.open(SEARCH_URL)                       # already running: clicking it again opens the search page
        return 0
    eng = Engine(data, int(arg("--port", "8090")))
    eng.start()
    threading.Thread(target=eng.watch, daemon=True).start()
    Tray(eng).loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
