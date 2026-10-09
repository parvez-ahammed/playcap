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

"Open folder" (POST /api/open/library) opens only the configured recordings
folder; the request carries no path.

Queue edits (retry / skip) are refused while recording: the recorder holds
the progress file in memory and rewrites it after every item, so an edit made
underneath it would be silently lost.

"Try it now" (/api/demo/*) records the bundled demo through the ordinary
browser and record jobs; playcap.demo explains how it stays apart from the
real queue and library. /api/install runs winget for one tool named from a
fixed allowlist (playcap.install); the request never carries a command or
package id. Both go through the same Host/token/Origin/JSON guards.
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playcap import detect, jobs, obs_setup, record_quality, settings, state
from playcap import demo, firstrun, install

HERE = Path(__file__).resolve().parent
STATIC = {"app.js": "application/javascript; charset=utf-8",
          "style.css": "text/css; charset=utf-8"}
MAX_BODY = 1 << 20
DEFAULT_PORT = 8765
OBS_SETUP_TRIES = 8          # x OBS_SETUP_WAIT: how long Set up OBS waits for a booting OBS
OBS_SETUP_WAIT = 2.0

BROWSE_JS = r"""
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk(); root.withdraw(); root.attributes("-topmost", True)
kind = sys.argv[1]
path = filedialog.askdirectory() if kind == "folder" else filedialog.askopenfilename()
print(path or "")
"""


def recording_now(root):
    return jobs.status(root)["record"]["running"] or bool(jobs.external(max_age=0).get("record"))


def _progress_path(root):
    cfg = settings.read(root)
    try:
        merged, _ = state._effective(cfg)
        return Path(root) / merged["progress_file"]
    except Exception:
        return Path(root) / "progress.json"


def _queue_items(root):
    cfg = settings.read(root)
    merged, adapter = state._effective(cfg)
    raw = state._read_json(Path(root) / merged["queue_file"], [])
    out = {}
    for r in raw:
        try:
            it = adapter.item(r)
            out[str(it.id)] = it
        except Exception:
            continue
    return out


def _queue_ids(root):
    return {k: it.title for k, it in _queue_items(root).items()}


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
    try:
        # Strict: a progress file that cannot be read must never be replaced
        # by one holding only this edit -- that would forget every done item.
        progress = settings.read_strict(path, {})
    except settings.Unreadable as exc:
        return False, str(exc)
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
    # Show what the pipeline will actually use (defaults < adapter defaults <
    # config.json), so the wizard never offers a placeholder that would
    # overwrite an adapter's own default when saved.
    try:
        shown, _ = state._effective(cfg)
    except Exception:
        shown = dict(cfg)
    shown = {k: v for k, v in shown.items() if k != "obs_password" and not isinstance(v, list)}
    shown["adapter"] = settings.effective_adapter(cfg)
    info = {
        "config": shown,
        "has_obs_password": bool(cfg.get("obs_password")),
        "tools": detect.report(cfg),
        "obs": ({"url": ws["url"], "enabled": ws["enabled"],
                 "has_password": bool(ws["password"])} if ws else None),
        "obs_running": obs_setup.is_obs_running(),
        "adapters": settings.adapters_available(),
        "links": settings.read_links(root, cfg),
        "root": str(Path(root).resolve()),
        "recording": recording_info(cfg),
    }
    return info


def recording_info(cfg):
    """The chosen recording quality and whether OBS is already set to it."""
    try:
        in_sync = record_quality.in_sync(cfg, os.environ, sys.platform)
    except Exception:
        in_sync = False
    return {"preset": record_quality.preset_name(cfg),
            "values": record_quality.effective(cfg),
            "describe": record_quality.describe(cfg),
            "presets": record_quality.PRESETS,
            "x264_presets": list(record_quality.X264_PRESETS),
            "obs_in_sync": in_sync}


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
        # OBS reads its recording encoder only at start, so apply the chosen
        # quality now, while it is still closed (see record_quality).
        if cfg.get("output_dir"):        # absolute: OBS runs from its own folder
            cfg = {**cfg, "output_dir": str(Path(root) / cfg["output_dir"])}
        try:
            quality_now, _ = record_quality.apply(cfg, os.environ, sys.platform, obs_running=False)
        except Exception:
            quality_now = False
        obs_setup.launch_obs(exe)
        done = [m for m, on in (("websocket switched on", enabled_now),
                                ("recording quality applied", quality_now)) if on]
        return True, "Starting OBS" + (f" ({', '.join(done)})." if done else ".")
    if action == "setup":
        from playcap.obs_client import Obs, ObsError
        url, password = detect.obs_settings(cfg)
        # Right after Launch OBS the websocket refuses connections, then
        # accepts them before OBS can answer scene requests (a live click
        # got an HTTP 500 here). Keep trying for a while instead.
        obs, last = None, None
        for _ in range(OBS_SETUP_TRIES):
            try:
                obs = Obs(password, url=url, timeout=5)
                break
            except ObsError as exc:
                last = exc
            except Exception as exc:
                last = exc
            if not obs_setup.is_obs_running():
                break
            time.sleep(OBS_SETUP_WAIT)
        if obs is None:
            ws = detect.obs_websocket()
            if ws and not ws["enabled"]:
                return False, ("OBS's websocket server is off. Close OBS and press "
                               "Launch OBS -- playcap switches it on.")
            if not obs_setup.is_obs_running():
                return False, "OBS is not running. Press Launch OBS first."
            return False, (f"OBS is still starting ({str(last).splitlines()[0]}). "
                           "Try again in a few seconds.")
        try:
            actions = None
            for _ in range(OBS_SETUP_TRIES):
                try:
                    actions = obs_setup.ensure(obs)
                    break
                except ObsError as exc:
                    last = exc
                    time.sleep(OBS_SETUP_WAIT)
            if actions is None:
                return False, (f"OBS is not ready yet ({str(last).splitlines()[0]}). "
                               "Try again in a few seconds.")
        finally:
            obs.close()
        return True, ("OBS was already set up." if not actions
                      else "Done: " + "; ".join(actions) + ".")
    return False, "Unknown OBS action."


def job_action(root, action, name, mode="now", item=None):
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
        st = jobs.status(root)
        ext = jobs.external(max_age=0) if name in ("test", "record") else {}
        if name == "test" and (st["record"]["running"] or st["optimize"]["running"]
                               or ext.get("record") or ext.get("optimize")):
            return False, "Stop recording and re-compressing first; the test uses OBS and the browser."
        if name == "record" and st["test"]["running"]:
            return False, "A 25 s test is running; start recording when it finishes."
        if item:
            # One item only (the queue row's Record / Test buttons). Validated
            # against the queue so nothing but a known item ever reaches the
            # command line: the id for record, that item's queued URL for test.
            if name not in ("record", "test"):
                return False, "Only recording and testing can target one item."
            known = _queue_items(root)
            if str(item) not in known:
                return False, "No such item in the queue."
            if name == "test":
                return jobs.start(name, root, cfg, args=[known[str(item)].url])
            return jobs.start(name, root, cfg, args=["--id", str(item)])
        if name == "test":
            return False, "Pick an item to test."
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


def open_library(root):
    """Show the recordings folder in the file manager. The path comes from
    config only -- never from the request -- so the page cannot open anything else."""
    raw = settings.read(root)
    try:
        cfg, _ = state._effective(raw)
    except Exception as exc:
        return False, f"The selected source cannot be loaded: {exc}"
    lib = state.library_dir(root, cfg)
    if not lib.is_dir():
        return False, f"No recordings folder yet ({lib}). It appears with the first recording."
    try:
        if sys.platform == "win32":
            os.startfile(str(lib))
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(lib)])
    except OSError as exc:
        return False, f"Could not open {lib}: {exc}"
    return True, f"Opened {lib}"


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
        # No other site may frame the UI and borrow a click on its buttons.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
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
        if n < 0 or n > MAX_BODY:
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
            if path == "/api/demo":
                return self._send(200, demo.status(self.root))
            if path == "/api/install":
                return self._send(200, install.status(self.root))
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
                                     str(body.get("job", "")), str(body.get("mode", "now")),
                                     body.get("item"))
                return self._send(200, {"ok": ok, "message": msg})
            if path.startswith("/api/item/"):
                ok, msg = edit_item(self.root, path.rsplit("/", 1)[1], str(body.get("id", "")))
                return self._send(200, {"ok": ok, "message": msg})
            if path == "/api/open/library":
                ok, msg = open_library(self.root)
                return self._send(200, {"ok": ok, "message": msg})
            if path == "/api/browse":
                kind = "folder" if body.get("kind") == "folder" else "file"
                return self._send(200, {"ok": True, "path": browse(kind)})
            if path == "/api/demo/start":
                ok, msg = demo.start(self.root, lambda: obs_action(self.root, "launch"),
                                     lambda: obs_action(self.root, "setup"))
                return self._send(200, {"ok": ok, "message": msg})
            if path == "/api/demo/stop":
                ok, msg = demo.stop(self.root)
                return self._send(200, {"ok": ok, "message": msg})
            if path == "/api/demo/open":
                ok, msg = demo.open_folder(self.root)
                return self._send(200, {"ok": ok, "message": msg})
            if path == "/api/install":
                ok, msg = install.start(self.root, str(body.get("tool", "")))
                return self._send(200, {"ok": ok, "message": msg})
            return self._send(404, {"ok": False, "message": "not found"})
        except Exception as exc:
            return self._send(500, {"ok": False, "message": f"{exc.__class__.__name__}: {exc}"})


def make_server(root, port=DEFAULT_PORT):
    handler = type("BoundHandler", (Handler,), {
        "root": Path(root).resolve(), "token": secrets.token_urlsafe(32)})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd


def _running_root(port):
    """The folder a playcap UI already on this port serves, or None when the
    port belongs to something else."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/setup", timeout=20) as r:
            return json.loads(r.read()).get("root")
    except (OSError, ValueError, AttributeError):
        return None


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
        other = _running_root(args.port)
        if other is None:
            sys.exit(f"Port {args.port} is in use by another program. "
                     f"Start the UI on another port: python -m playcap ui --port {args.port + 1}")
        if Path(other) != root:
            sys.exit(f"A playcap UI for another folder ({other}) is already on port "
                     f"{args.port}. Close it, or use --port {args.port + 1}.")
        print(f"playcap is already running for this folder. Opening {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    state.start_background(root)
    firstrun.mark(root, "ui_first_started")      # local only; see playcap.firstrun
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
