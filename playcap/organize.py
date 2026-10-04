"""Lay the recordings out as a Jellyfin TV series and give each one real metadata.

Jellyfin shows the raw filename when nothing matches a metadata provider, and
a filename truncated mid-word is worse. So we do two things: name each file
SxxEyy like an episode, and drop an .nfo beside it that carries the
untruncated title, the air date and the full description.

    <output_dir>/<show>/Season 01/S01E07 - Some Topic- II.mp4
                                  S01E07 - Some Topic- II.nfo

Episode numbers are queue positions, so a later re-record lands on the same
number. Show name, season, and the title clean-up all come from config
("show", "season", "title_cleanup", "title_collapse_restated",
"title_max_len", "show_plot", "show_premiered") -- a site adapter usually
supplies them as defaults.

Usage:  python -m playcap.organize --dry-run     # show the plan
        python -m playcap.organize               # move files and write the .nfo files
"""
import argparse
import json
import re
import shutil
from pathlib import Path
from xml.sax.saxutils import escape

from playcap import config


def show_dir(cfg):
    return Path(cfg["output_dir"]) / cfg["show"] / f"Season {int(cfg['season']):02d}"


def partial_dir(cfg):
    return Path(cfg["output_dir"]) / "_partial"      # kept, but out of the library's way


def episode_label(cfg, index):
    return f"S{int(cfg['season']):02d}E{index:02d}"


def episode_stem(cfg, index, title):
    """The recorder's name: safe() runs on the stem, so a title truncated to
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
    sdir = show_dir(cfg)
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
        # organize.py has always run safe() over the whole file name, suffix
        # included; keep that so existing libraries map to the same paths.
        dst = sdir / safe(f"{episode_label(cfg, ep)} - {title}{src.suffix}")
        moves.append((src, dst, item, ep, title))

    moves.sort(key=lambda m: m[3])
    for src, dst, item, ep, title in moves:
        print(f"  E{ep:02d}  {aired(item)}  {title}")
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

    sdir.mkdir(parents=True, exist_ok=True)
    (outdir / cfg["show"] / "tvshow.nfo").write_text(show_nfo(cfg), encoding="utf-8")

    progress_changed = False
    for src, dst, item, ep, title in moves:
        if src.resolve() != dst.resolve():
            shutil.move(str(src), str(dst))
        dst.with_suffix(".nfo").write_text(episode_nfo(item, ep, title, cfg),
                                           encoding="utf-8")
        progress[item.id]["file"] = str(dst)
        progress_changed = True
        print(f"  E{ep:02d} {dst.name}")

    if strays:
        partial_dir(cfg).mkdir(exist_ok=True)
        for p in strays:
            shutil.move(str(p), str(partial_dir(cfg) / p.name))

    if progress_changed:
        progress_path.write_text(json.dumps(progress, indent=1))
    print(f"\n{len(moves)} episodes -> {sdir}")
    print(f"{len(strays)} partials -> {partial_dir(cfg)}")


if __name__ == "__main__":
    main()
