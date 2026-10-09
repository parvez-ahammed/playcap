"""Record every queued item, one at a time, and survive being interrupted.

Flow per item: navigate -> aim OBS at the browser's monitor, pin the
browser window on top, check OBS is not already black (playcap.screen) ->
click the player (a CDP click is a trusted gesture,
which is what starts gesture-gated playback; a synthetic JS
.click() does not) -> fullscreen the player element so the video renders at
its native resolution instead of the page's player box -> OBS records the
screen -> poll the <video> until it ends -> stop, file it under the library
layout (playcap.organize), remux to mp4.

Everything site-specific -- which tab, which element is the player, where its
<video> can be polled, what "logged out" looks like -- comes from the adapter
named in config.json (see playcap.adapters.base). This module never names a
site.

Progress lands in the progress file after every item, so a crash or Ctrl+C
costs at most the item in flight. Rerunning skips whatever already finished.

Usage:  python record_all.py                 # every unlocked item
        python record_all.py --only lecture  # one kind of item (adapter-defined)
        python record_all.py --speed 2       # play at 2x: half the hours, half the GB
        python record_all.py --limit 1       # rehearse on one item
        python record_all.py --dry-run       # print the plan, record nothing

Items dated in the future have no video published yet, so they are left in the
queue untouched rather than counted as failures -- run this again after they
air and only the newly available ones get recorded. Failed items are retried on
the next run too; only "done" is permanent.

Black frames: some protected players render black to screen capture while
looking fine on screen. If the capture is black from the first frame for
BLOCKED_SECONDS while the page itself was not (preflight), the item fails as
CaptureBlocked at once and is not retried: that is the player refusing capture,
and playcap does not work around it. The DOM cannot show that, so each poll also asks the
capture backend for a tiny frame of what it records (OBS: a screenshot of the
program output; ffmpeg: its own preview BMP) and records its mean brightness in
.playcap/now.json; the UI flags a near-zero value. A short dark stretch is
only a warning (intros and fades exist), but BLACK_ABORT_SECONDS of unbroken
black fails the item: a live run once captured a whole lecture as solid black
(OBS pointed at no monitor) and would have filed it as done. The item stays
queued. Rehearse with playcap.tools.smoke_test before a batch all the same.
These failure messages name the active recorder (capture.label), so an
ffmpeg user is never told to check OBS.

Live status and stopping (for the UI, see playcap.jobs): every poll writes
.playcap/now.json (title, position, brightness, and which item of this run it
is -- RUN_PLACE, so the UI can say "item 2 of 7"). A stop-now flag is checked
every second, also during retry and cooldown waits, and raises
KeyboardInterrupt so the normal cleanup runs; a stop-after-current flag is
checked between items. CTRL_BREAK is mapped to KeyboardInterrupt too.

The first item ever filed from this folder stamps .playcap/first_run.json
(playcap.firstrun): local-only install-time measurement, never sent anywhere.

Capture backend (playcap.capture): config "capture_backend" is "obs" (default)
or "ffmpeg". The OBS path is unchanged and still talks to obs_client.Obs
directly. With "ffmpeg" the `obs` variable below holds a capture.FfmpegCapture,
which answers the same start_record / stop_record / record_status calls; aiming
and brightness go through capture.aim / capture.luma, and capture.check raises
if ffmpeg died mid-item (OBS dying already surfaces through its websocket).
Fullscreen-before-recording and every black-frame rule apply to both.

Notifications and media-server refresh (playcap.notify, all optional, from
config.json): each finished item sends "recorded" and asks the media server to
rescan (debounced), a final failure sends "failed", a capture-blocked or black
capture sends "blocked", and the end of a run -- finished, stopped or exited
early -- sends "finished" with the counts, after a last refresh. Every send
runs on its own thread with a short timeout and swallows its own errors, so a
dead webhook can never cost an item or hold the run up; the end of the run
waits at most notify.FLUSH_SECONDS for the last messages.

"blocked" is sent once per item, not once per run. A blocked item stays
"failed" and is tried again on every run, so a scheduled re-scan would repeat
the same alert forever. The progress entry carries "blocked_notified" (saved
with the entry, so it survives a crash and a rerun like everything else);
while the previous entry has it, a new block is counted but not announced,
and a run whose only news is such repeats sends no "finished" either. Any
other outcome -- recorded, or failed for another reason -- writes a fresh
entry without the marker, so a later block is announced again.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from playcap import capture, cdp, config, firstrun, jobs, notify, obs_setup, organize, record_quality, screen
from playcap.settings import atomic_write_json
from playcap.adapters.base import VIDEO_STATE_JS, CaptureBlocked, ItemFailed  # noqa: F401
from playcap.obs_client import Obs, ObsError

POLL_SECONDS = 10
STALL_LIMIT = 120          # no currentTime movement for this long -> intervene
MAX_REATTACH = 5           # player frame target replacements tolerated per item
MAX_STALL_FIXES = 3        # nudges before a frozen player is called a failure
START_TOLERANCE = 15       # seconds into the video we accept as "the beginning"
MAX_ATTEMPTS = 3           # tries per item before it is called failed
RETRY_WAIT_SECONDS = 90    # pause between tries, to ride out a blip
COOLDOWN_AFTER = 3         # consecutive failed items that trigger a long wait
COOLDOWN_SECONDS = 600
MIN_FREE_GB = 10
OBS_ANSWER_TRIES = 40      # x 3 s: how long to wait for a running OBS to answer
MIN_PLAYED_FRACTION = 0.9  # an "ended" video must have taken this share of its length
PLAYER_WAIT_SECONDS = 45   # how long to wait for the player to appear
READY_WAIT_SECONDS = 20    # how long to let the player buffer before the first click
PREFLIGHT_SECONDS = 8      # how long the capture may stay black before recording starts
BLACK_LUMA = 8.0           # mean program brightness (0-255) below this is "black"
BLACK_ABORT_SECONDS = 90   # unbroken black this long fails the item
BLOCKED_SECONDS = 30       # black from the very start this long = capture-blocked player

EXIT_FULLSCREEN_JS = ("(async()=>{if(document.fullscreenElement)"
                      " await document.exitFullscreen(); return true;})()")

NOW_FILE = Path(jobs.STATE_DIR) / "now.json"

CFG = {}
ADAPTER = None
RUN_PLACE = (None, None)     # (this item's number in the run, items in the run)
LAST_DURATION = None         # the last recorded video's length (s), for the notification


def black_failure(msg):
    """An ItemFailed that notifications report as a black/blocked capture."""
    exc = ItemFailed(msg)
    exc.black = True
    return exc


def write_now(item, s, duration, luma=None, started=None):
    """What the UI shows as "now recording". Best effort, never fatal."""
    try:
        NOW_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(NOW_FILE, {
            "id": str(item.id), "title": item.title, "t": s.get("t"),
            "duration": duration, "w": s.get("w"), "h": s.get("h"),
            "luma": luma, "elapsed": (time.time() - started) if started else None,
            "n": RUN_PLACE[0], "of": RUN_PLACE[1], "updated_at": time.time()})
    except OSError:
        pass


def clear_now():
    NOW_FILE.unlink(missing_ok=True)


def nap(seconds, after_current=False):
    """time.sleep that a stop-now request interrupts within a second; with
    after_current, so does stop-after-current (between items, e.g. the
    failure cooldown, there is no current item left to finish)."""
    end = time.time() + seconds
    while True:
        if jobs.requested(Path.cwd(), "record", "now"):
            raise KeyboardInterrupt("stop requested")
        if after_current and jobs.requested(Path.cwd(), "record", "after_current"):
            return
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


def stop_after_current():
    return jobs.consume(Path.cwd(), "record", "after_current")


class BlackWatch:
    """Times unbroken black. update() -> seconds black so far (0 = not black).
    An unreadable brightness (None) neither starts nor breaks a black run."""

    def __init__(self, threshold=BLACK_LUMA):
        self.threshold = threshold
        self.since = None

    def update(self, luma, now):
        if luma is None:
            return now - self.since if self.since is not None else 0.0
        if luma >= self.threshold:
            self.since = None
            return 0.0
        if self.since is None:
            self.since = now
        return now - self.since


def program_luma(obs):
    try:
        return capture.luma(obs)
    except Exception:
        return None


def _setup():
    global CFG, ADAPTER
    if ADAPTER is None:
        CFG, ADAPTER = config.load()
    return CFG, ADAPTER


def outdir():
    return Path(CFG["output_dir"])


def state(player):
    return player.session.js_json(VIDEO_STATE_JS % player.video)


def apply_output_settings(obs):
    """Recording bitrate + GOP for OBS's *simple* output mode. A 2 s keyframe
    interval keeps seeking cheap; OBS defaults to 250 frames, which makes every
    seek re-buffer ~8 s. Once record_quality has switched OBS to advanced
    mode these values are ignored by OBS (recordEncoder.json rules), so this
    only covers an OBS that has not been relaunched by playcap yet."""
    obs.request("SetProfileParameter", {
        "parameterCategory": "SimpleOutput", "parameterName": "VBitrate",
        "parameterValue": str(CFG.get("video_bitrate_kbps", 2500))})
    x264 = CFG.get("x264_settings")
    if x264:
        # OBS ignores x264Settings outright unless UseAdvanced is on, so the
        # keyframe interval silently stayed at the 250-frame default. Preset
        # has to be set too: advanced mode reads it instead of the quality
        # preset it was using before.
        obs.request("SetProfileParameter", {
            "parameterCategory": "SimpleOutput", "parameterName": "UseAdvanced",
            "parameterValue": "true"})
        obs.request("SetProfileParameter", {
            "parameterCategory": "SimpleOutput", "parameterName": "Preset",
            "parameterValue": CFG.get("x264_preset", "veryfast")})
        obs.request("SetProfileParameter", {
            "parameterCategory": "SimpleOutput", "parameterName": "x264Settings",
            "parameterValue": x264})


def load_progress():
    p = Path(CFG["progress_file"])
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_progress(p):
    # Atomic: the UI edits this file too (retry/skip), and a crash mid-write
    # must never leave half a progress file.
    atomic_write_json(Path(CFG["progress_file"]), p)


def library_path(item, index):
    """Where the finished recording goes, without extension, per the library
    layout (organize.relpath). The queue position is the number, so a
    re-record keeps the name the item already had."""
    return outdir() / organize.relpath(CFG, index, item, organize.clean_title(item.title, CFG))


def wait_ready(obs, seconds=120):
    """OBS accepts websocket connections before its frontend has finished
    loading, and answers profile requests with "OBS is not ready" until it has.
    Poll a harmless profile read until it sticks."""
    deadline = time.time() + seconds
    while True:
        try:
            obs.request("GetProfileList", timeout=10)
            return obs
        except ObsError as exc:
            if "not ready" not in str(exc).lower() or time.time() > deadline:
                raise
            nap(3)


def ensure_chrome():
    """Bring the debug Chrome back if it died.

    connect_obs() already revives OBS, but nothing revived Chrome, so a single
    browser crash turned every remaining item into a CdpError and failed six
    items in a row. The adapter's launcher must be idempotent: exit early when
    the port is already answering.
    """
    try:
        cdp.targets()
        return True
    except Exception:
        pass
    print("    debug Chrome is gone -- relaunching it")
    ADAPTER.relaunch_browser(CFG)
    for _ in range(20):
        nap(3)
        try:
            cdp.targets()
            print("    debug Chrome is back")
            return True
        except Exception:
            continue
    print("    debug Chrome did not come back")
    return False


def report_quality():
    """Say how this run will be encoded, and whether OBS actually uses it."""
    print(f"recording quality: {record_quality.describe(CFG)}")
    try:
        ok = record_quality.in_sync(CFG, os.environ, sys.platform)
    except Exception:
        ok = False
    if not ok:
        print("  !! OBS is not set to this yet -- it still records with its old settings "
              f"({CFG.get('video_bitrate_kbps')} kbps fixed). Close OBS and press "
              "Launch OBS in the UI to apply it.")


def connect_obs(relaunch=True):
    """Reconnect to OBS, starting it first if it is not running.

    OBS being closed mid-batch used to end the whole run; an unattended job
    should just bring it back and carry on.
    """
    try:
        return Obs(CFG["obs_password"], CFG["obs_ws_url"])
    except ObsError:
        if not relaunch or not CFG.get("obs_exe"):
            raise
    if obs_setup.is_obs_running():
        # Running but not answering: still starting, or its websocket is off /
        # the password is wrong. A second OBS would only fight the first over
        # one profile, so wait for this one instead.
        print("    OBS is running but not answering yet -- waiting for it")
        last = None
        for _ in range(OBS_ANSWER_TRIES):
            nap(3)
            try:
                return Obs(CFG["obs_password"], CFG["obs_ws_url"])
            except ObsError as exc:
                last = exc
            if not obs_setup.is_obs_running():
                break
        else:
            raise ObsError(f"OBS is running but its websocket does not answer: {last}\n"
                           "Check Tools -> WebSocket Server Settings, or close OBS and "
                           "let playcap start it.")
    exe = Path(CFG["obs_exe"])
    print(f"    OBS is not answering -- relaunching {exe.name}")
    # After a crash or a hard reboot OBS opens a "Crash Detected" modal and
    # waits for a click, so the websocket server never starts and an
    # unattended run stalls forever. --disable-shutdown-check skips the
    # prompt; --multi skips the "already running" one.
    # Newer OBS ignores that flag and prompts anyway while crash markers are
    # left over; clear them first (only acts while OBS is closed).
    obs_setup.clear_crash_markers()
    # OBS reads its recording encoder only at start: apply the chosen quality
    # while it is still closed.
    if not obs_setup.is_obs_running():
        try:
            changed, msg = record_quality.apply(CFG, os.environ, sys.platform, obs_running=False)
            if changed:
                print(f"    {msg}")
        except Exception as exc:
            print(f"    could not apply recording quality: {exc}")
    subprocess.Popen([str(exe), "--disable-shutdown-check", "--multi"],
                     cwd=str(exe.parent),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    for _ in range(20):
        nap(3)
        try:
            obs = connect_obs(relaunch=False)
        except ObsError:
            continue
        print("    OBS is back")
        return wait_ready(obs)
    raise ObsError("OBS did not come back after a relaunch")


def diagnose(sess):
    """What the page actually looked like when we gave up waiting for a player."""
    try:
        return sess.js(
            "JSON.stringify({url: location.href.slice(-60),"
            " ready: document.readyState,"
            " fs: !!document.fullscreenElement,"
            " hidden: document.hidden,"
            " iframes: [...document.querySelectorAll('iframe')].map(f=>(f.src||'').slice(0,45)),"
            r" text: (document.body.innerText||'').replace(/\s+/g,' ').slice(0, 260)})",
            timeout=15)
    except Exception as exc:
        return f"(diagnostics failed: {exc.__class__.__name__}: {exc})"


def free_gb(path):
    """Free space where `path` will live. A fresh setup's output_dir does not
    exist yet (a dry run must not create it), so measure its nearest existing
    ancestor."""
    path = Path(path).resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(str(path)).free / 1024 ** 3


def gesture(sess, expr, timeout=25):
    """Runtime.evaluate with userGesture -- required for requestFullscreen()."""
    res = sess.call("Runtime.evaluate",
                    {"expression": expr, "returnByValue": True,
                     "awaitPromise": True, "userGesture": True}, timeout=timeout)
    if res.get("exceptionDetails"):
        raise ItemFailed("JS threw: " + str(res["exceptionDetails"].get("text")))
    return res.get("result", {}).get("value")


def wait_playable(player, seconds=READY_WAIT_SECONDS):
    """Wait until the <video> has data to play (readyState >= 3). A click on a
    player that is still loading was ignored twice in four live starts and
    burned a 90 s retry each time. Returns the last state seen; never fails --
    some players only load after the first click."""
    st = {}
    for _ in range(int(seconds * 2)):
        try:
            st = state(player)
        except Exception:
            st = {}
        if st.get("found") and st.get("readyState", 0) >= 3:
            break
        nap(0.5)
    return st


def preflight(obs, seconds=PREFLIGHT_SECONDS):
    """Refuse to record a capture that is already black before playback goes
    fullscreen (the page itself is on screen then, so black means the capture
    sees the wrong monitor or nothing). -> last brightness, or None if the
    backend cannot say."""
    luma = None
    for _ in range(int(seconds)):
        luma = program_luma(obs)
        if luma is None or luma >= BLACK_LUMA:
            return luma
        nap(1)
    rec = capture.label(CFG)
    where = ("check the capture source in OBS" if rec == "OBS"
             else "check the browser window is on screen")
    raise black_failure(f"{rec} output is black before recording (brightness {luma:.1f}); "
                        + where)


def start_playback(sess, player, rect):
    """Click the player centre until currentTime actually advances. An adapter
    may name a better spot (rect["click"] = [x, y], e.g. a recipe's play
    button); it is still a trusted CDP click, never a JS .click()."""
    wait_playable(player)
    x, y = rect.get("click") or (rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)
    for attempt in range(3):
        sess.click(x, y)
        time.sleep(4)
        st = state(player)
        if st.get("found") and st["t"] > 0.1 and not st["paused"]:
            return st
        if attempt == 1:
            sess.key(" ", code="Space", vk=32)
    raise ItemFailed("playback would not start")


def record_one(item, index, obs, speed, args):
    global LAST_DURATION
    print(f"\n=== [{index}] {item.day} {item.time} "
          f"[{item.kind}] {item.title[:70]}")

    if free_gb(outdir()) < MIN_FREE_GB:
        sys.exit(f"Only {free_gb(outdir()):.1f} GB free in {outdir()}; stopping.")

    sess = cdp.Session(ADAPTER.page_target(CFG, item))
    player = None
    pin = screen.Pin(sess)
    try:
        sess.bring_to_front()
        sess.navigate(item.url, settle=4)
        ADAPTER.check_page(sess, item)

        # Pages often build the player after their own API round-trips, and a
        # machine that just wrote a 9 GB file is slow. Wait for it rather than
        # guessing.
        rect = None
        for _ in range(PLAYER_WAIT_SECONDS):
            rect = ADAPTER.find_player(sess)
            if rect:
                break
            nap(1)
        if not rect:
            raise ItemFailed("no player after "
                             f"{PLAYER_WAIT_SECONDS}s: {diagnose(sess)}")
        print(f"    player: {rect['src'][:40]}")

        player = ADAPTER.attach_player(sess, rect)
        if not player:
            raise ItemFailed("cannot attach to the player")
        v = player.video

        # Make sure OBS is looking at this browser before anything is played.
        moved = capture.aim(obs, screen.window_bounds(sess))
        if moved:
            print(f"    {moved}")
        pin.__enter__()
        if pin.note:
            print(f"    {pin.note}")
        moved = capture.aim_window(obs, pin.hwnd)     # ffmpeg only: exact monitor
        if moved:
            print(f"    {moved}")
        preflight(obs)

        st = start_playback(sess, player, rect)
        duration = st["duration"]
        print(f"    playing: {duration/60:.1f} min, {st['w']}x{st['h']}")

        # The player resumes where a previous attempt stopped, which would
        # silently cut the opening minutes off the recording. Always rewind.
        if st["t"] > START_TOLERANCE:
            print(f"    resumed at {st['t']:.0f}s -- rewinding to the start")
            for _ in range(4):
                player.session.js(f"{v}.currentTime = 0")
                time.sleep(2)
                st = state(player)
                if st["t"] <= START_TOLERANCE:
                    break
            else:
                raise ItemFailed(f"could not rewind; player stuck at {st['t']:.0f}s")
            if st["paused"]:
                player.session.js(f"{v}.play()")
                time.sleep(2)
                st = state(player)
            print(f"    rewound to {st['t']:.0f}s")

        if not gesture(sess, ADAPTER.fullscreen_js(rect)):
            raise ItemFailed("fullscreen failed")
        time.sleep(4)
        # Some players pick their quality from the player size, so
        # the resolution that matters is the one after going fullscreen.
        fs = state(player)
        print(f"    recording at {fs['w']}x{fs['h']}")

        if speed != 1.0:
            player.session.js(f"{v}.playbackRate = {speed}")
            print(f"    playbackRate = {state(player)['rate']}")

        obs.start_record()
        t_started = time.time()
        # generous ceiling so a wedged player cannot record forever
        budget = duration / speed * 1.25 + 180
        last_t, last_move = st["t"], time.time()

        reattaches = stall_fixes = 0
        black = BlackWatch()
        while True:
            nap(POLL_SECONDS)
            try:
                s = state(player)
            except Exception as exc:
                # WinError 10053 and friends: the player's frame target went away.
                reattaches += 1
                if reattaches > MAX_REATTACH:
                    raise ItemFailed(f"lost the player {reattaches}x: {exc}")
                print(f"    lost player socket ({exc.__class__.__name__}); "
                      f"reattaching {reattaches}/{MAX_REATTACH}")
                if player.session is not sess:
                    try:
                        player.session.close()
                    except Exception:
                        pass
                fresh = ADAPTER.reattach_player(sess, player)
                if fresh:
                    player = fresh
                elif player.session is not sess:
                    raise ItemFailed("player target never came back")
                # else: the player lives in the page itself; one slow answer
                # from a busy page is not a lost player -- poll again.
                v = player.video
                continue
            if not s.get("found"):
                raise ItemFailed("player vanished mid-recording")

            if s["ended"] or s["t"] >= duration - 2:
                # A pre-roll ad in the same <video> ends too, and then the
                # real video starts with a longer duration. Look again.
                nap(3)
                try:
                    s2 = state(player)
                except Exception:
                    s2 = {}
                d2 = s2.get("duration") or 0
                another = (s2.get("found") and not s2.get("ended")
                           and duration + 5 < d2 < float("inf")
                           and s2.get("t", 0) < d2 - 2)
                if another:
                    print(f"    a {d2/60:.1f} min video followed a {duration:.0f}s one "
                          "(an ad?) -- recording on")
                    duration = d2
                    budget = (time.time() - t_started) + duration / speed * 1.25 + 180
                    last_t, last_move = s2.get("t", 0), time.time()
                    continue
                played = time.time() - t_started
                if duration != float("inf") and played < duration / speed * MIN_PLAYED_FRACTION:
                    raise ItemFailed(
                        f"the video ended after {played:.0f}s of recording but is "
                        f"{duration / speed:.0f}s long (it skipped ahead or was cut short)")
                print(f"    finished at {s['t']:.0f}/{duration:.0f}s")
                break

            if s["t"] > last_t + 0.5:
                last_t, last_move = s["t"], time.time()
                stall_fixes = 0
            elif time.time() - last_move > STALL_LIMIT:
                stall_fixes += 1
                if stall_fixes > MAX_STALL_FIXES:
                    raise ItemFailed(f"stalled at {s['t']:.0f}s, "
                                     f"{stall_fixes - 1} recovery attempts failed")
                print(f"    stalled at {s['t']:.0f}s -- nudging playback "
                      f"({stall_fixes}/{MAX_STALL_FIXES})")
                try:
                    player.session.js(f"(() => {{const v = {v};"
                                      " v.play(); return v.currentTime;})()")
                except Exception:
                    sess.click(sess.js("innerWidth") / 2, sess.js("innerHeight") / 2)
                last_move = time.time()      # give the nudge time to take effect

            if s["paused"]:
                # play() is precise; a centre click is a toggle and can pause a
                # video that resumed between the poll and the click.
                print("    paused -- resuming")
                try:
                    player.session.js(f"{v}.play()")
                except Exception:
                    sess.click(sess.js("innerWidth") / 2, sess.js("innerHeight") / 2)

            if time.time() - t_started > budget:
                raise ItemFailed("exceeded time budget; player likely wedged")

            mins = (time.time() - t_started) / 60
            print(f"    t={s['t']:.0f}/{duration:.0f}s  ({mins:.1f} min elapsed)",
                  flush=True)
            capture.check(obs)            # ffmpeg died mid-item -> retry
            luma = program_luma(obs)
            write_now(item, s, duration, luma, t_started)
            dark_for = black.update(luma, time.time())
            # The page was not black a moment ago (preflight), so black from the
            # first frame on means the player itself hides from capture.
            if black.since is not None and black.since <= t_started + POLL_SECONDS + 3 \
                    and dark_for >= BLOCKED_SECONDS:
                raise CaptureBlocked(
                    f"capture-blocked: the video is black to screen capture for "
                    f"{dark_for:.0f}s while it plays (protected player)")
            if dark_for > BLACK_ABORT_SECONDS:
                raise black_failure(f"capture has been black for over {BLACK_ABORT_SECONDS}s "
                                    f"({capture.label(CFG)} capturing the wrong/no monitor, "
                                    "or DRM)")

        path = obs.stop_record()
        LAST_DURATION = duration / speed
        time.sleep(2)
        return finalize(path, item, index)
    finally:
        try:
            if obs.record_status()["outputActive"]:
                # Never leave a recording running. A fragment of an item is
                # worth nothing and only risks passing for the real thing, so
                # bin it -- the item stays queued and gets recorded whole.
                partial = obs.stop_record()
                time.sleep(3)
                if partial and Path(partial).exists():
                    frag = Path(partial)
                    gb = frag.stat().st_size / 1024 ** 3
                    for _ in range(10):        # OBS may still be flushing
                        try:
                            frag.unlink()
                            print(f"    discarded {gb:.2f} GB partial recording")
                            break
                        except OSError:
                            time.sleep(1)
                    else:
                        print(f"    could not delete partial {frag.name}")
        except ObsError:
            pass
        try:
            # Leaving the tab fullscreen breaks the next item's page layout.
            gesture(sess, EXIT_FULLSCREEN_JS, timeout=10)
        except Exception:
            try:
                sess.key("Escape", code="Escape", vk=27)
            except Exception:
                pass
        pin.__exit__(None, None, None)
        if player and player.session is not sess:
            player.session.close()
        sess.close()


def to_mp4(src, dst=None):
    """Rewrap the recording as faststart MP4 -- no re-encode, so it costs
    seconds and loses nothing. MKV puts its seek index at the end of the file,
    which is what makes a 3 h recording stall on every seek in a browser player.

    An OBS that already records MP4 is left alone: remuxing a file onto its own
    name would have ffmpeg refuse and the cleanup delete the only copy. The
    remux goes to a ".part" file that is renamed into place only on success,
    so a stop or crash mid-remux leaves the source intact and no half-written
    file under the library name.
    """
    if src.suffix.lower() == ".mp4":
        return src
    dst = dst or src.with_suffix(".mp4")
    if dst.exists():
        print(f"    {dst.name} already exists; keeping {src.suffix}")
        return src
    part = dst.with_name(dst.stem + ".part.mp4")
    cmd = [CFG.get("ffmpeg", "ffmpeg"), "-hide_banner", "-loglevel", "error",
           "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart",
           str(part)]
    try:
        ok = subprocess.run(cmd, timeout=1800).returncode == 0
        if ok and part.exists() and part.stat().st_size > 0:
            os.replace(part, dst)
            src.unlink()
            return dst
        print(f"    remux failed; keeping {src.suffix}")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"    remux failed ({exc}); keeping {src.suffix}")
    finally:
        part.unlink(missing_ok=True)
    return src


def move_file(src, dst, tries=10):
    """Rename, or copy-then-delete when OBS's folder is on another drive than
    the library (a rename cannot cross drives: WinError 17 / EXDEV). Retries
    while OBS may still be flushing the muxer. -> True when dst holds the file."""
    for _ in range(tries):
        try:
            src.rename(dst)
            return True
        except OSError as exc:
            if getattr(exc, "winerror", None) == 17 or exc.errno == 18:   # EXDEV
                try:
                    shutil.move(str(src), str(dst))
                    return True
                except OSError:
                    pass
            time.sleep(1)
    return False


def finalize(path, item, index):
    src = Path(path)
    base = library_path(item, index)
    base.parent.mkdir(parents=True, exist_ok=True)
    # A retry re-records an existing name. Check every extension the file can
    # end up with: a free "x.mkv" next to an earlier "x.mp4" would be remuxed
    # onto it.
    stem, n = str(base), 2
    while any(Path(stem + ext).exists() for ext in {src.suffix, ".mkv", ".mp4"}):
        stem = f"{base} ({n})"
        n += 1
    dst = Path(stem + src.suffix)
    if not move_file(src, dst):
        print(f"    could not move {src.name}; left in place")
        dst = src
    dst = to_mp4(dst)
    if organize.write_nfo(CFG):
        # Without it a media server falls back to the filename, truncated.
        dst.with_suffix(".nfo").write_text(
            organize.episode_nfo(item, index, organize.clean_title(item.title, CFG), CFG),
            encoding="utf-8")
    size = dst.stat().st_size / 1024 ** 3
    print(f"    wrote {dst.name}  ({size:.2f} GB)")
    return str(dst), size


def blocked_alert(alerts, prev, entry, title, reason):
    """Send "blocked" for this item unless its previous progress entry says it
    was already announced; mark the new entry either way. -> True if sent."""
    entry["blocked_notified"] = True
    if isinstance(prev, dict) and prev.get("blocked_notified"):
        print("    (already reported as blocked; no new notification)")
        return False
    alerts.send("blocked", title=title, reason=reason)
    return True


def run_end(alerts, refresher, ran, repeat_blocked=0):
    """Last library refresh + the "finished" message, then a bounded wait for
    both. A run that did nothing (a scheduled re-scan with nothing new) sends
    nothing -- and blocked items already announced on an earlier run are not
    news (repeat_blocked of ran["blocked"]). Never raises: it runs in main()'s
    finally."""
    try:
        refresher.finish()
        if (ran["recorded"] or ran["failed"] or ran["blocked"] > repeat_blocked
                or ran["stopped"] or ran["note"]):
            alerts.send("finished", **ran)
        alerts.flush()
        refresher.flush()
    except Exception as exc:
        print(f"    notifications: {exc.__class__.__name__}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all",
                    help="record only items of this kind (adapter-defined, e.g. lecture)")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--include-locked", action="store_true")
    ap.add_argument("--include-future", action="store_true",
                    help="try items that have not aired yet (usually unplayable)")
    ap.add_argument("--grace-hours", type=float, default=2.0,
                    help="wait this long after an item airs before recording it")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--id", action="append", default=[],
                    help="record only this item (repeatable); the UI's per-item Record button")
    args = ap.parse_args(argv)

    jobs.graceful_signals()
    jobs.clear_stale_flags(Path.cwd(), "record")
    _setup()
    labels = ADAPTER.labels
    queue = Path(CFG["queue_file"])
    if not queue.exists():
        sys.exit(f"No {queue} -- run: python build_queue.py")
    items = [ADAPTER.item(r) for r in json.loads(queue.read_text(encoding="utf-8"))]
    progress = load_progress()

    cutoff = datetime.now() - timedelta(hours=args.grace_hours)
    todo, future, locked = [], 0, []
    for pos, it in enumerate(items, 1):
        if args.only != "all" and it.kind != args.only:
            continue
        if it.locked and not args.include_locked:
            # Gated (e.g. behind an exam). Re-run build_queue.py once it
            # unlocks, otherwise this stays skipped on the stale flag forever.
            locked.append(it)
            continue
        if progress.get(it.id, {}).get("status") in ("done", "skipped"):
            continue
        when = it.aired_at
        if when and when > cutoff and not args.include_future:
            future += 1          # not published yet -- leave it for a later run
            continue
        todo.append((pos, it))
    if args.id:
        wanted = {str(i) for i in args.id}
        todo = [(pos, it) for pos, it in todo if str(it.id) in wanted]
    if args.limit:
        todo = todo[:args.limit]

    done_already = sum(1 for v in progress.values() if v["status"] == "done")
    user_skipped = sum(1 for v in progress.values() if v["status"] == "skipped")
    est_h = len(todo) * CFG["avg_item_minutes"] / 60 / args.speed
    print(f"{len(todo)} to record | {done_already} done | "
          f"{user_skipped} skipped by you | "
          f"{future} not aired yet | {len(locked)} {labels['locked']} | "
          f"~{est_h:.0f} h at {args.speed}x (rough) | "
          f"{free_gb(outdir()):.0f} GB free in {outdir()}")
    if locked:
        print(f"  {labels['locked']} ({labels['locked_hint']}):")
        for it in locked:
            print(f"     {it.day} [{it.kind}] {it.title[:60]}")
    if args.dry_run:
        for pos, it in todo:
            print(f"  {pos:3d}. {it.day} {it.time} "
                  f"[{it.kind}] {it.title[:65]}")
        return

    outdir().mkdir(parents=True, exist_ok=True)
    if capture.backend_name(CFG) == "ffmpeg":
        try:
            obs = capture.FfmpegCapture(CFG, outdir(), state_dir=jobs.STATE_DIR)
        except ObsError as exc:
            sys.exit(f"ffmpeg capture is not usable: {exc}")
        print(f"recording with {obs.describe()}")
        print(f"recording quality: {record_quality.describe(CFG)}")
    else:
        obs = wait_ready(connect_obs())
        apply_output_settings(obs)
        report_quality()

    global RUN_PLACE
    consecutive = 0
    alerts, refresher = notify.Notifier(CFG), notify.LibraryRefresher(CFG)
    ran = {"recorded": 0, "failed": 0, "blocked": 0, "stopped": False, "note": ""}
    repeat_blocked = 0
    try:
        for place, (index, it) in enumerate(todo, 1):
            RUN_PLACE = (place, len(todo))
            nap(0)                # a stop-now that arrived during the last finalize
            if stop_after_current():
                print("stop requested -- finishing here, before the next item")
                break
            # A stalled item is usually a network blip, and the same blip makes
            # the next few pages render no player at all. Retrying with a pause
            # turns a 10-item cascade of false failures into a short delay.
            prev = progress.get(it.id)
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    path, size = record_one(it, index, obs, args.speed, args)
                    progress[it.id] = {
                        "status": "done", "file": path, "gb": round(size, 2),
                        "title": it.title}
                    firstrun.mark(Path.cwd(), "first_recording")   # local only, never fails
                    consecutive = 0
                    ran["recorded"] += 1
                    alerts.send("recorded", title=it.title, duration=LAST_DURATION,
                                file=path, gb=round(size, 2))
                    refresher.item_filed()
                    break
                except (ItemFailed, cdp.CdpError, Exception) as exc:
                    label = ("skipped" if isinstance(exc, ItemFailed)
                             else exc.__class__.__name__)
                    print(f"    !! {label}: {exc}")
                    if isinstance(exc, CaptureBlocked):
                        # Same page, same player, same result: do not retry,
                        # and do not count it towards the failure cooldown.
                        print("    this player hides its video from screen capture; "
                              "playcap cannot record it (not retried)")
                        try:
                            if obs.record_status()["outputActive"]:
                                obs.stop_record()
                        except Exception:
                            pass
                        progress[it.id] = {"status": "failed", "title": it.title,
                                           "error": f"CaptureBlocked: {exc}"[:300]}
                        ran["blocked"] += 1
                        if not blocked_alert(alerts, prev, progress[it.id], it.title, str(exc)):
                            repeat_blocked += 1
                        break
                    if isinstance(exc, cdp.CdpError):
                        ensure_chrome()
                    try:
                        if obs.record_status()["outputActive"]:
                            obs.stop_record()
                    except Exception:
                        # ffmpeg has no server to reconnect to: the next
                        # start() launches a fresh process.
                        if not isinstance(obs, capture.Capture):
                            try:
                                obs = connect_obs()
                                apply_output_settings(obs)
                            except ObsError as re_exc:
                                sys.exit(f"OBS is gone and will not restart: {re_exc}")
                    if attempt < MAX_ATTEMPTS:
                        print(f"    retrying in {RETRY_WAIT_SECONDS}s "
                              f"(attempt {attempt + 1}/{MAX_ATTEMPTS})")
                        nap(RETRY_WAIT_SECONDS)
                        continue
                    progress[it.id] = {
                        "status": "failed", "title": it.title,
                        "error": f"{exc.__class__.__name__}: {exc}"[:300]}
                    consecutive += 1
                    if getattr(exc, "black", False):
                        ran["blocked"] += 1
                        if not blocked_alert(alerts, prev, progress[it.id], it.title, str(exc)):
                            repeat_blocked += 1
                    else:
                        ran["failed"] += 1
                        alerts.send("failed", title=it.title,
                                    reason=f"{exc.__class__.__name__}: {exc}")
            save_progress(progress)

            if consecutive >= COOLDOWN_AFTER:
                # something broader is wrong (network, site). Waiting beats
                # marching through the queue turning every item into a failure.
                print(f"{consecutive} failures in a row -- pausing "
                      f"{COOLDOWN_SECONDS // 60} min before continuing")
                if place < len(todo):        # nothing left: no point waiting
                    nap(COOLDOWN_SECONDS, after_current=True)
                consecutive = 0
    except SystemExit as exc:
        ran["note"] = str(exc.code) if exc.code not in (None, 0) else ""
        raise
    except KeyboardInterrupt:
        ran["stopped"] = True
        print("\ninterrupted -- stopping the recording cleanly")
        try:
            if obs.record_status()["outputActive"]:
                print("stopped:", obs.stop_record())
        except ObsError:
            pass
        save_progress(progress)
    finally:
        obs.close()
        clear_now()
        jobs.clear_flags(Path.cwd(), "record")
        run_end(alerts, refresher, ran, repeat_blocked)

    done = sum(1 for v in progress.values() if v["status"] == "done")
    failed = [v for v in progress.values() if v["status"] == "failed"]
    print(f"\n{done} recorded, {len(failed)} failed.")
    for f in failed:
        print(f"   FAILED {f['title'][:60]}: {f['error'][:60]}")


if __name__ == "__main__":
    main()
