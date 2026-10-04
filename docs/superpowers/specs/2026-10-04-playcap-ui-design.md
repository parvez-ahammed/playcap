# playcap UI — design

Date: 2026-10-04. Status: approved in conversation, awaiting spec review.

## Goal

One local web page that configures and runs playcap without the user ever editing
`config.json` or opening OBS settings. Public-first: a stranger can go from install to a
first recording through a 3-step wizard. Private setups (the owner's `local/` adapter)
appear as just another source.

Success:

- First run: wizard completes with at most 3-4 user inputs (source, its URL/links, output
  folder, show name); everything else is detected or defaulted.
- Daily use: one screen to start/stop recording or optimizing and see what is happening.
- No new runtime dependencies; no build step.

## Approach

Grow the existing stdlib control panel into a one-page app: Python `http.server` + JSON API
+ plain HTML/JS/CSS. Rejected: Tkinter/pywebview (dated or extra packaging), Flask/React
(dependencies and a build step for a small tool).

Launch: `python -m playcap ui` (and a double-clickable `playcap.bat`), which opens the
browser at `http://127.0.0.1:8765`.

## Screens

### Setup wizard (first run, and from the gear icon later)

1. **Tools** — Chrome, OBS, ffmpeg, ffprobe auto-detected, each with ✓/✗ and a Browse…
   fallback. OBS websocket port/password read from OBS's own config. A "Set up OBS" action
   creates the `playcap` scene, display capture and output settings.
2. **Source** — choose "List of links" (paste URLs, one per line) or any installed adapter
   (the owner's shows as its own entry). Each adapter declares the few fields it needs.
3. **Library** — output folder (Browse…) and show name. Finish writes `config.json`.

### Main screen

- Health bar: Browser, OBS, disk free (dots + one-line reason).
- Buttons: **Start recording**, **Stop after current** (finishes the item in flight), **Stop now** (interrupt; costs the item in flight), **Optimize library**. Record and
  optimize are mutually exclusive (both saturate CPU/disk; an encode next to OBS drops
  frames).
- Now playing: title, progress bar from the video position, elapsed, black-frame indicator.
- Queue: items with a state chip (waiting / done / failed / not aired / locked) and
  Retry / Skip per item.
- Library: episodes with size before → after.
- Settings gear: reopens the wizard; an "Advanced" fold exposes encoder preset, CRF,
  bitrate, port.

## Architecture

All new code under `playcap/`:

| Unit | Responsibility | Depends on |
| --- | --- | --- |
| `ui/server.py` | Serves the page and JSON API. Binds 127.0.0.1 only; Host allowlist, Origin check, per-run CSRF token on every POST (carried over from `control.py`). | jobs, detect, obs_setup, config, state files |
| `ui/index.html`, `ui/app.js`, `ui/style.css` | One page, vanilla JS, polls `/api/state` every 2 s. | API only |
| `detect.py` | Finds Chrome, OBS, ffmpeg, ffprobe (PATH, standard install dirs, Windows registry; macOS/Linux paths) and reads OBS websocket config (`%APPDATA%/obs-studio/plugin_config/obs-websocket/config.json` and the macOS/Linux equivalents). Pure functions, injectable roots for tests. | filesystem |
| `obs_setup.py` | Over obs-websocket: ensure scene `playcap`, a display-capture input, and record output settings. Idempotent. | `obs_client.py` |
| `jobs.py` | Start/stop chrome, queue, record, optimize as child processes; PID files so a job outlives the UI and is re-found on reopen; stop = CTRL_BREAK/SIGINT. Moves the job logic out of `control.py`. | subprocess |
| `config.py` (extended) | `save(partial)` validates and writes `config.json` atomically, merging with existing keys; unknown keys preserved. | — |

`control.py` becomes a thin alias that starts the new UI, so existing commands keep working.

### API

| Method | Path | Does |
| --- | --- | --- |
| GET | `/api/state` | health, running job, now-playing, queue summary + items, library sizes, last log lines |
| GET | `/api/detect` | wizard checks with found paths |
| POST | `/api/config` | validate + save settings (paths exist, output folder writable) |
| POST | `/api/obs/setup` | run `obs_setup` |
| POST | `/api/job/start`, `/api/job/stop` | body `{job, mode}`; mode `now` (interrupt) or `after_current` (writes `.playcap/record.after_current`; record only); record/optimize exclusive |
| POST | `/api/item/retry`, `/api/item/skip` | body `{id}`; edits progress file atomically (temp + rename) |

Every POST requires the token; every request requires an allowed Host.

### Data flow

- Recorder additionally writes `now.json` (title, position, duration, black-frame flag,
  updated_at) on each poll, and checks for a `.playcap/record.after_current` file between items (exits cleanly
  and deletes it). Those are the only two recorder changes.
- Queue/progress/optimize state are read from the existing JSON files; the UI never holds
  state the files do not.
- Retry = remove the item's `failed` entry; Skip = mark `skipped` (the recorder already
  treats `skipped` as permanent).

## Error handling

Each failure surfaces as one plain sentence plus a fix action, raw log behind "Details":

- OBS not running → [Launch OBS]; websocket auth failed → [Re-read OBS settings]
- Browser not reachable → [Open browser]; adapter reports login page → "Log in, then retry"
- Disk below 10 GB → blocks Start recording with the free-space number
- Job crashed → state "failed", last 25 log lines, [Restart]

Edits to config/progress are atomic, so a crash mid-save never corrupts them.

## Testing

- Unit: `detect` against fake directory trees; `obs_setup` against a fake websocket
  server (idempotency: second run makes no changes); `config.save` validation and
  key preservation; atomic retry/skip edits; server rejects bad Host/Origin/token.
- End-to-end (manual, when the machine is free): wizard → demo source → record
  `examples/demo` → optimize, in a real Chrome + OBS.

## Out of scope (v1)

Scheduling, remote access, multiple profiles, login automation (never), installers.

## Constraints carried over

All hard-won constraints in `CLAUDE.md` stay: trusted CDP input, fullscreen iframe,
black-frame check, verify-before-archive in optimize, resumable stages, config-driven
paths. The UI orchestrates existing stages; it does not reimplement them.
