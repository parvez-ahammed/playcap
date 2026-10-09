"""What the last run of each job did, in one plain sentence each.

    summarize(root) -> [{"job", "text", "level": "ok" | "bad" | "info", "when"}]

The UI's raw logs are for debugging; a person wants "23 recorded, 6 failed
because the browser closed". This reads the tail of each job's log (the same
files jobs.tail shows) and matches the lines the jobs themselves print. It is
deliberately a reader of existing output, not a second status channel: when
a job's wording changes, the fallback is the log's last line, never a crash.

Logs are appended to, so each reader looks only at the latest run: from the
last run marker jobs.start writes (RUN_MARK), or for a smoke test started on
the command line, from its own "[1] " step. A traceback counts only inside
that run, and the error reported is the exception line, not whatever the log
printed last.

Failure reasons go through state.friendly_error, so they read the same here
as on the queue rows. Jobs that never ran (no log file) are left out.
"""
import re
from collections import Counter
from pathlib import Path

from playcap import jobs

TAIL_BYTES = 8000


def _lines(root, name):
    path = Path(root) / jobs.LOGS[name][0]
    try:
        blob = path.read_bytes()[-TAIL_BYTES:].decode("utf-8", "replace")
        when = path.stat().st_mtime
    except OSError:
        return None, None
    lines = [ln.strip() for ln in blob.replace("\r", "\n").splitlines() if ln.strip()]
    marks = [i for i, ln in enumerate(lines) if ln.startswith(jobs.RUN_MARK)]
    if marks:
        lines = lines[marks[-1] + 1:]
    return lines, when


EXC_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt)\b")


def _crashed(lines):
    """The exception line of the last traceback in this run, if any."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("Traceback (most recent call last)"):
            for ln in lines[i + 1:]:
                if EXC_LINE.match(ln):
                    return ln
            return lines[-1]
    return None


def _last(lines, *needles):
    for ln in reversed(lines):
        if any(n in ln for n in needles):
            return ln
    return None


def _record(lines):
    from playcap.state import friendly_error
    # Only the latest run: it starts with the "N to record | ..." plan line.
    starts = [i for i, ln in enumerate(lines) if " to record | " in ln]
    lines = lines[starts[-1]:] if starts else lines
    end = _last(lines, " recorded, ")
    m = end and re.match(r"(\d+) recorded, (\d+) failed", end)
    stopped = _last(lines, "stop requested", "interrupted --")
    if m:
        done, failed = int(m.group(1)), int(m.group(2))
        text = f"In total: {done} recorded, {failed} failed."
        if failed:
            # The summary lists each failed item as "FAILED title: error".
            tail = lines[len(lines) - lines[::-1].index(end):]
            why = Counter(friendly_error(ln.split(": ", 1)[1]) for ln in tail
                          if ln.startswith("FAILED") and ": " in ln)
            if why:
                reason, n = why.most_common(1)[0]
                text += f" Most common reason ({n}): {reason}"
        if stopped:
            text = ("Stopped by you. " if "stop requested" in stopped
                    else "Stopped part way; the item in progress was discarded. ") + text
        return text, "bad" if failed else "ok"
    crash = _crashed(lines)
    if crash:
        return f"Recording stopped with an error: {crash[:160]}", "bad"
    if _last(lines, "run: python build_queue.py"):
        return "There was no queue to record. Load the queue first.", "bad"
    if _last(lines, "failures in a row -- pausing"):
        return "Several items failed in a row, so recording paused for a while before carrying on.", "info"
    current = _last(lines, "=== [")
    if current:
        return "Recording: " + current.split("] ", 2)[-1][:100], "info"
    return None


def _test(lines):
    from playcap.state import friendly_error
    starts = [i for i, ln in enumerate(lines) if ln.startswith("[1] ")]
    lines = lines[starts[-1]:] if starts else lines
    luma = _last(lines, "mean luma =")
    if luma and "BLACK" in luma:
        return ("Test: the recording came out black. Either OBS captures the wrong screen "
                "(Settings → Set up recording scene) or the site hides its video from "
                "screen capture."), "bad"
    if luma:
        return "Test passed: the 25 s recording shows a real picture.", "ok"
    failed = _last(lines, "*** FAILED:")
    if failed:
        return "Test failed: " + friendly_error(failed.split("FAILED:", 1)[1].strip()), "bad"
    crash = _crashed(lines)
    if crash:
        return f"Test stopped with an error: {crash[:160]}", "bad"
    return None


def _queue(lines):
    m = None
    for ln in reversed(lines):
        m = re.match(r"(\d+) items -> .*?(\d+) locked\)", ln)
        if m:
            break
    if m:
        n, locked = int(m.group(1)), int(m.group(2))
        return (f"Queue loaded: {n} item{'s' if n != 1 else ''}"
                + (f", {locked} locked." if locked else ".")), "ok" if n else "info"
    if _last(lines, "The source returned no items"):
        return ("The source listed no items, so the old queue was kept. "
                "Check you are logged in and the list page shows items."), "bad"
    missing = _last(lines, "Queue source not found")
    if missing:
        return "The list of links could not be found. Open Settings and add your links.", "bad"
    crash = _crashed(lines)
    if crash:
        return f"Loading the queue failed: {crash[:160]}", "bad"
    return None


def _optimize(lines):
    m = None
    for ln in reversed(lines):
        m = re.match(r"([\d.]+) GB -> ([\d.]+) GB \(([\d.]+) GB reclaimed\)", ln)
        if m or ln.startswith("stopped --"):
            break
    if m:
        return f"Re-compressing finished: {m.group(1)} GB → {m.group(2)} GB ({m.group(3)} GB saved).", "ok"
    if _last(lines, "stopped --"):
        return "Re-compressing was stopped. The file in progress was left as it was; run it again to continue.", "info"
    crash = _crashed(lines)
    if crash:
        return f"Re-compressing stopped with an error: {crash[:160]}", "bad"
    for ln in reversed(lines):
        cur = re.match(r"\[(\d+)/(\d+)\]", ln)
        if cur:
            return f"Re-compressing file {cur.group(1)} of {cur.group(2)}.", "info"
    return None


def _browser(lines):
    if _last(lines, "debug Chrome up"):
        return "The playcap browser started.", "ok"
    crash = _crashed(lines)
    if crash:
        return f"The browser did not start: {crash[:160]}", "bad"
    return None


READERS = [("record", _record), ("test", _test), ("queue", _queue),
           ("optimize", _optimize), ("browser", _browser)]


def summarize(root):
    out = []
    for name, reader in READERS:
        lines, when = _lines(root, name)
        if not lines:
            continue
        try:
            got = reader(lines)
        except Exception:
            got = None
        text, level = got or (lines[-1][:160], "info")
        out.append({"job": name, "text": text, "level": level, "when": when})
    return out
