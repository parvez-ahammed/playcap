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

The "links" pseudo-key belongs to the generic adapter: the pasted lines are
written to queue.txt next to config.json and queue_source points at it.
"""
import json
import os
import tempfile
from pathlib import Path

from playcap import adapters, record_quality

CONFIG_NAME = "config.json"
LINKS_FILE = "queue.txt"
GENERIC = "playcap.adapters.html5_video"
TOOL_KEYS = ("chrome_exe", "obs_exe", "ffmpeg", "ffprobe", "encode_ffmpeg")


def atomic_write_json(path, obj):
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def atomic_write_text(path, text):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read(root):
    try:
        data = json.loads((Path(root) / CONFIG_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


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


def _tool_ok(value):
    if Path(value).is_file():
        return True
    from shutil import which
    return "/" not in value and "\\" not in value and bool(which(value))


def _writable_dir(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".playcap-write-test"
        probe.write_text("")
        probe.unlink()
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
    if "links" in partial and not _clean_links(partial["links"]):
        errors["links"] = "Add at least one page URL."
    errors.update(record_quality.validate(partial))
    errors.update(_validate_layout(partial))
    return errors


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


def save(root, partial):
    root = Path(root)
    partial = dict(partial)
    errors = validate(partial, root)
    current = read(root)
    if errors:
        return current, errors
    links = partial.pop("links", None)
    if links is not None:
        atomic_write_text(root / LINKS_FILE, "\n".join(_clean_links(links)) + "\n")
        partial["queue_source"] = LINKS_FILE
    for key in TOOL_KEYS:
        if key in partial and not str(partial[key] or "").strip():
            partial.pop(key)                    # blank = keep auto-detection
    for key in ("record_crf", "video_bitrate_kbps", "keyframe_seconds"):
        if key in partial:
            partial[key] = int(partial[key])
    merged = {**current, **partial}
    atomic_write_json(root / CONFIG_NAME, merged)
    return merged, {}
