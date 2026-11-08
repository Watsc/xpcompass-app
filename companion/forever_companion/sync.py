"""One player's PCs in step (server/README.md, "POST /sync"). Each PC keeps and owns its own data. For a
signed-in player with sharing on, the app sends the server a copy of this PC's characters, ledger and
settings, and brings the copies from the same Discord account's other installs into the game's saved
files while the game is closed.

- **Every PC sends its copy.** Each signed-in install sends its copy, under a hash of its WoW account
  folder's name, one per game, with or without another PC (since 8 November: the owner, "if we're going to
  store their game info anyway we might as well back it up for them"). It is the player's backup: a new or
  reinstalled PC gets it back as another PC's, by the first-sync rule (fill gaps, overwrite nothing).
- **A PC that lost its saved files.** What this PC sent last is kept beside the app (sync-backup). A
  character it held then that its saved files no longer have, or whose ledger began again (the game
  reinstalled, its WTF folder gone), is frozen there as this PC's backup: shown in the game as another PC's would be, and sent with this PC's
  copy (its ledger added to what's been kept since), so it stays on the server. A record moved from a first
  name to the full name isn't lost. A character's play from before stays in the backup and the game; only a
  character not played since goes to the server with it.
- **Whose data.** A copy holds this PC's own records only, never what came from another PC, so nothing
  comes back round and is counted twice. Each install's new copy replaces its last.
- **Where it lands.** In tables of their own: XPCompassSync.peers (XPCompass.lua) for characters and
  settings, ForeverCoachMarketPeers (XPCompass_Market.lua) for the ledger. Nothing here writes a PC's
  own records. The addons add the two up when showing (Alts.lua, Ledger.lua) and take settings one at a
  time at login, newest stamp first (Sync.lua).
- **The ledger.** Rows with times (sales, buys, back, and the auction records) are merged by time,
  identical rows once. Day sums add per install. Of closing gold, the later stamp wins. Counters
  (soldCount) don't travel.
- **A character's play.** Each character's own saved files (ForeverCoachNextDB, XPCompassJourneyDB, under
  <account>/<realm>/<character>/SavedVariables) go too, as `play`, keyed by those two folder names, which
  are the same on every PC of a WoW account. The other PCs' land in that character's XPCompass.lua as
  XPCompassPlayPeers, beside its own (Play.lua). What happened adds up: time per level, the Journey's
  days, deaths, records, places and flights. Where you are (guide, step, hearth, quest log) follows the PC
  the character was played on last, by the stamp the addon writes at logout. Choices follow the latest
  change, stamped as settings are. Upload records, feedback and panel positions never go.
- **Old names.** Builds before 5 November kept a character under its first name ("Realm/Watts"). Such a
  record or book is moved under the full name when exactly one character fits it (realm, first name,
  class, and no lower level); when more than one does, it is left as it is and listed as unresolved.
- **The switch.** Settings' "Sync between your devices", XPCompassSync.off in this PC's saved file, is this
  PC's alone. Off, the app stops sending and fetching, takes this install's copy off the server, and clears
  the other PCs' tables and its own copies of them while the game is closed. On again is a first sync. A
  switch it can't read counts as off for that look, and nothing is sent, fetched or written.
- **Safety.** Written like uploads._clear: to a temporary file, read back, swapped in, only while the
  game is closed. If a saved file, a download or the server can't be trusted, nothing is written or
  sent, and the PC carries on with its own data.
"""
import gzip
import hashlib
import math
import traceback
import threading
import sys
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import auth, uploads

OWN_FILE, MARKET_FILE, MAIL_FILE = uploads.ADDON_FILE, "XPCompass_Market.lua", "XPCompass_Mail.lua"
CHARACTERS, SETUP, STAMPS_TABLE = "XPCompassCharacters", "XPCompassSetup", "XPCompassSync"
MARKET_DB, MAIL_DB, PEERS_TABLE = "ForeverCoachMarketDB", "ForeverCoachMailDB", "ForeverCoachMarketPeers"
JOURNEY_FILE, CHARACTER_DB, JOURNEY_DB = "XPCompass_Journey.lua", uploads.CHARACTER_DB, "XPCompassJourneyDB"
PLAY_PEERS = "XPCompassPlayPeers"       # in each character's XPCompass.lua: the other PCs' play of it
GOLD = "#gold"
KINDS = ("sales", "buys", "back")

# A character's play, field by field (addon/XPCompass/Play.lua holds the same lists; the tests hold them equal).
# Where you are: from the PC the character was played on last.
PLAY_STATE = ("guide", "lastRoute", "side", "holding", "deferred", "owed", "bind", "hearthCooldown", "trainedAt", "questLog",
              "classPutOff", "runPlan", "released", "chain", "runFirst", "ownWay", "mountSpeed")
JOURNEY_STATE = ("notices",)
# Kept by every PC that saw it: shown together (skipped and visited by guide).
PLAY_SETS = ("skipped", "visited", "skippedQuests", "gearDeclined", "attuneSkipped", "dungeons", "flights", "demonSpells", "flightsSeen")
# Choices: the latest change, stamped at logout (ForeverCoachNextDB.syncStamps) as account settings are in XPCompassSync.stamps.
PLAY_CHOICES = ("auto", "fastRoute", "sellJunk", "sellFood", "makeRoom", "groupQuests", "autoEquip", "upgradeOff", "autoReward",
                "rewardOff", "pinned", "autoFly", "dropChances", "skipFirstAid", "arrow", "othersInViewer", "dungeonRoute", "talents",
                "talentBuild", "stepSound", "mapFog", "compassCompact", "compassLinks", "compassLoopAhead", "compassFinish",
                "compassWayIn", "compassDanger", "campFeature")
PLAY_FIELDS = ("at", "level", "first", "times", "now", "sets", "state", "choice", "j")
JOURNEY_PARTS = ("days", "past", "deaths", "zones", "records")
JOURNEY_VERSION = 2          # Tally.lua's VERSION: another layout's tallies don't add up with these

POLL_EVERY = 15 * 60         # seconds between asking the server what the other PCs have
SEND_GAP = 10 * 60           # the least between sending this PC's copy
RETRY = 30 * 60              # after the server didn't answer
DAY = 86400
EARLIEST = 1_700_000_000     # the server's: older than any build that records

# What a copy may hold, the server's own limits (server/forever_server/sync.py; tests hold them equal).
MOST_RECORDS = 300           # rows of each kind, a character: Ledger.lua's MOST_RECORDS
MOST_CHARS = 60
MOST_DAYS = 800
MOST_SETTINGS = 6000
MOST_HELD = 500              # kinds of item in a character's bags or bank
MOST_UNPACKED = 3_000_000    # bytes of JSON
MOST_PACKED = 900_000        # bytes gzipped: the server takes no request over 1 MB
MOST_PLAY_DAYS = 40          # a character's Journey days: Tally.lua keeps 35, the rest in `past`
MOST_DEATHS = 100            # Tally.lua keeps 40
MOST_LEVELS = 100
MOST_VALUES = 40_000         # numbers, words and tables in one character's play
# When a copy won't fit: the most ledger days and rows of each kind, then Journey days and deaths, each character
# sends; a Journey day left out is added into the `past` sent, so totals stay whole. Only what's sent: this PC's own
# stay, and where a character is never shrinks. Almost nobody gets past the first.
FITS = ((MOST_DAYS, MOST_RECORDS, MOST_PLAY_DAYS, MOST_DEATHS), (MOST_DAYS, MOST_RECORDS, 14, 40), (400, MOST_RECORDS, 7, 20),
        (400, 150, 7, 20), (200, 100, 3, 10), (100, 50, 1, 5), (60, 20, 0, 0))

COPPER = 10 ** 13
BOOK_KEY = re.compile(r"^[^\x00-\x1f/]{0,60}/[^\x00-\x1f/]{1,40}$")
TEXT = re.compile(r"^[^\x00-\x1f]{0,100}$")
KIND = re.compile(r"^[^\x00-\x1f=,]{1,40}$")
SETTING = re.compile(r"^(?:m|mail|g|r|o|s)\.[^\x00-\x1f]{1,120}$")
ITEM = re.compile(r"^\d{1,9}$")
SCOPE = re.compile(r"^[0-9a-f]{16}$")
DAY_KEY = re.compile(r"^\d{1,7}$")
RECORD_FIELDS = ("name", "realm", "level", "money", "class", "rides", "profs", "skills", "bags", "bank", "at")
# A character's folders, <realm>/<character>, as WoW names them: never a path of any other kind.
PLAY_KEY = re.compile(r'^[^\x00-\x1f/\\:*?"<>|. ][^\x00-\x1f/\\:*?"<>|]{0,59}/[^\x00-\x1f/\\:*?"<>|. ][^\x00-\x1f/\\:*?"<>|]{0,59}$')
LONG_TEXT = re.compile(r"^[^\x00-\x1f]{0,200}$")
NAME_TEXT = re.compile(r"^[^\x00-\x1f]{1,80}$")
LEVEL_KEY = re.compile(r"^\d{1,3}$")


GUARD_WAIT = 120         # seconds the minute loop waits for a look
GUARD_STUCK = 300        # seconds a look runs before where it is gets written down


def look(uploader, game, game_id="forever"):
    """For the app's minute loop (tray.py): one Sync beside the uploader, made at its first look."""
    if not isinstance(getattr(uploader, "pc_sync", None), Sync):
        uploader.pc_sync = Sync(uploader)
    return uploader.pc_sync.look(game, game_id)


def guarded_look(uploader, game, game_id="forever", wait=GUARD_WAIT, stuck_after=GUARD_STUCK, look_with=None, clock=time.time):
    """look, which can't hold up the minute loop. It runs on a thread of its own and the loop waits for it, so one at a
    time as ever; a look that hasn't ended by `wait` is left to it, and later looks are skipped until it ends. On the owner's
    PC, 5 November, a first sync never finished and every upload after it stopped, with no way of telling where: one
    stuck for `stuck_after` writes every thread's stack beside the uploader's file, once."""
    guard = getattr(uploader, "pc_sync_guard", None)
    if guard is None:
        guard = uploader.pc_sync_guard = {"worker": None, "since": 0, "told": False}
    worker = guard["worker"]
    if worker is not None and worker.is_alive():
        if not guard["told"] and clock() - guard["since"] >= stuck_after:
            guard["told"] = True
            _write_stacks(uploader)
            uploader.last = {"at": clock(), "text": "Syncing your computers has stuck; your uploads carry on. Restart the app to try it again."}
        return 0

    def run():
        try:
            (look_with or look)(uploader, game, game_id)
        except Exception as error:   # noqa: BLE001 - as the minute loop's own: a sync problem never stops the app
            uploader.last = {"at": clock(), "text": f"Computers not synced, will try again: {error}"}

    guard.update(worker=threading.Thread(target=run, name="pc-sync", daemon=True), since=clock(), told=False)
    guard["worker"].start()
    guard["worker"].join(wait)
    return 0


def _write_stacks(uploader):
    """Where every thread is, for whoever looks into it: code positions only, nothing of the player's."""
    try:
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        lines = [time.strftime("%Y-%m-%d %H:%M:%S")]
        for ident, frame in sys._current_frames().items():
            lines.append(f"\n{names.get(ident, ident)}:")
            lines.extend(line.rstrip() for line in traceback.format_stack(frame))
        uploader.path.with_name("sync-stuck.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def whole(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def scope_of(account_folder):
    """Which WoW account a copy is of, without saying: a hash of its folder's name, the same on each PC."""
    return hashlib.sha256(("xpc-sync:" + account_folder.upper()).encode("utf-8")).hexdigest()[:16]


def trusted(stamp, now):
    """A stamp, or None: one far ahead of the clock counts as none, as the server treats a row's time."""
    return stamp if whole(stamp, EARLIEST, int(now) + DAY) else None


# ---------------------------------------------------------------------------------------------
# What a good copy looks like: this check is the server's too (server/forever_server/sync.py)
# ---------------------------------------------------------------------------------------------

def _amounts(side):
    return isinstance(side, dict) and len(side) <= 40 and all(
        isinstance(kind, str) and KIND.match(kind) and whole(copper, 0, COPPER) for kind, copper in side.items())


def _value(value, depth=0):
    if isinstance(value, bool) or (isinstance(value, (int, float)) and abs(value) < COPPER):
        return True
    if isinstance(value, str):
        return len(value) <= 100 and bool(TEXT.match(value))
    if isinstance(value, dict) and depth < 3 and len(value) <= 40:
        return all(isinstance(key, str) and len(key) <= 60 and _value(item, depth + 1) for key, item in value.items())
    return False


def _row(row):
    if not isinstance(row, dict) or set(row) - {"t", "name", "item", "n", "each", "why"}:
        return False
    return (whole(row.get("t"), 0, 2 ** 33) and isinstance(row.get("name"), str) and bool(TEXT.match(row["name"]))
            and whole(row.get("n"), 0, 100_000)
            and (row.get("item") is None or whole(row["item"], 0, 10 ** 9))
            and (row.get("each") is None or whole(row["each"], 0, COPPER))
            and (row.get("why") is None or (isinstance(row["why"], str) and bool(TEXT.match(row["why"])))))


# An auction record (addon/XPCompass_Market/Ledger.lua): its post and how it ended. The app's own list (tests hold them equal).
AUCTION_FIELDS = ("t", "name", "item", "n", "each", "dep", "dur", "p", "o", "ot", "cut", "net", "why", "under", "lost", "x")
AUCTION_OUTCOMES = ("up", "sold", "expired", "cancelled", "failed")


def _auction(row):
    if not isinstance(row, dict) or set(row) - set(AUCTION_FIELDS):
        return False
    if not (whole(row.get("t"), 0, 2 ** 33) and isinstance(row.get("name"), str) and TEXT.match(row["name"]) and row.get("o") in AUCTION_OUTCOMES):
        return False
    if row.get("why") is not None and not (isinstance(row["why"], str) and TEXT.match(row["why"])):
        return False
    for field in ("item", "n", "each", "dep", "dur", "ot", "cut", "net", "under", "lost", "x"):
        if row.get(field) is not None and not whole(row[field], 0, COPPER):
            return False
    return row.get("p") in (None, 1)


def _book(book):
    if not isinstance(book, dict) or set(book) - {"days", "auctions", *KINDS}:
        return "bad book"
    days = book.get("days", {})
    if not isinstance(days, dict) or len(days) > MOST_DAYS:
        return "bad days"
    for day, row in days.items():
        if not (isinstance(day, str) and DAY_KEY.match(day) and isinstance(row, dict)) or set(row) - {"e", "s", "g", "gt"}:
            return "bad day"
        if not (_amounts(row.get("e", {})) and _amounts(row.get("s", {}))):
            return "bad day"
        if (row.get("g") is not None and not whole(row["g"], 0, COPPER)) or (row.get("gt") is not None and not whole(row["gt"], 0, 2 ** 33)):
            return "bad day"
    for kind in KINDS:
        rows = book.get(kind, [])
        if not isinstance(rows, list) or len(rows) > MOST_RECORDS or not all(_row(row) for row in rows):
            return f"bad {kind}"
    rows = book.get("auctions", [])
    if not isinstance(rows, list) or len(rows) > MOST_RECORDS or not all(_auction(row) for row in rows):
        return "bad auctions"
    return None


def _record(record):
    if not isinstance(record, dict) or set(record) - set(RECORD_FIELDS):
        return False
    for field in ("name", "realm", "class"):
        if record.get(field) is not None and not (isinstance(record[field], str) and len(record[field]) <= 60 and TEXT.match(record[field])):
            return False
    for field in ("level", "money", "rides", "at"):
        if record.get(field) is not None and not whole(record[field], 0, COPPER):
            return False
    for field in ("profs", "skills"):
        held = record.get(field, {})
        if not isinstance(held, dict) or len(held) > 40 or not all(isinstance(k, str) and len(k) <= 40 and (v is True or whole(v, 0, 100_000)) for k, v in held.items()):
            return False
    for field in ("bags", "bank"):
        held = record.get(field, {})
        if not isinstance(held, dict) or len(held) > MOST_HELD or not all(isinstance(k, str) and ITEM.match(k) and whole(v, 0, 10 ** 7) for k, v in held.items()):
            return False
    return True


def _number(value, low, high):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high


def _data(value, depth, left):
    """Plain saved data: numbers, true and false, short text, and tables of them at most `depth` deep. `left` is
    [how many values may still come], shared by one character's play."""
    left[0] -= 1
    if left[0] < 0:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return _number(value, -COPPER, COPPER)
    if isinstance(value, str):
        return bool(LONG_TEXT.match(value))
    if depth <= 0:
        return False
    if isinstance(value, list):
        return len(value) <= 2000 and all(_data(item, depth - 1, left) for item in value)
    if isinstance(value, dict):
        return len(value) <= 2000 and all(isinstance(key, str) and NAME_TEXT.match(key) and _data(item, depth - 1, left)
                                          for key, item in value.items())
    return False


def _play(entry):
    """Why one character's play can't be one the app made, or None."""
    if not isinstance(entry, dict) or set(entry) - set(PLAY_FIELDS):
        return "bad play"
    left = [MOST_VALUES]
    if entry.get("at") is not None and not whole(entry["at"], 0, 2 ** 33):
        return "bad play"
    for field in ("level", "first"):
        if entry.get(field) is not None and not whole(entry[field], 0, 1000):
            return "bad play"
    if entry.get("now") is not None and not _number(entry["now"], 0, 10 ** 9):
        return "bad play"
    times = entry.get("times", {})
    if not isinstance(times, dict) or len(times) > MOST_LEVELS or not all(
            isinstance(level, str) and LEVEL_KEY.match(level) and _number(seconds, 0, 10 ** 9) for level, seconds in times.items()):
        return "bad play"
    for part, allowed in (("sets", PLAY_SETS), ("state", PLAY_STATE + JOURNEY_STATE)):
        table = entry.get(part, {})
        if not isinstance(table, dict) or set(table) - set(allowed) or not all(_data(value, 4, left) for value in table.values()):
            return "bad play"
    choices = entry.get("choice", {})
    if not isinstance(choices, dict) or set(choices) - set(PLAY_CHOICES):
        return "bad play"
    for choice in choices.values():
        if not isinstance(choice, dict) or set(choice) - {"v", "at"}:
            return "bad play"
        if ("v" in choice and not _data(choice["v"], 2, left)) or (choice.get("at") is not None and not whole(choice["at"], 0, 2 ** 33)):
            return "bad play"
    journey = entry.get("j", {})
    if not isinstance(journey, dict) or set(journey) - set(JOURNEY_PARTS):
        return "bad play"
    days, deaths = journey.get("days", {}), journey.get("deaths", [])
    if not isinstance(days, dict) or len(days) > MOST_PLAY_DAYS or not all(
            isinstance(day, str) and DAY_KEY.match(day) and isinstance(d, dict) and _data(d, 4, left) for day, d in days.items()):
        return "bad play"
    if not isinstance(deaths, list) or len(deaths) > MOST_DEATHS or not all(isinstance(death, dict) and _data(death, 1, left) for death in deaths):
        return "bad play"
    for part in ("past", "zones", "records"):
        if part in journey and not (isinstance(journey[part], dict) and _data(journey[part], 4, left)):
            return "bad play"
    return None


def problem(copy):
    """Why a copy can't be one the app made, or None."""
    if not isinstance(copy, dict) or set(copy) - {"made", "chars", "books", "settings", "play"}:
        return "not a copy"
    play = copy.get("play", {})
    if not isinstance(play, dict) or len(play) > MOST_CHARS:
        return "bad play"
    for key, entry in play.items():
        if not (isinstance(key, str) and PLAY_KEY.match(key)):
            return "bad play"
        why = _play(entry)
        if why:
            return why
    if not whole(copy.get("made"), EARLIEST, 2 ** 33):
        return "bad time"
    chars, books, settings = copy.get("chars", {}), copy.get("books", {}), copy.get("settings", {})
    if not (isinstance(chars, dict) and isinstance(books, dict) and isinstance(settings, dict)):
        return "not a copy"
    if len(chars) > MOST_CHARS or len(books) > MOST_CHARS:
        return "too many characters"
    if len(settings) > MOST_SETTINGS:
        return "too many settings"
    for key, record in chars.items():
        if not (isinstance(key, str) and BOOK_KEY.match(key) and _record(record)):
            return "bad character"
    for key, book in books.items():
        if not (isinstance(key, str) and BOOK_KEY.match(key)):
            return "bad character"
        why = _book(book)
        if why:
            return why
    for key, entry in settings.items():
        if not (isinstance(key, str) and SETTING.match(key) and isinstance(entry, dict)) or set(entry) - {"v", "at"}:
            return "bad setting"
        if "v" in entry and not _value(entry["v"]):
            return "bad setting"
        if entry.get("at") is not None and not whole(entry["at"], 0, 2 ** 33):
            return "bad setting"
    return None


# ---------------------------------------------------------------------------------------------
# This PC's copy, from its saved files
# ---------------------------------------------------------------------------------------------

def _digits(value):
    """A saved number as a whole one, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _clean_record(record):
    out = {}
    for field in RECORD_FIELDS:
        value = record.get(field)
        if field in ("name", "realm", "class"):
            if isinstance(value, str):
                out[field] = value
        elif field in ("level", "money", "rides", "at"):
            if _digits(value) is not None and _digits(value) >= 0:
                out[field] = _digits(value)
        elif field in ("bags", "bank"):
            if isinstance(value, dict):
                out[field] = {str(item): int(count) for item, count in value.items()
                              if isinstance(item, int) and _digits(count) is not None and count >= 0}
        elif isinstance(value, dict):
            out[field] = {str(key): item if item is True else int(item) for key, item in value.items()
                          if item is True or _digits(item) is not None}
    return out


def _clean_row(row):
    out = {"t": _digits(row.get("t")), "name": row.get("name"), "n": _digits(row.get("n")) if row.get("n") is not None else 1}
    if _digits(row.get("item")) is not None:
        out["item"] = _digits(row["item"])
    if _digits(row.get("each")) is not None:
        out["each"] = _digits(row["each"])
    if isinstance(row.get("why"), str):
        out["why"] = row["why"]
    return out if _row(out) else None


def _clean_auction(row):
    out = {}
    for field in AUCTION_FIELDS:
        value = row.get(field)
        if field in ("name", "o", "why"):
            if isinstance(value, str):
                out[field] = value
        elif field == "p":
            if value is True:
                out["p"] = 1
        elif _digits(value) is not None and _digits(value) >= 0:
            out[field] = _digits(value)
    return out if _auction(out) else None


def _clean_day(row):
    out = {"e": {}, "s": {}}
    for side in ("e", "s"):
        amounts = row.get(side)
        for kind, copper in (amounts.items() if isinstance(amounts, dict) else ()):
            if isinstance(kind, str) and KIND.match(kind) and _digits(copper) is not None and copper >= 0:
                out[side][kind] = _digits(copper)
    if _digits(row.get("g")) is not None and row["g"] >= 0:
        out["g"] = _digits(row["g"])
    if _digits(row.get("gt")) is not None and row["gt"] >= 0:
        out["gt"] = _digits(row["gt"])
    return out


def _clean_book(book, days_most=MOST_DAYS, rows_most=MOST_RECORDS):
    """A book from the saved file, within limits: its newest days and rows. Row `who` is the page's, not the record's."""
    days = book.get("days") if isinstance(book.get("days"), dict) else {}
    newest = sorted((day for day in days if isinstance(day, int) and isinstance(days[day], dict)), reverse=True)[:days_most]
    out = {"days": {str(day): _clean_day(days[day]) for day in newest}}
    for kind in KINDS:
        rows = book.get(kind) if isinstance(book.get(kind), list) else []
        good = [clean for clean in (_clean_row(row) for row in rows if isinstance(row, dict)) if clean]
        out[kind] = good[-rows_most:] if rows_most else []
    rows = book.get("auctions") if isinstance(book.get("auctions"), list) else []
    good = [clean for clean in (_clean_auction(row) for row in rows if isinstance(row, dict)) if clean]
    if good:
        out["auctions"] = good[-rows_most:] if rows_most else []
    return out


def settings_of(saved, now):
    """{setting: {"v": value, "at": stamp}} of this PC, from the saved files that could be read: no stamp,
    no "at"; a stamp and no value, cleared. A file absent says nothing, not that its settings were cleared."""
    out = {}
    own, market, mail = saved.get(OWN_FILE), saved.get(MARKET_FILE), saved.get(MAIL_FILE)
    if own is not None:
        chars = own.get(CHARACTERS) if isinstance(own.get(CHARACTERS), dict) else {}
        for key, record in chars.items():
            if not isinstance(record, dict) or not isinstance(key, str):
                continue
            if key == GOLD:
                for field in ("how", "cap"):
                    if record.get(field) is not None:
                        out[f"g.{field}"] = {"v": record[field]}
                continue
            if isinstance(record.get("roles"), dict):
                out[f"r.{key}"] = {"v": record["roles"]}
            if isinstance(record.get("off"), dict):
                out[f"o.{key}"] = {"v": record["off"]}
        setup = own.get(SETUP) if isinstance(own.get(SETUP), dict) else {}
        for key, answered in setup.items():
            if isinstance(key, str) and isinstance(answered, dict) and isinstance(answered.get("answers"), dict):
                out[f"s.{key}"] = {"v": {"answers": answered["answers"], "at": answered.get("at", 0)}}
    if market is not None:
        db = market.get(MARKET_DB) if isinstance(market.get(MARKET_DB), dict) else {}
        for prefix, table in (("m.rule.", db.get("rules")), ("m.list.", db.get("list"))):
            for key, value in (table.items() if isinstance(table, dict) else ()):
                out[f"{prefix}{key}"] = {"v": value}
        for name, field in (("m.sellAt", "sellAt"), ("m.length", "length"), ("m.tips", "priceTips"), ("m.scan", "autoScan")):
            if db.get(field) is not None:
                out[name] = {"v": db[field]}
    if mail is not None:
        db = mail.get(MAIL_DB) if isinstance(mail.get(MAIL_DB), dict) else {}
        for name, field in (("mail.bankAlt", "bankAlt"), ("mail.paused", "paused")):
            if db.get(field) is not None:
                out[name] = {"v": db[field]}
    readable = []
    if own is not None:
        readable += ["r.", "o.", "s.", "g."]
    if market is not None:
        readable.append("m.")
    if mail is not None:
        readable.append("mail.")
    table = own.get(STAMPS_TABLE) if own is not None and isinstance(own.get(STAMPS_TABLE), dict) else {}
    stamps = table.get("stamps") if isinstance(table.get("stamps"), dict) else {}
    for key, stamp in stamps.items():
        good = trusted(_digits(stamp), now)
        if good and isinstance(key, str) and key.startswith(tuple(readable)) and SETTING.match(key):
            out.setdefault(key, {})["at"] = good
    fine = {key: entry for key, entry in out.items() if SETTING.match(key) and ("v" not in entry or _value(entry["v"]))}
    if len(fine) > MOST_SETTINGS:           # the stamped ones first: they're what another PC can take from us
        kept = sorted(fine, key=lambda key: ("at" not in fine[key], key))[:MOST_SETTINGS]
        fine = {key: fine[key] for key in kept}
    return fine


# ---------------------------------------------------------------------------------------------
# A character's play, from its own saved files
# ---------------------------------------------------------------------------------------------

def _as_map(value):
    """A table read as a list (its keys 1 to n) or as a map, as a map."""
    if isinstance(value, list):
        return dict(enumerate(value, 1))
    return value if isinstance(value, dict) else {}


def _plain(value):
    """Saved data as a copy holds it: keys as text, fractions to three places, text cut to what a copy takes, and
    nothing a copy can't hold (a key that isn't a name or a number, a value of nothing, a number out of range)."""
    if isinstance(value, list):
        if any(item is None for item in value):
            value = {index: item for index, item in enumerate(value, 1) if item is not None}
        else:
            return [_plain(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, bool) or not isinstance(key, (str, int)) or item is None:
                continue
            if not NAME_TEXT.match(str(key)):
                continue
            plain = _plain(item)
            if plain is not None:
                out[str(key)] = plain
        return out
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if not _number(value, -COPPER, COPPER):
            return None
        return int(value) if float(value).is_integer() else round(value, 3)
    if isinstance(value, str):
        return re.sub(r"[\x00-\x1f]", " ", value)[:200]
    return None


def _plain_map(value):
    return {str(key): item for key, item in _as_map(value).items()}


def add_into(into, day):
    """One Journey day's tallies added into another, as Tally.lua's add: counts add, tables add inside them, and a level
    ended keeps its time."""
    for key, value in _plain_map(day).items():
        if key == "ended":
            into["ended"] = {**_plain_map(into.get("ended")), **_plain_map(value)}
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)):
            have = into.get(key)
            into[key] = (have if _number(have, -COPPER, COPPER) else 0) + value
        elif isinstance(value, (dict, list)):
            inner = _plain_map(into.get(key))
            add_into(inner, value)
            into[key] = inner
    return into


def level_of(db):
    """The level a character's file was left at: the addon's own note (syncLevel, from 6 November); before that,
    one past the last level it timed, else the first."""
    if whole(db.get("syncLevel"), 1, 1000):
        return db["syncLevel"]
    levels = [int(key) for key in _as_map(db.get("levelTimes")) if LEVEL_KEY.match(str(key))]
    if levels:
        return max(levels) + 1
    return db["firstTimed"] if whole(db.get("firstTimed"), 1, 1000) else None


def played_at(db, now, record_at=None):
    """When the character was last played on this PC: the stamp the addon writes at logout (syncAt). Before that, the
    newest the addon wrote of it: its feedback's, or its record's in the account's file. None when nothing says."""
    stamp = trusted(_digits(db.get("syncAt")), now)
    if stamp:
        return stamp
    seen = [record_at] if trusted(record_at, now) else []
    feedback = db.get("feedback") if isinstance(db.get("feedback"), dict) else {}
    for item in _as_map(feedback.get("items")).values():
        stamp = trusted(_digits(item.get("at")), now) if isinstance(item, dict) else None
        if stamp:
            seen.append(stamp)
    return max(seen) if seen else None


def play_of(db, journey, now, record_at=None, days_most=MOST_PLAY_DAYS, deaths_most=MOST_DEATHS):
    """One character's play on this PC, from its ForeverCoachNextDB and XPCompassJourneyDB, or None. Its own only: the
    other PCs' (XPCompassPlayPeers) never go back."""
    if not isinstance(db, dict) or not db:
        return None
    entry = {}
    at = played_at(db, now, record_at)
    if at:
        entry["at"] = at
    level = level_of(db)
    if level:
        entry["level"] = level
    if whole(db.get("firstTimed"), 1, 1000):
        entry["first"] = db["firstTimed"]
    times = {str(key): int(round(seconds)) for key, seconds in _as_map(db.get("levelTimes")).items()
             if LEVEL_KEY.match(str(key)) and _number(seconds, 0, 10 ** 9)}
    entry["times"] = {key: times[key] for key in sorted(times, key=int)[:MOST_LEVELS]}
    if _number(db.get("levelSeconds"), 0, 10 ** 9):
        entry["now"] = int(db["levelSeconds"])
    left = [MOST_VALUES]

    def fits(value, depth=4):
        return value is not None and _data(value, depth, left)

    sets = {name: _plain(db[name]) for name in PLAY_SETS if db.get(name) not in (None, False, {}, [])}
    state = {name: _plain(db[name]) for name in PLAY_STATE if db.get(name) is not None}
    stamps = db.get("syncStamps") if isinstance(db.get("syncStamps"), dict) else {}
    choices = {}
    for name in PLAY_CHOICES:
        choice = {}
        if db.get(name) is not None and fits(_plain(db[name]), 2):
            choice["v"] = _plain(db[name])
        stamp = trusted(_digits(stamps.get(name)), now)
        if stamp:
            choice["at"] = stamp          # a stamp and no value: turned off, or cleared, there
        if choice:
            choices[name] = choice
    journey = journey if isinstance(journey, dict) and journey.get("v") == JOURNEY_VERSION else None
    if journey and isinstance(journey.get("notices"), dict):
        state["notices"] = _plain(journey["notices"])
    entry["sets"] = {name: value for name, value in sets.items() if fits(value)}
    entry["state"] = {name: value for name, value in state.items() if fits(value)}
    entry["choice"] = choices
    if journey:
        days = {str(day): _plain(d) for day, d in _as_map(journey.get("days")).items()
                if isinstance(day, int) and not isinstance(day, bool) and DAY_KEY.match(str(day)) and isinstance(d, dict)}
        newest = sorted(days, key=int, reverse=True)
        past = _plain_map(_plain(journey.get("past")))
        for day in newest[days_most:]:        # left out: added into all time, so its totals stay whole
            add_into(past, days[day])
        deaths = [_plain(death) for death in _as_map(journey.get("deaths")).values() if isinstance(death, dict)]
        part = {"days": {day: days[day] for day in newest[:days_most] if fits(days[day])},
                "deaths": [death for death in deaths[-deaths_most:] if fits(death, 1)] if deaths_most else []}
        for name, value in (("past", past), ("zones", _plain_map(_plain(journey.get("levelZones")))),
                            ("records", _plain_map(_plain(journey.get("records"))))):
            if value and fits(value):
                part[name] = value
        entry["j"] = part
    return entry if _play(entry) is None else None


def play_files(saved_dir):
    """{"<realm>/<character>": that character's SavedVariables folder} under the WoW account of `saved_dir`."""
    out = {}
    for folder in sorted(saved_dir.parent.glob("*/*/SavedVariables")):
        key = f"{folder.parent.parent.name}/{folder.parent.name}"
        if folder.is_dir() and PLAY_KEY.match(key):
            out[key] = folder
    return out


# ---------------------------------------------------------------------------------------------
# Records and books from before full names
# ---------------------------------------------------------------------------------------------

def renames(chars, known):
    """({old key: full-name key}, [old keys more than one character fits]) for the records in `chars` kept under a first
    name only, as builds before 5 November did. One fits when it is on the same realm, its first name is that name, it
    is of the same class, and it isn't a lower level. `known`: every record there is of a character, here or on another PC."""
    moves, unresolved = {}, []
    for key, record in chars.items():
        realm, _, name = key.partition("/") if isinstance(key, str) else ("", "", "")
        if not name or " " in name or not isinstance(record, dict) or not record.get("class"):
            continue
        fits = set()
        for other, theirs in known.items():
            their_realm, _, their_name = other.partition("/")
            if their_realm != realm or their_name.split(" ", 1)[0] != name or " " not in their_name or not isinstance(theirs, dict):
                continue
            if theirs.get("class") != record["class"]:
                continue
            if whole(record.get("level"), 0, 1000) and whole(theirs.get("level"), 0, 1000) and theirs["level"] < record["level"]:
                continue
            fits.add(other)
        if len(fits) == 1:
            moves[key] = fits.pop()
        elif len(fits) > 1:
            unresolved.append(key)
    return moves, sorted(unresolved)


def _merge_book(into, book):
    """Two books of one character as one: day sums added, the later closing gold, rows once each, newest kept."""
    days = into.setdefault("days", {})
    for day, row in book.get("days", {}).items():
        have = days.setdefault(day, {"e": {}, "s": {}})
        for side in ("e", "s"):
            for kind, copper in row.get(side, {}).items():
                have.setdefault(side, {})[kind] = have.get(side, {}).get(kind, 0) + copper
        if row.get("g") is not None and (have.get("g") is None or (row.get("gt") or 0) > (have.get("gt") or 0)):
            have["g"] = row["g"]
            have.pop("gt", None)
            if row.get("gt") is not None:
                have["gt"] = row["gt"]
    for kind in KINDS:
        rows = {_row_key(row): row for row in into.get(kind, []) + book.get(kind, [])}
        into[kind] = sorted(rows.values(), key=lambda row: (row["t"], str(_row_key(row))))[-MOST_RECORDS:]
    rows = {_auction_key(row): row for row in into.get("auctions", []) + book.get("auctions", [])}
    if rows:
        into["auctions"] = sorted(rows.values(), key=lambda row: (row["t"], str(_auction_key(row))))[-MOST_RECORDS:]
    return into


def renamed(copy, known):
    """A copy with its first-name records and books under the full name where one character fits: (copy, unresolved)."""
    chars, books = copy.get("chars", {}), copy.get("books", {})
    moves, unresolved = renames(chars, known)
    if not moves:
        return copy, unresolved
    out = dict(copy, chars=dict(chars), books=dict(books))
    for old, new in moves.items():
        record = out["chars"].pop(old)
        if new not in out["chars"]:
            out["chars"][new] = dict(record, name=new.partition("/")[2])
        if old in out["books"]:
            book = out["books"].pop(old)
            out["books"][new] = _merge_book(out["books"][new], book) if new in out["books"] else book
    return out, unresolved


def copy_of(saved, now, days_most=MOST_DAYS, rows_most=MOST_RECORDS, play_days=MOST_PLAY_DAYS, deaths_most=MOST_DEATHS, known=None):
    """This PC's copy from its saved files: {made, chars, books, settings, play}. `known`: the other PCs' records of
    characters, for moving a record kept under a first name only to the character's full name."""
    copy = {"made": int(now), "chars": {}, "books": {}, "settings": settings_of(saved, now), "play": {}}
    own, market = saved.get(OWN_FILE), saved.get(MARKET_FILE)
    chars = own.get(CHARACTERS) if own is not None and isinstance(own.get(CHARACTERS), dict) else {}
    played = [key for key, record in chars.items() if key != GOLD and isinstance(key, str) and BOOK_KEY.match(key)
              and isinstance(record, dict) and record.get("level") is not None]     # one known only by name has no facts
    for key in sorted(played, key=lambda key: -(_digits(chars[key].get("at")) or 0))[:MOST_CHARS]:
        record = _clean_record(chars[key])
        if trusted(record.get("at"), now) is None:
            record.pop("at", None)
        if _record(record):
            copy["chars"][key] = record
    ledger = (market.get(MARKET_DB) or {}).get("ledger") if market is not None and isinstance(market.get(MARKET_DB), dict) else None
    for key, book in (ledger.items() if isinstance(ledger, dict) else ()):
        if isinstance(key, str) and BOOK_KEY.match(key) and isinstance(book, dict) and len(copy["books"]) < MOST_CHARS:
            cleaned = _clean_book(book, days_most, rows_most)
            for row in cleaned["days"].values():
                if "gt" in row and trusted(row["gt"], now) is None:
                    del row["gt"]
            copy["books"][key] = cleaned
    copy, _ = renamed(copy, {**(known or {}), **copy["chars"]})
    plays = []
    for key, files in (saved.get("play") or {}).items():
        db = (files.get("own") or {}).get(CHARACTER_DB)
        name = key.partition("/")[2].replace("-", " ")
        records = [record for record_key, record in copy["chars"].items() if record_key.partition("/")[2] == name]
        record_at = records[0].get("at") if len(records) == 1 else None
        entry = play_of(db, (files.get("journey") or {}).get(JOURNEY_DB), now, record_at, play_days, deaths_most)
        if entry is not None:
            plays.append((key, entry))
    for key, entry in sorted(plays, key=lambda pair: -(pair[1].get("at") or 0))[:MOST_CHARS]:
        copy["play"][key] = entry
    return copy


def packed(copy):
    """A copy as (its JSON, gzipped). The JSON is sorted, so the same data is the same bytes."""
    text = json.dumps(copy, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return text, gzip.compress(text, mtime=0)


def fitted(saved, now, known=None):
    """This PC's copy within what the server takes, trimming only what's sent, or None."""
    for days_most, rows_most, play_days, deaths_most in FITS:
        copy = copy_of(saved, now, days_most, rows_most, play_days, deaths_most, known)
        text, zipped = packed(copy)
        if len(text) <= MOST_UNPACKED and len(zipped) <= MOST_PACKED:
            return copy
    return None


def _moved_to_full(key, now):
    """A key under a first name whose character is now kept under the full name: moved, not lost."""
    realm, _, name = key.partition("/")
    return " " not in name and any(other.startswith(f"{realm}/{name} ") for other in now)


def _restarted(before, after):
    """Whether a character's ledger began again: days it had are gone, and it holds none from before them (its saved
    file was lost, then the character played). A ledger trimmed to fit drops only its oldest days, keeping the rest."""
    had = {int(day) for day in before.get("days", {})}
    has = {int(day) for day in after.get("days", {})}
    return bool(had - has) and bool(has) and min(has) >= max(had)


def lost_parts(before, after):
    """What a PC's last copy held that its copy now doesn't, by character: {"chars", "books", "play", "settings"}, or
    None when nothing is lost. A ledger that began again counts as lost whole. Settings count only beside a lost
    character: a setting alone goes when it's cleared."""
    out = {"chars": {}, "books": {}, "play": {}, "settings": {}}
    for part in ("chars", "books", "play"):
        now = after.get(part, {})
        for key, value in before.get(part, {}).items():
            if key not in now and not (part != "play" and _moved_to_full(key, now)):
                out[part][key] = value
            elif part == "books" and key in now and _restarted(value, now[key]):
                out[part][key] = value
    if not any(out.values()):
        return None
    now = after.get("settings", {})
    out["settings"] = {key: entry for key, entry in before.get("settings", {}).items() if key not in now}
    return out


def with_lost(backup, lost):
    """The backup with more of what was lost: what it holds already stays, frozen. A ledger lost again adds to the one
    kept (what came between), and a character's newer record replaces its older."""
    out = {part: dict(backup.get(part, {})) for part in ("chars", "books", "play", "settings")}
    for part in out:
        for key, value in lost.get(part, {}).items():
            if part == "books" and key in out[part]:
                out[part][key] = _merge_book(json.loads(json.dumps(out[part][key])), value)
            elif part == "chars" and key in out[part]:
                if (value.get("at") or 0) > (out[part][key].get("at") or 0):
                    out[part][key] = value
            else:
                out[part].setdefault(key, value)
    return out


def with_backup(copy, backup):
    """This PC's copy with its backup in: a character only the backup has, whole; a ledger both have, added (its
    days and rows from before and since); a setting only where the copy has none. Within the copy's limits, or the
    copy as it was."""
    out = dict(copy, chars=dict(copy.get("chars", {})), books=dict(copy.get("books", {})), play=dict(copy.get("play", {})),
               settings=dict(copy.get("settings", {})))
    for part in ("chars", "play", "settings"):
        for key, value in backup.get(part, {}).items():
            if key not in out[part] and (part == "settings" or len(out[part]) < MOST_CHARS):
                out[part][key] = value
    for key, book in backup.get("books", {}).items():
        if key in out["books"]:
            out["books"][key] = _merge_book(json.loads(json.dumps(out["books"][key])), book)
        elif len(out["books"]) < MOST_CHARS:
            out["books"][key] = book
    if problem(out):
        return copy
    text, zipped = packed(out)
    return out if len(text) <= MOST_UNPACKED and len(zipped) <= MOST_PACKED else copy


def data_of(copy):
    """A copy without when it was made: what says whether anything changed."""
    return {key: value for key, value in copy.items() if key != "made"}


# ---------------------------------------------------------------------------------------------
# The other PCs' copies, as the game's tables
# ---------------------------------------------------------------------------------------------

def _pairs(side):
    return ",".join(f"{kind}={copper}" for kind, copper in sorted(side.items()))


def _row_key(row):
    return (row.get("t"), row.get("name"), row.get("item"), row.get("n"), row.get("each"), row.get("why"))


def _auction_key(row):
    return tuple(row.get(field) for field in AUCTION_FIELDS)


def _empty(value):
    return "" if value is None else str(value)


def _book_lines(key, books, own_book, now):
    """One character's other-PC book, each thing a line of text (Ledger.lua's unpackBook reads it). Day sums add per
    install, each install's newest copy counted once. Closing gold, the later stamp; rows, identical ones once."""
    days = {}
    for book in books:
        for day, row in book.get("days", {}).items():
            total = days.setdefault(int(day), {"e": {}, "s": {}})
            for side in ("e", "s"):
                for kind, copper in row.get(side, {}).items():
                    total[side][kind] = total[side].get(kind, 0) + copper
            if row.get("g") is not None:
                stamp = trusted(row.get("gt"), now)
                if "g" not in total or (stamp and stamp > (total.get("gt") or 0)):
                    total["g"] = row["g"]
                    total.pop("gt", None)
                    if stamp:
                        total["gt"] = stamp
    out = {"days": "\n".join(f"{day}\t{_empty(row.get('g'))}\t{_empty(row.get('gt'))}\t{_pairs(row['e'])}\t{_pairs(row['s'])}"
                             for day, row in sorted(days.items()))}
    for kind in KINDS:
        held = {_row_key(row) for row in own_book.get(kind, []) if isinstance(row, dict)} if own_book else set()
        rows = {}
        for book in books:
            for row in book.get(kind, []):
                if _row_key(row) not in held:
                    rows[_row_key(row)] = row
        ordered = sorted(rows.values(), key=lambda row: (row["t"], str(_row_key(row))))[-MOST_RECORDS:]
        out[kind] = "\n".join("\t".join((str(row["t"]), row["name"], _empty(row.get("item")), _empty(row.get("n")),
                                         _empty(row.get("each")), _empty(row.get("why")))) for row in ordered)
    # The auction records: each one's fields in Ledger.lua's order (AUCTION_FIELDS), a tab between.
    held = {_auction_key(row) for row in own_book.get("auctions", []) if isinstance(row, dict)} if own_book else set()
    rows = {}
    for book in books:
        for row in book.get("auctions", []):
            if _auction_key(row) not in held:
                rows[_auction_key(row)] = row
    ordered = sorted(rows.values(), key=lambda row: (row["t"], str(_auction_key(row))))[-MOST_RECORDS:]
    out["auctions"] = "\n".join("\t".join(_empty(row.get(field)) for field in AUCTION_FIELDS) for row in ordered)
    return {name: text for name, text in out.items() if text}


def _bags_back(record):
    """A record as the game keeps it: its item ids are numbers again."""
    out = dict(record)
    for field in ("bags", "bank"):
        if isinstance(out.get(field), dict):
            out[field] = {int(item): count for item, count in out[field].items()}
    return out


def lua_back(value):
    """Copy data as the game keeps it: a key that is a number is a number again, and keys 1 to n are a list, as the
    saved file reads back."""
    if isinstance(value, dict):
        out = {(int(key) if isinstance(key, str) and re.match(r"^-?\d{1,12}$", key) else key): lua_back(item) for key, item in value.items()}
        keys = list(out)
        if keys and all(isinstance(key, int) for key in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
            return [out[key] for key in range(1, len(keys) + 1)]
        return out
    if isinstance(value, list):
        return [lua_back(item) for item in value]
    return value


def known_records(saved, copies):
    """Every record of a character there is, this PC's and the other PCs', by key."""
    own = saved.get(OWN_FILE) or {}
    out = {}
    for copy in copies:
        out.update(copy.get("chars", {}))
    chars = own.get(CHARACTERS) if isinstance(own.get(CHARACTERS), dict) else {}
    out.update({key: record for key, record in chars.items() if isinstance(key, str) and isinstance(record, dict)})
    return out


def plays_for(copies):
    """{"<realm>/<character>": [each other PC's play of it, oldest install first]}."""
    out = {}
    for copy in copies:
        for key, entry in copy.get("play", {}).items():
            if PLAY_KEY.match(key):
                out.setdefault(key, []).append(lua_back(entry))
    return out


def tables_for(saved, copies, now):
    """(XPCompassSync.peers, ForeverCoachMarketPeers' books) from the other PCs' copies, oldest install first. What this
    PC's record of a character already beats is left out, and a setting it already holds with the same stamp. A copy's
    records kept under a first name are taken as the full name's where one character fits (an older app's copy)."""
    own, market = saved.get(OWN_FILE) or {}, saved.get(MARKET_FILE) or {}
    own_chars = own.get(CHARACTERS) if isinstance(own.get(CHARACTERS), dict) else {}
    known = known_records(saved, copies)
    copies = [renamed(copy, known)[0] for copy in copies]
    chars = {}
    for copy in copies:
        for key, record in copy.get("chars", {}).items():
            have = chars.get(key)
            if not have or (trusted(record.get("at"), now) or 0) > (trusted(have.get("at"), now) or 0):
                chars[key] = record
    kept = {}
    for key, record in chars.items():
        mine = own_chars.get(key) if isinstance(own_chars.get(key), dict) else None
        mine_at = (trusted(_digits(mine.get("at")), now) or 0) if mine and mine.get("level") is not None else -1
        if (trusted(record.get("at"), now) or 0) > mine_at:
            kept[key] = _bags_back(record)
    settings = {}
    for copy in copies:
        for key, entry in copy.get("settings", {}).items():
            stamp = trusted(entry.get("at"), now)
            have = settings.get(key)
            have_at = trusted(have.get("at"), now) if have else None
            if have is None or (stamp and (not have_at or stamp > have_at)):
                settings[key] = {name: value for name, value in entry.items() if name != "at" or stamp}
    mine = settings_of(saved, now)
    settings = {key: entry for key, entry in settings.items()
                if not (key in mine and uploads.same(mine[key].get("v"), entry.get("v")) and mine[key].get("at") == entry.get("at"))}
    db = market.get(MARKET_DB) if isinstance(market.get(MARKET_DB), dict) else {}
    ledger = db.get("ledger") if isinstance(db.get("ledger"), dict) else {}
    books = {}
    names = sorted({key for copy in copies for key in copy.get("books", {})})
    for key in names:
        theirs = [copy["books"][key] for copy in copies if key in copy.get("books", {})]
        lines = _book_lines(key, theirs, _clean_book(ledger[key]) if isinstance(ledger.get(key), dict) else None, now)
        if lines:
            books[key] = lines
    return {"chars": kept, "settings": settings}, books


def _rev(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:16]


def put(path, name, build, running):
    """Set one assignment of a saved file, build(its value now) being the new value, changing nothing else. Only while
    the game is closed; the same value writes nothing. A file that can't be read, or won't read back as meant, stays as
    it is. Returns whether it was written. A saved file WoW hasn't made yet is made."""
    if running():
        return False
    try:
        text = path.read_bytes().decode("utf-8").replace("\r\n", "\n") if path.exists() else ""
        saved, spans = uploads.read_saved(text)
    except (OSError, UnicodeDecodeError, uploads.LuaError):
        return False
    value = build(saved.get(name))
    if value is None or (name in saved and uploads.same(saved[name], value)):
        return False
    if name in spans:
        start, end = spans[name]
        new_text = text[:start] + f"{name} = " + uploads.lua(value) + text[end:]
    else:
        new_text = text + ("" if not text or text.endswith("\n") else "\n") + f"{name} = " + uploads.lua(value) + "\n"
    check, _ = uploads.read_saved(new_text)
    if not uploads.same(check, dict(saved, **{name: value})):
        return False
    temp = path.with_name(path.name + ".xpc-sync-tmp")
    temp.write_bytes(new_text.encode("utf-8"))
    if running():                       # the game started meanwhile: leave its file alone
        temp.unlink()
        return False
    os.replace(temp, path)
    return True


class Sync:
    def __init__(self, uploader, path=None, clock=time.time, fetch=None):
        self.uploader, self.clock = uploader, clock
        self.asked = False           # this look comes from Check now
        self.path = path or uploader.path.with_name("sync.json")
        self.cache = self.path.with_name("sync-copies")
        self.fetch = fetch or self._get
        self.switches = {}           # each XPCompass.lua's size and time when last read, and what its switch said
        self.state = self._load()

    def _load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state = state if isinstance(state, dict) else {}
        state["accounts"] = state.get("accounts") if isinstance(state.get("accounts"), dict) else {}
        state["retry"] = state.get("retry") if isinstance(state.get("retry"), (int, float)) else 0
        return state

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(temp, self.path)

    # One look

    def look(self, game, game_id="forever"):
        """Each WoW account folder under a client, once. Returns how many saved files were written."""
        account = self.uploader.account
        self.asked = bool(getattr(self.uploader, "check_now", False))   # Check now: ask the server and send, whatever the timers say
        if self.asked:
            self.uploader.check_now = False
            self.state["retry"] = 0
        if (account and not account.session()) or self.clock() < self.state["retry"]:
            return 0
        accounts = Path(game) / "WTF" / "Account"
        if not accounts.is_dir():
            return 0
        written = 0
        for saved_dir in sorted(accounts.glob("*/SavedVariables")):
            try:
                written += self._account(saved_dir, game_id)
            except uploads.UploadError as failure:
                self.state["retry"] = self.clock() + RETRY
                self.uploader.last = {"at": self.clock(), "text": f"Computers not synced, will try again: {failure}"}
                break
        self._save()
        return written

    def _stats(self, saved_dir):
        out = {}
        paths = [(name, saved_dir / name) for name in (OWN_FILE, MARKET_FILE, MAIL_FILE)]
        for key, folder in play_files(saved_dir).items():
            paths += [(f"{key}/{name}", folder / name) for name in (OWN_FILE, JOURNEY_FILE)]
        for name, path in paths:
            try:
                stat = path.stat()
                out[name] = [stat.st_mtime_ns, stat.st_size]
            except OSError:
                out[name] = None
        return out

    def _read(self, saved_dir):
        """{file: {name: value}} of the saved files there are, with "play": {character: {"own", "journey"}} for each
        character's, or None when any can't be read: then nothing is sent or written this time, since an unreadable file
        must not look like settings cleared. Sharing off reads as False."""
        def parsed(path):
            return uploads.read_saved(path.read_bytes().decode("utf-8").replace("\r\n", "\n"))[0]

        saved = {}
        try:
            for name in (OWN_FILE, MARKET_FILE, MAIL_FILE):
                if (saved_dir / name).exists():
                    saved[name] = parsed(saved_dir / name)
            saved["play"] = {}
            for key, folder in play_files(saved_dir).items():
                saved["play"][key] = {part: parsed(folder / name) for part, name in (("own", OWN_FILE), ("journey", JOURNEY_FILE))
                                      if (folder / name).exists()}
        except (OSError, UnicodeDecodeError, uploads.LuaError):
            return None
        records = (saved.get(OWN_FILE) or {}).get(uploads.RECORDS)
        if isinstance(records, dict) and (records.get("share") is False or records.get("on") is False):
            return False
        return saved

    def _switched_off(self, saved_dir):
        """Whether Settings' switch is off in this WoW account's saved file. On unless it says so. None when it can't be
        read, the file not there yet included. The file is only read again when it has changed."""
        path = saved_dir / OWN_FILE
        try:
            stat = path.stat()
            seen = [stat.st_mtime_ns, stat.st_size]
            known = self.switches.get(str(path))
            if known and known[0] == seen:
                return known[1]
            saved, _ = uploads.read_saved(path.read_bytes().decode("utf-8").replace("\r\n", "\n"))
        except (OSError, UnicodeDecodeError, uploads.LuaError):
            return None
        table = saved.get(STAMPS_TABLE)
        if table is not None and not isinstance(table, dict):
            return None
        said = table.get("off") if table else None
        if said not in (None, True):
            return None
        self.switches[str(path)] = (seen, said is True)
        return said is True

    def _switch_off(self, saved_dir, key, game_id, scope):
        """This PC's switch is off: the other PCs' tables go once the game is closed, and this install's copy leaves the
        server, once. Nothing else is sent or fetched."""
        accounts = self.state["accounts"]
        mine = accounts[key]
        written = 0
        if not mine.get("cleared") and not self.uploader.running():
            written = self._clear_peers(saved_dir, game_id, scope)
            mine["cleared"] = self._peers_gone(saved_dir)
        if not mine.get("removed"):
            self._send(game_id, scope, None, off=True)
            accounts[key] = dict(fresh_account(), removed=True, cleared=mine.get("cleared"))
        return written

    def _clear_peers(self, saved_dir, game_id, scope):
        def without_peers(before):
            if not isinstance(before, dict) or not {"peers", "rev", "merged"} & set(before):
                return None
            return {name: value for name, value in before.items() if name not in ("peers", "rev", "merged")}

        def without_books(before):
            return {} if before else None

        written = 0
        if (saved_dir / OWN_FILE).exists() and put(saved_dir / OWN_FILE, STAMPS_TABLE, without_peers, self.uploader.running):
            written += 1
        if (saved_dir / MARKET_FILE).exists() and put(saved_dir / MARKET_FILE, PEERS_TABLE, without_books, self.uploader.running):
            written += 1
        for folder in play_files(saved_dir).values():
            if (folder / OWN_FILE).exists() and put(folder / OWN_FILE, PLAY_PEERS, without_books, self.uploader.running):
                written += 1
        for path in self.cache.glob(f"{game_id}-{scope}-*.json.gz"):     # the app's own copies of the other PCs' data
            try:
                path.unlink()
            except OSError:
                pass
        return written

    def _peers_gone(self, saved_dir):
        try:
            own, _ = uploads.read_saved((saved_dir / OWN_FILE).read_bytes().decode("utf-8").replace("\r\n", "\n"))
            market_path = saved_dir / MARKET_FILE
            market = uploads.read_saved(market_path.read_bytes().decode("utf-8").replace("\r\n", "\n"))[0] if market_path.exists() else {}
            plays = [uploads.read_saved((folder / OWN_FILE).read_bytes().decode("utf-8").replace("\r\n", "\n"))[0]
                     for folder in play_files(saved_dir).values() if (folder / OWN_FILE).exists()]
        except (OSError, UnicodeDecodeError, uploads.LuaError):
            return False
        table = own.get(STAMPS_TABLE)
        return (not (isinstance(table, dict) and "peers" in table) and not market.get(PEERS_TABLE)
                and not any(play.get(PLAY_PEERS) for play in plays))

    def _account(self, saved_dir, game_id):
        now = self.clock()
        scope = scope_of(saved_dir.parent.name)
        key = f"{game_id}/{scope}"
        accounts = self.state["accounts"]
        off = self._switched_off(saved_dir)
        if off is None:                 # can't tell: as if off, for this look, and nothing is sent or written
            return 0
        accounts.setdefault(key, fresh_account())
        if off:
            return self._switch_off(saved_dir, key, game_id, scope)
        if accounts[key].get("removed") or accounts[key].get("cleared"):    # on again: a first sync
            accounts[key] = fresh_account()
        mine = accounts[key]
        if not self.uploader.sharing():
            return 0
        running = self.uploader.running()
        stats = self._stats(saved_dir)
        changed = stats != mine["seen"]
        closed_now = mine["ran"] and not running
        mine["ran"] = running
        if self.asked or now >= mine["polled"] + POLL_EVERY or closed_now or "listed" not in mine:
            listed = self._list(game_id, scope)
            if listed["copies"] != mine.get("listed"):
                mine["dirty"] = True    # another PC has something new
            mine["polled"], mine["listed"], mine["known"] = now, listed["copies"], bool(listed.get("me"))
        waited = mine.get("wake") and now >= mine["wake"]
        if not (self.asked or changed or waited or not mine["known"] or (mine["dirty"] and not running)):
            return 0                    # the saved files are only read when there's cause
        saved = self._read(saved_dir)
        if not saved:                   # can't be read, or sharing's off: this PC carries on as it is
            mine["seen"], mine["dirty"] = stats, False
            if saved is None:
                self.uploader.last = {"at": now, "text": "A saved file could not be read, so your computers weren't synced."}
            return 0
        others = mine["listed"]
        mine["wake"] = None
        sent = self._send_copy(mine, saved, game_id, scope, now)   # every signed-in PC's: its backup, and what its other PCs see
        if not mine["known"]:
            if not sent:                # nothing to send yet: the server hears that this install exists
                self._send(game_id, scope, None)
            mine["known"] = True
        mine["seen"] = stats
        if running:
            mine["dirty"] = True        # written once the game has closed
            return 0
        written = self._bring(saved_dir, saved, mine, others, game_id, scope, now)
        mine["dirty"] = False
        mine["seen"] = self._stats(saved_dir)
        return written

    def _held_copies(self, mine, game_id, scope):
        """The other PCs' copies this app holds already, without asking the server."""
        return [copy for copy in (self._cached(self._copy_path(game_id, scope, copy_id)) for copy_id in mine["copies"]) if copy]

    def _send_copy(self, mine, saved, game_id, scope, now):
        held = self._held_copies(mine, game_id, scope)
        known = {}
        for other in held:
            known.update(other.get("chars", {}))
        own = saved.get(OWN_FILE) or {}
        chars = own.get(CHARACTERS) if isinstance(own.get(CHARACTERS), dict) else {}
        moves, unresolved = renames({key: record for key, record in chars.items() if isinstance(key, str)}, known_records(saved, held))
        mine["unresolved"] = unresolved     # a first-name record more than one character fits: left as it is
        mine["moves"] = sorted(moves.items())
        copy = fitted(saved, now, known)
        if copy is None or problem(copy):
            self.uploader.last = {"at": now, "text": "Your data is too large to back up and sync."}
            return False
        backup = self._backup(game_id, scope, copy)
        sending = with_backup(copy, backup) if backup else copy
        rev = _rev(data_of(sending))
        if rev == mine["sent"]:
            return False
        if now < mine["sent_at"] + SEND_GAP and not self.asked:
            mine["wake"] = mine["sent_at"] + SEND_GAP       # it goes then, with no new reason to read the files
            return False
        self._send(game_id, scope, sending)
        mine["sent"], mine["sent_at"] = rev, now
        self._keep(self._backup_path(game_id, scope, "sent"), copy)
        return True

    # What this PC sent last, and what it lost since, kept beside the app (not in sync-copies: switching sync off
    # clears those, and this is this PC's own data)

    def _backup_path(self, game_id, scope, part):
        return self.path.with_name("sync-backup") / f"{game_id}-{scope}-{part}.json.gz"

    def _keep(self, path, value):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".tmp")
            temp.write_bytes(packed(value)[1])
            os.replace(temp, path)
        except OSError:
            pass

    def _held(self, path):
        try:
            value = unpack(path.read_bytes())
        except OSError:
            return None
        return value if isinstance(value, dict) else None

    def _backup(self, game_id, scope, copy):
        """This PC's frozen backup, with anything the last copy sent held that this one has lost added to it, or None."""
        backup = self._held(self._backup_path(game_id, scope, "backup"))
        last = self._held(self._backup_path(game_id, scope, "sent"))
        lost = lost_parts(last, copy) if last else None
        if lost:
            backup = with_lost(backup or {}, lost)
            self._keep(self._backup_path(game_id, scope, "backup"), backup)
        return backup if backup and any(backup.get(part) for part in ("chars", "books", "play", "settings")) else None

    def _backup_copy(self, game_id, scope, now):
        """The backup as another PC's copy, for the game's tables, or None."""
        backup = self._held(self._backup_path(game_id, scope, "backup"))
        if not backup:
            return None
        copy = {"made": int(now), **{part: backup.get(part, {}) for part in ("chars", "books", "settings", "play")}}
        return None if problem(copy) else copy

    def _bring(self, saved_dir, saved, mine, others, game_id, scope, now):
        copies = self._copies(mine, others, game_id, scope, now)
        if copies is None:              # one that can't be trusted: nothing changes until it can, and it's asked for again
            mine["wake"] = now + POLL_EVERY
            return 0
        backup = self._backup_copy(game_id, scope, now)
        if backup:                      # what this PC lost, shown as another PC's: filling gaps, overwriting nothing
            copies = copies + [backup]
        peers, books = tables_for(saved, copies, now)
        plays = plays_for(copies)
        own = saved.get(OWN_FILE) or {}
        chars = own.get(CHARACTERS) if isinstance(own.get(CHARACTERS), dict) else {}
        moves, mine["unresolved"] = renames({key: record for key, record in chars.items() if isinstance(key, str)}, known_records(saved, copies))
        if [list(move) for move in sorted(moves.items())] != [list(move) for move in mine.get("moves", [])]:
            mine["wake"] = now          # what came says where a first-name record belongs: this PC's copy goes again, moved
            mine["moves"] = sorted(moves.items())
        rev = _rev([peers, books])

        def sync_table(before):
            table = dict(before) if isinstance(before, dict) else {}
            if not table and not peers["chars"] and not peers["settings"]:
                return None             # nothing here, and nothing to bring
            table.update(peers=peers, rev=rev)
            return table

        def books_table(before):
            if not books and not (isinstance(before, dict) and before.get("books")):
                return None
            return {"rev": rev, "books": books}

        written = 0
        if put(saved_dir / OWN_FILE, STAMPS_TABLE, sync_table, self.uploader.running):
            written += 1
        if put(saved_dir / MARKET_FILE, PEERS_TABLE, books_table, self.uploader.running):
            written += 1
        return written + self._bring_play(saved_dir, plays)

    def _bring_play(self, saved_dir, plays):
        """Each character's XPCompassPlayPeers: the other PCs' play of it, in its own XPCompass.lua. A character played
        only on another PC gets its folder here once the realm's is, so its first login here knows where it is."""
        written = 0
        account = saved_dir.parent.resolve()
        folders = play_files(saved_dir)
        for key in sorted(set(folders) | set(plays)):
            entries = plays.get(key, [])
            folder = folders.get(key)
            if folder is None:
                realm, character = key.split("/")
                folder = saved_dir.parent / realm / character / "SavedVariables"
                if not entries or not folder.parent.parent.is_dir() or folder.resolve().parent.parent.parent != account:
                    continue
                folder.mkdir(parents=True, exist_ok=True)

            def build(before, entries=entries):
                if not entries:
                    return {} if before else None
                return {"rev": _rev(entries), "peers": entries}

            if put(folder / OWN_FILE, PLAY_PEERS, build, self.uploader.running):
                written += 1
        return written

    # The other PCs' copies, kept beside the app's files

    def _copy_path(self, game_id, scope, copy_id):
        return self.cache / f"{game_id}-{scope}-{int(copy_id)}.json.gz"

    def _copies(self, mine, others, game_id, scope, now):
        """Each other install's newest copy, in the order the server lists them, from here when it hasn't changed. A
        download that's too large or doesn't read as a copy is refused: None, and the last good one stays."""
        out = []
        held = mine["copies"]
        for entry in others:
            copy_id, rev = str(entry.get("id")), entry.get("rev")
            if not entry.get("full"):
                continue
            path = self._copy_path(game_id, scope, entry["id"])
            copy = self._cached(path) if held.get(copy_id) == rev else None
            if copy is None:
                body = self._fetch_copy(game_id, scope, entry["id"])
                copy = unpack(body)
                if copy is None or problem(copy):
                    self.uploader.last = {"at": now, "text": "A copy from your other computer could not be read, so nothing changed."}
                    return None
                self.cache.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + ".tmp")
                temp.write_bytes(body)
                os.replace(temp, path)
                held[copy_id] = rev
            out.append(copy)
        listed = {str(entry.get("id")) for entry in others if entry.get("full")}
        for copy_id in [key for key in held if key not in listed]:      # the server no longer has it: nor do we
            held.pop(copy_id)
            try:
                self._copy_path(game_id, scope, copy_id).unlink()
            except OSError:
                pass
        return out

    def _cached(self, path):
        try:
            copy = unpack(path.read_bytes())
        except OSError:
            return None
        return copy if copy is not None and not problem(copy) else None

    # Talking to the server

    def _authorised(self, call):
        """(status, answer) of `call(token)`, once more with a fresh token if the one it had has run out."""
        token = self.uploader.signed_token()
        status, answer = call(token)
        if status == 401 and token and self.uploader.account:
            try:
                token = self.uploader.account.refresh()["token"]
            except auth.AuthError as error:
                raise uploads.UploadError(auth.SIGN_IN_FIRST) from error
            status, answer = call(token)
        return status, answer

    def _send(self, game_id, scope, copy, off=False):
        payload = {"v": 1, "game": game_id, "scope": scope, "copy": copy}
        if off:
            payload["off"] = True       # the switch is off here: this install's copy goes
        data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        status, answer = self._authorised(lambda token: self.uploader.send("/sync", data, token, gz=True))
        if status != 200:
            raise uploads.UploadError(f"{status} {answer.get('error', '') if isinstance(answer, dict) else ''}".strip())
        return answer

    def _list(self, game_id, scope):
        query = urllib.parse.urlencode({"game": game_id, "scope": scope})
        status, raw = self._authorised(lambda token: self.fetch(f"/sync?{query}", token))
        if status != 200:
            raise uploads.UploadError(f"{status}")
        try:
            listed = json.loads(raw)
        except ValueError:
            raise uploads.UploadError("the list of your other computers could not be read") from None
        if not isinstance(listed, dict) or not isinstance(listed.get("copies"), list) or not all(
                isinstance(entry, dict) and isinstance(entry.get("id"), int) for entry in listed["copies"]):
            raise uploads.UploadError("the list of your other computers could not be read")
        return listed

    def _fetch_copy(self, game_id, scope, copy_id):
        query = urllib.parse.urlencode({"game": game_id, "scope": scope, "id": copy_id})
        status, raw = self._authorised(lambda token: self.fetch(f"/sync/copy?{query}", token))
        if status != 200:
            raise uploads.UploadError(f"{status}")
        return raw

    def _get(self, path, token):
        server = self.uploader.settings.server
        headers = {"X-XPC-Install": self.uploader.install, "User-Agent": "ForeverCoachCompanion"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(server + path, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read(MOST_PACKED + 200_000)
        except urllib.error.HTTPError as error:
            return error.code, error.read(10_000)
        except OSError as error:
            raise uploads.UploadError(f"could not reach the XPCompass server: {error}") from error


def fresh_account():
    """What's remembered of a WoW account folder before its first sync."""
    return {"seen": None, "polled": 0, "sent": None, "sent_at": 0, "copies": {}, "ran": False, "dirty": True}


def unpack(body):
    """A downloaded copy, or None: gzip of at most MOST_UNPACKED bytes of JSON."""
    if not isinstance(body, (bytes, bytearray)) or len(body) > MOST_PACKED + 100_000:
        return None
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as packed_file:
            text = packed_file.read(MOST_UNPACKED + 1)
        if len(text) > MOST_UNPACKED:
            return None
        return json.loads(text)
    except (OSError, EOFError, ValueError):
        return None
