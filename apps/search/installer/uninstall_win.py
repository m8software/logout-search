#!/usr/bin/env python3
"""Windows uninstall, the parts that can be tested anywhere (Standard library only).

Why `logout-search uninstall --yes` exited 1 and removed nothing (buildbox, 2026-11-17), from the code, not guessed:
  1. the command only called `logout-search-tray.exe --uninstall`, which ignores --yes and always asks with a message box: a caller
     with no one at the screen (ssh, a script) got an unanswered or refused dialog;
  2. when the tray did go on it ran `taskkill /F /T /IM logout-search.exe`: by IMAGE NAME, so it killed the command that had started it
     (exit code 1) and every OTHER install's engine on the computer, and
  3. `unregister()` deleted the Run key and the Settings entry of whatever install was registered, whichever folder was being removed.
So: the command does the work itself; it stops only processes whose program file is under the folder being removed (never itself), and it
removes a registration only when that registration points at this folder.
"""
from __future__ import annotations

import json
import os
import subprocess


def _n(p: str) -> str:
    """A Windows path in one spelling: lower case, backslashes, no trailing slash (plain string work, so it runs anywhere)."""
    return (p or "").replace("/", "\\").lower().rstrip("\\")


def under(path: str, base: str) -> bool:
    """path is inside base (case-insensitive, either slash; a folder with a longer, similar name is not)."""
    a, b = _n(path), _n(base)
    return bool(a) and bool(b) and (a == b or a.startswith(b + "\\"))


def plan_kill(base: str, procs: list, keep_pids=()) -> list:
    """Process ids to stop: (pid, exe path) pairs whose program file is under base, the tray first (it would start the engine again),
    never a pid in keep_pids (this very command)."""
    keep = {int(p) for p in keep_pids}
    mine = [(int(pid), exe) for pid, exe in procs if exe and under(exe, base) and int(pid) not in keep]
    mine.sort(key=lambda t: (0 if "tray" in os.path.basename(t[1]).lower() else 1, t[0]))
    return [pid for pid, _ in mine]


def registered_for(base: str, run_value: str) -> bool:
    """The Run-key command (or an uninstall entry's InstallLocation) belongs to this folder."""
    return bool(run_value) and (_n(base) + "\\") in (_n(run_value) + "\\")


def list_processes() -> list:
    """[(pid, exe path)] of the running programs that have a file path (PowerShell, Windows)."""
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath } | ForEach-Object { '{0}|{1}' -f $_.ProcessId, $_.ExecutablePath }"],
                       capture_output=True, text=True, timeout=60, creationflags=0x08000000)
    out = []
    for ln in (r.stdout or "").splitlines():
        if "|" in ln:
            pid, exe = ln.split("|", 1)
            if pid.strip().isdigit():
                out.append((int(pid), exe.strip()))
    return out


def stop_under(base: str, keep_pids=None, procs=None, run=subprocess.run) -> list:
    """taskkill /F /T /PID for each process of plan_kill. -> the pids asked to stop."""
    keep = set(keep_pids or ()) | {os.getpid()}
    pids = plan_kill(base, procs if procs is not None else list_processes(), keep)
    for pid in pids:
        run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=0x08000000)
    return pids


def remove_folder_later(base: str, popen=subprocess.Popen) -> None:
    """The program folder holds the running program: a hidden shell removes it a few seconds after we exit."""
    popen(f'cmd /c ping -n 4 127.0.0.1 >nul & rmdir /s /q "{base}"', creationflags=0x08000000 | 0x00000008,
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
