"""Time to first recording, kept on this machine only.

    mark(root, key)   record when something first happened; later calls are no-ops

Writes <root>/.playcap/first_run.json:

    {"ui_first_started": <epoch>, "first_recording": <epoch>,
     "seconds_to_first_recording": <float>}

The UI marks "ui_first_started" when it starts; the recorder marks
"first_recording" after its first item is filed. Nothing reads it back and
nothing is sent anywhere: it exists so whoever tests a fresh install can see
how long it took from opening playcap to a finished recording. Never raises --
a timestamp is never worth failing a recording over.
"""
import json
import time
from pathlib import Path

FILE = "first_run.json"


def path(root):
    return Path(root) / ".playcap" / FILE


def read(root):
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mark(root, key, now=None):
    try:
        data = read(root)
        if key in data:
            return False
        data[key] = now if now is not None else time.time()
        start = data.get("ui_first_started")
        if key == "first_recording" and isinstance(start, (int, float)):
            data["seconds_to_first_recording"] = round(data[key] - start, 1)
        from playcap.settings import atomic_write_json
        path(root).parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path(root), data)
        return True
    except Exception:
        return False
