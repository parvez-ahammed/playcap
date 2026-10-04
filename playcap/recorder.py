"""Record every queued item, one at a time, and survive being interrupted.

Flow per item: navigate -> click the player (a CDP click is a trusted gesture,
which is what starts DRM-protected and gesture-gated playback; a synthetic JS
.click() does not) -> fullscreen the player element so the video renders at
its native resolution instead of the page's player box -> OBS records the
screen -> poll the <video> until it ends -> stop, rename, remux to mp4.

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

Black frames: some DRM renders black to screen capture while looking fine on
screen. The recorder cannot see that from the DOM, so it does not try; rehearse
with playcap.tools.smoke_test (which checks a captured frame) before a batch.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from playcap import cdp, config, organize
from playcap.adapters.base import VIDEO_STATE_JS, ItemFailed  # noqa: F401
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
PLAYER_WAIT_SECONDS = 45   # how long to wait for the player to appear

EXIT_FULLSCREEN_JS = ("(async()=>{if(document.fullscreenElement)"
                      " await document.exitFullscreen(); return true;})()")

CFG = {}
ADAPTER = None


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
    """Recording bitrate + GOP. A 2 s keyframe interval keeps seeking cheap;
    OBS defaults to 250 frames, which makes every seek re-buffer ~8 s."""
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
    return json.loads(p.read_text()) if p.exists() else {}


def save_progress(p):
    Path(CFG["progress_file"]).write_text(json.dumps(p, indent=1))


def safe_name(item, index):
    """Jellyfin episode name. The queue position is the episode number,
    so a re-record keeps the number the item already had."""
    return organize.episode_stem(CFG, index, organize.clean_title(item.title, CFG))


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
            time.sleep(3)


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
        time.sleep(3)
        try:
            cdp.targets()
            print("    debug Chrome is back")
            return True
        except Exception:
            continue
    print("    debug Chrome did not come back")
    return False


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
    exe = Path(CFG["obs_exe"])
    print(f"    OBS is not answering -- relaunching {exe.name}")
    # After a crash or a hard reboot OBS opens a "Crash Detected" modal and
    # waits for a click, so the websocket server never starts and an
    # unattended run stalls forever. --disable-shutdown-check skips the
    # prompt; --multi skips the "already running" one.
    subprocess.Popen([str(exe), "--disable-shutdown-check", "--multi"],
                     cwd=str(exe.parent),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    for _ in range(20):
        time.sleep(3)
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


def start_playback(sess, player, rect):
    """Click the player centre until currentTime actually advances."""
    for attempt in range(3):
        sess.click(rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)
        time.sleep(4)
        st = state(player)
        if st.get("found") and st["t"] > 0.1 and not st["paused"]:
            return st
        if attempt == 1:
            sess.key(" ", code="Space", vk=32)
    raise ItemFailed("playback would not start")


def record_one(item, index, obs, speed, args):
    print(f"\n=== [{index}] {item.day} {item.time} "
          f"[{item.kind}] {item.title[:70]}")

    if free_gb(outdir()) < MIN_FREE_GB:
        sys.exit(f"Only {free_gb(outdir()):.1f} GB free in {outdir()}; stopping.")

    sess = cdp.Session(ADAPTER.page_target(CFG, item))
    player = None
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
            time.sleep(1)
        if not rect:
            raise ItemFailed("no player after "
                             f"{PLAYER_WAIT_SECONDS}s: {diagnose(sess)}")
        print(f"    player: {rect['src'][:40]}")

        player = ADAPTER.attach_player(sess, rect)
        if not player:
            raise ItemFailed("cannot attach to the player")
        v = player.video

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
        # Some players (YouTube) pick their quality from the player size, so
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
        while True:
            time.sleep(POLL_SECONDS)
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
                player = ADAPTER.reattach_player(sess, player)
                if not player:
                    raise ItemFailed("player target never came back")
                v = player.video
                continue
            if not s.get("found"):
                raise ItemFailed("player vanished mid-recording")

            if s["ended"] or s["t"] >= duration - 2:
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

        path = obs.stop_record()
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
        if player and player.session is not sess:
            player.session.close()
        sess.close()


def to_mp4(src):
    """Rewrap the recording as faststart MP4 -- no re-encode, so it costs
    seconds and loses nothing. MKV puts its seek index at the end of the file,
    which is what makes a 3 h recording stall on every seek in a browser player.
    """
    dst = src.with_suffix(".mp4")
    cmd = [CFG.get("ffmpeg", "ffmpeg"), "-hide_banner", "-loglevel", "error",
           "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart",
           str(dst)]
    try:
        if subprocess.run(cmd, timeout=1800).returncode == 0 and dst.exists():
            src.unlink()
            return dst
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"    remux failed ({exc}); keeping {src.suffix}")
    dst.unlink(missing_ok=True)
    return src


def finalize(path, item, index):
    src = Path(path)
    sdir = organize.show_dir(CFG)
    sdir.mkdir(parents=True, exist_ok=True)
    dst = sdir / (safe_name(item, index) + src.suffix)
    n = 2
    while dst.exists():                     # a retry re-recorded an existing name
        dst = sdir / f"{safe_name(item, index)} ({n}){src.suffix}"
        n += 1
    for _ in range(10):                     # OBS may still be flushing the muxer
        try:
            src.rename(dst)
            break
        except OSError:
            time.sleep(1)
    else:
        print(f"    could not rename {src.name}; left in place")
        dst = src
    dst = to_mp4(dst)
    # Without this Jellyfin falls back to the filename and shows it truncated.
    dst.with_suffix(".nfo").write_text(
        organize.episode_nfo(item, index, organize.clean_title(item.title, CFG), CFG),
        encoding="utf-8")
    size = dst.stat().st_size / 1024 ** 3
    print(f"    wrote {dst.name}  ({size:.2f} GB)")
    return str(dst), size


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
    args = ap.parse_args(argv)

    _setup()
    labels = ADAPTER.labels
    queue = Path(CFG["queue_file"])
    if not queue.exists():
        sys.exit(f"No {queue} -- run: python build_queue.py")
    items = [ADAPTER.item(r) for r in json.loads(queue.read_text())]
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
    obs = wait_ready(connect_obs())
    apply_output_settings(obs)

    consecutive = 0
    try:
        for index, it in todo:
            # A stalled item is usually a network blip, and the same blip makes
            # the next few pages render no player at all. Retrying with a pause
            # turns a 10-item cascade of false failures into a short delay.
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    path, size = record_one(it, index, obs, args.speed, args)
                    progress[it.id] = {
                        "status": "done", "file": path, "gb": round(size, 2),
                        "title": it.title}
                    consecutive = 0
                    break
                except (ItemFailed, cdp.CdpError, Exception) as exc:
                    label = ("skipped" if isinstance(exc, ItemFailed)
                             else exc.__class__.__name__)
                    print(f"    !! {label}: {exc}")
                    if isinstance(exc, cdp.CdpError):
                        ensure_chrome()
                    try:
                        if obs.record_status()["outputActive"]:
                            obs.stop_record()
                    except Exception:
                        try:
                            obs = connect_obs()
                            apply_output_settings(obs)
                        except ObsError as re_exc:
                            sys.exit(f"OBS is gone and will not restart: {re_exc}")
                    if attempt < MAX_ATTEMPTS:
                        print(f"    retrying in {RETRY_WAIT_SECONDS}s "
                              f"(attempt {attempt + 1}/{MAX_ATTEMPTS})")
                        time.sleep(RETRY_WAIT_SECONDS)
                        continue
                    progress[it.id] = {
                        "status": "failed", "title": it.title,
                        "error": f"{exc.__class__.__name__}: {exc}"[:300]}
                    consecutive += 1
            save_progress(progress)

            if consecutive >= COOLDOWN_AFTER:
                # something broader is wrong (network, site). Waiting beats
                # marching through the queue turning every item into a failure.
                print(f"{consecutive} failures in a row -- pausing "
                      f"{COOLDOWN_SECONDS // 60} min before continuing")
                time.sleep(COOLDOWN_SECONDS)
                consecutive = 0
    except KeyboardInterrupt:
        print("\ninterrupted -- stopping the recording cleanly")
        try:
            if obs.record_status()["outputActive"]:
                print("stopped:", obs.stop_record())
        except ObsError:
            pass
        save_progress(progress)
    finally:
        obs.close()

    done = sum(1 for v in progress.values() if v["status"] == "done")
    failed = [v for v in progress.values() if v["status"] == "failed"]
    print(f"\n{done} recorded, {len(failed)} failed.")
    for f in failed:
        print(f"   FAILED {f['title'][:60]}: {f['error'][:60]}")


if __name__ == "__main__":
    main()
