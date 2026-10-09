"""Smoke-test a built portable zip the way a user would run it.

    python packaging/windows/smoke_portable.py dist/playcap-<version>-windows-x64.zip

1. Extract the zip into a fresh temp folder.
2. Run the embedded python.exe: import playcap, its UI server, requests,
   websocket and tkinter, and start a Tcl interpreter (proves the grafted
   Tcl/Tk DLLs find their script library; the Browse dialog needs it).
   `-m playcap --version` is tried too, and only reported, because older
   builds of the CLI don't have it.
3. Start playcap.bat itself with --no-browser on a free port, wait for
   GET / to answer 200, check the launcher created data\\ with the demo in it,
   then kill the process tree. Repeat with a data-location.txt holding a
   %VARIABLE% path (what the installer ships). Finally check nothing was
   written under app\\ besides __pycache__.
The temp folder is removed afterwards. Exit code 0 means every check passed.
Windows only (it runs the .bat and the bundled python.exe).
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

IMPORT_CHECK = (
    "import sys, playcap, playcap.ui.server, playcap.recorder, requests, websocket, tkinter;"
    "print('playcap', playcap.__version__, '| python', sys.version.split()[0],"
    " '| tcl', tkinter.Tcl().eval('info patchlevel'), '| requests', requests.__version__)"
)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def app_files(top):
    """Files under app\\, ignoring the bytecode Python caches on its own."""
    return {p.relative_to(top) for p in (top / "app").rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    return ok


def serve(top, label, env=None):
    """Start playcap.bat, wait for the UI to answer GET /, kill the tree."""
    port = free_port()
    proc = subprocess.Popen(["cmd", "/c", str(top / "playcap.bat"), "--no-browser", "--port", str(port)],
                            cwd=str(top), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    status, body, deadline = None, "", time.time() + 30
    try:
        while time.time() < deadline and status is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                    status, body = r.status, r.read(4096).decode("utf-8", "replace")
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.5)
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        proc.wait(timeout=15)
        time.sleep(1)                               # let killed processes release file handles
    return check(status == 200 and "playcap" in body.lower(),
                 f"{label} served GET http://127.0.0.1:{port}/ -> {status}")


def run(zip_path):
    zip_path = Path(zip_path).resolve()
    results = []
    with tempfile.TemporaryDirectory(prefix="playcap-smoke-") as tmp:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        top = Path(tmp) / zip_path.stem
        py = top / "app" / "python" / "python.exe"
        results.append(check(py.exists(), f"extracted {zip_path.name}"))
        shipped = app_files(top)

        out = subprocess.run([str(py), "-c", IMPORT_CHECK], capture_output=True, text=True)
        print("  " + (out.stdout.strip() or out.stderr.strip()))
        results.append(check(out.returncode == 0, "imports (playcap, ui, requests, websocket, tkinter)"))

        ver = subprocess.run([str(py), "-m", "playcap", "--version"], capture_output=True, text=True)
        print(f"  -m playcap --version -> exit {ver.returncode}: "
              f"{(ver.stdout or ver.stderr).strip().splitlines()[-1:] }")

        results.append(serve(top, "playcap.bat"))
        results.append(check((top / "data" / "examples" / "demo" / "index.html").exists(),
                             "data\\ created next to playcap.bat, demo copied in"))

        # The installer's way: data-location.txt with a %VARIABLE% in it.
        (top / "data-location.txt").write_text("%PLAYCAP_SMOKE_DIR%\\relocated\n", encoding="ascii")
        results.append(serve(top, "playcap.bat with data-location.txt",
                             env={**os.environ, "PLAYCAP_SMOKE_DIR": tmp}))
        results.append(check((Path(tmp) / "relocated" / "examples" / "demo" / "index.html").exists(),
                             "data-location.txt honoured, %VARIABLE% expanded"))

        stray = sorted(app_files(top) - shipped)
        results.append(check(not stray, "no user files written under app\\"
                             + (f" (found {stray[:3]})" if stray else "")))
    return all(results)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(0 if run(sys.argv[1]) else 1)
