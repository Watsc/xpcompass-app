"""Auction house scans from XPC - Market (addon/XPCompass_Market), sent to the XPCompass server
(server/README.md, "POST /market"). The addon keeps its whole scans in ForeverCoachMarketDB.scans, in
each WoW account's WTF/Account/<account>/SavedVariables/XPCompass_Market.lua, which the game writes at
logout, /reload and exit. Every minute, with the uploads, a file that changed is read again and each
scan the server doesn't hold yet goes to it, oldest first, one request each.

- **What goes.** The scan as the addon saved it: the auction house (realm and faction) and its map,
  when, how many auctions, and each item's auctions as unit buyout and stack size; the addon never
  reads sellers or bidders. With it, the XPCompass build the app installed. No account or character
  name, and no path.
- **Sharing.** As for the uploads: nothing goes for a player who turned sharing off before it became
  required (the app's settings, or XPCompassRecords.share, or .on at the last login).
  Scans made meanwhile are passed over and never sent.
- **Remembered** in market.json beside uploads.json: each file's size and time when last read, and
  the scans in it the server holds or that were passed over, by auction house and time. The game's
  file is never written: the addon keeps its scans within its own limit.
- **Failures.** When the server can't be reached or doesn't take a scan, nothing more goes for RETRY,
  so a server that doesn't know scans yet isn't sent the same one every minute. A scan the server
  refuses as one the game couldn't make (400), or one too large to send, is passed over.
"""
import gzip
import json
import os
import time
from pathlib import Path

from . import auth, games, install, uploads
from .release import ReleaseError

MARKET_FILE = "XPCompass_Market.lua"
MARKET_DB = "ForeverCoachMarketDB"
FIELDS = ("house", "realm", "faction", "ah", "map", "t", "api", "listings", "bids", "seconds", "items")
RETRY = 30 * 60              # seconds before trying again after the server didn't take a scan
LARGEST = 1_000_000          # bytes of a gzipped scan: the server takes no larger request


def look(uploader, game, game_id="forever"):
    """For the app's minute loop (tray.py): one MarketScans beside the uploader, made at its first look, for
    the client `game` of the game `game_id`."""
    if not isinstance(getattr(uploader, "market_scans", None), MarketScans):
        uploader.market_scans = MarketScans(uploader)
    return uploader.market_scans.look(game, game_id)


def key_of(scan):
    """A scan's name in market.json: its auction house and when it was made."""
    return f"{scan.get('house')}@{scan.get('t')}"


class MarketScans:
    def __init__(self, uploader, path=None, clock=time.time):
        self.uploader, self.clock = uploader, clock
        self.path = path or uploader.path.with_name("market.json")
        self.state = self._load()

    def _load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state = state if isinstance(state, dict) else {}
        state["files"] = state.get("files") if isinstance(state.get("files"), dict) else {}
        state["retry"] = state.get("retry") if isinstance(state.get("retry"), (int, float)) else 0
        return state

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(temp, self.path)

    # One look

    def look(self, game, game_id="forever"):
        """Send each account's scans the server lacks, tagged with the game they were made in (a client's
        files are told apart by their path, so each game's are remembered on their own). Returns how many
        the server took."""
        if self.clock() < self.state["retry"]:
            return 0
        accounts = Path(game) / "WTF" / "Account"
        if not accounts.is_dir():
            return 0
        taken = 0
        for path in sorted(accounts.glob(f"*/SavedVariables/{MARKET_FILE}")):
            taken += self._file(Path(game), path, game_id)
            if self.clock() < self.state["retry"]:
                break
        self._save()
        return taken

    def _file(self, game, path, game_id="forever"):
        mine = self.state["files"].setdefault(str(path), {"seen": None, "sent": [], "waiting": False})
        try:
            stat = path.stat()
        except OSError:
            return 0
        seen = [stat.st_mtime_ns, stat.st_size]
        if seen == mine["seen"] and not mine["waiting"]:
            return 0
        shares = self.sharing(path)
        if shares is None:
            return 0                        # the account's XPCompass file can't be read: look again next time
        try:
            saved, _ = uploads.read_saved(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, uploads.LuaError) as problem:
            self.uploader.last = {"at": self.clock(), "text": f"Could not read a Market file: {problem}"}
            mine["seen"] = seen
            return 0
        db = saved.get(MARKET_DB) if isinstance(saved.get(MARKET_DB), dict) else {}
        scans = db.get("scans") if isinstance(db.get("scans"), list) else []
        scans = [scan for scan in scans if isinstance(scan, dict) and isinstance(scan.get("items"), dict)]
        present = {key_of(scan) for scan in scans}
        sent = [key for key in mine["sent"] if key in present]      # what the addon dropped is forgotten
        new = sorted((scan for scan in scans if key_of(scan) not in sent), key=lambda scan: scan.get("t") or 0)
        mine.update(seen=seen, sent=sent, waiting=False)
        if not shares:
            sent += [key_of(scan) for scan in new]                  # made while sharing was off: never sent
            return 0
        build = self._build(game, game_id)
        taken = 0
        for scan in new:
            try:
                status, answer = self._send(scan, build, game_id)
            except uploads.UploadError as failure:
                status, answer = None, {"error": str(failure)}
            if status == 200:
                sent.append(key_of(scan))
                taken += answer.get("stored", 0)
                self.uploader.last = {"at": self.clock(), "text": f"Sent an auction house scan: {scan.get('house')},"
                                                                  f" {answer.get('auctions', 0)} auctions."}
            elif status in (400, 413):
                sent.append(key_of(scan))
                self.uploader.last = {"at": self.clock(), "text": "An auction house scan was not taken:"
                                                                  f" {answer.get('message') or answer.get('error')}"}
            else:
                self.state["retry"] = self.clock() + RETRY
                mine["waiting"] = True
                self.uploader.last = {"at": self.clock(), "text": "Auction house scan not sent, will try again: "
                                                                  f"{status or ''} {answer.get('error', '')}".strip()}
                break
        return taken

    def sharing(self, market_file):
        """Whether this WoW account shares: the app's switch, and its XPCompass file not saying no.
        None when that file can't be read."""
        if not self.uploader.sharing():
            return False
        own = market_file.with_name(uploads.ADDON_FILE)
        if not own.exists():
            return True
        try:
            saved, _ = uploads.read_saved(own.read_text(encoding="utf-8", errors="replace"))
        except (OSError, uploads.LuaError):
            return None
        records = saved.get(uploads.RECORDS)
        return not (isinstance(records, dict) and (records.get("share") is False or records.get("on") is False))

    def _build(self, game, game_id="forever"):
        """The XPCompass build the app installed in that client, or ""."""
        try:
            manifest = install.installed(game, games.GAMES[game_id].package) or {}
        except (OSError, ValueError, ReleaseError):
            manifest = {}
        return str(manifest.get("build") or "")[:40]

    # Talking to the server

    def _send(self, scan, build, game_id="forever"):
        payload = {"v": 1, "game": game_id, "b": build, "scan": {key: scan[key] for key in FIELDS if key in scan}}
        data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        if len(data) > LARGEST:
            return 413, {"error": "too_large", "message": f"the scan is {len(data) // 1000} KB packed"}
        token = self.uploader.signed_token()
        status, answer = self.uploader.send("/market", data, token, gz=True)
        if status == 401 and token and self.uploader.account:
            try:
                token = self.uploader.account.refresh()["token"]
            except auth.AuthError as error:     # signed out meanwhile: the scan waits for the next sign-in
                raise uploads.UploadError(auth.SIGN_IN_FIRST) from error
            status, answer = self.uploader.send("/market", data, token, gz=True)
        return status, answer
