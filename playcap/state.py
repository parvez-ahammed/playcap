"""Everything the UI shows, as one JSON-ready snapshot.

    snapshot(root) -> dict

Reads only the files the pipeline already writes (config, queue, progress,
optimize state, .playcap/now.json, logs) plus three live checks: is the debug
browser's port open, does OBS answer, how much disk is free. Nothing here
holds state of its own, so the UI can be closed and reopened at any time.

The three live checks are slow when things are down (a closed localhost port
takes ~0.5 s to refuse on Windows, an OBS handshake times out after 2 s, the
process scan takes ~1 s), so the UI server runs them in a background thread
(start_background) and snapshot() reads the latest result. Without the thread
-- tests, one-off calls -- they run inline.

Every file read degrades to "empty" when the file is missing, half-written or
corrupt -- a status page must never be the thing that crashes.

Item states mirror the recorder's own rules: done / skipped / failed come from
the progress file; otherwise locked, then "not aired" for anything dated later
than now minus the recorder's default 2 h grace, else waiting.
"""
import json
import threading
import time
from urllib.parse import urlparse
from datetime import datetime, timedelta
from pathlib import Path

from playcap import adapters, config, detect, jobs, settings
from playcap.browser import port_open
from playcap.recorder import free_gb
from playcap.status import tail_progress

GRACE_HOURS = 2.0          # recorder's --grace-hours default
NOW_STALE_S = 60           # now.json older than this is a leftover, not live
MIN_FREE_GB = 10           # recorder refuses to start below this
OBS_CACHE_S = 5
_obs_cache = {}


def _read_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def check_obs(url, password):
    """(reachable, message). Cached briefly: the UI polls every 2 s."""
    key = (url, password)
    hit = _obs_cache.get(key)
    if hit and time.time() - hit[0] < OBS_CACHE_S:
        return hit[1]
    from playcap.obs_client import Obs, ObsError
    port = urlparse(url).port or 4455
    if not port_open(port):
        result = (False, "OBS is not running")
        _obs_cache[key] = (time.time(), result)
        return result
    try:
        obs = Obs(password, url=url, timeout=2)
        try:
            obs.version()
        finally:
            obs.close()
        result = (True, "connected")
    except ObsError as exc:
        msg = str(exc)
        result = (False, "OBS refused the password" if "identify" in msg.lower()
                  else f"OBS error: {msg[:120]}")
    except Exception:
        result = (False, "OBS is not running")
    _obs_cache[key] = (time.time(), result)
    return result


def _effective(cfg):
    """config.load()'s merge (defaults < adapter defaults < config.json),
    without its exit-on-missing and per-process cache."""
    adapter = adapters.load(settings.effective_adapter(cfg))
    merged = {**config.DEFAULTS, **adapter.config_defaults, **cfg}
    adapter.cfg = merged
    return merged, adapter


FRIENDLY_ERRORS = [   # (substring of the raw error, what to tell a person)
    ("debug port unreachable", "The playcap browser was closed."),
    ("Cannot reach obs-websocket", "OBS was not running."),
    ("OBS refused", "OBS rejected the websocket password."),
    ("no player after", "No video player appeared on the page."),
    ("cannot attach to the player", "Could not connect to the video player."),
    ("stalled at", "Playback froze and could not be restarted."),
    ("exceeded time budget", "Playback took far longer than the video's length."),
    ("player vanished", "The video player disappeared mid-recording."),
    ("lost the player", "Lost the video player repeatedly."),
    ("fullscreen failed", "Could not make the video fullscreen."),
    ("could not rewind", "Could not rewind the video to the start."),
    ("/login", "The site asked to log in again."),
]


def friendly_error(raw):
    if not raw:
        return None
    for needle, text in FRIENDLY_ERRORS:
        if needle.lower() in raw.lower():
            return text
    return raw.split(":", 1)[-1].strip()[:140] or raw[:140]


def _items(root, cfg, adapter):
    raw = _read_json(Path(root) / cfg["queue_file"], [])
    progress = _read_json(Path(root) / cfg["progress_file"], {})
    cutoff = datetime.now() - timedelta(hours=GRACE_HOURS)
    out = []
    for r in raw:
        try:
            it = adapter.item(r)
        except Exception:
            continue
        p = progress.get(str(it.id), {}) if isinstance(progress.get(str(it.id)), dict) else {}
        st = p.get("status")
        if st in ("done", "skipped", "failed"):
            chip = st
        elif it.locked:
            chip = "locked"
        elif it.aired_at and it.aired_at > cutoff:
            chip = "not_aired"
        else:
            chip = "waiting"
        out.append({"id": str(it.id), "title": it.title, "kind": it.kind,
                    "day": it.day, "time": it.time, "state": chip,
                    "error": friendly_error(p.get("error")), "error_raw": p.get("error"),
                    "gb": p.get("gb")})
    return out


def _library(root, cfg):
    lib = Path(cfg["output_dir"])
    if not lib.is_absolute():
        lib = Path(root) / lib
    if not lib.exists():
        return []
    archive = lib.parent / (lib.name + "_originals")
    done = {str(Path(v["file"])).lower()
            for v in _read_json(Path(root) / cfg["optimize_state"], {}).values()
            if isinstance(v, dict) and v.get("status") == "done" and v.get("file")}
    out = []
    for f in sorted(list(lib.rglob("*.mp4")) + list(lib.rglob("*.mkv"))):
        if "_partial" in f.parts:
            continue
        before = None
        if archive.exists():
            orig = next((p for ext in (".mkv", ".mp4")
                         for p in archive.rglob(f.stem + ext)), None)
            before = orig.stat().st_size / 1024 ** 3 if orig else None
        out.append({"name": f.stem, "gb": f.stat().st_size / 1024 ** 3,
                    "optimized": str(f).lower() in done, "before_gb": before})
    return out


def _now(root, recording):
    if not recording:
        return None
    data = _read_json(Path(root) / jobs.STATE_DIR / "now.json", {})
    if not data or time.time() - data.get("updated_at", 0) > NOW_STALE_S:
        return None
    luma = data.get("luma")
    data["black"] = luma is not None and luma < 3
    return data


_live = {"data": None, "t": 0.0, "thread": None}
LIVE_INTERVAL_S = 3
LIVE_STALE_S = 15


def probe(root, raw_cfg, cfg):
    out = {"browser": port_open(int(cfg["chrome_debug_port"])), "disk_gb": None}
    out["obs"], out["obs_message"] = check_obs(*detect.obs_settings(raw_cfg))
    try:
        lib = Path(cfg["output_dir"])
        out["disk_gb"] = round(free_gb(lib if lib.is_absolute() else Path(root) / lib), 1)
    except OSError:
        pass
    out["external"] = jobs.external_untracked(root)
    return out


def start_background(root):
    """Refresh the live checks every few seconds in a daemon thread."""
    if _live["thread"]:
        return

    def loop():
        while True:
            try:
                raw = settings.read(root)
                if settings.is_configured(raw):
                    cfg, _ = _effective(raw)
                    _live["data"], _live["t"] = probe(root, raw, cfg), time.time()
            except Exception:
                pass
            time.sleep(LIVE_INTERVAL_S)

    _live["thread"] = threading.Thread(target=loop, daemon=True, name="playcap-probe")
    _live["thread"].start()


def snapshot(root):
    root = Path(root)
    raw_cfg = settings.read(root)
    snap = {"configured": settings.is_configured(raw_cfg), "problems": [],
            "items": [], "library": [], "now": None, "encode": None,
            "counts": {k: 0 for k in ("done", "failed", "skipped", "waiting",
                                      "not_aired", "locked", "total")},
            "health": {"browser": False, "obs": False, "obs_message": "",
                       "disk_gb": None},
            "jobs": jobs.status(root), "external": {}, "log": {},
            "show": raw_cfg.get("show"), "source": settings.effective_adapter(raw_cfg),
            "generated": time.time()}
    problems = snap["problems"]
    if not snap["configured"]:
        problems.append({"code": "setup", "text": "playcap is not set up yet.",
                         "fix": "setup"})
        return snap
    try:
        cfg, adapter = _effective(raw_cfg)
    except Exception as exc:
        problems.append({"code": "adapter",
                         "text": f"The selected source cannot be loaded: {exc}",
                         "fix": "setup"})
        return snap
    snap["source_label"] = adapter.label

    fresh = _live["thread"] and _live["data"] and time.time() - _live["t"] < LIVE_STALE_S
    live = _live["data"] if fresh else probe(root, raw_cfg, cfg)
    snap["health"] = {k: live[k] for k in ("browser", "obs", "obs_message", "disk_gb")}
    ok, msg = live["obs"], live["obs_message"]
    snap["external"] = live["external"]

    snap["items"] = _items(root, cfg, adapter)
    for it in snap["items"]:
        snap["counts"][it["state"]] += 1
    snap["counts"]["total"] = len(snap["items"])
    snap["library"] = _library(root, cfg)
    recording = snap["jobs"]["record"]["running"] or bool(snap["external"].get("record"))
    snap["now"] = _now(root, recording)
    if snap["jobs"]["optimize"]["running"] or snap["external"].get("optimize"):
        snap["encode"] = tail_progress(root / "optimize.err")
    snap["log"] = {n: jobs.tail(root, n, 15) for n in jobs.NAMES}

    if not ok:
        problems.append({"code": "obs", "text": msg, "fix": "launch_obs"})
    if not snap["health"]["browser"]:
        problems.append({"code": "browser",
                         "text": "The playcap browser window is not open.",
                         "fix": "start_browser"})
    disk = snap["health"]["disk_gb"]
    if disk is not None and disk < MIN_FREE_GB:
        problems.append({"code": "disk",
                         "text": f"Only {disk:.1f} GB free for recordings; need {MIN_FREE_GB} GB.",
                         "fix": None})
    if not snap["items"]:
        problems.append({"code": "queue", "text": "Nothing in the queue yet.",
                         "fix": "build_queue"})
    return snap
