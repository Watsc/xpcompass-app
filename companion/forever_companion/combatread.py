"""Reading the game's combat log into fight summaries, sent to the XPCompass server (docs/GUILD_SPEC.md, "The combat
log"; the owner, 9 November: kept small, "so we dont blow up the mac").

The addon turns logging on at login (addon/XPCompass/CombatLog.lua); the game writes <client>/Logs/WoWCombatLog-*.txt.
Every minute (tray.py) this reads what a log gained since the last look, once the game has left it alone for SETTLE
seconds (between sessions, or a long pause in one), at most MOST_READ bytes a look:

- **Only the player's own lines.** A line counts when its source or target is the logger's own character (the log's
  "mine" flag on a player) or its pet; every other line is passed over unread. Nothing about other players is kept
  or sent.
- **Fight summaries, never lines.** A fight runs while the character or its pet hits or is hit, ending after
  FIGHT_GAP seconds of neither. Each becomes one "cfight" row: when it began, how long it ran, the character's level
  and class (from the addon's session record), damage dealt by the character and by its pet, damage taken, each
  spell's damage, hits, crits, misses and casts (at most MOST_SPELLS), the creatures hit by id (at most MOST_NPCS)
  and how many died, the seconds it cast nothing (gaps past IDLE_GAP), and whether it died. Rows go with the app's
  other uploads (uploads.py's /upload). The owner's 193 MB, 2.9-hour Era raid log came to 108 fights, 49 KB of rows
  (8 KB gzipped on the way), the biggest 949 bytes. A fight cut by the MOST_READ edge counts as two.
- **A small copy, kept a week.** The player's own lines are kept on this computer, gzip level 9, in
  <data>/combatlogs/<log name>.gz, deleted after Settings.combat_log_days like the logs (combatlogs.py). gzip, not
  zstd: the frozen app has zlib and not zstandard, and on the owner's 193 MB Era raid log the own lines (13.5 MB) came
  to 778 KB with gzip-9 against 661 KB with zstd-9.
- **Sharing.** Nothing is read for a player who turned sharing off before it became required (gamelogs.py's rule).
- **Remembered** in combatlogs.json: how far each log was read, when it was read to its end ("summarised", which
  lets combatlogs.clean delete it once old), its own slot and row numbers, and rows the server has yet to confirm.
"""
import csv
import gzip
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

from . import gamelogs, system, uploads

PATTERN = "WoWCombatLog-*.txt"
SETTLE = 15 * 60          # seconds a log is left alone before it is read on: the game may still be writing it
MOST_READ = 16_000_000    # bytes of logs read at most in one look (a 193 MB raid log: 6 s to read, so ~0.5 s a look)
FIGHT_GAP = 6.0           # seconds without a hit either way that end a fight
IDLE_GAP = 2.5            # seconds between the character's casts that count as idle in a fight
MOST_SPELLS = 30
MOST_NPCS = 10
PENDING = 2000            # stop reading more logs while this many rows await acknowledgement
OLDEST = 14 * 86400       # a log already older than this when first seen is passed over
MINE, PLAYER, PET, GUARDIAN = 0x1, 0x400, 0x1000, 0x2000
DAMAGE = {"SWING_DAMAGE", "RANGE_DAMAGE", "SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "DAMAGE_SHIELD", "DAMAGE_SPLIT"}
MISSED = {"SWING_MISSED", "RANGE_MISSED", "SPELL_MISSED", "SPELL_PERIODIC_MISSED"}
LINE = re.compile(r"^(\d+)/(\d+)(?:/(\d+))? (\d+):(\d+):(\d+)(?:\.\d+)?\s+(.*)$")


def flags(text):
    try:
        return int(text, 16)
    except (TypeError, ValueError):
        return 0


def stamp(match, year):
    """A line's local time as seconds since 1970."""
    mo, d, y, hh, mm, ss = match.groups()[:6]
    y = int(y) if y else year
    y = y + 2000 if y < 100 else y
    try:
        return datetime(y, int(mo), int(d), int(hh), int(mm), int(ss)).timestamp()
    except ValueError:
        return None


def npc_of(guid):
    parts = guid.split("-")
    if parts[0] in ("Creature", "Vehicle") and len(parts) > 5 and parts[5].isdigit():
        return int(parts[5])
    return None


def spell_name(event, fields):
    if event.startswith("SWING"):
        return "Melee"
    return fields[9][:60] if len(fields) > 9 else event


class Reader:
    """The player's own fights in a stream of log lines; `feed` each line, `close` at the end."""

    def __init__(self, year):
        self.year, self.me, self.pets = year, None, set()
        self.fight, self.done, self.kept = None, [], []

    def _mine(self, guid, fl):
        if self.me is None and fl & MINE and fl & PLAYER and guid.startswith("Player"):
            self.me = guid
        return guid == self.me

    def _touch(self, at):
        if self.fight and at - self.fight["last"] > FIGHT_GAP:
            self._end()
        if self.fight is None:
            self.fight = {"start": at, "last": at, "dmg": 0, "pet": 0, "taken": 0, "casts": [], "died": False,
                          "spells": {}, "hit": {}, "kills": 0}
        self.fight["last"] = at
        return self.fight

    def _end(self):
        fight, self.fight = self.fight, None
        if fight and (fight["dmg"] or fight["pet"] or fight["taken"]):
            self.done.append(fight)

    def feed(self, raw):
        m = LINE.match(raw)
        if not m:
            return
        rest = m.group(7)
        try:
            fields = next(csv.reader([rest]))
        except (csv.Error, StopIteration):
            return
        event, f = fields[0], fields[1:]
        if len(f) < 8:
            return
        src, sfl, dst, dfl = f[0], flags(f[2]), f[4], flags(f[6])
        mine_src, mine_dst = self._mine(src, sfl), self._mine(dst, dfl)
        if event in ("SPELL_SUMMON", "SPELL_CREATE") and mine_src:
            self.pets.add(dst)
        pet_src = not mine_src and (src in self.pets or bool(sfl & MINE and sfl & (PET | GUARDIAN)))
        if pet_src:
            self.pets.add(src)
        pet_dst = dst in self.pets
        if not (mine_src or mine_dst or pet_src or pet_dst):
            return
        at = stamp(m, self.year)
        if at is None:
            return
        self.kept.append(raw)
        if event in DAMAGE and len(f) >= 10:
            try:
                amount = int(f[-10])
            except ValueError:
                return
            crit = f[-3] not in ("nil", "", "0")
            if mine_src or pet_src:
                fight = self._touch(at)
                npc = npc_of(dst)
                if npc:
                    fight["hit"][dst] = npc
                if mine_src:
                    spell = fight["spells"].setdefault(spell_name(event, f), [0, 0, 0, 0, 0])
                    spell[0] += amount
                    spell[1] += 1
                    spell[2] += crit
                    fight["dmg"] += amount
                else:
                    fight["pet"] += amount
            elif mine_dst:
                self._touch(at)["taken"] += amount
        elif event in MISSED and mine_src:
            self._touch(at)["spells"].setdefault(spell_name(event, f), [0, 0, 0, 0, 0])[3] += 1
        elif event == "SPELL_CAST_SUCCESS" and mine_src and len(f) > 9:   # the opening cast starts the fight
            fight = self._touch(at)
            fight["casts"].append(at)
            fight["spells"].setdefault(f[9][:60], [0, 0, 0, 0, 0])[4] += 1
        elif event == "UNIT_DIED":
            if mine_dst and self.fight:
                self.fight["died"] = True
            elif self.fight and dst in self.fight["hit"]:
                self.fight["kills"] += 1

    def feed_died(self, raw):
        """UNIT_DIED lines name no source: a creature the character hit dying is read here."""
        if "UNIT_DIED" not in raw or not self.fight:
            return
        m = LINE.match(raw)
        if m:
            try:
                f = next(csv.reader([m.group(7)]))[1:]
            except (csv.Error, StopIteration):
                return
            if len(f) > 4 and f[4] in self.fight["hit"]:
                self.fight["kills"] += 1

    def close(self):
        self._end()


def idle(fight):
    marks = sorted(fight["casts"])
    if not marks:
        return round(fight["last"] - fight["start"], 1)
    edges = [fight["start"]] + marks + [fight["last"]]
    return round(sum(b - a - IDLE_GAP for a, b in zip(edges, edges[1:]) if b - a > IDLE_GAP), 1)


def row_of(fight, who):
    spells = sorted(fight["spells"].items(), key=lambda kv: (-kv[1][0], -kv[1][4]))[:MOST_SPELLS]
    npcs = sorted(set(fight["hit"].values()))[:MOST_NPCS]
    row = {"e": "cfight", "at": int(fight["start"]), "s": round(max(fight["last"] - fight["start"], 0.0), 1),
           "dmg": fight["dmg"], "pet": fight["pet"], "taken": fight["taken"], "idle": idle(fight),
           "kills": fight["kills"], "died": fight["died"], "npcs": npcs,
           "spells": [[name] + values for name, values in spells]}
    if who.get("lvl"):
        row["lvl"] = who["lvl"]
    if who.get("class"):
        row["cls"] = who["class"]
    return row


def read_lines(path, start, most):
    """(complete lines from byte `start`, the byte after the last one read)."""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(most)
    end = data.rfind(b"\n")
    if end < 0:
        return [], start
    data = data[:end + 1]
    return data.decode("utf-8", errors="replace").splitlines(), start + len(data)


def look(uploader, settings, game, game_id="forever"):
    """For the app's minute loop (tray.py): one CombatLogs per client beside the uploader."""
    if not isinstance(getattr(uploader, "combat_logs", None), dict):
        uploader.combat_logs = {}
    if game_id not in uploader.combat_logs:
        uploader.combat_logs[game_id] = CombatLogs(uploader, game_id=game_id)
    return uploader.combat_logs[game_id].look(game)


class CombatLogs:
    def __init__(self, uploader, path=None, copies=None, clock=time.time, game_id="forever"):
        from . import combatlogs
        self.uploader, self.clock, self.game_id = uploader, clock, game_id
        self.path = path or combatlogs.state_path(game_id)
        self.copies = copies or system.data_dir() / ("combatlogs" if game_id == "forever" else f"combatlogs-{game_id}")
        self.state = self._load()

    def _load(self):
        from . import combatlogs
        state = combatlogs.read_state(self.path)
        if not re.match(r"^[A-Za-z0-9_-]{8,64}$", str(state.get("slot", ""))):
            state["slot"] = secrets.token_urlsafe(12)
        state["next"] = state.get("next") if isinstance(state.get("next"), int) and state["next"] > 0 else 1
        state["read"] = state.get("read") if isinstance(state.get("read"), dict) else {}
        state["pending"] = state.get("pending") if isinstance(state.get("pending"), list) else []
        state["acked"] = state.get("acked", 0)
        # Older apps called a log summarised before its upload succeeded. Their marker cannot
        # authorize deletion; only a persisted range acknowledged by this app can do so.
        state["summarised"] = {name: entry["summarised"] for name, entry in state["read"].items()
                               if isinstance(entry, dict) and entry.get("summarised")
                               and "last_id" in entry and entry["last_id"] <= state["acked"]}
        return state

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(temp, self.path)

    def look(self, game):
        """Read what the logs gained since the last look and send the fights in it; how many rows the server took."""
        from . import combatlogs
        game, now = Path(game), self.clock()
        shares = gamelogs.GameLogs.sharing(self, game)
        if shares is None:
            return 0
        if not shares:
            return 0
        noted = combatlogs.sessions(game)
        budget, changed = MOST_READ, False
        for path in sorted((game / "Logs").glob(PATTERN)):
            if len(self.state["pending"]) >= PENDING:
                break              # raw logs remain the durable backlog; never trim unsent rows
            try:
                stat = path.stat()
            except OSError:
                continue
            seen = self.state["read"].get(path.name)
            if seen is None and now - stat.st_mtime > OLDEST:
                self.state["read"][path.name] = {"to": stat.st_size, "skipped": True}
                changed = True
                continue
            upto = (seen or {}).get("to", 0)
            if upto >= stat.st_size or now - stat.st_mtime < SETTLE or budget <= 0:
                continue
            rows, to, parser = self._read(path, upto, budget, noted, (seen or {}).get("parser"))
            self.state["summarised"].pop(path.name, None)
            budget -= to - upto
            for row in rows:
                row["id"] = self.state["next"]
                self.state["next"] += 1
            self.state["pending"].extend(rows)
            entry = {"to": to, "last_id": self.state["next"] - 1, "parser": parser}
            if to >= stat.st_size:
                entry["summarised"] = int(now)
                if entry["last_id"] <= self.state["acked"]:
                    self.state.setdefault("summarised", {})[path.name] = int(now)
            self.state["read"][path.name] = entry
            changed = True
        if changed:
            self._save()
        return self._send()

    def _read(self, path, start, most, noted, parser=None):
        lines, to = read_lines(path, start, most)
        try:
            year = datetime.fromtimestamp(path.stat().st_mtime).year
        except OSError:
            year = datetime.now().year
        reader = Reader(year)
        if isinstance(parser, dict):
            reader.me, reader.pets, reader.fight = parser.get("me"), set(parser.get("pets", [])), parser.get("fight")
        for raw in lines:
            reader.feed(raw)
            if "UNIT_DIED" in raw:
                reader.feed_died(raw)
        if to >= path.stat().st_size:
            reader.close()
        if reader.kept:
            self.copies.mkdir(parents=True, exist_ok=True)
            with gzip.open(self.copies / (path.stem + ".gz"), "ab", compresslevel=9) as out:   # a member a look
                out.write(("\n".join(reader.kept) + "\n").encode("utf-8"))
        rows = []
        for fight in reader.done:
            before = [s for s in noted if s["at"] <= fight["start"] + 60]
            who = max(before, key=lambda s: s["at"]) if before else {}
            rows.append(row_of(fight, who))
        return rows, to, {"me": reader.me, "pets": sorted(reader.pets), "fight": reader.fight}

    def _send(self):
        pending = self.state["pending"]
        if not pending:
            return 0
        try:
            answer = self.uploader._upload(self.state["slot"], {"rows": pending[:uploads.BATCH]}, self.game_id)
        except uploads.UploadError as failure:
            self.uploader.last = {"at": self.clock(), "text": f"Fights not sent, will try again: {failure}"}
            return 0
        upto = answer.get("upto", 0)
        sent = pending[:uploads.BATCH]
        if isinstance(upto, bool) or not isinstance(upto, int) or upto > sent[-1]["id"]:
            self.uploader.last = {"at": self.clock(), "text": "Fights not acknowledged safely; will try again."}
            return 0
        self.state["acked"] = max(self.state["acked"], upto)
        self.state["pending"] = [row for row in pending if row["id"] > upto]
        for name, entry in self.state["read"].items():
            if entry.get("summarised") and "last_id" in entry and entry["last_id"] <= self.state["acked"]:
                self.state["summarised"][name] = entry["summarised"]
        self._save()
        return answer.get("stored", 0)
