"""What a list may say, and what data fits one: the same code in the app and on the server.

This file is kept byte for byte the same as companion/forever_companion/listed.py and server/forever_server/listed.py
(tests/test_sync_list.py holds them equal), so the app's ceiling and the server's check of what it is sent are one
rule, not two that drift. It imports nothing of either and holds no table's name: every name comes from the list.

- **The ceiling** (`check`): the widest any list may be, exactly what the app read and sent on 18 November 2026. The
  XPCompass addons' own saved files, tables in XPCompass's own names, the jobs of the minute loop with the fields
  each already sent. A list that steps outside it anywhere is refused whole.
- **Plain data** (`plain`, `plain_ok`): numbers, true and false, short text and tables of them, to a depth and a
  count. Nothing else is ever sent, stored or written into a saved file.
- **A copy** (`problem`): one PC's tables as the list names them. Every value in it sits under a path the list
  takes, within the list's limits. Anything not on the list is refused.
"""
import math
import re

FORMAT = 1
KIND = "sync-list"

# The saved files the app may read, by the addon folder the game names each after. All under
# WTF/Account/<account>/SavedVariables or WTF/Account/<account>/<realm>/<character>/SavedVariables, nowhere else.
SAVED_FILES = ("XPCompass.lua", "XPCompass_Journey.lua", "XPCompass_Mail.lua", "XPCompass_Market.lua")
WHERE = ("account", "character")
# The only tables the app writes for the addon to merge: its inbox, one for the account and one a character.
INBOX_FILE = "XPCompass.lua"
INBOX = {"account": "XPCompassInbox", "character": "XPCompassCharInbox"}
# The two other things the app writes in a saved file, as it did before the list, and nothing a list can rename: the
# recorder's rows the server holds are taken out of the outbox (nothing else in it changes), and other players' level
# times are put where XPC - Journey reads them. The outbox also says whether the player shares at all (SHARING: a
# player who turned sharing off before it became required).
OUTBOX = ("XPCompass.lua", "XPCompassRecords")
SHARING = ("share", "on")
DELIVERED = ("XPCompass_Journey.lua", "XPCompassLevelMedians")
WRITTEN = (*INBOX.values(), OUTBOX[1], DELIVERED[1])
MODES = ("newest", "both", "upload", "never")
TRAVELS = ("newest", "both")             # the modes whose data goes back to the player's other PCs
TRIMS = ("newest", "last", "most")

TABLE = re.compile(r"(?:XPCompass|ForeverCoach)[A-Za-z0-9_]{0,40}")      # a table of ours, never another addon's
SEGMENT = re.compile(r"\*|[A-Za-z0-9_#]{1,48}")
ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
MOST_ENTRIES = 64
MOST_PATHS = 96
MOST_DEEP = 6                # segments in a path
MOST_LEVELS = 8              # steps a trim tightens by when a copy won't fit
CAPS = {"unpacked": (10_000, 3_000_000), "packed": (10_000, 900_000), "text": (1, 200), "depth": (1, 8),
        "characters": (0, 60), "kept": (0, 8)}
MOST_VALUES = 400_000
MOST_BACK = 3_000_000        # values in one of the server's own entries: its bytes are what bound it
MOST_ITEMS = 5000            # entries of one table
NUMBER = 10 ** 13
EARLIEST = 1_700_000_000     # older than any build that records

# The jobs of the minute loop, each with everything a list may set and how far. A job's detail can be narrowed,
# never widened past what the app sent on 18 November: the fields are the ones it sent then.
EVERY = (60, 86400)
SLOW = (300, 86400)
CRASH_FIELDS = ("cur", "fn", "object", "desc", "ver", "loaded")
FIGHT_FIELDS = ("dmg", "pet", "taken", "idle", "kills", "died", "npcs", "spells", "lvl", "cls")
SCAN_FIELDS = ("house", "realm", "faction", "ah", "map", "t", "api", "listings", "bids", "seconds", "items")
JOBS = {
    "records": {"on": bool, "every": EVERY, "batch": (1, 2000)},
    "feedback": {"on": bool, "file": "file", "table": "table", "path": "segment", "most": (0, 1000)},
    "logs": {"on": bool, "every": EVERY, "load": bool, "crash": bool, "read": (0, 2_000_000), "fields": CRASH_FIELDS},
    "combat": {"on": bool, "every": EVERY, "settle": SLOW, "read": (0, 16_000_000), "spells": (0, 30), "npcs": (0, 10),
               "copy": bool, "file": "file", "table": "table", "fields": FIGHT_FIELDS},
    "market": {"on": bool, "every": EVERY, "file": "file", "table": "table", "path": "segment", "fields": SCAN_FIELDS},
    "medians": {"on": bool, "every": SLOW},
    "data": {"on": bool, "every": SLOW},
    "linking": {"on": bool},
    "sync": {"on": bool, "every": SLOW, "gap": EVERY},
}

# A character's folders, <realm>/<character>, as WoW names them: never a path of any other kind.
CHARACTER = re.compile(r'^[^\x00-\x1f/\\:*?"<>|. ][^\x00-\x1f/\\:*?"<>|]{0,59}/[^\x00-\x1f/\\:*?"<>|. ][^\x00-\x1f/\\:*?"<>|]{0,59}$')
KEY_TEXT = re.compile(r"^[^\x00-\x1f]{1,80}$")
WHOLE_KEY = re.compile(r"^-?\d{1,15}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ListError(ValueError):
    """A list that is not taken; the message says why."""


def whole(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _need(ok, why):
    if not ok:
        raise ListError(why)


# ---------------------------------------------------------------------------------------------
# The list, held to the ceiling
# ---------------------------------------------------------------------------------------------

def path_of(text, what="a path"):
    """A path's segments: names between dots, `*` for any one key. "" is the table itself."""
    _need(isinstance(text, str) and len(text) <= 200, f"{what} is not text")
    if text == "":
        return ()
    parts = tuple(text.split("."))
    _need(len(parts) <= MOST_DEEP and all(SEGMENT.fullmatch(part) for part in parts), f"{what} is not a path: {text!r}"[:120])
    return parts


def _keys(entry, needed, optional=()):
    _need(isinstance(entry, dict), "an entry is not an object")
    have = set(entry)
    _need(have >= set(needed), f"an entry lacks {sorted(set(needed) - have)}")
    extra = have - set(needed) - set(optional)
    _need(not extra, f"an entry holds {sorted(map(str, extra))[:4]}, which is outside what the app does")


def _file(value):
    _need(isinstance(value, str) and value in SAVED_FILES, f"{str(value)[:60]!r} is not one of XPCompass's saved files")
    return value


def _table(value):
    _need(isinstance(value, str) and TABLE.fullmatch(value), f"{str(value)[:60]!r} is not a table of XPCompass's")
    _need(value not in INBOX.values() and value != DELIVERED[1], f"{value} is the app's own to write, never to collect")
    return value


def _place(entry, more=()):
    """{file, table, ...}: a table in one of our saved files."""
    _keys(entry, ("file", "table", *more))
    return {"file": _file(entry["file"]), "table": _table(entry["table"])}


def _sync_entry(entry):
    _keys(entry, ("id", "file", "per", "table", "mode", "take"), ("trim", "units", "history", "values", "depth"))
    out = {"id": entry["id"], "file": _file(entry["file"]), "per": entry["per"], "table": _table(entry["table"]), "mode": entry["mode"]}
    _need(isinstance(out["id"], str) and ID.fullmatch(out["id"]), "an entry's id is not a name")
    _need(out["per"] in WHERE, "an entry is per account or per character")
    _need(isinstance(out["mode"], str) and out["mode"] in MODES, f"{out['id']}: a mode is one of {MODES}")
    take = entry["take"]
    _need(isinstance(take, list) and 0 < len(take) <= MOST_PATHS, f"{out['id']}: take is a list of paths")
    out["take"] = [path_of(path) for path in take]
    _need(all(out["take"]), f"{out['id']}: a whole table is never taken; name its fields")
    out["trim"] = []
    trims = entry.get("trim", [])
    _need(isinstance(trims, list) and len(trims) <= MOST_PATHS, f"{out['id']}: trim is a list")
    for trim in trims:
        _need(isinstance(trim, dict) and len(trim) == 2 and "path" in trim, f"{out['id']}: a trim is a path and one limit")
        kind = next(key for key in trim if key != "path")
        levels = trim[kind]
        _need(kind in TRIMS and isinstance(levels, list) and 0 < len(levels) <= MOST_LEVELS
              and all(whole(level, 0, 100_000) for level in levels), f"{out['id']}: a trim's limit is a list of numbers")
        _need(all(a >= b for a, b in zip(levels, levels[1:])), f"{out['id']}: a trim only tightens")
        out["trim"].append({"path": path_of(trim["path"]), "kind": kind, "levels": list(levels)})
    out["units"] = path_of(entry["units"]) if "units" in entry else None
    out["history"] = entry.get("history")
    _need(out["history"] is None or (isinstance(out["history"], str) and SEGMENT.fullmatch(out["history"]) and out["history"] != "*"),
          f"{out['id']}: history is a field's name")
    out["values"] = entry.get("values", 40_000)
    _need(whole(out["values"], 1, MOST_VALUES), f"{out['id']}: values is a count")
    out["depth"] = entry.get("depth", CAPS["depth"][1])
    _need(whole(out["depth"], *CAPS["depth"]), f"{out['id']}: depth is 1 to {CAPS['depth'][1]}")
    return out


def _job(name, entry):
    shape = JOBS[name]
    _keys(entry, shape)
    out = {}
    for key, rule in shape.items():
        value = entry[key]
        if rule is bool:
            _need(isinstance(value, bool), f"{name}.{key} is true or false")
        elif rule == "file":
            _file(value)
        elif rule == "table":
            _table(value)
        elif rule == "segment":
            _need(isinstance(value, str) and SEGMENT.fullmatch(value) and value != "*", f"{name}.{key} is a field's name")
        elif isinstance(rule[0], str):      # a choice of fields: only ones the app already sends, each once
            _need(isinstance(value, list) and all(isinstance(field, str) and field in rule for field in value)
                  and len(set(value)) == len(value), f"{name}.{key} names a field this app never sends")
            value = list(value)
        else:
            _need(whole(value, *rule), f"{name}.{key} is outside {rule[0]} to {rule[1]}")
        out[key] = value
    return out


def check(raw):
    """The list in `raw` as it is used, every value checked against the ceiling, or ListError: a list is taken whole
    or not at all."""
    _keys(raw, ("format", "kind", "version", "time", "caps", "switch", "sync", "back", "jobs"), ("about",))
    _need(raw["format"] == FORMAT and raw["kind"] == KIND, "not a list this app reads")
    _need(whole(raw["version"], 1, 2 ** 53) and whole(raw["time"], 0, 2 ** 40), "a list has a version and a time")
    _need("about" not in raw or (isinstance(raw["about"], str) and len(raw["about"]) <= 400), "about is a line of text")
    caps = raw["caps"]
    _keys(caps, CAPS)
    for name, (low, high) in CAPS.items():
        _need(whole(caps[name], low, high), f"caps.{name} is outside {low} to {high}")
    out = {"version": raw["version"], "time": raw["time"], "caps": dict(caps)}
    out["switch"] = dict(_place(raw["switch"], ("path",)), path=path_of(raw["switch"]["path"]))
    _need(out["switch"]["path"] and "*" not in out["switch"]["path"], "the switch is one named value")
    entries = raw["sync"]
    _need(isinstance(entries, list) and len(entries) <= MOST_ENTRIES, "sync is a list of entries")
    out["sync"] = [_sync_entry(entry) for entry in entries]
    ids = [entry["id"] for entry in out["sync"]]
    _need(len(set(ids)) == len(ids), "two entries share an id")
    back = raw["back"]
    _need(isinstance(back, list) and len(back) <= 8, "back is a few entries of the server's own")
    out["back"] = []
    for entry in back:
        _keys(entry, ("id", "per"))
        _need(isinstance(entry["id"], str) and ID.fullmatch(entry["id"]) and entry["per"] in WHERE, "a back entry is an id, per account or character")
        out["back"].append({"id": entry["id"], "per": entry["per"]})
    names = ids + [entry["id"] for entry in out["back"]]
    _need(len(set(names)) == len(names), "two entries share an id")
    jobs = raw["jobs"]
    _need(isinstance(jobs, dict) and set(jobs) == set(JOBS), "jobs names each job once and no other")
    out["jobs"] = {name: _job(name, jobs[name]) for name in JOBS}
    return out


def entries_of(listing):
    """{id: entry} of what a list has travel: its sync entries, and the server's own (`back`), which only come."""
    out = {entry["id"]: entry for entry in listing["sync"]}
    for entry in listing["back"]:
        out[entry["id"]] = {"id": entry["id"], "per": entry["per"], "mode": "back"}
    return out


# ---------------------------------------------------------------------------------------------
# Plain data, and its keys
# ---------------------------------------------------------------------------------------------

def encode_key(key):
    """A saved table's key as a copy holds it, or None for one a copy can't hold. A number stays its digits; a name
    that reads as a number, or starts with "~", gets "~" before it, so each comes back as what it was."""
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return str(key) if abs(key) < 10 ** 15 else None
    if not isinstance(key, str) or not KEY_TEXT.match(key) or len(key) > 79:
        return None
    return "~" + key if key.startswith("~") or WHOLE_KEY.match(key) else key


def decode_key(text):
    if text.startswith("~"):
        return text[1:]
    return int(text) if WHOLE_KEY.match(text) else text


def number_ok(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and abs(value) < NUMBER


def plain(value, depth, text, left):
    """Saved data as a copy holds it, or None for what a copy can't hold: keys as text, fractions to three places,
    text without control characters and cut to `text`, tables to `depth`. `left` is [how many values may still come]:
    past it the rest is left out."""
    left[0] -= 1
    if left[0] < 0:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if not number_ok(value):
            return None
        return int(value) if float(value).is_integer() else round(value, 3)
    if isinstance(value, str):
        return CONTROL.sub(" ", value)[:text]
    if depth <= 0:
        return None
    if isinstance(value, list):
        if len(value) > MOST_ITEMS:
            return None
        items = [plain(item, depth - 1, text, left) for item in value]
        if all(item is not None for item in items):
            return items
        return {str(index): item for index, item in enumerate(items, 1) if item is not None}
    if isinstance(value, dict):
        out = {}
        for key in sorted(value, key=lambda key: (isinstance(key, str), str(key))):
            name = encode_key(key)
            if name is None or len(out) >= MOST_ITEMS:
                continue
            item = plain(value[key], depth - 1, text, left)
            if item is not None:
                out[name] = item
        return out
    return None


def plain_ok(value, depth, text, left):
    """Whether `value` is plain data as `plain` makes it: the check of anything that arrives."""
    left[0] -= 1
    if left[0] < 0:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return number_ok(value)
    if isinstance(value, str):
        return len(value) <= text and not CONTROL.search(value)
    if depth <= 0:
        return False
    if isinstance(value, list):
        return len(value) <= MOST_ITEMS and all(plain_ok(item, depth - 1, text, left) for item in value)
    if isinstance(value, dict):
        return len(value) <= MOST_ITEMS and all(
            isinstance(key, str) and encode_key(decode_key(key)) == key and plain_ok(item, depth - 1, text, left)
            for key, item in value.items())
    return False


def back(value):
    """Copy data as the game keeps it: each key what it was, and keys 1 to n a list, as a saved file reads back."""
    if isinstance(value, dict):
        out = {decode_key(key): back(item) for key, item in value.items()}
        keys = list(out)
        if keys and all(isinstance(key, int) for key in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
            return [out[key] for key in range(1, len(keys) + 1)]
        return out
    if isinstance(value, list):
        return [back(item) for item in value]
    return value


# ---------------------------------------------------------------------------------------------
# What a list takes of a table
# ---------------------------------------------------------------------------------------------

def as_map(value):
    """A table read as a list (its keys 1 to n) or as a map, as a map; None for anything else."""
    if isinstance(value, list):
        return dict(enumerate(value, 1))
    return value if isinstance(value, dict) else None


def select(node, patterns):
    """The parts of a saved table the paths in `patterns` name, or None when it holds none of them. A path's end takes
    all that is under it."""
    if any(not pattern for pattern in patterns):
        return node
    table = as_map(node)
    if table is None:
        return None
    out = {}
    for key, child in table.items():
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            continue
        under = [pattern[1:] for pattern in patterns if pattern[0] == "*" or pattern[0] == str(key)]
        if under:
            found = select(child, under)
            if found is not None:
                out[key] = found
    return out or None


def at(node, path):
    """[(parent, key)] of everything in a table at `path`, reached through tables only: a list is taken whole."""
    found = [(None, None, node)]
    for segment in path:
        nxt = []
        for _, _, value in found:
            if isinstance(value, dict):
                nxt.extend((value, key, child) for key, child in value.items() if segment == "*" or segment == str(key))
        found = nxt
    return [(parent, key) for parent, key, _ in found if parent is not None]


def _tail(value, most):
    """The newest `most` of a table: a list's last ones, a map's highest numbered keys."""
    if isinstance(value, list):
        return value[len(value) - most:] if most else []
    if isinstance(value, dict):
        numbered = sorted(key for key in value if isinstance(key, int) and not isinstance(key, bool))
        return {key: value[key] for key in (numbered[len(numbered) - most:] if most else [])}
    return value


def _first(value, most):
    if isinstance(value, list):
        return value[:most]
    if isinstance(value, dict):
        return {key: value[key] for key in sorted(value, key=lambda key: (isinstance(key, str), str(key)))[:most]}
    return value


def trimmed(table, entry, level=0):
    """`table` (as `select` gave it; changed in place) within the entry's limits at this `level` of tightness."""
    for trim in entry["trim"]:
        most = trim["levels"][min(level, len(trim["levels"]) - 1)]
        cut = _first if trim["kind"] == "most" else _tail
        if not trim["path"]:
            kept = cut(table, most)
            for key in [key for key in table if key not in kept]:
                del table[key]
        for parent, key in at(table, trim["path"]):
            parent[key] = cut(parent[key], most)
    return table


def taken(table, entry, text, level=0):
    """What the list's `entry` takes of one saved table, as plain data, or None for nothing."""
    found = select(table, entry["take"])
    if not isinstance(found, dict):
        return None
    found = _copied(found)
    out = plain(trimmed(found, entry, level), entry["depth"], text, [entry["values"]])
    return out or None


def _copied(value):
    if isinstance(value, dict):
        return {key: _copied(item) for key, item in value.items()}
    return value        # a list, or a value: taken whole, never changed in place


# ---------------------------------------------------------------------------------------------
# A copy: what one PC sends, and what comes back to another
# ---------------------------------------------------------------------------------------------

def _within(node, patterns, depth, text, left):
    """Whether everything in `node` sits under a path the entry takes, and is plain, tables at most `depth` deep."""
    if any(not pattern for pattern in patterns):
        return plain_ok(node, depth, text, left)
    left[0] -= 1
    if depth <= 0 or left[0] < 0 or not isinstance(node, dict) or len(node) > MOST_ITEMS:
        return False
    for key, child in node.items():
        if not isinstance(key, str) or encode_key(decode_key(key)) != key:
            return False
        name = str(decode_key(key))
        under = [pattern[1:] for pattern in patterns if pattern[0] == "*" or pattern[0] == name]
        if not under or not _within(child, under, depth - 1, text, left):
            return False
    return True


def _sized(node, entry):
    for trim in entry["trim"]:
        found = [node] if not trim["path"] else [parent[key] for parent, key in at(node, tuple(trim["path"]))]
        if any(isinstance(value, (list, dict)) and len(value) > trim["levels"][0] for value in found):
            return False
    return True


def _table_problem(data, entry, text):
    if not isinstance(data, dict) or not data:
        return f"{entry['id']} is not a table"
    if entry["mode"] == "back":
        return None if plain_ok(data, CAPS["depth"][1], text, [MOST_BACK]) else f"{entry['id']} is not plain data"
    if not _within(data, entry["take"], entry["depth"], text, [entry["values"]]):
        return f"{entry['id']} holds something the list doesn't take"
    if not _sized(_decoded_paths(data), entry):
        return f"{entry['id']} holds more than the list takes"
    return None


def _decoded_paths(node):
    """A copy's table with its keys as they were, for finding what sits at a path."""
    if isinstance(node, dict):
        return {decode_key(key): _decoded_paths(item) for key, item in node.items()}
    return node


def tables_problem(listing, tables, modes):
    """Why `tables` ({id: data}) isn't what this list has travel in one of `modes`, or None."""
    entries, caps = entries_of(listing), listing["caps"]
    if not isinstance(tables, dict) or len(tables) > MOST_ENTRIES + 8:
        return "not tables"
    for name, data in tables.items():
        entry = entries.get(name) if isinstance(name, str) else None
        if entry is None or entry["mode"] not in modes:
            return f"{str(name)[:40]} is not on the list"
        if entry["per"] == "character":
            if not isinstance(data, dict) or not data or len(data) > caps["characters"]:
                return f"{name} is not by character"
            for key, one in data.items():
                if not (isinstance(key, str) and CHARACTER.match(key)):
                    return f"{name} has a character that isn't one"
                why = _table_problem(one, entry, caps["text"])
                if why:
                    return why
        else:
            why = _table_problem(data, entry, caps["text"])
            if why:
                return why
    return None


def problem(listing, copy, modes=("newest", "both", "upload")):
    """Why `copy` ({made, tables, kept}) can't be one an app made under this list, or None. `modes`: what may be in it;
    for a copy on its way to another PC, the modes that go back and the server's own."""
    if not isinstance(copy, dict) or set(copy) - {"made", "tables", "kept"}:
        return "not a copy"
    if not whole(copy.get("made"), EARLIEST, 2 ** 33):
        return "bad time"
    why = tables_problem(listing, copy.get("tables", {}), modes)
    if why:
        return why
    kept = copy.get("kept", [])
    if not isinstance(kept, list) or len(kept) > listing["caps"]["kept"]:
        return "too many backups"
    for tables in kept:
        if not isinstance(tables, dict) or not tables:
            return "not a backup"
        why = tables_problem(listing, tables, modes)
        if why:
            return why
    return None


# ---------------------------------------------------------------------------------------------
# A copy made under one list, held to another
# ---------------------------------------------------------------------------------------------

def pruned(node, patterns):
    """A copy's table with only what the paths in `patterns` name, or None when none of it is on the list: what a
    narrower list leaves of data sent or stored under a wider one."""
    if any(not pattern for pattern in patterns):
        return node
    if not isinstance(node, dict):
        return None
    out = {}
    for key, child in node.items():
        if not isinstance(key, str):
            continue
        name = str(decode_key(key))
        under = [pattern[1:] for pattern in patterns if pattern[0] == "*" or pattern[0] == name]
        if under:
            found = pruned(child, under)
            if found is not None:
                out[key] = found
    return out or None


def going(listing, tables, modes):
    """`tables` ({id: data}) with only what this list has travel in one of `modes`: an id it doesn't name is left
    out, and so is anything outside the paths it takes. What is left still has to pass `tables_problem`."""
    entries, out = entries_of(listing), {}
    for name, data in (tables.items() if isinstance(tables, dict) else ()):
        entry = entries.get(name) if isinstance(name, str) else None
        if entry is None or entry["mode"] not in modes:
            continue
        if entry["mode"] == "back" or not isinstance(data, dict):
            kept = data                 # not a table at all: left in, for `tables_problem` to refuse
        elif entry["per"] == "character":
            kept = {key: found for key, one in data.items()
                    for found in [pruned(one, entry["take"]) if isinstance(one, dict) else one] if found is not None}
        else:
            kept = pruned(data, entry["take"])
        if kept is not None and kept != {}:
            out[name] = kept
    return out
