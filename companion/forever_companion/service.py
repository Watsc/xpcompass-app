"""The background updater: every few minutes, ask the build source for each package's newest build
and install it when that is safe. The tray (tray.py) shows what it says; nothing here draws.

When to install:
- the game is closed: install now;
- the game is open and the build only changes file contents: install now, and say to /reload;
- the game is open and the build adds, removes or renames files or changes a .toc: wait, since a
  /reload would mix the old file list with new files; install as soon as the game closes;
- one exception: the switch to the members edition when the public edition already holds its folder empty (fills_in):
  install now, and say "You're verified: type /reload in game".

While the server says an Only Bans account waits on verification, each check also sends the characters on this PC
(linking.py), and checks come every minute for its first hour.

Only the manifest (two small files) is fetched on each check; the zip only when there is something
to install. check_app does the same for the app itself (selfupdate.py). Between checks the app asks the
server whether the account's roles changed (wait), so an Only Bans role given or taken shows within a minute.
"""
import json
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import auth, channels, games, install, linking, release, selfupdate, system, uploads
from .catalog import Catalog
from .release import PACKAGES, RETIRED, ReleaseError, SignInNeeded

CHECK_EVERY = 15 * 60       # seconds between checks
WAITING_EVERY = 60          # while a build waits for the game to close
ASK_AT_LEAST = 15           # seconds: the fastest the server may have the app ask about its roles
# What the player is told when the account's edition changes. Going to the members edition adds an addon folder,
# and the game reads the list of addons only when it starts, so a /reload can't load it (install.needs). Since
# 11 November the public edition carries that folder empty (tools/publish_build.py's stubs), so the members edition
# only fills it in: that one switch installs with the game open, and a /reload loads it ("reload", fills_in).
EDITION_NEWS = {
    "members": {"reload": "You're verified: type /reload in game to load every Horde guide.",
                "waiting": "Your account now gets every Horde guide. Close the game to install them, then start it again.",
                "installed": "Your account now gets every Horde guide. Installed: they load when you start the game.",
                "running": "Your account now gets every Horde guide. Installed: restart the game fully to load them, "
                           "a /reload is not enough."},
    "public": {"waiting": "Your account no longer has Only Bans access. Member guide files were withdrawn. "
               "Close the game to finish installing the starting zones.",
               "installed": "Your account no longer has Only Bans access. XPCompass is back to the starting zones.",
               "running": "Your account no longer has Only Bans access. XPCompass goes back to the starting zones "
                          "when you restart the game."},
}


def settings_path():
    return system.data_dir() / "companion.json"


def fills_in(package, current, offered):
    """Whether going from `current` (the public edition) to `offered` (the members edition) only fills in folders
    the public edition already holds empty, with nothing else needing more than a /reload: then it may install
    while the game runs. A public edition from before the empty folder, or any other change to the file list or a
    .toc outside those folders, still waits for the game to close."""
    editions = PACKAGES.get(package, {}).get("editions") or {}
    filled = set(editions.get("members", ())) - set(editions.get("public", ()))
    if not current or not offered or not filled or not filled <= set(current.get("addons") or ()):
        return False

    def outside(manifest):
        return {"files": {name: digest for name, digest in (manifest.get("files") or {}).items()
                          if name.split("/")[0] not in filled}}
    return install.needs(outside(current), outside(offered)) != "restart"


@dataclass
class Settings:
    source: str = auth.SERVER + "/builds"   # https base URL or folder; <source>/<package>/manifest.json
    channel: str = "stable"             # one explicit choice for addon and app updates
    app_switch_to: str = None           # explicit channel switch, including older legacy app builds
    entitlement_account: str = None    # non-secret hash binding managed member payloads to an account
    channel_migrated: bool = True        # old configs migrate installed tester builds once
    server: str = auth.SERVER           # Discord sign-in
    game: str = None                    # WoW Forever's client folder, when the player chose it; None finds it by product
    clients: dict = field(default_factory=dict)        # the other games' folders the player chose, by game id ("era")
    last_clients: dict = field(default_factory=dict)   # the client each game's build was last installed into, by game id
    packages: list = field(default_factory=lambda: ["forever-coach"])
    auto_ticked: list = field(default_factory=list)    # packages the app ticked itself on finding their game, once each
    autostart_set: bool = False         # Start with Windows was turned on once; the player may turn it off
    sharing: bool = True                # False: turned off before sharing was required; no uploads, no updates (uploads.py)
    data_channel: bool = True           # "Data from the server": the app keeps XPCompass_Data up to date (datachannel.py)
    combat_log_days: int = 7            # combat logs XPCompass caused are kept this many days at least (combatlogs.py)
    path: Path = field(default=None, compare=False, repr=False)     # where it is saved; None: settings_path()

    KEYS = ("source", "channel", "app_switch_to", "channel_migrated", "entitlement_account", "server", "game", "clients", "last_clients", "packages", "auto_ticked",
            "autostart_set", "sharing", "data_channel", "combat_log_days")

    def __post_init__(self):
        self.channel = channels.canonical(self.channel)
        if self.app_switch_to is not None:
            self.app_switch_to = channels.canonical(self.app_switch_to)

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
        """The addon and app use the same explicit stable/nightly choice."""
        return channels.source(self.source, self.channel)

    @classmethod
    def load(cls, path=None):
        path = path or settings_path()
        if not path.exists():
            return cls(path=path)
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("source", "server"):     # the server moved to its own domain
            if isinstance(data.get(key), str):
                data[key] = auth.OLD_SERVER.sub(auth.SERVER, data[key], count=1)
        if "channel" not in data:
            data["channel_migrated"] = False
            if str(data.get("source", "")).rstrip("/").endswith("/builds-prerelease"):
                data["channel"] = "nightly"
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
        self.to_release = set()      # packages the player asked to switch from a testers build to the players' build
        self.said = set()            # (package, text) already reported, so a notice shows once
        self.notices = {}            # game id -> a standing notice for the window (moved clients), until dismissed
        self.status = "Not checked yet"
        self.info = {}               # package -> what the window shows: state, text, builds, notes
        self.checked_at = None
        self.access = None           # what the server said the account holds at the last check (auth.Account.access)
        self.linker = linking.Linker(account) if account else None    # the characters on this PC, while verifying

    def game(self, game_id=games.FOREVER.id):
        """The client folder of one game: the player's choice, else the one found by product. A choice
        that is another game's client is refused, not installed into: both games' packages own the same
        folders, so each would move the other's aside at every check."""
        title = games.GAMES[game_id].title
        chosen = self.settings.chosen(game_id)
        if chosen and self.outgrown(chosen, game_id):
            self.settings.choose(game_id, None)         # moved() then says where it installs now
            if self.settings.path:
                self.settings.save()
            chosen = None
        if chosen:
            other = games.other_game(chosen, game_id)
            if other:
                raise ReleaseError(f"{chosen} is a {other.title} client, not {title}. "
                                   f"Choose your {title} folder.")
            return Path(chosen)
        found = games.pick(game=game_id)
        if not found:
            raise ReleaseError(f"Couldn't find your {title} game. Choose its folder.")
        return Path(found)

    @staticmethod
    def outgrown(chosen, game_id):
        """Whether a folder the player chose for a game should give way to the client the app finds: the
        chosen one is gone, or it is an older product of the game (WoW Forever's beta, which uninstalling
        leaves behind) while its newest client (the live game) is here."""
        found = games.pick(game=game_id)
        if not found or games.same_folder(found, chosen) or games.product(found) != games.GAMES[game_id].products[0]:
            return False
        return not Path(chosen).is_dir() or games.newer(chosen, game_id) is not None

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
                    problems[other] = f"{names} are both set to {folder}. Choose each game's own folder."
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
        """A build waiting for the game to close, a tester's test builds, and an Only Bans account waiting on
        verification in its first hour (linking.FAST_FOR) are checked every minute."""
        testing = self.settings.channel == "nightly"
        verifying = bool(self.linker and self.linker.fast())
        return WAITING_EVERY if self.waiting or testing or verifying else CHECK_EVERY

    def link_characters(self):
        """While the server says this account waits on verification, send the characters on this PC (linking.py),
        from WoW Forever's client only. Returns {how: names} sent."""
        if not self.linker:
            return {}
        verifying = bool(self.access and self.access.get("verifying") is True)
        try:
            client = self.game(games.FOREVER.id) if verifying else None
        except ReleaseError:
            client = None
        try:
            return self.linker.step(self.access, client)
        except Exception:   # noqa: BLE001 - linking never stops a check
            return {}

    def ask_every(self):
        """Seconds between questions about the account's roles, as the server says; None when it says not to ask."""
        every = (self.access or {}).get("every")
        if not isinstance(every, (int, float)) or isinstance(every, bool):
            return None
        return max(ASK_AT_LEAST, every)

    def access_changed(self):
        """Whether the account holds something other than it did at the last check."""
        now = self.account.access() if self.account else None
        if now is None or self.access is None:
            self.access = self.access or now
            return False
        if (now["roles"], now["prerelease"]) == (self.access["roles"], self.access["prerelease"]):
            self.access = now           # the server may have changed how often to ask
            return False
        self.said = {said for said in self.said if said[0] == ""}      # an edition's news can show again
        self.account.recheck()
        return True

    def wait(self, wake, stopping=lambda: False):
        """Until the next check is due: `wake` is set (Check now, a changed setting), the delay is up, or the
        server says the account's roles changed."""
        left = self.next_delay()
        while left > 0 and not stopping():
            step = min(left, self.ask_every() or left)
            if wake.wait(step):
                break
            left -= step
            if left > 0 and self.ask_every() and self.access_changed():
                break
        wake.clear()

    def check(self):
        """One round over every package. Returns the events worth telling the player, each once."""
        events = []
        self.migrate_channel()
        if not self.settings.active_source():
            self.status = "No build source set"
            return self._once(events, Event("error", "", "No build source set yet."))
        token = self._token(events)
        self.bind_account()
        if self.account:
            # Asked before the builds are: a change after this shows at the next question, not lost between.
            self.access = self.account.access() if self.account.session() else None
            self.link_characters()
            catalog_error = self.catalog.refresh(channels.source(self.account.server, self.settings.channel), token)
            self.withdraw_denied(catalog_error is None)
        self.tick_found_games(events)
        wanted = [package for package in self.settings.packages if package in PACKAGES]
        if self.account and not self.account.session():     # sign-in comes first: no anonymous mode
            self.withdraw_membership()
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
        """Let the sign-in notices show again the next time they apply; a waiting account's minute checks start over."""
        self.said = {said for said in self.said if not said[1].startswith("Sign in")}
        if self.linker:
            self.linker.restart()

    def bind_account(self):
        if not self.account:
            return
        session = self.account.session()
        identity = ((session or {}).get("account_id") or (session or {}).get("token"))
        bound = hashlib.sha256(json.dumps([self.settings.server, identity]).encode()).hexdigest() if identity else None
        before = self.settings.entitlement_account
        if bound != before:
            # A new account has no right to the previous account's private files while offline.
            self.withdraw_membership()
            from .catalog import built_in
            self.catalog.data, self.catalog.live = built_in(), False
            self.settings.entitlement_account = bound
            if self.settings.path:
                self.settings.save()

    def withdraw_denied(self, catalog_current=False):
        """A denied channel cannot hide a fresh account-level entitlement loss.

        Forever's member payload follows guild membership; Era's follows its
        separate tester entitlement. Unticking updates does not retain access.
        """
        denied = set()
        if self.access is not None:
            for package, about in PACKAGES.items():
                missing = (self.access.get("prerelease") is False if about["client"] == "era"
                           else "roles" in self.access and not self.access["roles"])
                if missing:
                    denied.add(package)
        if catalog_current:
            for entry in self.catalog.entries():
                if entry["id"] in PACKAGES and entry.get("edition") == "public":
                    denied.add(entry["id"])
            if self.catalog.member() is False:
                denied.update(package for package, about in PACKAGES.items() if about["client"] != "era")
        self.withdraw_membership(denied)

    def withdraw_membership(self, wanted=None):
        for package in PACKAGES if wanted is None else wanted:
            try:
                game = self.game(PACKAGES[package]["client"])
                current = install.installed(game, package)
                before = install.state_dir(game, package) / "previous" / "installed.json"
                previous = json.loads(before.read_text(encoding="utf-8")) if before.is_file() else None
                if any(receipt and receipt.get("edition") == "members" for receipt in (current, previous)):
                    install.revoke_membership(game, package)
            except (ReleaseError, OSError, ValueError) as error:
                self._note(package, "error", f"Member files could not be withdrawn: {error}")

    def migrate_channel(self):
        """Preserve an installed legacy tester choice; access alone never opts somebody in."""
        if self.settings.channel_migrated:
            return
        nightly = channels.canonical((self.app or {}).get("channel")) == "nightly"
        for package in self.settings.packages:
            if package not in PACKAGES:
                continue
            try:
                current = install.installed(self.game(PACKAGES[package]["client"]), package)
                nightly = nightly or channels.canonical((current or {}).get("channel")) == "nightly"
            except (ReleaseError, OSError, ValueError):
                continue
        if nightly:
            self.settings.channel = "nightly"
        self.settings.channel_migrated = True
        if self.settings.path:
            self.settings.save()

    def app_source(self):
        """The selected stable/nightly build source, with the signed-in account's token."""
        try:
            token = self.account and self.account.token()
        except auth.AuthError:
            token = None
        return self.settings.active_source(), token

    def check_app(self):
        """(manifest of a newer build of the app itself or None, events). The tray applies it."""
        events = []
        self.migrate_channel()
        if not self.settings.source or not self.app:
            return None, events
        try:
            source, token = self.app_source()
            explicit = self.settings.app_switch_to == self.settings.channel and self.app.get("channel") is None
            offered = selfupdate.offered(source, self.app, self.keys, token, allow_older=explicit)
            if self.app.get("channel") == self.settings.channel and self.settings.app_switch_to is not None:
                self.settings.app_switch_to = None
                self.settings.save()
            return offered, events
        except ReleaseError as error:
            if "is missing" in str(error):          # no app build published at this source yet
                return None, events
            return None, self._once(events, Event("error", release.app_package(), f"Companion app update: {error}"))

    def _check(self, game, package, events, token=None):
        name = PACKAGES[package]["name"]
        source = self.settings.active_source()
        offered = release.latest(source, package, self.keys, token)
        current = install.installed(game, package)
        # A testers build (one test_build.py published, or one tools/owner_install.py put in) is never stepped back
        # to stable while Nightly remains selected. Explicit stable selection and access withdrawal
        # take precedence over retention; channel selection never grants membership.
        was, now = (current or {}).get("edition"), offered.get("edition")
        revoked = was == "members" and now == "public"
        if revoked:
            install.revoke_membership(game, package)
        held = (channels.canonical((current or {}).get("channel")) == "nightly"
                and channels.canonical(offered.get("channel")) != "nightly"
                and self.settings.channel == "nightly" and not revoked)
        if held and package not in self.to_release:
            self.waiting.pop(package, None)
            self.forced.discard(package)
            players = offered.get("version") or offered["build"]
            self._note(package, "testing", f"Nightly build. The stable build is {players}.", current, offered)
            self.info[package]["channel"] = "prerelease"
            return f"{name} {current['build']}, a nightly build kept (the stable build is {offered['build']})"
        if not revoked and channels.canonical(offered.get("channel")) != self.settings.channel:
            raise ReleaseError("the source did not offer the selected build channel; nothing was installed")
        # Moving between a test build and the release, or between editions, may step back to an older build:
        # the server decides who is a tester and who a member, so that is allowed, once. A release's two
        # editions are stamped moments apart, the public one first.
        same_channel = channels.canonical((current or {}).get("channel")) == channels.canonical(offered.get("channel"))
        was, now = (current or {}).get("edition"), offered.get("edition")
        moved_edition = bool(was and now and was != now)
        news = EDITION_NEWS.get(now) if moved_edition and same_channel else None
        switching = bool(current) and (not same_channel or moved_edition)
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
        # Just verified: the members edition fills in the empty folder the public one holds, and a /reload loads it.
        reload_in = bool(news) and was == "public" and now == "members" and fills_in(package, current, offered)
        if need == "restart" and current and self.running() and not forced and not reload_in:
            self.waiting[package] = offered["build"]
            self._note(package, "waiting", news["waiting"] if news else "Update ready. It installs when you close the game.",
                       current, offered)
            self._once(events, Event("waiting", package, news["waiting"] if news else
                                     f"{name} {offered['build']} is ready. It installs when you close the game."))
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
        self.to_release.discard(package)
        if news:
            text = news["reload" if reload_in and self.running() else "running" if self.running() else "installed"]
        elif not self.running() and result.needs != "nothing":
            text = f"{name} updated to {result.build}. It loads next time you start the game."
        else:
            text = result.advice()
        events.append(Event("installed", package, text))
        short = text if news else text.replace(f"{name} ", "", 1)
        self._note(package, "installed", short[:1].upper() + short[1:], install.installed(game, package), offered)
        return f"{name} {result.build}, installed"

    def _once(self, events, event):
        if (event.package, event.text) not in self.said:
            self.said.add((event.package, event.text))
            events.append(event)
        return events
