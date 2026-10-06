# playcap (repo: video-recorder)

An open-source browser DVR (GPL-3.0-or-later). It drives a real logged-in Chrome through
CDP, captures the screen with OBS until the page's `<video>` ends, then re-encodes and files
the results as a Jellyfin/Emby TV series. The core is generic. Everything site-specific
goes through an adapter.

Tests: `python -m pytest tests -q` (stdlib + pytest; no OBS/Chrome needed). Only the scripts are tracked: `.gitignore` excludes `local/`, `chrome-profile/`,
`browser-profile/`, `config.json`, the state JSON and the logs. **Git will not bring back a
deleted recording.** Prefer additive changes, and never delete recordings or
`progress.json` to "clean up".

## Layout

```
playcap/                 generic core. No site names anywhere in it.
  config.py              config.json + adapter defaults; picks the adapter
  adapters/base.py       Adapter interface, Item, Player
  adapters/html5_video.py  public adapter: queue = URL list file, player = first <video>
  recorder.py            record loop (CDP click -> fullscreen -> OBS -> poll <video> -> finalize)
  build_queue.py optimize.py organize.py status.py browser.py
  cdp.py obs_client.py   minimal DevTools / obs-websocket v5 clients
  ui/                    the UI: server.py (JSON API, Host/Origin/token guards) + index.html/app.js/style.css
  detect.py              finds Chrome/OBS/ffmpeg/ffprobe; reads OBS's websocket config
  settings.py            validated, atomic config.json saves for the UI
  jobs.py                start/stop jobs: PID files, stop flags in .playcap/, CTRL_BREAK, kill
  state.py               one snapshot for the UI (items, library, health, problems)
  obs_setup.py           idempotent OBS scene/capture setup; enables OBS websocket while OBS is closed
  record_quality.py      recording encoder (CRF/CBR) presets; written into OBS's profile while OBS is closed
  screen.py              aims OBS at the browser's monitor, pins the browser window on top (Windows)
  control.py             old entry point, now opens the UI
  tools/                 smoke_test, inspect_live, probe (generic diagnostics)
record_all.py build_queue.py optimize.py organize.py status.py control.py
                         thin root wrappers so the old commands still work
examples/demo/           test-pattern page + queue + config. Try playcap with no real site.
tests/                   pytest suite
playcap.bat              double-click launcher for the UI (Windows)
local/                   PRIVATE, GITIGNORED: the owner's site adapter and launchers
```

## local/ is private

`local/` holds the owner's own site adapter and launchers. It is gitignored: never commit it,
never name its site in tracked files. Its details live in `CLAUDE.local.md` (also gitignored).
Nothing from local/ belongs in the public tree: no login automation, no session/token copying,
no device-limit workarounds.

## Hard-won constraints. Do not "simplify" these away

- **A site's private API is usually a dead end.** Read the rendered DOM instead (adapter `build_queue`).
- **CDP input events are what make playback start.** They count as trusted user gestures;
  synthetic JS `.click()` does not start DRM playback.
- **Fullscreen the player element before recording**, or the capture is the page's player
  box (936px in the original setup) instead of native 1920x1080.
- **Chrome must be fully closed while a profile is copied.** Cookie and leveldb files are
  locked, and a half-copied leveldb reads as empty.
- **Chrome 136+ refuses `--remote-debugging-port` on the default user-data-dir**, hence a
  dedicated profile (`playcap.browser` uses `browser-profile/`).
- **DRM sometimes renders black to screen capture** while looking fine on screen. Verify
  captured frames are not black before trusting a run (`playcap.tools.smoke_test` does this).
- **`optimize` only moves the original after verifying the new file plays full length.**
  Keep that ordering. A crashed encode must never be able to lose the source.
- **A matching duration does not prove the encode is good.** `verify()` compares container
  length and nothing else. S01E10 passed it while carrying a malformed AAC frame two hours
  in. Before *deleting* a source, as opposed to archiving it, decode the whole replacement
  (`ffmpeg -v error -i new.mp4 -f null -`) and require zero decoder output. At CRF 24 that
  runs 4-25x realtime, so a full season takes a few hours.
- **The audio re-encode is the weak link, not the video.** `-c:a aac -b:a 96k -ac 1`
  downmixing a source's AAC LC 48 kHz stereo emitted one malformed frame, twice,
  deterministically, at the same timestamp, from a source that decoded clean both times.
  When the source audio is already AAC LC, copy it (`-c:a copy`) rather than re-encoding.
  That sidesteps the encoder, costs ~3% file size, and is bit-exact.

## Working rules

- Every stage is resumable and writes progress after each item. Keep it that way: a crash or
  Ctrl+C should cost at most the item in flight, and a rerun should skip finished work.
- Rehearse on one item with the smoke test before running a batch.
- Paths, ports, credentials, encoder settings, show name and title regexes come from
  `config.json` (or adapter `config_defaults`). Do not hardcode.
- Site knowledge goes in an adapter, never in `playcap/`. Check with
  `git grep -niE "<site names>"` before committing.
- Module docstrings carry the reasoning for each design choice. When you change behaviour,
  update the docstring in the same edit.
- Verify before destroying, not after. The 14 archived originals were each decoded frame by
  frame before being deleted. Every `optimize.json` entry carries `original_deleted` with the
  evidence that justified it. One file failed that check and was rebuilt instead of trusted.

## UI rules

- `python -m playcap ui` (root = current folder). Never call `config.load()` from UI code: it
  exits when config.json is missing and caches per process; use `settings.read` / `state._effective`.
- Stops are layered (flag file -> CTRL_BREAK -> kill). The recorder checks
  `.playcap/record.now` every second and `.playcap/record.after_current` between items; the
  optimizer checks `.playcap/optimize.now`. Keep those checks if you touch the loops.
- Queue edits (retry/skip) are refused while recording: the recorder rewrites the progress file
  after every item from memory.
- The page inserts data with textContent only. Keep it that way.

