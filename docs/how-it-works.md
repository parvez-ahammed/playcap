# How it works

The pipeline, the design choices behind it, and what to do when a recording
comes out black. [Back to the README](../README.md)

## Features in detail

- **Stops when playback ends.** It polls the page's `<video>` (`currentTime`,
  `ended`) rather than guessing a duration.
- **Recovers from stalls.** A frozen player gets nudged, a replaced player
  frame gets re-attached, a failed item is retried after a pause, and a run of
  failures triggers a cooldown instead of failing the whole queue.
- **Resumable.** Progress is saved after every item. A crash or Ctrl+C costs
  at most the item in flight, and a rerun skips finished work. Partial
  recordings are discarded, never mistaken for the real thing.
- **Recorded at the size you choose.** OBS records in constant-quality mode
  (x264 CRF) by default, so a still slide costs almost nothing and the file is
  final. Pick Small, Balanced, High or a fixed bitrate in the UI; playcap
  writes it into OBS's profile while OBS is closed.
- **Optional verified re-compress.** For fixed-bitrate recordings, `optimize`
  re-encodes to CRF 24 and only archives the original once the new file runs
  the full length. Files already recorded at that quality are skipped. AAC
  audio is copied, not re-encoded.
- **Your naming, your layout.** Default: `My Recordings/03 - Title.mp4`.
  Media-server layout: `Show/Season 01/S01E03 - Title.mp4` plus `.nfo` files
  with the untruncated title and date. Or a template of your own, such as
  `{show}/{date} - {title} ({n})`. Numbers follow the queue, so a re-recorded
  item keeps its name.
- **Simple local UI.** Runs on `127.0.0.1`, protected against cross-site
  requests. Jobs keep running if you close the UI; reopening it picks them up
  again.
- **Hands-off overnight runs.** Optional notifications (ntfy, Discord, any
  JSON webhook, Windows desktop toast) when an item is recorded, fails, is
  capture-blocked or black, and when a run ends. Optional Jellyfin/Emby and
  Plex library refresh after each filed recording. Optional schedule that
  refreshes the queue and records whatever is new. All of it lives under
  *Notifications & automation* in the UI.

## Notifications, media-server refresh and the schedule

Every setting below lives in `config.json` (the UI writes it for you). Leave a
channel blank and it is off.

| Key | What it does |
| --- | --- |
| `notify_events` | Which events to send: any of `recorded`, `failed`, `blocked`, `finished` (default: all) |
| `notify_ntfy_server`, `notify_ntfy_topic`, `notify_ntfy_token` | ntfy push (default server `https://ntfy.sh`). The topic works like a password: anyone who knows it can read your messages |
| `notify_discord_webhook` | A Discord channel webhook URL |
| `notify_webhook_url` | Any URL; gets a JSON POST `{event, title, message, data, time}` |
| `notify_desktop` | `true` for a Windows toast (uses Windows PowerShell, nothing to install) |
| `jellyfin_url`, `jellyfin_api_key` | Jellyfin or Emby: `POST /Library/Refresh` after a recording is filed |
| `plex_url`, `plex_token`, `plex_section_id` | Plex: `POST /library/sections/{id}/refresh` (blank id = all libraries) |
| `library_refresh_minutes` | At most one library refresh per this many minutes (default 10), plus one at the end of the run |
| `schedule_mode` | `off`, `daily` or `every` |
| `schedule_time` | `HH:MM`. Daily: the time. Every N hours: the time the N-hour steps are counted from |
| `schedule_every_hours` | 1-23 (for `every`) |

How it behaves:

- A notifier or media server that is down never stops a recording. Each send
  runs in the background with a short timeout. Its first error is logged once,
  with webhook URLs and tokens blanked out. Secrets stay in `config.json`: they
  are never written to the logs or sent back to the UI page.
- A scheduled run opens the playcap browser if needed, refreshes the queue,
  then records. The recorder already skips finished items and waits for
  unaired ones, so only new items get recorded. Failed items are retried.
  A scheduled run is skipped, not queued, when a recording or re-compress is
  already running.
- On Windows, *Run on schedule* in the UI (or `python -m playcap schedule
  --register`) adds a Task Scheduler task for the current user. It needs no
  admin rights, survives reboots, and runs only while you are logged on,
  because OBS needs your desktop. Its output goes to `schedule.log`. Task
  Scheduler ends a task after 72 hours by default. If a long backlog hits
  that limit, the next run picks up where it stopped. On other systems, keep
  `python -m playcap schedule` running, or call `--once` from cron.

## Architecture

```
config.json ──> playcap.config ──> adapter (playcap.adapters.* or your module)
                                       │
build_queue ── adapter.build_queue ──> queue.json
                                       │
recorder ── per item: page_target -> navigate -> check_page -> find_player
            -> CDP trusted click (start playback) -> rewind to 0 if resumed
            -> fullscreen player -> start capture (OBS StartRecord, or ffmpeg)
            -> poll <video> every 10 s
               (stall nudge, re-attach, pause resume, time budget)
            -> stop capture -> file by the naming layout -> remux to faststart mp4
            -> .nfo (media-server layout, or when switched on)
            -> progress.json (after every item)
                                       │
optimize ── CRF re-encode to a sidecar -> verify duration -> archive original
            -> sidecar takes the episode name -> optimize.json
            -> notify (recorded / failed / blocked) + debounced media-server refresh
            -> at the end of the run: "finished" + last refresh
organize / status / control ── filing, dashboard, local control panel
schedule ── at each scheduled time: browser if closed -> build_queue -> recorder
            (skipped while a recording runs; Windows Task Scheduler keeps it across reboots)
```

## Design notes

Worth knowing before you change anything:

- CDP input events count as trusted user gestures. Many players only start
  on a real gesture, and a synthetic JavaScript `.click()` does not count.
- Fullscreen the player before recording. Otherwise you capture the page's
  player box instead of the video at its native resolution.
- Some protected video renders black to screen capture while looking fine on
  screen. playcap does not get around that. The smoke test detects it, so you
  find out before a batch.
- A matching duration does not prove an encode is good. Before deleting an
  archived original, decode the whole replacement
  (`ffmpeg -v error -i new.mp4 -f null -`) and require zero output.
- Chrome 136+ refuses `--remote-debugging-port` on the default profile, which
  is why playcap uses a dedicated one.

## Capture backends: OBS or ffmpeg

`"capture_backend"` in config.json (or *Screen recorder* in the UI's Tools
step) picks the program that records the screen. Everything else (the CDP
click, fullscreen before recording, polling the `<video>`, the black checks,
filing and remuxing) is the same for both.

| | OBS (`"obs"`, default) | ffmpeg (`"ffmpeg"`) |
|---|---|---|
| Installs | Chrome + OBS + ffmpeg | Chrome + ffmpeg |
| Screen capture | OBS monitor capture, aimed at the browser's monitor | `ddagrab` (Desktop Duplication, FFmpeg 6.0+) on the browser's monitor; `gdigrab` when the build has no ddagrab or the monitor is on a second GPU |
| System audio | Built in (desktop audio) | Only from a DirectShow loopback device: *Stereo Mix*, *virtual-audio-capturer* (screen-capture-recorder) or a virtual audio cable. None found: picture only, and the log says so |
| Encoder | x264 via OBS's profile | NVENC / Quick Sync / AMF when one actually works (tested with a 5-frame encode), else libx264 |
| Maturity | Years of use behind it | New. Windows only so far |

Why audio needs a device with ffmpeg: mainline FFmpeg has no WASAPI loopback
input. [Trac #9408](https://trac.ffmpeg.org/ticket/9408) is still open, and
the Changelog through 8.1 has no WASAPI entry. DirectShow is the input every
Windows build has, and it can only record a device that carries what the
speakers play. Many sound drivers ship *Stereo Mix* disabled
(Sound control panel > Recording > show disabled devices > Enable).
`"capture_audio"`: `"auto"` (default, first loopback-looking device),
`"none"`, or an exact device name.

Other ffmpeg settings: `"capture_grabber"` (`auto`/`ddagrab`/`gdigrab`),
`"capture_encoder"` (`auto`/`libx264`/`h264_nvenc`/`h264_qsv`/`h264_amf`),
`"capture_fps"` (10-60, default 30). Quality (CRF or bitrate, keyframe
interval) comes from the same *Recording quality* setting OBS uses.

How the ffmpeg backend behaves:

- It records to MKV. If ffmpeg is killed, an MKV still plays up to the last
  cluster; an MP4 without its index would not. The finished file is remuxed to
  faststart MP4 like an OBS recording.
- Stopping sends `q` to ffmpeg so it writes a complete file. It runs in its own
  process group, so stopping the recorder does not cut it off mid-write, and in
  a kill-on-close job so it cannot outlive a crashed recorder.
- If ffmpeg dies mid-item, the item fails and is retried; the partial file is
  discarded, as with OBS.
- **The DRM-black principle is the same.** While recording, the same ffmpeg
  writes a tiny frame of what it is encoding once a second, and the recorder
  measures its brightness: black before recording fails the item, black from
  the first frame marks the player as capture-blocked, and long black aborts.
  Rehearse with one item before a batch either way.

## Black recordings

Some players use protected playback: the video shows on your screen but
reaches screen capture as black. playcap notices this (the page was fine a
moment before, then the recording stays black from the first frame), stops
the item, does not retry it, and says so in the UI. It does not work around
it: no flags, no settings changes, no other tricks. If a site blocks capture,
playcap is the wrong tool for that site.

A black recording can also mean OBS is capturing the wrong screen. Press
*Set up recording scene*, or run the smoke test on one page
(`python -m playcap.tools.smoke_test "<page-url>"`) to tell the two apart.
