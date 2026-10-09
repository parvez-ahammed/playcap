"""End-to-end rehearsal on ONE page, stage by stage, through the configured adapter.

Every assumption the recorder makes gets checked here before a batch, so a
failure points at exactly which one was wrong. Records only ~25 s, then
verifies a captured frame is not black: some DRM renders black to screen
capture while looking fine on screen, and nothing in the DOM reveals it.

It records with the configured capture backend (playcap.capture: OBS over its
websocket, or ffmpeg run by playcap), through the same Capture surface and the
same aim / pin steps the recorder uses, so a pass means the real run's capture
path works. The black check decodes the finished clip with ffmpeg whichever
backend made it: the clip on disk is what matters, not a live preview.

Whatever happens -- a failed step, an exception, Stop in the UI -- the
cleanup runs: the recording is stopped, the page leaves fullscreen and the
browser window is un-pinned. A black or undecodable capture is a failure, not
a pass.

Usage:  python -m playcap.tools.smoke_test "<page-url>"
"""
import subprocess
import sys
import time
from pathlib import Path

from playcap import capture, cdp, config, detect, jobs
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
    jobs.graceful_signals()
    undo = []              # cleanup steps, run last-first however the test ends
    try:
        _run(url, undo)
    finally:
        for fn in reversed(undo):
            try:
                fn()
            except Exception:
                pass


def _stop_if_recording(cap):
    if cap.is_recording():
        cap.stop()


def open_capture(cfg, fail=fail):
    """The configured backend as a capture.Capture, checked enough to trust a
    25 s recording. -> (cap, label)."""
    if capture.backend_name(cfg) == "ffmpeg":
        try:
            # Its own folder: keep_clip then moves the clip into _partial/tests.
            cap = capture.FfmpegCapture(cfg, Path(cfg["output_dir"]) / "_partial",
                                        state_dir=jobs.STATE_DIR)
        except ObsError as exc:
            fail(f"ffmpeg capture is not usable: {exc}")
        print("    ffmpeg:", cap.describe())
        return cap, "ffmpeg"
    try:
        obs = Obs(cfg["obs_password"], cfg["obs_ws_url"])
    except ObsError as exc:
        fail(str(exc))
    cap = capture.ObsCapture(obs)
    print("    obs:", obs.version()["obsVersion"])
    scene = obs.current_scene()
    items = [(i["sourceName"], i["sceneItemEnabled"]) for i in obs.scene_items(scene)]
    print(f"    scene '{scene}':", items)
    if not any(en for _, en in items):
        cap.close()
        fail(f"No enabled source in OBS scene '{scene}' -- it would record nothing.")
    return cap, "OBS"


def _leave_fullscreen(sess):
    try:
        gesture(sess, EXIT_FULLSCREEN_JS, timeout=10)
    except Exception:
        sess.key("Escape", code="Escape", vk=27)


def _run(url, undo):
    cfg, adapter = config.load()
    item = Item(id="smoke", title="smoke test", url=url)

    step(1, f"Preflight: Chrome debug port + screen recorder ({capture.label(cfg)})")
    page = adapter.page_target(cfg, item)
    print("    chrome tab:", page["url"][:80])
    cap, rec = open_capture(cfg)
    undo.append(cap.close)
    undo.append(lambda: _stop_if_recording(cap))

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

    step(5, f"Aim {rec} at this browser and keep it on top (as the recorder does)")
    moved = capture.aim(cap, screen.window_bounds(sess))
    print("   ", moved or f"{rec} already captures the browser's monitor")
    pin = screen.Pin(sess).__enter__()
    undo.append(lambda: pin.__exit__(None, None, None))
    if pin.note:
        print("   ", pin.note)
    moved = capture.aim_window(cap, pin.hwnd)        # ffmpeg only: exact monitor
    if moved:
        print("   ", moved)

    step(6, "Start playback: wait until playable, then a trusted click (recorder.start_playback)")
    try:
        st = start_playback(sess, player, rect)
    except ItemFailed as exc:
        fail(f"Playback would not start via CDP input ({exc}).")
    print(f"    PLAYING -- duration {st['duration']:.1f}s, {st['w']}x{st['h']}")

    step(7, "Fullscreen")
    undo.append(lambda: _leave_fullscreen(sess))
    if not gesture(sess, adapter.fullscreen_js(rect)):
        fail("fullscreen failed")
    time.sleep(3)
    print("    after:", sess.js("JSON.stringify({w: innerWidth, h: innerHeight, "
                                 "fs: !!document.fullscreenElement})"))

    step(8, f"{rec} record {RECORD_SECONDS}s")
    cap.start()
    t0 = time.time()
    while time.time() - t0 < RECORD_SECONDS:
        time.sleep(5)
        cap.check()                    # ffmpeg died mid-test -> fail loudly
        s = read()
        print(f"    t={s['t']:.1f}/{s['duration']:.1f} paused={s['paused']} "
              f"ended={s['ended']} ready={s['readyState']}")
    path = cap.stop()
    time.sleep(2)
    print("    wrote:", path)

    step(9, "Verify the capture is not black")
    mean, err = black_check(detect.resolve("ffmpeg", cfg) or cfg["ffmpeg"], path)
    keep_clip(cfg, path)
    if mean is None:
        fail(f"could not decode a frame of the test clip: {err}")
    print(f"    mean luma = {mean:.1f} "
          f"({'BLACK' if mean < 3 else 'OK, real picture'})")
    if mean < 3:
        where = ("(check the\n    'playcap' scene in OBS)" if rec == "OBS"
                 else "(keep the\n    browser window on screen, not minimized)")
        fail(f"the capture is black. Either {rec} is capturing the wrong screen {where},\n"
             "    or this site's player hides its video from screen capture (protected\n"
             "    playback). playcap records only what the screen shows and does not work\n"
             "    around protected players, so such a site cannot be recorded.")
    print("\nSmoke test finished.")


def keep_clip(cfg, path):
    """Keep the test clip, but out of the library: it is not an item."""
    from playcap.recorder import move_file
    try:
        keep = Path(cfg["output_dir"]) / "_partial" / "tests"
        keep.mkdir(parents=True, exist_ok=True)
        moved = keep / Path(path).name
        if not move_file(Path(path), moved, tries=3):
            raise OSError("move failed")
        print("    test clip kept at:", moved)
    except OSError as exc:
        print("    test clip left at:", path, f"({exc})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: python -m playcap.tools.smoke_test "<page-url>"')
    main(sys.argv[1])
