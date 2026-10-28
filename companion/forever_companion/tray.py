"""The app: runs the updater (service.py) in the background, with a tray icon and a window.

The window (window.py, ui.html) opens at start, or only at the first start when the system starts
the app at login (--background), and from the tray or menu bar icon. Notices (an update installed,
one waiting for the game to close, a problem) also appear as system notifications, each once.
What differs between Windows and macOS lives in system.py.
"""
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from . import auth, games, release, selfupdate, service, system, uploads, window
from . import gamelogs, market
from .release import PACKAGES, TRUSTED_KEYS, ReleaseError

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

    def __init__(self, settings=None, show=True, account=None, updater=None, make_window=True, uploader=None):
        self.settings = settings or service.Settings.load()
        self.account = account or auth.Account(self.settings.server)
        self.updater = updater or service.Updater(self.settings, account=self.account)
        self.uploader = uploader or uploads.Uploader(self.settings, account=self.account)
        self.wake = threading.Event()
        self.upload_wake = threading.Event()
        self.stopping, self.checking = False, False
        self.signin = {"busy": False, "url": None, "error": None}
        self.cancel = threading.Event()
        self.app_update = None          # {"state", "progress"} while the app replaces itself
        self.held_back = None           # the app build whose failed install was last reported
        self.logo = window.data_uri(icon_image())
        self.crest = window.data_uri(Image.open(system.bundled("onlybans-crest.png", Path(__file__).parent / "onlybans-crest.png")))
        self.icon = pystray.Icon("ForeverCoachCompanion", icon_image(), "XPCompass", menu=pystray.Menu(
            pystray.MenuItem("Open XPCompass", lambda: self.window.show(), default=True),
            pystray.MenuItem("Check now", lambda: self.wake.set()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        ))
        welcome = not self.account.session()        # sign-in comes first: no anonymous mode
        self.window = window.Window(self, show or welcome) if make_window else None

    def run(self):
        threading.Thread(target=self.loop, daemon=True).start()
        threading.Thread(target=self.upload_loop, daemon=True).start()
        self.icon.run_detached()
        window.Window.run()

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
            self.wake.wait(self.updater.next_delay())
            self.wake.clear()

    def upload_loop(self):
        """Every minute: what each game's client recorded since the last look goes to the server (uploads.py)."""
        while not self.stopping:
            for game_id, folder in self.updater.clients().items():      # no client yet: nothing to look at
                try:
                    self.uploader.look(folder, game_id)
                    gamelogs.look(self.uploader, folder, game_id)       # the game's load errors and crashes
                    market.look(self.uploader, folder, game_id)         # XPC - Market's auction house scans
                except Exception as error:   # noqa: BLE001 - an upload problem never stops the app
                    self.uploader.last = {"at": time.time(), "text": f"Upload failed, will try again: {error}"}
            self.upload_wake.wait(uploads.LOOK_EVERY)
            self.upload_wake.clear()

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
        self.notify(f"Updating XPCompass to {offered['build']}.")
        if not setup:
            selfupdate.relaunch(path)
        self.quit()
        return True

    # What the window shows and does (window.Api calls these).

    def ui_state(self):
        session = self.account.session()
        try:
            game = str(self.updater.game())
        except ReleaseError:
            game = None
        rows = self.updater.catalog.entries()
        names = {row["id"]: row["name"] for row in rows}
        addons = []
        for row in rows:                    # the server's list for this account, in its order
            info = self.updater.info.get(row["id"], {})
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
                "testing": info.get("channel") == "prerelease" or (not info.get("channel") and row.get("testing", False)),
                "progress": info.get("progress"), "upgrade": row.get("upgrade"),
                "edition_name": release.EDITION_NAMES.get(info.get("edition") or row.get("edition") or "")
                                or row.get("edition_name"),
            })
        checked = self.updater.checked_at
        sent = self.uploader.last
        return {
            "uploaded": sent and f"{sent['text']} ({ago(time.time() - sent['at'])})",
            "logo": self.logo, "signed_in": bool(session), "name": session and session.get("name"),
            "member": bool(self.updater.catalog.member()), "app_update": self.app_update, "crest": self.crest,
            "welcome": not session, "signin": dict(self.signin),
            "addons": addons,
            "checking": self.checking, "checked": checked and ago(time.time() - checked),
            "game": game, "clients": self.updater.client_rows(), "notices": self.updater.notice_rows(),
            "start_with_windows": starts_with_windows(),
            "login_label": "Open at login" if system.MAC else "Start with Windows",
            "version": (selfupdate.current() or {}).get("build", "dev"),
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
        if not (Path(folder) / "Interface" / "AddOns").is_dir():
            refusal = (f"{folder} has no Interface\\AddOns folder. Pick the client folder, such as "
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

    def set_start(self, on):
        set_start_with_windows(on)

    def open_addons(self, game_id=games.FOREVER.id):
        try:
            system.open_folder(self.updater.game(game_id) / "Interface" / "AddOns")
        except ReleaseError as error:
            self.notify(str(error))

    def quit(self):
        self.stopping = True
        self.wake.set()
        self.upload_wake.set()
        self.cancel.set()
        self.icon.stop()
        if self.window:
            self.window.destroy()


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
    if "--selftest" in sys.argv:
        selftest(sys.argv[sys.argv.index("--selftest") + 1])
        return 0
    if not single_instance(wait=15 if "--after-update" in sys.argv else 0):
        return 0
    if system.packaged():
        selfupdate.clean_up()
        if (selfupdate.current() or {}).get("kind") == selfupdate.INSTALLED:
            settle_install()
    app = Tray(show="--background" not in sys.argv)
    start_with_windows_by_default(app.settings, system.packaged())
    app.run()
    return 0
