"""XPCompass's errors that the game writes down itself, sent to the XPCompass server (server/README.md,
"POST /upload"). The game writes runtime Lua errors to no file: the base addon catches those
(addon/XPCompass/Errors.lua) and they travel with the recorded rows (uploads.py). This reads the two
it does write, every minute with the uploads:

- **Load errors.** <game>/Logs/FrameXML.log gets a line when an addon file fails to load:
  `9/27 14:04:25.130  Interface/AddOns/XPCompass/XPCompass_Camelot.toc:23 Error loading
  Interface/AddOns/XPCompass/Browser.lua`. A line naming an XPCompass folder is sent as a "load"
  row: the line, when it was written, and the build the app installed.
- **Crashes.** <game>/Errors/<time>_Error_<n>.txt is the game's crash report; the .dmp beside it is
  never opened. One is sent as a "crash" row only when the addon running at the crash
  (<Addons.Current>) is one of ours, and then only these fields: Addons.Current, its .Function and
  .Object, the Set.Addons.All.Loaded lines, ErrorDescription and Version. Nothing else in the report
  leaves the computer.

- **Sharing.** Nothing is sent for a player who turned sharing off before it became required: the
  app's settings or a WoW account's saved file say so (XPCompassRecords.share, and .on, whether
  sharing was on at the last login). What the game wrote meanwhile is passed over and never sent.
- **Private.** A user's folder (C:\\Users\\<name>, /Users/<name>) and whatever follows WTF/Account
  (the account, realm and character) are taken out of every text sent.
- **Remembered** in errorlogs.json beside uploads.json: how far FrameXML.log was read, the crash
  reports already looked at, its own slot and row numbers, and rows the server has yet to confirm,
  which go again at the next look. A log or report already older than OLDEST when first seen is
  passed over.
- **One client each.** Every game's client has logs of its own, so each gets its own look and its own
  record: WoW Forever's is errorlogs.json, another game's errorlogs-<game>.json (errorlogs-era.json).
  Its rows say which game they came from, and the build is the one installed in that client.
"""
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import games, install, uploads
from .release import ReleaseError

LOG = Path("Logs") / "FrameXML.log"
CRASHES = "Errors"
OURS = re.compile(r"(?<![A-Za-z0-9_])XPCompass[A-Za-z0-9_]*[/\\]")   # a path into one of our folders
OUR_FOLDER = re.compile(r"^XPCompass[A-Za-z0-9_]*$")
LINE = re.compile(r"^(\d{1,2})/(\d{1,2}) (\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?\s+(.*)$")
TAG = re.compile(r"^<([A-Za-z0-9_.]+)>[ \t]?(.*)$")
FIELDS = {"Addons.Current": "cur", "Addons.Current.Function": "fn", "Addons.Current.Object": "object",
          "ErrorDescription": "desc", "Version": "ver"}
LOADED = "Set.Addons.All.Loaded"
OLDEST = 14 * 86400          # seconds
SETTLE = 30                  # seconds a crash report is left alone after it was written: the game may still be at it
LONGEST = {"line": 500, "desc": 1000, "cur": 60, "fn": 200, "object": 200, "ver": 40}
MOST_LOADED = 80
LOG_READ = 2_000_000         # bytes of FrameXML.log read at most, the newest
REPORT_READ = 8_000_000      # bytes of a crash report read at most
PENDING = 500                # rows kept waiting for the server at most
HOME = re.compile(r"(?i)\b[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s\"'<>]+|/Users/[^/\s\"'<>]+")
ACCOUNT = re.compile(r"(?i)(WTF[\\/]Account[\\/])[^\s\"'<>]+")


def private(text):
    """A text with any user folder and WTF account path taken out."""
    return ACCOUNT.sub(r"\1<account>", HOME.sub("<home>", text))


def look(uploader, game, game_id="forever"):
    """For the app's minute loop (tray.py): one GameLogs per client beside the uploader, each made at
    its first look."""
    if not isinstance(getattr(uploader, "game_logs", None), dict):
        uploader.game_logs = {}
    if game_id not in uploader.game_logs:
        uploader.game_logs[game_id] = GameLogs(uploader, game_id=game_id)
    return uploader.game_logs[game_id].look(game)


class GameLogs:
    def __init__(self, uploader, path=None, clock=time.time, game_id="forever"):
        self.uploader, self.clock, self.game_id = uploader, clock, game_id
        self.package = games.GAMES[game_id].package
        name = "errorlogs.json" if game_id == games.FOREVER.id else f"errorlogs-{game_id}.json"
        self.path = path or uploader.path.with_name(name)
        self.state = self._load()

    def _load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not re.match(r"^[A-Za-z0-9_-]{8,64}$", str(state.get("slot", ""))):
            state["slot"] = secrets.token_urlsafe(12)
        state["next"] = state.get("next") if isinstance(state.get("next"), int) and state["next"] > 0 else 1
        state["log"] = state.get("log") if isinstance(state.get("log"), dict) else {}
        state["crashes"] = state.get("crashes") if isinstance(state.get("crashes"), list) else []
        state["pending"] = state.get("pending") if isinstance(state.get("pending"), list) else []
        return state

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(temp, self.path)

    # One look

    def look(self, game):
        """Read what the game wrote since the last look; send our errors in it. Returns how many rows
        the server took."""
        game = Path(game)
        log, reports = self._log_changed(game), self._new_reports(game)
        if not log and not reports and not self.state["pending"]:
            return 0
        shares = self.sharing(game)
        if shares is None:
            return 0                        # a saved file it cannot read: look again next time
        if not shares:
            self._pass_over(game, log, reports)
            self.state["pending"] = []
            self._save()
            return 0
        build = self._build(game)
        rows = self._load_rows(game, log) + [row for path in reports for row in self._crash_rows(path)]
        self._mark(game, reports)
        for row in rows:
            row.update({key: value for key, value in build.items() if value})
            row["id"] = self.state["next"]
            self.state["next"] += 1
        self.state["pending"] = (self.state["pending"] + rows)[-PENDING:]
        self._save()
        return self._send()

    def sharing(self, game):
        """Whether XPCompass may share: the app's switch, and no WoW account's saved file saying no.
        None when a saved file cannot be read."""
        if not self.uploader.sharing():
            return False
        own, _ = self.uploader.saved_files(game)
        for path in own:
            try:
                saved, _ = uploads.read_saved(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, uploads.LuaError):
                return None
            records = saved.get(uploads.RECORDS)
            if isinstance(records, dict) and (records.get("share") is False or records.get("on") is False):
                return False
        return True

    def _send(self):
        pending = self.state["pending"]
        if not pending:
            return 0
        try:
            answer = self.uploader._upload(self.state["slot"], {"rows": pending[:uploads.BATCH]}, self.game_id)
        except uploads.UploadError as failure:
            self.uploader.last = {"at": self.clock(), "text": f"Error reports not sent, will try again: {failure}"}
            return 0
        upto = answer.get("upto", 0)
        self.state["pending"] = [row for row in pending if row["id"] > upto]
        self._save()
        self.uploader.last = {"at": self.clock(), "text": f"Sent {len(pending) - len(self.state['pending'])} error reports."}
        return answer.get("stored", 0)

    def _build(self, game):
        try:
            manifest = install.installed(game, self.package) or {}
        except (OSError, ValueError, ReleaseError):
            manifest = {}
        return {"b": str(manifest.get("build") or "")[:40], "v": str(manifest.get("version") or "")[:40]}

    # FrameXML.log: read on from where the last look stopped. The game writes a new one each session,
    # told apart by its first bytes.

    def _log_changed(self, game):
        """FrameXML.log's [time, size] when it changed since the last look, else None."""
        try:
            stat = (game / LOG).stat()
        except OSError:
            return None
        seen = [stat.st_mtime_ns, stat.st_size]
        return seen if seen != self.state["log"].get("seen") else None

    def _read_log(self, game, seen, upto=None):
        """(the bytes since the last look, where they start in the file); reads nothing with `upto`,
        only marks the file read that far."""
        mine = self.state["log"]
        with open(game / LOG, "rb") as handle:
            known = mine.get("head_n", 0)
            same = 0 < known <= seen[1] and mine.get("upto", 0) <= seen[1] \
                and hashlib.sha256(handle.read(known)).hexdigest() == mine.get("head")
            start = max(mine.get("upto", 0) if same else 0, seen[1] - LOG_READ)
            if upto is None:
                handle.seek(start)
                data = handle.read(seen[1] - start)
                upto = start + data.rfind(b"\n") + 1     # a line still being written waits for the next look
            else:
                data = b""
            handle.seek(0)
            head = handle.read(min(256, upto))
        mine.update(seen=seen, upto=upto, head=hashlib.sha256(head).hexdigest(), head_n=len(head))
        return data[:upto - start], start

    def _load_rows(self, game, seen):
        if not seen:
            return []
        first = not self.state["log"].get("seen")
        try:
            data, _ = self._read_log(game, seen)
        except OSError:
            return []
        if first and self.clock() - seen[0] / 1e9 > OLDEST:
            return []                        # a log from long before the app read logs
        modified = datetime.fromtimestamp(seen[0] / 1e9)
        rows = []
        for raw in data.decode("utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not OURS.search(line):
                continue
            at, text = log_time(line, modified)
            rows.append({"e": "load", "at": at or int(seen[0] / 1e9), "line": private(text)[:LONGEST["line"]]})
        return rows

    # Crash reports

    def _reports(self, game):
        folder = game / CRASHES
        return sorted(folder.glob("*.txt")) if folder.is_dir() else []

    def _new_reports(self, game):
        """Reports not looked at yet, once the game has finished writing them."""
        known, now = set(self.state["crashes"]), self.clock()
        new = []
        for path in self._reports(game):
            try:
                settled = now - path.stat().st_mtime > SETTLE
            except OSError:
                continue
            if path.name not in known and settled:
                new.append(path)
        return new

    def _mark(self, game, reports):
        """Remember the reports looked at, of those still in the folder."""
        present = {path.name for path in self._reports(game)}
        self.state["crashes"] = sorted((set(self.state["crashes"]) | {path.name for path in reports}) & present)

    def _crash_rows(self, path):
        try:
            stat = path.stat()
            if self.clock() - stat.st_mtime > OLDEST:
                return []
            with open(path, "rb") as handle:
                text = handle.read(REPORT_READ).decode("utf-8", errors="replace")
        except OSError:
            return []
        report = crash_fields(text)
        if not OUR_FOLDER.match(report.get("cur", "")):
            return []
        return [dict(report, e="crash", at=int(stat.st_mtime))]

    def _pass_over(self, game, log, reports):
        """Sharing is off: mark what the game wrote as seen, without reading it."""
        if log:
            try:
                self._read_log(game, log, upto=log[1])
            except OSError:
                pass
        self._mark(game, reports)


def log_time(line, modified):
    """(unix seconds, the rest of the line) of a FrameXML.log line ("9/27 14:04:25.130  ..."), local
    time; the year is the one that puts it on or before the day the log was written."""
    match = LINE.match(line)
    if not match:
        return None, line
    month, day, hour, minute, second = (int(part) for part in match.groups()[:5])
    try:
        when = datetime(modified.year, month, day, hour, minute, second)
        if when > modified + timedelta(days=1):
            when = when.replace(year=modified.year - 1)
        return int(when.timestamp()), match.group(6)
    except (ValueError, OverflowError, OSError):
        return None, match.group(6)


def crash_fields(text):
    """The fields of a crash report that may be sent, and nothing else."""
    out, loaded = {}, []
    for line in text.splitlines():
        match = TAG.match(line)
        if not match:
            continue
        tag, value = match.group(1), match.group(2).strip()
        if tag == LOADED and len(loaded) < MOST_LOADED:
            loaded.append(value[:120])
        elif tag in FIELDS and FIELDS[tag] not in out:
            out[FIELDS[tag]] = private(value)[:LONGEST[FIELDS[tag]]]
    if loaded:
        out["loaded"] = loaded
    return out
