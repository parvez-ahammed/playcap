"""Launch a debuggable Chrome on a dedicated profile.

Chrome 136+ refuses --remote-debugging-port on the default user-data-dir, so
playcap drives a separate profile directory (config "browser_profile",
default ./browser-profile). The first time, sign in to whatever you want to
record in that window by hand -- playcap never automates logins and never
copies cookies, tokens or sessions from your everyday profile.

Idempotent: if something already answers on the debug port it exits at once,
which is what lets the recorder call it again after a browser crash.

    python -m playcap.browser
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

from playcap import config, detect

def find_chrome(cfg):
    path = detect.resolve("chrome", cfg)
    if not path:
        sys.exit("Chrome not found; set chrome_exe in config.json or pick it in the playcap UI.")
    return path


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    cfg, _ = config.load()
    port = int(cfg["chrome_debug_port"])
    if port_open(port):
        print(f"[browser] debug Chrome already listening on {port}")
        return
    profile = Path(cfg.get("browser_profile", "browser-profile")).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        find_chrome(cfg),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--start-maximized",
    ]
    subprocess.Popen(args, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    for _ in range(40):
        if port_open(port):
            print(f"[browser] debug Chrome up on http://127.0.0.1:{port}")
            return
        time.sleep(0.5)
    sys.exit("Chrome did not open the debugging port in time.")


if __name__ == "__main__":
    main()
