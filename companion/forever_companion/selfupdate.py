"""The app updating itself. The build source holds signed builds of the app, checked like any addon
build (release.py) before anything is run or replaced:

- <source>/companion-setup/ holds "XPCompass-Setup.exe", the Windows installer (Inno Setup,
  companion/installer.iss). The app saves it in its data folder (updates\\, not Temp), starts it
  silently and quits. The setup waits for the app to close, installs the new app over the old one in
  %LOCALAPPDATA%\\Programs\\XPCompass and starts it. If the install fails it starts the old app again,
  which leaves that build alone for RETRY_AFTER.
- <source>/companion/ holds "XPCompass.exe", the one-file app from before the installer. Apps up to
  86b88674 replace themselves with it: write it beside the running .exe, rename the running one to
  .old.exe (Windows lets a running .exe be renamed but not overwritten), rename the new one into
  place, start it and quit. It now carries the migration build (stamped MIGRATE), which takes
  whatever installer build is offered, notes where it ran from, and runs the setup like any update.
  The installed app then deletes that loose .exe and points Start with Windows at itself.
- <source>/companion-mac/ holds a zip of "XPCompass.app". Unpack the new one with ditto (which keeps
  its links and permissions), rename the running bundle to .old.app, move the new one into place,
  open it and quit.

A failed swap puts the old copy back. Only the packaged app updates itself. Run from source (no
_build.py), the app is updated by git.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import release, system
from .release import APP_FILES, APP_SETUP_FILE, ReleaseError

# How a build was packed (_build.KIND): the installed one-folder app, the one-file migration build,
# or the Mac bundle.
INSTALLED, MIGRATE, BUNDLE = "installed", "migrate", "bundle"
RETRY_AFTER = 3600          # seconds before an installer build that did not install is tried again
SETUP_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
OLD_COPIES = "old-copies.json"


def current():
    """{"build", "published", "kind"} of this copy of the app, or None when run from source."""
    try:
        from . import _build      # written when the app is packaged
    except ImportError:
        return None
    return {"build": _build.BUILD, "published": _build.PUBLISHED, "kind": getattr(_build, "KIND", "")}


def offered(source, mine, keys=None, token=None, package=None):
    """The verified manifest of a newer app build at `source`, or None. The migration build takes
    any installer build, since it is never the copy a player keeps."""
    if not mine:
        return None
    manifest = release.latest(source, package or release.app_package(), keys, token)
    if mine.get("kind") == MIGRATE:
        return manifest
    return manifest if manifest["published"] > mine["published"] else None


def pending_dir():
    """Where a downloaded installer waits to run."""
    return system.data_dir() / "updates"


def apply(source, manifest, target=None, token=None, progress=None):
    """Put the build `manifest` describes in place. `target` is the running .exe, the .app on a Mac,
    or for the installer the folder it is saved in. Returns the path to start."""
    package = manifest["package"]
    data = release.download(source, manifest, token, progress)[APP_FILES[package]]
    if package == release.APP_MAC:
        return _swap_bundle(data, Path(target) if target else system.mac_bundle())
    if package == release.APP_SETUP:
        return _save_setup(data, manifest["build"], Path(target) if target else pending_dir())
    return _swap_exe(data, Path(target or sys.executable))


def _swap_exe(data, exe):
    new, old = exe.with_name(exe.stem + ".new.exe"), exe.with_name(exe.stem + ".old.exe")
    new.write_bytes(data)
    if old.exists():
        old.unlink()
    exe.rename(old)
    try:
        new.rename(exe)
    except OSError as error:
        old.rename(exe)
        new.unlink(missing_ok=True)
        raise ReleaseError(f"could not replace the app ({error}); it carries on as it was") from error
    return exe


def _save_setup(data, build, folder):
    """Write the verified installer into `folder` as XPCompass-Setup-<build>.exe, whole or not at all."""
    build = re.sub(r"[^A-Za-z0-9._-]", "", str(build))[:40] or "build"
    target = folder / f"{Path(APP_SETUP_FILE).stem}-{build}.exe"
    part = target.with_suffix(".part")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _remove_setups(folder)
        part.write_bytes(data)
        os.replace(part, target)
    except OSError as error:
        part.unlink(missing_ok=True)
        raise ReleaseError(f"could not save the app update ({error}); it carries on as it was") from error
    return target


def _remove_setups(folder):
    """Delete the installers in `folder`; one still running stays until the next try."""
    for old in [*Path(folder).glob(f"{Path(APP_SETUP_FILE).stem}-*.exe"), *Path(folder).glob("*.part")]:
        try:
            old.unlink()
        except OSError:
            pass


def run_setup(setup, build, me=None, migrating=False, env=None, clock=time.time):
    """Start the installer for `build` silently and return; the caller quits at once, since the
    setup waits for this app to close before it replaces its files. The setup starts the new app
    when it is done, or this one (`me`) again if the install failed. A migration build first notes
    where it runs from, for the installed app to delete."""
    setup, me = Path(setup), Path(me or sys.executable)
    folder = setup.parent
    args = [str(setup), *SETUP_ARGS, f"/LOG={folder / 'setup.log'}", f"/RELAUNCH={me}"]
    try:
        if migrating:
            _remember_old_copy(me, folder)
        _write_json(folder / "tried.json", {"build": build, "at": clock()})
        subprocess.Popen(args, close_fds=True, env=fresh_environment(env))
    except OSError as error:
        raise ReleaseError(f"could not start the app update ({error}); it carries on as it was") from error


def tried_recently(build, folder=None, clock=time.time):
    """True when the installer for `build` was started less than RETRY_AFTER ago and this app is
    still the one running: that install failed, and trying again at once would fail the same way."""
    tried = _read_json(Path(folder or pending_dir()) / "tried.json")
    if not isinstance(tried, dict) or tried.get("build") != build:
        return False
    try:
        return clock() - float(tried.get("at", 0)) < RETRY_AFTER
    except (TypeError, ValueError):
        return False


def _remember_old_copy(exe, folder):
    entries = _read_json(folder / OLD_COPIES)
    entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") != str(exe)] \
        if isinstance(entries, list) else []
    entries.append({"path": str(exe), "sha256": _file_hash(exe)})
    _write_json(folder / OLD_COPIES, entries)


def forget_old_copies(me=None, folder=None):
    """In the installed app: delete each loose .exe a migration build ran from, if it is still that
    very file (same hash), and what an update left beside it. One still locked by its quitting
    process stays listed for the next start. Returns the paths deleted."""
    me = Path(me or sys.executable).resolve()
    record = Path(folder or pending_dir()) / OLD_COPIES
    entries = _read_json(record)
    if not isinstance(entries, list):
        return []
    left, gone = [], []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        exe = Path(entry["path"])
        if exe.resolve() == me:
            continue
        try:
            if exe.is_file():
                if _file_hash(exe) != entry.get("sha256"):
                    continue            # replaced since: not ours to delete
                exe.unlink()
                gone.append(exe)
        except OSError:
            left.append(entry)
            continue
        for leftover in (exe.with_name(exe.stem + ".old.exe"), exe.with_name(exe.stem + ".new.exe")):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
    try:
        if left:
            _write_json(record, left)
        else:
            record.unlink(missing_ok=True)
    except OSError:
        pass
    return gone


def _file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value), encoding="utf-8")


def _swap_bundle(data, bundle, unpack=None):
    """Swap the .app `bundle` for the one inside the zip `data`. `unpack(zip, folder)` is ditto on a
    Mac; tests pass their own."""
    old = bundle.with_name(bundle.stem + ".old.app")
    with tempfile.TemporaryDirectory(dir=bundle.parent) as work:
        archive = Path(work) / "update.zip"
        archive.write_bytes(data)
        (unpack or _ditto)(archive, Path(work) / "new")
        found = list((Path(work) / "new").glob("*.app"))
        if len(found) != 1:
            raise ReleaseError("the app update does not hold exactly one .app; it was not installed")
        if old.exists():
            shutil.rmtree(old)
        bundle.rename(old)
        try:
            found[0].rename(bundle)
        except OSError as error:
            old.rename(bundle)
            raise ReleaseError(f"could not replace the app ({error}); it carries on as it was") from error
    return bundle


def _ditto(archive, folder):
    subprocess.run(["ditto", "-x", "-k", str(archive), str(folder)], check=True)


def relaunch(path, env=None):
    if str(path).endswith(".app"):
        subprocess.Popen(["open", "-n", str(path), "--args", "--after-update"])
    else:
        subprocess.Popen([str(path), "--after-update"], close_fds=True, env=fresh_environment(env))


def fresh_environment(env=None):
    """The environment for a program this app starts (the new .exe, or the setup, which passes it on
    to the app it starts): this one, told to start as its own program. The one-file app unpacks
    itself into a temporary folder; a copy started from inside it would otherwise take that folder
    for its own, and this copy deletes it as it quits, so the new one lost its files and crashed on
    start (17 October)."""
    env = dict(os.environ if env is None else env)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for name in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL"):
        env.pop(name, None)
    return env


def clean_up(target=None, folder=None):
    """Remove what an earlier update left: the old copy beside the app, and installers already run.
    A file may stay locked a moment while the process that held it quits; the next start gets it."""
    if target is None:
        folder = folder or pending_dir()
        target = system.mac_bundle() if system.MAC else Path(sys.executable)
    if folder:
        _remove_setups(folder)
    target = Path(target)
    if target.suffix == ".app":
        shutil.rmtree(target.with_name(target.stem + ".old.app"), ignore_errors=True)
        return
    for leftover in (target.with_name(target.stem + ".old.exe"), target.with_name(target.stem + ".new.exe")):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass
