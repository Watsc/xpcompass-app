"""Durable status for the independent jobs in the existing companion minute loop."""
import json
import os
import time
from pathlib import Path

from . import system

NAMES = {"clients": "Finding game folders", "records": "Game recordings", "medians": "Level times",
         "logs": "Game error reports", "combat": "Combat summaries", "market": "Auction scans",
         "sync": "Character backup", "data": "Data from the server"}
RECOVERY = "Retries automatically. Check your connection, then choose Check now."


class Status:
    def __init__(self, path=None, now=time.time):
        self.path = Path(path) if path is not None else system.data_dir() / "jobs.json"
        self.now, self.rows, self.save_error = now, {}, None
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(rows, dict):
                for key, row in rows.items():
                    game, _, name = key.partition(":")
                    if game in ("all", "forever", "era") and name in NAMES and isinstance(row, dict):
                        self.rows[key] = {field: row[field] for field in ("attempt", "success", "error", "retry", "running") if field in row}
                        if row.get("running"):
                            self.rows[key].update(running=False, error="Interrupted before completion.", retry=int(self.now()))
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.path.with_suffix(".new")
            with pending.open("w", encoding="utf-8") as stream:
                json.dump(self.rows, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, self.path)
            self.save_error = None
        except OSError:
            self.save_error = "Job status could not be saved. Check available disk space."

    def run(self, game, name, function, retry=60):
        if game not in ("all", "forever", "era") or name not in NAMES:
            raise ValueError("unknown companion job")
        at = int(self.now())
        row = self.rows.setdefault(game + ":" + name, {})
        row.update(attempt=at, running=True, error=None, retry=at + retry)
        self.save()
        try:
            result = function()
        except Exception as error:  # one failed stage must not suppress independent jobs
            # Error classes are useful without persisting URLs, credentials or saved-file contents.
            row.update(running=False, error=type(error).__name__ + ": could not finish.")
            self.save()
            return False, None
        row.update(running=False, success=int(self.now()), error=None)
        self.save()
        return True, result

    def failures(self):
        out = []
        for key, row in sorted(self.rows.items()):
            if row.get("error"):
                game, _, name = key.partition(":")
                out.append({"job": key, "text": NAMES[name] + (" (" + game + ")" if game != "all" else "") + ": " + str(row["error"]),
                            "attempt": row.get("attempt"), "success": row.get("success"), "retry": row.get("retry"), "recovery": RECOVERY})
        if self.save_error:
            out.append({"job": "status", "text": self.save_error, "recovery": "Free disk space, then choose Check now."})
        return out
