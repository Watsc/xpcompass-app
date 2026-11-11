"""Other players' level times (server/forever_server/medians.py), brought into the game for XPC - Journey's
"you vs others" and the guide panel's pace line.

- **Asked.** About every ASK_EVERY seconds, and soon after the app starts, the app asks the server for
  each level's median in each game it has a client for (GET /medians.json?game=<id>, with the install
  id, and the token when signed in, as an upload goes). Nothing is sent: the times come from the level
  rows the recorder already shares. Each game's medians are its own and go only to its own client.
- **Brought back.** While the game is closed, the app puts them in each WoW account's XPC - Journey file,
  WTF/Account/<account>/SavedVariables/XPCompass_Journey.lua, as XPCompassLevelMedians = { at, levels =
  { [level] = seconds } }, only the levels the server has five players' times for. The file is rewritten
  whole (to a temporary file, read back, then swapped in) with only that value changed, never while the
  game runs: the game writes its own copy at logout. A file Journey hasn't written yet is left for the
  next look.
- **Sharing.** For a player who turned sharing off before it became required (uploads.opted_out), in
  the app or for that account in the game, nothing is asked and the medians put there before are taken
  out.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from . import tls
from pathlib import Path

from . import auth, system
from .uploads import ADDON_FILE, RECORDS, LuaError, lua, read_saved, same

JOURNEY_FILE = "XPCompass_Journey.lua"
NAME = "XPCompassLevelMedians"
ASK_EVERY = 3600
RETRY = 300                 # after a failed ask
STATE = "level-medians.json"
MAX_LEVEL = 59


def levels_of(body):
    """{level: seconds} from the server's answer, only what the game could use."""
    out = {}
    levels = body.get("levels") if isinstance(body, dict) else None
    for key, seconds in (levels.items() if isinstance(levels, dict) else []):
        if str(key).isdigit() and 1 <= int(key) <= MAX_LEVEL and isinstance(seconds, int) and not isinstance(seconds, bool) \
                and seconds > 0:
            out[int(key)] = seconds
    return out


class LevelMedians:
    def __init__(self, uploader, running=None, path=None, fetch=None, clock=time.time):
        self.uploader = uploader
        self.settings, self.account = uploader.settings, uploader.account
        self.running = running or system.game_running
        self.path = path or uploader.path.with_name(STATE)
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.state = self._load()

    def _load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state = state if isinstance(state, dict) else {}
        for key in ("asked", "medians"):      # per game: when last asked; {"at", "levels": {level: seconds}}
            if not isinstance(state.get(key), dict):
                state[key] = {}
        state.setdefault("files", {})          # saved file -> [its size and time, the digest written]
        return state

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(self.state), encoding="utf-8")
        os.replace(temp, self.path)

    def look(self, game, game_id="forever"):
        """For the app's minute loop (tray.py): the client `game` of the game `game_id`."""
        sharing = self.uploader.sharing()
        if not sharing:
            self.state["medians"].pop(game_id, None)
        elif self.clock() - self.state["asked"].get(game_id, 0) >= (
                ASK_EVERY if self.state["medians"].get(game_id) else RETRY):
            self._ask(game_id)
        if not self.running():
            for account in sorted((Path(game) / "WTF" / "Account").glob(f"*/SavedVariables/{ADDON_FILE}")):
                path = account.parent / JOURNEY_FILE
                if path.exists():
                    value = self.state["medians"].get(game_id) if sharing and self._game_shares(account) else None
                    self._write(path, self._lua(value))
        self._save()

    @staticmethod
    def _lua(medians):
        if not medians:
            return None
        return {"at": int(medians["at"]), "levels": {int(level): seconds for level, seconds in medians["levels"].items()}}

    @staticmethod
    def _game_shares(account_file):
        """Whether this WoW account kept sharing on (Record.lua's XPCompassRecords: share, or on at the last
        login, as uploads.opted_out reads them)."""
        try:
            saved, _ = read_saved(account_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, LuaError):
            return False
        records = saved.get(RECORDS)
        return not (isinstance(records, dict) and (records.get("share") is False or records.get("on") is False))

    # Asked

    def _ask(self, game_id="forever"):
        token = self.uploader._token()
        where = f"/medians.json?game={game_id}"
        status, body = self.fetch(where, token)
        if status == 401 and token and self.account:
            try:
                token = self.account.refresh()["token"]
            except auth.AuthError:
                token = None
            status, body = self.fetch(where, token)
        if status == 200:
            self.state["medians"][game_id] = {"at": int(self.clock()),
                                              "levels": {str(k): v for k, v in levels_of(body).items()}}
        elif status == 403:
            self.state["medians"].pop(game_id, None)
        else:
            self.state["asked"][game_id] = self.clock() - ASK_EVERY + RETRY   # down: again in a few minutes
            return
        self.state["asked"][game_id] = self.clock()

    def _fetch(self, path, token):
        headers = {"X-XPC-Install": self.uploader.install, "User-Agent": "ForeverCoachCompanion"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.settings.server + path, headers=headers)
        try:
            with tls.urlopen(request, timeout=60) as response:
                status, raw = response.status, response.read(1_000_000)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(100_000)
        except OSError:
            return 0, {}
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {}

    # Brought back

    def _write(self, path, value):
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        stat = self._stat(path)
        if self.state["files"].get(str(path)) == [stat, digest]:
            return                        # neither the file nor the medians changed since
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            saved, spans = read_saved(text)
        except (OSError, LuaError):
            return                        # never rewrite what cannot be read
        if same(saved.get(NAME), value):
            self.state["files"][str(path)] = [stat, digest]
            return
        assignment = f"{NAME} = {lua(value)}" if value is not None else ""
        if NAME in spans:
            start, end = spans[NAME]
            new_text = text[:start] + assignment + text[end:]
        elif value is None:
            return
        else:
            new_text = text + ("" if not text or text.endswith("\n") else "\n") + assignment + "\n"
        check, _ = read_saved(new_text)
        wanted = {key: item for key, item in saved.items() if key != NAME}
        if value is not None:
            wanted[NAME] = value
        if not same(check, wanted):
            return                        # never write what does not read back as meant
        temp = path.with_name(path.name + ".xpc-tmp")
        temp.write_text(new_text, encoding="utf-8", newline="\n")
        if self.running():                # the game started meanwhile: leave its file alone
            temp.unlink()
            return
        os.replace(temp, path)
        self.state["files"][str(path)] = [self._stat(path), digest]

    @staticmethod
    def _stat(path):
        try:
            stat = path.stat()
        except OSError:
            return None
        return [stat.st_mtime_ns, stat.st_size]
