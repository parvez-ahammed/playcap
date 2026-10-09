playcap {version} for Windows (portable)
==========================================

playcap records web video until it ends: it opens each page in your own
Chrome, presses play, records the screen with OBS until the video finishes,
and files the result in a tidy library.

Start
-----
1. Double-click playcap.bat. A console window opens (leave it open; closing
   it closes playcap) and your browser shows http://127.0.0.1:8765
2. Follow the three setup steps on that page.

No Python install is needed: a private copy of Python is inside "app".

You also need (install them once; playcap finds them for you):
  - Google Chrome        https://www.google.com/chrome/
  - OBS Studio 28+       https://obsproject.com/
  - ffmpeg (has ffprobe) https://ffmpeg.org/download.html
    or, in a terminal:   winget install Gyan.FFmpeg

"Windows protected your PC"
---------------------------
This download is not code-signed yet, so Windows SmartScreen may warn the
first time you run playcap.bat. Click "More info", then "Run anyway".
If Windows blocked the whole zip, right-click the zip before extracting it,
choose Properties, tick "Unblock", and click OK.
Only do this for a zip you downloaded from the official releases page:
https://github.com/parvez-ahammed/playcap/releases
and, if you like, check it against the .sha256 file published next to it:
  certutil -hashfile playcap-{version}-windows-x64.zip SHA256

Where your files go
-------------------
Settings, the queue and recordings are kept in the "data" folder next to
playcap.bat (created on first run). "data" is not part of the download.

Upgrading: extract the new zip over this folder (your "data" folder is left
alone), or extract it somewhere new and move your "data" folder across.

To keep data somewhere else, set the PLAYCAP_DATA environment variable to a
folder, or put that folder's path on the first line of a file named
data-location.txt next to playcap.bat.

Use it responsibly
------------------
playcap is for video you are entitled to keep. It does not decrypt DRM,
bypass access controls or log in for you. Please read RESPONSIBLE_USE.md
(included here) or
https://github.com/parvez-ahammed/playcap/blob/main/RESPONSIBLE_USE.md

License
-------
playcap is free software under the GNU GPL, version 3 or later (LICENSE.txt).
The bundled Python is under the PSF License (app\python\LICENSE.txt).
Source code and help: https://github.com/parvez-ahammed/playcap
