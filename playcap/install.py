"""One-click installs of the programs playcap drives, through winget.

    available()          winget answers `winget --version` (Windows only)
    command(tool)        the fixed argv for one tool, or None
    start(root, tool)    run it as the "install" job (output in install.log)
    status(root)         {winget, running, packages, log} for the UI

Only the tools in PACKAGES can be installed, and the UI sends a tool name
(chrome / obs / ffmpeg / ffprobe), never a package id or a command: the
argv is built here from the fixed table, so a request cannot make the
server run anything else. ffprobe ships inside the ffmpeg package.

Package ids, checked against microsoft/winget-pkgs (manifests/o/OBSProject/
OBSStudio, manifests/g/Gyan/FFmpeg, manifests/g/Google/Chrome). Gyan.FFmpeg
is a portable zip whose ffmpeg/ffprobe aliases land in
%LOCALAPPDATA%\\Microsoft\\WinGet\\Links, which detect.find_tool already
searches -- so a fresh install is found without restarting the UI, whose
PATH predates it.

The install runs as an ordinary job (playcap.jobs) so it outlives a page
reload and its output is readable through /api/log?job=install. When it
finishes, the page re-runs detection. Where winget is missing (other
platforms, older Windows) the UI shows the download link instead.
"""
import subprocess
import sys

from playcap import jobs

PACKAGES = {"obs": "OBSProject.OBSStudio", "ffmpeg": "Gyan.FFmpeg",
            "chrome": "Google.Chrome"}
ALIASES = {"ffprobe": "ffmpeg"}
_cache = {}


def available(platform=None, run=subprocess.run):
    """winget is on this machine. Cached: the check spawns a process."""
    platform = platform or sys.platform
    if not platform.startswith("win"):
        return False
    if "winget" not in _cache:
        try:
            out = run(["winget", "--version"], capture_output=True, text=True, timeout=15,
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _cache["winget"] = out.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _cache["winget"] = False
    return _cache["winget"]


def package(tool):
    return PACKAGES.get(ALIASES.get(tool, tool))


def command(tool):
    pkg = package(tool)
    if not pkg:
        return None
    return ["winget", "install", "--id", pkg, "-e",
            "--accept-source-agreements", "--accept-package-agreements"]


def start(root, tool):
    cmd = command(str(tool))
    if not cmd:
        return False, "playcap can only install Chrome, OBS and ffmpeg."
    if not available():
        return False, "winget is not available here; use the download link instead."
    ok, msg = jobs.start("install", root, {}, cmd=cmd)
    if ok:
        msg = (f"Installing {package(tool)} with winget. Windows may ask for permission; "
               "the output shows below.")
    return ok, msg


def status(root):
    st = jobs.status(root)["install"]
    return {"winget": available(), "running": st["running"],
            "packages": {t: package(t) for t in ("chrome", "obs", "ffmpeg", "ffprobe")},
            "log": jobs.tail(root, "install", 40)}
