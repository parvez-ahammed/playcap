"""Build the recording queue. Thin wrapper so `python build_queue.py` keeps working.

See playcap/build_queue.py; where the list comes from is up to the adapter.
"""
from playcap.build_queue import main

if __name__ == "__main__":
    main()
