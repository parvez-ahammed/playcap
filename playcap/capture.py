"""Screen-capture backends: OBS (default) or ffmpeg alone.

    "capture_backend": "obs"      OBS records, driven over obs-websocket (obs_client)
    "capture_backend": "ffmpeg"   playcap runs ffmpeg itself; no OBS install needed

Both are used through one small surface (Capture): start(path) / stop() -> path
/ is_recording() / luma() / aim(bounds) / close(). The recorder still talks to a
raw obs_client.Obs on the OBS path, so that path is byte-for-byte what it was;
the ffmpeg backend also answers the handful of Obs-shaped calls the recorder
makes (start_record, stop_record, record_status()["outputActive"]), and the
two places that differ -- aiming the capture and reading its brightness -- go
through aim() / luma() / check() below, which dispatch on the type.
CaptureError subclasses ObsError so every cleanup handler the recorder already
has for OBS (stop the recording, bin the partial) covers ffmpeg too.

Research behind the ffmpeg backend (October 2026):

* Video, ``ddagrab``: a libavfilter *source* (use it via ``-f lavfi``) on the
  Desktop Duplication API, added in FFmpeg 6.0 (Changelog: "ddagrab (Desktop
  Duplication) video capture filter"). Options: output_idx (DXGI output on the
  D3D11 device's adapter), framerate (default 30), draw_mouse, video_size,
  offset_x/offset_y, output_fmt, dup_frames. It returns D3D11 hardware frames
  only, so software encoding or any CPU filter needs ``hwdownload,format=bgra``.
  output_idx enumerates the outputs of the *default adapter only*
  (IDXGIAdapter_EnumOutputs on the device's parent adapter) and ffmpeg logs
  only the dimensions, never the position -- so playcap reads the outputs'
  desktop rectangles itself through DXGI (dxgi_outputs) to turn "the monitor
  the browser is on" into an index. A monitor driven by a second GPU (hybrid
  laptops) is not reachable by ddagrab; playcap falls back to gdigrab there.
  Sources: https://github.com/FFmpeg/FFmpeg/blob/master/Changelog ,
  https://ayosec.github.io/ffmpeg-filters-docs/8.0/Sources/Video/ddagrab.html ,
  https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/vsrc_ddagrab.c
* FFmpeg 8.1 adds ``gfxcapture`` (Windows.Graphics.Capture). Newer than most
  installed builds; not used yet.
  Source: https://ayosec.github.io/ffmpeg-filters-docs/8.1/Sources/Video/gfxcapture.html
* Video fallback, ``gdigrab``: a GDI device in every Windows build for years
  (``-f gdigrab -i desktop`` with -offset_x/-offset_y/-video_size in virtual-
  desktop pixels). CPU-bound but universal; monitor targeting is plain
  coordinates. "auto" picks ddagrab when the build has it and the monitor is
  on the default adapter, else gdigrab.
* System audio: mainline FFmpeg has **no WASAPI (loopback) input device**.
  Trac ticket #9408 "WASAPI Audio Input/Output Support" was still status
  "new" with no owner in its last activity (May 2025), and the Changelog
  through 8.1 has no WASAPI or loopback entry. Patches and forks exist; the
  builds people install do not have it. What does work is DirectShow
  (``-f dshow -i audio="<device>"``) on a device that carries what the
  speakers play: the sound driver's own "Stereo Mix" (often disabled or absent
  on modern laptops), the free screen-capture-recorder package's
  "virtual-audio-capturer", or a virtual cable (VB-Cable "CABLE Output").
  So there is no install-free system audio with ffmpeg alone on a machine
  whose driver hides Stereo Mix. capture_audio is therefore a setting:
  "auto" uses the first loopback-looking dshow device it finds and otherwise
  records video only and says so; "none" never records audio; any other value
  is the exact dshow device name. A stdlib-only WASAPI capture (ctypes COM
  plus feeding PCM to ffmpeg through a pipe) is possible but large and fragile
  next to the alternatives, so it was not built.
  Sources: https://trac.ffmpeg.org/ticket/9408 ,
  https://ffmpeg.org/pipermail/ffmpeg-trac/2025-May/073534.html ,
  https://trac.ffmpeg.org/wiki/DirectShow ,
  https://github.com/rdp/screen-capture-recorder-to-video-windows-free

Design choices:

* Container: Matroska while recording, like OBS's default, because a killed
  ffmpeg leaves an MKV that still plays up to the last cluster; an MP4 without
  its moov atom is unreadable. The recorder's finalize step remuxes it to
  faststart MP4 exactly as it does OBS's files.
* Graceful stop: "q" on ffmpeg's stdin makes it flush the encoders and write
  the trailer. ffmpeg runs in its own process group so the CTRL_BREAK that
  stops the recorder (playcap.jobs) does not kill it under the recorder's
  feet -- the recorder's cleanup decides what happens to the file. On Windows
  it is also put in a kill-on-close job object, so if the recorder dies hard
  ffmpeg dies with it instead of recording an empty screen forever.
* "Recording" means "started and not yet stopped by us", not "the process is
  alive": if ffmpeg dies mid-item, check() raises (the item is retried),
  is_recording() stays true, and the recorder's cleanup then calls stop(),
  which hands back the partial file so it is discarded like an OBS fragment.
  stop() raises the first time if ffmpeg had already exited on its own, so a
  short file can never be filed as a finished recording.
* Brightness (the DRM-black principle applies here exactly as with OBS): while
  recording, the same ffmpeg writes a 64x36 BMP of what it is encoding once a
  second (split -> fps=1 -> scale -> image2 -update 1), so the check measures
  the actual capture, not a second grab that might differ. Before recording
  (the recorder's preflight) a one-frame grab of the target monitor is used.
* Monitor: aim(bounds) matches the browser window's centre (CDP
  Browser.getWindowForTarget) against the monitors, like screen.aim_capture
  does for OBS; aim_window(hwnd) then pins it exactly with MonitorFromWindow
  once screen.Pin has found the window. The process is made per-monitor DPI
  aware so monitor rectangles are physical pixels, what ffmpeg captures in.
* Encoding: record_quality's mode maps onto the encoder. CRF -> libx264 -crf,
  h264_nvenc -rc vbr -cq, h264_qsv -global_quality, h264_amf -rc cqp -qp_i/p;
  bitrate -> CBR at video_bitrate_kbps. Keyframes every keyframe_seconds.
  "auto" tries the hardware encoders with a 5-frame test encode (an ffmpeg
  build lists h264_nvenc even on a machine with no NVIDIA GPU) and falls back
  to libx264 with x264_preset. Audio is AAC 160 kbps stereo: no downmix (the
  -ac 1 downmix is what produced the malformed AAC frame noted in CLAUDE.md).
* Windows only for now (ddagrab, gdigrab and dshow are Windows devices).
"""
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playcap.obs_client import Obs, ObsError, bmp_mean_luma

BACKENDS = ("obs", "ffmpeg")
GRABBERS = ("auto", "ddagrab", "gdigrab")
ENCODERS = ("auto", "libx264", "h264_nvenc", "h264_qsv", "h264_amf")
HW_ORDER = ("h264_nvenc", "h264_qsv", "h264_amf")
DEFAULTS = {
    "capture_backend": "obs",     # "obs" or "ffmpeg"
    "capture_grabber": "auto",    # ffmpeg only: ddagrab, gdigrab or auto
    "capture_encoder": "auto",    # ffmpeg only: hardware first, else libx264
    "capture_audio": "auto",      # ffmpeg only: auto, none, or a dshow device name
    "capture_fps": 30,
}
KEYS = tuple(DEFAULTS)
# dshow device names that carry what the speakers play.
LOOPBACK_HINTS = ("virtual-audio-capturer", "stereo mix", "what u hear", "wave out mix",
                  "wave out", "cable output", "voicemeeter out", "loopback")
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "160k"]
PREVIEW_SIZE = (64, 36)
PREVIEW_STALE_S = 6
STOP_TIMEOUT_S = 30
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


class CaptureError(ObsError):
    """A capture backend failed. An ObsError, so the recorder's OBS cleanup
    handlers cover every backend."""


def backend_name(cfg):
    return cfg.get("capture_backend") or "obs"


def label(cfg):
    """The active screen recorder's name as people know it: "OBS" or "ffmpeg"."""
    return "ffmpeg" if backend_name(cfg) == "ffmpeg" else "OBS"


def validate(partial):
    errors = {}
    if "capture_backend" in partial and partial["capture_backend"] not in BACKENDS:
        errors["capture_backend"] = "Pick OBS or ffmpeg."
    if "capture_grabber" in partial and partial["capture_grabber"] not in GRABBERS:
        errors["capture_grabber"] = "Pick auto, ddagrab or gdigrab."
    if "capture_encoder" in partial and partial["capture_encoder"] not in ENCODERS:
        errors["capture_encoder"] = "Unknown encoder."
    if "capture_audio" in partial:
        a = partial["capture_audio"]
        if not isinstance(a, str) or not a.strip() or '"' in a or len(a) > 200:
            errors["capture_audio"] = "auto, none, or a sound device name."
    if "capture_fps" in partial:
        try:
            ok = 10 <= int(partial["capture_fps"]) <= 60 and not isinstance(
                partial["capture_fps"], bool)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            errors["capture_fps"] = "Frame rate must be 10-60."
    return errors


# --- dispatch used by the recorder (Obs or Capture) -----------------------------
def aim(cap, bounds):
    """Point the capture at the monitor showing `bounds`. -> note or None."""
    if isinstance(cap, Capture):
        return cap.aim(bounds)
    from playcap import screen
    return screen.aim_capture(cap, bounds)


def aim_window(cap, hwnd):
    if isinstance(cap, Capture) and hwnd:
        return cap.aim_window(hwnd)
    return None


def luma(cap):
    """Mean brightness (0-255) of what is being captured, or None."""
    if isinstance(cap, Capture):
        return cap.luma()
    return cap.screenshot_luma(cap.current_scene())


def check(cap):
    """Raise CaptureError when the backend died mid-recording (ffmpeg only;
    a dead OBS already surfaces through its websocket)."""
    if isinstance(cap, Capture):
        cap.check()


# --- the interface ----------------------------------------------------------------
class Capture:
    name = "?"

    def start(self, path=None):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def is_recording(self):
        raise NotImplementedError

    def luma(self):
        return None

    def aim(self, bounds):
        return None

    def aim_window(self, hwnd):
        return None

    def check(self):
        return None

    def describe(self):
        return self.name

    def close(self):
        pass

    # Obs-shaped aliases: the recorder's loop and cleanup call these.
    def start_record(self):
        self.start()

    def stop_record(self):
        return self.stop()

    def record_status(self):
        return {"outputActive": self.is_recording()}


class ObsCapture(Capture):
    """Thin wrapper over obs_client.Obs: no behaviour of its own. OBS picks the
    file name itself (its recording folder and format), so start() ignores
    `path`."""
    name = "obs"

    def __init__(self, obs):
        self.obs = obs

    def start(self, path=None):
        self.obs.start_record()

    def stop(self):
        return self.obs.stop_record()

    def is_recording(self):
        return bool(self.obs.record_status().get("outputActive"))

    def luma(self):
        try:
            return self.obs.screenshot_luma(self.obs.current_scene())
        except Exception:
            return None

    def aim(self, bounds):
        from playcap import screen
        return screen.aim_capture(self.obs, bounds)

    def close(self):
        self.obs.close()


def open_backend(cfg, out_dir, **kw):
    """The configured backend, ready to record. OBS: connects to its websocket."""
    if backend_name(cfg) == "ffmpeg":
        return FfmpegCapture(cfg, out_dir, **kw)
    return ObsCapture(Obs(cfg.get("obs_password", ""), cfg.get("obs_ws_url")
                          or "ws://127.0.0.1:4455"))


# --- ffmpeg: what the build can do -----------------------------------------------
_caps_cache = {}
_enc_cache = {}


def _run(cmd, timeout=20):
    # stdin=DEVNULL: with an inherited, redirected stdin (the UI started from a
    # background shell or a launcher) some ffmpeg builds print an empty
    # `-devices` list, which made gdigrab look missing.
    kw = {"creationflags": CREATE_NO_WINDOW} if sys.platform.startswith("win") else {}
    return subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                          timeout=timeout, **kw)


def parse_dshow_audio(text):
    """Audio device names from `ffmpeg -list_devices true -f dshow -i dummy`.
    Handles both log styles: ffmpeg <5 lists audio under a "DirectShow audio
    devices" heading; ffmpeg 5+ tags each line with "(audio)"."""
    out, in_audio = [], False
    for line in text.splitlines():
        if "DirectShow audio devices" in line:
            in_audio = True
            continue
        if "DirectShow video devices" in line:
            in_audio = False
            continue
        if "Alternative name" in line:
            continue
        m = re.search(r'"([^"]+)"\s*(\((audio|video|audio, video|none)\))?', line)
        if not m:
            continue
        kind = m.group(3)
        if (kind and "audio" in kind) or (kind is None and in_audio):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


def pick_loopback(devices):
    for hint in LOOPBACK_HINTS:
        for d in devices:
            if hint in d.lower():
                return d
    return None


def ffmpeg_caps(ffmpeg):
    """{ddagrab, gdigrab, dshow, encoders, audio_devices} for this ffmpeg."""
    key = str(ffmpeg)
    try:
        key += f"@{os.path.getmtime(ffmpeg)}"
    except OSError:
        pass
    if key in _caps_cache:
        return _caps_cache[key]
    caps = {"ddagrab": False, "gdigrab": False, "dshow": False, "encoders": [],
            "audio_devices": [], "version": ""}
    try:
        filters = _run([ffmpeg, "-hide_banner", "-filters"]).stdout.decode(errors="replace")
        devices = _run([ffmpeg, "-hide_banner", "-devices"]).stdout.decode(errors="replace")
        encoders = _run([ffmpeg, "-hide_banner", "-encoders"]).stdout.decode(errors="replace")
        version = _run([ffmpeg, "-hide_banner", "-version"]).stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        _caps_cache[key] = caps
        return caps
    caps["version"] = (version.splitlines() or [""])[0]
    caps["ddagrab"] = bool(re.search(r"\sddagrab\s", filters))
    caps["gdigrab"] = bool(re.search(r"\sgdigrab\s", devices))
    caps["dshow"] = bool(re.search(r"\sdshow\s", devices))
    caps["encoders"] = [e for e in ("libx264", *HW_ORDER)
                        if re.search(r"\s%s\s" % e, encoders)]
    if caps["dshow"]:
        try:
            r = _run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow",
                      "-i", "dummy"])
            caps["audio_devices"] = parse_dshow_audio(r.stderr.decode(errors="replace"))
        except (OSError, subprocess.SubprocessError):
            pass
    _caps_cache[key] = caps
    return caps


def encoder_works(ffmpeg, encoder):
    """A listed hardware encoder may have no hardware behind it: try 5 frames."""
    key = (str(ffmpeg), encoder)
    if key not in _enc_cache:
        try:
            r = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                      "-i", "color=c=gray:s=640x360:r=30", "-frames:v", "5",
                      "-pix_fmt", "yuv420p", "-c:v", encoder, "-f", "null", "-"], timeout=30)
            _enc_cache[key] = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _enc_cache[key] = False
    return _enc_cache[key]


def choose_encoder(ffmpeg, choice, caps):
    if choice and choice != "auto":
        return choice
    for enc in HW_ORDER:
        if enc in caps["encoders"] and encoder_works(ffmpeg, enc):
            return enc
    return "libx264"


def choose_audio(setting, devices):
    """-> (device or None, note)."""
    setting = (setting or "auto").strip()
    if setting.lower() == "none":
        return None, "audio off (capture_audio is none)"
    if setting.lower() == "auto":
        dev = pick_loopback(devices)
        if dev:
            return dev, f"audio from {dev}"
        return None, ("no system-audio device found -- recording video only. "
                      "Enable Stereo Mix, or install screen-capture-recorder "
                      "(virtual-audio-capturer) or a virtual audio cable")
    return setting, f"audio from {setting}"


# --- argument building (pure: unit tested) -----------------------------------------
def encoder_args(encoder, quality, fps):
    """record_quality.effective(cfg) -> encoder arguments for ffmpeg."""
    crf = str(quality["record_crf"])
    kbps = int(quality["video_bitrate_kbps"])
    gop = ["-g", str(int(fps) * int(quality["keyframe_seconds"]))]
    cbr = ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    bitrate = quality["record_mode"] == "bitrate"
    if encoder == "libx264":
        rc = cbr if bitrate else ["-crf", crf]
        return ["-c:v", "libx264", "-preset", quality["x264_preset"], *rc, *gop]
    if encoder == "h264_nvenc":
        rc = ["-rc", "cbr", *cbr] if bitrate else ["-rc", "vbr", "-cq", crf, "-b:v", "0"]
        return ["-c:v", "h264_nvenc", *rc, *gop]
    if encoder == "h264_qsv":
        rc = cbr if bitrate else ["-global_quality", crf, "-look_ahead", "0"]
        return ["-c:v", "h264_qsv", *rc, *gop]
    if encoder == "h264_amf":
        rc = ["-rc", "cbr", *cbr] if bitrate else ["-rc", "cqp", "-qp_i", crf, "-qp_p", crf]
        return ["-c:v", "h264_amf", *rc, *gop]
    raise CaptureError(f"unknown encoder {encoder}")


def grab_input(grabber, region, output_idx, fps):
    """Video input arguments. region = (x, y, w, h) in desktop pixels."""
    if grabber == "ddagrab":
        src = (f"ddagrab=output_idx={int(output_idx or 0)}:framerate={int(fps)}"
               ":draw_mouse=0,hwdownload,format=bgra")
        return ["-f", "lavfi", "-i", src]
    if grabber == "gdigrab":
        args = ["-f", "gdigrab", "-framerate", str(int(fps)), "-draw_mouse", "0",
                "-thread_queue_size", "512"]
        if region:
            x, y, w, h = region
            args += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}"]
        return args + ["-i", "desktop"]
    raise CaptureError(f"unknown grabber {grabber}")


def record_args(ffmpeg, *, grabber, region, output_idx, fps, encoder, quality,
                audio, out_path, preview_path):
    """The whole recording command line."""
    pw, ph = PREVIEW_SIZE
    cmd = [str(ffmpeg), "-hide_banner", "-nostats", "-loglevel", "warning", "-y",
           *grab_input(grabber, region, output_idx, fps)]
    if audio:
        cmd += ["-f", "dshow", "-audio_buffer_size", "50", "-thread_queue_size", "1024",
                "-i", f"audio={audio}"]
    cmd += ["-filter_complex",
            f"[0:v]split=2[rec][pv];[rec]format=yuv420p[v];"
            f"[pv]fps=1,scale={pw}:{ph},format=bgr24[p]",
            "-map", "[v]"]
    if audio:
        cmd += ["-map", "1:a"]
    cmd += [*encoder_args(encoder, quality, fps)]
    if audio:
        cmd += AUDIO_ARGS
    # -flush_packets 1: write each packet through instead of filling a 32 KB
    # buffer first. A still picture at CRF 24 can take longer than start()'s
    # confirmation window to fill it (the bundled demo did), and unflushed
    # bytes are also what a killed ffmpeg would lose.
    cmd += ["-flush_packets", "1", "-f", "matroska", str(out_path),
            "-map", "[p]", "-c:v", "bmp", "-f", "image2", "-update", "1", str(preview_path)]
    return cmd


def frame_args(ffmpeg, grabber, region, output_idx):
    """One small BMP of the target monitor on stdout, for the preflight check."""
    pw, ph = PREVIEW_SIZE
    return [str(ffmpeg), "-hide_banner", "-loglevel", "error",
            *grab_input(grabber, region, output_idx, 10),
            "-frames:v", "1", "-vf", f"scale={pw}:{ph},format=bgr24",
            "-c:v", "bmp", "-f", "image2pipe", "-"]


# --- monitors (Windows) ------------------------------------------------------------
_dpi_done = False


def _dpi_aware():
    """Physical pixels from the monitor APIs, which is what ffmpeg captures in."""
    global _dpi_done
    if _dpi_done or not sys.platform.startswith("win"):
        return
    _dpi_done = True
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def monitors():
    """[(x, y, w, h, primary)] for every monitor, or [] off Windows."""
    if not sys.platform.startswith("win"):
        return []
    _dpi_aware()
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    u = ctypes.windll.user32
    found = []
    proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def visit(hmon, _hdc, _rect, _lp):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            found.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                          bool(mi.dwFlags & 1)))
        return True

    u.EnumDisplayMonitors(None, None, proc_t(visit), 0)
    return found


def window_monitor(hwnd):
    """(x, y, w, h) of the monitor holding most of this window, or None."""
    if not sys.platform.startswith("win") or not hwnd:
        return None
    _dpi_aware()
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    u = ctypes.windll.user32
    u.MonitorFromWindow.restype = wintypes.HMONITOR
    u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    hmon = u.MonitorFromWindow(hwnd, 2)          # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    if not hmon or not u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    r = mi.rcMonitor
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def dxgi_outputs():
    """[(output_idx, x, y, w, h)] for the outputs ddagrab can reach: those of
    DXGI adapter 0, the adapter a default D3D11 device is created on. Plain
    ctypes COM, no dependency. [] when DXGI is unavailable."""
    if not sys.platform.startswith("win"):
        return []
    _dpi_aware()
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                    ("d", ctypes.c_ubyte * 8)]

    class OUTPUT_DESC(ctypes.Structure):
        _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("Desktop", wintypes.RECT),
                    ("Attached", wintypes.BOOL), ("Rotation", ctypes.c_int),
                    ("Monitor", ctypes.c_void_p)]

    # IID_IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}
    iid = GUID(0x770aae78, 0xf26f, 0x4dba, (ctypes.c_ubyte * 8)(
        0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))

    def method(obj, index, *argtypes):
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])

    def release(obj):
        if obj:
            method(obj, 2)(obj)

    try:
        dxgi = ctypes.WinDLL("dxgi")
    except OSError:
        return []
    factory, adapter = ctypes.c_void_p(), ctypes.c_void_p()
    out = []
    try:
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return []
        # IDXGIFactory::EnumAdapters is vtable slot 7.
        if method(factory, 7, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
                factory, 0, ctypes.byref(adapter)) != 0:
            return []
        for i in range(16):
            output = ctypes.c_void_p()
            # IDXGIAdapter::EnumOutputs is slot 7; IDXGIOutput::GetDesc slot 7.
            if method(adapter, 7, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
                    adapter, i, ctypes.byref(output)) != 0:
                break
            try:
                desc = OUTPUT_DESC()
                if method(output, 7, ctypes.POINTER(OUTPUT_DESC))(output, ctypes.byref(desc)) == 0:
                    r = desc.Desktop
                    out.append((i, r.left, r.top, r.right - r.left, r.bottom - r.top))
            finally:
                release(output)
    except (OSError, ValueError):
        return out
    finally:
        release(adapter)
        release(factory)
    return out


def monitor_at(bounds, rects):
    """The rect (x, y, w, h, ...) holding the centre of `bounds`, or None."""
    if not bounds:
        return None
    cx = bounds["left"] + bounds["width"] / 2
    cy = bounds["top"] + bounds["height"] / 2
    for r in rects:
        if r[0] <= cx < r[0] + r[2] and r[1] <= cy < r[1] + r[3]:
            return r
    return None


def output_index(region, outputs):
    """ddagrab output_idx for a monitor rect, or None if ddagrab cannot see it."""
    if not region:
        return None
    for idx, x, y, w, h in outputs:
        if (x, y, w, h) == tuple(region[:4]):
            return idx
    return None


# --- Windows: keep ffmpeg from outliving the recorder ---------------------------
def _kill_on_close_job(proc):
    """Best effort. -> job handle (keep it open) or None."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        job = k.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED()
        info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info),
                                  ctypes.sizeof(info))
        if not k.AssignProcessToJobObject(wintypes.HANDLE(job),
                                          wintypes.HANDLE(int(proc._handle))):
            k.CloseHandle(wintypes.HANDLE(job))
            return None
        return job
    except Exception:
        return None


def _close_job(job):
    if job:
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
        except Exception:
            pass


# --- the ffmpeg backend -------------------------------------------------------------
class FfmpegCapture(Capture):
    name = "ffmpeg"

    def __init__(self, cfg, out_dir, state_dir=".playcap", ffmpeg=None, caps=None,
                 popen=subprocess.Popen, run=None):
        from playcap import detect, record_quality
        self.cfg = cfg
        self.out_dir = Path(out_dir)
        self.state_dir = Path(state_dir)
        self.ffmpeg = ffmpeg or detect.resolve("ffmpeg", cfg) or cfg.get("ffmpeg", "ffmpeg")
        self.caps = caps if caps is not None else ffmpeg_caps(self.ffmpeg)
        self.popen = popen
        self.run = run or _run
        self.fps = int(cfg.get("capture_fps", DEFAULTS["capture_fps"]))
        self.quality = record_quality.effective(cfg)
        self.preview = self.state_dir / "capture-preview.bmp"
        self.log_path = self.state_dir / "ffmpeg-capture.log"
        self.proc = None
        self.path = None
        self.job = None
        self.stopping = False
        self.died_reported = False
        if not sys.platform.startswith("win") and caps is None:
            raise CaptureError("The ffmpeg capture backend works on Windows only so far; "
                               "use capture_backend \"obs\".")
        want = cfg.get("capture_grabber", "auto")
        if want == "ddagrab" and not self.caps["ddagrab"]:
            raise CaptureError(f"{self.ffmpeg} has no ddagrab (needs FFmpeg 6.0+); "
                               "set capture_grabber to gdigrab or auto.")
        if not (self.caps["ddagrab"] or self.caps["gdigrab"]):
            raise CaptureError(f"{self.ffmpeg} can capture neither with ddagrab nor gdigrab.")
        self.grabber_setting = want
        self.encoder = choose_encoder(self.ffmpeg, cfg.get("capture_encoder", "auto"), self.caps)
        if self.encoder not in self.caps["encoders"] and caps is None:
            raise CaptureError(f"{self.ffmpeg} has no {self.encoder} encoder.")
        self.audio, self.audio_note = choose_audio(cfg.get("capture_audio", "auto"),
                                                   self.caps["audio_devices"])
        self.region = None             # (x, y, w, h); None = whole primary monitor
        self.output_idx = None
        prim = [m for m in (monitors() if caps is None else []) if m[4]]
        if prim:
            self._set_region(prim[0][:4])

    # -- aiming
    def _set_region(self, region):
        changed = tuple(region) != (tuple(self.region) if self.region else None)
        self.region = tuple(region)
        self.output_idx = output_index(self.region, dxgi_outputs()) \
            if self.caps["ddagrab"] else None
        return changed

    def grabber(self):
        if self.grabber_setting == "gdigrab" or not self.caps["ddagrab"]:
            return "gdigrab"
        if self.grabber_setting == "ddagrab":
            return "ddagrab"
        # auto: ddagrab only when it can see the monitor we want
        return "ddagrab" if self.output_idx is not None else "gdigrab"

    def aim(self, bounds):
        mons = monitors()
        hit = monitor_at(bounds, mons)
        if not hit or not self._set_region(hit[:4]):
            return None
        x, y, w, h = self.region
        return f"capture aimed at the {w}x{h} monitor at {x},{y}"

    def aim_window(self, hwnd):
        mon = window_monitor(hwnd)
        if not mon or not self._set_region(mon):
            return None
        x, y, w, h = self.region
        return f"capture aimed at the {w}x{h} monitor at {x},{y} (browser window)"

    def describe(self):
        where = (f"{self.region[2]}x{self.region[3]} at {self.region[0]},{self.region[1]}"
                 if self.region else "the primary monitor")
        return (f"ffmpeg {self.grabber()} {self.fps} fps, {self.encoder}, {where}; "
                f"{self.audio_note}")

    # -- recording
    def is_recording(self):
        return self.proc is not None

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _log_tail(self, n=600):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-n:].strip()
        except OSError:
            return ""

    def start(self, path=None, confirm_seconds=10):
        if self.proc is not None:
            raise CaptureError("ffmpeg is already recording; refusing to start another.")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = Path(path) if path else self.out_dir / (
            "playcap-" + datetime.now().strftime("%Y-%m-%d %H-%M-%S") + ".mkv")
        self.preview.unlink(missing_ok=True)
        cmd = record_args(self.ffmpeg, grabber=self.grabber(), region=self.region,
                          output_idx=self.output_idx, fps=self.fps, encoder=self.encoder,
                          quality=self.quality, audio=self.audio, out_path=path,
                          preview_path=self.preview)
        flags = (CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
                 if sys.platform.startswith("win") else 0)
        log = open(self.log_path, "w", encoding="utf-8")
        try:
            log.write(subprocess.list2cmdline(cmd) + "\n")
            log.flush()
            self.proc = self.popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=log, creationflags=flags)
        except OSError as exc:
            raise CaptureError(f"cannot start {self.ffmpeg}: {exc}") from exc
        finally:
            log.close()
        self.path, self.stopping, self.died_reported = path, False, False
        self.job = _kill_on_close_job(self.proc)
        deadline = time.time() + confirm_seconds
        while time.time() < deadline:
            if not self._alive():
                tail = self._log_tail()
                self._reap()
                Path(path).unlink(missing_ok=True)
                raise CaptureError(f"ffmpeg stopped as soon as it started: {tail}")
            try:
                if Path(path).stat().st_size > 0:
                    return
            except OSError:
                pass
            time.sleep(0.25)
        self.stop()
        Path(path).unlink(missing_ok=True)
        raise CaptureError("ffmpeg started but wrote nothing; see " + str(self.log_path))

    def check(self):
        if self.proc is not None and not self.stopping and not self._alive():
            self.died_reported = True
            raise CaptureError(f"ffmpeg stopped recording on its own "
                               f"(exit {self.proc.returncode}): {self._log_tail(300)}")

    def stop(self, timeout=STOP_TIMEOUT_S):
        """Finish the file and return its path. Raises once if ffmpeg had
        already died by itself (the file is short); the next call returns the
        path so the caller's cleanup can discard it."""
        if self.proc is None:
            raise CaptureError("ffmpeg is not recording")
        if not self._alive() and not self.died_reported:
            self.died_reported = True
            raise CaptureError(f"ffmpeg had stopped recording on its own "
                               f"(exit {self.proc.returncode}): {self._log_tail(300)}")
        self.stopping = True
        if self._alive():
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()        # MKV: what was written still plays
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        path = self.path
        self._reap()
        return str(path)

    def _reap(self):
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except (OSError, ValueError):
                pass
        _close_job(self.job)
        self.proc, self.job, self.stopping = None, None, False

    # -- brightness
    def luma(self):
        if self.proc is not None:
            try:
                st = self.preview.stat()
                if time.time() - st.st_mtime > PREVIEW_STALE_S:
                    return None
                return bmp_mean_luma(self.preview.read_bytes())
            except OSError:
                return None
        try:
            r = self.run(frame_args(self.ffmpeg, self.grabber(), self.region, self.output_idx),
                         timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return bmp_mean_luma(r.stdout) if r.returncode == 0 and r.stdout else None

    def close(self):
        if self.proc is not None:
            try:
                self.stop(timeout=10)
            except CaptureError:
                self._reap()


def report(cfg, ffmpeg=None):
    """What the setup wizard shows about capture: usable backends and why."""
    from playcap import detect
    out = {"backend": backend_name(cfg), "obs": {"ok": bool(detect.resolve("obs", cfg))}}
    ff = ffmpeg or detect.resolve("ffmpeg", cfg)
    f = {"ok": False, "path": ff, "grabber": None, "encoders": [], "audio_devices": [],
         "loopback": None, "why": ""}
    if not ff:
        f["why"] = "ffmpeg is not installed."
    elif not sys.platform.startswith("win"):
        f["why"] = "ffmpeg capture works on Windows only so far."
    else:
        caps = ffmpeg_caps(ff)
        f.update(encoders=caps["encoders"], audio_devices=caps["audio_devices"],
                 loopback=pick_loopback(caps["audio_devices"]), version=caps["version"])
        if caps["ddagrab"]:
            f.update(ok=True, grabber="ddagrab")
        elif caps["gdigrab"]:
            f.update(ok=True, grabber="gdigrab",
                     why="This ffmpeg has no ddagrab (FFmpeg 6.0+); gdigrab works but costs more CPU.")
        else:
            f["why"] = "This ffmpeg build cannot capture the screen."
        if f["ok"] and "libx264" not in caps["encoders"] and not any(
                e in caps["encoders"] for e in HW_ORDER):
            f.update(ok=False, why="This ffmpeg build has no H.264 encoder.")
    out["ffmpeg"] = f
    return out
