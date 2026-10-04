#!/usr/bin/env python3
"""Render status.html -- one page showing what is recorded, what is optimized,
and what either job is doing right now.

    python status.py            # write status.html once
    python status.py --watch    # rewrite it every 20 s

The page carries its own meta-refresh, so leaving it open in a browser next to
a --watch loop gives a live dashboard without any server.

Files are read from the current directory (the root-level status.py wrapper
changes into its own directory first, so it works from anywhere). Wording such
as what a locked item is called comes from the adapter's labels.
"""
import argparse
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from playcap import config

REFRESH_SECONDS = 20
CFG, ADAPTER = {}, None
HERE = LIB = ARCHIVE = OUT = None


def _setup():
    global CFG, ADAPTER, HERE, LIB, ARCHIVE, OUT
    if ADAPTER is None:
        HERE = Path.cwd()
        CFG, ADAPTER = config.load(HERE / "config.json")
        LIB = Path(CFG["output_dir"])
        ARCHIVE = LIB.parent / (LIB.name + "_originals")
        OUT = HERE / "status.html"


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def tail_progress(path):
    """Last ffmpeg -stats line, which uses \\r rather than newlines."""
    try:
        blob = Path(path).read_bytes()[-8000:].decode("utf-8", "replace")
    except OSError:
        return None
    lines = [ln for ln in blob.replace("\r", "\n").split("\n") if "time=" in ln]
    if not lines:
        return None
    last = lines[-1]
    got = {}
    for key in ("time", "bitrate", "speed", "fps"):
        m = re.search(rf"{key}=\s*([^\s]+)", last)
        if m:
            got[key] = m.group(1)
    return got


def running(names):
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower()
    except OSError:
        return {n: False for n in names}
    return {n: (n.lower() + ".exe") in out for n in names}


def hms(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def gather():
    _setup()
    progress = read_json(HERE / CFG["progress_file"], {})
    lectures = [ADAPTER.item(r) for r in read_json(HERE / CFG["queue_file"], [])]
    recorded = {k for k, v in progress.items() if v.get("status") == "done"}
    skipped = {k for k, v in progress.items() if v.get("status") == "skipped"}
    failed = {k for k, v in progress.items() if v.get("status") == "failed"}

    locked, pending, aired = [], [], 0
    now = datetime.now()
    for it in lectures:
        sid = str(it.id)
        if sid in recorded or sid in skipped:
            continue
        if it.locked:
            locked.append(it)
            continue
        when = it.aired_at or now        # no date known: treat as aired
        if when <= now:
            pending.append(it)
            aired += 1

    # Optimization: trust the disk, not the state file.
    episodes = []
    for mp4 in sorted(LIB.rglob("*.mp4")):
        episodes.append({"name": mp4.stem, "state": "optimized",
                         "gb": mp4.stat().st_size / 1024 ** 3})
    for mkv in sorted(LIB.rglob("*.mkv")):
        if "_partial" in mkv.parts:
            continue
        episodes.append({"name": mkv.stem, "state": "original",
                         "gb": mkv.stat().st_size / 1024 ** 3})
    for ep in episodes:
        src = ARCHIVE.rglob(ep["name"] + ".mkv")
        ep["before"] = next((p.stat().st_size / 1024 ** 3 for p in src), None)
    episodes.sort(key=lambda e: e["name"])

    archive_gb = sum(p.stat().st_size for p in ARCHIVE.rglob("*.mkv")) / 1024 ** 3 \
        if ARCHIVE.exists() else 0.0
    partials = [p.stem for p in LIB.rglob("*.optpart")]

    return {
        "recorded": len(recorded), "skipped": len(skipped), "failed": len(failed),
        "pending": pending, "locked": locked, "total_scheduled": len(lectures),
        "not_aired": len(lectures) - len(recorded) - len(skipped)
                     - len(locked) - len(pending),
        "episodes": episodes, "archive_gb": archive_gb, "partials": partials,
        "encode": tail_progress(HERE / "optimize.err"),
        "procs": running(["ffmpeg", "obs64", "chrome", "python"]),
        "generated": now.strftime("%a %d %b %Y, %H:%M:%S"),
    }


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--dim:#6b7280;--line:#e5e7eb;
--good:#128a5c;--warn:#b45309;--cold:#4b5563;--accent:#2563eb;--bar:#e9ecf1}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--ink:#e8eaed;
--dim:#9aa1ab;--line:#252a33;--good:#35c48c;--warn:#e3a008;
--cold:#9aa1ab;--accent:#60a5fa;--bar:#232833}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:13px;margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .n{font-size:26px;font-weight:600;letter-spacing:-.02em}
.card .l{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);
margin:26px 0 10px;font-weight:600}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--dim);padding:9px 12px;border-bottom:1px solid var(--line);font-weight:600}
td{padding:9px 12px;border-bottom:1px solid var(--line);font-size:14px}
tr:last-child td{border-bottom:none}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;font-weight:600}
.p-ok{background:color-mix(in srgb,var(--good) 16%,transparent);color:var(--good)}
.p-wait{background:color-mix(in srgb,var(--cold) 16%,transparent);color:var(--cold)}
.p-run{background:color-mix(in srgb,var(--accent) 16%,transparent);color:var(--accent)}
.p-lock{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.bar{height:7px;background:var(--bar);border-radius:99px;overflow:hidden;margin-top:8px}
.bar i{display:block;height:100%;background:var(--good);border-radius:99px}
.live{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:10px;padding:14px 16px;margin-bottom:6px}
.live .t{font-size:13px;color:var(--dim);margin-bottom:4px}
.mono{font-family:ui-monospace,"Cascadia Code",Consolas,monospace;font-size:13px}
.idle{border-left-color:var(--cold)}
.note{color:var(--dim);font-size:12.5px;margin-top:8px}
.trunc{max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render(d):
    _setup()
    L = ADAPTER.labels
    eps = d["episodes"]
    opt = [e for e in eps if e["state"] == "optimized"]
    orig = [e for e in eps if e["state"] == "original"]
    after = sum(e["gb"] for e in opt)
    before = sum(e["before"] or e["gb"] for e in opt)
    pct = round(100 * len(opt) / len(eps)) if eps else 0

    enc = d["encode"]
    encoding = d["procs"].get("ffmpeg")
    recording = d["procs"].get("obs64")

    rows = []
    for e in eps:
        was = f"{e['before']:.2f}" if e["before"] else "&mdash;"
        ratio = f"{e['before'] / e['gb']:.1f}x" if e["before"] else ""
        done = e["state"] == "optimized"
        rows.append(
            f"<tr><td class='trunc'>{esc(e['name'])}</td>"
            f"<td class='num'>{was}</td>"
            f"<td class='num'>{e['gb']:.2f}</td>"
            f"<td class='num'>{ratio}</td>"
            f"<td><span class='pill {'p-ok' if done else 'p-wait'}'>"
            f"{'optimized' if done else 'original'}</span></td></tr>")
    rows_opt = "".join(rows)

    rows_pend = "".join(
        f"<tr><td class='num'>{esc(i.day or '')}</td>"
        f"<td class='trunc'>{esc(i.title or '')[:90]}</td>"
        f"<td><span class='pill p-wait'>queued</span></td></tr>"
        for i in d["pending"]) or "<tr><td colspan=3 style='color:var(--dim)'>nothing waiting</td></tr>"

    rows_lock = "".join(
        f"<tr><td class='num'>{esc(i.day or '')}</td>"
        f"<td class='trunc'>{esc(i.title or '')[:90]}</td>"
        f"<td><span class='pill p-lock'>{esc(L['locked_pill'])}</span></td></tr>"
        for i in d["locked"])

    live_enc = (
        f"<div class='live'><div class='t'>Optimizing now</div>"
        f"<div class='mono'>at {enc.get('time','?')} &middot; {enc.get('bitrate','?')} "
        f"&middot; {enc.get('speed','?')} realtime</div></div>"
        if encoding and enc else
        "<div class='live idle'><div class='t'>Optimizer</div>"
        "<div class='mono'>not running</div></div>")

    live_rec = (
        "<div class='live'><div class='t'>Recording now</div>"
        "<div class='mono'>OBS is running</div></div>" if recording else
        "<div class='live idle'><div class='t'>Recorder</div>"
        "<div class='mono'>OBS not running</div></div>")

    warn = ""
    if encoding and recording:
        warn = ("<div class='note' style='color:var(--warn)'>Both jobs are running. "
                "They compete for the CPU and OBS may drop frames.</div>")
    if d["partials"]:
        warn += ("<div class='note'>Unfinished encode left behind: "
                 + ", ".join(esc(p) for p in d["partials"]) + "</div>")

    return f"""<title>{esc(L['page_title'])}</title>
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style>
<div class="wrap">
<h1>{esc(L['status_title'])}</h1>
<div class="sub">{esc(d['generated'])} &middot; refreshes every {REFRESH_SECONDS}s</div>

<div class="cards">
  <div class="card"><div class="n">{d['recorded']}</div><div class="l">recorded</div></div>
  <div class="card"><div class="n">{len(d['pending'])}</div><div class="l">to record</div></div>
  <div class="card"><div class="n">{len(d['locked'])}</div><div class="l">{esc(L['locked_pill'])}</div></div>
  <div class="card"><div class="n">{d['not_aired']}</div><div class="l">not aired</div></div>
  <div class="card"><div class="n">{len(opt)}/{len(eps)}</div><div class="l">optimized</div></div>
</div>

{live_rec}
{live_enc}
{warn}

<h2>Optimization &mdash; {before:.1f} GB to {after:.1f} GB ({before - after:.1f} GB reclaimed)</h2>
<div class="bar"><i style="width:{pct}%"></i></div>
<div class="note">{len(opt)} done, {len(orig)} still original.
Archived originals: {d['archive_gb']:.1f} GB in {esc(ARCHIVE)}</div>
<table style="margin-top:12px">
<tr><th>Episode</th><th class="num">Was</th><th class="num">Now (GB)</th>
<th class="num">Saved</th><th>State</th></tr>
{rows_opt}
</table>

<h2>Waiting to record ({len(d['pending'])})</h2>
<table><tr><th class="num">Aired</th><th>{esc(L['item'])}</th><th>State</th></tr>{rows_pend}</table>

<h2>{esc(L['locked_heading'])} ({len(d['locked'])}) &mdash; {esc(L['locked_heading_hint'])}</h2>
<table><tr><th class="num">Aired</th><th>{esc(L['item'])}</th><th>State</th></tr>{rows_lock}</table>
</div>"""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args(argv)
    _setup()
    while True:
        OUT.write_text(render(gather()), encoding="utf-8")
        if not args.watch:
            print(f"wrote {OUT}")
            return
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
