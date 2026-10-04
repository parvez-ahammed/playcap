#!/usr/bin/env python3
"""Local control panel: the status.py dashboard plus start/stop buttons.

    python control.py            # then open http://127.0.0.1:8765

Binds to 127.0.0.1 only -- it can launch processes, so it must never be
reachable from the network. Loopback alone is not enough: any web page open
in a browser can POST a form to 127.0.0.1, and DNS rebinding can make one read
it. So the Host header must name this panel, and every POST must carry a
random token generated at startup and only ever embedded in our own page. Nothing starts on its own: every job runs only
when its button is pressed, and each POST names the job explicitly.

Jobs are launched exactly as you would from a terminal, with stdout/stderr
going to the same log files, so status.py's progress parsing keeps working and
a job started here can be inspected (or killed) the usual way. "Stop" sends
CTRL_BREAK to the job's process group -- record_all.py and optimize.py both
treat an interrupt as "cost at most the item in flight", so stopping is safe.
Recording and optimizing are refused while the other runs: both saturate the
disk, and an encode competing with OBS drops capture frames.

The "chrome" button runs the adapter's browser launcher (playcap.browser
for the generic adapter). Jobs run as `python -m playcap.<module>` from the
current directory, the same code the root-level wrappers call.

The smoke test is not a button: it needs a page URL, so run it from a
terminal (python -m playcap.tools.smoke_test "<page-url>") before the first
batch.
"""
import json
import os
import secrets
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from playcap import config, status

HERE = Path.cwd()
HOST, PORT = "127.0.0.1", 8765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
TOKEN = secrets.token_urlsafe(32)

JOBS = {
    "chrome":   (None, "chrome_launch.log"),     # filled from the adapter in main()
    "queue":    ([sys.executable, "-u", "-m", "playcap.build_queue"], "build_queue.log"),
    "record":   ([sys.executable, "-u", "-m", "playcap.recorder"], "record_run.log"),
    "optimize": ([sys.executable, "-u", "-m", "playcap.optimize"], "optimize.log"),
}
EXCLUSIVE = {"record": "optimize", "optimize": "record"}
procs = {}


def alive(name):
    p = procs.get(name)
    return p is not None and p.poll() is None


def start(name):
    if alive(name):
        return f"{name} already running"
    other = EXCLUSIVE.get(name)
    if other and alive(other):
        return f"refused: {other} is running"
    cmd, log = JOBS[name]
    out = open(HERE / log, "a", encoding="utf-8")
    procs[name] = subprocess.Popen(
        cmd, cwd=HERE, stdout=out, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return f"started {name} (pid {procs[name].pid})"


def stop(name):
    if not alive(name):
        return f"{name} not running (from this panel)"
    procs[name].send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT))
    return f"interrupt sent to {name}"


def tail(log, n=25):
    try:
        blob = (HERE / log).read_bytes()[-6000:].decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(blob.replace("\r", "\n").splitlines()[-n:])


def page(msg=""):
    rows = []
    for name, (_, log) in JOBS.items():
        state = "running" if alive(name) else "idle"
        rows.append(
            f"<tr><td>{name}</td><td>{state}</td><td>"
            f"<form method=post style=display:inline><input type=hidden name=job value={name}>"
            f"<input type=hidden name=token value={TOKEN}>"
            f"<button name=op value=start>start</button>"
            f"<button name=op value=stop>stop</button></form></td>"
            f"<td><details><summary>{log}</summary><pre>{status.esc(tail(log))}</pre>"
            f"</details></td></tr>")
    controls = (
        "<div style='font-family:sans-serif;padding:12px;border-bottom:1px solid #888'>"
        "<b>Control</b> &mdash; local only. Jobs started from a terminal show as idle here "
        "but appear in the dashboard below."
        + (f"<p><i>{status.esc(msg)}</i></p>" if msg else "")
        + "<table>" + "".join(rows) + "</table></div>")
    return "<!doctype html><meta charset=utf-8><title>Recorder Control</title>" \
        + controls + status.render(status.gather())


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _trusted_host(self):
        if self.headers.get("Host") in ALLOWED_HOSTS:
            return True
        self._send("bad host", 403)
        return False

    def do_GET(self):
        if not self._trusted_host():
            return
        if self.path == "/api/state":
            body = json.dumps({n: alive(n) for n in JOBS})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
            return
        self._send(page())

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode())
        if not self._trusted_host():
            return
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in ALLOWED_HOSTS}:
            return self._send("bad origin", 403)
        if not secrets.compare_digest(form.get("token", [""])[0], TOKEN):
            return self._send("bad token", 403)
        job, op = form.get("job", [""])[0], form.get("op", [""])[0]
        if job not in JOBS or op not in ("start", "stop"):
            return self._send(page("unknown request"), 400)
        self._send(page(start(job) if op == "start" else stop(job)))

    def log_message(self, *a):
        pass


def main():
    os.chdir(HERE)
    _, adapter = config.load()
    JOBS["chrome"] = (adapter.browser_command(), JOBS["chrome"][1])
    print(f"control panel on http://{HOST}:{PORT}  (Ctrl+C to quit)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
