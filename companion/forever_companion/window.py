"""The app's window: ui.html in a native window (pywebview on Edge WebView2, part of Windows 11).

The page asks Api.state() once a second and draws it; its buttons call the other Api methods.
Closing the window hides it; the app keeps running in the tray until Quit.
"""
import base64
import io
import sys
import webbrowser
from pathlib import Path

import webview

# The About view's last line, the same words as the addon's About page (addon/XPCompass/Version.lua),
# pointing to the privacy policy and terms, which hold the detail (AGENTS.md, "Privacy text").
PRIVACY_URL = "https://xpcompass.com/privacy"
TERMS_URL = "https://xpcompass.com/terms"
PRIVACY_LINE = "Please refer to our Privacy Policy and Terms of Use."


def page():
    base = Path(sys._MEIPASS) / "forever_companion" if getattr(sys, "frozen", False) else Path(__file__).parent
    return (base / "ui.html").read_text(encoding="utf-8")


def data_uri(image):
    buffer = io.BytesIO()
    image.convert("RGBA").resize((128, 128)).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class Api:
    """What the page may call. Underscored names are not exposed to it."""

    def __init__(self, app):
        self._app = app

    def state(self):
        return dict(self._app.ui_state(), privacy_line=PRIVACY_LINE)

    def open_privacy(self):
        webbrowser.open(PRIVACY_URL)

    def open_terms(self):
        webbrowser.open(TERMS_URL)

    def sign_in(self):
        self._app.sign_in()

    def reopen_signin(self):
        self._app.reopen_signin()

    def cancel_signin(self):
        self._app.cancel_signin()

    def sign_out(self):
        self._app.sign_out()

    def set_wanted(self, package, on):
        self._app.set_wanted(package, bool(on))

    def check_now(self):
        self._app.wake.set()

    def open_addons(self, game_id="forever"):
        self._app.open_addons(str(game_id))

    def pick_game(self, game_id="forever"):
        chosen = self._app.window.create_file_dialog(webview.FOLDER_DIALOG)
        if chosen:
            self._app.set_game(chosen[0], str(game_id))

    def use_found_client(self, game_id):
        self._app.use_found_client(str(game_id))

    def dismiss_notice(self, game_id):
        self._app.dismiss_notice(str(game_id))

    def update_now(self, package):
        self._app.update_now(str(package))

    def toggle_start(self, on):
        self._app.set_start(bool(on))


class Window:
    def __init__(self, app, show):
        self.app = app
        self.native = webview.create_window("XPCompass", html=page(), js_api=Api(app), width=400, height=540,
                                            min_size=(360, 420), hidden=not show, background_color="#14110d")
        self.native.events.closing += self._closing

    def _closing(self):
        if self.app.stopping:
            return True
        self.native.hide()
        return False            # keep running in the tray

    def show(self):
        self.native.show()
        self.native.restore()

    def create_file_dialog(self, kind):
        return self.native.create_file_dialog(kind)

    def destroy(self):
        self.native.destroy()

    @staticmethod
    def run():
        webview.start()         # blocks on the main thread until the window is destroyed
