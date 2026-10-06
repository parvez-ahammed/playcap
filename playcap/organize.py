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

Usage:  python -m playcap.organize --dry-run     # show the plan
        python -m playcap.organize               # move files (and write .nfo files)
"""
import argparse
import json
import re
import shutil
from pathlib import Path
from xml.sax.saxutils import escape

from playcap import config

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


def _render(template, values):
    out = template.format(**values)
    parts = []
    for part in re.split(r"[\\/]", out):
        part = re.sub(r"^[\s\-_.,]+|[\s\-_,]+$", "", safe(part))
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
    return re.sub(r'[<>:"/\|?*]', "-", name).rstrip(". ")


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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg, adapter = config.load()
    outdir = Path(cfg["output_dir"])
    progress_path = Path(cfg["progress_file"])
    raws = json.loads(Path(cfg["queue_file"]).read_text())
    progress = json.loads(progress_path.read_text())
    items = [adapter.item(r) for r in raws]
    by_id = {it.id: (pos, it) for pos, it in enumerate(items, 1)}

    moves = []
    for sid, rec in progress.items():
        if rec.get("status") != "done" or sid not in by_id:
            continue
        ep, item = by_id[sid]
        src = Path(rec["file"])
        if not src.exists():               # the recording moved drives
            alt = outdir / src.name
            if not alt.exists():
                print(f"  MISSING {src}")
                continue
            src = alt
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

    progress_changed = False
    for src, dst, item, ep, title in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dst.resolve():
            shutil.move(str(src), str(dst))
        if nfo:
            dst.with_suffix(".nfo").write_text(episode_nfo(item, ep, title, cfg),
                                               encoding="utf-8")
        progress[item.id]["file"] = str(dst)
        progress_changed = True
        print(f"  #{ep:02d} {dst.relative_to(outdir)}")

    if strays:
        partial_dir(cfg).mkdir(exist_ok=True)
        for p in strays:
            shutil.move(str(p), str(partial_dir(cfg) / p.name))

    if progress_changed:
        progress_path.write_text(json.dumps(progress, indent=1))
    print(f"\n{len(moves)} recordings filed under {outdir} ({layout(cfg)} layout)")
    print(f"{len(strays)} partials -> {partial_dir(cfg)}")


if __name__ == "__main__":
    main()
