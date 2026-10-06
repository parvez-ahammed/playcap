"""Make OBS ready to record the screen, so nobody has to click through OBS.

    ensure(obs) -> [what was changed]     empty list = already set up

Creates a scene named "playcap" holding a full-screen capture of the primary
display and makes it the program scene -- the recorder records whatever the
program scene shows. Idempotent: an existing scene or capture input is reused,
and a "playcap" scene that already holds any screen-capture source is left
alone, so a hand-tuned setup is not overwritten.

Recording bitrate and keyframe settings are not set here; the recorder applies
them at the start of every run (recorder.apply_output_settings).

The capture kind differs per platform; the first one this OBS offers wins.

enable_websocket() switches OBS's websocket server on by editing OBS's own
config file -- only while OBS is closed, because OBS rewrites that file on
exit. launch_obs() starts OBS the way the recorder's relaunch does.
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from playcap import detect
from playcap.settings import atomic_write_json


SCENE = "playcap"
CAPTURE_NAME = "playcap display"
CAPTURE_KINDS = ("monitor_capture",                    # Windows
                 "screen_capture", "display_capture",  # macOS (new, old)
                 "pipewire-desktop-capture-source",    # Linux, Wayland
                 "xshm_input")                         # Linux, X11


def capture_kind(kinds):
    for k in CAPTURE_KINDS:
        if k in kinds:
            return k
    return None


def ensure(obs, scene=SCENE):
    actions = []
    kinds = obs.request("GetInputKindList", {"unversioned": True}).get("inputKinds", [])
    kind = capture_kind(kinds)
    if not kind:
        return ["This OBS offers no screen capture source; add one by hand."]

    listing = obs.request("GetSceneList")
    scenes = {s["sceneName"] for s in listing.get("scenes", [])}
    if scene not in scenes:
        obs.request("CreateScene", {"sceneName": scene})
        actions.append(f"created scene '{scene}'")

    items = obs.request("GetSceneItemList", {"sceneName": scene}).get("sceneItems", [])
    has_capture = any(i.get("inputKind") in CAPTURE_KINDS for i in items)
    if not has_capture:
        existing = {i["inputName"]: i.get("inputKind")
                    for i in obs.request("GetInputList").get("inputs", [])}
        if CAPTURE_NAME in existing:
            obs.request("CreateSceneItem", {"sceneName": scene, "sourceName": CAPTURE_NAME})
            actions.append(f"added '{CAPTURE_NAME}' to '{scene}'")
        else:
            obs.request("CreateInput", {"sceneName": scene, "inputName": CAPTURE_NAME,
                                        "inputKind": kind, "inputSettings": {},
                                        "sceneItemEnabled": True})
            actions.append(f"added a screen capture ({kind})")

    if listing.get("currentProgramSceneName") != scene:
        obs.request("SetCurrentProgramScene", {"sceneName": scene})
        actions.append(f"switched OBS to scene '{scene}'")
    return actions


# --- websocket on/off and launching OBS ------------------------------------------
def is_obs_running():
    """Is an OBS process up? (Windows: tasklist; elsewhere: pgrep.)"""
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"],
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return "obs64.exe" in out.lower()
        return subprocess.run(["pgrep", "-xi", "obs"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def enable_websocket(env=None, platform=None, obs_running=None):
    """Turn OBS's websocket server on by editing OBS's own config file.

    Only while OBS is closed: OBS rewrites the file when it exits, so an edit
    made while it runs would be lost. Keeps an existing password; creates one
    (auth on) when there is no file yet. A copy of the old file is kept as
    config.json.playcap-bak. Returns True when the file now has it enabled.
    """
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if is_obs_running() if obs_running is None else obs_running:
        return False
    f = detect.obs_config_file(env, platform)
    data = {}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        shutil.copy2(f, f.with_name(f.name + ".playcap-bak"))
    data["server_enabled"] = True
    data.setdefault("server_port", 4455)
    if not data.get("server_password"):
        data["server_password"] = secrets.token_urlsafe(18)
        data["auth_required"] = True
    data.setdefault("auth_required", True)
    data.setdefault("alerts_enabled", False)
    data.setdefault("first_load", False)
    f.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(f, data)
    return True


def clear_crash_markers(env=None, platform=None):
    """Remove OBS's leftover "unclean shutdown" markers, only while OBS is closed.

    OBS 30+ writes .sentinel/run_<uuid> when it starts and deletes it on a clean
    exit. A crash or a killed process leaves it behind, and the next start then
    stops at a "launch in safe mode?" dialog -- --disable-shutdown-check does not
    skip it on current versions -- so the websocket never comes up and an
    unattended run waits forever. Deleting the markers of runs that are no
    longer running is exactly what a clean exit would have done.
    Returns how many were removed."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if is_obs_running():
        return 0
    base = detect.obs_config_file(env, platform).parents[2]      # .../obs-studio
    removed = 0
    for marker in (base / ".sentinel").glob("run_*"):
        try:
            marker.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def launch_obs(exe):
    """Start OBS detached, from its own folder (it finds its data relative to
    the working directory). Stale crash markers are cleared first and
    --disable-shutdown-check is passed for older versions, so no
    crash-recovery prompt blocks the websocket server from starting."""
    clear_crash_markers()
    exe = Path(exe)
    subprocess.Popen([str(exe), "--disable-shutdown-check"], cwd=str(exe.parent),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
