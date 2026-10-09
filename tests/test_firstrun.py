"""Try it now (playcap.demo), winget installs (playcap.install), first-run
timestamps (playcap.firstrun), the packaged demo and --version."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from playcap import config, demo, firstrun, install, jobs, settings
from playcap.adapters.html5_video import read_queue_source

from test_server import call, token_of, ui  # noqa: F401  (fixture)


def wait_until(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(jobs, "external", lambda max_age=15: {})
    monkeypatch.setattr(install, "_cache", {})
    # The demo's backend choice must not depend on this machine's ffmpeg.
    monkeypatch.setattr(demo, "_ffmpeg_capture_ok", lambda cfg: False)


IDLE = {"running": False, "pid": None, "started": None, "stopping": False, "stopping_after": False}


# ---------------------------------------------------------------- packaged demo
def test_demo_files_ship_in_the_package():
    for name in demo.FILES:
        assert (demo.DATA / name).is_file(), name
    size = (demo.DATA / "test-video.mp4").stat().st_size
    assert 10_000 < size < 1_000_000      # small enough for a wheel
    items = read_queue_source(demo.DATA / "queue.txt")
    assert len(items) == 1 and items[0]["url"].startswith("file:")
    assert items[0]["url"].endswith("/index.html")


def test_example_page_points_at_the_packaged_video():
    repo = Path(__file__).resolve().parent.parent
    page = repo / "examples" / "demo" / "index.html"
    src = page.read_text(encoding="utf-8").split('<video src="', 1)[1].split('"', 1)[0]
    assert (page.parent / src).resolve() == (demo.DATA / "test-video.mp4").resolve()


def test_pyproject_ships_demo_data():
    repo = Path(__file__).resolve().parent.parent
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert '"playcap.demo" = ["*.html", "*.mp4", "*.txt"]' in text


# ---------------------------------------------------------------- demo isolation
def test_prepare_keeps_the_demo_apart_from_the_real_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.detect, "resolve", lambda tool, cfg: f"C:/fake/{tool}.exe")
    real = {"output_dir": "lib", "chrome_debug_port": 9333, "queue_file": "queue.json"}
    (tmp_path / "config.json").write_text(json.dumps(real))
    (tmp_path / "progress.json").write_text('{"x": {"status": "done"}}')
    cfg_path, cfg = demo.prepare(tmp_path, "obs")
    d = (tmp_path / "playcap-demo").resolve()
    assert cfg_path == tmp_path / "playcap-demo" / "config.json"
    for key in ("queue_file", "progress_file", "output_dir", "optimize_state"):
        assert Path(cfg[key]).is_absolute() and Path(cfg[key]).parent == d
    assert cfg["chrome_debug_port"] == 9333               # machine settings carry over
    assert cfg["obs_exe"] == "C:/fake/obs.exe"
    assert cfg["capture_backend"] == "obs"
    assert json.loads((tmp_path / "config.json").read_text()) == real   # untouched
    assert json.loads((tmp_path / "progress.json").read_text()) == {"x": {"status": "done"}}
    assert (d / "page" / "test-video.mp4").is_file()
    queue = json.loads(Path(cfg["queue_file"]).read_text())
    assert queue[0]["url"] == (d / "page" / "index.html").as_uri()
    assert json.loads(Path(cfg["progress_file"]).read_text()) == {}


def test_config_load_honours_playcap_config(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps({"output_dir": "demo-out", "chrome_debug_port": 9555,
                                 "obs_ws_url": "ws://x", "obs_password": "p"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(other))
    monkeypatch.setattr(config, "_cache", {})
    cfg, _ = config.load()
    assert cfg["output_dir"] == "demo-out" and cfg["chrome_debug_port"] == 9555


def test_jobs_start_passes_extra_env(tmp_path):
    ok, _ = jobs.start("queue", tmp_path, {}, env={"PLAYCAP_CONFIG": "X/demo.json"}, cmd=[
        sys.executable, "-c", "import os; print('CFG', os.environ.get('PLAYCAP_CONFIG'))"])
    assert ok
    assert wait_until(lambda: "CFG X/demo.json" in jobs.tail(tmp_path, "queue"))


def _fake_jobs(monkeypatch, started):
    def start(name, root, cfg, cmd=None, args=(), env=None):
        started.append((name, env))
        return True, "started"
    monkeypatch.setattr(jobs, "start", start)

    def status(root):
        st = {n: dict(IDLE) for n in jobs.NAMES}
        if any(n == "record" for n, _ in started):
            st["record"].update(running=True, pid=4242)
        return st
    monkeypatch.setattr(jobs, "status", status)


def test_start_runs_browser_obs_then_the_ordinary_record_job(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.detect, "report",
                        lambda cfg: {t: {"ok": True, "path": "x"} for t in ("chrome", "obs")})
    monkeypatch.setattr(demo.detect, "resolve", lambda tool, cfg: None)
    from playcap import obs_setup
    monkeypatch.setattr(obs_setup, "is_obs_running", lambda: False)
    started, calls = [], []
    _fake_jobs(monkeypatch, started)
    ports = iter([False, False, True])
    ok, _ = demo.start(tmp_path, lambda: calls.append("launch") or (True, "ok"),
                       lambda: calls.append("setup") or (True, "ok"),
                       port_open=lambda p: next(ports, True), background=False)
    assert ok
    assert [n for n, _ in started] == ["browser", "record"]
    env = started[-1][1]
    assert env == {config.CONFIG_ENV: str(tmp_path / "playcap-demo" / "config.json")}
    assert calls == ["launch", "setup"]
    st = demo.status(tmp_path, report={"chrome": {"ok": True}, "obs": {"ok": True}})
    assert st["phase"] == "recording" and st["running"]


def test_start_refuses_without_obs(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.detect, "report",
                        lambda cfg: {"chrome": {"ok": True}, "obs": {"ok": False}})
    started = []
    _fake_jobs(monkeypatch, started)
    ok, msg = demo.start(tmp_path, None, None, background=False)
    assert not ok and "OBS" in msg and "ffmpeg" in msg and not started


def test_demo_records_with_ffmpeg_when_obs_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.detect, "report", lambda cfg: {
        "chrome": {"ok": True}, "obs": {"ok": False}, "ffmpeg": {"ok": True}})
    monkeypatch.setattr(demo.detect, "resolve", lambda tool, cfg: f"C:/fake/{tool}.exe"
                        if tool != "obs" else None)
    monkeypatch.setattr(demo, "_ffmpeg_capture_ok", lambda cfg: True)
    from playcap import obs_setup
    monkeypatch.setattr(obs_setup, "is_obs_running",
                        lambda: pytest.fail("the ffmpeg demo must not look for OBS"))
    started, calls = [], []
    _fake_jobs(monkeypatch, started)
    ok, msg = demo.start(tmp_path, lambda: calls.append("launch") or (True, ""),
                         lambda: calls.append("setup") or (True, ""),
                         port_open=lambda p: True, background=False)
    assert ok
    assert [n for n, _ in started] == ["record"] and calls == []       # no OBS steps
    cfg = json.loads((tmp_path / "playcap-demo" / "config.json").read_text())
    assert cfg["capture_backend"] == "ffmpeg" and cfg["ffmpeg"] == "C:/fake/ffmpeg.exe"
    st = demo.status(tmp_path, report={"chrome": {"ok": True}, "ffmpeg": {"ok": True}})
    assert st["backend"] == "ffmpeg" and st["needs"] == [] and st["phase"] == "recording"


def test_demo_backend_choice():
    report = {"chrome": {"ok": True}, "obs": {"ok": True}, "ffmpeg": {"ok": False}}
    assert demo.choose_backend({}, report) == "obs"
    assert demo.choose_backend({"capture_backend": "ffmpeg"}, report) == "ffmpeg"
    assert demo.choose_backend({}, {"obs": {"ok": False}}) == "obs"   # neither: ask for OBS


def test_demo_needs_follow_the_chosen_backend(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"capture_backend": "ffmpeg"}))
    report = {"chrome": {"ok": True}, "obs": {"ok": True}, "ffmpeg": {"ok": False}}
    assert demo.plan(tmp_path, report) == ("ffmpeg", ["ffmpeg"])
    (tmp_path / "config.json").write_text(json.dumps({"capture_backend": "obs"}))
    assert demo.plan(tmp_path, {"chrome": {"ok": False}, "obs": {"ok": False}}) == (
        "obs", ["chrome", "obs"])


def test_start_refuses_while_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.detect, "report",
                        lambda cfg: {t: {"ok": True} for t in ("chrome", "obs")})
    monkeypatch.setattr(jobs, "status", lambda root: {
        n: dict(IDLE, running=(n == "record"), pid=1 if n == "record" else None) for n in jobs.NAMES})
    ok, msg = demo.start(tmp_path, None, None, background=False)
    assert not ok and "Recording" in msg
    assert not (tmp_path / "playcap-demo").exists()      # nothing prepared


def test_obs_setup_failure_is_reported_and_nothing_records(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.detect, "report",
                        lambda cfg: {t: {"ok": True} for t in ("chrome", "obs")})
    from playcap import obs_setup
    monkeypatch.setattr(obs_setup, "is_obs_running", lambda: True)
    started = []
    _fake_jobs(monkeypatch, started)
    demo.start(tmp_path, lambda: (True, ""), lambda: (False, "OBS's websocket server is off."),
               port_open=lambda p: True, background=False)
    assert not started
    st = demo.status(tmp_path, report={})
    assert st["phase"] == "failed" and "websocket" in st["error"]


def test_status_reads_the_demo_result(tmp_path, monkeypatch):
    monkeypatch.setattr(demo.detect, "resolve", lambda tool, cfg: None)
    monkeypatch.setattr(jobs, "status", lambda root: {n: dict(IDLE) for n in jobs.NAMES})
    _, cfg = demo.prepare(tmp_path, "obs")
    demo._set_state(tmp_path, phase="recording", record_pid=4242)
    Path(cfg["progress_file"]).write_text(json.dumps(
        {"abc": {"status": "done", "file": "C:/x/01 - Demo.mp4", "title": "Demo"}}))
    st = demo.status(tmp_path, report={"chrome": {"ok": True}, "obs": {"ok": True}})
    assert st["phase"] == "done" and st["file"].endswith("Demo.mp4") and not st["running"]


def test_stop_uses_the_ordinary_stop_flag(tmp_path, monkeypatch):
    demo._set_state(tmp_path, phase="recording", record_pid=4242)
    monkeypatch.setattr(jobs, "status", lambda root: {
        n: dict(IDLE, running=(n == "record"), pid=4242 if n == "record" else None)
        for n in jobs.NAMES})
    seen = []
    monkeypatch.setattr(jobs, "stop", lambda name, root, mode="now": seen.append((name, mode)) or "ok")
    ok, _ = demo.stop(tmp_path)
    assert ok and seen == [("record", "now")]


# ---------------------------------------------------------------- winget
def test_install_commands_are_a_fixed_allowlist():
    assert install.command("obs") == ["winget", "install", "--id", "OBSProject.OBSStudio", "-e",
                                      "--accept-source-agreements", "--accept-package-agreements"]
    assert install.command("ffmpeg")[3] == "Gyan.FFmpeg"
    assert install.command("ffprobe")[3] == "Gyan.FFmpeg"
    assert install.command("chrome")[3] == "Google.Chrome"
    for bad in ("", "evil", "OBSProject.OBSStudio", "obs; calc", "../obs"):
        assert install.command(bad) is None


def test_winget_detection(monkeypatch):
    assert install.available(platform="linux") is False
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "v1.9", "")
    assert install.available(platform="win32", run=run) is True
    assert seen == [["winget", "--version"]]

    monkeypatch.setattr(install, "_cache", {})

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    assert install.available(platform="win32", run=missing) is False


def test_install_start_runs_the_install_job(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "available", lambda: True)
    got = []
    monkeypatch.setattr(jobs, "start", lambda name, root, cfg, cmd=None, args=(), env=None:
                        got.append((name, cmd)) or (True, "started"))
    ok, _ = install.start(tmp_path, "obs")
    assert ok and got == [("install", install.command("obs"))]
    ok, msg = install.start(tmp_path, "notepad")
    assert not ok and len(got) == 1


def test_install_without_winget_points_at_the_download(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "available", lambda: False)
    ok, msg = install.start(tmp_path, "obs")
    assert not ok and "download" in msg


def test_install_endpoint_is_guarded_and_allowlisted(ui, monkeypatch):
    root, port = ui
    monkeypatch.setattr(install, "available", lambda: True)
    got = []
    monkeypatch.setattr(jobs, "start", lambda name, r, cfg, cmd=None, args=(), env=None:
                        got.append(cmd) or (True, "started"))
    assert call(port, "POST", "/api/install", {"tool": "obs"})[0] == 403          # no token
    tok = token_of(port)
    assert call(port, "POST", "/api/install", {"tool": "obs"}, token=tok,
                origin="http://evil.example")[0] == 403
    status, r = call(port, "POST", "/api/install",
                     {"tool": "calc", "cmd": ["calc.exe"], "id": "Evil.Pkg"}, token=tok)
    assert status == 200 and r["ok"] is False and got == []
    status, r = call(port, "POST", "/api/install", {"tool": "ffprobe", "cmd": "x"}, token=tok)
    assert r["ok"] is True and got == [install.command("ffmpeg")]


def test_get_install_and_demo(ui, monkeypatch):
    root, port = ui
    monkeypatch.setattr(install, "available", lambda: False)
    status, r = call(port, "GET", "/api/install")
    assert status == 200 and r["winget"] is False and r["running"] is False
    assert r["packages"]["obs"] == "OBSProject.OBSStudio"
    status, r = call(port, "GET", "/api/demo")
    assert status == 200 and r["phase"] == "idle" and r["running"] is False
    assert r["folder"].endswith("recordings")


def test_demo_start_endpoint_needs_the_token(ui, monkeypatch):
    root, port = ui
    assert call(port, "POST", "/api/demo/start", {})[0] == 403
    monkeypatch.setattr(demo.detect, "report",
                        lambda cfg: {"chrome": {"ok": False}, "obs": {"ok": False}})
    status, r = call(port, "POST", "/api/demo/start", {}, token=token_of(port))
    assert status == 200 and r["ok"] is False and "Chrome and OBS" in r["message"]


# ---------------------------------------------------------------- first-run timing
def test_firstrun_marks_once_and_measures(tmp_path):
    assert firstrun.mark(tmp_path, "ui_first_started", now=100.0)
    assert not firstrun.mark(tmp_path, "ui_first_started", now=200.0)
    assert firstrun.mark(tmp_path, "first_recording", now=190.5)
    data = json.loads((tmp_path / ".playcap" / "first_run.json").read_text())
    assert data == {"ui_first_started": 100.0, "first_recording": 190.5,
                    "seconds_to_first_recording": 90.5}


def test_firstrun_never_raises(tmp_path):
    blocker = tmp_path / ".playcap"
    blocker.write_text("a file where the folder should be")
    assert firstrun.mark(tmp_path, "first_recording") is False


# ---------------------------------------------------------------- --version
def test_version_flag():
    out = subprocess.run([sys.executable, "-m", "playcap", "--version"],
                         capture_output=True, text=True, timeout=30,
                         cwd=str(Path(__file__).resolve().parent.parent))
    assert out.returncode == 0
    assert out.stdout.startswith("playcap ") and out.stdout.split()[1][0].isdigit()
