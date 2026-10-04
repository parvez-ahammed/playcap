"""playcap's local web UI: setup wizard + control screen.

    python -m playcap ui [--port 8765] [--no-browser]

Serves index.html / app.js / style.css from this folder and a small JSON API.
All logic lives in the modules it calls (settings, detect, obs_setup, jobs,
state); this file only routes and guards.

Security: the server can start processes and edit files, and "only listening
on 127.0.0.1" does not stop a web page in the user's browser from sending it
requests. So every request must carry an allowed Host header (defeats DNS
rebinding), and every POST must carry the per-run token from the page
(X-Playcap-Token), a JSON content type (a cross-site form cannot send one
without a preflight) and, when present, a same-origin Origin header.

Queue edits (retry / skip) are refused while recording: the recorder holds
the progress file in memory and rewrites it after every item, so an edit made
underneath it would be silently lost.
"""
import argparse
import json
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playcap import detect, jobs, obs_setup, settings, state

HERE = Path(__file__).resolve().parent
STATIC = {"app.js": "application/javascript; charset=utf-8",
          "style.css": "text/css; charset=utf-8"}
MAX_BODY = 1 << 20
DEFAULT_PORT = 8765

BROWSE_JS = r"""
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk(); root.withdraw(); root.attributes("-topmost", True)
kind = sys.argv[1]
path = filedialog.askdirectory() if kind == "folder" else filedialog.askopenfilename()
print(path or "")
"""


def recording_now(root):
    return jobs.status(root)["record"]["running"] or bool(jobs.external().get("record"))


def _progress_path(root):
    cfg = settings.read(root)
    try:
        merged, _ = state._effective(cfg)
        return Path(root) / merged["progress_file"]
    except Exception:
        return Path(root) / "progress.json"


def _queue_ids(root):
    cfg = settings.read(root)
    merged, adapter = state._effective(cfg)
    raw = state._read_json(Path(root) / merged["queue_file"], [])
    out = {}
    for r in raw:
        try:
            it = adapter.item(r)
            out[str(it.id)] = it.title
        except Exception:
            continue
    return out


def edit_item(root, action, item_id):
    if recording_now(root):
        return False, "Stop recording first -- the recorder rewrites progress after each item."
    try:
        titles = _queue_ids(root)
    except Exception as exc:
        return False, f"Cannot read the queue: {exc}"
    if item_id not in titles:
        return False, "No such item in the queue."
    path = _progress_path(root)
    progress = state._read_json(path, {})
    entry = progress.get(item_id) or {}
    if action == "skip":
        progress[item_id] = {"status": "skipped", "title": titles[item_id],
                             "note": "skipped in the playcap UI"}
        msg = "Skipped."
    elif action == "unskip":
        if entry.get("status") != "skipped":
            return False, "That item is not skipped."
        progress.pop(item_id)
        msg = "Back in the queue."
    elif action == "retry":
        if entry.get("status") != "failed":
            return False, "Only failed items can be retried."
        progress.pop(item_id)
        msg = "Will be retried on the next recording run."
    else:
        return False, "Unknown action."
    settings.atomic_write_json(path, progress)
    return True, msg


def setup_info(root):
    cfg = settings.read(root)
    ws = detect.obs_websocket()
    info = {
        "config": {k: v for k, v in cfg.items() if k != "obs_password"},
        "has_obs_password": bool(cfg.get("obs_password")),
        "tools": detect.report(cfg),
        "obs": ({"url": ws["url"], "enabled": ws["enabled"],
                 "has_password": bool(ws["password"])} if ws else None),
        "obs_running": obs_setup.is_obs_running(),
        "adapters": settings.adapters_available(),
        "links": settings.read_links(root, cfg),
        "root": str(Path(root).resolve()),
    }
    return info


def obs_action(root, action):
    cfg = settings.read(root)
    if action == "launch":
        exe = detect.resolve("obs", cfg)
        if not exe:
            return False, "OBS is not installed (or not found). Install it from obsproject.com."
        if obs_setup.is_obs_running():
            return True, "OBS is already running."
        ws = detect.obs_websocket()
        enabled_now = False
        if not ws or not ws["enabled"]:
            enabled_now = obs_setup.enable_websocket()
        obs_setup.launch_obs(exe)
        return True, ("Starting OBS (websocket switched on)." if enabled_now
                      else "Starting OBS.")
    if action == "setup":
        from playcap.obs_client import Obs, ObsError
        url, password = detect.obs_settings(cfg)
        try:
            obs = Obs(password, url=url, timeout=5)
        except ObsError as exc:
            ws = detect.obs_websocket()
            if ws and not ws["enabled"]:
                return False, ("OBS's websocket server is off. Close OBS and press "
                               "Launch OBS -- playcap switches it on.")
            return False, f"Cannot reach OBS: {str(exc).splitlines()[0]}"
        except Exception:
            return False, "OBS is not running. Press Launch OBS first."
        try:
            actions = obs_setup.ensure(obs)
        finally:
            obs.close()
        return True, ("OBS was already set up." if not actions
                      else "Done: " + "; ".join(actions) + ".")
    return False, "Unknown OBS action."


def job_action(root, action, name, mode="now"):
    if name not in jobs.NAMES:
        return False, "Unknown job."
    if action == "start":
        raw = settings.read(root)
        if not settings.is_configured(raw):
            return False, "Finish setup first."
        try:
            cfg, _ = state._effective(raw)
        except Exception as exc:
            return False, f"The selected source cannot be loaded: {exc}"
        return jobs.start(name, root, cfg)
    if action == "stop":
        return True, jobs.stop(name, root, mode=mode)
    if action == "kill":
        msg = jobs.kill(name, root)
        if name == "record":
            msg += "; " + _stop_obs_recording(root)
        return True, msg
    return False, "Unknown action."


def _stop_obs_recording(root):
    """After a force-stop the recorder's own cleanup did not run."""
    from playcap.obs_client import Obs
    try:
        url, password = detect.obs_settings(settings.read(root))
        obs = Obs(password, url=url, timeout=5)
        try:
            if obs.record_status().get("outputActive"):
                path = obs.stop_record()
                return f"OBS recording stopped (partial file left at {path})"
            return "OBS was not recording"
        finally:
            obs.close()
    except Exception:
        return "could not reach OBS to stop its recording -- check OBS"


def browse(kind):
    try:
        out = subprocess.run([sys.executable, "-c", BROWSE_JS, kind],
                             capture_output=True, text=True, timeout=600)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "playcap"
    root = None
    token = None

    # --- plumbing ---------------------------------------------------------------
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body, default=str).encode() if not isinstance(body, str)
            else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self):
        if self.headers.get("Host") in self._allowed_hosts():
            return True
        self._send(403, {"ok": False, "message": "bad host"})
        return False

    def _post_ok(self):
        if not self._host_ok():
            return False
        if not secrets.compare_digest(self.headers.get("X-Playcap-Token", ""), self.token):
            self._send(403, {"ok": False, "message": "bad token"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in self._allowed_hosts()}:
            self._send(403, {"ok": False, "message": "bad origin"})
            return False
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._send(415, {"ok": False, "message": "JSON only"})
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    # --- routes -------------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return
        url = urlparse(self.path)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                html = (HERE / "index.html").read_text(encoding="utf-8")
                return self._send(200, html.replace("{{TOKEN}}", self.token),
                                  "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name in STATIC:
                    return self._send(200, (HERE / name).read_bytes(), STATIC[name])
                return self._send(404, {"ok": False, "message": "not found"})
            if path == "/api/state":
                return self._send(200, state.snapshot(self.root))
            if path == "/api/setup":
                return self._send(200, setup_info(self.root))
            if path == "/api/log":
                name = parse_qs(url.query).get("job", [""])[0]
                if name not in jobs.NAMES:
                    return self._send(400, {"ok": False, "message": "unknown job"})
                stream = parse_qs(url.query).get("stream", ["out"])[0]
                return self._send(200, {"text": jobs.tail(self.root, name, 300, stream)})
            return self._send(404, {"ok": False, "message": "not found"})
        except Exception as exc:          # never let one bad file kill the page
            return self._send(500, {"ok": False, "message": f"{exc.__class__.__name__}: {exc}"})

    def do_POST(self):
        if not self._post_ok():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
        except ValueError as exc:
            return self._send(400, {"ok": False, "message": str(exc)})
        try:
            if path == "/api/config":
                cfg, errors = settings.save(self.root, body)
                return self._send(200, {"ok": not errors, "errors": errors,
                                        "configured": settings.is_configured(cfg)})
            if path in ("/api/obs/launch", "/api/obs/setup"):
                ok, msg = obs_action(self.root, path.rsplit("/", 1)[1])
                return self._send(200, {"ok": ok, "message": msg})
            if path.startswith("/api/job/"):
                ok, msg = job_action(self.root, path.rsplit("/", 1)[1],
                                     str(body.get("job", "")), str(body.get("mode", "now")))
                return self._send(200, {"ok": ok, "message": msg})
            if path.startswith("/api/item/"):
                ok, msg = edit_item(self.root, path.rsplit("/", 1)[1], str(body.get("id", "")))
                return self._send(200, {"ok": ok, "message": msg})
            if path == "/api/browse":
                kind = "folder" if body.get("kind") == "folder" else "file"
                return self._send(200, {"ok": True, "path": browse(kind)})
            return self._send(404, {"ok": False, "message": "not found"})
        except Exception as exc:
            return self._send(500, {"ok": False, "message": f"{exc.__class__.__name__}: {exc}"})


def make_server(root, port=DEFAULT_PORT):
    handler = type("BoundHandler", (Handler,), {
        "root": Path(root).resolve(), "token": secrets.token_urlsafe(32)})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m playcap ui")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--root", default=".", help="folder holding config.json (default: here)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    url = f"http://127.0.0.1:{args.port}/"
    try:
        httpd = make_server(root, args.port)
    except OSError:
        print(f"Port {args.port} is busy -- playcap may already be running. Opening {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    state.start_background(root)
    print(f"playcap UI on {url}  (folder: {root})  -- Ctrl+C to quit")
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nUI closed. Running jobs keep going; reopen the UI to see them.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
