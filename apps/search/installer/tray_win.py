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


def shortcut(path: str, target: str, args: str) -> bool:
    """A .lnk through PowerShell's WScript.Shell (present on every Windows 10 and 11)."""
    q = lambda s: s.replace("'", "''")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(path)}');$s.TargetPath='{q(target)}';"
          f"$s.Arguments='{q(args)}';$s.WorkingDirectory='{q(os.path.dirname(target))}';$s.IconLocation='{q(target)},0';$s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    return r.returncode == 0 and os.path.isfile(path)


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
MF_STRING, MF_GRAYED, MF_SEPARATOR, MF_CHECKED = 0, 1, 0x800, 0x8
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
ID_OPEN, ID_RESTART, ID_UNINSTALL, ID_QUIT, ID_UPDATE_NOW, ID_UPDATE_AUTO = 1, 2, 3, 4, 5, 6
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


class Tray:
    def __init__(self, engine, selftest: bool = False):
        self.engine, self.selftest = engine, selftest
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
        m = user32.CreatePopupMenu()
        st = self.engine.state() if self.engine else "ready"
        user32.AppendMenuW(m, MF_STRING | MF_GRAYED, 0, {"ready": "Ready", "starting": "Starting…", "stopped": "Stopped"}[st])
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, ID_OPEN, "Open search")
        user32.AppendMenuW(m, MF_STRING, ID_RESTART, "Restart the engine")
        if self.engine:
            # the daily update (autoupdate.py, run by the engine's own process): its last result, check now, on/off
            s = au.load(self.engine.data)
            user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(m, MF_STRING | MF_GRAYED, 0, "Updates: " + (
                "checking\u2026" if s.get("requested") else (s.get("last_result") or "not checked yet")))
            user32.AppendMenuW(m, MF_STRING, ID_UPDATE_NOW, "Check for updates now")
            user32.AppendMenuW(m, MF_STRING | (MF_CHECKED if au.enabled(s) else 0), ID_UPDATE_AUTO, "Update automatically")
            user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, ID_UNINSTALL, "Uninstall…")
        user32.AppendMenuW(m, MF_STRING, ID_QUIT, "Quit (until the next sign-in)")
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
        elif cmd in (ID_UPDATE_NOW, ID_UPDATE_AUTO) and self.engine:
            au.command("now" if cmd == ID_UPDATE_NOW else ("off" if au.enabled(au.load(self.engine.data)) else "on"), self.engine.data)
        elif cmd == ID_UNINSTALL:
            if uninstall(self.engine.data if self.engine else None, True, self.engine):
                self.quit(stop_engine=False)
        elif cmd == ID_QUIT:
            self.quit()

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
