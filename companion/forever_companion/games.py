"""Find the game client folders on this computer, and say which game each one is.

Battle.net writes `.flavor.info` into each client folder; its second line names the product
(wow_classic_beta, wow_classic_era, wow for retail). That, not the folder's name and not whether
XPCompass is already in it, says which game a client is. Each game of ours has a list of products;
the app installs a game's package into the client whose product is on that list, newest product first.

Forever is the beta now and gets its release client on 4 November. When that client's product is
known, put it at the front of FOREVER's products: a computer that has both then moves to the release
client (service.Updater says so), and the beta folder's files stay as they are. Era and Retail are
never a Forever client, whatever they hold.
"""
import os
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOTS = (r"C:\Program Files (x86)\World of Warcraft", r"C:\Program Files\World of Warcraft")
MAC_ROOTS = ("/Applications/World of Warcraft", "~/Applications/World of Warcraft")
ADDONS = ("XPCompass", "ForeverCoachNext")   # the base addon's folder, and its name before 17 October

# For a client folder without a readable .flavor.info: the product its name means.
FOLDER_PRODUCTS = {"_classic_beta_": "wow_classic_beta", "_classic_era_": "wow_classic_era",
                   "_classic_": "wow_classic", "_retail_": "wow"}


@dataclass(frozen=True)
class Game:
    id: str              # as tools/game.py and every upload name it: "forever" or "era"
    title: str           # as the app shows it (release.PACKAGES' "game")
    package: str         # the package that installs into this game's client
    products: tuple      # .flavor.info products that are this game, the newest first


FOREVER = Game("forever", "WoW Forever", "forever-coach", ("wow_classic_beta",))   # + the release client's product
ERA = Game("era", "WoW Classic Era", "xpcompass-era", ("wow_classic_era",))
GAMES = {game.id: game for game in (FOREVER, ERA)}


def of_package(package):
    """The Game a package installs into, or None."""
    return next((game for game in GAMES.values() if game.package == package), None)


def install_roots():
    """World of Warcraft folders: the usual places plus where Battle.net says it installed the game."""
    if sys.platform == "darwin":
        roots = [Path(p).expanduser() for p in MAC_ROOTS]
    else:
        roots = [Path(p) for p in DEFAULT_ROOTS]
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


def clients(roots=None):
    """[(client folder, has XPCompass)] for every client folder with an AddOns folder."""
    found = []
    for root in install_roots() if roots is None else roots:
        for folder in sorted(Path(root).iterdir()):
            addons = folder / "Interface" / "AddOns"
            if folder.name.startswith("_") and addons.is_dir():
                found.append((folder, any((addons / name).is_dir() for name in ADDONS)))
    return found


def product(folder):
    """The product a client folder is: the second line of its .flavor.info, else what its folder's
    name means, else None."""
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
