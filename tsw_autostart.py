"""
Train Sim World 7 joystick bridge - open the bridge window by itself whenever the game starts.

Runs out of sight from Windows sign-in and looks every few seconds for Train Sim World. When the game starts the
bridge window (tsw_joystick_ui.py) opens, and it closes itself once the game has exited. Close the window while
you play and it stays closed until the next time the game starts. If a bridge window is already open (you started
it by hand), no second one is opened.

Usage:
    python tsw_autostart.py --install      turn it on: watch from now on, and from every Windows sign-in
    python tsw_autostart.py --uninstall    turn it off
    pythonw tsw_autostart.py               watch for the game (what runs at sign-in)

    (or double-click "Auto-start with TSW - on.bat" / "Auto-start with TSW - off.bat")
"""

import ctypes
import os
import subprocess
import sys
import time
import traceback
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.abspath(__file__)
UI_SCRIPT = os.path.join(HERE, "tsw_joystick_ui.py")

BRIDGE_TITLE = "TSW7 Joystick Bridge"           # the bridge window's title
GAME_EXE = "trainsimworld"                      # the game's process name contains this (lower case)
CHECK_SECONDS = 3                               # how often to look for the game
GONE_SECONDS = 10                               # the game has exited once it's been gone this long

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "TSW7 Joystick Bridge"
LOCK_NAME = "Local\\TSW7JoystickAutostart"      # held while watching, so only one copy watches
STOP_NAME = "Local\\TSW7JoystickAutostartStop"  # set to make the copy that's watching stop


# ---------------------------------------------------------------- Windows
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value
EVENT_MODIFY_STATE = 0x2
WAIT_OBJECT_0, WAIT_ABANDONED, WAIT_TIMEOUT = 0x0, 0x80, 0x102


def _api(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype, f.argtypes = restype, argtypes
    return f


if os.name == "nt":
    _k32, _u32 = ctypes.WinDLL("kernel32"), ctypes.WinDLL("user32")
    CreateToolhelp32Snapshot = _api(_k32, "CreateToolhelp32Snapshot", wintypes.HANDLE,
                                    wintypes.DWORD, wintypes.DWORD)
    Process32FirstW = _api(_k32, "Process32FirstW", wintypes.BOOL,
                           wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    Process32NextW = _api(_k32, "Process32NextW", wintypes.BOOL,
                          wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    CloseHandle = _api(_k32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    CreateMutexW = _api(_k32, "CreateMutexW", wintypes.HANDLE, wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    CreateEventW = _api(_k32, "CreateEventW", wintypes.HANDLE,
                        wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
    OpenEventW = _api(_k32, "OpenEventW", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    SetEvent = _api(_k32, "SetEvent", wintypes.BOOL, wintypes.HANDLE)
    WaitForSingleObject = _api(_k32, "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
    FindWindowW = _api(_u32, "FindWindowW", wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)


def game_running():
    """True while a Train Sim World process is running."""
    snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, INVALID_HANDLE):
        return False
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        ok = Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if GAME_EXE in entry.szExeFile.lower():
                return True
            ok = Process32NextW(snap, ctypes.byref(entry))
        return False
    finally:
        CloseHandle(snap)


def bridge_open():
    """True when a bridge window is already open."""
    return bool(FindWindowW(None, BRIDGE_TITLE))


def pythonw():
    """pythonw.exe beside this Python, so no console window opens."""
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return exe if os.path.exists(exe) else sys.executable


# ---------------------------------------------------------------- watching
def watch():
    lock = CreateMutexW(None, False, LOCK_NAME)
    stop = CreateEventW(None, False, False, STOP_NAME)
    if WaitForSingleObject(lock, 5000) not in (WAIT_OBJECT_0, WAIT_ABANDONED):
        return                # another copy is already watching
    seen = 0.0                # when the game was last seen running
    opened = False            # the bridge has been opened for this run of the game
    while WaitForSingleObject(stop, CHECK_SECONDS * 1000) == WAIT_TIMEOUT:
        now = time.monotonic()
        if game_running():
            seen = now
            if not opened:
                opened = True
                if not bridge_open():
                    subprocess.Popen([pythonw(), UI_SCRIPT, "--with-game"], cwd=HERE)
        elif opened and now - seen > GONE_SECONDS:
            opened = False    # the game has exited: open the bridge again next time it starts


def stop_watching():
    """Make the copy that's watching, if any, stop."""
    stop = OpenEventW(EVENT_MODIFY_STATE, False, STOP_NAME)
    if stop:
        SetEvent(stop)
        CloseHandle(stop)


def install():
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, f'"{pythonw()}" "{SELF}"')
    stop_watching()           # a copy already watching (perhaps older code, or another folder) makes way
    subprocess.Popen([pythonw(), SELF], cwd=HERE,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    print("Auto-start is on: the joystick bridge window now opens by itself whenever Train Sim World 7")
    print("starts, and closes again when the game exits. This carries on after restarting Windows.")
    print()
    print("The game still needs -HTTPAPI in its Steam launch options.")
    print("If you move this folder, turn auto-start on again from the new place.")
    print('To turn it off, run "Auto-start with TSW - off.bat".')


def uninstall():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_NAME)
    except FileNotFoundError:
        pass
    stop_watching()
    print("Auto-start is off: the joystick bridge no longer opens by itself when the game starts.")
    print('Open it by hand with "Start TSW Joystick.bat".')


if __name__ == "__main__":
    if "--install" in sys.argv:
        install()
    elif "--uninstall" in sys.argv:
        uninstall()
    else:
        try:
            watch()
        except Exception:
            # pythonw has no console: leave a trace next to the script
            with open(os.path.join(HERE, "autostart_error.log"), "w", encoding="utf-8") as f:
                traceback.print_exc(file=f)
            raise
