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

A Windows monitor_capture created with empty settings gets monitor_id
"DUMMY" and records solid black -- found when a live run captured 39 minutes
of nothing. So our own capture input is always pointed at a real monitor:
the one matching OBS's canvas size (the fullscreened player fills it at native
resolution), primary first among those, else the primary, else the first
listed. Only our own input is repaired; a hand-picked monitor is kept as long
as OBS still lists it.

An item added over the websocket sits at scale 1 in the canvas's top-left
corner, so a monitor whose size differs from the canvas was cropped: with a
1080x1920 portrait monitor and a 1920x1080 canvas a live run kept the top of
the screen and cut the centred, fullscreened video in half. So our own item is
given a fit-to-canvas bounds box (letterboxed, follows later monitor switches),
but only while its transform is still OBS's untouched default.

enable_websocket() switches OBS's websocket server on by editing OBS's own
config file -- only while OBS is closed, because OBS rewrites that file on
exit. launch_obs() starts OBS the way the recorder's relaunch does.

Command line: `python -m playcap.obs_setup` does both UI buttons in one go.
"""
import json
import os
import re
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


# Capture kinds whose default settings select no monitor -> property naming it.
MONITOR_PROP = {"monitor_capture": "monitor_id"}
_SIZE = re.compile(r"(\d+)x(\d+)")


def pick_monitor(items, width, height):
    """Choose from OBS list-property items ({itemName, itemValue, ...}).

    Names look like 'MSI G241V: 1920x1080 @ -1920,8 (Primary Monitor)'."""
    items = [i for i in items if i.get("itemEnabled", True) and i.get("itemValue")]
    if not items:
        return None

    def size(i):
        m = _SIZE.search(i.get("itemName", ""))
        return (int(m.group(1)), int(m.group(2))) if m else None

    def primary(i):
        return "primary" in i.get("itemName", "").lower()

    fits = [i for i in items if size(i) == (width, height)]
    pool = fits or items
    return next((i for i in pool if primary(i)), pool[0])


def _point_at_monitor(obs, name, kind):
    """Give our capture input a real monitor if it has none. -> action or None."""
    prop = MONITOR_PROP.get(kind)
    if not prop:
        return None
    current = obs.request("GetInputSettings", {"inputName": name}) \
                 .get("inputSettings", {}).get(prop)
    items = obs.request("GetInputPropertiesListPropertyItems",
                        {"inputName": name, "propertyName": prop}).get("propertyItems", [])
    if current and current != "DUMMY" and any(i.get("itemValue") == current for i in items):
        return None
    video = obs.request("GetVideoSettings")
    choice = pick_monitor(items, video.get("baseWidth"), video.get("baseHeight"))
    if not choice:
        return None
    obs.request("SetInputSettings", {"inputName": name,
                                     "inputSettings": {prop: choice["itemValue"]}})
    return f"pointed '{name}' at {choice.get('itemName', 'a monitor')}"


def _untouched(t):
    """Is this OBS's default transform for an item added over the websocket?"""
    return (t.get("boundsType", "OBS_BOUNDS_NONE") == "OBS_BOUNDS_NONE"
            and t.get("scaleX", 1) == 1 and t.get("scaleY", 1) == 1
            and t.get("positionX", 0) == 0 and t.get("positionY", 0) == 0
            and t.get("rotation", 0) == 0)


def _fit_to_canvas(obs, scene, name):
    """Scale our capture item to fit the canvas if nobody has placed it. -> action or None."""
    item = obs.request("GetSceneItemId", {"sceneName": scene, "sourceName": name})["sceneItemId"]
    t = obs.request("GetSceneItemTransform", {"sceneName": scene, "sceneItemId": item}) \
           .get("sceneItemTransform", {})
    if not _untouched(t):
        return None
    video = obs.request("GetVideoSettings")
    w, h = video.get("baseWidth"), video.get("baseHeight")
    if not (w and h):
        return None
    obs.request("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item,
                                          "sceneItemTransform": {
        "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": w, "boundsHeight": h,
        "boundsAlignment": 0, "alignment": 5, "positionX": 0, "positionY": 0}})
    return f"fit '{name}' to the {w}x{h} canvas"


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

    own = {i["inputName"]: i.get("inputKind")
           for i in obs.request("GetInputList").get("inputs", [])}
    if CAPTURE_NAME in own:
        fixed = _point_at_monitor(obs, CAPTURE_NAME, own[CAPTURE_NAME])
        if fixed:
            actions.append(fixed)
        in_scene = {i.get("sourceName") for i in
                    obs.request("GetSceneItemList", {"sceneName": scene}).get("sceneItems", [])}
        if CAPTURE_NAME in in_scene:
            fit = _fit_to_canvas(obs, scene, CAPTURE_NAME)
            if fit:
                actions.append(fit)

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


def main(argv=None):
    """`python -m playcap.obs_setup`: the UI's Launch OBS + Set up recording
    scene, for command-line users. Starts OBS if it is closed (switching its
    websocket on first), then makes sure the capture scene exists."""
    import argparse
    argparse.ArgumentParser(
        prog="python -m playcap.obs_setup",
        description="Start OBS if it is closed (switching its websocket server on "
                    "first), then create or repair the 'playcap' screen-capture "
                    "scene. Same as the UI's Launch OBS + Set up recording scene.",
    ).parse_args(argv)
    from playcap.ui import server       # deferred: the UI module is heavier
    root = Path.cwd()
    steps = ["setup"] if is_obs_running() else ["launch", "setup"]
    for step in steps:
        ok, msg = server.obs_action(root, step)
        print(msg)
        if not ok:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
