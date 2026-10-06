import json

from playcap import record_quality as rq

BASIC_INI = (
    "\ufeff[General]\nName=Untitled\n\n"
    "[Output]\nMode=Simple\nFilenameFormatting=%CCYY-%MM-%DD %hh-%mm-%ss\n\n"
    "[SimpleOutput]\nFilePath=K:/lectures\nRecFormat2=mkv\nVBitrate=2500\nABitrate=160\n"
    "RecQuality=Stream\nx264Settings=keyint=60:min-keyint=60:scenecut=0\n\n"
    "[AdvOut]\nTrackIndex=1\nRecType=Standard\nEncoder=obs_x264\nRecFilePath=C:/Users/x/Videos\n"
    "RecFormat2=mkv\nRecEncoder=none\n\n"
)


def obs_home(tmp_path, ini=BASIC_INI):
    base = tmp_path / "obs-studio"
    (base / "basic" / "profiles" / "Untitled").mkdir(parents=True)
    # Like a real user.ini: more keys after ProfileDir, more sections after [Basic].
    (base / "user.ini").write_text("[General]\nX=1\n\n[Basic]\nProfile=Untitled\nProfileDir=Untitled\n"
                                   "SceneCollection=Untitled\n\n[BasicWindow]\ngeometry=AdnQ\n")
    (base / "basic" / "profiles" / "Untitled" / "basic.ini").write_text(ini, encoding="utf-8")
    return {"APPDATA": str(tmp_path)}, base / "basic" / "profiles" / "Untitled"


def test_apply_switches_obs_to_advanced_crf(tmp_path):
    env, prof = obs_home(tmp_path)
    changed, msg = rq.apply({}, env, "win32", obs_running=False)
    assert changed and "CRF 24" in msg
    text = (prof / "basic.ini").read_bytes()
    assert text.startswith(b"\xef\xbb\xbf")                       # BOM kept
    t = text.decode("utf-8-sig")
    assert rq.ini_get(t, "Output", "Mode") == "Advanced"
    assert rq.ini_get(t, "AdvOut", "RecEncoder") == "obs_x264"
    assert rq.ini_get(t, "AdvOut", "RecFilePath") == "K:/lectures"   # carried over from simple mode
    assert rq.ini_get(t, "AdvOut", "Track1Bitrate") == "160"
    enc = json.loads((prof / "recordEncoder.json").read_text())
    assert enc["rate_control"] == "CRF" and enc["crf"] == 24 and enc["keyint_sec"] == 2
    assert (prof / "basic.ini.playcap-bak").read_text(encoding="utf-8") == BASIC_INI
    assert rq.in_sync({}, env, "win32")
    assert rq.apply({}, env, "win32", obs_running=False)[0] is False     # idempotent


def test_apply_refuses_while_obs_runs(tmp_path):
    env, prof = obs_home(tmp_path)
    changed, msg = rq.apply({}, env, "win32", obs_running=True)
    assert not changed and "running" in msg
    assert rq.ini_get((prof / "basic.ini").read_text(encoding="utf-8-sig"), "Output", "Mode") == "Simple"


def test_bitrate_mode_and_output_dir_from_config(tmp_path):
    env, prof = obs_home(tmp_path)
    cfg = {"record_mode": "bitrate", "video_bitrate_kbps": 4000, "output_dir": r"D:\rec"}
    rq.apply(cfg, env, "win32", obs_running=False)
    enc = json.loads((prof / "recordEncoder.json").read_text())
    assert enc["rate_control"] == "CBR" and enc["bitrate"] == 4000 and "crf" not in enc
    t = (prof / "basic.ini").read_text(encoding="utf-8-sig")
    assert rq.ini_get(t, "AdvOut", "RecFilePath") == "D:/rec"
    assert not rq.in_sync({}, env, "win32")          # CRF wanted, CBR on disk


def test_no_profile_is_reported_not_crashed(tmp_path):
    changed, msg = rq.apply({}, {"APPDATA": str(tmp_path)}, "win32", obs_running=False)
    assert not changed and "Start OBS once" in msg


def test_validate_and_presets():
    assert rq.validate({"record_crf": 24, "x264_preset": "veryfast", "keyframe_seconds": 2}) == {}
    errs = rq.validate({"record_mode": "lossless", "record_crf": 50, "video_bitrate_kbps": "x",
                        "x264_preset": "placebo", "keyframe_seconds": 0})
    assert set(errs) == {"record_mode", "record_crf", "video_bitrate_kbps", "x264_preset", "keyframe_seconds"}
    assert rq.preset_name({}) == "balanced"
    assert rq.preset_name({"record_crf": 28}) == "small"
    assert rq.preset_name({"record_crf": 26}) == "custom"
    assert rq.preset_name({"record_mode": "bitrate"}) == "bitrate"
    assert "GB per hour" in rq.describe({"record_mode": "bitrate", "video_bitrate_kbps": 2500})


def test_file_crf_reads_the_x264_settings_string(tmp_path):
    crf = tmp_path / "crf.mkv"
    crf.write_bytes(b"\x00" * 100 + b"x264 - core 164 - options: cabac=1 rc=crf mbtree=1 crf=24.0 qcomp=0.60" + b"\x00" * 100)
    cbr = tmp_path / "cbr.mkv"
    cbr.write_bytes(b"options: rc=cbr mbtree=1 bitrate=2500 ratetol=1.0")
    assert rq.file_crf(crf) == 24.0
    assert rq.file_crf(cbr) is None
    assert rq.file_crf(tmp_path / "missing.mkv") is None
