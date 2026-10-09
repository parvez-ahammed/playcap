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

Building the queue from a page. A line "collect: https://..." (in JSON, an
entry {"collect": "https://..."}) stands for every item linked from that
index page, read with the collect rule of the first recipe whose collect.page
matches it (playcap.recipes). config key "collect" may also hold full rules,
[{"page": url, "links": css, "url_regex", "title", "next", "max_pages",
"reverse"}], collected after the file's entries. Collecting reads the
*rendered* DOM in the debug browser over CDP -- the listing a person sees --
because a site's private API is usually a dead end. "next" is followed by its
href when it is a real link, otherwise by a trusted CDP click.

This lives in the generic adapter rather than a sibling one because it is the
same source (a list of pages, each playing a <video>) with one more way of
writing the list; a second adapter would split the setup wizard's choices for
no gain, and "collect:" lines mix freely with hand-typed ones.

Collected entries get the same id as the same URL typed by hand (sha1 of the
URL), so progress files keep working, and carry "collected_from" (the index
URL). A rebuild keeps each index page's earlier entries where they were, in
their old order, and appends what is new: item numbers -- and so file names --
come from queue position, and a newest-first listing would otherwise renumber
everything on every rebuild. Entries that dropped off the index page are kept
for the same reason (many listings show only the latest N). "reverse": true
reads a newest-first page bottom-up, so the first build numbers oldest first.

The player is the largest laid-out <video> on the page; failing that, the
largest laid-out iframe -- looked into directly when it is same-origin,
attached to as its own CDP target when it is cross-origin (Chrome runs those
out of process). When a page fools that guess, config "player_selector" (a CSS
selector, also in the setup wizard) names the player element instead.

Recipes (playcap.recipes, taught in the UI or imported) do the same per page:
the first recipe matching the page's URL (and markup, match.page_has) names
the player, and optionally a play button, which is then where the trusted CDP
click lands (rect["click"]); the player element is still what gets
fullscreened. Precedence: config "player_selector" > matching recipe > the
largest-video guess.

No logins, no cookies, no tokens: the page must already play in the debug
browser exactly as it would for you by hand.
"""
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from playcap import cdp, recipes
from playcap.adapters.base import VIDEO, Adapter as Base, Player

# Where's the player? The largest laid-out <video> wins (ads and preview
# thumbnails are smaller); failing that, the largest laid-out iframe. A
# configured CSS selector ("player_selector") overrides the guess: it may name
# the <video>, the player's <iframe>, or a container holding a <video>.
# Elements under 50 px are not laid out yet. %s is the selector, JSON-encoded.
FIND_PLAYER_JS = r"""
JSON.stringify((() => {
  const sel = %s;
  const box = el => { const r = el.getBoundingClientRect();
                      return (r.width >= 50 && r.height >= 50) ? r : null; };
  const out = (r, where, index, src) =>
    ({x: r.x, y: r.y, w: r.width, h: r.height, where, index, src: (src || '').slice(0, 120)});
  const videos = [...document.querySelectorAll('video')];
  const frames = [...document.querySelectorAll('iframe')];
  const frameHit = (f, r) => {
    let doc = null;
    try { doc = f.contentDocument; } catch (e) {}
    if (doc) return doc.querySelector('video') ? out(r, 'frame-same', frames.indexOf(f), f.src) : null;
    return out(r, 'frame', frames.indexOf(f), f.src);
  };
  if (sel) {
    const e = document.querySelector(sel);
    const r = e && box(e);
    if (!r) return null;                       // configured player not on screen (yet)
    if (e.tagName === 'VIDEO') return out(r, 'page', videos.indexOf(e), e.currentSrc || e.src);
    if (e.tagName === 'IFRAME') return frameHit(e, r);
    const v = e.querySelector('video');
    if (v) return out(r, 'page', videos.indexOf(v), v.currentSrc || v.src);
    const f = e.querySelector('iframe');
    return f ? frameHit(f, box(f) || r) : null;
  }
  const area = r => r.width * r.height;
  let best = null;
  videos.forEach((v, i) => { const r = box(v);
    if (r && (!best || area(r) > area(best.r))) best = {r, i, src: v.currentSrc || v.src}; });
  if (best) return out(best.r, 'page', best.i, best.src);
  const ranked = frames.map(f => ({f, r: box(f)})).filter(x => x.r)
                       .sort((a, b) => area(b.r) - area(a.r));
  for (const {f, r} of ranked) { const hit = frameHit(f, r); if (hit) return hit; }
  return null;
})())
"""

# Where a recipe's play button is, if it is laid out. %s: JSON-encoded selector.
CLICK_TARGET_JS = r"""
JSON.stringify((() => {
  let e = null;
  try { e = document.querySelector(%s); } catch (err) { return null; }
  if (!e) return null;
  const r = e.getBoundingClientRect();
  if (r.width < 4 || r.height < 4) return null;
  const s = getComputedStyle(e);
  if (s.visibility === 'hidden' || s.display === 'none') return null;
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
})())
"""

PAGE_HAS_JS = "(() => { try { return !!document.querySelector(%s); } catch (e) { return false; } })()"

# The item links on an index page, in page order. %s: links selector, then
# title selector (both JSON). An element that is not a link stands for the
# link inside it, or around it (a card whose <a> is a child or a parent).
COLLECT_JS = r"""
JSON.stringify((() => {
  const sel = %s, tsel = %s;
  let nodes;
  try { nodes = [...document.querySelectorAll(sel)]; } catch (e) { return {error: 'bad selector'}; }
  const out = [];
  for (const n of nodes) {
    const a = n.matches('a[href]') ? n : (n.querySelector('a[href]') || n.closest('a[href]'));
    if (!a) continue;
    let t = '';
    if (tsel) {
      let scope = a;
      for (let i = 0; i < 4 && scope && !t; i++, scope = scope.parentElement) {
        let hit = [];
        try { hit = scope.querySelectorAll(tsel); } catch (e) { break; }
        if (hit.length === 1) t = hit[0].textContent;
      }
    }
    if (!t) t = a.getAttribute('title') || a.textContent || a.getAttribute('aria-label') || '';
    out.push({url: a.href, title: t.replace(/\s+/g, ' ').trim().slice(0, 200)});
  }
  return {links: out};
})())
"""

# The "next page" control: its href when it is a real link, and its centre for
# a trusted click otherwise. %s: JSON-encoded selector.
NEXT_JS = r"""
JSON.stringify((() => {
  let e = null;
  try { e = document.querySelector(%s); } catch (err) { return null; }
  if (!e || e.disabled || e.getAttribute('aria-disabled') === 'true') return null;
  const a = e.closest('a[href]') || e.querySelector('a[href]');
  const href = a ? a.href : '';
  e.scrollIntoView({block: 'center'});
  const r = e.getBoundingClientRect();
  if (!href && (r.width < 2 || r.height < 2)) return null;
  return {href: /^https?:|^file:/.test(href) ? href : '', x: r.x + r.width / 2, y: r.y + r.height / 2};
})())
"""

LINK_WAIT_SECONDS = 20        # an index page often renders its list after an API round-trip
NEXT_WAIT_SECONDS = 15        # a "load more" click: wait this long for the list to change
COLLECT_PREFIX = "collect:"


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


def parse_source(path):
    """The queue source's entries, unexpanded: URL strings, entry objects and
    {"collect": index_url} markers, in file order."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    raw = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith(COLLECT_PREFIX):
            raw.append({"collect": line[len(COLLECT_PREFIX):].strip()})
        elif " | " in line:
            title, url = line.rsplit(" | ", 1)
            raw.append({"title": title.strip(), "url": url.strip()})
        else:
            raw.append(line)
    return raw


def _entry(entry, base, source=None):
    url = _to_url(entry["url"], base)
    item = {
        "id": str(entry.get("id") or hashlib.sha1(url.encode()).hexdigest()[:12]),
        "title": entry.get("title") or _default_title(url),
        "url": url,
        "kind": entry.get("kind", "video"),
        "locked": bool(entry.get("locked", False)),
        "aired_at": entry.get("aired_at"),
    }
    if source:
        item["collected_from"] = source
    return item


def _merge_block(source, fresh, previous):
    """One index page's entries: last build's, in their old order, then the
    new ones. See the module docstring for why nothing moves or drops."""
    old = [dict(e) for e in previous or [] if isinstance(e, dict)
           and e.get("collected_from") == source and e.get("id")]
    known = {e["id"] for e in old}
    return old + [e for e in fresh if e["id"] not in known]


def rule_for(url, cfg):
    """The collect rule for a "collect:" line: the first recipe whose
    collect.page matches the URL, aimed at that URL."""
    if not re.match(r"^(https?|file):", url or "", re.I):
        raise ValueError(f"collect: needs a web address, got {url!r}")
    recipe = recipes.for_index(cfg, url)
    if not recipe:
        raise ValueError(f"No recipe says how to read {url}. Teach playcap that page "
                         "(Recipes in the UI) and pick the item links.")
    return {**recipe["collect"], "page": url}


def config_rules(cfg):
    """Validated collect rules from config key "collect"."""
    out = []
    for i, rule in enumerate((cfg or {}).get("collect") or [], 1):
        clean, errors = recipes.validate({"playcap_recipe": recipes.VERSION,
                                          "name": f"collect {i}", "match": {"url": "*"},
                                          "collect": rule})
        if errors:
            raise ValueError(f'config "collect" entry {i}: ' + " ".join(errors))
        out.append(clean["collect"])
    return out


def read_queue_source(path, collect=None, previous=None, extra=(), cfg=None):
    """Queue entries from a links file (path may be None when only config
    rules are used). collect(rule) -> [{"url", "title"}] reads one index page;
    the adapter passes one that drives the browser. previous is the last
    queue, for stable collected blocks; extra are collect rules from config,
    expanded after the file; cfg holds the recipes for "collect:" lines."""
    raw = list(parse_source(path)) if path else []
    base = Path(path).parent if path else Path.cwd()
    blocks = []
    for entry in raw + [{"collect": rule} for rule in extra]:
        if isinstance(entry, str):
            entry = {"url": entry}
        if "collect" in entry:
            rule = entry["collect"]
            if isinstance(rule, str):
                rule = rule_for(rule, cfg or {})
            if collect is None:
                raise ValueError(f"{rule['page']} needs the browser to read; "
                                 "build the queue with the debug browser running")
            fresh = [_entry(x, base, rule["page"]) for x in collect(rule)]
            if rule.get("reverse"):
                fresh.reverse()
            blocks.extend(_merge_block(rule["page"], fresh, previous))
        else:
            blocks.append(_entry(entry, base))
    items, seen = [], set()
    for item in blocks:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        items.append(item)
    return items


def keep_link(url, rule, page_url=""):
    """Item links worth queueing: web or file URLs, not the index page itself
    or an in-page anchor, and matching url_regex when one is set."""
    if not re.match(r"^(https?|file):", url or "", re.I):
        return False
    if url.split("#", 1)[0] == page_url.split("#", 1)[0]:
        return False
    rx = rule.get("url_regex")
    return not rx or re.search(rx, url) is not None


def _links(sess, sel, tsel=""):
    res = sess.js_json(COLLECT_JS % (json.dumps(sel), json.dumps(tsel or "")))
    if isinstance(res, dict) and res.get("error"):
        raise ValueError(f"links selector {sel!r} is not valid CSS")
    return (res or {}).get("links", []) if isinstance(res, dict) else []


def collect_with(sess, rule, wait=LINK_WAIT_SECONDS, sleep=time.sleep):
    """Read one index page, and its next pages, in a CDP session.
    -> [{"url", "title"}] in page order, each URL once."""
    sel = rule.get("links") or "a[href]"
    pages = max(1, int(rule.get("max_pages") or 1))
    found, seen, visited = [], set(), set()
    url = rule["page"]
    sess.navigate(url, settle=3)
    for page in range(pages):
        visited.add(url)
        links = []
        for _ in range(max(1, int(wait))):
            links = [x for x in _links(sess, sel, rule.get("title"))
                     if keep_link(x.get("url"), rule, url)]
            if links:
                break
            sleep(1)
        new = [x for x in links if x["url"] not in seen]
        for x in new:
            seen.add(x["url"])
            found.append({"url": x["url"], "title": x.get("title") or ""})
        if not rule.get("next") or page + 1 >= pages or (page and not new):
            break
        nxt = sess.js_json(NEXT_JS % json.dumps(rule["next"]))
        if not nxt:
            break
        if nxt.get("href"):
            if nxt["href"] in visited:
                break
            url = nxt["href"]
            sess.navigate(url, settle=3)
            continue
        # A button ("Load more", a script-driven pager): a trusted click, then
        # wait for a link we have not seen yet.
        sess.click(nxt["x"], nxt["y"])
        for _ in range(NEXT_WAIT_SECONDS):
            sleep(1)
            if any(x.get("url") not in seen and keep_link(x.get("url"), rule, url)
                   for x in _links(sess, sel)):
                break
        else:
            break
    return found


class Adapter(Base):
    config_defaults = {"queue_source": "queue.txt"}
    label = "List of links"
    setup_fields = [{
        "key": "links", "kind": "links",
        "label": "Pages to record, one per line",
        "help": "A page URL per line, or \"Title | URL\". Each page must play an HTML5 video "
                "in the playcap browser window. \"collect: URL\" adds every video linked from "
                "an index page that one of your recipes knows how to read.",
    }, {
        "key": "player_selector", "kind": "text", "optional": True,
        "label": "Player element (optional, CSS selector)",
        "help": "Leave empty: playcap picks the largest video on the page. Fill in only if it "
                "picks the wrong one, e.g. #main-player video or iframe.player.",
    }]

    @property
    def queue_needs_browser(self):
        """False while the queue is just the pasted list (the UI then rebuilds
        it by itself after a save); True once it collects from index pages.
        A relative queue_source is read from the current folder."""
        cfg = self.cfg or {}
        if cfg.get("collect"):
            return True
        try:
            return any(isinstance(e, dict) and "collect" in e
                       for e in parse_source(cfg.get("queue_source") or "queue.txt"))
        except (OSError, ValueError):
            return False

    def build_queue(self, cfg, argv):
        src = argv[0] if argv else cfg.get("queue_source")
        try:
            extra = config_rules(cfg)
        except ValueError as exc:
            sys.exit(str(exc))
        if not src or not Path(src).exists():
            if not extra:
                sys.exit(f"Queue source not found: {src!r}. Pass a path or set "
                         "queue_source in config.json.")
            src = None
        previous = []
        qf = Path(cfg.get("queue_file") or "queue.json")
        if qf.exists():
            try:
                previous = json.loads(qf.read_text(encoding="utf-8"))
            except ValueError:
                previous = []
        sessions = []

        def collect(rule):
            if not sessions:
                pages = [t for t in cdp.targets(("page",))
                         if not t["url"].startswith(("devtools://", "chrome-extension://"))]
                sessions.append(cdp.Session(pages[0] if pages else cdp.open_page(rule["page"])))
            print(f"collecting {rule['page']}")
            got = collect_with(sessions[0], rule)
            print(f"   {len(got)} links")
            return got

        try:
            return read_queue_source(src, collect, previous, extra, cfg)
        except ValueError as exc:
            sys.exit(str(exc))
        finally:
            for s in sessions:
                s.close()

    def recipe(self, sess):
        """The recipe for the page sess is on, or None."""
        cfg = self.cfg or {}
        if not cfg.get("recipes"):
            return None
        try:
            url = sess.js("location.href") or ""
        except cdp.CdpError:
            return None
        return recipes.for_page(
            cfg, url, lambda sel: bool(sess.js(PAGE_HAS_JS % json.dumps(sel))))

    def find_player(self, sess):
        sel = (self.cfg or {}).get("player_selector") or ""
        recipe = self.recipe(sess)
        if not sel and recipe and recipe["player"]:
            sel = recipe["player"]
        rect = sess.js_json(FIND_PLAYER_JS % json.dumps(sel))
        if rect and recipe and recipe["play_button"]:
            hit = sess.js_json(CLICK_TARGET_JS % json.dumps(recipe["play_button"]))
            if hit:
                rect["click"] = [hit["x"], hit["y"]]
        return rect

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
