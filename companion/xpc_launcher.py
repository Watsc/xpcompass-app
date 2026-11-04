"""The Windows app's launcher: XPCompass.exe is this, frozen with Python and every library the app uses,
but not the app's own code (forever_companion), which it loads from plain files. So a release replaces
only those files (package companion-code, selfupdate.py), and the .exe, the file Microsoft Defender
judges, stays the same from one release to the next. It changes only when ABI does: when the app needs
a library or a Python the frozen launcher lacks (tools/pack_companion.py --launcher builds it, with a
new setup).

Where the code is:
  <data>/app/<build>/        each build an update unpacked (selfupdate.py), with state.json beside them
  <XPCompass.exe's folder>/code/   the build the setup installed, so a first start needs no download

state.json: {"current": build, "previous": build, "good": [builds that started], "bad": [builds that
didn't], "trying": the build being started}. The launcher starts the current build, else the previous,
else the setup's own (that one first when it is newer, as after a new setup). A build counts as
started once the app marks it good (selfupdate.mark_good, when its tray icon is up). One that was being
tried and never marked good failed: it goes on the bad list and is never started again, and the
launcher starts the next. An error while loading or starting the code is caught, the build marked bad,
and the launcher started again on the next one.

    XPCompass.exe [--code <folder>] [app arguments]     --code: that folder only (the packer's self-test)
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ABI = 1                 # raise when the frozen libraries must change: the code says which it needs
STATE = "state.json"
KEEP = 10               # builds remembered as good or bad
RUNNING = False         # True once this launcher has started the app's code
CHOSEN = None           # {"build", "published", "folder"} of the code started


def data_dir():
    """The app's data folder, as forever_companion.system.data_dir() has it (that module isn't loaded yet)."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ForeverCoach"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ForeverCoach"
    return Path.home() / ".forever-coach-app"


def code_root():
    return data_dir() / "app"


def setup_code():
    """The code the setup installed beside the .exe."""
    return Path(sys.executable).resolve().parent / "code"


def stamp(folder):
    """{"build", "published"} of the code in `folder`, from its forever_companion/_build.py, or None."""
    try:
        text = (Path(folder) / "forever_companion" / "_build.py").read_text(encoding="utf-8")
        if not (Path(folder) / "forever_companion" / "tray.py").is_file():
            return None
    except OSError:
        return None
    build = re.search(r'^BUILD = "([^"]+)"', text, re.M)
    published = re.search(r'^PUBLISHED = "([^"]+)"', text, re.M)
    if not (build and published):
        return None
    return {"build": build.group(1), "published": published.group(1)}


def read_state(root=None):
    try:
        state = json.loads((Path(root or code_root()) / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    for name in ("good", "bad"):
        if not isinstance(state.get(name), list):
            state[name] = []
    return state


def write_state(state, root=None):
    """Write state.json whole or not at all."""
    root = Path(root or code_root())
    root.mkdir(parents=True, exist_ok=True)
    part = root / (STATE + ".part")
    part.write_text(json.dumps(state), encoding="utf-8")
    os.replace(part, root / STATE)


def remember(state, name, build):
    if build and build not in state[name]:
        state[name] = (state[name] + [build])[-KEEP:]


def settle(state):
    """A build that was being started and never marked good failed: it goes on the bad list."""
    trying = state.pop("trying", None)
    if trying and trying not in state["good"]:
        remember(state, "bad", trying)
    return state


def choose(state, root=None, setup=None):
    """The code folders to try, best first."""
    root, setup = Path(root or code_root()), Path(setup or setup_code())
    found, seen = [], set()
    for name in ("current", "previous"):
        build = state.get(name)
        if isinstance(build, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,40}", build) and build not in state["bad"]:
            info = stamp(root / build)
            if info and info["build"] == build and build not in seen:
                found.append((root / build, info))
                seen.add(build)
    mine = stamp(setup)
    if mine and mine["build"] not in state["bad"] and mine["build"] not in seen:
        newer = not found or mine["published"] > found[0][1]["published"]
        found.insert(0 if newer else len(found), (setup, mine))
    return found


def _forget_code():
    for name in [name for name in sys.modules if name == "forever_companion" or name.startswith("forever_companion.")]:
        del sys.modules[name]


def _restart(argv):
    """Start the launcher again (it picks the next build) and leave."""
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen([sys.executable, *argv], close_fds=True, env=env)
    return 1


def main(argv=None, root=None, setup=None):
    global RUNNING, CHOSEN
    argv = list(sys.argv[1:] if argv is None else argv)
    only = None
    if "--code" in argv:
        at = argv.index("--code")
        only = Path(argv[at + 1])
        del argv[at:at + 2]
    root = Path(root or code_root())
    if only:
        info = stamp(only)
        options, state = ([(only, info)] if info else []), None
    else:
        state = settle(read_state(root))
        options = choose(state, root, setup)
        try:
            write_state(state, root)
        except OSError:
            pass
    for folder, info in options:
        if state is not None:
            state["trying"] = info["build"]
            try:
                write_state(state, root)
            except OSError:
                pass
        sys.path.insert(0, str(folder))
        try:
            from forever_companion import tray
        except Exception:   # noqa: BLE001 - code that won't load is never started again
            sys.path.remove(str(folder))
            _forget_code()
            if state is not None:
                state.pop("trying", None)
                remember(state, "bad", info["build"])
                write_state(state, root)
            continue
        RUNNING, CHOSEN = True, {**info, "folder": str(folder)}
        sys.argv = [sys.argv[0], *argv]
        try:
            result = tray.main()
        except Exception:   # noqa: BLE001 - the code failed before it came up: start the next one
            if state is None:
                raise
            state = read_state(root)
            if info["build"] not in state["good"]:
                state.pop("trying", None)
                remember(state, "bad", info["build"])
                write_state(state, root)
                return _restart(argv)
            raise
        # The app ended on its own (quit, or another copy was already running): not a failure.
        if state is not None:
            state = read_state(root)
            if state.get("trying") == info["build"]:
                state.pop("trying", None)
                write_state(state, root)
        return result
    _say("XPCompass could not start: its files are missing or damaged. Install it again from xpcompass.com/download.")
    return 1


def _say(text):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "XPCompass", 0x10)
    except Exception:   # noqa: BLE001 - nowhere left to tell
        print(text, file=sys.stderr)
