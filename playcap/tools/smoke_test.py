"""End-to-end rehearsal on ONE page, stage by stage, through the configured adapter.

Every assumption the recorder makes gets checked here before a batch, so a
failure points at exactly which one was wrong. Records only ~25 s, then
verifies a captured frame is not black: some DRM renders black to screen
capture while looking fine on screen, and nothing in the DOM reveals it.

Usage:  python -m playcap.tools.smoke_test "<page-url>"
"""
import subprocess
import sys
import time

from playcap import cdp, config
from playcap.adapters.base import VIDEO_STATE_JS, Item
from playcap.obs_client import Obs, ObsError
from playcap import screen
from playcap.adapters.base import ItemFailed
from playcap.recorder import EXIT_FULLSCREEN_JS, gesture, start_playback

RECORD_SECONDS = 25


def step(n, msg):
    print(f"\n[{n}] {msg}")


def fail(msg):
    print(f"\n*** FAILED: {msg}")
    sys.exit(1)


def black_check(ffmpeg, path):
    """Decode one frame to 8-bit gray and average it. ~0 means a black capture."""
    out = subprocess.run(
        [ffmpeg, "-v", "error", "-ss", "8", "-i", str(path),
         "-frames:v", "1", "-vf", "format=gray,scale=160:90",
         "-f", "rawvideo", "-"],
        capture_output=True)
    if not out.stdout:
        return None, out.stderr.decode(errors="replace")[:200]
    data = out.stdout
    return sum(data) / len(data), None


def main(url):
    cfg, adapter = config.load()
    item = Item(id="smoke", title="smoke test", url=url)

    step(1, "Preflight: Chrome debug port + OBS websocket")
    page = adapter.page_target(cfg, item)
    print("    chrome tab:", page["url"][:80])
    try:
        obs = Obs(cfg["obs_password"], cfg["obs_ws_url"])
    except ObsError as exc:
        fail(str(exc))
    print("    obs:", obs.version()["obsVersion"])
    scene = obs.current_scene()
    items = [(i["sourceName"], i["sceneItemEnabled"]) for i in obs.scene_items(scene)]
    print(f"    scene '{scene}':", items)
    if not any(en for _, en in items):
        fail(f"No enabled source in OBS scene '{scene}' -- it would record nothing.")

    step(2, "Navigate")
    sess = cdp.Session(page)
    sess.bring_to_front()
    sess.navigate(url, settle=8)
    print("    landed on:", sess.js("location.href")[:100])
    adapter.check_page(sess, item)

    step(3, "Locate the player")
    rect = None
    for _ in range(20):
        rect = adapter.find_player(sess)
        if rect:
            break
        time.sleep(1)
    print("    player rect:", rect)
    if not rect:
        fail("No player found. iframes: " + str(sess.js(
            "JSON.stringify([...document.querySelectorAll('iframe')].map(f=>f.src))")))

    step(4, "Attach to the player and read <video>")
    player = adapter.attach_player(sess, rect)
    if not player:
        for t in cdp.targets(("page", "iframe", "other", "webview")):
            print("      ", t["type"], "|", t["url"][:80])
        fail("Cannot attach to the player -- end-of-video detection unavailable.")
    read = lambda: player.session.js_json(VIDEO_STATE_JS % player.video)  # noqa: E731
    print("    video:", read())

    step(5, "Aim OBS at this browser and keep it on top (as the recorder does)")
    moved = screen.aim_capture(obs, screen.window_bounds(sess))
    print("   ", moved or "OBS already captures the browser's monitor")
    pin = screen.Pin(sess).__enter__()
    if pin.note:
        print("   ", pin.note)

    step(6, "Start playback: wait until playable, then a trusted click (recorder.start_playback)")
    try:
        st = start_playback(sess, player, rect)
    except ItemFailed as exc:
        pin.__exit__(None, None, None)
        fail(f"Playback would not start via CDP input ({exc}).")
    print(f"    PLAYING -- duration {st['duration']:.1f}s, {st['w']}x{st['h']}")

    step(7, "Fullscreen")
    if not gesture(sess, adapter.fullscreen_js(rect)):
        fail("fullscreen failed")
    time.sleep(3)
    print("    after:", sess.js("JSON.stringify({w: innerWidth, h: innerHeight, "
                                 "fs: !!document.fullscreenElement})"))

    step(8, f"OBS record {RECORD_SECONDS}s")
    obs.start_record()
    t0 = time.time()
    while time.time() - t0 < RECORD_SECONDS:
        time.sleep(5)
        s = read()
        print(f"    t={s['t']:.1f}/{s['duration']:.1f} paused={s['paused']} "
              f"ended={s['ended']} ready={s['readyState']}")
    path = obs.stop_record()
    time.sleep(2)
    print("    wrote:", path)

    step(9, "Verify the capture is not black")
    mean, err = black_check(cfg["ffmpeg"], path)
    if mean is None:
        print("    could not decode a frame:", err)
    else:
        print(f"    mean luma = {mean:.1f} "
              f"({'BLACK' if mean < 3 else 'OK, real picture'})")
        if mean < 3:
            print("    Either OBS is capturing the wrong screen (check the 'playcap' scene in\n"
                  "    OBS), or this site's player hides its video from screen capture\n"
                  "    (protected playback). playcap records only what the screen shows and\n"
                  "    does not work around protected players, so such a site cannot be recorded.")

    # Keep the test clip, but out of the library: it is not an item.
    try:
        from pathlib import Path
        keep = Path(cfg["output_dir"]) / "_partial" / "tests"
        keep.mkdir(parents=True, exist_ok=True)
        moved = keep / Path(path).name
        from playcap.recorder import move_file
        if not move_file(Path(path), moved, tries=3):
            raise OSError("move failed")
        print("    test clip kept at:", moved)
    except OSError as exc:
        print("    test clip left at:", path, f"({exc})")

    try:
        gesture(sess, EXIT_FULLSCREEN_JS, timeout=10)
    except Exception:
        sess.key("Escape", code="Escape", vk=27)
    pin.__exit__(None, None, None)
    obs.close()
    print("\nSmoke test finished.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: python -m playcap.tools.smoke_test "<page-url>"')
    main(sys.argv[1])
