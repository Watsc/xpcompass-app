"""The app's window: ui.html in a native window (pywebview on Edge WebView2, part of Windows 11).

On Windows it has no title bar of Windows' own: ui.html's top bar is the title bar (it drags the window) and carries
minimise and close. The Mac keeps its own title bar and buttons.

The page asks Api.state() once a second and draws it; its buttons call the other Api methods.
Closing the window hides it; the app keeps running in the tray until Quit.
"""
import base64
import io
import logging
import sys
import time
import traceback
import webbrowser
from pathlib import Path

import webview

from . import system

# The About view's last line, the same words as the addon's About page (addon/XPCompass/Version.lua),
# pointing to the privacy policy and terms, which hold the detail (AGENTS.md, "Privacy text").
PRIVACY_URL = "https://xpcompass.com/privacy"
TERMS_URL = "https://xpcompass.com/terms"
PRIVACY_LINE = "Please refer to our Privacy Policy and Terms of Use."
# ui.html's top bar is the title bar on Windows (the launcher's pywebview, 6.2.1, draws a frameless window with
# Windows' shadow); the Mac keeps its own title bar with its traffic lights.
FRAMELESS = not system.MAC


def page():
    base = Path(__file__).parent       # the source, or the code the Windows launcher loads
    if not (base / "ui.html").is_file() and getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "forever_companion"
    return (base / "ui.html").read_text(encoding="utf-8")


def data_uri(image):
    buffer = io.BytesIO()
    image.convert("RGBA").resize((128, 128)).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _acts(method):
    """A call the player made by clicking: the app notes when, so it knows the window is in use (tray.safe_to_switch),
    and the page is told why when the click did nothing. An error inside the call, or the reason the app gives for
    refusing (a string), goes back as {"error": reason}, which ui.html says under the top bar; a call that worked
    gives None."""
    def wrapped(self, *args, **kwargs):
        self._app.touched = time.time()
        try:
            refused = method(self, *args, **kwargs)
        except Exception as error:   # noqa: BLE001 - a click never fails without the window saying why
            logging.getLogger("pywebview").error(traceback.format_exc())
            return {"error": str(error) or type(error).__name__}
        return {"error": refused} if refused and isinstance(refused, str) else None
    wrapped.__name__ = method.__name__
    return wrapped


class Api:
    """What the page may call. Underscored names are not exposed to it. `state` is the page asking on its own;
    every other call is a click."""

    def __init__(self, app):
        self._app = app

    def state(self):
        window = getattr(self._app, "window", None)
        return dict(self._app.ui_state(), privacy_line=PRIVACY_LINE, frameless=bool(window and window.frameless))

    @_acts
    def open_privacy(self):
        webbrowser.open(PRIVACY_URL)

    @_acts
    def open_terms(self):
        webbrowser.open(TERMS_URL)

    @_acts
    def sign_in(self):
        self._app.sign_in()

    @_acts
    def reopen_signin(self):
        self._app.reopen_signin()

    @_acts
    def cancel_signin(self):
        self._app.cancel_signin()

    @_acts
    def sign_out(self):
        self._app.sign_out()

    @_acts
    def set_wanted(self, package, on):
        self._app.set_wanted(package, bool(on))

    @_acts
    def check_now(self):
        self._app.check_now()

    @_acts
    def open_addons(self, game_id="forever"):
        self._app.open_addons(str(game_id))

    @_acts
    def pick_game(self, game_id="forever"):
        chosen = self._app.window.create_file_dialog(webview.FOLDER_DIALOG)
        if chosen:
            return self._app.set_game(chosen[0], str(game_id))      # why the folder was refused, or None
        return None

    @_acts
    def use_found_client(self, game_id):
        self._app.use_found_client(str(game_id))

    @_acts
    def dismiss_notice(self, game_id):
        self._app.dismiss_notice(str(game_id))

    @_acts
    def update_now(self, package):
        self._app.update_now(str(package))

    @_acts
    def use_release(self, package):
        self._app.use_release(str(package))

    @_acts
    def minimize(self):
        self._app.window.minimize()

    @_acts
    def close_window(self):
        self._app.window.put_away()        # to the tray, as the title bar's close did

    @_acts
    def set_channel(self, channel):
        return self._app.set_channel(str(channel))                  # why it didn't switch, or None

    @_acts
    def toggle_start(self, on):
        self._app.set_start(bool(on))

    @_acts
    def toggle_data(self, on):
        self._app.set_data_channel(bool(on))


class Window:
    def __init__(self, app, show):
        self.app = app
        self.frameless = FRAMELESS
        self.native = webview.create_window("XPCompass", html=page(), js_api=Api(app), width=400, height=540,
                                            min_size=(360, 420), hidden=not show, background_color="#14110d",
                                            frameless=self.frameless, easy_drag=False, shadow=True)
        self.native.events.closing += self._closing
        self.visible = bool(show)

    def _closing(self):
        if self.app.stopping:
            return True
        self.put_away()
        return False            # keep running in the tray

    def put_away(self):
        """Close to the tray: the window hides and the app keeps running until Quit."""
        self.native.hide()
        self.visible = False
        closed = getattr(self.app, "window_closed", None)
        if closed:
            closed()            # an update waiting for the window to close can switch now

    def minimize(self):
        self.native.minimize()

    def show(self):
        self.native.show()
        self.native.restore()
        self.visible = True

    def create_file_dialog(self, kind):
        return self.native.create_file_dialog(kind)

    def destroy(self):
        self.native.destroy()

    @staticmethod
    def run():
        webview.start()         # blocks on the main thread until the window is destroyed
