"""The server's list: what the app collects, carries between a player's PCs and brings back.

The owner, 18 November: "the app is about protecting the addon and sending and providing information and updates
shouldnt our server be the dictator of what it sends and what it recieves and the addon the writer?" So the app is a
courier. It reads exactly what the list names, sends it, and puts what comes back where the addon picks it up and
merges it under its own rules. A new table that should follow a player is a line in the list and code in the addon,
never an app release.

- **Signed offline.** The list is a file in the repository (server/sync-list.json), signed on the PC with the
  release key (tools/sync_list.py), the key that signs the app and the addons and never sits on the server. The
  server only serves the file and its signature (GET /sync/list). What is signed is SIGNED_AS and then the file's
  exact bytes, so a list's signature is never a build's, nor a build's a list's.
- **Checked whole.** A list is taken only when a pinned key signed it, it reads as a list of this format, every
  value in it is inside the ceiling below, and its version is no lower than the last one taken (a stolen server
  can't bring back an older, wider list). Anything else, and the list is refused whole, never partly applied: the
  app keeps the last good one, kept beside its settings and checked again each time it is read; with none, the one
  built in (builtin_list.py, today's behaviour).
- **The ceiling.** Whatever a list says, and whoever signed it, the app reads and writes only what listed.py names:
  the XPCompass addons' own saved files, tables in XPCompass's own names, and the game's logs the way the log
  readers already read them. A list can narrow any of it, switch a job off or slow it down. It can never reach
  another addon's file, the game's settings, macros or key bindings, chat, or the sign-in. The app writes only its
  own inbox tables: never a table the addon owns.
"""
import base64
import json
import os
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import builtin_list, listed, release

SIGNED_AS = b"xpcompass sync list v1\n"
FILE = "sync-list.json"
MAX = 64 * 1024              # bytes of a list
ASK_EVERY = 15 * 60          # seconds between asking the server for its list

# The ceiling itself is listed.py's, which the server holds a copy of: SAVED_FILES (the only files read), TABLE
# (tables of ours only), INBOX (the only tables written) and JOBS (each job's widest detail).
ListError, check = listed.ListError, listed.check
SAVED_FILES, INBOX, INBOX_FILE, JOBS = listed.SAVED_FILES, listed.INBOX, listed.INBOX_FILE, listed.JOBS


# ---------------------------------------------------------------------------------------------
# Signed, and never older than the last one taken
# ---------------------------------------------------------------------------------------------

def signed_by(body, signature, keys):
    """Whether one of `keys` signed this list: SIGNED_AS and then its exact bytes."""
    try:
        sig = base64.b64decode((signature or "").strip(), validate=True)
    except (ValueError, AttributeError):
        return False
    for key in keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True)).verify(sig, SIGNED_AS + body)
            return True
        except Exception:   # noqa: BLE001 - a bad key and a bad signature both mean "not this key"
            continue
    return False


def _no_constants(name):
    raise ListError(f"{name} is not a number a list holds")


def verify(body, signature, keys=None, least=0):
    """The checked list in the signed bytes `body`, or ListError saying why it is not taken."""
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX:
        raise ListError("the list is too large")
    body = bytes(body)
    if not signed_by(body, signature, release.TRUSTED_KEYS if keys is None else keys):
        raise ListError("the list's signature does not check out")
    try:
        raw = json.loads(body.decode("utf-8"), parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ListError(f"the list is not JSON: {error}"[:200]) from error
    found = check(raw)
    if found["version"] < least:
        raise ListError("the list is older than the one held")
    return found


def builtin():
    """The list built into this app: what it does with no list from the server."""
    return check(json.loads(builtin_list.TEXT))


class Lists:
    """The list in force for one app: the newest good one taken from the server, kept in `path` and checked again
    whenever it is read; before any, the built-in one."""

    def __init__(self, path=None, keys=None, clock=time.time):
        self.path, self.keys, self.clock = Path(path) if path else None, keys, clock
        self.lock = threading.Lock()
        self.base = builtin()
        self.current, self.source, self.asked, self.refused = self.base, "builtin", 0, None
        self._load()

    def _load(self):
        if not self.path:
            return
        try:
            kept = json.loads(self.path.read_text(encoding="utf-8"))
            self.current = verify(kept["body"].encode("utf-8"), kept["signature"], self.keys, self.base["version"])
            self.source = "server"
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass                    # none kept, or one that no longer checks out: the built-in list

    def take(self, body, signature):
        """Take the server's answer if it is a good list no older than the one in force. Returns the list in force;
        a refusal leaves it as it was and is said in `refused`."""
        with self.lock:
            try:
                found = verify(body, signature, self.keys, self.current["version"])
            except ListError as error:
                self.refused = str(error)
                return self.current
            if found["version"] == self.current["version"] and found != self.current:
                self.refused = "the list changed without a new version"
                return self.current
            self.refused = None
            if found != self.current or self.source != "server":
                self.current, self.source = found, "server"
                self._keep(body, signature)
            return self.current

    def _keep(self, body, signature):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_text(json.dumps({"body": bytes(body).decode("utf-8"), "signature": signature}), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            pass                    # held for this run; asked for again at the next start

    def refresh(self, fetch, force=False):
        """Ask the server for its list when it is time: fetch() gives (status, bytes of {"body", "signature"}). A
        server that can't be reached, or answers anything but a good list, changes nothing."""
        now = self.clock()
        if not force and now < self.asked + ASK_EVERY:
            return self.current
        self.asked = now
        try:
            status, raw = fetch()
            if status != 200 or not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX * 2:
                raise ListError(f"the server answered {status}")
            answer = json.loads(bytes(raw).decode("utf-8"))
            body, signature = answer["body"], answer["signature"]
            if not isinstance(body, str) or not isinstance(signature, str):
                raise ListError("the answer is not a signed list")
        except ListError as error:
            self.refused = str(error)
            return self.current
        except Exception as error:   # noqa: BLE001 - whatever the server sent or didn't: the list in force stays
            self.refused = f"the list could not be fetched: {type(error).__name__}"
            return self.current
        return self.take(body.encode("utf-8"), signature)


# ---------------------------------------------------------------------------------------------
# The one list the app's jobs go by
# ---------------------------------------------------------------------------------------------

_active = {"lists": None, "ran": {}}
_lock = threading.Lock()


def use(lists):
    """Make `lists` (a Lists, or None for a fresh built-in one) the app's; returns it."""
    with _lock:
        _active["lists"] = lists if lists is not None else Lists()
        _active["ran"] = {}
        return _active["lists"]


def active():
    with _lock:
        if _active["lists"] is None:
            _active["lists"] = Lists()
        return _active["lists"]


def current():
    return active().current


def job(name):
    """What the list in force says of one job of the minute loop."""
    return current()["jobs"][name]


def asked():
    """Check now: every job is due at its next turn, whatever its pace."""
    with _lock:
        _active["ran"] = {}


def due(name, key="", now=None):
    """Whether a job should run now: it is on, and its `every` has passed since it last ran for `key` (a game)."""
    entry = job(name)
    if not entry["on"]:
        return False
    now = time.time() if now is None else now
    with _lock:
        last = _active["ran"].get((name, key))
        if last is not None and now - last < entry.get("every", 0) - 1:
            return False
        _active["ran"][(name, key)] = now
        return True
