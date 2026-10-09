import json
import signal
import subprocess
import sys
import time

import pytest

from playcap import jobs

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture(autouse=True)
def no_external(monkeypatch):
    # The real machine may be running playcap jobs; tests see none unless asked.
    monkeypatch.setattr(jobs, "external", lambda max_age=15: {})


def test_job_running_elsewhere_blocks_start(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "external", lambda max_age=15: {"optimize": [4242]})
    ok, msg = jobs.start("record", tmp_path, {}, cmd=SLEEPER)
    assert not ok and "Re-compressing" in msg
    ok, msg = jobs.start("optimize", tmp_path, {}, cmd=SLEEPER)
    assert not ok and "outside" in msg


def wait_until(pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def test_start_status_stop(tmp_path):
    ok, msg = jobs.start("queue", tmp_path, {}, cmd=SLEEPER)
    assert ok, msg
    st = jobs.status(tmp_path)["queue"]
    assert st["running"] and st["pid"]
    assert (tmp_path / ".playcap" / "queue.json").exists()

    ok, msg = jobs.start("queue", tmp_path, {}, cmd=SLEEPER)
    assert not ok and "already running" in msg

    jobs.stop("queue", tmp_path)
    if not wait_until(lambda: not jobs.status(tmp_path)["queue"]["running"], 10):
        jobs.kill("queue", tmp_path)
    assert wait_until(lambda: not jobs.status(tmp_path)["queue"]["running"])


def test_kill_is_last_resort_and_cleans_pidfile(tmp_path):
    jobs.start("queue", tmp_path, {}, cmd=SLEEPER)
    jobs.kill("queue", tmp_path)
    assert wait_until(lambda: not jobs.status(tmp_path)["queue"]["running"])
    assert not (tmp_path / ".playcap" / "queue.json").exists()


def test_stale_pidfile_reads_idle_and_is_removed(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    d = tmp_path / ".playcap"
    d.mkdir()
    (d / "record.json").write_text(json.dumps({"pid": p.pid, "started": time.time() - 5}))
    assert jobs.status(tmp_path)["record"]["running"] is False
    assert not (d / "record.json").exists()


def test_pid_reuse_is_not_mistaken_for_our_job(tmp_path):
    d = tmp_path / ".playcap"
    d.mkdir()
    # a live pid (this test process) but a start time far from its real one
    (d / "record.json").write_text(json.dumps({"pid": jobs.os.getpid(), "started": 1000.0}))
    assert jobs.status(tmp_path)["record"]["running"] is False


def test_record_and_optimize_are_exclusive(tmp_path):
    ok, _ = jobs.start("optimize", tmp_path, {}, cmd=SLEEPER)
    assert ok
    try:
        ok, msg = jobs.start("record", tmp_path, {}, cmd=SLEEPER)
        assert not ok and "Re-compressing" in msg
    finally:
        jobs.kill("optimize", tmp_path)


def test_stop_after_current_writes_flag_and_consume_clears_it(tmp_path):
    jobs.start("record", tmp_path, {}, cmd=SLEEPER)
    try:
        msg = jobs.stop("record", tmp_path, mode="after_current")
        assert "after" in msg
        assert jobs.requested(tmp_path, "record", "after_current")
        assert jobs.consume(tmp_path, "record", "after_current") is True
        assert jobs.consume(tmp_path, "record", "after_current") is False
    finally:
        jobs.kill("record", tmp_path)


def test_after_current_only_for_record(tmp_path):
    jobs.start("optimize", tmp_path, {}, cmd=SLEEPER)
    try:
        assert "only" in jobs.stop("optimize", tmp_path, mode="after_current")
    finally:
        jobs.kill("optimize", tmp_path)


def test_stop_now_sets_cooperative_flag(tmp_path):
    jobs.start("record", tmp_path, {}, cmd=SLEEPER)
    try:
        jobs.stop("record", tmp_path, mode="now")
        assert jobs.requested(tmp_path, "record", "now")
    finally:
        jobs.kill("record", tmp_path)


def test_start_clears_old_flags(tmp_path):
    jobs.request(tmp_path, "record", "now")
    jobs.request(tmp_path, "record", "after_current")
    jobs.start("record", tmp_path, {}, cmd=SLEEPER)
    try:
        assert not jobs.requested(tmp_path, "record", "now")
        assert not jobs.requested(tmp_path, "record", "after_current")
    finally:
        jobs.kill("record", tmp_path)


def test_unknown_job(tmp_path):
    ok, msg = jobs.start("nope", tmp_path, {})
    assert not ok


def test_log_written(tmp_path):
    ok, _ = jobs.start("queue", tmp_path, {},
                       cmd=[sys.executable, "-c", "print('hello from job')"])
    assert ok
    assert wait_until(lambda: "hello from job" in jobs.tail(tmp_path, "queue"))


@pytest.mark.skipif(not hasattr(signal, "SIGBREAK"), reason="Windows only")
def test_graceful_signals_maps_sigbreak():
    old = signal.getsignal(signal.SIGBREAK)
    try:
        jobs.graceful_signals()
        assert signal.getsignal(signal.SIGBREAK) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGBREAK, old)


def test_parse_external_matches_our_modules():
    rows = [
        (101, r'C:\Python\python.exe -u -m playcap.recorder'),
        (102, r'python -u optimize.py'),
        (103, r'"C:\Python\python.exe" record_all.py --limit 1'),
        (104, r'python some_other.py'),
        (105, r'python -m playcap.ui'),
    ]
    assert jobs.parse_external(rows) == {"record": [101, 103], "optimize": [102]}


def test_job_can_import_playcap_from_any_folder(tmp_path):
    ok, _ = jobs.start("queue", tmp_path, {}, cmd=[
        sys.executable, "-c", "import playcap, sys; print('imported', playcap.__name__)"])
    assert ok
    assert wait_until(lambda: "imported playcap" in jobs.tail(tmp_path, "queue"))


def test_start_appends_args(tmp_path):
    ok, _ = jobs.start("queue", tmp_path, {}, cmd=[sys.executable, "-c",
                       "import sys; print('ARGS', sys.argv[1:])"], args=["--id", "42"])
    assert ok
    assert wait_until(lambda: "ARGS ['--id', '42']" in jobs.tail(tmp_path, "queue"))


def test_consume_survives_a_locked_flag(tmp_path, monkeypatch):
    # WinError 32: another process holds the flag file for a moment.
    jobs.request(tmp_path, "record", "now")
    real_unlink = type(tmp_path).unlink
    calls = []

    def flaky(self, *a, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(32, "in use")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(type(tmp_path), "unlink", flaky)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    assert jobs.consume(tmp_path, "record", "now") is True
    assert not jobs.requested(tmp_path, "record", "now")
    jobs.clear_flags(tmp_path, "record")      # must not raise either
