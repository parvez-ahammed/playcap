"""Build the portable Windows download: dist/playcap-<version>-windows-x64.zip

    python packaging/windows/build_portable.py          (from the repo root)

What the zip holds, and why:

    playcap-<version>-windows-x64/
      playcap.bat          launcher: app\\python\\python.exe -m playcap ui --root <data>
      README.txt           start, SmartScreen, data folder, upgrade, responsible use
      LICENSE.txt          GPL-3.0-or-later
      RESPONSIBLE_USE.md
      app/
        python/            official CPython embeddable package + tkinter
          Lib/site-packages/   playcap, requests, websocket-client (+ their deps)
        examples/demo/     copied into the data folder on first run

- Embeddable Python, not a PyInstaller exe. --onefile bundles unpack to %TEMP%
  at run time, which is what antivirus heuristics flag. The embeddable package
  is python.org's own signed python.exe; nothing is unpacked or generated.
- Pinned and hash-checked. The Python version and the SHA256 of every
  download are constants below; a mismatch stops the build. Both Python
  hashes are the ones python.org publishes (release-file API / SPDX). Runtime
  deps come from requirements-lock.txt with --require-hashes.
- tkinter is grafted in. The embeddable package ships without it, but the UI's
  Browse buttons open a tkinter file dialog (playcap/ui/server.py BROWSE_JS).
  The pieces come from python.org's full Windows zip of the *same* version
  (_tkinter.pyd, the Tcl/Tk 9 DLLs, which carry their script libraries inside,
  zlib1.dll which Tcl 9 links, and Lib/tkinter). --no-tk skips this.
- Data lives outside app/. The launcher runs the UI with --root pointing at a
  data folder (default "data" next to playcap.bat; overridable, see the .bat),
  so extracting a new version over the old one replaces app/ and never touches
  config.json, the queue, progress or recordings. "data" is not in the zip.
- The ._pth file is rewritten to add Lib, Lib\\site-packages and "import site".
  A ._pth puts Python in isolated mode, which also drops the current folder
  from sys.path. playcap-portable.pth appends it back (append, not insert, so a
  stray file in the data folder can't shadow the stdlib) so that a custom
  adapter module saved in the data folder imports the way it does with a normal
  `python -m playcap ui` run from that folder.
- Dependencies are installed with `pip install --target` from the *build*
  Python with --platform/--python-version/--abi set for cp314-win_amd64, so the
  build needs any Python 3.10+ with pip, not the embedded one. The `bin/`
  launchers pip --target generates point at the build Python and are removed.
- ffmpeg is not bundled. A static GPL build adds ~100 MB uncompressed (ffmpeg +
  ffprobe) to a ~20 MB download, and the setup screen already finds an
  installed ffmpeg (PATH, WinGet links, common folders). README.txt points to
  `winget install Gyan.FFmpeg`.

Downloads are cached in build/portable-cache/ (gitignored). Output: dist/
holds the zip and <zip>.sha256 in `sha256sum -c` format. Zip entries get a
fixed timestamp and order, and the per-build noise pip leaves (bin/ launchers,
direct_url.json, their RECORD lines) is removed, so two builds on one machine
give byte-identical zips. A different pip/setuptools may still change bytes.
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

PY_VERSION = "3.14.8"
PY_TAG = "314"                      # cp314: wheel ABI tag and python314.zip / ._pth stem
EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
EMBED_SHA256 = "a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310"
FULL_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-amd64.zip"
FULL_SHA256 = "4873947a8afc037846b180312b83c744a4146a851cfd316a75c3125a4d8299da"
TK_DLLS = ["DLLs/_tkinter.pyd", "DLLs/tcl90.dll", "DLLs/tcl9tk90.dll", "DLLs/zlib1.dll"]
TK_LIB = "Lib/tkinter/"

CACHE = REPO / "build" / "portable-cache"
STAGE_ROOT = REPO / "build" / "portable"
DIST = REPO / "dist"
ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def log(msg):
    print(f"[build] {msg}", flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, expected):
    """Download url into the cache once; refuse it unless the SHA256 matches."""
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / url.rsplit("/", 1)[1]
    if dest.exists() and sha256(dest) == expected:
        log(f"cached  {dest.name}")
        return dest
    part = dest.with_name(dest.name + ".part")
    log(f"fetch   {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(part, "wb") as out:
        shutil.copyfileobj(r, out)
    got = sha256(part)
    if got != expected:
        part.unlink()
        sys.exit(f"SHA256 mismatch for {url}\n  expected {expected}\n  got      {got}")
    os.replace(part, dest)
    return dest


def project_version():
    text = (REPO / "playcap" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        sys.exit("could not read __version__ from playcap/__init__.py")
    return m.group(1)


def pip(*args):
    cmd = [sys.executable, "-m", "pip", "--disable-pip-version-check", *args]
    log("run     " + " ".join(cmd[3:]))
    subprocess.run(cmd, check=True)


def write_pth(py_dir):
    pth = py_dir / f"python{PY_TAG}._pth"
    if not pth.exists():
        sys.exit(f"{pth.name} missing from the embeddable package; PY_TAG out of date?")
    pth.write_text(f"python{PY_TAG}.zip\n.\nLib\nLib\\site-packages\nimport site\n", encoding="ascii")


def graft_tk(py_dir, full_zip):
    with zipfile.ZipFile(full_zip) as z:
        names = z.namelist()
        for name in TK_DLLS:
            (py_dir / Path(name).name).write_bytes(z.read(name))
        lib = [n for n in names if n.startswith(TK_LIB) and not n.endswith("/")]
        if not lib:
            sys.exit(f"{TK_LIB} not found in {full_zip.name}")
        for name in lib:
            out = py_dir / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(name))
    log(f"tkinter {len(TK_DLLS)} DLLs + {len(lib)} Lib/tkinter files")


def install_packages(site, work):
    wheels = work / "wheel"
    pip("wheel", str(REPO), "--no-deps", "-w", str(wheels), "-q")
    wheel = next(wheels.glob("playcap-*.whl"))
    pip("install", "-q", "--target", str(site), "--no-deps", "--require-hashes",
        "--only-binary=:all:", "--platform", "win_amd64", "--implementation", "cp",
        "--python-version", PY_VERSION.rsplit(".", 1)[0],
        "--abi", f"cp{PY_TAG}", "--abi", "abi3", "--abi", "none",
        "-r", str(HERE / "requirements-lock.txt"))
    shutil.rmtree(site / "bin", ignore_errors=True)       # launchers for the build Python
    pip("install", "-q", "--target", str(site), "--no-deps", str(wheel))
    shutil.rmtree(site / "bin", ignore_errors=True)
    for f in site.glob("playcap-*.dist-info/direct_url.json"):
        f.unlink()                                        # records the temp wheel path
    # Drop RECORD lines for the removed files: the bin/ launchers differ on
    # every build, and listing files that aren't there would mislead.
    for record in site.glob("*.dist-info/RECORD"):
        lines = record.read_text(encoding="utf-8").splitlines(keepends=True)
        keep = [l for l in lines if not l.startswith("../../bin/") and "/direct_url.json," not in l]
        record.write_text("".join(keep), encoding="utf-8", newline="")
    (site / "playcap-portable.pth").write_text(
        "import os, sys; sys.path.append(os.getcwd())\n", encoding="ascii")


def write_crlf(src, dest, **fmt):
    text = src.read_text(encoding="utf-8")
    if fmt:
        text = text.format(**fmt)
    with open(dest, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(text)


def make_zip(stage, zip_path):
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_name(zip_path.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                # Fixed timestamp and order: same inputs give the same zip bytes.
                info = zipfile.ZipInfo((Path(stage.name) / p.relative_to(stage)).as_posix(),
                                       date_time=ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, p.read_bytes(), compresslevel=9)
    os.replace(tmp, zip_path)
    digest = sha256(zip_path)
    sums = zip_path.with_name(zip_path.name + ".sha256")
    sums.write_text(f"{digest} *{zip_path.name}\n", encoding="ascii")
    return digest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-tk", action="store_true",
                    help="skip tkinter (the UI's Browse buttons then do nothing)")
    args = ap.parse_args(argv)

    version = project_version()
    name = f"playcap-{version}-windows-x64"
    stage = STAGE_ROOT / name
    if stage.exists():
        shutil.rmtree(stage)
    py_dir = stage / "app" / "python"
    py_dir.mkdir(parents=True)

    with zipfile.ZipFile(fetch(EMBED_URL, EMBED_SHA256)) as z:
        z.extractall(py_dir)
    write_pth(py_dir)
    if not args.no_tk:
        graft_tk(py_dir, fetch(FULL_URL, FULL_SHA256))

    with tempfile.TemporaryDirectory(prefix="playcap-build-") as work:
        install_packages(py_dir / "Lib" / "site-packages", Path(work))

    shutil.copytree(REPO / "examples" / "demo", stage / "app" / "examples" / "demo",
                    ignore=shutil.ignore_patterns("recordings", "*.log", "progress.json"))
    write_crlf(HERE / "playcap.bat", stage / "playcap.bat")
    write_crlf(HERE / "README.txt", stage / "README.txt", version=version)
    write_crlf(REPO / "LICENSE", stage / "LICENSE.txt")
    shutil.copy2(REPO / "RESPONSIBLE_USE.md", stage / "RESPONSIBLE_USE.md")

    zip_path = DIST / f"{name}.zip"
    digest = make_zip(stage, zip_path)
    size = zip_path.stat().st_size / 1e6
    log(f"wrote   {zip_path.relative_to(REPO)}  {size:.1f} MB  sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
