"""Where each recording goes and what it is called.

A recording is filed by a name template, relative to output_dir. Three layouts:

    folder        (default)  {show}/{n:02} - {title}
                             My Recordings/03 - Product webinar.mp4
    media_server             {show}/Season {season:02}/S{season:02}E{n:02} - {title}
                             My Recordings/Season 01/S01E03 - Product webinar.mp4
                             + an .nfo beside each file and a tvshow.nfo, so media
                             servers (Jellyfin, Emby, Plex, Kodi) show real titles
                             and dates instead of the filename
    custom                   config "name_template", same placeholders

Placeholders: {show} {season} {n} (queue position, also {episode}) {title}
{date} (YYYY-MM-DD, empty when unknown) {time} (HH-MM) {kind} {id}. Format
specs work: {n:03}. "/" makes folders. Every folder and file name is made safe
for Windows, and parts left empty by a missing {date} lose their dangling
separators, so a template never produces "/ - Title".

Numbers are queue positions, so a re-record lands on the same name. .nfo files
are written for media_server unless "write_nfo" says otherwise; any layout can
turn them on. Show name, season, and the title clean-up come from config
("show", "season", "title_cleanup", "title_collapse_restated",
"title_max_len", "show_plot", "show_premiered") -- a site adapter usually
supplies them as defaults.

Moving never overwrites. A destination that is already taken (an earlier
take, another item rendering to the same name) is reported and left alone;
moves that free a name for another run first, so a renumbered queue still
files cleanly. progress.json is saved after every move, so a crash mid-way
loses nothing, and organize refuses to run during a recording: the recorder
rewrites progress.json from memory after every item. Renaming a file that
optimize.py made also moves its optimize.json entry and its archived
original, so optimize never mistakes it for a new raw recording.

Usage:  python -m playcap.organize --dry-run     # show the plan
        python -m playcap.organize               # move files (and write .nfo files)
"""
import argparse
import json
import re
import shutil
from pathlib import Path
from xml.sax.saxutils import escape

from playcap import config, jobs
from playcap.settings import atomic_write_json

LAYOUTS = {
    "folder": "{show}/{n:02} - {title}",
    "media_server": "{show}/Season {season:02}/S{season:02}E{n:02} - {title}",
}
PLACEHOLDERS = ("show", "season", "n", "episode", "title", "date", "time", "kind", "id")


def layout(cfg):
    return cfg.get("library_layout") or "folder"


def name_template(cfg):
    if layout(cfg) == "custom":
        return cfg.get("name_template") or LAYOUTS["folder"]
    return LAYOUTS.get(layout(cfg), LAYOUTS["folder"])


def write_nfo(cfg):
    if cfg.get("write_nfo") is not None:
        return bool(cfg["write_nfo"])
    return layout(cfg) == "media_server"


def fields(cfg, index, item, title):
    when = getattr(item, "aired_at", None)
    return {"show": cfg.get("show") or "Recordings", "season": int(cfg.get("season") or 1),
            "n": index, "episode": index, "title": title,
            "date": when.strftime("%Y-%m-%d") if when else "",
            "time": when.strftime("%H-%M") if when else "",
            "kind": getattr(item, "kind", "") or "", "id": getattr(item, "id", "") or ""}


RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)
PART_MAX = 120          # per folder/file name; keeps whole paths well under 260


def _value(v):
    """A field value is text inside one name: a "/" in a title must not make
    a folder, and control characters have no place in a file name."""
    if not isinstance(v, str):
        return v
    return re.sub(r"[\x00-\x1f\x7f]", " ", v).replace("/", "-").replace("\\", "-")


def _part(part):
    part = safe(part)
    while True:            # strip dangling separators and Windows-illegal trailing dots
        new = re.sub(r"^[\s\-_.,]+|[\s\-_,.]+$", "", part)
        if new == part:
            break
        part = new
    if len(part) > PART_MAX:
        part = part[:PART_MAX].rstrip(" .-_,")
    if RESERVED.match(part):
        part += "_"
    return part


def _render(template, values):
    out = template.format(**{k: _value(v) for k, v in values.items()})
    parts = []
    for part in re.split(r"[\\/]", out):
        part = _part(part)
        if part and part not in (".", ".."):
            parts.append(part)
    if not parts:
        raise ValueError("the template produced an empty name")
    return Path(*parts)


def relpath(cfg, index, item, title):
    """Path of the recording under output_dir, without the file extension."""
    return _render(name_template(cfg), fields(cfg, index, item, title))


def check_template(template):
    """-> error message, or None when the template renders."""
    sample = {"show": "Show", "season": 1, "n": 3, "episode": 3, "title": "Title",
              "date": "2026-01-31", "time": "14-00", "kind": "video", "id": "abc123"}
    try:
        _render(template, sample)
        _render(template, {**sample, "date": "", "time": ""})
    except KeyError as exc:
        return f"Unknown placeholder {{{exc.args[0]}}}. Use: " + " ".join("{%s}" % p for p in PLACEHOLDERS)
    except (ValueError, IndexError) as exc:
        return f"Template does not work: {exc}"
    if "{n" not in template and "{episode" not in template and "{id" not in template:
        return "Include {n} (or {id}) so two items cannot get the same name."
    return None


def show_root(cfg):
    """The top folder of the library: the template's first folder when it
    has one (normally {show}), else output_dir itself."""
    rel = _render(name_template(cfg), fields(cfg, 1, None, "x"))
    out = Path(cfg["output_dir"])
    return out / rel.parts[0] if len(rel.parts) > 1 else out


def show_dir(cfg):
    """Kept for callers that still want the media-server season folder."""
    return Path(cfg["output_dir"]) / cfg["show"] / f"Season {int(cfg['season']):02d}"


def partial_dir(cfg):
    return Path(cfg["output_dir"]) / "_partial"      # kept, but out of the library's way


def episode_label(cfg, index):
    return f"S{int(cfg['season']):02d}E{index:02d}"


def episode_stem(cfg, index, title):
    """Media-server stem. safe() runs on the stem, so a title truncated to
    "..." loses its dots before the extension is added."""
    return safe(f"{episode_label(cfg, index)} - {title}")


def _key(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def clean_title(raw, cfg):
    s = re.sub(r"\s+", " ", raw).strip()
    for pat in cfg.get("title_cleanup") or []:
        s = re.sub(pat, "", s, flags=re.I).strip()
    if cfg.get("title_collapse_restated"):
        # "Topic- I Topic: I" -> "Topic- I": some pages glue a restatement of
        # the topic onto the title with different punctuation and word breaks
        # ("Disease-II" vs "Disease II"), so compare letters and digits only
        # and let the restatement span a different number of words. Matching
        # on whole words both times is what stops "Topic - I" from swallowing
        # the "+ Topic - II" half of a combined title.
        words = s.split()
        for k in range(1, len(words)):          # shortest topic the rest restates
            head = _key(" ".join(words[:k]))
            if not head:
                continue
            acc = ""
            for w in words[k:]:
                acc += _key(w)
                if len(acc) >= len(head):
                    break
            if acc == head:
                s = " ".join(words[:k])
                break
    s = s.strip(" -:,(")
    limit = int(cfg.get("title_max_len") or 0)
    if limit and len(s) > limit:                # the .nfo keeps the full text
        s = s[:limit].rsplit(" ", 1)[0] + "..."
    return s


def safe(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).rstrip(". ")


def aired(item):
    return item.aired_at.strftime("%Y-%m-%d") if item.aired_at else None


def episode_nfo(item, ep, title, cfg):
    date = aired(item)
    plot = escape(re.sub(r"\s+", " ", item.title).strip())
    rows = [f"  <title>{escape(title)}</title>",
            f"  <season>{int(cfg['season'])}</season>", f"  <episode>{ep}</episode>",
            f"  <showtitle>{escape(cfg['show'])}</showtitle>",
            f"  <plot>{plot}</plot>"]
    if date:
        rows.insert(3, f"  <aired>{date}</aired>")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<episodedetails>\n'
            + "\n".join(rows) + "\n</episodedetails>\n")


def show_nfo(cfg):
    rows = [f"  <title>{escape(cfg['show'])}</title>"]
    if cfg.get("show_plot"):
        rows.append(f"  <plot>{escape(cfg['show_plot'])}</plot>")
    if cfg.get("show_premiered"):
        rows.append(f"  <premiered>{escape(cfg['show_premiered'])}</premiered>")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<tvshow>\n'
            + "\n".join(rows) + "\n</tvshow>\n")


def _same_file(a, b):
    try:
        return a.resolve() == b.resolve() or (b.exists() and a.samefile(b))
    except OSError:
        return False


def _find(src, outdir):
    """Where a recording is now: its recorded path, the same name in the
    library (it moved drives), or either as the .mp4 optimize made of it."""
    for p in (src, outdir / src.name):
        for cand in (p, p.with_suffix(".mp4")):
            if cand.exists():
                return cand
    return None


def _follow_optimize(cfg, src, dst):
    """optimize.py tracks its outputs and archived originals by path. When a
    file it made is renamed here, take its entry and its original along --
    otherwise the renamed output looks like a new raw recording and the
    original at the old name gets encoded into a duplicate episode."""
    path = Path(cfg.get("optimize_state") or "optimize.json")
    if not path.exists():
        return
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"     (optimize.json unreadable, not updated: {exc})")
        return
    lib = Path(cfg["output_dir"]).resolve()
    archive = lib.parent / (lib.name + "_originals")
    old, new = src.resolve(), dst.resolve()
    try:
        new_rel = new.relative_to(lib)
    except ValueError:
        return
    changed = False
    for key, entry in list(state.items()):
        if not isinstance(entry, dict) or not entry.get("file") \
                or Path(entry["file"]).resolve() != old:
            continue
        entry["file"] = str(dst)
        orig = Path(entry["original"]) if entry.get("original") else None
        if orig and orig.exists():
            target = archive / new_rel.with_suffix(orig.suffix)
            if target.exists():
                print(f"     archived original left at {orig}: {target} exists")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(orig), str(target))
                entry["original"] = str(target)
        state.pop(key)
        state[new_rel.with_suffix("").as_posix()] = entry
        changed = True
    if changed:
        atomic_write_json(path, state)


def recording_now():
    root = Path.cwd()
    return jobs.status(root)["record"]["running"] or bool(jobs.external(max_age=0).get("record"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg, adapter = config.load()
    if not args.dry_run and recording_now():
        raise SystemExit("A recording is running. Organize when it has finished: "
                         "the recorder rewrites progress.json after every item.")
    outdir = Path(cfg["output_dir"])
    progress_path = Path(cfg["progress_file"])
    raws = json.loads(Path(cfg["queue_file"]).read_text(encoding="utf-8"))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    items = [adapter.item(r) for r in raws]
    by_id = {it.id: (pos, it) for pos, it in enumerate(items, 1)}

    moves = []
    for sid, rec in progress.items():
        if rec.get("status") != "done" or sid not in by_id:
            continue
        ep, item = by_id[sid]
        found = _find(Path(rec["file"]), outdir)
        if not found:
            print(f"  MISSING {rec['file']}")
            continue
        src = found
        title = clean_title(item.title, cfg)
        dst = outdir / (str(relpath(cfg, ep, item, title)) + src.suffix)
        moves.append((src, dst, item, ep, title))

    moves.sort(key=lambda m: m[3])
    for src, dst, item, ep, title in moves:
        print(f"  #{ep:02d}  {aired(item) or '-'}  {title}")
        print(f"        {src.name}")
        print(f"     -> {dst.relative_to(outdir)}")

    # OBS's default "YYYY-MM-DD HH-MM-SS.mkv" names that no item claims.
    strays = [p for p in outdir.glob("*.mkv")
              if re.match(r"\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.mkv$", p.name)
              and p.name not in {m[0].name for m in moves}]
    for p in strays:
        print(f"  partial -> _partial/{p.name}")

    if args.dry_run:
        print(f"\n{len(moves)} episodes, {len(strays)} partials (dry run)")
        return

    nfo = write_nfo(cfg)
    if nfo and layout(cfg) == "media_server":
        show_root(cfg).mkdir(parents=True, exist_ok=True)
        (show_root(cfg) / "tvshow.nfo").write_text(show_nfo(cfg), encoding="utf-8")

    # Two items rendering to one name: file the first, report the rest.
    claimed, pending, conflicts = set(), [], []
    for m in moves:
        key = str(m[1]).lower()
        if key in claimed:
            conflicts.append((m, "another item files to the same name"))
        else:
            claimed.add(key)
            pending.append(m)

    filed = 0
    while pending:
        progressed, waiting = False, []
        for m in pending:
            src, dst, item, ep, title = m
            if dst.exists() and not _same_file(src, dst):
                waiting.append(m)           # a later move may free the name
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not _same_file(src, dst):
                shutil.move(str(src), str(dst))
                old_nfo = src.with_suffix(".nfo")
                if old_nfo.exists() and not dst.with_suffix(".nfo").exists():
                    shutil.move(str(old_nfo), str(dst.with_suffix(".nfo")))
                _follow_optimize(cfg, src, dst)
            if nfo:
                dst.with_suffix(".nfo").write_text(episode_nfo(item, ep, title, cfg),
                                                   encoding="utf-8")
            if progress[item.id].get("file") != str(dst):
                progress[item.id]["file"] = str(dst)
                atomic_write_json(progress_path, progress)
            filed += 1
            progressed = True
            print(f"  #{ep:02d} {dst.relative_to(outdir)}")
        pending = waiting
        if not progressed:
            conflicts += [(m, "a different file already has this name") for m in pending]
            break

    for (src, dst, item, ep, title), why in conflicts:
        print(f"  !! #{ep:02d} not moved, {why}: {dst}")
        print(f"     still at {src}")

    if strays:
        partial_dir(cfg).mkdir(exist_ok=True)
        for p in strays:
            target = partial_dir(cfg) / p.name
            if target.exists():
                print(f"  !! partial {p.name} not moved: {target} exists")
                continue
            shutil.move(str(p), str(target))

    print(f"\n{filed} recordings filed under {outdir} ({layout(cfg)} layout)")
    print(f"{len(strays)} partials -> {partial_dir(cfg)}")
    if conflicts:
        print(f"{len(conflicts)} left in place: rename or remove the files above, then rerun")


if __name__ == "__main__":
    main()
