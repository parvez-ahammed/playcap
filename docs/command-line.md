# Command line

Everything the UI does is also available as plain commands. Run them from the
folder that holds `config.json`. [Back to the README](../README.md)

## Try it with the bundled demo (no real site)

`examples/demo/` has a page that plays a six-second generated test pattern,
plus a queue file and a config. After `pip install -e .`, every command reads
`config.json` from the current folder:

```
cd examples/demo
python -m playcap.build_queue             # reads queue.txt -> queue.json
python -m playcap.recorder --dry-run      # prints the plan, records nothing
```

To record it for real:

1. Set up OBS once. Either open the UI (`playcap ui`) and press *Launch OBS*
   and *Set up recording scene* in the Tools step, or run
   `python -m playcap.obs_setup`, which switches on OBS's websocket (OBS must
   be closed for that part) and creates the screen-capture scene. You do not
   need to copy OBS's websocket password anywhere: playcap reads it from OBS's
   own settings.
2. Start the debug browser: `python -m playcap.browser` (run it from
   `examples/demo`; it uses a dedicated profile in `browser-profile/`).
3. Rehearse: `python -m playcap.tools.smoke_test "file:///.../examples/demo/index.html"`
   (the full path to the file). It records 25 seconds and checks that the
   captured frame is not black.
4. `python -m playcap.recorder`, then `python -m playcap.optimize` and
   `python -m playcap.status`.

## Your own queue

Copy `config.example.json` to `config.json` in a folder of your choice and
point `queue_source` at a text file of URLs (relative paths in it resolve
against that file):

```
# one per line; "Title | url" is also accepted
https://example.org/talks/opening-keynote
Closing panel | https://example.org/talks/closing-panel
```

## All commands

| Command | What it does |
| --- | --- |
| `python -m playcap ui [--port N] [--no-browser] [--root DIR]` (or `playcap ui`) | The UI on http://127.0.0.1:8765 |
| `python -m playcap.obs_setup` | Switch on OBS's websocket (OBS closed) and create the capture scene |
| `python -m playcap.browser` (or `playcap-browser`) | Launch Chrome with remote debugging on a dedicated profile |
| `python -m playcap.build_queue` (or `playcap-queue`) | Ask the adapter for the queue and save it |
| `python -m playcap.recorder [--dry-run] [--limit N] [--speed 2] [--only KIND]` (or `playcap-record`) | Record the queue |
| `python -m playcap.optimize [--verify] [--only TEXT] [--crf 24]` (or `playcap-optimize`) | Verified in-place re-encode |
| `python -m playcap.organize [--dry-run]` (or `playcap-organize`) | Re-file finished recordings under the current naming layout (and `.nfo` files if on) |
| `python -m playcap.status [--watch]` (or `playcap-status`) | Write `status.html` |
| `python -m playcap.tools.smoke_test URL` | Rehearse one page end to end |
| `python -m playcap.tools.inspect_live` | Show what the `<video>` in each open tab reports |

The repo root also keeps the old entry points (`python build_queue.py`,
`record_all.py`, `optimize.py`, `organize.py`, `status.py`, `control.py`) as
thin wrappers around the same modules.
