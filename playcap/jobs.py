"""Start, watch and stop the pipeline's long-running jobs.

    start(name, root, cfg, args=())  browser | queue | record | optimize | test | install
                                    (+ extra CLI args; env= adds environment variables)
    stop(name, root, mode)          "now" or "after_current" (record only)
    kill(name, root)                last resort: force-terminate the process tree
    status(root)                    {name: {running, pid, started}}
    external()                      jobs running that this module did not start

Each job is a child process with its output in the same log files the
command-line tools always used, and a PID file under <root>/.playcap/ so the
job outlives the UI that started it and a reopened UI finds it again. A PID
file is trusted only while that PID is alive *and* was created when the file
says -- otherwise a reused PID would pass for our job.

Stopping is layered because no single mechanism works everywhere:

1. A flag file (<root>/.playcap/<name>.now) that the recorder and optimizer
   check on their own loops. Works whoever started the job and from any
   console -- the reliable path.
2. CTRL_BREAK (Windows) / SIGINT. Immediate, but on Windows it only reaches a
   process attached to the sender's console. UI jobs run with
   CREATE_NO_WINDOW, i.e. on a console of their own, so for them this step
   is a no-op and the flag does the work; it helps command-line runs.
   graceful_signals() makes CTRL_BREAK raise KeyboardInterrupt, so the job's
   own cleanup runs (OBS recording stopped, progress saved, partial deleted)
   instead of Python's default of dying on the spot.
3. kill(): the UI offers it only when a stop has not taken effect, and the UI
   then stops OBS itself, since the recorder's cleanup did not run.

"after_current" is a flag the recorder reads between items: finish the item in
flight, then exit. Jobs drop flags older than themselves at startup
(clear_stale_flags), so a late Stop never carries over to the next run.

start() runs under a lock and records the OS's own creation time of the
child, so two Start clicks cannot launch two recorders and a recycled PID is
never taken for the job (and never force-killed).
"install" runs a fixed winget command (playcap.install builds it; the UI
passes cmd=) so its output lands in install.log like any other job's.
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from playcap import adapters, settings

STATE_DIR = ".playcap"
NAMES = ("browser", "queue", "record", "optimize", "test", "install")
MODULES = {"queue": "playcap.build_queue", "record": "playcap.recorder",
           "optimize": "playcap.optimize", "test": "playcap.tools.smoke_test"}
# (stdout log, separate stderr log or None). optimize keeps ffmpeg's -stats
# on stderr because status/state parse progress from optimize.err.
LOGS = {"browser": ("chrome_launch.log", None), "queue": ("build_queue.log", None),
        "record": ("record_run.log", None), "optimize": ("optimize.log", "optimize.err"),
        "test": ("test_run.log", None), "install": ("install.log", None)}
EXCLUSIVE = {"record": "optimize", "optimize": "record"}
START_TOLERANCE_S = 10   # PID-file start time vs. the process's real creation time
EXACT_TOLERANCE_S = 2    # ... when the file holds the OS's own creation time
FRIENDLY = {"browser": "The browser", "queue": "Loading the queue",
            "record": "Recording", "optimize": "Re-compressing", "test": "The test",
            "install": "The install"}
RUN_MARK = "=== playcap run:"   # written to the log at each start (read by activity)
_start_lock = threading.Lock()   # the UI server is threaded: two Start clicks at once


def _dir(root):
    d = Path(root) / STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pidfile(root, name):
    return _dir(root) / f"{name}.json"


# --- flags -------------------------------------------------------------------
def _flag(root, name, kind):
    return _dir(root) / f"{name}.{kind}"


def request(root, name, kind):
    _flag(root, name, kind).write_text(str(time.time()))


def requested(root, name, kind):
    return _flag(root, name, kind).exists()


def consume(root, name, kind):
    """Delete a flag; True if it was there. On Windows a reader (an indexer,
    antivirus) can hold the file for a moment, and the recorder once crashed on
    exit with WinError 32 here -- so retry briefly, then leave it: a stale flag
    is cleared again by the next start()."""
    f = _flag(root, name, kind)
    for attempt in range(10):
        try:
            f.unlink()
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            time.sleep(0.2)
    return f.exists()


def clear_flags(root, name):
    for kind in ("now", "after_current"):
        consume(root, name, kind)


def clear_stale_flags(root, name):
    """Called by a job as it starts: drop stop flags written before this process
    existed. A Stop pressed just as a command-line run ended would otherwise
    stop the next run at its first check."""
    born = process_created(os.getpid()) or time.time()
    for kind in ("now", "after_current"):
        try:
            if _flag(root, name, kind).stat().st_mtime < born - 1:
                consume(root, name, kind)
        except OSError:
            pass


# --- process liveness ----------------------------------------------------------
def _creation_time_windows(pid):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)        # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:
            return None                                 # 259 = STILL_ACTIVE
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
            return None
        c = times[0]
        return ((c.dwHighDateTime << 32) | c.dwLowDateTime) / 1e7 - 11644473600
    finally:
        k32.CloseHandle(h)


def _creation_time_posix(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    try:
        out = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return time.time() - int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return time.time()          # alive, age unknown: accept


def process_created(pid):
    """When the OS says pid was created (epoch seconds), or None if it is gone."""
    return (_creation_time_windows(pid) if sys.platform.startswith("win")
            else _creation_time_posix(pid))


def pid_alive(pid, started=None, exact=False):
    if not pid:
        return False
    created = process_created(pid)
    if created is None:
        return False
    tolerance = EXACT_TOLERANCE_S if exact else START_TOLERANCE_S
    if started is not None and abs(created - started) > tolerance:
        return False
    return True


def _alive(info):
    return bool(info) and pid_alive(info.get("pid"), info.get("started"),
                                    bool(info.get("exact")))


def _read_pidfile(root, name):
    try:
        return json.loads(_pidfile(root, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status(root):
    out = {}
    for name in NAMES:
        info = _read_pidfile(root, name)
        alive = _alive(info)
        if info and not alive:
            _pidfile(root, name).unlink(missing_ok=True)
        out[name] = {"running": alive, "pid": info.get("pid") if alive else None,
                     "started": info.get("started") if alive else None,
                     "stopping": alive and requested(root, name, "now"),
                     "stopping_after": alive and requested(root, name, "after_current")}
    return out


# --- start / stop ----------------------------------------------------------------
def command(name, cfg):
    if name == "browser":
        adapter = adapters.load(settings.effective_adapter(cfg))
        adapter.cfg = cfg
        return adapter.browser_command()
    return [sys.executable, "-u", "-m", MODULES[name]]


def start(name, root, cfg, cmd=None, args=(), env=None):
    if name not in NAMES:
        return False, f"unknown job {name!r}"
    with _start_lock:
        return _start(name, root, cfg, cmd, args, env)


def _start(name, root, cfg, cmd, args, extra_env=None):
    st = status(root)
    if st[name]["running"]:
        return False, f"{FRIENDLY[name]} is already running."
    other = EXCLUSIVE.get(name)
    if other and (st[other]["running"] or external(max_age=0).get(other)):
        return False, f"{FRIENDLY[other]} is running; stop it first."
    if name in EXCLUSIVE and external().get(name):
        return False, f"{FRIENDLY[name]} is already running outside the UI."
    clear_flags(root, name)
    root = Path(root)
    out_name, err_name = LOGS[name]
    out = open(root / out_name, "a", encoding="utf-8")
    print(f"{RUN_MARK} {name} {time.strftime('%Y-%m-%d %H:%M:%S')} ===", file=out, flush=True)
    err = open(root / err_name, "a", encoding="utf-8") if err_name else subprocess.STDOUT
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if sys.platform.startswith("win"):
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # The job runs in the user's folder, which need not be where playcap's
    # code lives (and playcap need not be pip-installed): put the package's
    # parent on PYTHONPATH so `python -m playcap.x` resolves from anywhere.
    code_root = str(Path(__file__).resolve().parent.parent)
    path = os.pathsep.join(p for p in (code_root, os.environ.get("PYTHONPATH", "")) if p)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONUTF8": "1", "PYTHONPATH": path, **(extra_env or {})}
    try:
        proc = subprocess.Popen((cmd or command(name, cfg)) + list(args), cwd=str(root), stdout=out,
                                stderr=err, stdin=subprocess.DEVNULL,
                                creationflags=flags, env=env,
                                start_new_session=not sys.platform.startswith("win"))
    except OSError as exc:
        return False, f"Could not start {FRIENDLY[name].lower()}: {exc}"
    finally:
        out.close()
        if err is not subprocess.STDOUT:
            err.close()
    # Popen still holds the process handle here, so the PID cannot have been
    # reused yet: the OS creation time read now is the job's own. Comparing
    # against it (not against time.time()) keeps a recycled PID from passing.
    created = process_created(proc.pid)
    settings.atomic_write_json(_pidfile(root, name), {
        "pid": proc.pid, "started": created or time.time(), "exact": created is not None})
    return True, f"{FRIENDLY[name]} started."


def stop(name, root, mode="now"):
    st = status(root).get(name)
    if not st or not st["running"]:
        if name in EXCLUSIVE and external().get(name):
            if mode == "after_current" and name == "record":
                request(root, name, "after_current")
                return "will stop after the current item (started outside the UI)"
            request(root, name, "now")
            return "stop requested (started outside the UI; it will stop at its next check)"
        return f"{FRIENDLY[name]} is not running."
    if mode == "after_current":
        if name != "record":
            return "stop after current is only for recording"
        request(root, name, "after_current")
        return "will stop after the current item"
    request(root, name, "now")
    try:
        sig = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT)
        os.kill(st["pid"], sig)
    except (OSError, ValueError):
        pass        # other console / gone: the flag still stops it
    return f"Stopping: {FRIENDLY[name].lower()}."


def kill(name, root):
    info = _read_pidfile(root, name)
    pid = info.get("pid") if info else None
    if pid and _alive(info):
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        for _ in range(50):
            if not _alive(info):
                break
            time.sleep(0.1)
    _pidfile(root, name).unlink(missing_ok=True)
    clear_flags(root, name)
    return f"{FRIENDLY[name]} was force-stopped."


def graceful_signals():
    """CTRL_BREAK -> KeyboardInterrupt, so the job's own cleanup runs."""
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)


# --- jobs started elsewhere ---------------------------------------------------------
PATTERNS = {
    "record": re.compile(r"(playcap\.recorder|record_all\.py)\b"),
    "optimize": re.compile(r"(playcap\.optimize|(?<![\w.])optimize\.py)\b"),
}
_ext_cache = {"t": 0.0, "v": {}}


def parse_external(rows):
    found = {}
    for pid, cmdline in rows:
        for name, pat in PATTERNS.items():
            if cmdline and pat.search(cmdline):
                found.setdefault(name, []).append(pid)
    return found


def _process_rows():
    try:
        if sys.platform.startswith("win"):
            ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                  "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=8,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            rows = [ln.split("\t", 1) for ln in out.splitlines() if "\t" in ln]
        else:
            out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                                 text=True, timeout=5).stdout
            rows = [ln.strip().split(None, 1) for ln in out.splitlines() if ln.strip()]
        return [(int(r[0]), r[1] if len(r) > 1 else "") for r in rows if r[0].strip().isdigit()]
    except (OSError, subprocess.SubprocessError):
        return []


def external(max_age=15):
    """{name: [pid, ...]} for record/optimize processes this machine is running,
    minus the ones we track ourselves. Cached; the process scan costs ~1 s."""
    now = time.time()
    if now - _ext_cache["t"] > max_age:
        _ext_cache["v"] = parse_external(_process_rows())
        _ext_cache["t"] = now
    return _ext_cache["v"]


def external_untracked(root):
    ours = {v["pid"] for v in status(root).values() if v["pid"]}
    return {n: [p for p in pids if p not in ours] for n, pids in external().items()
            if [p for p in pids if p not in ours]}


def tail(root, name, n=25, stream="out"):
    out_name, err_name = LOGS[name]
    fname = err_name if stream == "err" and err_name else out_name
    try:
        blob = (Path(root) / fname).read_bytes()[-8000:].decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(blob.replace("\r", "\n").splitlines()[-n:])
