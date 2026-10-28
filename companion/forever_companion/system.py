"""Everything that differs between Windows and macOS, in one place: where the app keeps its files,
the stored sign-in, starting at login, one copy at a time, whether the game is running, opening a
folder and notifications. Everything else in the app is the same on both.
"""
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"
APP_ID = "com.forevercoach.companion"


def data_dir():
    """Where the app keeps its settings and lock: %LOCALAPPDATA% on Windows, Application Support on a Mac."""
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ForeverCoach"
    if MAC:
        return Path.home() / "Library" / "Application Support" / "ForeverCoach"
    return Path.home() / ".forever-coach-app"


def packaged():
    """True in the built app (.exe or .app), False when run from source."""
    return bool(getattr(sys, "frozen", False))


def bundled(name, dev_path):
    """A file packed into the built app, or `dev_path` when run from source."""
    return Path(sys._MEIPASS) / name if packaged() else Path(dev_path)


def mac_bundle():
    """The running "XPCompass.app", when packaged on a Mac."""
    return Path(sys.executable).resolve().parents[2] if MAC and packaged() else None


# --- Is the game running? -------------------------------------------------------------------------

def process_names():
    if WINDOWS:
        listing = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return [line.split('","')[0].strip('"') for line in listing.splitlines()]
    listing = subprocess.run(["ps", "-Ao", "comm="], capture_output=True, text=True).stdout
    return [line.strip() for line in listing.splitlines()]


def is_game(name):
    """Wow.exe, WowB.exe, WowClassic.exe on Windows; ".../World of Warcraft Classic.app/..." on a Mac."""
    low = name.lower()
    return (low.startswith("wow") and low.endswith(".exe")) or "world of warcraft" in low


def game_running():
    return any(is_game(name) for name in process_names())


# --- Opening a folder, notifications --------------------------------------------------------------

def open_folder(path):
    if WINDOWS:
        os.startfile(path)
    else:
        subprocess.Popen(["open" if MAC else "xdg-open", str(path)])


def mac_notify(title, text):
    """macOS notification through AppleScript (pystray cannot notify on a Mac). Inputs are quoted."""
    script = f"display notification {json.dumps(text)} with title {json.dumps(title)}"
    subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- Starting at login -----------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ForeverCoachCompanion"


def launch_args():
    """How the OS should start the app at login, hidden in the background."""
    if packaged():
        return [sys.executable, "--background"]
    runner = Path(sys.executable)
    if WINDOWS:
        runner = runner.with_name("pythonw.exe")
    return [str(runner), str(Path(__file__).resolve().parents[1] / "ForeverCompanion.pyw"), "--background"]


def launch_agent():
    return Path.home() / "Library" / "LaunchAgents" / f"{APP_ID}.plist"


def login_command():
    """launch_args as the one command line Windows' Run key holds."""
    return " ".join(part if part.startswith("--") else f'"{part}"' for part in launch_args())


def login_entry():
    """The command Windows runs at login for the app, or None when start-at-login is off."""
    if not WINDOWS:
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0]
    except OSError:
        return None


def starts_at_login():
    if WINDOWS:
        return login_entry() is not None
    return MAC and launch_agent().exists()


def repoint_start_at_login(read=login_entry, write=None):
    """When start-at-login is on for another copy of the app, such as the loose .exe the installed
    app replaced, point it at this one. Off stays off, and so does a developer's entry that runs a
    checkout through Python. True when it changed."""
    command = read()
    if command is None or command == login_command():
        return False
    text = command.strip()
    program = text.split('"')[1] if text.startswith('"') else (text.split() or [""])[0]
    if Path(program).name.lower().startswith("python"):
        return False
    (write or set_start_at_login)(True)
    return True


def set_start_at_login(on):
    """Only ever changed by the player, from the window (or once at the built app's first start)."""
    if WINDOWS:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, login_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except OSError:
                    pass
    elif MAC:
        agent = launch_agent()
        if on:
            agent.parent.mkdir(parents=True, exist_ok=True)
            agent.write_bytes(plistlib.dumps({"Label": APP_ID, "ProgramArguments": launch_args(),
                                              "RunAtLoad": True, "ProcessType": "Interactive"}))
        else:
            agent.unlink(missing_ok=True)


# --- One copy at a time ----------------------------------------------------------------------------

_held = []      # keeps the lock alive for the life of the process


def single_instance(wait=0):
    """False when another copy of the app is already running. Just after a self-update the old copy
    is still quitting, so the new one waits up to `wait` seconds for it."""
    deadline = time.monotonic() + wait
    while True:
        if _try_lock():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _try_lock():
    if WINDOWS:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.CreateMutexW(None, False, "ForeverCoachCompanion")
        if kernel.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
            kernel.CloseHandle(handle)
            return False
        _held.append(handle)
        return True
    import fcntl
    data_dir().mkdir(parents=True, exist_ok=True)
    handle = open(data_dir() / "companion.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _held.append(handle)
    return True


# --- The stored sign-in ----------------------------------------------------------------------------

def credential_store():
    """Windows Credential Manager, or the macOS Keychain (through the keyring package)."""
    if WINDOWS:
        from .auth import CredentialStore
        return CredentialStore()
    return KeychainStore()


class KeychainStore:
    """The session in the macOS Keychain, as a generic password for this user."""

    SERVICE, ACCOUNT = "XPCompass", "session"

    def load(self):
        import keyring
        raw = keyring.get_password(self.SERVICE, self.ACCOUNT)
        return json.loads(raw) if raw else None

    def save(self, session):
        import keyring
        keyring.set_password(self.SERVICE, self.ACCOUNT, json.dumps(session))

    def clear(self):
        import keyring
        from keyring.errors import PasswordDeleteError
        try:
            keyring.delete_password(self.SERVICE, self.ACCOUNT)
        except PasswordDeleteError:
            pass
