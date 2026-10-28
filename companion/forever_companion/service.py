"""The background updater: every few minutes, ask the build source for each package's newest build
and install it when that is safe. The tray (tray.py) shows what it says; nothing here draws.

When to install:
- the game is closed: install now;
- the game is open and the build only changes file contents: install now, and say to /reload;
- the game is open and the build adds, removes or renames files or changes a .toc: wait, since a
  /reload would mix the old file list with new files; install as soon as the game closes.

Only the manifest (two small files) is fetched on each check; the zip only when there is something
to install. check_app does the same for the app itself (selfupdate.py).
"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import auth, games, install, release, selfupdate, system, uploads
from .catalog import Catalog
from .release import PACKAGES, RETIRED, ReleaseError, SignInNeeded

CHECK_EVERY = 15 * 60       # seconds between checks
WAITING_EVERY = 60          # while a build waits for the game to close


def settings_path():
    return system.data_dir() / "companion.json"


@dataclass
class Settings:
    source: str = auth.SERVER + "/builds"   # https base URL or folder; <source>/<package>/manifest.json
    server: str = auth.SERVER           # Discord sign-in
    game: str = None                    # WoW Forever's client folder, when the player chose it; None finds it by product
    clients: dict = field(default_factory=dict)        # the other games' folders the player chose, by game id ("era")
    last_clients: dict = field(default_factory=dict)   # the client each game's build was last installed into, by game id
    packages: list = field(default_factory=lambda: ["forever-coach"])
    auto_ticked: list = field(default_factory=list)    # packages the app ticked itself on finding their game, once each
    autostart_set: bool = False         # Start with Windows was turned on once; the player may turn it off
    sharing: bool = True                # False: turned off before sharing was required; no uploads, no updates (uploads.py)
    path: Path = field(default=None, compare=False, repr=False)     # where it is saved; None: settings_path()

    KEYS = ("source", "server", "game", "clients", "last_clients", "packages", "auto_ticked",
            "autostart_set", "sharing")

    def chosen(self, game_id):
        """The client folder the player chose for a game (games.py's id), or None. A choice outranks
        the one the app finds by product."""
        return (self.game if game_id == games.FOREVER.id else self.clients.get(game_id)) or None

    def choose(self, game_id, folder):
        """Record the player's choice of a game's client folder; None drops it."""
        folder = str(folder) if folder else None
        if game_id == games.FOREVER.id:
            self.game = folder
        elif folder:
            self.clients[game_id] = folder
        else:
            self.clients.pop(game_id, None)

    def active_source(self):
        """Where builds come from. Testers get test builds from the same address: the server decides
        by who is signed in, not the app."""
        return self.source

    @classmethod
    def load(cls, path=None):
        path = path or settings_path()
        if not path.exists():
            return cls(path=path)
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("source", "server"):     # the server moved to its own domain
            if isinstance(data.get(key), str):
                data[key] = auth.OLD_SERVER.sub(auth.SERVER, data[key], count=1)
        data["packages"] = [name for name in data.get("packages", ["forever-coach"]) if name not in RETIRED]
        return cls(**{key: data[key] for key in cls.KEYS if key in data}, path=path)

    def save(self, path=None):
        path = path or self.path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {key: getattr(self, key) for key in self.KEYS}
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")


game_running = system.game_running


def sections(notes):
    """What's new as [{"title", "items"}]; older builds carried a plain list of lines."""
    notes = notes or []
    if notes and isinstance(notes[0], str):
        return [{"title": "Changes", "items": list(notes)}]
    return [section for section in notes if isinstance(section, dict) and section.get("items")]


def releases(manifest):
    """The release history for the What's new window, newest first."""
    if not manifest:
        return []
    newest = {"version": manifest.get("version") or manifest["build"], "date": manifest.get("published", "")[:10],
              "notes": sections(manifest.get("notes"))}
    older = [{"version": item.get("version") or item.get("build"), "date": item.get("date", ""),
              "notes": sections(item.get("notes"))} for item in manifest.get("history", [])]
    return [entry for entry in [newest, *older] if entry["notes"]]


@dataclass
class Event:
    kind: str          # "installed", "waiting", "signin", "error"
    package: str
    text: str


class Updater:
    def __init__(self, settings, keys=None, running=game_running, app=None, account=None, catalog=None):
        self.settings, self.keys, self.running, self.account = settings, keys, running, account
        if catalog is None:     # without an account there is nobody to ask: the app's own list
            cache = settings.path.parent / "catalog.json" if settings.path else None
            catalog = Catalog(cache) if account else Catalog(False)
        self.catalog = catalog
        self.app = selfupdate.current() if app is None else app      # None when run from source
        self.waiting = {}            # package -> build waiting for the game to close
        self.forced = set()          # packages the player asked to update now, game open or not
        self.said = set()            # (package, text) already reported, so a notice shows once
        self.notices = {}            # game id -> a standing notice for the window (moved clients), until dismissed
        self.status = "Not checked yet"
        self.info = {}               # package -> what the window shows: state, text, builds, notes
        self.checked_at = None

    def game(self, game_id=games.FOREVER.id):
        """The client folder of one game: the player's choice, else the one found by product. A choice
        that is another game's client is refused, not installed into: both games' packages own the same
        folders, so each would move the other's aside at every check."""
        title = games.GAMES[game_id].title
        chosen = self.settings.chosen(game_id)
        if chosen:
            other = games.other_game(chosen, game_id)
            if other:
                raise ReleaseError(f"{chosen} is a {other.title} client, not {title}. "
                                   f"Choose the {title} folder in Options.")
            return Path(chosen)
        found = games.pick(game=game_id)
        if not found:
            raise ReleaseError(f"could not find a {title} client; choose its folder in the settings"
                               if game_id != games.FOREVER.id else
                               "could not tell which game client to use; choose it in the settings")
        return Path(found)

    def resolve(self, wanted=None):
        """({game id: client folder}, {game id: why it has none}) for the games in `wanted` (default all).
        Two games on one folder get none: each would install over the other, and one client's records
        would go up as both games'."""
        folders, problems = {}, {}
        for game_id in (wanted if wanted is not None else games.GAMES):
            try:
                folders[game_id] = self.game(game_id)
            except ReleaseError as error:
                problems[game_id] = str(error)
        for game_id, folder in list(folders.items()):
            sharing = [other for other, theirs in folders.items() if games.same_folder(folder, theirs)]
            if len(sharing) > 1:
                names = " and ".join(games.GAMES[other].title for other in sharing)
                for other in sharing:
                    problems[other] = f"{names} are both set to {folder}. Choose each game's own folder in Options."
                    folders.pop(other, None)
        return folders, problems

    def clients(self):
        """{game id: client folder} for each game that has a client here; the others are left out."""
        return self.resolve()[0]

    def tick_found_games(self, events):
        """The first time a client of another game than Forever turns up, and the server's list offers its
        package, tick the package for the player, as a new player would expect; once. An untick sticks: the
        package is then in auto_ticked, so it is not ticked again. Forever's package is ticked from the
        start, and an existing install is never touched."""
        folders = self.clients()
        for game_id, game in games.GAMES.items():
            package = game.package
            if (game_id == games.FOREVER.id or package in self.settings.auto_ticked or game_id not in folders
                    or package not in PACKAGES or not self.catalog.live or not self.catalog.installable(package)):
                continue
            self.settings.auto_ticked.append(package)
            if package not in self.settings.packages:
                self.settings.packages = [name for name in PACKAGES if name in {*self.settings.packages, package}]
                self._once(events, Event("ticked", package, f"Found your {game.title} game at {folders[game_id]}. "
                                                            f"{PACKAGES[package]['name']} will be installed there; "
                                                            "untick it in the window to stop."))
            if self.settings.path:
                self.settings.save()

    def client_rows(self):
        """What Options shows for each game's client: its folder (None: not found), whether the player
        chose it, and a newer client of the same game that their choice is passing over. A game
        the player has no client for and has not ticked is left out; Forever is always there."""
        rows = []
        found, problems = self.resolve()
        for game_id, game in games.GAMES.items():
            folder = found.get(game_id)
            if folder is None and game_id != games.FOREVER.id and game.package not in self.settings.packages:
                continue
            chosen = bool(self.settings.chosen(game_id))
            newer = games.newer(folder, game_id) if folder and chosen else None
            rows.append({"id": game_id, "title": game.title, "path": folder and str(folder), "chosen": chosen,
                         "newer": newer and str(newer), "problem": problems.get(game_id)})
        return rows

    def notice_rows(self):
        return [{"id": game_id, "text": text} for game_id, text in self.notices.items()]

    def moved(self, game_id, folder):
        """Say, once, that a game's build now installs into another client than it last did (its
        release client has appeared beside the beta); the old client's files are left alone. The
        folder is then remembered as the game's. Returns the notice or None."""
        before = self.settings.last_clients.get(game_id)
        same = before and Path(before).as_posix().lower() == Path(folder).as_posix().lower()
        if same:
            return None
        self.settings.last_clients[game_id] = str(folder)
        if self.settings.path:
            self.settings.save()
        if not before:
            return None
        text = (f"XPCompass now installs into your {games.GAMES[game_id].title} client at {folder}, not {before}. "
                f"The copy in {before} is left as it is.")
        self.notices[game_id] = text
        return text

    def next_delay(self):
        """A build waiting for the game to close, or a tester's test builds, are checked every minute."""
        testing = any(info.get("channel") == "prerelease" for info in self.info.values())
        return WAITING_EVERY if self.waiting or testing else CHECK_EVERY

    def check(self):
        """One round over every package. Returns the events worth telling the player, each once."""
        events = []
        if not self.settings.active_source():
            self.status = "No build source set"
            return self._once(events, Event("error", "", "No build source set yet."))
        token = self._token(events)
        if self.account:
            self.catalog.refresh(self.account.server, token)
        self.tick_found_games(events)
        wanted = [package for package in self.settings.packages if package in PACKAGES]
        if self.account and not self.account.session():     # sign-in comes first: no anonymous mode
            for package in wanted:
                self._note(package, "signin", auth.SIGN_IN_FIRST)
            self.status = auth.SIGN_IN_FIRST
            self.checked_at = time.time()
            return events
        folders, no_client = self.resolve(list(dict.fromkeys(PACKAGES[package]["client"] for package in wanted)))
        if wanted and not folders:      # nothing to install into at all: say so once, for everything
            error = next(iter(no_client.values()))
            self.status = error
            return self._once(events, Event("error", "", error))
        for game_id, folder in folders.items():
            if not self.running():
                install.move_saved_settings(folder)      # the addons' folders were renamed on 17 October
            notice = self.moved(game_id, folder)
            if notice:
                self._once(events, Event("moved", games.GAMES[game_id].package, notice))
        lines = []
        self.info = {package: info for package, info in self.info.items() if package in self.settings.packages}
        if any(uploads.opted_out(self.settings, folder) for folder in folders.values()):
            self.notices["sharing"] = uploads.OPTED_OUT     # one line at the window's top, back at each check
            for package in self.settings.packages:      # sharing off: no addon updates (DECISIONS.md)
                if package in PACKAGES:
                    self._note(package, "paused", uploads.OPTED_OUT)
            self.status = uploads.OPTED_OUT
            self.checked_at = time.time()
            return self._once(events, Event("sharing", "", uploads.OPTED_OUT))
        self.notices.pop("sharing", None)
        for package in self.settings.packages:
            if package not in PACKAGES:
                continue
            name = PACKAGES[package]["name"]
            game = folders.get(PACKAGES[package]["client"])
            if game is None:                    # this package's game has no client here; the others go on
                text = no_client[PACKAGES[package]["client"]]
                self._note(package, "error", text)
                lines.append(f"{name}: {text}")
                continue
            if not self.catalog.installable(package):       # the server says this account can't have it
                reason = self.catalog.locked_reason(package)
                self._note(package, "locked", reason)
                lines.append(f"{name}: {reason}")
                continue
            missing = [PACKAGES[need]["name"] for need in PACKAGES[package]["needs"]
                       if need not in self.settings.packages]
            if missing:
                self._note(package, "needs", f"Needs {', '.join(missing)}. Tick it too.")
                lines.append(f"{name}: needs {', '.join(missing)}")
                continue
            try:
                try:
                    lines.append(self._check(game, package, events, token))
                except SignInNeeded as need:
                    if need.code != "token_expired" or not self.account:
                        raise
                    token = self.account.refresh()["token"]         # once, then try again
                    lines.append(self._check(game, package, events, token))
            except (SignInNeeded, auth.AuthError) as need:
                lines.append(f"{name}: sign in with Discord")
                self._note(package, "signin", "Sign in with Discord to get this addon.")
                self._once(events, Event("signin", package, f"Sign in with Discord to get {name}. {need}"))
            except ReleaseError as error:
                if "manifest.json is missing" in str(error):      # nothing published for it yet
                    lines.append(f"{name}: no build published yet")
                    self._note(package, "none", "No build published yet.")
                    continue
                lines.append(f"{name}: {error}")
                self._note(package, "error", str(error))
                self._once(events, Event("error", package, f"{name}: {error}"))
        self.status = "\n".join(lines)
        self.checked_at = time.time()
        return events

    def _note(self, package, state, text, current=None, offered=None):
        """Record what the window shows for `package`."""
        info = self.info.setdefault(package, {})
        info.update(state=state, text=text)
        if state not in ("downloading", "installing"):
            info.pop("progress", None)
        if current is not None or offered is not None:
            info.update(installed=current and current["build"], offered=offered and offered["build"],
                        installed_version=current and (current.get("version") or current["build"]),
                        offered_version=offered and (offered.get("version") or offered["build"]),
                        published=offered and offered.get("published"),
                        notes=(offered or {}).get("notes", []), releases=releases(offered),
                        edition=(offered or current or {}).get("edition"),
                        channel=(offered or current or {}).get("channel"))

    def _token(self, events):
        if not self.account:
            return None
        try:
            return self.account.token()
        except auth.AuthError as error:
            self._once(events, Event("signin", "", str(error)))
            return None

    def signed_in(self):
        """Let the sign-in notices show again the next time they apply."""
        self.said = {said for said in self.said if not said[1].startswith("Sign in")}

    def app_source(self):
        """Where the app's own build comes from, with the token: the server sends testers its test build."""
        try:
            token = self.account and self.account.token()
        except auth.AuthError:
            token = None
        return self.settings.source, token

    def check_app(self):
        """(manifest of a newer build of the app itself or None, events). The tray applies it."""
        events = []
        if not self.settings.source or not self.app:
            return None, events
        try:
            source, token = self.app_source()
            return selfupdate.offered(source, self.app, self.keys, token), events
        except ReleaseError as error:
            if "is missing" in str(error):          # no app build published at this source yet
                return None, events
            return None, self._once(events, Event("error", release.app_package(), f"Companion app update: {error}"))

    def _check(self, game, package, events, token=None):
        name = PACKAGES[package]["name"]
        source = self.settings.active_source()
        offered = release.latest(source, package, self.keys, token)
        current = install.installed(game, package)
        # Moving between a test build and the release may step back to an older build: the server
        # decides who is a tester, so that is allowed, once.
        switching = bool(current) and current.get("channel", "release") != offered.get("channel", "release")
        if current and not switching and install.new_is_older(current, offered):
            self._note(package, "current", f"Up to date ({current['build']}, newer than the server's).",
                       current, offered)
            return f"{name} {current['build']} (newer than the source's {offered['build']})"
        need = install.needs(current, offered)
        if need == "nothing" and not install.local_changes(game, package):
            self.waiting.pop(package, None)
            if current and (current.get("version"), current.get("notes")) != (offered.get("version"), offered.get("notes")):
                install.remember(game, offered)
                current = offered
            self._note(package, "current", "Up to date.", current, offered)
            return f"{name} {offered['build']}, up to date"
        forced = package in self.forced
        self.forced.discard(package)
        if need == "restart" and current and self.running() and not forced:
            self.waiting[package] = offered["build"]
            self._note(package, "waiting", "Update ready. It installs when you close the game.", current, offered)
            self._once(events, Event("waiting", package, f"{name} {offered['build']} is ready. "
                                                          f"It installs when you close the game."))
            return f"{name} {offered['build']} waits for the game to close"
        def progress(done, total):
            self._note(package, "downloading", f"Downloading… {done * 100 // total}%" if total else "Downloading…",
                       current, offered)
            self.info[package]["progress"] = done / total if total else None

        files = release.download(source, offered, token, progress)
        self._note(package, "installing", "Installing…", current, offered)
        self.info[package]["progress"] = 1.0
        result = install.install(game, offered, files, allow_older=switching)
        self.info[package].pop("progress", None)
        self.waiting.pop(package, None)
        if not self.running() and result.needs != "nothing":
            text = f"{name} updated to {result.build}. It loads next time you start the game."
        else:
            text = result.advice()
        events.append(Event("installed", package, text))
        short = text.replace(f"{name} ", "", 1)
        self._note(package, "installed", short[:1].upper() + short[1:], install.installed(game, package), offered)
        return f"{name} {result.build}, installed"

    def _once(self, events, event):
        if (event.package, event.text) not in self.said:
            self.said.add((event.package, event.text))
            events.append(event)
        return events
