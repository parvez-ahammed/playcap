# Roadmap

What's planned, roughly in order. Items marked **help wanted** are good places
to jump in; open an issue or a discussion before starting anything large.

## Now (0.2)

- Portable Windows zip on every release, and `uvx playcap ui` from PyPI.
- *Try it now* first screen with the bundled demo; one-click winget installs.
- ffmpeg capture backend, so OBS is optional.
- Recipes: teach playcap a page by clicking, build the queue from an index page.
- Notifications, Jellyfin/Plex refresh, scheduled re-checks ("subscribe").

## Next

- **Signed Windows builds** through SignPath Foundation, then an installer and
  a winget package (drafts in [packaging/](packaging/)).
- **System audio without OBS.** Mainline ffmpeg has no WASAPI loopback input
  yet, so the ffmpeg backend needs a "Stereo Mix"-style device for sound.
  A small, dependency-free loopback capture would remove that. **help wanted**
- **More recipes** for common player types (Video.js, Plyr, JW Player,
  Shaka, hls.js demo pages, self-hosted media servers), tested on pages you
  run yourself. **help wanted**
- **Picking inside cross-origin player iframes** in the recipe picker.
- **Display scaling** other than 100% for the ffmpeg backend, tested.

## Later

- **macOS and Linux.** The pure-Python parts already run in CI there; capture,
  window pinning and monitor detection need ports and testers. **help wanted**
- Headless/virtual-display mode on Linux.
- Per-item retry policy (for example: don't retry capture-blocked items on
  every scheduled run).

## Not planned

Anything in the "will not do" list of [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md):
DRM decryption, stream or key extraction, login automation, session copying,
and adapters or recipes for paid streaming or course platforms.
