# Packaging

How playcap reaches people who don't use git or Python. Only step 1 is live.
Everything else here is prepared but **not published or submitted anywhere**.

| Path | What it is | Status |
|---|---|---|
| `windows/build_portable.py` | builds `dist/playcap-<version>-windows-x64.zip` | live (CI) |
| `windows/smoke_portable.py` | extracts a zip and checks it runs | live (CI) |
| `windows/playcap.bat` | launcher shipped in the zip | live |
| `windows/README.txt` | user-facing readme shipped in the zip | live |
| `windows/requirements-lock.txt` | hash-pinned runtime deps for the zip | live |
| `../.github/workflows/release-windows.yml` | build, test, attach to a GitHub release | live |
| `windows/playcap.iss` | Inno Setup per-user installer | draft |
| `winget/manifests/...` | winget manifests (for the installer) | draft |
| `scoop/playcap.json` | scoop manifest (for the zip) | draft |

## The order

1. **Portable zip now.** Publishing a GitHub release runs
   `release-windows.yml`: tests on Python 3.14, checks the tag matches
   `playcap.__version__`, builds the zip, smoke-tests it, and attaches the zip
   and its `.sha256`. Unsigned, so SmartScreen warns on first run; the zip's
   README.txt tells people how to proceed.
2. **Code signing via SignPath Foundation.** Free for OSS projects. Apply at
   signpath.org, then add their GitHub Action to the release workflow to sign
   the exe/dll files you produce (here: the Inno installer and, if wanted, a
   signed copy of the zip's contents). The certificate is SignPath
   Foundation's, so the publisher shown by Windows is "SignPath Foundation",
   not playcap. Signing removes the "unknown publisher" warning, but
   SmartScreen reputation still builds over time and downloads; expect warnings
   for the first weeks even when signed.
3. **Inno Setup installer.** Once signing works, build
   `playcap-<version>-setup.exe` from `windows/playcap.iss` in the release
   workflow (after the zip step; `choco install innosetup` or the preinstalled
   copy on windows-latest), sign it, attach it.
4. **winget.** Submit `winget/manifests/p/playcap/playcap/<version>/` to
   microsoft/winget-pkgs (use `wingetcreate update` for later versions, which
   fills `InstallerSha256`). winget points at the signed installer, because its
   zip/portable type needs an .exe and playcap starts from a .bat. Scoop
   (`scoop/playcap.json`) can go to a bucket at any point after step 1; it
   uses the zip and fills hashes from the `.sha256` file via `autoupdate`.

## Decisions

- **No PyInstaller.** `--onefile` exes unpack to %TEMP% at run time and are a
  steady source of antivirus false positives. The zip ships python.org's own
  embeddable CPython (signed by the PSF) with playcap in its site-packages.
- **Pinned and hash-checked.** Python version and download SHA256s are
  constants in `build_portable.py` (the hashes python.org publishes); deps are
  installed with `--require-hashes`. Bump them together, then run the build and
  the smoke test.
- **tkinter is grafted in** from python.org's full Windows zip of the same
  version, because the UI's Browse buttons use a tkinter file dialog and the
  embeddable package has no tkinter.
- **ffmpeg is not bundled.** It would grow a ~17 MB zip by ~100 MB uncompressed.
  The setup screen finds an installed ffmpeg; README.txt points to
  `winget install Gyan.FFmpeg`.
- **User data never lives in the program folder.**
  - Portable zip: `data\` next to `playcap.bat`, created on first run and not
    part of the zip, so extracting a new version over the old one keeps it.
  - Installer: `%USERPROFILE%\playcap` (via `data-location.txt`, which the
    launcher reads). Visible, and outside OneDrive-synced Documents because
    recordings are large. Uninstall leaves it alone.
  - Scoop: `persist: data` keeps it in scoop's persist folder across updates.
  - Anyone: set `PLAYCAP_DATA` to override.
  The launcher passes the folder to `python -m playcap ui --root`, which
  already existed, so playcap itself needed no change.

## Build locally

```
python packaging/windows/build_portable.py
python packaging/windows/smoke_portable.py dist/playcap-0.1.0-windows-x64.zip
```

Needs any Python 3.10+ with pip and network access the first time (downloads
are cached in `build/portable-cache/`). `build/` and `dist/` are gitignored.
