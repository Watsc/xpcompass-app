# XPCompass app

The source of the XPCompass desktop app for Windows and Mac: the small tray app that installs the
XPCompass addons into World of Warcraft, keeps them up to date, and sends what the addons record to
xpcompass.com. This repository is published one way from the project's private repository at each app
release; each commit is one release. Issues and questions go to the Only Bans Discord.

XPCompass is not made or endorsed by Blizzard Entertainment.

## What the app does

- **Signs you in with Discord**, the first time it runs. Nothing is installed or sent before that.
  Your browser opens xpcompass.com, which sends you to Discord and back to the app on `127.0.0.1`.
  The app never sees your Discord password. Its sign-in token is kept in Windows Credential Manager
  or the macOS Keychain (`auth.py`).
- **Installs and updates the addons** in your game's `Interface/AddOns` folder, and only the
  XPCompass folders (`release.py`, `install.py`). Every build is checked against an Ed25519
  signature (`TRUSTED_KEYS` in `release.py`) before anything is written. A build that fails the check
  is refused, whoever served it.
- **Updates itself** the same way, with the same signature check (`selfupdate.py`).
- **Sends what the addons record** to xpcompass.com (`uploads.py`, `gamelogs.py`, `market.py`).

## What it reads

- Where World of Warcraft is installed: the Battle.net install entries in the Windows registry, or the
  usual folders on a Mac (`games.py`).
- The game's saved data, which WoW writes at logout, `/reload` and exit:
  - `WTF/Account/<account>/SavedVariables/XPCompass.lua`: the play data the base addon recorded.
  - each character's `SavedVariables/XPCompass.lua`: the feedback notes waiting to be sent.
  - `WTF/Account/<account>/SavedVariables/XPCompass_Market.lua`: auction house scans.

  These files are read as plain data; nothing in them is run.
- `Logs/FrameXML.log` and the crash reports in `Errors/`, keeping only lines and crashes that name
  an XPCompass folder. The `.dmp` file beside a crash report is never opened. Your user folder and
  WTF account path are taken out of any line before it is sent (`gamelogs.py`).

It writes to the game's saved data only while the game is closed, and only to:
- remove rows the server already holds;
- copy an addon's settings to its new name after a rename.

## What it sends, and where

Everything goes over https to **xpcompass.com**, and nowhere else:

| Address | What |
|---|---|
| `/auth/discord/start` (in your browser), `/auth/refresh`, `/auth/signout`, `/me` | Discord sign-in |
| `/catalog`, `/builds/...` | Which addons your account may have, and the signed builds |
| `POST /upload` | The rows the base addon recorded, your feedback notes, and XPCompass errors |
| `POST /market` | Auction house scans: items, unit buyouts and stack sizes, realm, faction, time. Never sellers or bidders |
| `POST /sharing` | Only from a copy where sharing was turned off before it became required |

Each request carries:
- your Discord sign-in token;
- a random install id the app makes once;
- a random id for each saved file.

Your WoW account name, file paths and anything else about your computer are never sent. A character's
name goes only for an Only Bans member's guild chronicle. What the server keeps, and for how long, is
in the [Privacy Policy](https://xpcompass.com/privacy).

## Building it yourself

You need Python 3.12 on Windows 10/11 or macOS.

    python -m pip install -r requirements.txt
    cd companion
    python -m forever_companion --help           # run from source: no build needed
    pythonw ForeverCompanion.pyw                 # the tray app, from source
    cd ..
    python build.py                              # a PyInstaller build in dist/

`build.py` makes a one-folder build: `dist/XPCompass/XPCompass.exe` on Windows, or
`dist/XPCompass.app` on a Mac. It isn't code-signed, and it never replaces itself with the official
build, because it carries no build stamp (`selfupdate.py`). It installs the same signed addons as the
official app. The official Windows setup is packed from the same build with Inno Setup
(`companion/installer.iss`).

To check that a download matches a release, compare its SHA-256 with the hash shown beside the
release's VirusTotal link on [xpcompass.com/download](https://xpcompass.com/download).

## Licence

All rights reserved, unless a LICENSE file in this repository says otherwise. You may read and build
the code to check what the app does.
