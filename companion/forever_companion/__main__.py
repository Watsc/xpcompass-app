"""XPCompass companion: install and update the addon from a signed build.

    python -m forever_companion tray                      the background app (tray icon)
    python -m forever_companion signin | signout | whoami Discord sign-in for guild builds
    python -m forever_companion config   [--source URL_OR_FOLDER] [--game DIR]
    python -m forever_companion clients
    python -m forever_companion status   [--game DIR]
    python -m forever_companion update   [--source URL_OR_FOLDER] [--game DIR] [--dry-run]
    python -m forever_companion rollback [--game DIR]

Run from the companion folder (or with it on PYTHONPATH). --source and --game default to the saved
settings (config).
"""
import argparse
import sys

from . import auth, games, install, release, service
from .release import PACKAGES, ReleaseError


def game_dir(args, settings, package="forever-coach"):
    """The client folder `package` installs into: --game, else the player's choice, else the one found by product."""
    game_id = PACKAGES[package]["client"]
    chosen = args.game or settings.chosen(game_id) or games.pick(game=game_id)
    if chosen is None:
        raise ReleaseError(f"could not tell which {games.GAMES[game_id].title} client to use; pass --game (see `clients`)")
    other = games.other_game(chosen, game_id)
    if other:
        raise ReleaseError(f"{chosen} is a {other.title} client, not {games.GAMES[game_id].title}; pass its own with --game")
    return chosen


def main(argv=None):
    parser = argparse.ArgumentParser(prog="forever_companion", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tray", help="run in the background with a tray icon")
    commands.add_parser("clients", help="list the game client folders found")
    for name in ("signin", "signout", "whoami"):
        commands.add_parser(name)
    for name in ("config", "status", "update", "rollback"):
        sub = commands.add_parser(name)
        sub.add_argument("--game", help="client folder, e.g. ...\\World of Warcraft\\_forever_")
        if name in ("config", "update"):
            sub.add_argument("--source", help="https base URL or folder holding <package>/manifest.json")
        if name in ("config", "status", "update", "rollback"):
            sub.add_argument("--package", default="forever-coach", choices=sorted(PACKAGES),
                             help="the package whose game --game names; default WoW Forever's")
        if name == "update":
            sub.add_argument("--dry-run", action="store_true", help="download and check, install nothing")
            sub.add_argument("--allow-older", action="store_true", help="accept a build older than the installed one")
    args = parser.parse_args(argv)
    settings = service.Settings.load()

    try:
        if args.command == "tray":
            from . import tray
            return tray.main()
        account = auth.Account(settings.server)
        if args.command == "signin":
            print("Opening your browser to sign in with Discord...")
            print(f"Signed in as {account.sign_in() or 'a guild member'}.")
            return 0
        if args.command == "signout":
            account.sign_out()
            print("Signed out.")
            return 0
        if args.command == "whoami":
            session = account.session()
            print(f"Signed in as {session.get('name') or 'a guild member'}" if session else "Not signed in.")
            return 0
        if args.command == "config":
            settings.source = args.source or settings.source
            if args.game:
                game_id = PACKAGES[args.package]["client"]
                other = games.other_game(args.game, game_id)
                if other:
                    raise ReleaseError(f"{args.game} is a {other.title} client, not {games.GAMES[game_id].title}")
                settings.choose(game_id, args.game)
            settings.save()
            print(f"source: {settings.source or '(not set)'}")
            for game_id in games.GAMES:
                print(f"{game_id + ':':<8}{settings.chosen(game_id) or '(found automatically)'}")
            print(f"saved in {service.settings_path()}")
        elif args.command == "clients":
            found = games.clients()
            for folder, has in found:
                game = games.game_of(folder)
                print(f"{folder}   {game.title if game else games.product(folder) or 'unknown'}"
                      f"{'   (has XPCompass)' if has else ''}")
            if not found:
                print("no game client folders found; pass --game to the other commands")
        elif args.command == "status":
            for package, about in PACKAGES.items():
                if package != args.package and args.game:
                    continue                    # --game names one package's client
                try:
                    game = game_dir(args, settings, package)
                except ReleaseError as error:
                    print(f"  {about['name']}: {error}")
                    continue
                print(game)
                current = install.installed(game, package)
                print(f"  {about['name']}: " + (f"build {current['build']} published {current['published']}"
                                                if current else "nothing installed by the companion"))
                for line in install.local_changes(game, package):
                    print(f"    {line}")
        elif args.command == "update":
            game, source = game_dir(args, settings, args.package), args.source or settings.source
            if not source:
                raise ReleaseError("no build source; pass --source or set one with config")
            manifest, files = release.load(source, args.package, token=account.token())
            if args.dry_run:
                need = install.needs(install.installed(game, args.package), manifest)
                print(f"build {manifest['build']} checks out: {len(files)} files, signed; nothing installed "
                      f"(would need: {need})")
                return 0
            print(install.install(game, manifest, files, args.allow_older).advice())
        elif args.command == "rollback":
            build = install.roll_back(game_dir(args, settings, args.package), args.package)
            print(f"Back on {PACKAGES[args.package]['name']} {build}. Restart the game fully.")
    except ReleaseError as error:
        print(f"Not done: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
