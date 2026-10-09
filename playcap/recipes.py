"""Recipes: what playcap needs to know about a page, without writing an adapter.

A recipe is a small JSON object a non-programmer can make by pointing at a
page ("Teach playcap a page" in the UI, playcap.teach) or download from
someone else:

    {
      "playcap_recipe": 1,
      "name": "Video.js player",
      "description": "optional, one line",
      "match": {"url": "*", "page_has": ".video-js"},
      "player": ".video-js",
      "play_button": ".vjs-big-play-button",
      "collect": {"page": "https://example.org/talks/*", "links": "main a.talk",
                  "url_regex": "", "title": "", "next": "a[rel=next]",
                  "max_pages": 5, "reverse": false}
    }

match.url is a glob over the whole page URL (fnmatch: * also crosses "/"),
or a regular expression when it starts with "re:". match.page_has is an
optional CSS selector that must exist on the page; that is how the gallery
in recipes/ matches a *player type* by its markup instead of naming a site.

player names the element to click and fullscreen (the <video>, its <iframe>
or a container holding one); play_button, when set and visible, is where the
trusted CDP click lands instead of the player's centre. collect tells
build_queue how to turn an index page whose URL matches collect.page into
queue entries (playcap.adapters.html5_video): links is a CSS selector for the
item links (an element that is not an <a> stands for the link inside or
around it), url_regex keeps only matching link URLs (either may be left out,
not both), title is searched inside the link and then up to three ancestors
up, next is the "next page" control, followed at most max_pages pages.

Where recipes live: the "recipes" list in config.json, which is gitignored,
so taught recipes stay on the user's machine. The UI exports one as a .json
file and imports such files back; import goes through validate(), which is
strict (unknown keys, wrong types, bad regexes and oversized values are all
refused) because the file came from someone else. Selectors only ever reach
the page JSON-encoded, never spliced into script text.

First matching recipe wins. New recipes go to the front of the list, so a
recipe just taught beats an older, broader one.

Selector generation is pure (candidates / set_candidates) so it can be
tested without a browser: the page overlay (playcap.teach) describes the
picked element and its ancestors, Python proposes selectors from that, and
the page then checks which proposal matches exactly the pick. Stable hooks
(ids, data-* attributes, meaningful classes) are preferred over position
(nth-of-type), which breaks as soon as the page adds an element above.
"""
import fnmatch
import json
import re
from pathlib import Path

VERSION = 1
MAX_RECIPES = 200
MAX_SELECTOR = 300
MAX_NAME = 80
MAX_TEXT = 300
MAX_PAGES = 50

TOP_KEYS = {"playcap_recipe", "name", "description", "match", "player", "play_button", "collect"}
MATCH_KEYS = {"url", "page_has"}
COLLECT_KEYS = {"page", "links", "url_regex", "title", "next", "max_pages", "reverse"}


# ---------------------------------------------------------------- validation
def _text(value, limit, what, errors, required=False):
    if value is None or value == "":
        if required:
            errors.append(f"{what} is required.")
        return ""
    if not isinstance(value, str):
        errors.append(f"{what} must be text.")
        return ""
    value = value.strip()
    if len(value) > limit:
        errors.append(f"{what} is longer than {limit} characters.")
    if any(ord(c) < 32 for c in value):
        errors.append(f"{what} contains control characters.")
    if required and not value:
        errors.append(f"{what} is required.")
    return value


def _pattern(value, what, errors, required=False):
    """A URL glob, or a regex after "re:". Regexes are compiled here so a
    broken one is refused at save/import time, not mid-batch."""
    value = _text(value, MAX_TEXT, what, errors, required)
    if value.startswith("re:"):
        try:
            re.compile(value[3:])
        except re.error as exc:
            errors.append(f"{what}: not a valid regular expression ({exc}).")
    return value


def _regex(value, what, errors):
    value = _text(value, MAX_TEXT, what, errors)
    if value:
        try:
            re.compile(value)
        except re.error as exc:
            errors.append(f"{what}: not a valid regular expression ({exc}).")
    return value


def _unknown(obj, allowed, where, errors):
    extra = sorted(set(obj) - allowed)
    if extra:
        errors.append(f"Unknown {where}key(s): {', '.join(map(str, extra))}.")


def validate(obj):
    """-> (clean recipe, [error, ...]). The clean copy has every optional key
    present, trimmed, so the rest of playcap never has to guess."""
    errors = []
    if not isinstance(obj, dict):
        return None, ["A recipe must be a JSON object."]
    _unknown(obj, TOP_KEYS, "", errors)
    if obj.get("playcap_recipe") != VERSION or isinstance(obj.get("playcap_recipe"), bool):
        errors.append(f'Not a playcap recipe (needs "playcap_recipe": {VERSION}).')
    out = {"playcap_recipe": VERSION,
           "name": _text(obj.get("name"), MAX_NAME, "name", errors, required=True),
           "description": _text(obj.get("description"), MAX_TEXT, "description", errors)}
    match = obj.get("match")
    if not isinstance(match, dict):
        errors.append('"match" must be an object with a "url" pattern.')
        match = {}
    _unknown(match, MATCH_KEYS, "match ", errors)
    out["match"] = {"url": _pattern(match.get("url"), "match.url", errors, required=True),
                    "page_has": _text(match.get("page_has"), MAX_SELECTOR, "match.page_has", errors)}
    out["player"] = _text(obj.get("player"), MAX_SELECTOR, "player", errors)
    out["play_button"] = _text(obj.get("play_button"), MAX_SELECTOR, "play_button", errors)
    col = obj.get("collect")
    if col in (None, {}):
        out["collect"] = None
    elif not isinstance(col, dict):
        errors.append('"collect" must be an object.')
        out["collect"] = None
    else:
        _unknown(col, COLLECT_KEYS, "collect ", errors)
        pages = col.get("max_pages", 5)
        if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= MAX_PAGES:
            errors.append(f"collect.max_pages must be a whole number from 1 to {MAX_PAGES}.")
            pages = 1
        rev = col.get("reverse", False)
        if not isinstance(rev, bool):
            errors.append("collect.reverse must be true or false.")
        out["collect"] = {
            "page": _pattern(col.get("page"), "collect.page", errors, required=True),
            "links": _text(col.get("links"), MAX_SELECTOR, "collect.links", errors),
            "url_regex": _regex(col.get("url_regex"), "collect.url_regex", errors),
            "title": _text(col.get("title"), MAX_SELECTOR, "collect.title", errors),
            "next": _text(col.get("next"), MAX_SELECTOR, "collect.next", errors),
            "max_pages": pages, "reverse": rev is True}
        if not (out["collect"]["links"] or out["collect"]["url_regex"]):
            errors.append("collect needs a links selector, a url_regex, or both.")
    if not (out["player"] or out["play_button"] or out["collect"]):
        errors.append("A recipe needs at least a player, a play button or a collect rule.")
    return (None if errors else out), errors


def parse_import(text):
    """A shared .json file: one recipe, or a list of them. -> ([recipe], [error])."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        return [], [f"Not valid JSON: {exc}"]
    items = data if isinstance(data, list) else [data]
    if not items or len(items) > MAX_RECIPES:
        return [], [f"Expected 1 to {MAX_RECIPES} recipes."]
    good, errors = [], []
    for i, item in enumerate(items, 1):
        clean, errs = validate(item)
        if errs:
            errors.extend(f"recipe {i}: {e}" if len(items) > 1 else e for e in errs)
        else:
            good.append(clean)
    return ([] if errors else good), errors


# ---------------------------------------------------------------- matching
def url_matches(pattern, url):
    if not pattern:
        return False
    if pattern.startswith("re:"):
        try:
            return re.search(pattern[3:], url) is not None
        except re.error:
            return False
    return fnmatch.fnmatchcase(url, pattern)


def usable(cfg):
    """The valid recipes in cfg["recipes"], in order. A hand-edited entry that
    no longer validates is skipped rather than allowed to break a batch."""
    out = []
    for r in (cfg or {}).get("recipes") or []:
        clean, errs = validate(r)
        if not errs:
            out.append(clean)
    return out


def for_page(cfg, url, page_has=None):
    """First recipe whose match.url fits url and whose match.page_has (if any)
    is on the page. page_has(selector) -> bool asks the page; without it,
    recipes that need markup are skipped."""
    for r in usable(cfg):
        if not url_matches(r["match"]["url"], url):
            continue
        sel = r["match"]["page_has"]
        if sel and not (page_has and page_has(sel)):
            continue
        return r
    return None


def for_index(cfg, url):
    """First recipe with a collect rule whose collect.page fits url."""
    for r in usable(cfg):
        if r["collect"] and url_matches(r["collect"]["page"], url):
            return r
    return None


# ---------------------------------------------------------------- storage
CONFIG_KEY = "recipes"


def load_all(root):
    from playcap import settings
    return usable(settings.read(root))


def _write(root, recipes):
    from playcap import settings
    path = Path(root) / settings.CONFIG_NAME
    # Strict read: never replace a config.json we could not read with one
    # holding only recipes.
    current = settings.read_strict(path, {})
    current[CONFIG_KEY] = recipes
    settings.atomic_write_json(path, current)


def upsert(root, recipe, replace=None):
    """Save one recipe (validated) at the front; a recipe of the same name, or
    named `replace`, is replaced. -> (ok, message)."""
    from playcap import settings
    clean, errors = validate(recipe)
    if errors:
        return False, " ".join(errors)
    try:
        existing = usable(settings.read_strict(Path(root) / settings.CONFIG_NAME, {}))
    except settings.Unreadable as exc:
        return False, str(exc)
    names = {clean["name"], replace}
    kept = [r for r in existing if r["name"] not in names]
    if len(kept) >= MAX_RECIPES:
        return False, f"Too many recipes (limit {MAX_RECIPES})."
    _write(root, [clean] + kept)
    replaced = len(kept) < len(existing)
    return True, f"Recipe “{clean['name']}” {'updated' if replaced else 'saved'}."


def import_text(root, text):
    recipes, errors = parse_import(text)
    if errors:
        return False, "Not imported. " + " ".join(errors[:6])
    for r in reversed(recipes):
        ok, msg = upsert(root, r)
        if not ok:
            return False, msg
    return True, (f"Imported {len(recipes)} recipes." if len(recipes) > 1
                  else f"Imported “{recipes[0]['name']}”.")


def delete(root, name):
    from playcap import settings
    try:
        existing = usable(settings.read_strict(Path(root) / settings.CONFIG_NAME, {}))
    except settings.Unreadable as exc:
        return False, str(exc)
    kept = [r for r in existing if r["name"] != name]
    if len(kept) == len(existing):
        return False, "No recipe by that name."
    _write(root, kept)
    return True, "Recipe deleted."


# ---------------------------------------------------------------- selectors
# A picked element arrives as a path, leaf first, each node:
#   {"tag": "a", "id": "x", "classes": [...], "attrs": {"data-id": "7", ...},
#    "nth": 3 (1-based nth-of-type), "same": 5 (siblings of that tag)}
# The overlay never reports its own data-playcap-* marks.

STATE_WORDS = ("active", "selected", "current", "hover", "focus", "visited", "open",
               "closed", "hidden", "visible", "playing", "paused", "loading",
               "loaded", "inactive", "disabled", "checked", "expanded", "collapsed",
               "started", "ended", "waiting", "seeking", "fullscreen", "user-")
STABLE_ATTRS = ("data-testid", "data-test", "data-qa", "data-cy", "itemprop", "name",
                "role", "aria-label", "rel", "type")
_HASHY = re.compile(r"(\d{4,}|^[a-z]{1,4}[-_](?=[a-z]*\d)[a-z0-9]{5,}$|^_(?=[a-z]*\d)[a-z0-9]{5,}|"
                    r"[0-9a-f]{8,}|"
                    r"^(css|sc|jsx|emotion|svelte|ng|vue)-)", re.I)
_IDENT = re.compile(r"^-?[_a-zA-Z][-_a-zA-Z0-9]*$")


def stable_token(tok):
    """True for an id/class that will likely still be there tomorrow: not
    generated (hash-like, long numbers, CSS-in-JS prefixes) and not a state
    such as "active" or "vjs-playing" that changes as the page is used."""
    if not tok or len(tok) > 60 or not _IDENT.match(tok):
        return False
    if _HASHY.search(tok):
        return False
    low = tok.lower()
    return not any(w in low for w in STATE_WORDS) and not low.startswith(("is-", "has-"))


def _attr_value_ok(v):
    return isinstance(v, str) and 0 < len(v) <= 80 and not any(ord(c) < 32 for c in v) \
        and not _HASHY.search(v)


def _quote(v):
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tag(node):
    t = str(node.get("tag") or "").lower()
    return t if re.match(r"^[a-z][a-z0-9-]*$", t) else "*"


def _classes(node):
    return [c for c in (node.get("classes") or []) if stable_token(c)][:4]


def node_parts(node):
    """Selectors for one element on its own, best first (no position)."""
    tag, out = _tag(node), []
    if stable_token(node.get("id") or ""):
        out.append(f"#{node['id']}")
    attrs = node.get("attrs") or {}
    data = sorted(k for k in attrs if k.startswith("data-") and not k.startswith("data-playcap"))
    for key in [k for k in STABLE_ATTRS if k in attrs] + [k for k in data if k not in STABLE_ATTRS]:
        if re.match(r"^[a-z][a-z0-9_-]*$", key) and _attr_value_ok(attrs[key]):
            out.append(f"{tag}[{key}={_quote(attrs[key])}]")
    cls = _classes(node)
    for c in cls:
        out.append(f"{tag}.{c}")
    if len(cls) >= 2:
        out.append(f"{tag}.{cls[0]}.{cls[1]}")
    if tag in ("video", "iframe", "a", "button"):
        out.append(tag)
    return out


def _positional(node):
    tag = _tag(node)
    return f"{tag}:nth-of-type({int(node.get('nth') or 1)})" if (node.get("same") or 1) > 1 else tag


def candidates(path):
    """Selectors for one picked element (path is leaf first), best first.
    The caller keeps the first that matches exactly that element."""
    if not path:
        return []
    leaf, out = path[0], []
    leaf_parts = node_parts(leaf)
    out.extend(leaf_parts)
    # Anchored on the nearest ancestors with a stable hook.
    for anc in path[1:6]:
        for ap in node_parts(anc)[:2]:
            if ap in ("a", "button", "video", "iframe"):
                continue
            for lp in (leaf_parts[:3] or [_tag(leaf)]):
                if not lp.startswith("#"):
                    out.append(f"{ap} {lp}")
            out.append(f"{ap} {_positional(leaf)}")
    # Last resort: a position chain up to the nearest stable ancestor or body.
    chain = []
    for node in path:
        hooks = node_parts(node)
        if chain and hooks and hooks[0].startswith("#"):
            chain.append(hooks[0])
            break
        if _tag(node) in ("body", "html"):
            chain.append("body")
            break
        chain.append(_positional(node))
    out.append(" > ".join(reversed(chain)))
    return _dedupe(out)


def set_candidates(paths):
    """Selectors that may match every picked example of a list (each path leaf
    first). Built from what the examples share, so they generalise to the
    other items; position at the leaf is dropped on purpose."""
    if not paths:
        return []
    per = []
    for path in paths:
        leaf, opts = path[0], []
        leaf_parts = [p for p in node_parts(leaf) if not p.startswith("#")] or [_tag(leaf)]
        opts.extend(p for p in leaf_parts if p != _tag(leaf))
        for depth, anc in enumerate(path[1:6], 1):
            for ap in node_parts(anc)[:3]:
                for lp in leaf_parts[:3]:
                    opts.append(f"{ap} {lp}")
            # The item container shared by the examples (li.card a).
            if depth == 1 and _tag(anc) not in ("body", "html"):
                for ap in [p for p in node_parts(anc) if not p.startswith("#")] or [_tag(anc)]:
                    opts.append(f"{ap} > {_tag(leaf)}")
        per.append(_dedupe(opts))
    common = [s for s in per[0] if all(s in other for other in per[1:])]
    return common


def _dedupe(seq):
    seen, out = set(), []
    for s in seq:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# Run in the page: how many elements each selector matches, and whether that
# includes every element marked with data-playcap-pick~=<role>. %s is a JSON
# list of selectors, then the JSON-encoded role.
CHECK_SELECTORS_JS = r"""
JSON.stringify((() => {
  const sels = %s, role = %s;
  const want = [...document.querySelectorAll('[data-playcap-pick~="' + role + '"]')];
  return sels.map(s => {
    let all;
    try { all = [...document.querySelectorAll(s)]; } catch (e) { return {sel: s, ok: false, count: -1}; }
    const hits = want.filter(w => all.includes(w)).length;
    return {sel: s, count: all.length, hits, want: want.length,
            first: all.length > 0 && all[0] === want[0]};
  });
})())
"""


def choose_single(results):
    """First selector matching exactly the one picked element. Failing that,
    one whose *first* match is the pick (querySelector takes the first), the
    fewest matches winning, since a selector that also matches later elements
    breaks only if one of them moves above the pick."""
    for r in results:
        if r.get("count") == 1 and r.get("hits") == 1:
            return r["sel"]
    loose = [r for r in results if r.get("first") and r.get("hits") == 1 and r.get("count", 0) > 1]
    return min(loose, key=lambda r: r["count"])["sel"] if loose else None


def choose_set(results):
    """The tightest selector that still matches every picked example: of those
    that include all picks, the one matching the fewest elements (it is the
    least likely to pull in navigation links), earliest on a tie."""
    best = None
    for r in results:
        want = r.get("want") or 0
        if want and r.get("hits") == want and r.get("count", 0) >= want:
            if best is None or r["count"] < best["count"]:
                best = r
    return best["sel"] if best else None


def suggest_match(url):
    """A starting URL glob for a page: same site and section, any item."""
    m = re.match(r"^(https?://[^/?#]+)(/[^?#]*)?", url or "")
    if not m:
        return url or "*"
    path = (m.group(2) or "/").rstrip("/")
    head = path.rsplit("/", 1)[0] if path.count("/") > 1 else ""
    return f"{m.group(1)}{head}/*"
