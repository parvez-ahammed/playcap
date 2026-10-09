# Writing an adapter

playcap's core knows nothing about any particular site. Everything
site-specific lives in an adapter. [Back to the README](../README.md)

Before writing one, check whether a [recipe](#recipes) is enough.

An adapter is a module that defines a class named `Adapter`, subclassing
`playcap.adapters.base.Adapter`. Select it in `config.json` with
`"adapter": "your_module"` (a dotted path to any importable module). The
built-in `html5_video` adapter works with any page that has a `<video>`, so
you only need your own when a site needs more than that.

```python
from playcap import cdp
from playcap.adapters.base import Adapter as Base, Item, Player, VIDEO

class Adapter(Base):
    config_defaults = {"show": "Conference 2026"}

    def build_queue(self, cfg, argv):
        # return a list of JSON-serialisable dicts; saved as the queue file
        return [{"id": "1", "title": "Keynote", "url": "https://example.org/1"}]

    def find_player(self, sess):
        # rect of the element to click and fullscreen, or None if not ready
        return sess.js_json("JSON.stringify((() => { const v = document.querySelector('video');"
                            " if (!v) return null; const r = v.getBoundingClientRect();"
                            " return {x: r.x, y: r.y, w: r.width, h: r.height, src: v.src}; })())")

    def attach_player(self, sess, rect):
        return Player(sess, VIDEO, rect["src"])   # where <video> can be polled

    def fullscreen_js(self, rect):
        return "(async()=>{await document.querySelector('video').requestFullscreen(); return true;})()"
```

## Optional hooks

- `item(raw)` maps your queue entries onto `Item` (`id`, `title`, `url`,
  `kind`, `locked`, `aired_at`), so existing state files can keep their own
  shape.
- `page_target` picks the tab.
- `check_page` can stop the run early (for example when the page shows a
  sign-in form).
- `reattach_player` handles a player frame being replaced.
- `relaunch_browser` and `browser_command` start your browser.
- `labels` changes the wording.

`playcap/adapters/html5_video.py` is a complete, small example.

## Recipes

A recipe is the no-code alternative: a JSON object that the built-in
`html5_video` adapter reads. In the UI, **Recipes → Teach playcap a page**
puts a pick bar on a page in the playcap browser; you click the video, the
play button and a few item links, and playcap writes robust CSS selectors
(ids, `data-*` attributes and stable class names before positions) and checks
that each one matches exactly what you picked. Recipes are saved in
`config.json` (gitignored), and can be exported and imported as `.json`
files. Ready-made ones for common player types are in
[`recipes/`](../recipes/README.md).

```json
{
  "playcap_recipe": 1,
  "name": "Conference talks",
  "match": {"url": "https://talks.example.org/watch/*", "page_has": ""},
  "player": "#talk-player",
  "play_button": "#talk-player .big-play",
  "collect": {"page": "https://talks.example.org/archive*", "links": "ul.talks a.talk-link",
              "url_regex": "/watch/", "title": ".talk-title", "next": "a[rel=next]",
              "max_pages": 5, "reverse": false}
}
```

- `match.url` is a glob over the page address (`*` matches anything), or a
  regular expression after `re:`. `match.page_has` optionally requires an
  element on the page; the gallery uses it to match a player by its markup.
- `player` is the element to fullscreen (the `<video>`, its `<iframe>`, or a
  container holding one). `play_button` is where the start click lands; it is
  still a trusted CDP click.
- `collect` reads an index page. Put `collect: <index page address>` on its
  own line in your list of links (the recipe form has a button for it), and
  **Refresh queue** with the playcap browser open adds every linked video.
  `links` and/or `url_regex` pick the links, `title` is looked up inside each
  link and up to three levels around it, `next` is followed for up to
  `max_pages` pages. Hand-written rules can also go in `config.json` as
  `"collect": [{"page": "https://…", "links": "…"}]`.

Collected videos get the same ids as the same addresses typed by hand, so
progress is kept. A rebuild keeps earlier items where they were and appends
new ones, so file numbers never shift.

Precedence for the player: `player_selector` in config, then the first
matching recipe, then the largest video on the page.

**A recipe is enough** when the page plays in the playcap browser once it is
open, the player is an element you can click, and the list of videos is a page
of links. **Write an adapter** when you need logic: deciding what is locked or
not yet aired, reading dates, a player whose frame is replaced mid-playback
(`reattach_player`), checking that the page is usable (`check_page`), or a
listing that is not a page of links.

## Rules

Adapters must not automate logins, copy sessions or tokens, or work around
device or concurrency limits. See [RESPONSIBLE_USE.md](../RESPONSIBLE_USE.md).
The same goes for recipes, and recipes for paid streaming or course platforms
are not accepted into `recipes/`.

Keep private adapters in `local/`. That directory is gitignored, and when
`config.json` has no `"adapter"` key, playcap uses `local.ADAPTER` if a
`local/__init__.py` defines one.
