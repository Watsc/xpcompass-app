"""Put a verified release into one game client's AddOns folder, or put the previous copy back.

The new folders are written beside AddOns first and swapped in by renaming, so a failure part way
leaves the old copy in place. Only the package's own folders are touched; Capture and every other addon
stay as they are. The app's own state (what it installed, the copy it replaced) lives in
Interface/.forever-companion/<package>, inside the client it belongs to.
"""
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .release import PACKAGES, ReleaseError, folders, sha256

STATE = ".forever-companion"


@dataclass
class Result:
    package: str
    build: str
    previous: str = None
    needs: str = "restart"            # "restart", "reload" or "nothing"
    changed: list = field(default_factory=list)

    def advice(self):
        name = PACKAGES[self.package]["name"]
        if self.needs == "nothing":
            return f"{name} {self.build} is already installed."
        was = f" (was {self.previous})" if self.previous else ""
        if self.needs == "reload":
            return f"Installed {name} {self.build}{was}. In game, type /reload."
        return (f"Installed {name} {self.build}{was}. Restart the game fully (a /reload is not enough), "
                f"and check {name} is ticked under AddOns at character select.")


def addons_dir(game):
    """The AddOns folder of a client folder such as ..._classic_beta_."""
    path = Path(game) / "Interface" / "AddOns"
    if not path.is_dir():
        raise ReleaseError(f"{game} has no Interface\\AddOns folder; is that the game client folder?")
    return path


def state_dir(game, package):
    folders(package)
    return Path(game) / "Interface" / STATE / package


def installed(game, package):
    """The manifest of the build of `package` the app last installed into `game`, or None."""
    path = state_dir(game, package) / "installed.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def local_changes(game, package):
    """Managed files that differ from what the app installed: edited, deleted or added since."""
    manifest = installed(game, package)
    if not manifest:
        return []
    addons, changes = addons_dir(game), []
    for name, digest in sorted(manifest["files"].items()):
        path = addons / name
        if not path.is_file():
            changes.append(f"missing {name}")
        elif sha256(path.read_bytes()) != digest:
            changes.append(f"changed {name}")
    for folder in manifest["addons"]:
        for path in sorted((addons / folder).rglob("*")) if (addons / folder).is_dir() else []:
            name = path.relative_to(addons).as_posix()
            if path.is_file() and name not in manifest["files"]:
                changes.append(f"added {name}")
    return changes


def needs(old, new):
    """What the game needs to pick up `new` over `old`: a .toc change or a new or removed file needs a
    restart, since the client reads the file list only at start; changed file contents need a /reload."""
    if old is None or set(old["files"]) != set(new["files"]):
        return "restart"
    changed = [name for name in new["files"] if old["files"][name] != new["files"][name]]
    if not changed:
        return "nothing"
    return "restart" if any(name.endswith(".toc") for name in changed) else "reload"


def install(game, manifest, files, allow_older=False):
    """Install the verified release (release.load) into `game`. Returns a Result."""
    package = manifest["package"]
    addons, state = addons_dir(game), state_dir(game, package)
    old = installed(game, package)
    if old and not allow_older and new_is_older(old, manifest):
        raise ReleaseError(f"the source offers {manifest['build']}, older than the installed {old['build']}; "
                           f"not installed")
    result = Result(package, manifest["build"], old and old["build"], needs(old, manifest))
    result.changed = local_changes(game, package)
    if result.needs == "nothing" and not result.changed:
        return result
    if result.needs == "nothing":
        result.needs = "reload"     # repairing edited files

    staging, previous = state / "staging", state / "previous"
    shutil.rmtree(staging, ignore_errors=True)
    for name, data in files.items():
        path = staging / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    shutil.rmtree(previous, ignore_errors=True)
    previous.mkdir(parents=True)
    moved = []                              # (from, to) renames done so far, undone on failure
    try:
        # Every folder the package owns is moved aside, not only the ones this build has: a player
        # going from the members edition to the public one stops having the guild guides.
        for folder in folders(package):
            if (addons / folder).exists():
                (addons / folder).rename(previous / folder)
                moved.append((addons / folder, previous / folder))
        for folder in manifest["addons"]:
            (staging / folder).rename(addons / folder)
            moved.append((staging / folder, addons / folder))
    except OSError as error:
        for source, target in reversed(moved):
            if target.exists():
                target.rename(source)
        raise ReleaseError(f"could not replace the addon folders ({error}); the old copy is still in place. "
                           f"Close anything that has an XPCompass file open and try again.") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if old:
        (previous / "installed.json").write_text(json.dumps(old, indent=1), encoding="utf-8")
    (state / "installed.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return result


def remember(game, manifest):
    """Record `manifest` as installed when its files are the ones already in place: a republish with a
    new version or new notes, nothing to copy."""
    (state_dir(game, manifest["package"]) / "installed.json").write_text(json.dumps(manifest, indent=1),
                                                                        encoding="utf-8")


def new_is_older(old, new):
    return new.get("published", "") < old.get("published", "")


def move_saved_settings(game):
    """Copies each renamed addon's saved settings to its new name, for every account and character in
    `game`: the game keys the files (WTF/.../SavedVariables/<folder>.lua) by the addon's folder, so after
    the rename the new folders would start with none. Only while the game is closed (the caller checks):
    a running game writes its files from memory as it closes. An old file is copied when the new one is
    missing or older, so a last session on the old folders still carries over. The old files are left
    in place. Returns the files written."""
    wtf = Path(game) / "WTF" / "Account"
    if not wtf.is_dir():
        return []
    renamed = {old: new for package in PACKAGES.values() for old, new in package.get("renamed", {}).items()}
    written = []
    for saved in [*wtf.glob("*/SavedVariables"), *wtf.glob("*/*/*/SavedVariables")]:
        for old, new in renamed.items():
            for suffix in (".lua", ".lua.bak"):
                source, target = saved / (old + suffix), saved / (new + suffix)
                if source.is_file() and (not target.exists() or target.stat().st_mtime < source.stat().st_mtime):
                    shutil.copy2(source, target)
                    written.append(target)
    return written


def roll_back(game, package):
    """Put back the copy the last install of `package` replaced. Returns the build now installed."""
    addons, state = addons_dir(game), state_dir(game, package)
    previous = state / "previous"
    before = previous / "installed.json"
    if not before.exists():
        raise ReleaseError("there is no earlier copy to go back to")
    manifest = json.loads(before.read_text(encoding="utf-8"))
    for folder in manifest["addons"]:
        if not (previous / folder).is_dir():
            raise ReleaseError(f"the earlier copy of {folder} is gone; reinstall instead")
    for folder in folders(package):         # back to exactly the earlier set of folders
        if (addons / folder).exists():
            shutil.rmtree(addons / folder)
    for folder in manifest["addons"]:
        (previous / folder).rename(addons / folder)
    (state / "installed.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    shutil.rmtree(previous, ignore_errors=True)
    return manifest["build"]
