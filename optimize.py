"""Re-encode recordings in place, verified. Thin wrapper so `python optimize.py` keeps working.

See playcap/optimize.py for the verify-before-archive rules.
"""
from playcap.optimize import main

if __name__ == "__main__":
    main()
