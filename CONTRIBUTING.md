# Contributing to playcap

Thanks for helping. A few ground rules keep the project useful and safe to
publish.

## Scope

- **Welcome:** recorder robustness (stall recovery, re-attach, resumability),
  generic adapters for open standards (plain HTML5 video, HLS test pages,
  self-hosted players), OBS/ffmpeg handling, verification, filing, docs, and
  support for more platforms (Linux, macOS).
- **Not accepted:** DRM decryption or stream/key extraction, login
  automation, session or token copying, device- or concurrency-limit
  workarounds, and adapters for paid content platforms. See
  [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md).

Keep site-specific adapters in your own module, or in the gitignored
`local/` directory, and point `"adapter"` in `config.json` at it.

## Before you change the recorder

- Rehearse against the demo: `cd examples/demo`, then
  `python ../../build_queue.py` and `python ../../record_all.py --dry-run`.
  For a real capture, use `python -m playcap.tools.smoke_test <url>`, which
  checks that the captured frame is not black.
- Run `python -m py_compile` on every file you touch.
- Every stage must stay resumable. A crash or Ctrl+C may cost at most the
  item in flight, and a rerun must skip finished work.
- Never let a code path delete a source recording before its replacement has
  been verified. A duration match is enough to archive a source. Deleting one
  needs a full clean decode first.
- Paths, ports, credentials and encoder settings belong in `config.json`, not
  in code.

## Style

- Plain Python 3.10+, standard library plus `requests` and `websocket-client`.
- Module docstrings explain *why* a design choice was made, especially the
  ones learned the hard way. When you change behaviour, update the docstring in
  the same commit.
- Small, focused commits with messages that say what changed and why.

## License

By contributing you agree that your contribution is licensed under
GPL-3.0-or-later.
