"""playcap.capture: argument building, backend choice, settings, stop handling.
ffmpeg is never run: Popen and run are fakes."""
import subprocess
import time
from pathlib import Path

import pytest

from playcap import capture, record_quality, settings
from playcap.obs_client import ObsError

CAPS = {"ddagrab": True, "gdigrab": True, "dshow": True, "version": "ffmpeg 7",
        "encoders": ["libx264", "h264_nvenc"], "audio_devices": ["Microphone (USB)", "Stereo Mix (Realtek)"]}
Q = record_quality.effective({})


# --- argument building ------------------------------------------------------------
def test_libx264_crf_and_cbr():
    assert capture.encoder_args("libx264", Q, 30) == [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-g", "60"]
    q = record_quality.effective({"record_mode": "bitrate", "video_bitrate_kbps": 3000,
                                  "keyframe_seconds": 4})
    assert capture.encoder_args("libx264", q, 25) == [
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", "3000k", "-maxrate", "3000k",
        "-bufsize", "6000k", "-g", "100"]


@pytest.mark.parametrize("enc,expect", [
    ("h264_nvenc", ["-rc", "vbr", "-cq", "20", "-b:v", "0"]),
    ("h264_qsv", ["-global_quality", "20", "-look_ahead", "0"]),
    ("h264_amf", ["-rc", "cqp", "-qp_i", "20", "-qp_p", "20"]),
])
def test_hardware_quality_mapping(enc, expect):
    args = capture.encoder_args(enc, record_quality.effective({"record_crf": 20}), 30)
    assert args[:2] == ["-c:v", enc] and args[2:-2] == expect and args[-2:] == ["-g", "60"]


def test_unknown_encoder_raises():
    with pytest.raises(capture.CaptureError):
        capture.encoder_args("h265_magic", Q, 30)


def test_ddagrab_input_downloads_frames():
    args = capture.grab_input("ddagrab", (0, 0, 1920, 1080), 1, 30)
    assert args[:3] == ["-f", "lavfi", "-i"]
    assert args[3].startswith("ddagrab=output_idx=1:framerate=30")
    assert "hwdownload,format=bgra" in args[3]


def test_gdigrab_input_targets_monitor():
    args = capture.grab_input("gdigrab", (-1920, 8, 1920, 1080), None, 30)
    assert args[args.index("-offset_x") + 1] == "-1920"
    assert args[args.index("-offset_y") + 1] == "8"
    assert args[args.index("-video_size") + 1] == "1920x1080"
    assert args[-2:] == ["-i", "desktop"]


def test_record_args_with_audio_and_preview():
    cmd = capture.record_args("ffmpeg", grabber="gdigrab", region=(0, 0, 1920, 1080),
                              output_idx=None, fps=30, encoder="libx264", quality=Q,
                              audio="Stereo Mix (Realtek)", out_path=Path("o/x.mkv"),
                              preview_path=Path("s/p.bmp"))
    assert cmd[cmd.index("-f", cmd.index("desktop")) + 1] == "dshow"
    assert "audio=Stereo Mix (Realtek)" in cmd
    assert "1:a" in cmd and "aac" in cmd
    assert "-ac" not in cmd                       # no downmix (CLAUDE.md: AAC downmix bug)
    mkv = cmd.index(str(Path("o/x.mkv")))
    assert cmd[mkv - 1] == "matroska"             # crash-safe container
    assert cmd[-1] == str(Path("s/p.bmp")) and "-update" in cmd


def test_record_args_without_audio_maps_no_audio():
    cmd = capture.record_args("ffmpeg", grabber="ddagrab", region=None, output_idx=0, fps=30,
                              encoder="h264_nvenc", quality=Q, audio=None,
                              out_path=Path("x.mkv"), preview_path=Path("p.bmp"))
    assert "dshow" not in cmd and "1:a" not in cmd and "aac" not in cmd


def test_frame_args_one_bmp_on_stdout():
    cmd = capture.frame_args("ffmpeg", "gdigrab", (0, 0, 800, 600), None)
    assert cmd[-1] == "-" and "image2pipe" in cmd and cmd[cmd.index("-frames:v") + 1] == "1"


# --- device / encoder / monitor choice --------------------------------------------
def test_parse_dshow_old_and_new_styles():
    old = ('[dshow @ 0] DirectShow video devices\n[dshow @ 0]  "Cam"\n'
           '[dshow @ 0]     Alternative name "@device_x"\n'
           '[dshow @ 0] DirectShow audio devices\n[dshow @ 0]  "Stereo Mix (Realtek)"\n')
    assert capture.parse_dshow_audio(old) == ["Stereo Mix (Realtek)"]
    new = ('[dshow @ 0] "Cam" (video)\n[dshow @ 0]   Alternative name "@x"\n'
           '[dshow @ 0] "virtual-audio-capturer" (audio)\n')
    assert capture.parse_dshow_audio(new) == ["virtual-audio-capturer"]


def test_choose_audio():
    assert capture.choose_audio("auto", CAPS["audio_devices"])[0] == "Stereo Mix (Realtek)"
    dev, note = capture.choose_audio("auto", ["Microphone (USB)"])
    assert dev is None and "video only" in note
    assert capture.choose_audio("none", CAPS["audio_devices"])[0] is None
    assert capture.choose_audio("CABLE Output", [])[0] == "CABLE Output"


def test_choose_encoder_prefers_working_hardware(monkeypatch):
    monkeypatch.setattr(capture, "encoder_works", lambda ff, enc: enc == "h264_amf")
    caps = {**CAPS, "encoders": ["libx264", "h264_nvenc", "h264_amf"]}
    assert capture.choose_encoder("ffmpeg", "auto", caps) == "h264_amf"
    monkeypatch.setattr(capture, "encoder_works", lambda ff, enc: False)
    assert capture.choose_encoder("ffmpeg", "auto", caps) == "libx264"
    assert capture.choose_encoder("ffmpeg", "h264_nvenc", caps) == "h264_nvenc"


def test_monitor_at_and_output_index():
    mons = [(0, 0, 1920, 1080, True), (-1920, 8, 1920, 1080, False)]
    left = {"left": -1900, "top": 0, "width": 1940, "height": 1100}   # maximized overhang
    assert capture.monitor_at(left, mons)[:4] == (-1920, 8, 1920, 1080)
    assert capture.monitor_at(None, mons) is None
    outs = [(0, 0, 0, 1920, 1080), (1, -1920, 8, 1920, 1080)]
    assert capture.output_index((-1920, 8, 1920, 1080), outs) == 1
    assert capture.output_index((5000, 0, 800, 600), outs) is None


# --- backend selection -------------------------------------------------------------
def make(tmp_path, cfg=None, caps=None, popen=None, run=None):
    cfg = {"capture_backend": "ffmpeg", "capture_encoder": "libx264", **(cfg or {})}
    return capture.FfmpegCapture(cfg, tmp_path / "out", state_dir=tmp_path / "st",
                                 ffmpeg="ffmpeg", caps=caps or dict(CAPS), popen=popen, run=run)


def test_backend_name_default_is_obs():
    assert capture.backend_name({}) == "obs"
    assert capture.backend_name({"capture_backend": "ffmpeg"}) == "ffmpeg"


def test_grabber_auto_needs_reachable_output(tmp_path):
    cap = make(tmp_path)
    cap.output_idx = None
    assert cap.grabber() == "gdigrab"            # monitor not on ddagrab's adapter
    cap.output_idx = 0
    assert cap.grabber() == "ddagrab"
    cap = make(tmp_path, caps={**CAPS, "ddagrab": False})
    assert cap.grabber() == "gdigrab"


def test_ddagrab_forced_without_support_refuses(tmp_path):
    with pytest.raises(capture.CaptureError):
        make(tmp_path, cfg={"capture_grabber": "ddagrab"}, caps={**CAPS, "ddagrab": False})


def test_dispatch_obs_path_unchanged():
    calls = []

    class FakeObs:
        def current_scene(self):
            return "Scene"

        def screenshot_luma(self, source):
            calls.append(source)
            return 42.0

    assert capture.luma(FakeObs()) == 42.0 and calls == ["Scene"]
    assert capture.aim_window(FakeObs(), 1234) is None
    capture.check(FakeObs())                     # no-op for OBS


def test_obs_capture_wrapper():
    class FakeObs:
        def __init__(self):
            self.log = []

        def start_record(self):
            self.log.append("start")

        def stop_record(self):
            return "C:/x.mkv"

        def record_status(self):
            return {"outputActive": True}

    o = FakeObs()
    cap = capture.ObsCapture(o)
    cap.start("ignored")
    assert o.log == ["start"] and cap.stop() == "C:/x.mkv" and cap.is_recording()


# --- settings ----------------------------------------------------------------------
def test_validate_capture_keys():
    assert capture.validate({"capture_backend": "ffmpeg", "capture_fps": 30,
                             "capture_audio": "Stereo Mix (Realtek)"}) == {}
    errs = capture.validate({"capture_backend": "vlc", "capture_grabber": "x",
                             "capture_encoder": "h265", "capture_fps": 240,
                             "capture_audio": 'bad"name'})
    assert set(errs) == {"capture_backend", "capture_grabber", "capture_encoder",
                         "capture_fps", "capture_audio"}


def test_settings_save_accepts_and_rejects_capture(tmp_path):
    cfg, errs = settings.save(tmp_path, {"capture_backend": "ffmpeg", "capture_fps": "25"})
    assert errs == {} and cfg["capture_backend"] == "ffmpeg" and cfg["capture_fps"] == 25
    cfg, errs = settings.save(tmp_path, {"capture_backend": "nope"})
    assert "capture_backend" in errs and settings.read(tmp_path)["capture_backend"] == "ffmpeg"


# --- start / stop with a fake ffmpeg ------------------------------------------------
class FakeStdin:
    def __init__(self, proc):
        self.proc, self.data, self.closed = proc, b"", False

    def write(self, b):
        self.data += b
        if b"q" in b and self.proc.quits_on_q:
            self.proc.code = 0

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, cmd, out, quits_on_q=True, write=True):
        self.cmd, self.code, self.quits_on_q, self.killed = cmd, None, quits_on_q, False
        self.stdin = FakeStdin(self)
        self._handle = 0
        if write:
            Path(out).write_bytes(b"\x1aE\xdf\xa3 mkv")

    def poll(self):
        return self.code

    @property
    def returncode(self):
        return self.code

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.code

    def kill(self):
        self.killed, self.code = True, 1


def fake_popen(store, **kw):
    def popen(cmd, **_):
        out = cmd[cmd.index("matroska") + 1]
        store.append(FakeProc(cmd, out, **kw))
        return store[-1]
    return popen


def test_start_then_graceful_stop_with_q(tmp_path):
    procs = []
    cap = make(tmp_path, popen=fake_popen(procs))
    cap.start(tmp_path / "out" / "a.mkv")
    assert cap.is_recording() and cap.record_status() == {"outputActive": True}
    path = cap.stop_record()
    assert path == str(tmp_path / "out" / "a.mkv")
    assert procs[0].stdin.data == b"q" and not procs[0].killed
    assert not cap.is_recording()


def test_stop_kills_when_q_is_ignored(tmp_path):
    procs = []
    cap = make(tmp_path, popen=fake_popen(procs, quits_on_q=False))
    cap.start(tmp_path / "out" / "b.mkv")
    assert cap.stop(timeout=0) == str(tmp_path / "out" / "b.mkv")
    assert procs[0].killed


def test_died_midway_raises_then_hands_back_partial(tmp_path):
    procs = []
    cap = make(tmp_path, popen=fake_popen(procs))
    cap.start(tmp_path / "out" / "c.mkv")
    procs[0].code = 1                             # ffmpeg crashed
    with pytest.raises(ObsError):                 # an ObsError: recorder cleanup catches it
        cap.check()
    assert cap.record_status()["outputActive"]    # so the recorder's finally calls stop
    assert cap.stop_record() == str(tmp_path / "out" / "c.mkv")   # partial, to discard
    assert not cap.is_recording()


def test_stop_raises_once_if_ffmpeg_ended_unnoticed(tmp_path):
    procs = []
    cap = make(tmp_path, popen=fake_popen(procs))
    cap.start(tmp_path / "out" / "d.mkv")
    procs[0].code = 1
    with pytest.raises(capture.CaptureError):     # never filed as a finished recording
        cap.stop()
    assert cap.stop() == str(tmp_path / "out" / "d.mkv")


def test_start_fails_when_ffmpeg_exits_at_once(tmp_path):
    procs = []

    def popen(cmd, **kw):
        p = FakeProc(cmd, cmd[cmd.index("matroska") + 1], write=False)
        p.code = 1
        procs.append(p)
        return p

    cap = make(tmp_path, popen=popen)
    with pytest.raises(capture.CaptureError):
        cap.start(tmp_path / "out" / "e.mkv")
    assert not cap.is_recording()


def test_refuses_second_start(tmp_path):
    cap = make(tmp_path, popen=fake_popen([]))
    cap.start(tmp_path / "out" / "f.mkv")
    with pytest.raises(capture.CaptureError):
        cap.start(tmp_path / "out" / "g.mkv")
    cap.stop()


# --- brightness ----------------------------------------------------------------------
def _bmp(rgb, w=4, h=2):
    import struct
    row = bytes([rgb[2], rgb[1], rgb[0]]) * w
    pad = (4 - len(row) % 4) % 4
    px = (row + b"\0" * pad) * h
    return (b"BM" + struct.pack("<IHHI", 54 + len(px), 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(px), 0, 0, 0, 0) + px)


def test_luma_before_recording_grabs_one_frame(tmp_path):
    class R:
        returncode, stdout = 0, _bmp((0, 0, 0))
    cap = make(tmp_path, run=lambda cmd, timeout: R())
    assert cap.luma() == pytest.approx(0)


def test_luma_while_recording_reads_preview_and_ignores_stale(tmp_path):
    cap = make(tmp_path, popen=fake_popen([]))
    cap.start(tmp_path / "out" / "h.mkv")
    cap.preview.write_bytes(_bmp((255, 255, 255)))
    assert cap.luma() == pytest.approx(255, abs=0.5)
    old = time.time() - 60
    import os
    os.utime(cap.preview, (old, old))
    assert cap.luma() is None                     # a frozen ffmpeg is not "bright"
    cap.stop()


def test_recorder_program_luma_uses_backend(tmp_path):
    from playcap import recorder

    class R:
        returncode, stdout = 0, _bmp((100, 100, 100))
    cap = make(tmp_path, run=lambda cmd, timeout: R())
    assert recorder.program_luma(cap) == pytest.approx(100, abs=0.5)

    class Broken:
        def current_scene(self):
            raise ObsError("gone")
    assert recorder.program_luma(Broken()) is None
