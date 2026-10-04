"""Find the tools playcap drives, so nobody has to type an install path.

    find_tool(name)          chrome | obs | ffmpeg | ffprobe -> path or None
    resolve(name, cfg)       a configured path when it still exists, else find_tool
    obs_websocket()          OBS's own websocket settings (url, password, enabled)
    report(cfg)              what the setup wizard shows: {name: {path, ok}}

Where it looks: the standard per-machine and per-user install folders on
Windows, the usual app bundles on macOS, then PATH. Everything that touches
the outside world (environment, PATH lookup, platform) is a parameter, so the
tests can point it at a fake directory tree.

ffmpeg and ffprobe are looked up as a pair: some programs (ImageMagick on
Windows, for one) ship an ffmpeg.exe without ffprobe, and optimize.py needs
both from one build. A directory holding both wins over a lone ffmpeg found
earlier on PATH.

The OBS websocket password is read from OBS's own config file
(plugin_config/obs-websocket/config.json), which is what lets the wizard skip
"open OBS, Tools, WebSocket Server Settings, copy the password".
"""
import json
import os
import shutil
import sys
from pathlib import Path

TOOLS = ("chrome", "obs", "ffmpeg", "ffprobe")
CONFIG_KEY = {"chrome": "chrome_exe", "obs": "obs_exe",
              "ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}
EXE_NAMES = {
    "chrome": ["chrome.exe", "chrome", "google-chrome", "google-chrome-stable",
               "chromium", "chromium-browser"],
    "obs": ["obs64.exe", "obs.exe", "obs"],
    "ffmpeg": ["ffmpeg.exe", "ffmpeg"],
    "ffprobe": ["ffprobe.exe", "ffprobe"],
}


def _install_paths(name, env):
    """Absolute paths where an installer usually puts the tool."""
    pf = env.get("ProgramFiles", r"C:\Program Files")
    pf86 = env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = env.get("LOCALAPPDATA", "")
    out = []
    if name == "chrome":
        for base in (pf, pf86, local):
            if base:
                out.append(Path(base) / "Google/Chrome/Application/chrome.exe")
        out.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    elif name == "obs":
        for base in (pf, pf86):
            out.append(Path(base) / "obs-studio/bin/64bit/obs64.exe")
        out.append(Path("/Applications/OBS.app/Contents/MacOS/OBS"))
    return out


# Well-known ffmpeg locations beyond PATH (module-level so tests can clear it).
SYSTEM_DIRS = [r"C:\msys64\mingw64\bin", r"C:\msys64\ucrt64\bin", r"C:\ffmpeg\bin",
               r"C:\ProgramData\chocolatey\bin", "/opt/homebrew/bin", "/usr/local/bin",
               "/usr/bin"]


def _tool_dirs(env):
    """Directories that commonly hold an ffmpeg build, PATH first."""
    dirs = [d for d in env.get("PATH", "").split(os.pathsep) if d]
    local = env.get("LOCALAPPDATA", "")
    if local:
        dirs.append(str(Path(local) / "Microsoft/WinGet/Links"))
    return dirs + SYSTEM_DIRS


def _in_dir(directory, name):
    for exe in EXE_NAMES[name]:
        p = Path(directory) / exe
        if p.is_file():
            return p
    return None


def find_tool(name, env=None, which=shutil.which):
    env = os.environ if env is None else env
    if name in ("ffmpeg", "ffprobe"):
        other = "ffprobe" if name == "ffmpeg" else "ffmpeg"
        lone = None
        for d in _tool_dirs(env):
            hit = _in_dir(d, name)
            if hit and _in_dir(d, other):
                return str(hit)
            lone = lone or hit
        found = which(name)
        return found or (str(lone) if lone else None)
    for p in _install_paths(name, env):
        if p.is_file():
            return str(p)
    for exe in EXE_NAMES[name]:
        found = which(exe)
        if found:
            return found
    return None


def resolve(name, cfg, env=None, which=shutil.which):
    """A configured value wins if it still points at something real."""
    value = (cfg or {}).get(CONFIG_KEY[name])
    if value:
        if Path(value).is_file():
            return str(value)
        if os.sep not in value and "/" not in value:   # bare command name
            found = which(value)
            if found:
                return found
    return find_tool(name, env=env, which=which)


def obs_config_file(env, platform):
    if platform.startswith("win"):
        base = Path(env.get("APPDATA", ""))
    elif platform == "darwin":
        base = Path(env.get("HOME", "~")) / "Library/Application Support"
    else:
        base = Path(env.get("HOME", "~")) / ".config"
    return base / "obs-studio/plugin_config/obs-websocket/config.json"


def obs_websocket(env=None, platform=None):
    env = os.environ if env is None else env
    platform = platform or sys.platform
    try:
        data = json.loads(obs_config_file(env, platform).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    port = data.get("server_port", 4455)
    password = data.get("server_password", "") if data.get("auth_required", True) else ""
    return {"url": f"ws://127.0.0.1:{port}", "password": password,
            "enabled": bool(data.get("server_enabled", False))}


def obs_settings(user_cfg, found=None):
    """(url, password) for obs-websocket. Values typed into config.json win;
    anything left out comes from OBS's own config file, so a setup where the
    user never copied the password still connects."""
    url, password = user_cfg.get("obs_ws_url"), user_cfg.get("obs_password")
    if url and password:
        return url, password
    found = obs_websocket() if found is None else found
    found = found or {}
    return (url or found.get("url") or "ws://127.0.0.1:4455",
            password or found.get("password", ""))


def report(cfg, env=None, which=shutil.which):
    out = {}
    for name in TOOLS:
        path = resolve(name, cfg, env=env, which=which)
        out[name] = {"path": path, "ok": bool(path)}
    return out
