"""Sending what the game recorded to the XPCompass server (server/README.md, "POST /upload").

The base addon (Record.lua) keeps its rows in XPCompassRecords, in the account's saved file
WTF/Account/<account>/SavedVariables/XPCompass.lua (the game names it after the addon's folder);
feedback items wait in each character's
ForeverCoachNextDB.feedback (WTF/Account/<account>/<realm>/<character>/SavedVariables/). WoW writes
those files at logout, /reload and exit. Every minute the app looks at their size and time, and a
file that changed is read again: its new rows go to the server. Only once the player has signed in
with Discord: there is no anonymous mode, and until then the rows wait in the game's file.

- **Who.** The app makes a random install id once (uploads.json in its data folder) and sends it
  with every upload, beside the Discord token. Each saved file gets its own random
  "slot" id. No account, realm or character name, and no path, ever leaves the computer.
- **Sharing.** Required to receive updates (DECISIONS.md), with no switch left in the game or the
  app. A player who turned it off before the switches went stays off: their saved file says so, or
  the app's settings do. Then nothing is sent, the server is told once (POST /sharing) so nothing
  is shared back, and the app installs no addon updates (opted_out, service.py).
- **Clearing.** Once the server says it holds the rows, they are removed from the game's file, but
  only while WoW is closed: the game keeps its own copy in memory and writes it back at logout, so
  anything cleared while it runs would come back. The file is rewritten whole (to a temporary file,
  read back, then swapped in), with only XPCompassRecords' rows changed; while WoW runs, the row
  numbers keep anything from being sent twice.
- **Failures.** Nothing is cleared until the server confirms; a failed upload is tried again at the
  next look.
"""
import gzip
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import auth, system

LOOK_EVERY = 60              # seconds between looks at the saved files
BATCH = 2000                 # rows per request
OPTED_OUT = "Sharing is now required to receive updates. See xpcompass.com/privacy."


def opted_out(settings, game):
    """Whether the player turned sharing off before it became required: in the app's settings, or in
    any WoW account's XPCompass saved file under this client (its "share", or "on" at the last login)."""
    if getattr(settings, "sharing", True) is False:
        return True
    for path in sorted((Path(game) / "WTF" / "Account").glob(f"*/SavedVariables/{ADDON_FILE}")):
        try:
            saved, _ = read_saved(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, LuaError):
            continue
        records = saved.get(RECORDS)
        if isinstance(records, dict) and (records.get("share") is False or records.get("on") is False):
            return True
    return False


# Only the current folder's file. The one from before the rename (ForeverCoachNext.lua) is never read
# or written: install.move_saved_settings copies it over XPCompass.lua when it is the newer of the two.
ADDON_FILE = "XPCompass.lua"
RECORDS = "XPCompassRecords"
CHARACTER_DB = "ForeverCoachNextDB"
VERSION = 1


def state_path():
    return system.data_dir() / "uploads.json"


# ---------------------------------------------------------------------------------------------
# Reading and writing WoW's saved files: a Lua subset (tables, strings, numbers, booleans, nil)
# ---------------------------------------------------------------------------------------------

class LuaError(ValueError):
    pass


TOKEN = re.compile(r"""
    (?P<space>\s+|--\[\[.*?\]\]|--[^\n]*)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<number>-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct>[{}\[\]=,;])
""", re.VERBOSE | re.DOTALL)
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "\n": "\n", "a": "\a", "b": "\b",
           "f": "\f", "v": "\v"}


def unquote(text):
    out, index, body = [], 0, text[1:-1]
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        nxt = body[index + 1] if index + 1 < len(body) else ""
        if nxt.isdigit():
            digits = re.match(r"\d{1,3}", body[index + 1:]).group()
            out.append(chr(int(digits)))
            index += 1 + len(digits)
        else:
            out.append(ESCAPES.get(nxt, nxt))
            index += 2
    return "".join(out)


class Reader:
    def __init__(self, text):
        self.text, self.at = text, 0
        self.tokens = []
        for match in TOKEN.finditer(text):
            if match.lastgroup != "space":
                self.tokens.append((match.lastgroup, match.group(), match.start(), match.end()))
        covered = sum(m.end() - m.start() for m in TOKEN.finditer(text))
        if covered != len(text):
            raise LuaError("the saved file has text this reader does not understand")
        self.index = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else (None, None, len(self.text), len(self.text))

    def take(self, value=None):
        token = self.peek()
        if token[0] is None or (value is not None and token[1] != value):
            raise LuaError(f"expected {value or 'more'} at character {token[2]}")
        self.index += 1
        return token

    def value(self):
        kind, text, _, _ = self.peek()
        if kind == "string":
            self.index += 1
            return unquote(text)
        if kind == "number":
            self.index += 1
            number = int(text, 16) if text.lower().startswith(("0x", "-0x")) else float(text)
            return int(number) if isinstance(number, float) and number.is_integer() and "." not in text \
                and "e" not in text.lower() else number
        if kind == "name" and text in ("true", "false", "nil"):
            self.index += 1
            return {"true": True, "false": False, "nil": None}[text]
        if text == "{":
            return self.table()
        raise LuaError(f"unexpected {text!r} at character {self.peek()[2]}")

    def table(self):
        self.take("{")
        array, mapping = [], {}
        while self.peek()[1] != "}":
            if self.peek()[1] == "[":
                self.take("[")
                key = self.value()
                self.take("]")
                self.take("=")
                mapping[key] = self.value()
            elif self.peek()[0] == "name" and self.index + 1 < len(self.tokens) and self.tokens[self.index + 1][1] == "=":
                key = self.take()[1]
                self.take("=")
                mapping[key] = self.value()
            else:
                array.append(self.value())
            if self.peek()[1] in (",", ";"):
                self.index += 1
        self.take("}")
        for index, item in enumerate(array, 1):
            mapping[index] = item
        keys = list(mapping)
        if keys and all(isinstance(k, int) and not isinstance(k, bool) for k in keys) \
                and sorted(keys) == list(range(1, len(keys) + 1)):
            return [mapping[k] for k in range(1, len(keys) + 1)]
        return {k: v for k, v in mapping.items() if v is not None} if keys else {}

    def assignments(self):
        """[(name, value, start, end)] for each `Name = value` at the top of the file."""
        found = []
        while self.peek()[0] is not None:
            kind, name, start, _ = self.take()
            if kind != "name":
                raise LuaError(f"expected a name at character {start}")
            self.take("=")
            value = self.value()
            end = self.tokens[self.index - 1][3]
            found.append((name, value, start, end))
        return found


def read_saved(text):
    """{name: value} of a saved file, and where each assignment sits in the text."""
    found = Reader(text).assignments()
    return {name: value for name, value, _, _ in found}, {name: (start, end) for name, _, start, end in found}


def lua_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def lua(value, indent=0):
    """A value as WoW writes it: one entry a line."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if not value.is_integer() else str(int(value))
    if isinstance(value, str):
        return lua_string(value)
    if isinstance(value, list):
        if not value:
            return "{\n}"
        return "{\n" + "".join(f"{lua(item, indent + 1)}, -- [{index}]\n" for index, item in enumerate(value, 1)) + "}"
    if isinstance(value, dict):
        lines = []
        for key in sorted(value, key=lambda k: (not isinstance(k, str), str(k))):
            shown = lua_string(key) if isinstance(key, str) else str(key)
            lines.append(f"[{shown}] = {lua(value[key], indent + 1)},\n")
        return "{\n" + "".join(lines) + "}"
    raise LuaError(f"cannot write {type(value).__name__}")


# ---------------------------------------------------------------------------------------------
# The uploader
# ---------------------------------------------------------------------------------------------

class Uploader:
    def __init__(self, settings, account=None, running=None, path=None, send=None, clock=time.time):
        self.settings, self.account = settings, account
        self.running = running or system.game_running
        # Beside the settings (a test's settings live in its own folder), else the app's data folder.
        self.path = path or (settings.path.parent / "uploads.json" if getattr(settings, "path", None) else state_path())
        self.send = send or self._post
        self.clock = clock
        self.state = self._load()
        self.last = None                 # {"at", "text"} for the window

    # The app's own record: the install id, each file's slot and what the server holds

    def _load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not re.match(r"^[0-9a-f]{32}$", str(state.get("install", ""))):
            state["install"] = secrets.token_hex(16)
        state.setdefault("files", {})
        state.setdefault("told_off", False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._save(state)
        return state

    def _save(self, state=None):
        state = state or self.state
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(temp, self.path)

    @property
    def install(self):
        return self.state["install"]

    def file_state(self, path):
        files = self.state["files"]
        key = str(path)
        if key not in files:
            files[key] = {"slot": secrets.token_urlsafe(12), "upto": 0, "feedback": {}, "seen": None, "clear": False}
        return files[key]

    # Where the saved files are

    def saved_files(self, game):
        """(account files, character files) under the client's WTF folder."""
        accounts = Path(game) / "WTF" / "Account"
        if not accounts.is_dir():
            return [], []
        own = sorted(accounts.glob(f"*/SavedVariables/{ADDON_FILE}"))
        chars = sorted(accounts.glob(f"*/*/*/SavedVariables/{ADDON_FILE}"))
        return own, chars

    # One look

    def sharing(self):
        return getattr(self.settings, "sharing", True) is not False

    def look(self, game, game_id="forever"):
        """Look at one client's saved files once; send what is new, tagged with the game (tools/game.py's
        id, "forever" or "era"); clear what the server holds while the game is closed. Returns how many
        rows the server took this time."""
        if self.account and not self.account.session():
            self.last = {"at": self.clock(), "text": auth.SIGN_IN_FIRST}
            return 0
        own, chars = self.saved_files(game)
        running = self.running()
        sent = 0
        for path in own:
            sent += self._account_file(path, running, chars_for(path, chars), game_id)
        self._save()
        return sent

    def _account_file(self, path, running, char_files, game_id="forever"):
        mine = self.file_state(path)
        try:
            stat = path.stat()
        except OSError:
            return 0
        seen = [stat.st_mtime_ns, stat.st_size]
        changed = seen != mine["seen"]
        char_changed = any(self._changed(c) for c in char_files)
        if not changed and not char_changed and not (mine["clear"] and not running):
            return 0
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            saved, spans = read_saved(text)
        except LuaError as problem:
            self.last = {"at": self.clock(), "text": f"Could not read a saved file: {problem}"}
            mine["seen"] = seen
            return 0
        records = saved.get(RECORDS)
        records = records if isinstance(records, dict) else {}
        game_says = records.get("share")
        if game_says is False or not self.sharing():
            if not self.state["told_off"]:
                self._tell_off()
            mine["seen"] = seen
            for char in char_files:
                self.file_state(char)["seen"] = self._stat(char)
            return 0
        self.state["told_off"] = False
        rows = [r for r in (records.get("rows") or []) if isinstance(r, dict) and isinstance(r.get("id"), int)]
        new = [r for r in rows if r["id"] > mine["upto"]]
        sent = 0
        chars = records.get("chars") if isinstance(records.get("chars"), list) else []
        try:
            for start in range(0, len(new), BATCH):
                answer = self._upload(mine["slot"], {"rows": new[start:start + BATCH], "chars": chars}, game_id)
                mine["upto"] = max(mine["upto"], answer.get("upto", 0))
                sent += answer.get("stored", 0)
            for char, feedback, char_seen in self._feedback(char_files):
                answer = self._upload(char["slot"], {"feedback": feedback}, game_id)
                before = char["feedback"].get("upto", 0) if char["feedback"].get("epoch") == feedback["epoch"] else 0
                char["feedback"] = {"epoch": feedback["epoch"], "upto": max(before, answer.get("feedback_upto", 0))}
                char["seen"] = char_seen
        except UploadError as failure:
            self.last = {"at": self.clock(), "text": f"Upload failed, will try again: {failure}"}
            return sent
        held = [r for r in rows if r["id"] <= mine["upto"]]
        if held and running:
            mine["clear"] = True            # cleared once the game has closed
        elif held:
            if self._clear(path, text, saved, spans, mine["upto"]):
                mine["clear"] = False
                stat = path.stat()
                seen = [stat.st_mtime_ns, stat.st_size]
        mine["seen"] = seen
        if sent or new:
            self.last = {"at": self.clock(), "text": f"Sent {len(new)} new rows."}
        return sent

    def _stat(self, path):
        try:
            stat = path.stat()
        except OSError:
            return None
        return [stat.st_mtime_ns, stat.st_size]

    def _changed(self, path):
        return self._stat(path) != self.file_state(path)["seen"]

    def _feedback(self, char_files):
        """[(file state, {epoch, items}, its size and time)] for each character file that changed and
        holds feedback items the server lacks. A changed file with none is marked seen here."""
        out = []
        for path in char_files:
            mine, seen = self.file_state(path), self._stat(path)
            if seen == mine["seen"]:
                continue
            try:
                saved, _ = read_saved(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, LuaError):
                saved = {}
            db = saved.get(CHARACTER_DB) if isinstance(saved.get(CHARACTER_DB), dict) else {}
            store = db.get("feedback") if isinstance(db.get("feedback"), dict) else {}
            epoch, items = store.get("epoch"), store.get("items") or []
            upto = mine["feedback"].get("upto", 0) if mine["feedback"].get("epoch") == epoch else 0
            fresh = [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), int) and i["id"] > upto]                 if isinstance(epoch, str) and isinstance(items, list) else []
            if fresh:
                out.append((mine, {"epoch": epoch, "items": fresh[:1000]}, seen))
            else:
                mine["seen"] = seen
        return out

    def _clear(self, path, text, saved, spans, upto):
        """Remove the rows the server holds from the game's file. Only while the game is closed."""
        records = dict(saved[RECORDS])
        records["rows"] = [r for r in records.get("rows") or [] if not (isinstance(r, dict) and isinstance(r.get("id"), int)
                                                                         and r["id"] <= upto)]
        start, end = spans[RECORDS]
        new_text = text[:start] + f"{RECORDS} = " + lua(records) + text[end:]
        check, _ = read_saved(new_text)
        if not same(check, dict(saved, **{RECORDS: records})):
            return False                    # never write what does not read back as meant
        temp = path.with_name(path.name + ".xpc-tmp")
        temp.write_text(new_text, encoding="utf-8", newline="\n")
        if self.running():                  # the game started meanwhile: leave its file alone
            temp.unlink()
            return False
        os.replace(temp, path)
        return True

    # Talking to the server

    def _token(self):
        if not self.account or not self.account.session():
            return None
        try:
            return self.account.token()
        except auth.AuthError:
            return None

    def signed_token(self):
        """The token to send with, or UploadError while nobody is signed in: nothing goes anonymously."""
        token = self._token()
        if self.account and not token:
            raise UploadError(auth.SIGN_IN_FIRST)
        return token

    def _upload(self, slot, body, game="forever"):
        payload = {"v": VERSION, "game": game, "slot": slot, "chars": body.get("chars", []), "rows": body.get("rows", [])}
        if body.get("feedback"):
            payload["feedback"] = body["feedback"]
        data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        token = self.signed_token()
        status, answer = self.send("/upload", data, token, gz=True)
        if status == 401 and token and self.account:
            try:
                token = self.account.refresh()["token"]
            except auth.AuthError as error:     # signed out meanwhile: the rows wait for the next sign-in
                raise UploadError(auth.SIGN_IN_FIRST) from error
            status, answer = self.send("/upload", data, token, gz=True)
        if status != 200:
            raise UploadError(f"{status} {answer.get('error', '')}".strip())
        return answer

    def _tell_off(self):
        try:
            status, _ = self.send("/sharing", b'{"on": false}', self.signed_token())
        except UploadError:
            return
        if status == 204:
            self.state["told_off"] = True

    def _post(self, path, data, token, gz=False):
        server = self.settings.server
        headers = {"X-XPC-Install": self.install, "Content-Type": "application/json",
                   "User-Agent": "ForeverCoachCompanion"}
        if gz:
            headers["Content-Encoding"] = "gzip"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(server + path, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, raw = response.status, response.read(100_000)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(100_000)
        except OSError as error:
            raise UploadError(f"could not reach the XPCompass server: {error}") from error
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {}


class UploadError(Exception):
    pass


def same(a, b):
    """Equal as Lua sees it: an empty table is an empty table, list or not."""
    if isinstance(a, (dict, list)) and isinstance(b, (dict, list)) and not a and not b:
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def chars_for(account_file, char_files):
    """The character files under the same WoW account as `account_file`."""
    account = account_file.parent.parent
    return [c for c in char_files if c.parent.parent.parent.parent == account]
