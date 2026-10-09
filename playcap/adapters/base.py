"""The adapter interface: everything the core needs to know about a site.

The recorder (playcap.recorder) is site-agnostic. It asks an adapter:

    build_queue(cfg, argv) -> [raw dict]   what to record; saved as the queue file
    item(raw)              -> Item         normalise one queue entry
    page_target(cfg, item) -> CDP target   which tab to drive
    check_page(sess, item)                 bail out early (e.g. logged out)
    find_player(sess)      -> rect | None  where to click / what to fullscreen
    attach_player(sess, rect) -> Player    where the <video> can be polled
    reattach_player(sess, player)          the player's frame was replaced
    fullscreen_js(rect)    -> JS           fullscreen the player element
    relaunch_browser(cfg)                  the debug browser died mid-batch
    browser_command()      -> argv         control panel "chrome" button

Queue entries stay in whatever shape the adapter wrote them (so existing state
files keep working); item() maps them onto the few fields the core reads.

Why the recorder clicks instead of calling video.play(): CDP input events are
trusted user gestures, and some players (DRM ones especially) only start on a
real gesture. Why fullscreen: otherwise the capture is the page's player box,
not the video's native resolution. Adapters decide *which* element; the core
decides *when*.

Adapters must not automate logins, copy sessions/tokens, or work around device
limits. They read pages you are already entitled to open in your own browser.
"""
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playcap import cdp

VIDEO = "document.querySelector('video')"


class ItemFailed(RuntimeError):
    """This item could not be recorded this time; retry later."""


class CaptureBlocked(ItemFailed):
    """The page plays, but its video reaches screen capture as black: the
    player uses protected playback. Retrying cannot change that, and playcap
    does not work around it (see RESPONSIBLE_USE.md)."""


@dataclass
class Item:
    id: str
    title: str
    url: str
    kind: str = "video"
    locked: bool = False
    aired_at: Optional[datetime] = None   # None = no date known, never "future"
    day: Any = None                       # display strings for the plan output
    time: Any = None
    raw: dict = field(default_factory=dict)


@dataclass
class Player:
    """Where the <video> lives: a CDP session plus the JS expression that
    returns the element inside it."""
    session: Any
    video: str = VIDEO
    src: str = ""


# Formatted with Player.video (the expression that yields the element).
VIDEO_STATE_JS = r"""
JSON.stringify((() => {
  const v = %s;
  if (!v) return {found: false};
  return {found: true, t: v.currentTime, duration: v.duration, paused: v.paused,
          ended: v.ended, readyState: v.readyState, rate: v.playbackRate,
          w: v.videoWidth, h: v.videoHeight};
})())
"""


class Adapter:
    """Override what your site needs; the defaults suit a plain HTML5 page."""

    #: merged under config.json (config.json wins)
    config_defaults: dict = {}

    #: how the setup UI names this source, and the few fields it asks for:
    #: [{"key", "label", "kind": "text" | "url" | "links", "help"?}]
    label: str = "Custom adapter"
    setup_fields: list = []

    #: False when build_queue reads only local files (no browser, no login),
    #: so the UI can rebuild the queue by itself right after settings are saved
    queue_needs_browser: bool = True

    #: wording used in plans and the dashboard
    labels = {
        "locked": "locked",
        "locked_hint": "rerun build_queue.py once they unlock",
        "locked_pill": "locked",
        "locked_heading": "Locked",
        "locked_heading_hint": "rerun build_queue.py once they unlock",
        "status_title": "playcap pipeline",
        "page_title": "playcap status",
        "item": "Item",
    }

    cfg: dict = {}

    # --- queue -------------------------------------------------------------
    def build_queue(self, cfg, argv):
        raise NotImplementedError

    def item(self, raw):
        when = None
        if raw.get("aired_at"):
            try:
                when = datetime.fromisoformat(raw["aired_at"])
            except ValueError:
                pass
        return Item(id=str(raw["id"]), title=raw.get("title") or raw["url"],
                    url=raw["url"], kind=raw.get("kind", "video"),
                    locked=bool(raw.get("locked")), aired_at=when,
                    day=when.strftime("%d %b %Y") if when else "-",
                    time=when.strftime("%I:%M %p") if when else "",
                    raw=raw)

    # --- page and player ---------------------------------------------------
    def page_target(self, cfg, item):
        """Reuse an open tab; a freshly launched browser has only a blank one."""
        pages = [t for t in cdp.targets(("page",))
                 if not t["url"].startswith(("devtools://", "chrome-extension://"))]
        return pages[0] if pages else cdp.open_page(item.url)

    def check_page(self, sess, item):
        pass

    def find_player(self, sess):
        raise NotImplementedError

    def attach_player(self, sess, rect):
        raise NotImplementedError

    def reattach_player(self, sess, player):
        return None

    def fullscreen_js(self, rect):
        raise NotImplementedError

    # --- browser -------------------------------------------------------------
    def browser_command(self):
        return [sys.executable, "-u", "-m", "playcap.browser"]

    def relaunch_browser(self, cfg):
        """Start the debug browser again, detached. The recorder then polls the
        port until it answers."""
        subprocess.Popen(self.browser_command(), cwd=str(Path.cwd()),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
