"""Build the XPCompass app yourself: a PyInstaller one-folder build in dist/, the same way the official
build is made, without the code signing, the build stamp or the setup.

    python -m pip install -r requirements.txt
    python build.py

Windows: dist/XPCompass/XPCompass.exe. Mac: dist/XPCompass.app.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "companion"
BRANDING = HERE / "assets" / "branding"
MAC = sys.platform == "darwin"


def main():
    sep = ":" if MAC else ";"
    icon = BRANDING / "XPCompass.ico"
    if MAC:
        from PIL import Image
        icon = HERE / "build" / "icon.icns"
        icon.parent.mkdir(exist_ok=True)
        Image.open(BRANDING / "XPCompassLogo-1024.png").convert("RGBA").resize((512, 512)).save(icon)
    hidden = ["pystray._darwin", "keyring.backends.macOS"] if MAC else ["pystray._win32"]
    ui = APP / "forever_companion" / "ui.html"
    crest = APP / "forever_companion" / "onlybans-crest.png"
    logo = BRANDING / "XPCompassLogo-256.png"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "XPCompass",
                    "--icon", str(icon), "--distpath", str(HERE / "dist"), "--workpath", str(HERE / "build"),
                    "--specpath", str(HERE / "build"), "--paths", str(APP),
                    *[arg for name in hidden for arg in ("--hidden-import", name)],
                    "--add-data", f"{ui}{sep}forever_companion", "--add-data", f"{logo}{sep}.",
                    "--add-data", f"{crest}{sep}.", str(APP / "ForeverCompanion.pyw")], cwd=HERE, check=True)
    print("built", HERE / "dist" / ("XPCompass.app" if MAC else "XPCompass"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
