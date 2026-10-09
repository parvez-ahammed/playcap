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

## Architecture

```
config.json ──> playcap.config ──> adapter (playcap.adapters.* or your module)
                                       │
build_queue ── adapter.build_queue ──> queue.json
                                       │
recorder ── per item: page_target -> navigate -> check_page -> find_player
            -> CDP trusted click (start playback) -> rewind to 0 if resumed
            -> fullscreen player -> OBS StartRecord -> poll <video> every 10 s
               (stall nudge, re-attach, pause resume, time budget)
            -> OBS StopRecord -> file by the naming layout -> remux to faststart mp4
            -> .nfo (media-server layout, or when switched on)
            -> progress.json (after every item)
                                       │
optimize ── CRF re-encode to a sidecar -> verify duration -> archive original
            -> sidecar takes the episode name -> optimize.json
organize / status / control ── filing, dashboard, local control panel
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
