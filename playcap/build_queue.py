"""Build the recording queue by asking the adapter, and save it.

The adapter decides where the list comes from: the generic html5_video adapter
reads a file of URLs; a site adapter typically reads the rendered DOM of a
listing page in the logged-in debug browser. (Reading the DOM beats calling a
site's private API: such APIs tend to demand headers only the site's own
front end can produce.)

Writes the queue file (config "queue_file") in the adapter's own entry shape,
then prints a summary using the normalised fields.

Usage:  python build_queue.py                # adapter default source from config.json
        python build_queue.py <source>       # adapter-specific: a URL or file path
"""
import json
import sys
from pathlib import Path

from playcap import config


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg, adapter = config.load()
    out = Path(cfg["queue_file"])

    raws = adapter.build_queue(cfg, argv)
    out.write_text(json.dumps(raws, indent=1))

    items = [adapter.item(r) for r in raws]
    locked = sum(1 for i in items if i.locked)
    kinds = {}
    for i in items:
        kinds[i.kind] = kinds.get(i.kind, 0) + 1
    print(f"{len(items)} items -> {out}   ({kinds}, {locked} locked)")
    for i in items[:8]:
        print(f"   {i.day} {i.time} [{i.kind}]"
              f"{' LOCKED' if i.locked else ''} {i.title[:60]}")
    if len(items) > 8:
        print(f"   ... {len(items) - 8} more")


if __name__ == "__main__":
    main()
