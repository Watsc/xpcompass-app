"""The app: runs the updater (service.py) in the background, with a tray icon and a window.

The window (window.py, ui.html) opens at start, or only at the first start when the system starts
the app at login (--background), and from the tray or menu bar icon. Notices (an update installed,
one waiting for the game to close, a problem) also appear as system notifications, each once.
What differs between Windows and macOS lives in system.py.
"""
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from . import auth, games, release, selfupdate, service, system, uploads, window, tls
from . import combatlogs, datachannel, gamelogs, market, sync, jobs
from .release import PACKAGES, TRUSTED_KEYS, ReleaseError
from . import level_medians

QUIET = 300          # seconds with no click in an open window before a waiting code update switches anyway

LOGO = Path(__file__).resolve().parents[2] / "assets" / "branding" / "XPCompassLogo-256.png"
FOLDER_HINT = {"forever": "_forever_", "era": "_classic_era_"}      # for the message about a wrong folder

# Kept under their old names for the rest of the app and the tests.
starts_with_windows = system.starts_at_login
set_start_with_windows = system.set_start_at_login
single_instance = system.single_instance


def icon_image():
    try:
        return Image.open(system.bundled(LOGO.name, LOGO)).convert("RGBA")
    except OSError:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(image).ellipse((4, 4, 60, 60), fill=(212, 175, 55, 255))
        return image


def start_with_windows_by_default(settings, packaged, setter=None):
    """Turn start-at-login on at the packaged app's first start, once: unticking it later sticks.
    A copy run from source is a developer's checkout, so it never adds itself."""
    if packaged and not settings.autostart_set:
        (setter or system.set_start_at_login)(True)
        settings.autostart_set = True
        settings.save()


def ago(seconds):
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


class Tray:
    """The app: the background updater, the tray icon and the window."""

    def __init__(self, settings=None, show=True, account=None, updater=None, make_window=True, uploader=None,
                 updated=False):
        self.settings = settings or service.Settings.load()
        self.account = account or auth.Account(self.settings.server)
        self.updater = updater or service.Updater(self.settings, account=self.account)
        self.uploader = uploader or uploads.Uploader(self.settings, account=self.account)
        self.data = datachannel.Channel(self.settings, self.uploader)
        self.jobs = jobs.Status()
        self.wake = threading.Event()
        self.upload_wake = threading.Event()
        self.stopping, self.checking = False, False
        self.signin = {"busy": False, "url": None, "error": None}
        self.medians = level_medians.LevelMedians(self.uploader)   # other players' level times, into the game
        self.cancel = threading.Event()
        self.app_update = None          # {"state", "progress"} while the app replaces itself
        self.held_back = None           # the app build whose failed install was last reported
        self.logo = window.data_uri(icon_image())
        self.crest = window.data_uri(Image.open(system.bundled("onlybans-crest.png", Path(__file__).parent / "onlybans-crest.png")))
        self.icon = pystray.Icon("ForeverCoachCompanion", icon_image(), "XPCompass", menu=pystray.Menu(
            pystray.MenuItem("Open XPCompass", lambda: self.window.show(), default=True),
            pystray.MenuItem("Check now", lambda: self.check_now()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        ))
        welcome = not self.account.session()        # sign-in comes first: no anonymous mode
        # After an update or a reinstall the app comes back in the tray only; the window waits for a click.
        self.updated = updated
        self.waiting_switch = None              # a code update in place, waiting for a safe moment to switch
        self.touched = 0.0                      # when the player last clicked anything in the window (window.Api)
        self.window = window.Window(self, show or (welcome and not updated)) if make_window else None

    def run(self):
        threading.Thread(target=self.loop, daemon=True).start()
        threading.Thread(target=self.upload_loop, daemon=True).start()
        self.icon.run_detached()
        if self.updated:
            threading.Timer(3, self.say_updated).start()   # once the icon is up to carry it
        window.Window.run()

    def window_closed(self):
        """The window went to the tray: a code update waiting for that switches now."""
        if self.waiting_switch:
            self.wake.set()

    def say_updated(self):
        """After an update or a reinstall: one small notification naming the version, only when it is a
        new one (a failed update starts the old app again, which says nothing)."""
        mine = selfupdate.current() or {}
        build = mine.get("build")
        if build and selfupdate.note_build(build):
            self.notify(f"XPCompass updated to {mine.get('version') or build}.")   # 0.2.2190, as the window shows

    def loop(self):
        self.account.recheck()          # the name may have changed since sign-in
        while not self.stopping:
            if self.update_self():
                return
            self.checking = True
            try:
                events = self.updater.check()
            except Exception as error:   # noqa: BLE001 - keep the app alive and say what went wrong
                self.updater.status = f"Check failed: {error}"
                events = [service.Event("error", "", self.updater.status)]
            self.checking = False
            self.icon.title = ("XPCompass: " + self.updater.status.replace("\n", "; "))[:127]
            for event in events:
                if event.kind != "signin" or self.account.session():
                    self.notify(event.text)
            self.updater.wait(self.wake, lambda: self.stopping)

    def upload_loop(self):
        """Every minute: what each game's client recorded since the last look goes to the server (uploads.py)."""
        while not self.stopping:
            self.upload_once()
            self.upload_wake.wait(uploads.LOOK_EVERY)
            self.upload_wake.clear()

    def upload_once(self):
        """Each stage retains its own retry status and cannot suppress a later stage."""
        _, clients = self.jobs.run("all", "clients", self.updater.clients, uploads.LOOK_EVERY)
        for game_id, folder in (clients or {}).items():
            tasks = (("records", self.uploader.look), ("medians", self.medians.look),
                     ("logs", lambda f, g: gamelogs.look(self.uploader, f, g)),
                     ("combat", lambda f, g: combatlogs.look(self.uploader, f, g)),
                     ("market", lambda f, g: market.look(self.uploader, f, g)),
                     ("sync", lambda f, g: sync.guarded_look(self.uploader, f, g)))
            for name, task in tasks:
                self.jobs.run(game_id, name, lambda: task(folder, game_id), uploads.LOOK_EVERY)
        self.jobs.run("all", "data", lambda: self.data.tick(clients or {}), uploads.LOOK_EVERY)

    def notify(self, text):
        try:
            if system.MAC:
                system.mac_notify("XPCompass", text)
            else:
                self.icon.notify(text, "XPCompass")
        except Exception:   # noqa: BLE001 - a notification is never worth stopping for
            pass

    def update_self(self):
        """Move onto a newer signed build of the app: run its installer, or on a Mac swap the bundle
        and restart into it. True when quitting."""
        if self.waiting_switch:                 # code already in place: switch at the first safe moment
            if not self.safe_to_switch():
                return False
            selfupdate.relaunch(self.waiting_switch)
            self.quit()
            return True
        offered, events = self.updater.check_app()
        for event in events:
            self.notify(event.text)
        if not offered or not system.packaged():
            return False
        setup = offered["package"] == release.APP_SETUP
        if setup and selfupdate.tried_recently(offered["build"]):
            if self.held_back != offered["build"]:
                self.held_back = offered["build"]
                self.notify(f"XPCompass {offered['build']} didn't install; it tries again within the hour. "
                            f"Why is in {selfupdate.pending_dir() / 'setup.log'}.")
            return False
        try:
            source, token = self.updater.app_source()
            def progress(done, total):
                self.app_update = {"state": "downloading", "progress": done / total if total else None}

            self.app_update = {"state": "downloading", "progress": None}
            path = selfupdate.apply(source, offered, token=token, progress=progress)
            self.app_update = {"state": "installing", "progress": 1.0}
            if setup:
                selfupdate.run_setup(path, offered["build"],
                                     migrating=(self.updater.app or {}).get("kind") == selfupdate.MIGRATE)
        except ReleaseError as error:
            self.app_update = None
            self.notify(str(error))
            return False
        if offered["package"] == release.APP_CODE and self.window and self.window.visible:
            # The window is open (a new install signing in, or a player looking): the new code is in place
            # and starts at the first safe moment (safe_to_switch); switching now would pull the window away from them.
            self.app_update = None
            self.waiting_switch = path
            return False
        self.notify(f"Updating XPCompass to {offered['build']}.")
        if not setup:
            selfupdate.relaunch(path)
        self.quit()
        return True

    def safe_to_switch(self):
        """Whether the app may restart into code that is already in place: the window is closed to the tray, or
        it is open but nobody has clicked in it for QUIET seconds and nothing is being signed in or installed.
        An open window nobody is using must not hold an update back for days (a laptop left on the page)."""
        if not (self.window and self.window.visible):
            return True
        if self.signin["busy"] or self.app_update:
            return False
        return time.time() - self.touched >= QUIET

    # What the window shows and does (window.Api calls these).

    def ui_state(self):
        session = self.account.session()
        try:
            game = str(self.updater.game())
        except ReleaseError:
            game = None
        rows = self.updater.catalog.entries()
        names = {row["id"]: row["name"] for row in rows}
        clients = self.updater.client_rows()
        missing = {client["id"] for client in clients if not client["path"]}     # games with no folder to install into
        addons = []
        for row in rows:                    # the server's list for this account, in its order
            info = self.updater.info.get(row["id"], {})
            client = PACKAGES.get(row["id"], {}).get("client")
            installable = row["status"] == "installable"
            addons.append({
                "id": row["id"], "name": row["name"], "about": row["about"],
                "wanted": installable and row["id"] in self.settings.packages,
                "available": installable, "unlock": row.get("unlock"),
                "needs": [names.get(need, need) for need in row["needs"]],
                "state": info.get("state", "pending"), "text": info.get("text", "Checking for updates…"),
                "installed": info.get("installed_version") if info.get("installed_version") != info.get("installed") else None,
                "has_old_build": bool(info.get("installed")) and info.get("installed_version") == info.get("installed"),
                "offered": info.get("offered_version") or info.get("offered"),
                "build": info.get("offered"), "releases": info.get("releases", []), "notes": info.get("notes", []),
                "game": row.get("game", "Other"),
                "pick": client if installable and client in missing else None,     # the card's Choose folder
                "testing": info.get("channel") in ("prerelease", "nightly") or (not info.get("channel") and row.get("testing", False)),
                "progress": info.get("progress"), "upgrade": row.get("upgrade"),
                "edition_name": release.EDITION_NAMES.get(info.get("edition") or row.get("edition") or "")
                                or row.get("edition_name"),
            })
        checked = self.updater.checked_at
        sent = self.uploader.last
        return {
            "uploaded": sent and f"{sent['text']} ({ago(time.time() - sent['at'])})",
            "job_errors": self.jobs.failures() if getattr(self, "jobs", None) else [],
            "logo": self.logo, "signed_in": bool(session), "name": session and session.get("name"),
            "member": bool(self.updater.catalog.member()), "app_update": self.app_update, "crest": self.crest,
            "welcome": not session, "signin": dict(self.signin),
            "addons": addons, "channel": self.settings.channel,
            "can_nightly": bool(session and session.get("prerelease")),
            "checking": self.checking, "checked": checked and ago(time.time() - checked),
            "game": game, "clients": clients, "notices": self.updater.notice_rows(),
            "start_with_windows": starts_with_windows(), "data_channel": self.settings.data_channel,
            "login_label": "Open at login" if system.MAC else "Start with Windows",
            # The readable version (0.2.2190), as the addons show theirs; the build code for older builds.
            "version": (selfupdate.current() or {}).get("version") or (selfupdate.current() or {}).get("build", "dev"),
        }

    def sign_in(self, open_browser=webbrowser.open):
        if self.signin["busy"]:
            return
        self.cancel = threading.Event()
        self.signin = {"busy": True, "url": None, "error": None}

        def opener(url):
            self.signin["url"] = url
            open_browser(url)

        def run():
            try:
                name = self.account.sign_in(opener, cancel=self.cancel)
            except auth.AuthError as error:
                self.signin = {"busy": False, "url": None, "error": None if error.code == "cancelled" else str(error)}
                return
            self.signin = {"busy": False, "url": None, "error": None}
            self.updater.signed_in()
            self.notify(f"Signed in as {name or 'a guild member'}.")
            self.wake.set()
        threading.Thread(target=run, daemon=True).start()

    def reopen_signin(self):
        if self.signin.get("url"):
            webbrowser.open(self.signin["url"])

    def cancel_signin(self):
        self.cancel.set()

    def sign_out(self):
        self.account.sign_out()
        self.updater.bind_account()
        self.wake.set()
        self.upload_wake.set()

    def set_wanted(self, package, on):
        """Tick or untick an addon. Ticking one ticks what it needs; unticking one unticks what needs it.
        Only what the catalog lets this account install can be ticked; anything can be unticked."""
        if package not in PACKAGES or (on and not self.updater.catalog.installable(package)):
            return
        wanted = set(self.settings.packages)
        if on:
            wanted |= {package, *PACKAGES[package]["needs"]}
        else:
            wanted = {name for name in wanted if name != package and package not in PACKAGES[name]["needs"]}
        self.settings.packages = [name for name in PACKAGES if name in wanted]
        self.settings.save()
        self.wake.set()

    def set_game(self, folder, game_id=games.FOREVER.id):
        """The player picks a game's client folder: their choice outranks the one found by product.
        A folder that is another game's client, or one the other game already uses, is refused with
        the reason, which is also returned. Returns None when the folder was taken."""
        title, clients = games.GAMES[game_id].title, self.updater.clients()
        refusal = None
        if not games.is_client(folder):
            refusal = (f"{folder} is not a game client folder. Pick the client folder, such as "
                       f"{FOLDER_HINT[game_id]}.")
        elif games.other_game(folder, game_id):
            refusal = (f"{folder} is a {games.other_game(folder, game_id).title} client, not {title}. "
                       f"Pick the {title} client folder, such as {FOLDER_HINT[game_id]}.")
        else:
            taken = next((other for other_id, other in games.GAMES.items() if other_id != game_id
                          and other_id in clients and games.same_folder(clients[other_id], folder)), None)
            if taken:
                refusal = (f"{folder} is where {taken.title} is installed. Pick {title}'s own client folder, "
                           f"such as {FOLDER_HINT[game_id]}.")
        if refusal:
            self.notify(refusal)
            return refusal
        self.updater.notices.pop(game_id, None)
        self.settings.choose(game_id, folder)
        self.settings.last_clients[game_id] = str(folder)       # their choice, not a move the app made
        self.settings.save()
        self.wake.set()
        return None

    def use_found_client(self, game_id):
        """Drop the player's choice for a game, so the app finds its client by product again (a player
        who chose the beta folder moves to the release client once it is there)."""
        if game_id in games.GAMES:
            self.settings.choose(game_id, None)
            self.settings.save()
            self.wake.set()

    def dismiss_notice(self, game_id):
        self.updater.notices.pop(game_id, None)

    def update_now(self, package):
        """Install the waiting update straight away, even with the game open; the player then
        restarts the game (a /reload would mix old and new files)."""
        if package in PACKAGES:
            self.updater.forced.add(package)
            self.wake.set()

    def use_release(self, package):
        """Replace the testers build in the game with the players' build, the player's own choice, straight away
        like Update now (the app never does it on its own: service.Updater._check)."""
        if package in PACKAGES:
            self.set_channel("stable")
            self.updater.to_release.add(package)
            self.wake.set()

    def set_channel(self, channel):
        """A channel change covers both the companion and every addon; never forces a running-game swap."""
        from .channels import canonical
        wanted = canonical(channel)
        session = self.account.session() or {}
        if wanted == "nightly" and not session.get("prerelease"):
            return
        self.settings.channel, self.settings.channel_migrated = wanted, True
        self.settings.app_switch_to = wanted
        self.settings.save()
        self.updater.waiting.clear()
        self.updater.forced.clear()
        self.wake.set()

    def set_start(self, on):
        set_start_with_windows(on)

    def set_data_channel(self, on):
        """The "Data from the server" setting. Off empties Data.lua at the next look; on asks again."""
        self.settings.data_channel = bool(on)
        self.settings.save()
        self.upload_wake.set()

    def open_addons(self, game_id=games.FOREVER.id):
        try:
            system.open_folder(self.updater.game(game_id) / "Interface" / "AddOns")
        except ReleaseError as error:
            self.notify(str(error))

    def check_now(self):
        """The window's and the tray's Check now: updates, uploads and the other PCs, now. It only woke the update check
        until 6 November, when the owner pressed it on a laptop that had just come online and nothing synced."""
        self.uploader.check_now = True
        self.wake.set()
        self.upload_wake.set()

    def quit(self):
        """Stop and go. On the owner's PC, 5 November, Quit stopped the loops and the tray icon but the hidden window
        never closed: the app stayed running with nothing uploading, and a copy opened from the Start menu found it
        running and went away. So whatever the window does, the app ends within QUIT_GRACE."""
        self.stopping = True
        self.wake.set()
        self.upload_wake.set()
        self.cancel.set()
        end_after(QUIT_GRACE)
        try:
            self.icon.stop()
        except Exception:   # noqa: BLE001 - the app ends anyway
            pass
        if self.window:
            try:
                self.window.destroy()
            except Exception:   # noqa: BLE001 - the app ends anyway
                pass


QUIT_GRACE = 5      # seconds Quit gives the window to close before it ends the app; under the 15 a new copy waits


def end_after(wait, end=None):
    """End this process `wait` seconds from now, unless it has ended by then. It stops the loops where they are, as a
    Quit that works always has: they are daemon threads, which end with the process."""
    def run():
        time.sleep(wait)
        (end or os._exit)(0)

    threading.Thread(target=run, name="quit-guard", daemon=True).start()


def selftest(out):
    """For tools/pack_companion.py: prove the packaged .exe has its modules and files, then exit."""
    import json
    report = {"build": selfupdate.current(), "page": len(window.page()), "logo": icon_image().size[0] > 1,
              "keys": len(TRUSTED_KEYS),
              "frozen": system.packaged()}
    Path(out).write_text(json.dumps(report), encoding="utf-8")


def settle_install():
    """In the installed app: delete the loose .exe it moved from, and keep Start with Windows on
    this copy. Neither is worth stopping the app for."""
    selfupdate.forget_old_copies()
    try:
        system.repoint_start_at_login()
    except OSError:
        pass


def main():
    tls.install()
    if "--selftest" in sys.argv:
        selftest(sys.argv[sys.argv.index("--selftest") + 1])
        return 0
    if not single_instance(wait=15 if "--after-update" in sys.argv else 0):
        return 0
    if system.packaged():
        selfupdate.clean_up()
        if (selfupdate.current() or {}).get("kind") in (selfupdate.INSTALLED, selfupdate.CODE):
            settle_install()
    updated = "--after-update" in sys.argv
    if not updated and (selfupdate.current() or {}).get("build"):
        selfupdate.note_build(selfupdate.current()["build"])      # what an update's notice compares with
    app = Tray(show="--background" not in sys.argv and not updated, updated=updated)
    selfupdate.mark_good()      # under the launcher: this build came up, so it is kept
    start_with_windows_by_default(app.settings, system.packaged())
    app.run()
    return 0
