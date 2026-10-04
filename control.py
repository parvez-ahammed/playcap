"""Local control panel. Thin wrapper so `python control.py` keeps working.

Changes into this directory first so jobs and logs land beside it, as before.
See playcap/control.py.
"""
import os
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

from playcap.control import main  # noqa: E402

if __name__ == "__main__":
    main()
