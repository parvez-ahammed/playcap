"""Subscribe: re-scan the source on a schedule and record whatever is new.

    python -m playcap schedule            loop: wait for each scheduled time, run a cycle
    python -m playcap schedule --once     run one cycle now and wait for it (what the
                                          Windows scheduled task runs)
    python -m playcap schedule --next     print when the next run is due
    python -m playcap schedule --stop     ask a running loop to exit
    python -m playcap schedule --register / --unregister / --status
                                          the Windows Task Scheduler entry

Config (config.json, editable in the UI):

    schedule_mode         "off" | "daily" | "every"
    schedule_time         "HH:MM"; daily: the time of day; every: the anchor the
                          N-hour grid is counted from (so runs land on predictable
                          times, the same ones Task Scheduler uses)
    schedule_every_hours  1-23 (Task Scheduler's HOURLY modifier allows no more;
                          daily covers 24)

A cycle is what a person would do by hand: open the playcap browser if its
debug port is closed, refresh the queue (build_queue), then start a recording.
The recorder already skips finished items and leaves not-yet-aired ones for
later, so "record what is new" needs no extra bookkeeping here. Both steps go
through playcap.jobs, so the UI shows a scheduled run exactly like one it
started itself -- same PID files, same logs, same Stop buttons.

Never two recorders: a cycle is skipped, not queued, when a recording (or
re-compressing, which shares OBS's CPU budget) is already running -- from the
UI, the command line or a previous cycle. jobs.start() checks the same thing
again under its lock, so a Start click racing the schedule cannot win twice.

Stop-aware: the loop checks the flag file .playcap/schedule.now every second
(`--stop` writes it) and Ctrl+C. Stopping the scheduler does not stop a
recording it started; that is the recording's own Stop button. --once waits
for the recording to end so Task Scheduler's "already running" rule holds.

Why Task Scheduler instead of a background service: it survives reboots,
needs no admin (a task for the current user, run only while they are logged
on -- OBS needs their desktop to capture), and Windows already ships it. The
task runs pythonw.exe on a small launcher file written to
.playcap/schedule_task.pyw, because /TR is limited to 261 characters and has
no working-directory option, and playcap need not be pip-installed. Output
goes to schedule.log. Task Scheduler stops a task after 72 hours by default;
the recorder is resumable, so the worst case is one item recorded again.
"""
import argparse
import hashlib
import math
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from playcap import jobs, settings

MODES = ("off", "daily", "every")
DEFAULTS = {"schedule_mode": "off", "schedule_time": "03:00", "schedule_every_hours": 6}
UI_KEYS = set(DEFAULTS)
LOG = "schedule.log"
LAUNCHER = "schedule_task.pyw"
RECHECK_SECONDS = 60       # the loop re-reads config.json this often while it waits
BROWSER_WAIT_SECONDS = 60
POLL_SECONDS = 5           # how often a cycle checks whether its job has finished
TR_LIMIT = 261             # schtasks /TR maximum length


# --- schedule arithmetic ----------------------------------------------------------
def parse_hhmm(text):
    """"HH:MM" -> (h, m), or None."""
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 and len(str(text).strip()) <= 5 else None


def next_run(cfg, now):
    """The first scheduled time strictly after `now` (a naive local datetime),
    or None when the schedule is off or invalid."""
    mode = cfg.get("schedule_mode", DEFAULTS["schedule_mode"])
    hm = parse_hhmm(cfg.get("schedule_time", DEFAULTS["schedule_time"]))
    if mode not in ("daily", "every") or hm is None:
        return None
    anchor = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if mode == "daily":
        return anchor if anchor > now else anchor + timedelta(days=1)
    try:
        hours = int(cfg.get("schedule_every_hours", DEFAULTS["schedule_every_hours"]))
    except (TypeError, ValueError):
        return None
    if not 1 <= hours <= 23:
        return None
    period = timedelta(hours=hours)
    k = math.floor((now - anchor) / period) + 1
    return anchor + k * period


def validate(partial):
    errors = {}
    if "schedule_mode" in partial and partial["schedule_mode"] not in MODES:
        errors["schedule_mode"] = "Pick off, daily or every N hours."
    if "schedule_time" in partial and parse_hhmm(partial["schedule_time"]) is None:
        errors["schedule_time"] = "A time like 03:00 (24-hour)."
    if "schedule_every_hours" in partial:
        v = partial["schedule_every_hours"]
        try:
            ok = not isinstance(v, bool) and 1 <= int(v) <= 23
        except (TypeError, ValueError):
            ok = False
        if not ok:
            errors["schedule_every_hours"] = "A whole number from 1 to 23 (use daily for 24)."
    return errors


def describe(cfg):
    mode = cfg.get("schedule_mode", "off")
    t = cfg.get("schedule_time", DEFAULTS["schedule_time"])
    if mode == "daily":
        return f"every day at {t}"
    if mode == "every":
        return f"every {cfg.get('schedule_every_hours', 6)} hours from {t}"
    return "off"


# --- one cycle ----------------------------------------------------------------------
def _say(msg):
    print(f"[schedule {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _console_python():
    """python.exe next to a pythonw.exe: the jobs run with CREATE_NO_WINDOW
    anyway, and a console interpreter always has working stdout handles."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return sys.executable


def _job_cmd(name):
    return [_console_python(), "-u", "-m", jobs.MODULES[name]]


def busy(root):
    """-> reason a cycle must not start now, or None."""
    st = jobs.status(root)
    ext = jobs.external(max_age=0)
    for name, why in (("record", "a recording is already running"),
                      ("optimize", "re-compressing is running"),
                      ("queue", "the queue is being refreshed"),
                      ("test", "a 25 s test is running")):
        if st.get(name, {}).get("running") or ext.get(name):
            return why
    return None


def _stop_requested(root):
    return jobs.requested(root, "schedule", "now")


def _wait_job(root, name, stop_aware=True):
    """Until the job is no longer running. False if the scheduler was stopped."""
    while jobs.status(root)[name]["running"]:
        if stop_aware and _stop_requested(root):
            return False
        time.sleep(POLL_SECONDS)
    return True


def run_cycle(root, wait=True):
    """Browser if needed -> refresh queue -> record. -> short outcome string."""
    from playcap import state
    from playcap.browser import port_open
    root = Path(root)
    raw = settings.read(root)
    if not settings.is_configured(raw):
        _say("playcap is not set up in this folder yet; nothing to do")
        return "not configured"
    why = busy(root)
    if why:
        _say(f"skipped: {why}")
        return "busy"
    try:
        cfg, adapter = state._effective(raw)
    except Exception as exc:
        _say(f"the source cannot be loaded: {exc}")
        return "error"

    port = int(cfg.get("chrome_debug_port", 9222))
    if not port_open(port):
        ok, msg = jobs.start("browser", root, cfg)
        _say(f"browser: {msg}")
        for _ in range(BROWSER_WAIT_SECONDS):
            if port_open(port):
                break
            time.sleep(1)
        else:
            _say("the browser did not open its debug port; carrying on (the recorder retries)")

    ok, msg = jobs.start("queue", root, cfg, cmd=_job_cmd("queue"))
    _say(f"queue: {msg}")
    if ok and not _wait_job(root, "queue"):
        return "stopped"
    # A failed refresh (logged out, empty scrape) leaves the old queue as it
    # was; recording what it holds is still right.
    ok, msg = jobs.start("record", root, cfg, cmd=_job_cmd("record"))
    _say(f"record: {msg}")
    if not ok:
        return "busy"
    if wait and not _wait_job(root, "record"):
        return "stopped"
    return "ran"


# --- the loop -------------------------------------------------------------------------
def loop(root, clock=datetime.now, sleep=time.sleep):
    root = Path(root)
    jobs.clear_stale_flags(root, "schedule")
    announced = None
    while True:
        cfg = {**DEFAULTS, **settings.read(root)}
        nxt = next_run(cfg, clock())
        if nxt is None:
            _say("the schedule is off (schedule_mode in config.json); exiting")
            return 0
        if nxt != announced:
            _say(f"next run {nxt:%Y-%m-%d %H:%M} ({describe(cfg)})")
            announced = nxt
        deadline = time.time() + min(RECHECK_SECONDS, max(0.0, (nxt - clock()).total_seconds()))
        while time.time() < deadline:
            if _stop_requested(root):
                jobs.consume(root, "schedule", "now")
                _say("stop requested; exiting")
                return 0
            sleep(1)
        if clock() >= nxt:
            run_cycle(root, wait=True)
            announced = None
            if _stop_requested(root):
                jobs.consume(root, "schedule", "now")
                _say("stop requested; exiting")
                return 0


# --- Windows Task Scheduler ----------------------------------------------------------------
def task_name(root):
    root = Path(root).resolve()
    h = hashlib.sha1(str(root).lower().encode("utf-8")).hexdigest()[:8]
    label = "".join(c for c in root.name if c.isalnum() or c in " -_")[:40].strip() or "folder"
    return f"playcap {label} {h}"


def _pythonw():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def launcher_text(root, code_root):
    return ("# Written by playcap (python -m playcap schedule --register). Run by Windows\n"
            "# Task Scheduler; removed again by --unregister. Safe to delete with the task.\n"
            "import os, sys\n"
            f"sys.path.insert(0, {str(code_root)!r})\n"
            f"os.chdir({str(root)!r})\n"
            "from playcap import schedule\n"
            "sys.exit(schedule.task_entry())\n")


def create_args(cfg, name, tr):
    """The schtasks /Create command line for cfg's schedule, or None if off."""
    hm = parse_hhmm(cfg.get("schedule_time", DEFAULTS["schedule_time"]))
    mode = cfg.get("schedule_mode", "off")
    if hm is None or mode not in ("daily", "every"):
        return None
    st = f"{hm[0]:02d}:{hm[1]:02d}"
    args = ["schtasks", "/Create", "/TN", name, "/TR", tr, "/F"]
    if mode == "daily":
        return args + ["/SC", "DAILY", "/ST", st]
    return args + ["/SC", "HOURLY", "/MO", str(int(cfg.get("schedule_every_hours", 6))),
                   "/ST", st]


def _schtasks(args, run):
    try:
        out = run(args, capture_output=True, text=True, timeout=30,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    return out.returncode == 0, text


def register(root, cfg=None, run=subprocess.run, platform=sys.platform):
    """Create or replace this folder's scheduled task. -> (ok, message)."""
    if not platform.startswith("win"):
        return False, ("Scheduled runs are registered with Windows Task Scheduler. "
                       "Elsewhere, run `python -m playcap schedule` (e.g. from cron or a login item).")
    root = Path(root).resolve()
    cfg = {**DEFAULTS, **(cfg if cfg is not None else settings.read(root))}
    if cfg.get("schedule_mode") not in ("daily", "every"):
        return False, "Pick a schedule (daily or every N hours) and save it first."
    errors = validate(cfg)
    if errors:
        return False, "; ".join(errors.values())
    state_dir = root / jobs.STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    launcher = state_dir / LAUNCHER
    settings.atomic_write_text(launcher, launcher_text(root, Path(__file__).resolve().parent.parent))
    tr = f'"{_pythonw()}" "{launcher}"'
    if len(tr) > TR_LIMIT:
        return False, (f"The command for the task is {len(tr)} characters; Windows allows "
                       f"{TR_LIMIT}. Move playcap or Python to a shorter path.")
    ok, text = _schtasks(create_args(cfg, task_name(root), tr), run)
    if not ok:
        return False, f"Task Scheduler refused: {text.splitlines()[-1] if text else 'unknown error'}"
    return True, f"Scheduled: {describe(cfg)} (Windows Task Scheduler, task \"{task_name(root)}\")."


def unregister(root, run=subprocess.run, platform=sys.platform):
    if not platform.startswith("win"):
        return False, "Nothing registered: scheduled tasks are Windows only."
    root = Path(root).resolve()
    ok, text = _schtasks(["schtasks", "/Delete", "/TN", task_name(root), "/F"], run)
    (root / jobs.STATE_DIR / LAUNCHER).unlink(missing_ok=True)
    if not ok and "cannot find" in text.lower():
        return True, "No scheduled task was registered."
    return ok, ("Scheduled task removed." if ok else f"Task Scheduler refused: {text}")


def task_status(root, run=subprocess.run, platform=sys.platform):
    """{"registered": bool, "next_run": str|None, "name": str}. Read-only."""
    name = task_name(root)
    if not platform.startswith("win"):
        return {"registered": False, "next_run": None, "name": name, "supported": False}
    ok, text = _schtasks(["schtasks", "/Query", "/TN", name, "/FO", "CSV", "/NH"], run)
    nxt = None
    if ok:
        import csv
        for row in csv.reader(text.splitlines()):
            if len(row) >= 2 and row[0].lstrip("\\") == name:
                nxt = row[1]
                break
    return {"registered": ok, "next_run": nxt, "name": name, "supported": True}


def task_entry():
    """What the scheduled task runs (under pythonw: no console, so log to a file)."""
    log = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    try:
        return main(["--once"])
    except SystemExit as exc:
        return exc.code
    except Exception as exc:          # pythonw would swallow the traceback
        import traceback
        traceback.print_exc()
        _say(f"cycle crashed: {exc}")
        return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m playcap schedule")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="run one cycle now and wait for it")
    g.add_argument("--next", action="store_true", help="print the next scheduled time")
    g.add_argument("--stop", action="store_true", help="ask a running schedule loop to exit")
    g.add_argument("--register", action="store_true", help="add the Windows scheduled task")
    g.add_argument("--unregister", action="store_true", help="remove the Windows scheduled task")
    g.add_argument("--status", action="store_true", help="show the Windows scheduled task")
    ap.add_argument("--root", default=".", help="folder holding config.json (default: here)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    jobs.graceful_signals()
    if args.stop:
        jobs.request(root, "schedule", "now")
        print("stop requested; a running schedule loop exits within a second")
        return 0
    if args.next:
        cfg = {**DEFAULTS, **settings.read(root)}
        nxt = next_run(cfg, datetime.now())
        print(f"next run {nxt:%Y-%m-%d %H:%M} ({describe(cfg)})" if nxt else "the schedule is off")
        return 0
    if args.register or args.unregister:
        ok, msg = register(root) if args.register else unregister(root)
        print(msg)
        return 0 if ok else 1
    if args.status:
        st = task_status(root)
        print(f"task \"{st['name']}\": " + ("registered, next run " + str(st["next_run"])
                                          if st["registered"] else "not registered"))
        return 0
    try:
        if args.once:
            jobs.clear_stale_flags(root, "schedule")
            run_cycle(root, wait=True)
            return 0
        return loop(root)
    except KeyboardInterrupt:
        _say("interrupted; a recording this started keeps going (stop it in the UI)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
