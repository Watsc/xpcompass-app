"""Clearing out the combat logs XPCompass caused (docs/GUILD_SPEC.md, "The combat log").

The addon turns the game's combat logging on at every login (addon/XPCompass/CombatLog.lua), and the game writes a
new <client>/Logs/WoWCombatLog-MMDDYY_HHMMSS.txt each session, never overwriting one. Left alone they fill a folder
the player never opens. This deletes a log only when every one of these holds (the owner, 9 November: "we shouldnt
delete recent logs just old ones and only if they didnt enable it themselves"):

- **Old.** Its last write is more than Settings.combat_log_days days ago (7 by default; 0 or less keeps every log).
- **Read.** The app has read it to its end and sent its fights (combatread.py): its name is in combatlogs.json's
  "summarised". A log it hasn't finished is kept, however old.
- **Ours.** It began within MATCH seconds of a session the addon noted (XPCompassCombatLog.sessions, in each WoW
  account's SavedVariables/XPCompass.lua) as one where XPCompass turned logging on.
- **Never the player's own.** If any session on this client found logging already on (the player's own /combatlog,
  or a raid addon's), no log on this client is ever deleted.

It deletes only those files, never trims or moves one, and never touches anything else in the folder. The small
copies of the player's own lines (combatread.py) go after the same number of days.
"""
import json
import time
from datetime import datetime
from pathlib import Path

from . import system, uploads

KEEP_DAYS = 7          # the default for Settings.combat_log_days
MATCH = 300            # seconds between a session's start and its log file's name
PATTERN = "WoWCombatLog-*.txt"


def state_path(game_id="forever"):
    return system.data_dir() / ("combatlogs.json" if game_id == "forever" else f"combatlogs-{game_id}.json")


def read_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("summarised"), dict):
        state["summarised"] = {}
    return state


def _items(value):
    if isinstance(value, dict):
        return [value[key] for key in sorted(value, key=lambda k: (not isinstance(k, int), k))]
    return list(value) if isinstance(value, list) else []


def sessions(game):
    """Every session the addon noted on this client: [{"at": seconds, "ours": bool}]."""
    found = []
    for path in sorted((Path(game) / "WTF" / "Account").glob(f"*/SavedVariables/{uploads.ADDON_FILE}")):
        try:
            saved, _ = uploads.read_saved(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:   # noqa: BLE001 - a file the game is writing, or a broken one: read nothing from it
            continue
        log = saved.get("XPCompassCombatLog")
        if not isinstance(log, dict):
            continue
        for session in _items(log.get("sessions")):
            if isinstance(session, dict) and isinstance(session.get("at"), (int, float)):
                found.append({"at": float(session["at"]), "ours": session.get("ours") is True,
                              "lvl": session.get("lvl") if isinstance(session.get("lvl"), int) else None,
                              "class": session.get("class") if isinstance(session.get("class"), str) else None})
    return found


def began(path):
    """When a log began, from its name (the game's local time), or None."""
    try:
        return datetime.strptime(path.stem.split("-", 1)[1], "%m%d%y_%H%M%S").timestamp()
    except (IndexError, ValueError):
        return None


def clean(game, keep_days=KEEP_DAYS, state=None, now=None, remove=None):
    """Delete this client's old combat logs that XPCompass caused and the app has summarised; the names deleted."""
    now = time.time() if now is None else now
    remove = remove or (lambda path: path.unlink())
    if not isinstance(keep_days, (int, float)) or keep_days <= 0:
        return []
    noted = sessions(game)
    if not noted or any(not session["ours"] for session in noted):
        return []
    summarised = (state or {}).get("summarised") or {}
    deleted = []
    for path in sorted((Path(game) / "Logs").glob(PATTERN)):
        try:
            stat = path.stat()
            written = stat.st_mtime
        except OSError:
            continue
        start = began(path)
        entry = ((state or {}).get("read") or {}).get(path.name, {})
        acknowledged = ("last_id" in entry and entry["last_id"] <= (state or {}).get("acked", -1)
                        and entry.get("to") == stat.st_size)
        if now - written <= keep_days * 86400 or path.name not in summarised or start is None or not acknowledged:
            continue
        if not any(abs(session["at"] - start) <= MATCH for session in noted):
            continue
        try:
            remove(path)
            deleted.append(path.name)
        except OSError:
            continue
    return deleted


def clean_copies(folder, keep_days=KEEP_DAYS, now=None, state=None):
    """Delete old app copies only after their source log's summaries were acknowledged."""
    now = time.time() if now is None else now
    if not isinstance(keep_days, (int, float)) or keep_days <= 0:
        return []
    deleted = []
    for path in sorted(Path(folder).glob("*.gz")):
        if path.stem + ".txt" not in ((state or {}).get("summarised") or {}):
            continue
        try:
            if now - path.stat().st_mtime > keep_days * 86400:
                path.unlink()
                deleted.append(path.name)
        except OSError:
            continue
    return deleted


def look(uploader, game, game_id="forever"):
    """For the app's minute loop (tray.py): read the logs on (combatread.py), then clear out what is old."""
    from . import combatread
    combatread.look(uploader, uploader.settings, game, game_id)
    keep = getattr(uploader.settings, "combat_log_days", KEEP_DAYS)
    state = uploader.combat_logs[game_id].state
    clean_copies(uploader.combat_logs[game_id].copies, keep, state=state)
    return clean(game, keep, state)
