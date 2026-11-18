"""The app's window: ui.html in a native window (pywebview on Edge WebView2, part of Windows 11).

On Windows it has no title bar of Windows' own: ui.html's top bar is the title bar (it drags the window) and carries
minimise and close. The Mac keeps its own title bar and buttons.

The page asks Api.state() once a second and draws it; its buttons call the other Api methods.
Closing the window hides it; the app keeps running in the tray until Quit.
"""
import base64
import io
import logging
import struct
import sys
import time
import traceback
import webbrowser
from pathlib import Path

import webview
from PIL import Image

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


# The window's icon on Windows. The taskbar button and Alt+Tab show a window's own icon, and a window that has none
# gets the .exe's: the fixed launcher's, which an update never replaces. So the app makes an .ico of its icon from
# the two pictures packed with its code and gives it to its window, and an ordinary update changes what the taskbar
# shows. (The pictures travel as .png: apps refuse a code update holding any other kind of file,
# release.CODE_SUFFIXES.) Sizes are what Windows asks for at each display scaling, 100% to 250%.
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128)
LETTERS_FROM = 24           # below this the icon's letters can't be read, so the drawing without them is used
ICON_FILE = "XPCompass.ico"


def icon_frame(image):
    """One picture of an .ico: an uncompressed 32-bit bitmap, the kind every reader of the format takes."""
    side = image.width
    pixels = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes("raw", "BGRA")    # rows from the bottom up
    mask = bytes(((side + 31) // 32) * 4 * side)                                   # all clear: the alpha decides
    return struct.pack("<IiiHHIIiiII", 40, side, side * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0) + pixels + mask


def icon_bytes(big, small, sizes=ICON_SIZES):
    """The app's icon as an .ico file's bytes: `big` (with its letters) scaled to each size from LETTERS_FROM up,
    `small` (without them) to each size below."""
    frames = []
    for side in sizes:
        source = big if side >= LETTERS_FROM else small
        frames.append((side, icon_frame(source.convert("RGBA").resize((side, side), Image.LANCZOS))))
    out, at = struct.pack("<HHH", 0, 1, len(frames)), 6 + 16 * len(frames)
    for side, frame in frames:
        out += struct.pack("<BBBBHHII", side % 256, side % 256, 0, 0, 1, 32, len(frame), at)
        at += len(frame)
    return out + b"".join(frame for _, frame in frames)


def icon_file(folder=None):
    """Windows: the app's icon as an .ico in the app's data folder, written when it isn't there or isn't today's.
    None anywhere else, and when it can't be made (the window then keeps the launcher's icon)."""
    if not system.WINDOWS:
        return None
    try:
        data = icon_bytes(Image.open(system.branding(system.ICON)), Image.open(system.branding(system.ICON_SMALL)))
        path = Path(folder or system.data_dir()) / ICON_FILE
        if not path.is_file() or path.read_bytes() != data:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return path
    except Exception:   # noqa: BLE001 - the icon is never worth stopping the app for
        return None


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
        self.icon = icon_file()                         # Windows: what the taskbar button shows
        if self.icon:
            for name in ("before_show", "shown"):       # once it exists, before it is first seen; and when it is
                event = getattr(self.native.events, name, None)
                if event is not None:
                    event += self._dressed
        self.visible = bool(show)

    def handle(self):
        """Windows' handle of the window, once it exists; else None."""
        try:
            return int(self.native.native.Handle.ToInt64())
        except Exception:   # noqa: BLE001 - not made yet, or not a Windows window
            return None

    def dress(self):
        """Give the window the app's icon (system.set_window_icon). Asked again each time the window is opened from
        the tray: it costs nothing, and the window's own library set the launcher's icon when it made the window."""
        return bool(self.icon) and system.set_window_icon(self.handle(), self.icon)

    def _dressed(self):
        self.dress()            # an event's answer means something to the window's library; this one has none

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
        self.dress()

    def create_file_dialog(self, kind):
        return self.native.create_file_dialog(kind)

    def destroy(self):
        self.native.destroy()

    @staticmethod
    def run(icon=None):
        """Blocks on the main thread until the window is destroyed. `icon` (Windows, an .ico) is what the window's
        library gives each window it makes, in place of the launcher's, so the two never disagree."""
        webview.start(**start_options(icon))


def start_options(icon, start=None):
    """webview.start's options for `icon`: none when there is no icon, or when the window library the launcher
    froze is older than its `icon` option (a fixed launcher keeps its libraries, and an option it doesn't know
    would stop the app from opening at all). WM_SETICON (Window.dress) covers the window either way."""
    import inspect
    if not icon or not Path(icon).is_file():
        return {}
    try:
        known = "icon" in inspect.signature(start or webview.start).parameters
    except (TypeError, ValueError):
        known = False
    return {"icon": str(icon)} if known else {}
