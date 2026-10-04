"""Record every queued item. Thin wrapper so `python record_all.py` keeps working.

The record loop lives in playcap/recorder.py; site knowledge in the adapter
named by config.json. Flags are unchanged: --dry-run, --limit, --speed,
--only, --include-locked, --include-future, --grace-hours.
"""
from playcap.recorder import main

if __name__ == "__main__":
    main()
