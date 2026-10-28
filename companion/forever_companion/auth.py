"""Signing in with Discord through the XPCompass server (server/README.md has the contract).

The app never sees the Discord password or the server's Discord secret. It opens the player's
browser at the server, which sends it on to Discord and back to a one-off listener on 127.0.0.1
with a token. The token is kept in Windows Credential Manager, never in a file, and refreshed
when it has less than six hours left; each refresh is the server's daily role check.
"""
import base64
import ctypes
import http.server
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from . import system
from .release import ReleaseError, local

SERVER = "https://xpcompass.com"
SIGN_IN_FIRST = "Sign in with Discord to start."     # nothing is installed or sent before (no anonymous mode)
# Before the domain the server was a Tailscale address (https://<machine>.<tailnet>.ts.net): settings
# saved with one move to SERVER.
OLD_SERVER = re.compile(r"^https://[a-z0-9-]+\.[a-z0-9-]+\.ts\.net(?=/|$)")
TARGET = "ForeverCoachCompanion/session"       # the Credential Manager entry
REFRESH_WITHIN = 6 * 3600
SIGN_IN_TIMEOUT = 10 * 60

REFUSED = {
    "access_denied": "Sign-in was cancelled on Discord's page.",
    "not_member": "That Discord account is not in the Only Bans Discord.",
    "no_role": "That Discord account has no member role in Only Bans yet.",
    "expired": "Discord's sign-in link expired. Try again.",
    "discord_unavailable": "Discord could not be reached. Try again later.",
    "signin_required": "Sign in with Discord again.",
}


class AuthError(ReleaseError):
    def __init__(self, code, message=None):
        super().__init__(message or REFUSED.get(code, f"Sign-in failed ({code})."))
        self.code = code


class CredentialStore:
    """The session in Windows Credential Manager (a generic credential for this Windows user).
    The Mac's Keychain store is system.KeychainStore."""

    def __init__(self, target=TARGET):
        self.target = target

    def load(self):
        if os.name != "nt":
            return None
        api, cred = _credapi(), ctypes.c_void_p()
        if not api.CredReadW(self.target, 1, 0, ctypes.byref(cred)):
            return None
        try:
            c = ctypes.cast(cred, ctypes.POINTER(_Credential)).contents
            return json.loads(ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8"))
        finally:
            api.CredFree(cred)

    def save(self, session):
        blob = json.dumps(session).encode("utf-8")
        buffer = ctypes.create_string_buffer(blob, len(blob))
        cred = _Credential(Type=1, TargetName=self.target, CredentialBlobSize=len(blob),
                           CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)), Persist=2,
                           UserName="XPCompass")
        if not _credapi().CredWriteW(ctypes.byref(cred), 0):
            raise AuthError("store", "could not save the sign-in to Windows Credential Manager")

    def clear(self):
        if os.name == "nt":
            _credapi().CredDeleteW(self.target, 1, 0)


class MemoryStore:
    """For tests, and anywhere without Credential Manager."""

    def __init__(self, session=None):
        self.session = session

    def load(self):
        return self.session

    def save(self, session):
        self.session = session

    def clear(self):
        self.session = None


class Account:
    """The signed-in player, as far as the app knows."""

    def __init__(self, server=SERVER, store=None, now=time.time):
        self.server, self.store, self.now = server.rstrip("/"), store or system.credential_store(), now

    def session(self):
        return self.store.load()

    def name(self):
        session = self.session()
        return session and session.get("name")

    def token(self):
        """A usable token or None, refreshed first when it is close to expiring."""
        session = self.session()
        if not session:
            return None
        if session["expires_at"] - self.now() < REFRESH_WITHIN:
            try:
                session = self.refresh()
            except AuthError as error:
                if error.code != "discord_unavailable":
                    raise
        return session["token"] if session["expires_at"] > self.now() else None

    def sign_in(self, open_browser=webbrowser.open, timeout=SIGN_IN_TIMEOUT, cancel=None):
        """Open the browser and wait for the server to hand back a token. Returns the player's name.
        `cancel` (a threading.Event) stops the wait."""
        state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        got = {}

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                if url.path != "/callback" or query.get("state") != state:
                    self.send_error(404)
                    return
                got.update(query)
                body = ("<!doctype html><meta charset=utf-8><title>XPCompass</title>"
                        "<p style='font:16px sans-serif'>"
                        + ("Signed in. You can close this tab." if "token" in query
                           else "Sign-in did not finish. You can close this tab.") + "</p>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        listener = http.server.HTTPServer(("127.0.0.1", 0), Callback)
        listener.timeout = 1
        try:
            port = listener.server_address[1]
            query = urllib.parse.urlencode({"port": port, "state": state})
            open_browser(f"{self.server}/auth/discord/start?{query}")
            deadline = time.monotonic() + timeout
            while not got and time.monotonic() < deadline and not (cancel and cancel.is_set()):
                listener.handle_request()
        finally:
            listener.server_close()
        if not got and cancel and cancel.is_set():
            raise AuthError("cancelled", "Sign-in cancelled.")
        if not got:
            raise AuthError("timeout", "Sign-in timed out. Try again.")
        if "error" in got:
            raise AuthError(got["error"])
        session = {"token": got["token"], "expires_at": int(got["expires_at"])}
        self.store.save(session)
        return self._remember_name(session)

    def refresh(self):
        """Swap the token for a new one, which re-checks the player's roles on the server."""
        session = self.session()
        if not session:
            raise AuthError("signin_required")
        status, body = self.call("POST", "/auth/refresh", session["token"])
        if status == 200:
            fresh = {"token": body["token"], "expires_at": int(body["expires_at"]),
                     "name": body.get("display_name") or body.get("username") or session.get("name"),
                     "prerelease": bool(body.get("prerelease"))}
            self.store.save(fresh)
            return fresh
        code = body.get("error", f"http_{status}")
        if status in (401, 403):
            self.store.clear()
        raise AuthError(code, REFUSED.get(code) or body.get("message"))

    def sign_out(self):
        session = self.session()
        self.store.clear()
        if session:
            try:
                self.call("POST", "/auth/signout", session["token"])
            except AuthError:
                pass        # forgotten here either way; the server's copy expires within a day

    def _remember_name(self, session):
        status, body = self.call("GET", "/me", session["token"])
        if status == 200:
            session["name"] = body.get("display_name") or body.get("username")
            session["prerelease"] = bool(body.get("prerelease"))
            self.store.save(session)
        return session.get("name")

    def recheck(self):
        """Ask the server again who this is (name, prerelease access), without a new sign-in."""
        session = self.session()
        if session:
            try:
                self._remember_name(session)
            except AuthError:
                pass


    def call(self, method, path, token):
        """(status, JSON body) of a request to the server, with the token."""
        if not (self.server.startswith("https://") or local(self.server)):
            raise AuthError("insecure", "the sign-in server must use https")
        request = urllib.request.Request(self.server + path, method=method, data=b"" if method == "POST" else None,
                                         headers={"Authorization": f"Bearer {token}",
                                                  "User-Agent": "ForeverCoachCompanion"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw = response.status, response.read(1_000_000)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(1_000_000)
        except OSError as error:
            raise AuthError("discord_unavailable", f"could not reach the XPCompass server: {error}") from error
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {}


class _Credential(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p), ("LastWritten", ctypes.c_uint64),
                ("CredentialBlobSize", ctypes.c_uint32), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", ctypes.c_uint32), ("AttributeCount", ctypes.c_uint32),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", ctypes.c_wchar_p),
                ("UserName", ctypes.c_wchar_p)]


def _credapi():
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), ctypes.c_uint32]
    api.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    api.CredFree.argtypes = [ctypes.c_void_p]
    return api
