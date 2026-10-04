"""Render status.html. Thin wrapper so `python status.py [--watch]` keeps working.

Changes into this directory first, as the old script read its files from
beside itself rather than from the current directory. See playcap/status.py.
"""
import os
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

from playcap.status import main  # noqa: E402

if __name__ == "__main__":
    main()
