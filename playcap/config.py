"""Load config.json and the adapter it names.

Every stage reads the same config.json from the current directory, so paths,
ports, credentials and encoder settings live in one place and nothing is
hardcoded in the scripts.

Precedence, lowest first:

    DEFAULTS            generic fallbacks below
    adapter defaults    Adapter.config_defaults -- what a site/setup needs
    config.json         always wins

The adapter is the dotted module path in config key "adapter". When the key is
missing, an optional, untracked `local` package may name one via
`local.ADAPTER`; that is how a private setup keeps working with a config.json
that predates the key. Otherwise the generic html5_video adapter is used.

The config is read lazily (on first call), not at import, so importing a
module to inspect it -- or py_compile-ing it -- needs no config.json.
"""
import json
import sys
from pathlib import Path

from playcap import adapters

CONFIG_FILE = Path("config.json")
GENERIC_ADAPTER = "playcap.adapters.html5_video"

DEFAULTS = {
    "chrome_debug_port": 9222,
    "obs_ws_url": "ws://127.0.0.1:4455",
    "obs_password": "",
    "output_dir": "recordings",
    # ffmpeg remuxes recordings; encode_ffmpeg (falls back to ffmpeg) is the
    # one optimize.py encodes with, in case you want a different build there.
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "queue_file": "queue.json",
    "progress_file": "progress.json",
    "optimize_state": "optimize.json",
    # library filing (organize.py and the recorder's finalize step)
    "show": "Recordings",
    "season": 1,
    "title_cleanup": [],              # regexes stripped from raw titles, in order
    "title_collapse_restated": False,  # "Topic X Topic: X" -> "Topic X"
    "title_max_len": 80,
    "show_plot": "",
    "show_premiered": "",
    "avg_item_minutes": 120,          # only for the rough hours estimate
}

_cache = {}


def default_adapter_path():
    try:
        import local  # optional, untracked private package
        return getattr(local, "ADAPTER", None) or GENERIC_ADAPTER
    except ImportError:
        return GENERIC_ADAPTER


def load(path=None):
    """Return (cfg, adapter). Cached per path for the life of the process."""
    path = Path(path or CONFIG_FILE)
    key = str(path.resolve())
    if key in _cache:
        return _cache[key]
    if not path.exists():
        sys.exit(f"No {path} -- copy config.example.json to config.json and edit it.")
    user = json.loads(path.read_text())
    adapter = adapters.load(user.get("adapter") or default_adapter_path())
    cfg = {**DEFAULTS, **adapter.config_defaults, **user}
    adapter.cfg = cfg
    _cache[key] = (cfg, adapter)
    return cfg, adapter
