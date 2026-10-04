import json
import os

import pytest

from playcap import detect


@pytest.fixture(autouse=True)
def no_system_dirs(monkeypatch):
    monkeypatch.setattr(detect, "SYSTEM_DIRS", [])


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def env_for(tmp_path):
    return {"ProgramFiles": str(tmp_path / "pf"),
            "ProgramFiles(x86)": str(tmp_path / "pf86"),
            "LOCALAPPDATA": str(tmp_path / "local"),
            "APPDATA": str(tmp_path / "roaming"),
            "HOME": str(tmp_path / "home")}


def no_which(_name):
    return None


def test_finds_chrome_in_program_files(tmp_path):
    exe = touch(tmp_path / "pf/Google/Chrome/Application/chrome.exe")
    assert detect.find_tool("chrome", env=env_for(tmp_path), which=no_which) == str(exe)


def test_finds_chrome_per_user_install(tmp_path):
    exe = touch(tmp_path / "local/Google/Chrome/Application/chrome.exe")
    assert detect.find_tool("chrome", env=env_for(tmp_path), which=no_which) == str(exe)


def test_finds_obs(tmp_path):
    exe = touch(tmp_path / "pf/obs-studio/bin/64bit/obs64.exe")
    assert detect.find_tool("obs", env=env_for(tmp_path), which=no_which) == str(exe)


def test_ffmpeg_prefers_dir_that_also_has_ffprobe(tmp_path):
    lonely = touch(tmp_path / "a/ffmpeg.exe")
    paired = touch(tmp_path / "b/ffmpeg.exe")
    touch(tmp_path / "b/ffprobe.exe")
    which = {"ffmpeg": str(lonely)}.get
    env = env_for(tmp_path)
    env["PATH"] = os.pathsep.join([str(tmp_path / 'a'), str(tmp_path / 'b')])
    assert detect.find_tool("ffmpeg", env=env, which=which) == str(paired)
    assert detect.find_tool("ffprobe", env=env, which=no_which) == str(tmp_path / "b/ffprobe.exe")


def test_ffmpeg_falls_back_to_which(tmp_path):
    assert detect.find_tool("ffmpeg", env=env_for(tmp_path),
                            which=lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None) \
        == "/usr/bin/ffmpeg"


def test_missing_tool_is_none(tmp_path):
    assert detect.find_tool("obs", env=env_for(tmp_path), which=no_which) is None


def test_configured_path_wins_when_it_exists(tmp_path):
    exe = touch(tmp_path / "custom dir/it's chrome.exe")
    touch(tmp_path / "pf/Google/Chrome/Application/chrome.exe")
    got = detect.resolve("chrome", {"chrome_exe": str(exe)}, env=env_for(tmp_path), which=no_which)
    assert got == str(exe)


def test_configured_path_that_vanished_falls_back(tmp_path):
    exe = touch(tmp_path / "pf/Google/Chrome/Application/chrome.exe")
    got = detect.resolve("chrome", {"chrome_exe": str(tmp_path / "gone.exe")},
                         env=env_for(tmp_path), which=no_which)
    assert got == str(exe)


def test_bare_command_in_config_is_resolved_via_which(tmp_path):
    got = detect.resolve("ffmpeg", {"ffmpeg": "ffmpeg"}, env=env_for(tmp_path),
                         which=lambda n: "/opt/ffmpeg" if n == "ffmpeg" else None)
    assert got == "/opt/ffmpeg"


def test_obs_websocket_reads_obs_config(tmp_path):
    cfg = tmp_path / "roaming/obs-studio/plugin_config/obs-websocket/config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"server_enabled": True, "server_port": 4466,
                               "auth_required": True, "server_password": "s3cret"}))
    got = detect.obs_websocket(env=env_for(tmp_path), platform="win32")
    assert got == {"url": "ws://127.0.0.1:4466", "password": "s3cret", "enabled": True}


def test_obs_websocket_no_auth_means_empty_password(tmp_path):
    cfg = tmp_path / "roaming/obs-studio/plugin_config/obs-websocket/config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"server_enabled": False, "server_port": 4455,
                               "auth_required": False, "server_password": "ignored"}))
    got = detect.obs_websocket(env=env_for(tmp_path), platform="win32")
    assert got == {"url": "ws://127.0.0.1:4455", "password": "", "enabled": False}


def test_obs_websocket_missing_or_corrupt(tmp_path):
    assert detect.obs_websocket(env=env_for(tmp_path), platform="win32") is None
    cfg = tmp_path / "roaming/obs-studio/plugin_config/obs-websocket/config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{not json")
    assert detect.obs_websocket(env=env_for(tmp_path), platform="win32") is None


def test_obs_websocket_linux_path(tmp_path):
    cfg = tmp_path / "home/.config/obs-studio/plugin_config/obs-websocket/config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"server_enabled": True, "server_port": 4455,
                               "auth_required": True, "server_password": "p"}))
    assert detect.obs_websocket(env=env_for(tmp_path), platform="linux")["password"] == "p"


def test_report_shape(tmp_path):
    touch(tmp_path / "pf/Google/Chrome/Application/chrome.exe")
    rep = detect.report({}, env=env_for(tmp_path), which=no_which)
    assert set(rep) == {"chrome", "obs", "ffmpeg", "ffprobe"}
    assert rep["chrome"]["ok"] is True and rep["obs"] == {"path": None, "ok": False}


def test_obs_settings_prefers_config_then_detected():
    found = {"url": "ws://127.0.0.1:4466", "password": "det", "enabled": True}
    assert detect.obs_settings({"obs_ws_url": "ws://h:1", "obs_password": "mine"}, found) == ("ws://h:1", "mine")
    assert detect.obs_settings({}, found) == ("ws://127.0.0.1:4466", "det")
    assert detect.obs_settings({"obs_password": ""}, found) == ("ws://127.0.0.1:4466", "det")
    assert detect.obs_settings({}, {}) == ("ws://127.0.0.1:4455", "")
