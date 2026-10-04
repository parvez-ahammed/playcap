"""Old entry point for the control panel; it now opens the playcap UI.

    python control.py        same as: python -m playcap ui

The UI (playcap/ui/server.py) replaced the form-based panel that lived here:
a setup wizard, live recording status, queue retry/skip, and layered stops.
The root-level control.py wrapper changes into its own folder first, so the
UI manages the config.json and logs that sit beside it, as before.
"""
from playcap.ui.server import main

if __name__ == "__main__":
    main()
