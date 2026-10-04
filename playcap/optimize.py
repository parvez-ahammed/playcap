#!/usr/bin/env python3
"""Re-encode the recordings down to a sane bitrate, in place.

OBS writes everything at a constant bitrate (8.16 Mbps in the original
setup), which is ~4x more than a camera-on-a-whiteboard / camera-on-a-page
feed needs. CRF 24 reproduces the same
picture at ~2 Mbps and, with a 2 s keyframe interval + faststart, also fixes
the seeking stalls in Emby's browser player.

Layout after a run:

    <output_dir>/...            optimized .mp4 + .nfo   <- what Emby scans
    <output_dir>_originals/...  the untouched .mkv      <- outside Emby's root

The original is only moved after the new file has been verified to run the
full length, so a crashed encode can never lose the source. Keep that
ordering.

A matching duration does not prove the encode is good: verify() compares
container length and nothing else, and one episode passed it while carrying a
malformed AAC frame two hours in. Archiving on a duration match is fine
(the source is still there); before *deleting* an archived source, decode the
whole replacement (ffmpeg -v error -i new.mp4 -f null -) and require zero
decoder output. This script never deletes a source.

Since the recorder started remuxing to faststart .mp4, new recordings land
in the library already named like the finished episode. Those are sources
too: any .mp4 that optimize.json does not list as a finished output. For
them source and destination are the same path, so the encode goes to a
sidecar file, is verified against the source, the source is archived, and
only then does the sidecar take the episode's name. Files touched in the
last FRESH_MINUTES are skipped -- that is a remux still being written.

Audio that is already AAC is stream-copied, not re-encoded: the old
`-c:a aac -b:a 96k -ac 1` downmix emitted a malformed frame on one episode,
twice, deterministically, at the same timestamp, from a source that decoded
clean both times. Copying costs ~3% size and is bit-exact.

Binaries come from config: "encode_ffmpeg" (falls back to "ffmpeg") and
"ffprobe".

    python optimize.py                 # encode everything not done yet
    python optimize.py --only S01E09   # one episode (substring match)
    python optimize.py --crf 23        # higher quality, bigger
    python optimize.py --verify        # re-check finished work, encode nothing
    python optimize.py --keep          # encode but leave the .mkv in place
                                       # (.mp4 recordings are always archived:
                                       #  the new file takes their name)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playcap import config

# Filled in by _setup() from config.json, so importing needs no config.
CFG = {}
FFMPEG = FFPROBE = LIB = ARCHIVE = STATE = None

DURATION_TOLERANCE = 2.0   # seconds; a good re-encode matches the source
# Emby scans the library live, so the half-written file must not look like a
# video to it. Anything but a known media extension will do.
PART_SUFFIX = ".optpart"
FRESH_MINUTES = 15         # younger .mp4 may still be mid-remux by the recorder


def _setup():
    global CFG, FFMPEG, FFPROBE, LIB, ARCHIVE, STATE
    if LIB is None:
        CFG, _ = config.load()
        FFMPEG = CFG.get("encode_ffmpeg") or CFG["ffmpeg"]
        FFPROBE = CFG["ffprobe"]
        LIB = Path(CFG["output_dir"])
        ARCHIVE = LIB.parent / (LIB.name + "_originals")
        STATE = Path(CFG["optimize_state"])


def probe(path, entries, stream=None):
    sel = ["-select_streams", stream] if stream else []
    out = subprocess.run(
        [FFPROBE, "-v", "error", *sel, "-show_entries", entries,
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return out.stdout.strip()


def duration(path):
    try:
        return float(probe(path, "format=duration"))
    except ValueError:
        return 0.0


def finished(state):
    """Library files optimize.py itself produced."""
    return {Path(v["file"]) for v in state.values()
            if v.get("status") == "done" and v.get("file")}


def sources(state):
    """Recordings still in the library, plus any already archived. A library
    .mp4 counts unless it is one of our own outputs or is still being
    written."""
    _setup()
    done = finished(state)
    fresh = time.time() - FRESH_MINUTES * 60
    live = [p for p in LIB.rglob("*.mkv") if "_partial" not in p.parts]
    live += [p for p in LIB.rglob("*.mp4") if "_partial" not in p.parts
             and p not in done and p.stat().st_mtime < fresh]
    archived = ([p for ext in ("*.mkv", "*.mp4") for p in ARCHIVE.rglob(ext)]
                if ARCHIVE.exists() else [])
    return sorted(live + archived, key=lambda p: p.name)


def in_library(src):
    """Where this episode's .mp4 belongs, whether src is live or archived."""
    root = ARCHIVE if ARCHIVE in src.parents else LIB
    return LIB / src.relative_to(root).with_suffix(".mp4")


def in_archive(src):
    return ARCHIVE / src.relative_to(LIB)


def verify(src, dst):
    """Done only if the new file exists and runs the full length."""
    if not dst.exists() or dst.stat().st_size == 0:
        return False, "missing"
    src_d, dst_d = duration(src), duration(dst)
    if src_d == 0:
        return False, "source unreadable"
    if abs(src_d - dst_d) > DURATION_TOLERANCE:
        return False, f"runs {dst_d:.0f}s, source is {src_d:.0f}s"
    return True, f"{dst_d / 60:.0f} min"


def audio_args(src):
    if probe(src, "stream=codec_name", "a:0") == "aac":
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]


def encode(src, dst, crf, preset):
    """Encode to the sidecar and return (seconds, sidecar). The caller moves
    it into place once it has been verified."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(PART_SUFFIX)
    # Leave two cores for whoever is using the machine, and run below normal
    # priority: a 9 h batch should never be the reason the desktop stutters.
    threads = max(1, (os.cpu_count() or 4) - 2)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-stats", "-y",
           "-threads", str(threads),
           "-i", str(src),
           "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
           "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
           "-pix_fmt", "yuv420p",
           *audio_args(src),
           "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    started = time.time()
    idle = getattr(subprocess, "IDLE_PRIORITY_CLASS", 0)
    if subprocess.run(cmd, creationflags=idle).returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg failed")
    return time.time() - started, tmp


def archive(src):
    """Move the original out of the library. The .nfo stays behind: its
    basename already matches the new .mp4, which is what Emby reads."""
    dest = in_archive(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


def gb(path):
    return path.stat().st_size / 1024 ** 3


def main(argv=None):
    _setup()
    ap = argparse.ArgumentParser()
    ap.add_argument("--crf", type=int, default=24)
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--only", help="substring of the filename to encode")
    ap.add_argument("--verify", action="store_true",
                    help="check finished work, encode nothing")
    ap.add_argument("--keep", action="store_true",
                    help="do not move originals to the archive")
    args = ap.parse_args(argv)

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    todo = sources(state)
    if args.only:
        todo = [p for p in todo if args.only.lower() in p.name.lower()]
    if not todo:
        sys.exit("nothing matched")

    before_total = after_total = 0.0
    for i, src in enumerate(todo, 1):
        dst = in_library(src)
        print(f"[{i}/{len(todo)}] {src.stem}")
        source_gb = gb(src)

        in_place = src == dst
        ok, why = (False, "raw recording") if in_place else verify(src, dst)
        if not ok and args.verify:
            print(f"    not optimized: {why}")
            continue
        if not ok:
            print(f"    encoding at crf {args.crf} ...")
            try:
                took, tmp = encode(src, dst, args.crf, args.preset)
            except RuntimeError as exc:
                print(f"    !! {exc}")
                state[src.name] = {"status": "failed", "error": str(exc)}
                STATE.write_text(json.dumps(state, indent=1))
                continue
            ok, why = verify(src, tmp)
            print(f"    encoded in {took / 60:.1f} min")
            if not ok:
                tmp.unlink(missing_ok=True)
                print(f"    !! not usable, original untouched: {why}")
                state[src.name] = {"status": "failed", "error": why}
                STATE.write_text(json.dumps(state, indent=1))
                continue
            if in_place:
                # Same name: the source must leave before the encode moves in.
                archived = archive(src)
                src = archived
            tmp.replace(dst)

        new_gb = gb(dst)
        before_total += source_gb
        after_total += new_gb
        print(f"    {source_gb:.2f} GB -> {new_gb:.2f} GB "
              f"({source_gb / new_gb:.1f}x smaller, {why})")

        state[src.name] = {"status": "done", "file": str(dst),
                           "gb": round(new_gb, 2), "crf": args.crf}

        if ARCHIVE in src.parents and in_place:
            state[src.name]["original"] = str(src)
            print("    original archived")
        elif not args.keep and ARCHIVE not in src.parents:
            state[src.name]["original"] = str(archive(src))
            print("    original archived")
        STATE.write_text(json.dumps(state, indent=1))

    if before_total:
        print(f"\n{before_total:.1f} GB -> {after_total:.1f} GB "
              f"({before_total - after_total:.1f} GB reclaimed)")
        print(f"originals: {ARCHIVE}")


if __name__ == "__main__":
    main()
