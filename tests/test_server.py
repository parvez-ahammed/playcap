import http.client
import json
import threading

import pytest

from playcap import jobs, state
from playcap.ui import server


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "port_open", lambda port: False)
    monkeypatch.setattr(state, "check_obs", lambda url, pw: (False, "OBS is not running"))
    monkeypatch.setattr(jobs, "external", lambda max_age=15: {})
    httpd = server.make_server(tmp_path, port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield tmp_path, port
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, token=None, host=None, origin=None,
         ctype="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-Playcap-Token"] = token
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = ctype
    conn.request(method, path, body=data, headers=headers)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw.decode("utf-8", "replace")


def token_of(port):
    status, html = call(port, "GET", "/")
    assert status == 200
    marker = 'name="playcap-token" content="'
    return html.split(marker, 1)[1].split('"', 1)[0]


def test_index_and_static(ui):
    _, port = ui
    status, html = call(port, "GET", "/")
    assert status == 200 and "<title>playcap</title>" in html
    assert call(port, "GET", "/static/app.js")[0] == 200
    assert call(port, "GET", "/static/style.css")[0] == 200
    assert call(port, "GET", "/static/../server.py")[0] == 404


def test_state_unconfigured(ui):
    _, port = ui
    status, snap = call(port, "GET", "/api/state")
    assert status == 200 and snap["configured"] is False


def test_bad_host_rejected(ui):
    _, port = ui
    assert call(port, "GET", "/api/state", host="evil.example:80")[0] == 403


def test_post_needs_token_origin_and_json(ui):
    root, port = ui
    tok = token_of(port)
    body = {"show": "X", "output_dir": str(root / "out")}
    assert call(port, "POST", "/api/config", body)[0] == 403                       # no token
    assert call(port, "POST", "/api/config", body, token="wrong")[0] == 403
    assert call(port, "POST", "/api/config", body, token=tok,
                origin="http://evil.example")[0] == 403
    assert call(port, "POST", "/api/config", body, token=tok,
                ctype="text/plain")[0] == 415
    status, res = call(port, "POST", "/api/config", body, token=tok,
                       origin=f"http://127.0.0.1:{port}")
    assert status == 200 and res["ok"] is True
    assert json.loads((root / "config.json").read_text())["show"] == "X"


def test_config_validation_errors_returned(ui):
    root, port = ui
    tok = token_of(port)
    status, res = call(port, "POST", "/api/config", {"show": " "}, token=tok)
    assert status == 200 and res["ok"] is False and "show" in res["errors"]
    assert not (root / "config.json").exists()


def test_setup_info_never_contains_secrets(ui, monkeypatch):
    root, port = ui
    from playcap import detect
    monkeypatch.setattr(detect, "obs_websocket", lambda env=None, platform=None: {
        "url": "ws://127.0.0.1:4455", "password": "SECRET-OBS-PW", "enabled": True})
    (root / "config.json").write_text(json.dumps({"obs_password": "SECRET-CFG-PW"}))
    status, info = call(port, "GET", "/api/setup")
    assert status == 200
    assert set(info["tools"]) == {"chrome", "obs", "ffmpeg", "ffprobe"}
    blob = json.dumps(info)
    assert "SECRET-OBS-PW" not in blob and "SECRET-CFG-PW" not in blob
    assert info["obs"]["has_password"] is True and info["has_obs_password"] is True
    assert any(a["module"] == "playcap.adapters.html5_video" for a in info["adapters"])


def configured_with_queue(root):
    (root / "config.json").write_text(json.dumps({
        "adapter": "playcap.adapters.html5_video", "output_dir": str(root / "out")}))
    (root / "queue.json").write_text(json.dumps([
        {"id": "a", "url": "https://x/a", "title": "A"},
        {"id": "b", "url": "https://x/b", "title": "B"}]))
    (root / "progress.json").write_text(json.dumps(
        {"b": {"status": "failed", "title": "B", "error": "boom"}}))


def test_skip_retry_unskip(ui):
    root, port = ui
    configured_with_queue(root)
    tok = token_of(port)
    assert call(port, "POST", "/api/item/skip", {"id": "a"}, token=tok)[1]["ok"]
    prog = json.loads((root / "progress.json").read_text())
    assert prog["a"]["status"] == "skipped"
    assert call(port, "POST", "/api/item/retry", {"id": "b"}, token=tok)[1]["ok"]
    assert "b" not in json.loads((root / "progress.json").read_text())
    assert call(port, "POST", "/api/item/unskip", {"id": "a"}, token=tok)[1]["ok"]
    assert json.loads((root / "progress.json").read_text()) == {}
    status, res = call(port, "POST", "/api/item/skip", {"id": "zzz"}, token=tok)
    assert res["ok"] is False


def test_item_edits_refused_while_recording(ui, monkeypatch):
    root, port = ui
    configured_with_queue(root)
    tok = token_of(port)
    monkeypatch.setattr(server, "recording_now", lambda root: True)
    status, res = call(port, "POST", "/api/item/skip", {"id": "a"}, token=tok)
    assert res["ok"] is False and "recording" in res["message"].lower()


def test_unknown_job_and_route(ui):
    _, port = ui
    tok = token_of(port)
    status, res = call(port, "POST", "/api/job/start", {"job": "rm -rf"}, token=tok)
    assert res["ok"] is False
    assert call(port, "GET", "/api/nope")[0] == 404
    assert call(port, "POST", "/api/nope", {}, token=tok)[0] == 404


def test_log_endpoint(ui):
    root, port = ui
    (root / "record_run.log").write_text("line1\nline2\n")
    status, res = call(port, "GET", "/api/log?job=record")
    assert status == 200 and "line2" in res["text"]
    assert call(port, "GET", "/api/log?job=../../etc")[0] == 400


def test_enable_websocket_edits_obs_config_only_when_closed(tmp_path):
    from playcap import obs_setup
    env = {"APPDATA": str(tmp_path)}
    f = tmp_path / "obs-studio/plugin_config/obs-websocket/config.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"server_enabled": False, "server_port": 4455,
                             "auth_required": True, "server_password": "keep"}))
    assert obs_setup.enable_websocket(env=env, platform="win32", obs_running=True) is False
    assert obs_setup.enable_websocket(env=env, platform="win32", obs_running=False) is True
    data = json.loads(f.read_text())
    assert data["server_enabled"] is True and data["server_password"] == "keep"
    assert (f.parent / "config.json.playcap-bak").exists()


def test_enable_websocket_creates_config_with_password(tmp_path):
    from playcap import obs_setup
    env = {"APPDATA": str(tmp_path)}
    assert obs_setup.enable_websocket(env=env, platform="win32", obs_running=False) is True
    f = tmp_path / "obs-studio/plugin_config/obs-websocket/config.json"
    data = json.loads(f.read_text())
    assert data["server_enabled"] and data["auth_required"] and len(data["server_password"]) >= 16


def test_setup_reports_effective_adapter_for_old_config(ui):
    root, port = ui
    from playcap import config
    (root / "config.json").write_text(json.dumps({"output_dir": str(root / "out")}))
    status, info = call(port, "GET", "/api/setup")
    assert info["config"]["adapter"] == config.default_adapter_path()
    status, snap = call(port, "GET", "/api/state")
    assert snap["configured"] is True


def test_setup_shows_adapter_defaults_not_placeholders(ui):
    root, port = ui
    (root / "config.json").write_text(json.dumps({
        "adapter": "playcap.adapters.html5_video", "output_dir": str(root / "out")}))
    status, info = call(port, "GET", "/api/setup")
    from playcap import config
    assert info["config"]["show"] == config.DEFAULTS["show"]
    assert info["config"]["queue_source"] == "queue.txt"     # adapter default
