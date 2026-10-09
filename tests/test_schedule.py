import json
import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest

from playcap import jobs, schedule


# --- next_run ----------------------------------------------------------------------
def at(h, m, day=10):
    return datetime(2026, 10, day, h, m)


def test_off_and_invalid_mean_no_run():
    assert schedule.next_run({}, at(1, 0)) is None
    assert schedule.next_run({"schedule_mode": "off"}, at(1, 0)) is None
    assert schedule.next_run({"schedule_mode": "daily", "schedule_time": "25:00"}, at(1, 0)) is None
    assert schedule.next_run({"schedule_mode": "every", "schedule_every_hours": 0,
                              "schedule_time": "00:00"}, at(1, 0)) is None


def test_daily():
    cfg = {"schedule_mode": "daily", "schedule_time": "03:00"}
    assert schedule.next_run(cfg, at(1, 0)) == at(3, 0)
    assert schedule.next_run(cfg, at(3, 0)) == at(3, 0, day=11)      # strictly after now
    assert schedule.next_run(cfg, at(23, 59)) == at(3, 0, day=11)


def test_every_n_hours_is_anchored_to_the_time():
    cfg = {"schedule_mode": "every", "schedule_every_hours": 6, "schedule_time": "03:00"}
    assert schedule.next_run(cfg, at(1, 0)) == at(3, 0)
    assert schedule.next_run(cfg, at(3, 0)) == at(9, 0)
    assert schedule.next_run(cfg, at(10, 30)) == at(15, 0)
    assert schedule.next_run(cfg, at(22, 0)) == at(3, 0, day=11)
    cfg5 = {**cfg, "schedule_every_hours": 5, "schedule_time": "00:00"}
    assert schedule.next_run(cfg5, at(23, 0)) == at(1, 0, day=11)     # 20:00 + 5 h


def test_validate():
    assert schedule.validate({"schedule_mode": "daily", "schedule_time": "07:30"}) == {}
    bad = schedule.validate({"schedule_mode": "weekly", "schedule_time": "7.30",
                             "schedule_every_hours": 24})
    assert set(bad) == {"schedule_mode", "schedule_time", "schedule_every_hours"}
    assert "schedule_every_hours" in schedule.validate({"schedule_every_hours": True})


# --- Task Scheduler (schtasks is always mocked) -------------------------------------------
class FakeRun:
    def __init__(self, code=0, out=""):
        self.calls, self.code, self.out = [], code, out

    def __call__(self, args, **kw):
        self.calls.append(args)
        return SimpleNamespace(returncode=self.code, stdout=self.out, stderr="")


def test_create_args():
    tr = '"py" "x.pyw"'
    daily = schedule.create_args({"schedule_mode": "daily", "schedule_time": "3:05"}, "T", tr)
    assert daily == ["schtasks", "/Create", "/TN", "T", "/TR", tr, "/F",
                     "/SC", "DAILY", "/ST", "03:05"]
    every = schedule.create_args({"schedule_mode": "every", "schedule_time": "00:00",
                                  "schedule_every_hours": 4}, "T", tr)
    assert every[-6:] == ["/SC", "HOURLY", "/MO", "4", "/ST", "00:00"]
    assert schedule.create_args({"schedule_mode": "off"}, "T", tr) is None


def test_register_writes_launcher_and_calls_schtasks(tmp_path):
    run = FakeRun()
    ok, msg = schedule.register(tmp_path, {"schedule_mode": "daily", "schedule_time": "02:00"},
                                run=run, platform="win32")
    assert ok, msg
    (args,) = run.calls
    assert args[:2] == ["schtasks", "/Create"] and "/RU" not in args     # current user, no admin
    assert args[args.index("/TN") + 1] == schedule.task_name(tmp_path)
    launcher = tmp_path / jobs.STATE_DIR / schedule.LAUNCHER
    assert str(launcher) in args[args.index("/TR") + 1]
    text = launcher.read_text(encoding="utf-8")
    assert "schedule.task_entry()" in text and repr(str(tmp_path.resolve())) in text
    compile(text, "launcher", "exec")


def test_register_refuses_off_and_non_windows(tmp_path):
    run = FakeRun()
    assert not schedule.register(tmp_path, {"schedule_mode": "off"}, run=run, platform="win32")[0]
    assert not schedule.register(tmp_path, {"schedule_mode": "daily"}, run=run, platform="linux")[0]
    assert run.calls == []


def test_register_reports_schtasks_error(tmp_path):
    run = FakeRun(code=1, out="ERROR: Access is denied.")
    ok, msg = schedule.register(tmp_path, {"schedule_mode": "daily", "schedule_time": "02:00"},
                                run=run, platform="win32")
    assert not ok and "Access is denied" in msg


def test_unregister_and_status(tmp_path):
    name = schedule.task_name(tmp_path)
    run = FakeRun(out=f'"\\{name}","10/11/2026 3:00:00 AM","Ready"\n')
    st = schedule.task_status(tmp_path, run=run, platform="win32")
    assert st["registered"] and st["next_run"] == "10/11/2026 3:00:00 AM"
    assert run.calls[0][:2] == ["schtasks", "/Query"]
    missing = FakeRun(code=1, out="ERROR: The system cannot find the file specified.")
    assert not schedule.task_status(tmp_path, run=missing, platform="win32")["registered"]
    assert schedule.unregister(tmp_path, run=missing, platform="win32")[0]
    gone = FakeRun()
    assert schedule.unregister(tmp_path, run=gone, platform="win32") == (True, "Scheduled task removed.")
    assert gone.calls[0][:4] == ["schtasks", "/Delete", "/TN", name]


def test_task_name_is_per_folder(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert schedule.task_name(a) != schedule.task_name(b)
    assert schedule.task_name(a).startswith("playcap a ")


# --- a cycle never doubles up on a recording --------------------------------------------------
@pytest.fixture
def configured(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"output_dir": "rec"}))
    monkeypatch.setattr(jobs, "external", lambda max_age=15: {})
    return tmp_path


def _status(running=()):
    return {n: {"running": n in running, "pid": None} for n in jobs.NAMES}


def test_cycle_skips_while_recording(configured, monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "status", lambda root: _status({"record"}))
    monkeypatch.setattr(jobs, "start", lambda *a, **k: started.append(a) or (True, ""))
    assert schedule.run_cycle(configured) == "busy"
    assert started == []


def test_cycle_skips_for_external_recorder(configured, monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "status", lambda root: _status())
    monkeypatch.setattr(jobs, "external", lambda max_age=15: {"record": [1234]})
    monkeypatch.setattr(jobs, "start", lambda *a, **k: started.append(a) or (True, ""))
    assert schedule.run_cycle(configured) == "busy"
    assert started == []


def test_cycle_refreshes_queue_then_records(configured, monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "status", lambda root: _status())
    monkeypatch.setattr(jobs, "start", lambda name, root, cfg, cmd=None, args=():
                        started.append((name, cmd)) or (True, "started"))
    monkeypatch.setattr("playcap.browser.port_open", lambda port: True)
    assert schedule.run_cycle(configured, wait=True) == "ran"
    assert [s[0] for s in started] == ["queue", "record"]
    assert started[1][1][-2:] == ["-m", "playcap.recorder"]


def test_cycle_not_configured(tmp_path):
    assert schedule.run_cycle(tmp_path) == "not configured"


def test_loop_exits_when_off_or_stopped(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"schedule_mode": "off"}))
    assert schedule.loop(tmp_path) == 0
    (tmp_path / "config.json").write_text(json.dumps(
        {"schedule_mode": "daily", "schedule_time": "03:00"}))
    ran = []
    monkeypatch.setattr(schedule, "run_cycle", lambda *a, **k: ran.append(1))
    jobs.request(tmp_path, "schedule", "now")
    assert schedule.loop(tmp_path, sleep=lambda s: None) == 0
    assert ran == [] and not jobs.requested(tmp_path, "schedule", "now")


def test_main_never_runs_schtasks_for_next(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("schtasks called"))
    (tmp_path / "config.json").write_text(json.dumps(
        {"schedule_mode": "daily", "schedule_time": "03:00"}))
    assert schedule.main(["--next", "--root", str(tmp_path)]) == 0
    assert "every day at 03:00" in capsys.readouterr().out
