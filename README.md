# playcap

[![tests](https://github.com/parvez-ahammed/playcap/actions/workflows/tests.yml/badge.svg)](https://github.com/parvez-ahammed/playcap/actions/workflows/tests.yml)
[![License: GPL v3+](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Chrome + OBS](https://img.shields.io/badge/records%20with-Chrome%20%2B%20OBS-red.svg)

**Screen recorders stop by the clock. playcap stops when the video does.**

Give playcap a list of web pages that play video. It opens each one in your
own Chrome, presses play, records the screen with OBS until the video ends,
and saves everything in a tidy folder. Then it moves on to the next one, so
you can leave it running overnight.

[![playcap in 60 seconds](docs/media/playcap-overview.gif)](docs/media/playcap-overview.mp4)

*playcap in 60 seconds.* Click for the [full-quality MP4 (3.5 MB)](docs/media/playcap-overview.mp4).
Made with example data only.

## Why playcap

- **Stops when the video ends**, not at a guessed time. No cut-off endings, no hour of black screen.
- **Runs unattended.** Stuck players get nudged, failed items get retried, and a crash loses at most one video.
- **A library, not a pile.** Files come out numbered and named, or laid out for Jellyfin, Emby, Plex and Kodi.
- **Small files.** Pick the recording quality once; still slides cost almost nothing.
- **Point and click.** A local setup screen finds Chrome, OBS and ffmpeg and sets up OBS for you.

## What you need

- [Python 3.10+](https://www.python.org/downloads/) on Windows (tested; macOS and Linux should work but are untested)
- [Google Chrome](https://www.google.com/chrome/)
- [OBS Studio](https://obsproject.com/) 28 or newer
- [ffmpeg](https://ffmpeg.org/download.html) (includes ffprobe)

## Getting started

1. Download playcap and install it:

   ```
   git clone https://github.com/parvez-ahammed/playcap.git
   cd playcap
   pip install -e .
   ```

2. Start it: `playcap ui` (on Windows you can double-click `playcap.bat`).
   Your browser opens on http://127.0.0.1:8765.

3. Follow the three-step setup:
   - **Tools:** Chrome, OBS and ffmpeg are found for you. Press *Launch OBS*
     and *Set up recording scene*.
   - **What to record:** paste page links, one per line. To try it first,
     press *Try it now* at the top of the page: it records a six-second test
     video that ships with playcap into its own `playcap-demo/` folder.
     Missing Chrome, OBS or ffmpeg? On Windows with winget, each has an
     *Install with winget* button.
   - **Library:** choose where recordings go, how they are named and the
     quality.

4. Press *Open browser* and sign in to your site once in that window. Then
   *Refresh queue* and *Start recording*.

*Stop after this one* finishes the current video; *Stop now* stops straight
away and keeps the video queued for next time. Recordings keep going even if
you close the page.

## Learn more

| | |
| --- | --- |
| [How it works](docs/how-it-works.md) | Features in detail, architecture, design notes, black recordings |
| [Command line](docs/command-line.md) | Run everything without the UI, all commands and options |
| [Writing an adapter](docs/adapters.md) | Teach playcap about a site whose player needs special handling |
| [Responsible use](RESPONSIBLE_USE.md) | What playcap will and will not do |
| [Contributing](CONTRIBUTING.md) | Bug reports, fixes and new adapters |

## Responsible use

playcap records your screen, like a camera pointed at your monitor. It does
not decrypt DRM, download streams or automate logins. Record only what you are
allowed to watch, respect each site's terms, and do not share recordings of
content you do not own. Some sites block screen capture; playcap tells you and
skips them rather than working around it. Full statement:
[RESPONSIBLE_USE.md](RESPONSIBLE_USE.md).

## Contributing

Bug reports, fixes and adapters for new players are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md). If playcap saves you a night of babysitting
a recorder, a star helps other people find it.

## License

GNU General Public License v3.0 or later. See [LICENSE](LICENSE).
