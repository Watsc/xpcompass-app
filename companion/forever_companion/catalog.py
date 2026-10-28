"""Which addons this account sees: the server's /catalog (server/README.md), per account.

The server decides what is listed, what this account may install, and how to unlock the rest
(sign in, join Only Bans, later buy). The app still decides what it may write: a package is only
installable here if release.PACKAGES knows its folders, whatever the catalog says.

The last answer is kept in catalog.json beside the settings, so the window still shows the list
when the server can't be reached. With no answer at all, the app's own list is shown and the
server's download checks still decide what actually installs.
"""
import json
from pathlib import Path

from . import release, system
from .release import COMING, PACKAGES, ReleaseError


def built_in():
    """The app's own list, for when the server has never answered."""
    packages = [{"id": package, "name": about["name"], "about": about["about"], "needs": list(about["needs"]),
                 "order": index, "status": "installable"} for index, (package, about) in enumerate(PACKAGES.items())]
    packages += [{"id": package, "name": about["name"], "about": about["about"], "needs": [], "order": 100 + index,
                  "status": "locked", "unlock": {"kind": "coming", "text": about["about"]}}
                 for index, (package, about) in enumerate(COMING.items())]
    return {"channel": None, "account": None, "packages": packages}


class Catalog:
    def __init__(self, cache=None):
        """`cache`: where to keep the last answer (default beside the settings); False keeps none."""
        self.cache = None if cache is False else Path(cache) if cache else system.data_dir() / "catalog.json"
        self.data = self._load() or built_in()
        self.live = False                 # True once the server has answered this run

    def _load(self):
        if self.cache is None:
            return None
        try:
            return json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def refresh(self, server, token=None):
        """Ask the server again. On failure keep the last answer and say why (None when fine)."""
        try:
            raw = release.fetch(server, "catalog", 1_000_000, token)
            data = json.loads(raw)
            if not isinstance(data.get("packages"), list):
                raise ValueError("no package list")
        except (ReleaseError, ValueError) as error:
            return str(error)
        self.data, self.live = data, True
        if self.cache is None:
            return None
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
        return None

    def entries(self):
        """The list for the window, in the server's order, with the app's own rules applied."""
        rows = []
        for item in sorted(self.data["packages"], key=lambda item: item.get("order", 0)):
            if item.get("id") in release.APP_FILES:
                continue
            known = PACKAGES.get(item["id"]) or COMING.get(item["id"]) or {}
            # The app's own short name and game for packages it knows; the server's for new ones.
            row = {"id": item["id"], "name": known.get("name") or item.get("name", item["id"]),
                   "game": known.get("game") or item.get("game") or "Other", "about": item.get("about", ""),
                   "needs": list(item.get("needs") or PACKAGES.get(item["id"], {}).get("needs", ())),
                   "status": item.get("status", "locked"), "unlock": item.get("unlock"),
                   "edition": item.get("edition"), "edition_name": item.get("edition_name"),
                   "upgrade": item.get("upgrade"), "testing": bool(item.get("testing"))}
            if row["status"] == "installable" and row["id"] not in PACKAGES:
                row["status"] = "locked"
                row["unlock"] = {"kind": "update", "text": "Update XPCompass to install this."}
            rows.append(row)
        return rows

    def member(self):
        """Whether the server says this account is an Only Bans member (None if it hasn't said)."""
        return (self.data.get("account") or {}).get("member")

    def installable(self, package):
        return any(row["id"] == package and row["status"] == "installable" for row in self.entries())

    def locked_reason(self, package):
        for row in self.entries():
            if row["id"] == package and row["status"] != "installable":
                return (row.get("unlock") or {}).get("text") or "Not available to this account."
        return None if package in PACKAGES else "Not offered any more."
