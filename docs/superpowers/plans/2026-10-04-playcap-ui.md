# playcap UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local one-page web app (`python -m playcap ui`) that sets up and runs playcap with a 3-step wizard and one control screen.

**Architecture:** stdlib `http.server` serving static files from `playcap/ui/` plus a JSON API. Pure helper modules (`detect`, `settings`, `jobs`, `state`, `obs_setup`) hold all logic and are unit-tested; the server is a thin router. Jobs are child processes tracked by PID files so they survive the UI.

**Tech Stack:** Python 3.10+ stdlib, `websocket-client` (already a dependency), vanilla HTML/CSS/JS, pytest for tests.

**Spec:** `docs/superpowers/specs/2026-10-04-playcap-ui-design.md`

## Global Constraints

- No new runtime dependencies; no build step.
- Bind 127.0.0.1 only; Host allowlist `127.0.0.1:<port>`, `localhost:<port>`; Origin check; per-run token on every POST.
- `config.json` and progress files written atomically (temp file + `os.replace`); unknown config keys preserved.
- UI must work before `config.json` exists (wizard) — never call `config.load()` (it exits when the file is missing, and caches).
- Record and optimize are mutually exclusive.
- Recorder changes limited to: write `now.json` each poll; honour `.playcap/record.after_current` between items; SIGBREAK → KeyboardInterrupt (same for optimize).
- Hard-won constraints in CLAUDE.md unchanged.

## Review Focus

- Stop from the UI on Windows: CTRL_BREAK must reach the job and trigger its clean shutdown (OBS stopped, progress saved), not kill it — test the handler is installed.
- UI restarted while a job runs: job must show as running (PID file) and Stop must still work; stale PID file of a dead process must read as idle.
- Paths with spaces/apostrophes (e.g. `Lib Dec'26`) in config and library listing — test with such a path.
- Missing or corrupt JSON state files (empty progress.json, half-written queue) — state must degrade to empty, not 500.
- Requests from another site (bad Host/Origin/no token) — rejected with 403, nothing started.

---

### Task 1: detect.py — find tools and OBS websocket settings
**Files:** Create `playcap/detect.py`, `tests/test_detect.py`
**Produces:** `find_tool(name, roots=None) -> str|None` for `chrome|obs|ffmpeg|ffprobe`; `obs_websocket(appdata=None) -> {"url","password","enabled"}|None`; `report(cfg) -> dict[name -> {"path","ok"}]`.
- [ ] Tests: fake dir trees (tmp_path) with `chrome.exe`, `obs64.exe`, `ffmpeg.exe` at standard relative locations; PATH lookup via monkeypatched `shutil.which`; obs websocket JSON parsed (port→url, password); missing/corrupt JSON → None; configured path that exists wins over detection.
- [ ] Implement, run `python -m pytest tests/test_detect.py -q`, commit.

### Task 2: settings.py — validate + atomic save of config.json
**Files:** Create `playcap/settings.py`, `tests/test_settings.py`
**Produces:** `read(path) -> dict` ({} when missing/corrupt); `save(path, partial) -> (cfg, errors)`; `atomic_write_json(path, obj)`; `adapters_available() -> [{"module","label","fields":[...]}]`.
- [ ] Tests: merge preserves unknown keys; output_dir must be creatable/writable; tool paths must exist when given; errors returned not raised; atomic write leaves no temp file; path with apostrophe+space round-trips.
- [ ] Implement (adapters list: `playcap.adapters.html5_video` always; `local.ADAPTER` when importable; adapter class attrs `label` and `setup_fields` with fallbacks), test, commit.

### Task 3: jobs.py — PID-file job control + SIGBREAK handling
**Files:** Create `playcap/jobs.py`, `tests/test_jobs.py`; Modify `playcap/recorder.py`, `playcap/optimize.py` (install `jobs.graceful_signals()` at top of `main`).
**Produces:** `JOBS` names `browser|queue|record|optimize`; `start(name, root) -> str`; `stop(name, root, mode="now"|"after_current") -> str`; `status(root) -> {name: {"running","pid","started"}}`; `graceful_signals()`.
- [ ] Tests: start a dummy long-running python child via injected command → status running, PID file written; stop → process exits; stale PID file → idle and file removed; exclusive record/optimize refused; `after_current` writes `.playcap/record.after_current`; `graceful_signals` maps SIGBREAK (when present) to `default_int_handler`.
- [ ] Implement, test, commit.

### Task 4: recorder hooks — now.json, `.playcap/record.after_current`, black-frame sample
**Files:** Modify `playcap/recorder.py`, `playcap/obs_client.py` (add `screenshot_luma(source, w=64, h=36) -> float|None` via `GetSourceScreenshot` bmp); Test `tests/test_recorder_hooks.py`
**Produces:** `recorder.write_now(item, s, duration, luma)`, `recorder.stop_requested() -> bool` (consumes flag).
- [ ] Tests: `write_now` writes atomic JSON with title/t/duration/luma/updated_at; `stop_requested` true once then false; BMP luma parser on a synthetic BMP (black → 0, white → 255).
- [ ] Wire: call `write_now` each poll (luma best-effort, exceptions swallowed); check `stop_requested()` before each item → break with message; clear `now.json` at exit. Docstring updated. Test, commit.

### Task 5: obs_setup.py — ensure scene and capture
**Files:** Create `playcap/obs_setup.py`, `tests/test_obs_setup.py`
**Produces:** `ensure(obs, scene="playcap") -> [actions]`.
- [ ] Tests with a fake Obs object recording requests: empty OBS → creates scene, adds display capture input, sets current scene; second run → `[]` actions.
- [ ] Implement (input kind by platform: `monitor_capture` Windows, `screen_capture` macOS, `xshm_input` Linux), test, commit.

### Task 6: state.py — one snapshot for the UI
**Files:** Create `playcap/state.py`, `tests/test_state.py`
**Produces:** `snapshot(root) -> dict` keys `configured, health{browser,obs,disk_gb}, jobs, now, counts, items[], library[], log{name: tail}`.
- [ ] Tests: no config → `configured False` and no exception; corrupt progress/queue → empty lists; items get state chips (done/failed/skipped/waiting/not_aired/locked); library sizes from output_dir incl. apostrophe path; stale now.json (>60 s) ignored.
- [ ] Implement using adapter only if config present (load fresh, no cache), test, commit.

### Task 7: ui/server.py + static UI + `python -m playcap ui`
**Files:** Create `playcap/ui/__init__.py`, `playcap/ui/server.py`, `playcap/ui/index.html`, `playcap/ui/app.js`, `playcap/ui/style.css`, `playcap/__main__.py`, `playcap.bat`; Modify `playcap/control.py` → thin alias to UI; Test `tests/test_server.py`
**API:** as spec; plus `GET /` serves index with token injected; `/static/*`.
- [ ] Tests: run server on an ephemeral port in a thread; GET `/api/state` 200; POST without token 403; bad Host 403; bad Origin 403; POST `/api/config` validates; `/api/item/skip` edits progress atomically.
- [ ] Implement UI (wizard: tools/source/library; main: health, buttons, now-playing, queue with retry/skip, library, details logs; dark/light via prefers-color-scheme), test, commit.

### Task 8: docs + end-to-end smoke
**Files:** Modify `README.md` (UI quick start first), `CLAUDE.md` (pipeline map), `pyproject.toml` (package data for ui files).
- [ ] Run full test suite; start UI headless and hit every GET endpoint against the owner config copy and demo config; confirm dry-run parity still `IDENTICAL`.
- [ ] Commit.
