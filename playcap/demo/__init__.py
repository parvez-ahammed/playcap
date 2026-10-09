"""The bundled demo and the UI's "Try it now" button.

This folder ships inside the package: index.html (a page with one <video>),
test-video.mp4 (a six-second test pattern, ~66 KB) and queue.txt (one entry
pointing at the page). So `uvx playcap ui` can record something real with no
git checkout and no site of your own. examples/demo/ in a checkout reuses the
same video.

    folder(root)                  <root>/playcap-demo
    plan(root)                    (capture backend, tools still missing)
    needs(root)                   tools the demo cannot run without
    prepare(root)                 copy the page, write the demo config + queue
    start(root, launch_obs, setup_obs)   one click: browser, (OBS,) record
    stop(root)                    cancel the setup or stop the demo recording
    status(root)                  what the Try-it-now panel shows

Isolation without a second code path. The demo runs the ordinary "browser"
and "record" jobs (playcap.jobs) from the user's folder, so PID files, stop
flags (.playcap/record.now), logs and the UI's Stop / Force stop all work
exactly as for a real run. What differs is the config: the jobs get
PLAYCAP_CONFIG=<root>/playcap-demo/config.json (see playcap.config), whose
queue, progress file and recordings folder are absolute paths inside
playcap-demo/. The user's own config.json, queue, progress.json and library
are never read or written. Settings the user already chose that describe the
machine (Chrome/OBS/ffmpeg paths, debug port, browser profile, OBS websocket,
ffmpeg capture settings) are carried over, so the demo uses the same browser
and screen recorder a real run would.

Which screen recorder (playcap.capture). The demo must work on a machine
with only Chrome and ffmpeg, so it does not assume OBS: a user who picked
"capture_backend": "ffmpeg" gets ffmpeg; otherwise OBS when it is installed
(the default backend, and the one a real run would use); otherwise ffmpeg
when its build can actually grab the screen (detect.capture_backends: ddagrab
or gdigrab plus an H.264 encoder). With ffmpeg the demo needs only Chrome and
ffmpeg, writes "capture_backend": "ffmpeg" into the demo config, and skips the
OBS launch and scene setup entirely. With neither, it asks for OBS (the
default) and names ffmpeg as the alternative.

Every Try it now starts a fresh demo progress file (only the demo's own), so
the button always records again; finalize() numbers repeats "(2)", "(3)".

Setup (opening the browser; with OBS, starting and configuring it) takes a few
seconds of polling, so start() runs it in a background thread and reports
phases through <root>/.playcap/demo.json. The thread only calls the same
functions the UI's buttons do (server.obs_action is passed in as
launch_obs / setup_obs) and ends by starting the record job; from then on
the recorder owns the run. A stop during setup cancels before anything is
recorded.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from playcap import detect, jobs, settings

DATA = Path(__file__).resolve().parent
FILES = ("index.html", "test-video.mp4", "queue.txt")
DIR_NAME = "playcap-demo"
STATE = "demo.json"
# Per backend. With OBS, ffmpeg only remuxes (without it the file stays .mkv).
NEEDED = {"obs": ("chrome", "obs"), "ffmpeg": ("chrome", "ffmpeg")}
NAMES = {"chrome": "Chrome", "obs": "OBS", "ffmpeg": "ffmpeg"}
CARRY = ("chrome_exe", "obs_exe", "chrome_debug_port", "browser_profile",
         "obs_ws_url", "obs_password", "ffmpeg", "ffprobe",
         "capture_grabber", "capture_encoder", "capture_audio", "capture_fps")
BROWSER_WAIT_S = 30
SETUP_TRIES = 3

_lock = threading.Lock()
_run = {"thread": None, "cancel": threading.Event()}


def folder(root):
    return Path(root) / DIR_NAME


def _state_path(root):
    return Path(root) / jobs.STATE_DIR / STATE


def _read_state(root):
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _set_state(root, **kw):
    data = {**_read_state(root), **kw, "updated": time.time()}
    _state_path(root).parent.mkdir(parents=True, exist_ok=True)
    settings.atomic_write_json(_state_path(root), data)
    return data


def _ffmpeg_capture_ok(user_cfg):
    """Can this machine's ffmpeg record the screen (not merely: is it installed)?"""
    try:
        return bool(detect.capture_backends(user_cfg)["ffmpeg"]["ok"])
    except Exception:
        return False


def choose_backend(user_cfg, report):
    """The user's explicit ffmpeg choice; else OBS if installed; else ffmpeg
    if it can capture; else OBS (what the UI then asks to install)."""
    if user_cfg.get("capture_backend") == "ffmpeg":
        return "ffmpeg"
    if (report.get("obs") or {}).get("ok"):
        return "obs"
    return "ffmpeg" if _ffmpeg_capture_ok(user_cfg) else "obs"


def plan(root, report=None, user_cfg=None):
    """-> (backend, [tools the demo cannot run without that are missing])."""
    user_cfg = settings.read(root) if user_cfg is None else user_cfg
    report = report if report is not None else detect.report(user_cfg)
    backend = choose_backend(user_cfg, report)
    return backend, [t for t in NEEDED[backend] if not (report.get(t) or {}).get("ok")]


def needs(root, report=None):
    """Tools the demo cannot run without that are not installed."""
    return plan(root, report)[1]


def config_for(root, user_cfg=None, backend="obs"):
    user_cfg = settings.read(root) if user_cfg is None else user_cfg
    d = folder(root).resolve()
    cfg = {
        "adapter": "playcap.adapters.html5_video",
        "queue_source": str(d / "page" / "queue.txt"),
        "queue_file": str(d / "queue.json"),
        "progress_file": str(d / "progress.json"),
        "optimize_state": str(d / "optimize.json"),
        "output_dir": str(d / "recordings"),
        "show": "playcap demo",
        "library_layout": "folder",
        "write_nfo": False,
        "avg_item_minutes": 1,
        "capture_backend": backend,
    }
    for key in CARRY:
        if user_cfg.get(key) not in (None, ""):
            cfg[key] = user_cfg[key]
    # The recorder restarts OBS itself only when it knows where OBS is, and
    # runs ffmpeg from the path it is given (config's default is a bare "ffmpeg").
    for tool, key in (("obs", "obs_exe"), ("chrome", "chrome_exe"),
                      ("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe")):
        if not cfg.get(key):
            found = detect.resolve(tool, user_cfg)
            if found:
                cfg[key] = found
    return cfg


def prepare(root, backend=None):
    """Copy the page into <root>/playcap-demo/page and write the demo's own
    config, queue and a fresh progress file. -> (config path, cfg)."""
    from playcap.adapters.html5_video import read_queue_source
    if backend is None:
        backend = plan(root)[0]
    d = folder(root)
    page = d / "page"
    page.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        src, dst = DATA / name, page / name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
    cfg = config_for(root, backend=backend)
    cfg_path = d / "config.json"
    settings.atomic_write_json(cfg_path, cfg)
    settings.atomic_write_json(Path(cfg["queue_file"]), read_queue_source(page / "queue.txt"))
    settings.atomic_write_json(Path(cfg["progress_file"]), {})
    return cfg_path, cfg


def _busy_reason(root):
    st = jobs.status(root)
    ext = jobs.external(max_age=0)
    if st["record"]["running"] or ext.get("record"):
        return "Recording is already running."
    if st["optimize"]["running"] or ext.get("optimize"):
        return "Re-compressing is running; stop it first."
    if st["test"]["running"]:
        return "A 25 s test is running; try again when it finishes."
    return None


def _port_open(port):
    from playcap.browser import port_open
    return port_open(port)


def start(root, launch_obs, setup_obs, port_open=_port_open, background=True):
    """Set up and record the demo. launch_obs / setup_obs are the UI's own
    OBS buttons (each -> (ok, message)). Returns at once; status() follows."""
    with _lock:
        t = _run["thread"]
        if t and t.is_alive():
            return False, "The demo is already starting."
        backend, missing = plan(root)
        if missing:
            names = " and ".join(NAMES[m] for m in missing)
            alt = (" (or ffmpeg, which records the screen without OBS)"
                   if "obs" in missing else "")
            return False, f"The demo needs {names}{alt}. Install it first."
        busy = _busy_reason(root)
        if busy:
            return False, busy
        cfg_path, cfg = prepare(root, backend)
        _run["cancel"] = threading.Event()
        _set_state(root, phase="starting", message="Getting ready…", record_pid=None,
                   started=time.time(), error=None)
        args = (root, cfg_path, cfg, launch_obs, setup_obs, port_open, _run["cancel"])
        if not background:
            _setup(*args)
            return True, "Demo started."
        t = threading.Thread(target=_setup, args=args, daemon=True, name="playcap-demo")
        _run["thread"] = t
        t.start()
    if backend == "ffmpeg":
        return True, "Setting up the demo: browser, then a 6-second recording with ffmpeg."
    return True, "Setting up the demo: browser, then OBS, then a 6-second recording."


def _setup(root, cfg_path, cfg, launch_obs, setup_obs, port_open, cancel):
    from playcap import config, obs_setup
    env = {config.CONFIG_ENV: str(cfg_path)}

    def fail(msg):
        _set_state(root, phase="failed", message=msg, error=msg)

    def cancelled():
        if cancel.is_set():
            _set_state(root, phase="stopped", message="Stopped before recording.")
            return True
        return False

    try:
        port = int(cfg.get("chrome_debug_port") or 9222)
        if not port_open(port):
            _set_state(root, phase="browser", message="Opening the playcap browser…")
            ok, msg = jobs.start("browser", root, cfg, env=env)
            if not ok:
                return fail(msg)
            for _ in range(BROWSER_WAIT_S * 2):
                if port_open(port) or cancel.is_set():
                    break
                time.sleep(0.5)
            else:
                return fail("The browser did not open. See the Browser log.")
        if cancelled():
            return
        if cfg.get("capture_backend") != "ffmpeg":     # ffmpeg: the recorder starts it
            if not obs_setup.is_obs_running():
                _set_state(root, phase="obs", message="Starting OBS…")
                ok, msg = launch_obs()
                if not ok:
                    return fail(msg)
            _set_state(root, phase="obs", message="Setting up OBS's recording scene…")
            for attempt in range(SETUP_TRIES):
                if cancelled():
                    return
                ok, msg = setup_obs()
                if ok:
                    break
            else:
                return fail(msg)
        if cancelled():
            return
        _set_state(root, phase="recording", message="Recording the demo…")
        ok, msg = jobs.start("record", root, cfg, env=env)
        if not ok:
            return fail(msg)
        pid = jobs.status(root)["record"].get("pid")
        _set_state(root, record_pid=pid)
    except Exception as exc:          # a thread has nobody to raise to
        fail(f"{exc.__class__.__name__}: {exc}")


def stop(root):
    _run["cancel"].set()
    s = _read_state(root)
    rec = jobs.status(root)["record"]
    if rec["running"] and s.get("record_pid") and rec["pid"] == s.get("record_pid"):
        return True, jobs.stop("record", root, mode="now")
    t = _run["thread"]
    if t and t.is_alive():
        return True, "Stopping the demo setup."
    return False, "The demo is not running."


def _result(root):
    """(file, error) from the demo's own progress file."""
    try:
        cfg = json.loads((folder(root) / "config.json").read_text(encoding="utf-8"))
        progress = json.loads(Path(cfg["progress_file"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    for entry in progress.values() if isinstance(progress, dict) else ():
        if entry.get("status") == "done":
            return entry.get("file"), None
        if entry.get("status") == "failed":
            return None, entry.get("error")
    return None, None


def status(root, report=None):
    s = _read_state(root)
    phase = s.get("phase") or "idle"
    t = _run["thread"]
    setting_up = bool(t and t.is_alive())
    rec = jobs.status(root)["record"]
    recording = bool(rec["running"] and s.get("record_pid") and rec["pid"] == s.get("record_pid"))
    backend, missing = plan(root, report)
    out = {"phase": phase, "message": s.get("message") or "", "running": setting_up or recording,
           "file": None, "error": None, "folder": str(folder(root).resolve() / "recordings"),
           "needs": missing, "backend": backend, "now": None}
    if phase == "recording" and not recording and not setting_up:
        file, error = _result(root)
        if file:
            out.update(phase="done", message="Done. Your first recording is saved.", file=file)
        elif error:
            out.update(phase="failed", message="The demo did not record.", error=error)
        else:
            out.update(phase="stopped", message="The demo stopped before it finished.")
    elif phase in ("starting", "browser", "obs") and not setting_up:
        out.update(phase="stopped", message="The demo setup was interrupted.")
    if recording:
        try:
            now = json.loads((Path(root) / jobs.STATE_DIR / "now.json").read_text(encoding="utf-8"))
            out["now"] = {k: now.get(k) for k in ("t", "duration")}
        except (OSError, ValueError):
            pass
    if phase == "failed":
        out["error"] = s.get("error")
    return out


def open_folder(root):
    """Show the demo recordings folder. Fixed path; the request carries none."""
    d = folder(root) / "recordings"
    if not d.is_dir():
        return False, "No demo recording yet."
    try:
        if sys.platform == "win32":
            os.startfile(str(d))
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(d)])
    except OSError as exc:
        return False, f"Could not open {d}: {exc}"
    return True, f"Opened {d}"
