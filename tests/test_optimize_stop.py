import sys
import threading
import time

import pytest

from playcap import jobs, optimize

FAKE_FFMPEG = """
import sys, time
out = sys.argv[-1]
with open(out, "w") as f:
    f.write("partial")
    f.flush()
    time.sleep(60)
"""


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(FAKE_FFMPEG)
    if sys.platform.startswith("win"):
        launcher = tmp_path / "ffmpeg.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        launcher = tmp_path / "ffmpeg"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    monkeypatch.setattr(optimize, "FFMPEG", str(launcher))
    monkeypatch.setattr(optimize, "audio_args", lambda src: ["-c:a", "copy"])
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_stop_now_interrupts_encode_and_removes_sidecar(fake_ffmpeg):
    src = fake_ffmpeg / "S01E01 - A's talk.mp4"
    src.write_text("source")
    dst = src
    timer = threading.Timer(2.0, lambda: jobs.request(fake_ffmpeg, "optimize", "now"))
    timer.start()
    t = time.time()
    with pytest.raises(KeyboardInterrupt):
        optimize.encode(src, dst, 24, "veryfast")
    assert time.time() - t < 30
    assert not dst.with_suffix(optimize.PART_SUFFIX).exists()
    assert src.read_text() == "source"


def test_main_stop_leaves_state_unmarked(fake_ffmpeg, monkeypatch):
    calls = {}
    monkeypatch.setattr(optimize, "_run", lambda argv=None: (_ for _ in ()).throw(KeyboardInterrupt()))
    jobs.request(fake_ffmpeg, "optimize", "now")
    optimize.main([])          # must not raise
    assert not jobs.requested(fake_ffmpeg, "optimize", "now")
    assert calls == {}
