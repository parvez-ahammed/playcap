import json
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


def test_loose_root_videos_are_never_episodes(tmp_path, monkeypatch):
    # A raw OBS file in the library root (crash leftover, smoke test) was
    # picked up as the next episode on a live run.
    lib = tmp_path / "lib"
    season = lib / "Show" / "Season 01"
    season.mkdir(parents=True)
    (lib / "2026-10-06 13-54-14.mkv").write_bytes(b"x")
    (lib / "stray.mp4").write_bytes(b"x")
    (season / "S01E01 - Real.mkv").write_bytes(b"x")
    (lib / "_partial").mkdir()
    (lib / "_partial" / "old.mkv").write_bytes(b"x")
    monkeypatch.setattr(optimize, "_setup", lambda: None)
    monkeypatch.setattr(optimize, "LIB", lib)
    monkeypatch.setattr(optimize, "ARCHIVE", tmp_path / "lib_originals")
    monkeypatch.setattr(optimize, "finished", lambda state: set())
    # Every file is a known recording here, so only the unfiled/partial rules filter.
    names = {"2026-10-06 13-54-14", "stray", "s01e01 - real", "old"}
    monkeypatch.setattr(optimize, "recordings", lambda: (set(), names))
    assert [p.name for p in optimize.sources({})] == ["S01E01 - Real.mkv"]
    # A root .mp4 with a real name is a library file (custom layouts may file
    # into the root); only OBS's own timestamp names count as unfiled.
    assert [p.name for p in optimize.loose()] == ["2026-10-06 13-54-14.mkv"]


def test_crf_recordings_are_not_encoded_again(tmp_path, monkeypatch, capsys):
    rec = tmp_path / "S01E01 - Talk.mp4"
    rec.write_bytes(b"\x00" * 64 + b"x264 - options: rc=crf mbtree=1 crf=24.0 qcomp=0.60" + b"\x00" * 64)
    monkeypatch.setattr(optimize, "_setup", lambda: None)
    monkeypatch.setattr(optimize, "STATE", tmp_path / "optimize.json")
    monkeypatch.setattr(optimize, "loose", lambda: [])
    monkeypatch.setattr(optimize, "sources", lambda state: [rec])
    monkeypatch.setattr(optimize, "in_library", lambda src: src)

    def no_encode(*a, **kw):
        raise AssertionError("a CRF 24 recording must not be re-encoded at CRF 24")

    monkeypatch.setattr(optimize, "encode", no_encode)
    optimize._run([])
    assert "recorded at CRF 24 already" in capsys.readouterr().out


def _lib(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(optimize, "_setup", lambda: None)
    monkeypatch.setattr(optimize, "LIB", lib)
    monkeypatch.setattr(optimize, "ARCHIVE", tmp_path / "lib_originals")
    return lib


def test_videos_playcap_did_not_record_are_left_alone(tmp_path, monkeypatch):
    # output_dir pointed at a folder that already holds personal videos.
    lib = _lib(tmp_path, monkeypatch)
    (lib / "holiday.mkv").write_bytes(b"x")
    (lib / "01 - Talk.mkv").write_bytes(b"x")
    monkeypatch.setattr(optimize, "recordings", lambda: (set(), {"01 - talk"}))
    assert [p.name for p in optimize.sources({})] == ["01 - Talk.mkv"]


def test_archive_never_replaces_an_original(tmp_path, monkeypatch):
    lib = _lib(tmp_path, monkeypatch)
    src = lib / "01 - Talk.mp4"
    src.write_text("second generation")
    taken = tmp_path / "lib_originals" / "01 - Talk.mp4"
    taken.parent.mkdir()
    taken.write_text("true original")
    with pytest.raises(optimize.Refused):
        optimize.archive(src)
    assert taken.read_text() == "true original"
    assert src.read_text() == "second generation"


def test_existing_destination_that_is_not_ours_is_not_replaced(tmp_path, monkeypatch, capsys):
    lib = _lib(tmp_path, monkeypatch)
    old = lib / "03 - T.mkv"
    old.write_text("old take")
    newer = lib / "03 - T.mp4"
    newer.write_text("new take")
    monkeypatch.setattr(optimize, "STATE", tmp_path / "optimize.json")
    monkeypatch.setattr(optimize, "loose", lambda: [])
    monkeypatch.setattr(optimize, "sources", lambda state: [old])
    monkeypatch.setattr(optimize, "duration", lambda p: 100.0 if p == old else 50.0)
    monkeypatch.setattr(optimize.record_quality, "file_crf", lambda p: None)

    def no_encode(*a, **kw):
        raise AssertionError("must not encode over a file it did not make")

    monkeypatch.setattr(optimize, "encode", no_encode)
    optimize._run([])
    assert newer.read_text() == "new take" and old.read_text() == "old take"
    state = json.loads((tmp_path / "optimize.json").read_text(encoding="utf-8"))
    assert state["03 - T"]["status"] == "failed"
