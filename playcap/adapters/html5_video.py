"""Generic adapter: a list of pages, each with an ordinary HTML5 <video>.

Queue source (config key "queue_source", or the first argument to
build_queue.py) is either

  * a .json file holding a list whose entries are a URL/path string or an
    object {"url", "title"?, "id"?, "kind"?, "aired_at"? (ISO 8601), "locked"?}
  * a text file, one entry per line: "url" or "Title | url"; blank lines and
    lines starting with # are ignored.

Entries that are not URLs are treated as file paths relative to the queue
source and turned into file:// URLs, which is how examples/demo works with no
server at all.

The player is the first laid-out <video> on the page; failing that, the first
laid-out iframe -- looked into directly when it is same-origin, attached to as
its own CDP target when it is cross-origin (Chrome runs those out of process).

No logins, no cookies, no tokens: the page must already play in the debug
browser exactly as it would for you by hand.
"""
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from playcap import cdp
from playcap.adapters.base import VIDEO, Adapter as Base, Player

# Where's the player? Elements under 50 px are not laid out yet.
FIND_PLAYER_JS = r"""
JSON.stringify((() => {
  const box = el => { const r = el.getBoundingClientRect();
                      return (r.width >= 50 && r.height >= 50) ? r : null; };
  const out = (r, where, index, src) =>
    ({x: r.x, y: r.y, w: r.width, h: r.height, where, index, src: (src || '').slice(0, 120)});
  const videos = [...document.querySelectorAll('video')];
  for (let i = 0; i < videos.length; i++) {
    const r = box(videos[i]);
    if (r) return out(r, 'page', i, videos[i].currentSrc || videos[i].src);
  }
  const frames = [...document.querySelectorAll('iframe')];
  for (let i = 0; i < frames.length; i++) {
    const r = box(frames[i]);
    if (!r) continue;
    let doc = null;
    try { doc = frames[i].contentDocument; } catch (e) {}
    if (doc) {
      if (doc.querySelector('video')) return out(r, 'frame-same', i, frames[i].src);
      continue;
    }
    return out(r, 'frame', i, frames[i].src);
  }
  return null;
})())
"""


def _element_js(rect):
    """JS for the element to fullscreen, as seen from the top page."""
    if rect["where"] == "page":
        return f"document.querySelectorAll('video')[{rect['index']}]"
    return f"document.querySelectorAll('iframe')[{rect['index']}]"


def _to_url(entry, base):
    if "://" in entry:
        return entry
    return (base / entry).resolve().as_uri()


def _default_title(url):
    path = unquote(urlparse(url).path).rstrip("/")
    name = path.rsplit("/", 1)[-1] or urlparse(url).netloc or url
    return name.rsplit(".", 1)[0] if "." in name else name


def read_queue_source(path):
    path = Path(path)
    base = path.parent
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raw = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if " | " in line:
                title, url = line.rsplit(" | ", 1)
                raw.append({"title": title.strip(), "url": url.strip()})
            else:
                raw.append(line)
    items, seen = [], set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {"url": entry}
        url = _to_url(entry["url"], base)
        item = {
            "id": str(entry.get("id") or hashlib.sha1(url.encode()).hexdigest()[:12]),
            "title": entry.get("title") or _default_title(url),
            "url": url,
            "kind": entry.get("kind", "video"),
            "locked": bool(entry.get("locked", False)),
            "aired_at": entry.get("aired_at"),
        }
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        items.append(item)
    return items


class Adapter(Base):
    config_defaults = {"queue_source": "queue.txt"}
    label = "List of links"
    setup_fields = [{
        "key": "links", "kind": "links",
        "label": "Pages to record, one per line",
        "help": "A page URL per line, or \"Title | URL\". Each page must play an HTML5 video "
                "in the playcap browser window.",
    }]

    def build_queue(self, cfg, argv):
        src = argv[0] if argv else cfg.get("queue_source")
        if not src or not Path(src).exists():
            sys.exit(f"Queue source not found: {src!r}. Pass a path or set "
                     "queue_source in config.json.")
        return read_queue_source(src)

    def find_player(self, sess):
        return sess.js_json(FIND_PLAYER_JS)

    def attach_player(self, sess, rect):
        if rect["where"] == "page":
            return Player(sess, _element_js(rect), rect["src"])
        if rect["where"] == "frame-same":
            return Player(sess, _element_js(rect) + ".contentDocument.querySelector('video')",
                          rect["src"])
        tgt = cdp.find(rect["src"][:60], kinds=("iframe",), tries=8, delay=1.5)
        return Player(cdp.Session(tgt), VIDEO, rect["src"]) if tgt else None

    def reattach_player(self, sess, player):
        if player.session is sess:
            return None          # the page itself went away; nothing to re-find
        tgt = cdp.find(player.src[:60], kinds=("iframe",), tries=10, delay=1.5)
        return Player(cdp.Session(tgt), VIDEO, player.src) if tgt else None

    def fullscreen_js(self, rect):
        return ("(async()=>{const e=%s; if(!e) return false;"
                " await e.requestFullscreen(); return true;})()" % _element_js(rect))
