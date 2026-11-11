"""Release-channel names: durable user choice, compatible release/prerelease wire values."""
import urllib.parse

ALIASES = {"stable": "stable", "release": "stable", "players": "stable",
           "nightly": "nightly", "prerelease": "nightly", "testers": "nightly"}
WIRE = {"stable": "release", "nightly": "prerelease"}


def canonical(value):
    if value is None:
        return "stable"
    try:
        return ALIASES[value]
    except (KeyError, TypeError):
        raise ValueError("channel must be stable or nightly") from None


def source(url, channel):
    """An explicit selection; local signed build folders already identify their own channel."""
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme not in ("http", "https"):
        return url
    query = dict(urllib.parse.parse_qsl(parsed.query))
    query["channel"] = WIRE[canonical(channel)]
    path = parsed.path.rstrip("/")
    if path.endswith("/builds-prerelease"):
        path = path[:-len("-prerelease")]
    return urllib.parse.urlunsplit(parsed._replace(path=path, query=urllib.parse.urlencode(query)))
