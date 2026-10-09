"""Read, validate and save config.json for the setup UI.

config.py loads the config for the pipeline stages; it exits when the file is
missing and caches per process, which is right for a one-shot job and wrong for
a long-running UI that has to work before the file exists and see every save.
This module is the UI's side: plain reads, validation, atomic writes.

    read(root)                  current config.json, or {} if missing/corrupt
    save(root, partial)         validate + merge + write -> (cfg, errors)
    adapters_available()        sources the wizard can offer
    read_links(root, cfg)       the "List of links" text, for re-editing

save() never writes when any value is invalid, and it keeps every key it was
not given -- a hand-written config.json with extra keys survives the UI.
Writes go to a temp file in the same directory and are swapped in with
os.replace, so a crash mid-save leaves the old file, never half of a new one.

Library-layout keys (library_layout, name_template, write_nfo) are checked
against playcap.organize: a custom template must render and include {n} or
{id}.

Recording-quality keys (record_mode, record_crf, video_bitrate_kbps,
x264_preset, keyframe_seconds) are checked by record_quality.validate and
stored as numbers; they reach OBS the next time playcap starts it.

Capture keys (capture_backend, capture_grabber, capture_encoder,
capture_audio, capture_fps) are checked by capture.validate.
Notification, media-server and schedule keys are checked by notify.validate
and schedule.validate. Secrets among them (notify.SECRET_KEYS: webhook URLs,
the ntfy topic, API tokens) are never sent back to the page, so the page sends
"" to mean "keep the saved one" and null to mean "remove it".

The "links" pseudo-key belongs to the generic adapter: the pasted lines are
written to queue.txt next to config.json and queue_source points at it.

The "recipes" list in config.json is not a UI_KEYS setting: playcap.recipes
writes it through its own validated read-modify-write, and save() keeps it
like any other key it was not given.
"""
import json
import os
import tempfile
import time
from pathlib import Path

from playcap import adapters, capture, record_quality

CONFIG_NAME = "config.json"
LINKS_FILE = "queue.txt"
GENERIC = "playcap.adapters.html5_video"
REPLACE_TRIES = 20          # x 0.25 s: how long a save waits out a brief Windows file lock
TOOL_KEYS = ("chrome_exe", "obs_exe", "ffmpeg", "ffprobe", "encode_ffmpeg")


def atomic_write_json(path, obj):
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def atomic_write_text(path, text):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # Windows refuses the replace while anyone has the target open (the
        # UI polling it, an indexer, antivirus). Those holds last moments, and
        # failing here would lose the progress of an item that just finished.
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(0.25)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read(root):
    try:
        data = json.loads((Path(root) / CONFIG_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class Unreadable(Exception):
    """A file exists but cannot be read as the JSON object it should hold."""


def read_strict(path, default):
    """For read-modify-write: the default only when the file is missing.
    A corrupt or locked file raises, so a save never replaces a file it could
    not read with a fresh one holding only the new keys."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unreadable(f"{path.name} cannot be read ({exc}); fix or move it first") from exc
    if not isinstance(data, type(default)):
        raise Unreadable(f"{path.name} does not hold a JSON {type(default).__name__}")
    return data


def is_configured(cfg):
    """An output folder is the one thing nothing can default. The adapter may
    be left out: like config.load(), it then falls back to local.ADAPTER or
    the generic adapter (effective_adapter)."""
    return bool(cfg.get("output_dir"))


def effective_adapter(cfg):
    from playcap import config   # deferred: config imports detect, not settings
    return cfg.get("adapter") or config.default_adapter_path()


def adapters_available():
    """The generic adapter, plus a private one named by an importable `local`
    package. Each entry: {module, label, fields}."""
    modules = [GENERIC]
    try:
        import local  # optional, untracked
        extra = getattr(local, "ADAPTER", None)
        if extra and extra not in modules:
            modules.append(extra)
    except ImportError:
        pass
    out = []
    for mod in modules:
        try:
            a = adapters.load(mod)
        except Exception:
            continue
        out.append({"module": mod, "label": a.label, "fields": list(a.setup_fields)})
    return out


def _clean_links(text):
    lines = [ln.strip() for ln in str(text).splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def read_links(root, cfg):
    src = Path(root) / cfg.get("queue_source", LINKS_FILE)
    try:
        return src.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _link_ok(line, root):
    """A web address, a file:// URL, or a file that exists next to the list.
    "www.site.com/talk" without a scheme would otherwise turn into a local path
    and fail much later with a confusing error. "collect: URL" (an index page a
    recipe reads; see html5_video) must be a web address."""
    if line.lower().startswith("collect:"):
        line = line[len("collect:"):].strip()
        return "://" in line and line.split("://", 1)[0].lower() in ("http", "https", "file")
    if "://" in line:
        return line.split("://", 1)[0].lower() in ("http", "https", "file")
    p = Path(line)
    return (p if p.is_absolute() else Path(root) / p).is_file()


def _tool_ok(value):
    if Path(value).is_file():
        return True
    from shutil import which
    return "/" not in value and "\\" not in value and bool(which(value))


def _writable_dir(path):
    """Can this folder be created and written? Checked on the nearest folder
    that exists, so a save that is then refused leaves nothing behind."""
    path = Path(path).resolve()
    while not path.exists():
        if path.parent == path:
            return False
        path = path.parent
    if not path.is_dir():
        return False
    try:
        fd, probe = tempfile.mkstemp(dir=path, prefix=".playcap-write-test")
        os.close(fd)
        os.unlink(probe)
        return True
    except OSError:
        return False


def validate(partial, root):
    errors = {}
    if "show" in partial and not str(partial["show"]).strip():
        errors["show"] = "Give the library a name."
    if "output_dir" in partial:
        out = Path(str(partial["output_dir"]).strip() or ".")
        if not out.is_absolute():
            out = Path(root) / out
        if not str(partial["output_dir"]).strip():
            errors["output_dir"] = "Pick a folder for the recordings."
        elif not _writable_dir(out):
            errors["output_dir"] = f"Cannot create or write to {out}."
    for key in TOOL_KEYS:
        value = str(partial.get(key) or "").strip()
        if value and not _tool_ok(value):
            errors[key] = f"Not found: {value}"
    if "obs_ws_url" in partial and not str(partial["obs_ws_url"]).startswith(("ws://", "wss://")):
        errors["obs_ws_url"] = "Should look like ws://127.0.0.1:4455"
    if "adapter" in partial:
        if partial["adapter"] not in {a["module"] for a in adapters_available()}:
            errors["adapter"] = "Unknown source."
    if "links" in partial:
        links = _clean_links(partial["links"])
        if not links:
            errors["links"] = "Add at least one page URL."
        bad = [ln for ln in links if not _link_ok(ln.rsplit(" | ", 1)[-1].strip(), root)]
        if bad:
            errors["links"] = (f"Not a web address or a file here: {bad[0]} "
                               "(web pages start with https://)")
    for key, lo, hi in (("season", 0, 9999), ("title_max_len", 0, 240),
                        ("avg_item_minutes", 1, 24 * 60),
                        ("chrome_debug_port", 1024, 65535)):
        if key in partial:
            try:
                ok = lo <= int(partial[key]) <= hi and not isinstance(partial[key], bool)
            except (TypeError, ValueError):
                ok = False
            if not ok:
                errors[key] = f"A whole number from {lo} to {hi}."
    errors.update(record_quality.validate(partial))
    errors.update(capture.validate(partial))
    errors.update(_validate_layout(partial))
    errors.update(_validate_automation(partial))
    return errors


def _validate_automation(partial):
    from playcap import notify, schedule   # deferred: schedule imports jobs, which imports this
    return {**notify.validate(partial), **schedule.validate(partial)}


def automation_keys():
    from playcap import notify, schedule
    return notify.UI_KEYS | schedule.UI_KEYS


LAYOUTS = ("folder", "media_server", "custom")


def _validate_layout(partial):
    from playcap import organize
    errors = {}
    layout = partial.get("library_layout")
    if layout is not None and layout not in LAYOUTS:
        errors["library_layout"] = "Pick folder, media server or custom."
    if layout == "custom" or (layout is None and partial.get("name_template")):
        msg = organize.check_template(str(partial.get("name_template") or ""))
        if not str(partial.get("name_template") or "").strip():
            msg = "Write a name template, e.g. {show}/{date} - {title} ({n})"
        if msg:
            errors["name_template"] = msg
    if "write_nfo" in partial and partial["write_nfo"] not in (True, False, None):
        errors["write_nfo"] = "Should be on, off or automatic."
    return errors


# Keys the UI may write. Anything else in a request is refused, so the page
# cannot point the pipeline's files somewhere else (queue_source, progress_file
# and friends are set by playcap itself, relative to the folder).
UI_KEYS = {"adapter", "links", "show", "output_dir", "obs_ws_url", "obs_password",
           "chrome_debug_port", "library_layout", "name_template", "write_nfo",
           "season", "title_max_len", "avg_item_minutes", *TOOL_KEYS,
           *record_quality.DEFAULTS, *capture.DEFAULTS}


def _adapter_keys(partial, current):
    try:
        a = adapters.load(partial.get("adapter") or effective_adapter(current))
    except Exception:
        return set()
    return {f.get("key") for f in a.setup_fields if isinstance(f, dict)}


def _apply_secrets(partial, current, merged):
    """Secrets are never shown to the page: "" keeps the saved value, and
    null removes a key (any automation key, secret or not)."""
    from playcap import notify
    for key in automation_keys() & set(partial):
        if partial[key] is None:
            merged.pop(key, None)
        elif key in notify.SECRET_KEYS and partial[key] == "":
            if key in current:
                merged[key] = current[key]
            else:
                merged.pop(key, None)


def save(root, partial):
    root = Path(root)
    partial = dict(partial)
    try:
        current = read_strict(root / CONFIG_NAME, {})
    except Unreadable as exc:
        return read(root), {"_file": str(exc)}
    unknown = set(partial) - UI_KEYS - automation_keys() - _adapter_keys(partial, current)
    if unknown:
        return current, {k: "Not a setting the UI can change." for k in sorted(unknown)}
    errors = validate(partial, root)
    if "name_template" not in partial and partial.get("library_layout") == "custom"             and current.get("name_template"):
        errors.pop("name_template", None)      # keeps the template it already has
    if errors:
        return current, errors
    links = partial.pop("links", None)
    if links is not None:
        atomic_write_text(root / LINKS_FILE, "\n".join(_clean_links(links)) + "\n")
        partial["queue_source"] = LINKS_FILE
    for key in TOOL_KEYS:
        if key in partial and not str(partial[key] or "").strip():
            partial.pop(key)                    # blank = keep auto-detection
    for key in ("record_crf", "video_bitrate_kbps", "keyframe_seconds", "season", "capture_fps",
                "title_max_len", "avg_item_minutes", "chrome_debug_port"):
        if key in partial:
            partial[key] = int(partial[key])
    merged = {**current, **partial}
    _apply_secrets(partial, current, merged)
    for key in ("library_refresh_minutes", "schedule_every_hours"):
        if key in merged and key in partial:
            merged[key] = int(merged[key])
    if partial.get("output_dir"):        # every check passed: now make the folder
        out = Path(str(partial["output_dir"]).strip())
        try:
            (out if out.is_absolute() else root / out).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return current, {"output_dir": f"Cannot create {out}: {exc}"}
    atomic_write_json(root / CONFIG_NAME, merged)
    return merged, {}
