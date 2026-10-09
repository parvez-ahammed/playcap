# Writing an adapter

playcap's core knows nothing about any particular site. Everything
site-specific lives in an adapter. [Back to the README](../README.md)

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

## Rules

Adapters must not automate logins, copy sessions or tokens, or work around
device or concurrency limits. See [RESPONSIBLE_USE.md](../RESPONSIBLE_USE.md).

Keep private adapters in `local/`. That directory is gitignored, and when
`config.json` has no `"adapter"` key, playcap uses `local.ADAPTER` if a
`local/__init__.py` defines one.
