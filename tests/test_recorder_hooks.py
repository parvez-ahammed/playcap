import base64
import json
import struct
import time

import pytest

from playcap import jobs, recorder
from playcap.adapters.base import Item
from playcap.obs_client import bmp_mean_luma


def bmp(width, height, rgb, bpp=24):
    row = bytes([rgb[2], rgb[1], rgb[0]] + ([255] if bpp == 32 else [])) * width
    pad = (4 - len(row) % 4) % 4
    pixels = (row + b"\0" * pad) * height
    header = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, bpp, 0, len(pixels), 0, 0, 0, 0)
    return header + info + pixels


@pytest.mark.parametrize("bpp", [24, 32])
def test_bmp_luma_black_white_grey(bpp):
    assert bmp_mean_luma(bmp(5, 3, (0, 0, 0), bpp)) == pytest.approx(0)
    assert bmp_mean_luma(bmp(5, 3, (255, 255, 255), bpp)) == pytest.approx(255, abs=0.5)
    assert bmp_mean_luma(bmp(7, 2, (100, 100, 100), bpp)) == pytest.approx(100, abs=0.5)


def test_bmp_luma_rejects_garbage():
    assert bmp_mean_luma(b"not a bmp") is None


def test_screenshot_luma_uses_data_url():
    class FakeObs:
        def request(self, kind, data=None, timeout=20):
            assert kind == "GetSourceScreenshot" and data["imageFormat"] == "bmp"
            return {"imageData": "data:image/bmp;base64,"
                    + base64.b64encode(bmp(4, 4, (255, 255, 255))).decode()}
    from playcap.obs_client import Obs
    assert Obs.screenshot_luma(FakeObs(), "scene") == pytest.approx(255, abs=0.5)


def test_write_and_clear_now(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    item = Item(id="7", title="Lecture it's 7", url="u")
    recorder.write_now(item, {"t": 12.5, "w": 1920, "h": 1080}, 600.0, luma=42.0,
                       started=time.time() - 30)
    data = json.loads((tmp_path / ".playcap" / "now.json").read_text())
    assert data["title"] == "Lecture it's 7" and data["t"] == 12.5
    assert data["duration"] == 600.0 and data["luma"] == 42.0 and data["id"] == "7"
    assert data["elapsed"] >= 29 and data["updated_at"] > 0
    recorder.clear_now()
    assert not (tmp_path / ".playcap" / "now.json").exists()
    recorder.clear_now()   # idempotent


def test_nap_raises_on_stop_now(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jobs.request(tmp_path, "record", "now")
    with pytest.raises(KeyboardInterrupt):
        recorder.nap(5)


def test_nap_sleeps_without_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    t = time.time()
    recorder.nap(0.3)
    assert time.time() - t >= 0.25


def test_stop_after_current_consumed_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert recorder.stop_after_current() is False
    jobs.request(tmp_path, "record", "after_current")
    assert recorder.stop_after_current() is True
    assert recorder.stop_after_current() is False


def test_black_watch_times_only_unbroken_black():
    w = recorder.BlackWatch(threshold=8)
    assert w.update(120, 0) == 0
    assert w.update(2.9, 10) == 0            # black starts here
    assert w.update(0.0, 40) == 30
    assert w.update(None, 50) == 40          # unreadable: run continues
    assert w.update(90, 60) == 0             # real picture resets it
    assert w.update(None, 70) == 0           # unreadable does not start a run
    assert w.update(1, 80) == 0
    assert w.update(1, 200) == 120


def test_preflight_passes_on_picture_and_fails_on_black(monkeypatch):
    monkeypatch.setattr(recorder.time, "sleep", lambda s: None)
    seq = iter([0.0, 2.0, 120.0])
    monkeypatch.setattr(recorder, "program_luma", lambda obs: next(seq))
    assert recorder.preflight(None, seconds=5) == 120.0
    monkeypatch.setattr(recorder, "program_luma", lambda obs: 0.0)
    with pytest.raises(recorder.ItemFailed, match="black before recording"):
        recorder.preflight(None, seconds=3)
    monkeypatch.setattr(recorder, "program_luma", lambda obs: None)   # OBS cannot say
    assert recorder.preflight(None, seconds=3) is None


def test_wait_playable_stops_at_ready_state_3(monkeypatch):
    monkeypatch.setattr(recorder.time, "sleep", lambda s: None)
    states = iter([{"found": True, "readyState": 1}, {"found": True, "readyState": 2},
                   {"found": True, "readyState": 4}, {"found": True, "readyState": 0}])
    monkeypatch.setattr(recorder, "state", lambda p: next(states))
    assert recorder.wait_playable(None, seconds=5)["readyState"] == 4
