"""A release of one package: manifest.json, manifest.sig and the zip the manifest names, in the
package's folder at the build source (<source>/<package>/).

The source is an https base URL or a folder. Nothing it serves is trusted until the manifest's
signature checks against a key in TRUSTED_KEYS, the zip's hash matches the manifest, and every file
in the zip is listed in the manifest with its hash. So the host can be anything (a public repository,
a Mac at home) and a tampered or truncated download is refused rather than installed.
"""
import base64
import hashlib
import http.client
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

FORMAT = 1

# Public keys whose signatures the app accepts (base64 of the raw 32-byte Ed25519 key).
# tools/publish_build.py --new-key prints the line to add. The private key never enters the repo.
TRUSTED_KEYS = [
    "SvMTMf0Pgjsgr5GwoJiYYB1RiZA9EsNnIKQzIqeIZFo=",     # release key made 2026-10-17
]

# What the app can install, and the only AddOns folders each package may write. Set here, in the
# app, so a build can never name a new folder. Capture is never managed: its folder holds the
# collector secret.
#
# A product is one package, one download. Its editions are what the server sends each account:
# everyone gets the public edition, Only Bans members the members edition with the guild guides
# inside. Features such as Mail ship in both: each is its own addon only so that a player can turn
# off one that throws Lua errors and keep the rest. Players turn the parts on and off in the game's
# AddOns list.
#
# Each package is one game's build ("client": the id in games.py, which also finds that game's client
# folder on this computer). tools/game.py names the same two packages; a test keeps them equal.
PACKAGES = {
    "forever-coach": {
        "name": "XPCompass",
        "game": "WoW Forever",
        "client": "forever",
        "about": "The leveling guide: quests in route order, with the arrow and map trail.",
        # The folders it had before the renames (17 October) are its too: an install moves them aside,
        # so the old copies stop loading beside the new ones, and a roll back can put them back.
        "folders": ("XPCompass", "XPCompass_HordeGuides", "XPCompass_Mail", "XPCompass_Guild", "XPCompass_Market",
                    "ForeverCoachNext", "ForeverCoachOB", "ForeverCoachMail", "XPCompass_BankMail"),
        "renamed": {"ForeverCoachNext": "XPCompass", "ForeverCoachOB": "XPCompass_HordeGuides",
                    "ForeverCoachMail": "XPCompass_Mail", "XPCompass_BankMail": "XPCompass_Mail"},
        "editions": {"public": ("XPCompass", "XPCompass_Mail", "XPCompass_Guild", "XPCompass_Market"),
                     "members": ("XPCompass", "XPCompass_HordeGuides", "XPCompass_Mail", "XPCompass_Guild",
                                 "XPCompass_Market")},
        "needs": (),
    },
    # Classic Era's build: the same addons as Forever's, minus the old names (nothing of Era was ever
    # installed under one), with Era's own .toc files and generated files. An addon with no Era .toc yet
    # is simply not in the build, but is listed here so that adding it later needs no new app release;
    # tests/test_era_clients.py keeps these lists equal to Forever's.
    "xpcompass-era": {
        "name": "XPCompass (Classic Era)",
        "game": "WoW Classic Era",
        "client": "era",
        "about": "The leveling guide: quests in route order, with the arrow and map trail.",
        "folders": ("XPCompass", "XPCompass_HordeGuides", "XPCompass_Mail", "XPCompass_Guild", "XPCompass_Market"),
        "editions": {"public": ("XPCompass", "XPCompass_Mail", "XPCompass_Guild", "XPCompass_Market"),
                     "members": ("XPCompass", "XPCompass_HordeGuides", "XPCompass_Mail", "XPCompass_Guild",
                                 "XPCompass_Market")},
        "needs": (),
    },
}
EDITION_NAMES = {"public": "Starting zones", "members": "Only Bans edition"}
RETIRED = ("forever-coach-ob", "forever-coach-mail")     # now inside forever-coach's members edition

# Shown in the app's list but not installable yet.
COMING = {
    "guild-chronicle": {"name": "Guild Chronicle", "game": "WoW Forever", "about": "Being rebuilt. It appears here when it's ready."},
}
NEVER = ("secret.lua",)

# The app itself is released like a package, as one file, under the same signing key (selfupdate.py):
# on Windows its installer, and on a Mac a zip of the .app (a bundle is a folder, with links). Players
# see it as APP_NAME; the package names are internal.
#
# APP is the bare .exe that apps from before the installer (up to 86b88674) replace themselves with.
# Those apps know only APP and its one file name, so APP stays as it is: it now carries the
# migration build, which installs APP_SETUP and quits (selfupdate.py).
APP_NAME = "XPCompass"
APP = "companion"
APP_FILE = f"{APP_NAME}.exe"
APP_SETUP = "companion-setup"
APP_SETUP_FILE = f"{APP_NAME}-Setup.exe"
APP_MAC = "companion-mac"
APP_MAC_FILE = "XPCompass-Mac.zip"
APP_DOWNLOADS = {APP: "XPCompass-Windows", APP_SETUP: "XPCompass-Setup", APP_MAC: "XPCompass-Mac"}  # release zip names
APP_FILES = {APP: APP_FILE, APP_SETUP: APP_SETUP_FILE, APP_MAC: APP_MAC_FILE}


def app_package():
    """The app package this computer updates from: the installer on Windows, the .app zip on a Mac."""
    return APP_MAC if sys.platform == "darwin" else APP_SETUP

MAX_ZIP = 20_000_000          # bytes downloaded
MAX_UNPACKED = 40_000_000     # bytes once extracted; the addon budget is 2 MB, so this is generous
MAX_APP = 150_000_000         # the app's own build (one .exe with Python inside), packed and unpacked


class ReleaseError(Exception):
    """The release cannot be used. The message says why, in words a tester can pass on."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def folders(package):
    if package in APP_FILES:
        return ()
    if package not in PACKAGES:
        raise ReleaseError(f"this app does not know the package {package!r}; update the companion app")
    return PACKAGES[package]["folders"]


class SignInNeeded(ReleaseError):
    """The source answered 401: the build is guild-only and the token is missing, unknown or expired."""

    def __init__(self, code):
        super().__init__("Sign in with Discord to get this build." if code != "token_expired"
                         else "The sign-in has expired.")
        self.code = code


def local(url):
    """True for http://127.0.0.1 or localhost: a test or development server on this PC."""
    return urllib.parse.urlsplit(str(url)).hostname in ("127.0.0.1", "localhost") and str(url).startswith("http://")


TRIES = 5


def _download(url, headers, limit, name, progress=None):
    """The body at `url`. A download that stalls or breaks off resumes from where it stopped (HTTP
    Range), up to TRIES times; if the server ignores the range, it starts again from the beginning.
    Whatever arrives is checked against the signed manifest afterwards, so a bad join is caught.
    `progress(done, total)` is called as bytes arrive (total 0 when the server doesn't say)."""
    data, last = bytearray(), None
    for _ in range(TRIES):
        wanted = dict(headers, **({"Range": f"bytes={len(data)}-"} if data else {}))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=wanted), timeout=30) as response:
                if data and response.status != 206:
                    data = bytearray()
                total = len(data) + int(response.headers.get("Content-Length") or 0)
                while len(data) <= limit:
                    if progress:
                        progress(len(data), total)
                    try:
                        chunk = response.read(1 << 16)
                    except http.client.IncompleteRead as broken:
                        data += broken.partial
                        raise
                    if not chunk:
                        if getattr(response, "length", None):     # closed before Content-Length arrived
                            raise http.client.IncompleteRead(b"", response.length)
                        return bytes(data)
                    data += chunk
                return bytes(data)          # over the limit: the caller refuses it
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException) as error:
            last = error
    raise ReleaseError(f"could not download {name} after {TRIES} tries: {last}")


def fetch(source, name, limit=MAX_ZIP, token=None, progress=None):
    """The bytes of `name` (a path under `source`, an https base URL or a folder). `token` is sent
    as a Bearer header; the server needs it for guild-only packages."""
    if str(source).startswith(("http://", "https://")):
        if not (str(source).startswith("https://") or local(source)):
            raise ReleaseError("the build source must use https")
        url = urllib.parse.urljoin(str(source).rstrip("/") + "/", name)
        headers = {"User-Agent": "ForeverCoachCompanion"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            data = _download(url, headers, limit, name, progress)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                try:
                    code = json.loads(error.read(10_000)).get("error", "invalid_token")
                except ValueError:
                    code = "invalid_token"
                raise SignInNeeded(code) from error
            if error.code == 404:
                raise ReleaseError(f"{name} is missing on the server") from error
            raise ReleaseError(f"could not download {name}: {error}") from error
        except OSError as error:
            raise ReleaseError(f"could not download {name}: {error}") from error
    else:
        path = Path(source) / name
        if not path.is_file():
            raise ReleaseError(f"{path} is missing")
        data = path.read_bytes()
    if len(data) > limit:
        raise ReleaseError(f"{name} is larger than {limit // 1_000_000} MB")
    return data


def verify_manifest(raw, signature, keys=None, package=None):
    """The manifest as a dict if `signature` (base64) signs `raw` with a trusted key."""
    keys = TRUSTED_KEYS if keys is None else keys
    if not keys:
        raise ReleaseError("this copy of the app trusts no signing key yet")
    try:
        sig = base64.b64decode(signature.strip(), validate=True)
    except ValueError as error:
        raise ReleaseError("the build's signature is not readable") from error
    for key in keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(sig, raw)
            break
        except InvalidSignature:
            continue
    else:
        raise ReleaseError("the build is not signed by XPCompass; it was not installed")
    manifest = json.loads(raw)
    if manifest.get("format") != FORMAT:
        raise ReleaseError(f"the build uses format {manifest.get('format')}; update the companion app")
    if package and manifest.get("package") != package:
        raise ReleaseError(f"the source offered {manifest.get('package')} where {package} was expected")
    zip_name = manifest["zip"]["name"]
    if PurePosixPath(zip_name).name != zip_name or zip_name.startswith("."):
        raise ReleaseError(f"the build names an odd zip: {zip_name}")
    if not set(manifest["addons"]) <= set(folders(manifest["package"])):
        raise ReleaseError(f"the build names folders its package does not own: {manifest['addons']}")
    for name in manifest["files"]:
        safe_path(name, manifest["package"])
    return manifest


def safe_path(name, package):
    """`name` as a relative path inside one of `package`'s folders, or ReleaseError."""
    path = PurePosixPath(name)
    if package in APP_FILES:
        if name != APP_FILES[package]:
            raise ReleaseError(f"refusing {name}: an app build holds only {APP_FILES[package]}")
        return path
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or len(path.parts) < 2:
        raise ReleaseError(f"refusing a file outside the addon folders: {name}")
    allowed = folders(package)
    if path.parts[0] not in allowed:
        raise ReleaseError(f"refusing to write {path.parts[0]}: {package} only manages {', '.join(allowed)}")
    if path.name.lower() in NEVER:
        raise ReleaseError(f"refusing {name}: a collector secret is never shipped")
    return path


def unpack(manifest, zipped):
    """{relative path: bytes} for every file, after checking each one against the manifest."""
    if sha256(zipped) != manifest["zip"]["sha256"]:
        raise ReleaseError("the download does not match the signed build (damaged or replaced)")
    files, total = {}, 0
    with zipfile.ZipFile(io.BytesIO(zipped)) as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            if info.filename not in manifest["files"]:
                raise ReleaseError(f"the zip holds a file the signed build does not list: {info.filename}")
            total += info.file_size
            if total > (MAX_APP if manifest["package"] in APP_FILES else MAX_UNPACKED):
                raise ReleaseError("the build unpacks to more than it should")
            data = bundle.read(info)
            if sha256(data) != manifest["files"][info.filename]:
                raise ReleaseError(f"{info.filename} does not match the signed build")
            files[info.filename] = data
    missing = set(manifest["files"]) - set(files)
    if missing:
        raise ReleaseError(f"the zip is missing {len(missing)} files of the signed build")
    return files


def latest(source, package, keys=None, token=None):
    """The verified manifest of `package`'s newest build at `source`: two small files, no zip."""
    folders(package)
    raw = fetch(source, f"{package}/manifest.json", 1_000_000, token)
    signature = fetch(source, f"{package}/manifest.sig", 10_000, token).decode("ascii", "replace")
    return verify_manifest(raw, signature, keys, package)


def download(source, manifest, token=None, progress=None):
    """The verified files of the build `manifest` describes."""
    limit = MAX_APP if manifest["package"] in APP_FILES else MAX_ZIP
    return unpack(manifest, fetch(source, f"{manifest['package']}/{manifest['zip']['name']}", limit, token, progress))


def load(source, package, keys=None, token=None):
    """(manifest, files) of `package`'s newest build at `source`, fully verified."""
    manifest = latest(source, package, keys, token)
    return manifest, download(source, manifest, token)
