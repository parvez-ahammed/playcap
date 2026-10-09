# Changelog

## 0.2.0

- **Try it now:** the first screen records a bundled six-second demo, so a new
  install proves itself before any setup. The demo ships inside the package.
- **One-click installs** for Chrome, OBS and ffmpeg through winget (Windows).
- **ffmpeg capture backend** (`"capture_backend": "ffmpeg"`): record without
  OBS. Hardware encoders when present, libx264 otherwise; same black-frame
  rules. System audio needs a loopback device (see docs/how-it-works.md).
- **Recipes:** teach playcap a page by clicking the player, play button and
  episode links; export/import as JSON. `collect: <index page>` builds the
  queue from a listing page, with pagination.
- **Notifications** (ntfy, Discord, webhook, Windows toast), **Jellyfin/Emby
  and Plex refresh** after each item, and **scheduled re-checks**
  (`playcap schedule`, optional Windows Task Scheduler task).
- `playcap --version`.
- **Portable Windows zip** built and smoke-tested on each release; PyPI
  publishing through trusted publishing.
- Responsible use: removed a network/DRM-API diagnostic from the public tree;
  clearer policy, rights-holder contact, issue templates.
- Fix: a partial ffmpeg recording is discarded when ffmpeg dies mid-item.

## 0.1.0

First public version.
