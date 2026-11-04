"""Find the game client folders on this computer, and say which game each one is.

Battle.net keeps a list of what it installed where: `product.db` in its Agent folder for the whole
computer, and `.product.db` in each World of Warcraft folder for the clients in it. Each entry names a
product (wowf for WoW Forever, wow_classic_beta for its beta, wow_classic_era, wow for retail), the
World of Warcraft folder and the client's folder in it. That list says which game a client is first;
then `.flavor.info` in the client folder (its second line is the product); then the folder's name.
Not whether XPCompass is already in it. Each game of ours has a list of products; the app installs a
game's package into the client whose product is on that list, newest product first.

Every launcher update rewrites these files, so none of them is required: a client is found by any of
them, and by its own folder. A client that has never had an addon has no Interface/AddOns folder yet
(a fresh install, or one that was never played); the app makes it when it installs.

Forever's release client is `_forever_`, product wowf (the live game since 28 October); the beta's,
`_classic_beta_`, comes after it. A computer with both moves to the release client (service.Updater
says so), and the beta folder's files stay as they are: uninstalling the beta leaves its folder,
AddOns and all, without its .flavor.info. Era and Retail are never a Forever client, whatever they hold.
"""
import os
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOTS = (r"C:\Program Files (x86)\World of Warcraft", r"C:\Program Files\World of Warcraft")
MAC_ROOTS = ("/Applications/World of Warcraft", "~/Applications/World of Warcraft")
ADDONS = ("XPCompass", "ForeverCoachNext")   # the base addon's folder, and its name before 17 October
# Battle.net's list of everything it installed on this computer.
AGENT_DB = (Path("/Users", "Shared", "Battle.net", "Agent", "product.db") if sys.platform == "darwin" else
            Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "Battle.net" / "Agent" / "product.db")
# A client folder's own game program: what makes a folder a client when Battle.net's files say nothing.
PROGRAMS = ("Wow.exe", "WowClassic.exe", "WowB.exe", "WowT.exe", "World of Warcraft.app",
            "World of Warcraft Classic.app")

# For a client folder without a readable .flavor.info: the product its name means.
FOLDER_PRODUCTS = {"_forever_": "wowf", "_classic_beta_": "wow_classic_beta", "_classic_era_": "wow_classic_era",
                   "_classic_": "wow_classic", "_retail_": "wow"}


@dataclass(frozen=True)
class Game:
    id: str              # as tools/game.py and every upload name it: "forever" or "era"
    title: str           # as the app shows it (release.PACKAGES' "game")
    package: str         # the package that installs into this game's client
    products: tuple      # .flavor.info products that are this game, the newest first


FOREVER = Game("forever", "WoW Forever", "forever-coach", ("wowf", "wow_classic_beta"))   # the live game, then the beta
ERA = Game("era", "WoW Classic Era", "xpcompass-era", ("wow_classic_era",))
GAMES = {game.id: game for game in (FOREVER, ERA)}


def of_package(package):
    """The Game a package installs into, or None."""
    return next((game for game in GAMES.values() if game.package == package), None)


def _varint(data, at):
    value = shift = 0
    while True:
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, at


def _fields(data):
    """[(field number, value)] of one protobuf message: bytes for a length-delimited field, an int for
    a varint. Raises ValueError (or IndexError) on bytes that are not a message."""
    found, at = [], 0
    while at < len(data):
        key, at = _varint(data, at)
        number, kind = key >> 3, key & 7
        if kind == 0:
            value, at = _varint(data, at)
        elif kind == 2:
            size, at = _varint(data, at)
            value, at = data[at:at + size], at + size
            if at > len(data):
                raise ValueError("field runs past the end")
        elif kind in (1, 5):
            value, at = None, at + (8 if kind == 1 else 4)
        else:
            raise ValueError(f"wire type {kind}")
        found.append((number, value))
    return found


def _text(fields, number):
    value = next((value for n, value in fields if n == number and isinstance(value, bytes)), b"")
    return value.decode("utf-8", errors="replace")


_RECORDS = {}       # product.db path -> ((mtime, size), records), so each file is read once per change


def battlenet_records(path):
    """[(client folder, product)] from one of Battle.net's product.db files: each installed product
    (field 1) with its product code (2), and in its settings (3) the install folder (1) and the client's
    folder in it (13). Only WoW's products (wow, wowf, wow_classic_era...). Empty
    for a file that is missing or that this reading does not understand, so a format change costs this
    one way of finding clients, not the others."""
    path = Path(path)
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if path in _RECORDS and _RECORDS[path][0] == stamp:
            return _RECORDS[path][1]
        data = path.read_bytes()
    except OSError:
        return []
    records = []
    try:
        for number, entry in _fields(data):
            if number != 1 or not isinstance(entry, bytes):
                continue
            entry = _fields(entry)
            settings = _fields(next((value for n, value in entry if n == 3 and isinstance(value, bytes)), b""))
            code, root, sub = _text(entry, 2).strip().lower(), _text(settings, 1).strip(), _text(settings, 13).strip()
            if code.startswith("wow") and root and sub:      # WoW's products only, not Warcraft III's
                records.append((Path(root) / sub, code))
    except (ValueError, IndexError):
        records = []
    _RECORDS[path] = (stamp, records)
    return records


def recorded_product(folder):
    """The product Battle.net's records give a client folder: the list in its World of Warcraft folder,
    else the computer's list. None when neither names it."""
    folder = Path(folder)
    for db in (folder.parent / ".product.db", AGENT_DB):
        for path, code in battlenet_records(db):
            if same_folder(path, folder):
                return code
    return None


def install_roots():
    """World of Warcraft folders: the usual places plus where Battle.net says it installed the game."""
    if sys.platform == "darwin":
        roots = [Path(p).expanduser() for p in MAC_ROOTS]
    else:
        roots = [Path(p) for p in DEFAULT_ROOTS]
    roots += [path.parent for path, _ in battlenet_records(AGENT_DB)]
    if os.name == "nt":
        import winreg
        for key in (r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
                    r"SOFTWARE\Blizzard Entertainment\World of Warcraft"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                    path = Path(winreg.QueryValueEx(handle, "InstallPath")[0])
            except OSError:
                continue
            # InstallPath points at one client folder (often _retail_); the root is its parent.
            roots.append(path.parent if path.name.startswith("_") else path)
    seen, unique = set(), []
    for root in roots:
        key = str(root).lower().rstrip("\\/")
        if key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def is_client(folder):
    """Whether a folder is a game client's: Battle.net records it, or it has a .flavor.info, an AddOns
    folder or the game's program. Any one will do, so a launcher update rewriting one of them, or a
    client never played with addons, does not hide it."""
    folder = Path(folder)
    if not folder.is_dir():
        return False
    return (recorded_product(folder) is not None or (folder / ".flavor.info").is_file()
            or (folder / "Interface" / "AddOns").is_dir() or any((folder / name).exists() for name in PROGRAMS))


def clients(roots=None):
    """[(client folder, has XPCompass)] for every client folder in the World of Warcraft folders."""
    found = []
    for root in install_roots() if roots is None else roots:
        try:
            folders = sorted(Path(root).iterdir())
        except OSError:
            continue
        for folder in folders:
            addons = folder / "Interface" / "AddOns"
            if folder.name.startswith("_") and is_client(folder):
                found.append((folder, any((addons / name).is_dir() for name in ADDONS)))
    return found


def product(folder):
    """The product a client folder is: what Battle.net's records say, else the second line of its
    .flavor.info, else what its folder's name means, else None."""
    recorded = recorded_product(folder)
    if recorded:
        return recorded
    try:
        lines = (Path(folder) / ".flavor.info").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    if len(lines) > 1 and lines[1].strip():
        return lines[1].strip().lower()
    return FOLDER_PRODUCTS.get(Path(folder).name)


def game_of(folder):
    """The Game a client folder is, or None for any other product (Retail, a PTR...)."""
    found = product(folder)
    return next((game for game in GAMES.values() if found in game.products), None)


def other_game(folder, game="forever"):
    """The other Game whose client `folder` is, or None. Only a folder whose product is one of our other
    games' is wrong for `game`: a folder with no .flavor.info or a product nobody knows yet (the release
    client's, before it is on the list) is the player's call."""
    game = GAMES[game] if isinstance(game, str) else game
    found = game_of(folder)
    return found if found and found.id != game.id else None


def same_folder(a, b):
    """Whether two paths name one folder (case and separators as the system sees them)."""
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def newer(folder, game="forever", roots=None):
    """A client of a newer product of `game` than `folder`'s (the beta's folder, with the release client
    beside it), or None. For a player who chose a folder: their choice stands, and the app says so."""
    game = GAMES[game] if isinstance(game, str) else game
    mine = product(folder)
    if mine not in game.products:
        return None
    found = [(path, product(path)) for path, _ in clients(roots)]
    for wanted in game.products[:game.products.index(mine)]:
        for path, found_product in found:
            if found_product == wanted:
                return path
    return None


def pick(roots=None, game="forever"):
    """The client to install `game` into when the player did not choose one: the folder whose product
    is the game's, the newest product first. Two folders of one product (a copy) go to the one that
    already has XPCompass; None when that is still ambiguous, or when the game has no client here."""
    game = GAMES[game] if isinstance(game, str) else game
    found = [(folder, product(folder), has) for folder, has in clients(roots)]
    for wanted in game.products:
        mine = [(folder, has) for folder, found_product, has in found if found_product == wanted]
        if len(mine) == 1:
            return mine[0][0]
        if mine:
            with_addon = [folder for folder, has in mine if has]
            return with_addon[0] if len(with_addon) == 1 else None
    return None
