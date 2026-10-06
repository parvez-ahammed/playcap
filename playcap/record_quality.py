"""How OBS encodes the recording -- chosen in the UI, applied to OBS's profile.

Two ways to record:

    quality  (default)  x264 CRF. Every frame gets the same visual quality and
                        the bitrate follows the picture: a still slide costs
                        almost nothing, motion costs more. The file is final;
                        re-compressing it later gains nothing.
    bitrate             x264 CBR at a fixed rate. Predictable size per hour,
                        but a still slide burns the same bits as motion, so
                        files are bigger than they need to be and "Re-compress
                        library" can shrink them afterwards.

Why this lives in OBS's profile files: OBS's simple output mode records CBR
(or a fixed "quality" tier), and switching that tier over the websocket made
StartRecord silently do nothing -- reproduced live. Its advanced mode takes a
full x264 configuration (rate control, CRF, keyframe interval, preset) from
<profile>/recordEncoder.json, read when OBS starts. So, like the websocket
switch in obs_setup, the profile is written only while OBS is closed:
apply() refuses while OBS runs, and Launch OBS / the recorder's own relaunch
call it right before starting OBS. A live run recorded rc=crf crf=24 keyint=60
this way.

Copies of the files are kept as *.playcap-bak before the first change.
"""
import json
import re
import shutil
from pathlib import Path

DEFAULTS = {
    "record_mode": "quality",     # "quality" (CRF) or "bitrate" (CBR)
    "record_crf": 24,             # 18 (near-lossless, big) .. 30 (small, soft)
    "video_bitrate_kbps": 2500,   # bitrate mode only
    "x264_preset": "veryfast",    # slower = smaller file for the same quality, more CPU
    "keyframe_seconds": 2,        # short GOP = fast seeking in Jellyfin/Emby players
}
KEYS = tuple(DEFAULTS)

# What the UI offers. Numbers are x264 CRF values.
PRESETS = {
    "small":    {"record_mode": "quality", "record_crf": 28},
    "balanced": {"record_mode": "quality", "record_crf": 24},
    "high":     {"record_mode": "quality", "record_crf": 20},
}
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium")


def effective(cfg):
    out = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    out["record_crf"] = int(out["record_crf"])
    out["video_bitrate_kbps"] = int(out["video_bitrate_kbps"])
    out["keyframe_seconds"] = int(out["keyframe_seconds"])
    return out


def validate(partial):
    errors = {}
    if "record_mode" in partial and partial["record_mode"] not in ("quality", "bitrate"):
        errors["record_mode"] = "Pick quality or bitrate."
    checks = {"record_crf": (14, 34, "Quality must be 14-34 (lower = better, bigger)."),
              "video_bitrate_kbps": (500, 50000, "Bitrate must be 500-50000 kbps."),
              "keyframe_seconds": (1, 10, "Keyframe interval must be 1-10 seconds.")}
    for key, (lo, hi, msg) in checks.items():
        if key in partial:
            try:
                ok = lo <= int(partial[key]) <= hi
            except (TypeError, ValueError):
                ok = False
            if not ok:
                errors[key] = msg
    if "x264_preset" in partial and partial["x264_preset"] not in X264_PRESETS:
        errors["x264_preset"] = "Unknown encoder speed."
    return errors


def preset_name(cfg):
    q = effective(cfg)
    for name, p in PRESETS.items():
        if q["record_mode"] == p["record_mode"] and q["record_crf"] == p["record_crf"]:
            return name
    return "bitrate" if q["record_mode"] == "bitrate" else "custom"


def describe(cfg):
    q = effective(cfg)
    if q["record_mode"] == "bitrate":
        return (f"Fixed {q['video_bitrate_kbps']} kbps (about "
                f"{q['video_bitrate_kbps'] * 3600 / 8 / 1024 / 1024:.1f} GB per hour). "
                "Re-compress library can shrink these later.")
    return (f"Constant quality, CRF {q['record_crf']}: size follows the picture. "
            "Files are final; re-compressing gains nothing.")


def encoder_json(cfg):
    q = effective(cfg)
    base = {"preset": q["x264_preset"], "profile": "high", "tune": "",
            "keyint_sec": q["keyframe_seconds"], "x264opts": ""}
    if q["record_mode"] == "bitrate":
        return {**base, "rate_control": "CBR", "bitrate": q["video_bitrate_kbps"]}
    return {**base, "rate_control": "CRF", "crf": q["record_crf"]}


# --- OBS profile files ---------------------------------------------------------
def obs_base(env, platform):
    from playcap import detect
    return detect.obs_config_file(env, platform).parents[2]          # .../obs-studio


def profile_dir(env, platform):
    base = obs_base(env, platform)
    for name in ("user.ini", "global.ini"):          # OBS 31+ uses user.ini
        ini = base / name
        if ini.exists():
            m = re.search(r"^\[Basic\][^\[]*?^ProfileDir=([^\r\n]+)", _read(ini), re.M | re.S)
            if m:
                return base / "basic" / "profiles" / m.group(1).strip()
    return None


def _read(path):
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def ini_get(text, section, key):
    m = re.search(r"^\[%s\]\s*\n(.*?)(?=^\[|\Z)" % re.escape(section), text, re.M | re.S)
    if not m:
        return None
    k = re.search(r"^%s=(.*)$" % re.escape(key), m.group(1), re.M)
    return k.group(1).strip() if k else None


def ini_set(text, section, key, value):
    m = re.search(r"^\[%s\]\s*\n(.*?)(?=^\[|\Z)" % re.escape(section), text, re.M | re.S)
    if not m:
        return text.rstrip("\n") + f"\n\n[{section}]\n{key}={value}\n"
    body = m.group(1)
    if re.search(r"^%s=.*$" % re.escape(key), body, re.M):
        body = re.sub(r"^%s=.*$" % re.escape(key), lambda _: f"{key}={value}", body, flags=re.M)
    else:
        body = body.rstrip("\n") + f"\n{key}={value}\n\n"
    return text[:m.start(1)] + body + text[m.end(1):]


def current(env, platform):
    """What OBS will use on its next start: {mode, encoder} or None."""
    d = profile_dir(env, platform)
    if not d or not (d / "basic.ini").exists():
        return None
    text = _read(d / "basic.ini")
    enc = None
    try:
        enc = json.loads((d / "recordEncoder.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {"mode": ini_get(text, "Output", "Mode") or "Simple",
            "rec_encoder": ini_get(text, "AdvOut", "RecEncoder"), "encoder": enc}


def in_sync(cfg, env, platform):
    cur = current(env, platform)
    if not cur:
        return False
    want = encoder_json(cfg)
    enc = cur["encoder"] or {}
    return (cur["mode"] == "Advanced" and cur["rec_encoder"] == "obs_x264"
            and all(enc.get(k) == v for k, v in want.items()))


def apply(cfg, env, platform, obs_running):
    """Write the chosen encoding into OBS's profile. Only while OBS is closed
    (OBS rewrites basic.ini on exit). -> (changed, message)."""
    if obs_running:
        return False, "OBS is running; recording quality is applied the next time it starts."
    d = profile_dir(env, platform)
    if not d or not (d / "basic.ini").exists():
        return False, "No OBS profile yet. Start OBS once, close it, then try again."
    if in_sync(cfg, env, platform):
        return False, "OBS already records with these settings."
    ini = d / "basic.ini"
    raw = ini.read_bytes()
    text = raw.decode("utf-8-sig")
    for f in (ini, d / "recordEncoder.json"):
        bak = f.with_name(f.name + ".playcap-bak")
        if f.exists() and not bak.exists():
            shutil.copy2(f, bak)
    # Carry the simple-mode output folder, container and audio bitrate over,
    # so switching modes changes only the video encoding.
    folder = (cfg.get("output_dir") or ini_get(text, "SimpleOutput", "FilePath")
              or ini_get(text, "AdvOut", "RecFilePath") or "")
    fmt = ini_get(text, "SimpleOutput", "RecFormat2") or "mkv"
    for key, value in (("RecEncoder", "obs_x264"), ("RecType", "Standard"),
                       ("RecFormat2", fmt), ("RecTracks", "1"), ("RecUseRescale", "false")):
        text = ini_set(text, "AdvOut", key, value)
    if folder:
        text = ini_set(text, "AdvOut", "RecFilePath", str(folder).replace("\\", "/"))
    abr = ini_get(text, "SimpleOutput", "ABitrate")
    if abr:
        text = ini_set(text, "AdvOut", "Track1Bitrate", abr)
    text = ini_set(text, "Output", "Mode", "Advanced")
    ini.write_bytes((b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8"))
    (d / "recordEncoder.json").write_text(json.dumps(encoder_json(cfg)), encoding="utf-8")
    return True, "OBS will record with: " + describe(cfg)


def file_crf(path, head=4 << 20):
    """CRF x264 recorded with, read from the encoder's settings string in the
    file header, or None when the file is not x264 CRF."""
    try:
        with open(path, "rb") as f:
            data = f.read(head)
    except OSError:
        return None
    if not re.search(rb"rc=crf\b", data):
        return None
    m = re.search(rb" crf=([\d.]+)", data)
    return float(m.group(1)) if m else None
