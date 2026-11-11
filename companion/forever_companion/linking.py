"""Linking a new Only Bans member's account to their guild character without a logout (the owner, 11 November: "i spose
we know the roster so it can probably verify from the wtf files?", then "this only needs to be done for onlybans
verifying though").

Only while the server says the signed-in account is in the Only Bans Discord and waits on verification ("verifying" in
its /me answer), and only for WoW Forever's client, the app sends the server the names of the characters on this PC:

- **The game's character folders.** The game keeps a folder per character, WTF/Account/<account>/<realm>/<First-Surname>.
  One counts only when it holds the files the game itself writes there when a character logs out (GAME_FILES), so an
  empty folder made by hand doesn't; those files are looked at for being there and when they changed, never opened.
  Only the characters' names are sent: no account or realm folder name, no file, no path.
- **The character playing now.** The addon turns combat logging on at login, so the game's newest combat log names the
  logged-in character in its own events (the log's "mine" flag on a player) within a minute or two of play, before any
  logout or /reload writes the folders above. Only that one name is sent.

The server keeps a name only when it is on the guild's roster, and the first account to send it holds it, as with an
uploaded character (server nicknames.py). Once verified, the server stops saying "verifying" and nothing more is sent.
While waiting, the app checks every minute for FAST_FOR after sign-in (or after it starts), then at its usual pace.
"""
import csv
import time
from pathlib import Path

from .combatread import LINE, MINE, PLAYER, flags

GAME_FILES = ("config-cache.wtf", "layout-local.txt")   # written by the game into a character's folder at logout
MOST = 60                   # names sent at most (the server's CLAIM_MOST)
LOG_RECENT = 15 * 60        # a combat log written to within this many seconds is the session being played
TAIL = 1_000_000            # bytes read from the end of that log, at most, for its character
FAST_FOR = 3600             # seconds after sign-in that a waiting account's app checks every minute
FAST_EVERY = 60


def two_part(parts):
    """'First Surname' from a name's parts, or None unless both are letters only (a Forever character's name)."""
    return " ".join(parts) if len(parts) == 2 and all(part.isalpha() for part in parts) else None


def folders(path):
    try:
        return [child for child in Path(path).iterdir() if child.is_dir()]
    except OSError:
        return []


def wtf_characters(client):
    """The names of the characters with a folder of their own in the client's WTF folder that holds the game's own
    files (GAME_FILES), most recently played first."""
    played = {}
    for account in folders(Path(client) / "WTF" / "Account"):
        for realm in folders(account):
            if realm.name == "SavedVariables":
                continue
            for folder in folders(realm):
                name = two_part(folder.name.split("-"))
                if not name:
                    continue
                try:
                    stamps = [(folder / part).stat().st_mtime for part in GAME_FILES if (folder / part).is_file()]
                except OSError:
                    continue
                if len(stamps) == len(GAME_FILES):
                    played[name] = max(played.get(name, 0), *stamps)
    return sorted(played, key=lambda name: -played[name])[:MOST]


def own_name(raw):
    """The logged-in character's name when this combat log line is one of its own events, else None. The log writes a
    player as "First-Surname-Realm-Region"."""
    match = LINE.match(raw)
    if not match:
        return None
    try:
        fields = next(csv.reader([match.group(7)]))[1:]
    except (csv.Error, StopIteration):
        return None
    for guid, name, flag in ((fields[0:3]), (fields[4:7])) if len(fields) >= 7 else ():
        if guid.startswith("Player") and flags(flag) & MINE and flags(flag) & PLAYER:
            parts = name.split("-")
            if len(parts) >= 3:
                return two_part(parts[:2])
    return None


def live_character(client, now=None):
    """The character playing now, by the newest combat log in the client's Logs folder written to in the last
    LOG_RECENT seconds: the last of its own events in the log's final TAIL bytes. None when there is none."""
    now = time.time() if now is None else now
    try:
        logs = [(path.stat().st_mtime, path) for path in (Path(client) / "Logs").glob("WoWCombatLog*.txt")]
    except OSError:
        return None
    if not logs:
        return None
    written, newest = max(logs)
    if now - written > LOG_RECENT:
        return None
    try:
        with open(newest, "rb") as log:
            log.seek(0, 2)
            size = log.tell()
            log.seek(max(0, size - TAIL))
            tail = log.read(TAIL).decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for raw in reversed(tail[1:] if len(tail) > 1 else tail):     # the first line may be cut
        name = own_name(raw)
        if name:
            return name
    return None


class Linker:
    """Sends the characters on this PC while the server says the account waits on verification; each list once,
    again only when it changes."""

    def __init__(self, account, clock=time.time, wtf=wtf_characters, live=live_character):
        self.account, self.clock, self.wtf, self.live = account, clock, wtf, live
        self.sent = {}          # how -> the names last sent that way
        self.since = None       # when this run of the app first saw the account waiting (sign-in, or the app's start)

    def restart(self):
        """A new sign-in: the fast checks start over, and what's on the PC goes again."""
        self.sent, self.since = {}, None

    def fast(self):
        """Whether checks come every minute: the account waits on verification, within FAST_FOR of sign-in."""
        return self.since is not None and self.clock() - self.since < FAST_FOR

    def step(self, access, client):
        """Send what's new on the PC when `access` (auth.Account.access) says the account waits on verification;
        `client` is Forever's client folder or None. Returns {how: names} sent this time."""
        if not access or access.get("verifying") is not True:
            self.sent, self.since = {}, None
            return {}
        if self.since is None:
            self.since = self.clock()
        if client is None:
            return {}
        sent = {}
        live = self.live(client)
        for how, names in (("log", [live] if live else []), ("folder", self.wtf(client))):
            if not names or tuple(names) == self.sent.get(how):
                continue
            answer = self.account.link(how, names)
            if answer is None:
                continue
            self.sent[how] = tuple(names)
            sent[how] = list(names)
            if answer.get("verifying") is False:        # verified meanwhile, or not waiting after all
                break
        return sent
