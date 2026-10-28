"""Start the companion's tray icon without a console window (pythonw ForeverCompanion.pyw)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from forever_companion.tray import main  # noqa: E402

sys.exit(main())
