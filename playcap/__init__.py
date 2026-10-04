"""playcap -- an open-source browser DVR.

Give it a queue of pages that play video. It opens each one in your own
debuggable Chrome, starts playback with a trusted input event, records the
screen with OBS until the <video> element itself says it has ended, and files
the result as a verified, compressed media library.

Everything that knows about a particular site lives in an adapter
(playcap.adapters). The core never names a site.
"""
__version__ = "0.1.0"
