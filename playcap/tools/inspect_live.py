"""Inspect whatever is open and playing in the debug browser.

Avoids all auth guesswork: instead of opening its own tab, this walks every
page and iframe target the debug Chrome exposes (cross-origin player iframes
are separate targets) and reports what any <video> element there exposes.
Whatever it finds is what the recorder can use to detect end-of-video.

Usage:  python -m playcap.tools.inspect_live            # every target
        python -m playcap.tools.inspect_live player.example  # URL substring filter
"""
import sys

from playcap import cdp

VIDEO_JS = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return JSON.stringify({frame: location.href, video: null});
  return JSON.stringify({
    frame: location.href,
    video: {
      currentTime: v.currentTime,
      duration: v.duration,
      paused: v.paused,
      ended: v.ended,
      readyState: v.readyState,
      currentSrc: (v.currentSrc || '').slice(0, 120),
      hasMediaKeys: !!v.mediaKeys,
      keySystem: v.mediaKeys ? v.mediaKeys.keySystem : null,
      videoWidth: v.videoWidth,
      videoHeight: v.videoHeight,
      buffered: v.buffered.length ? v.buffered.end(v.buffered.length - 1) : 0,
    },
  });
})()
"""


def main(filt=""):
    targets = [t for t in cdp.targets(("page", "iframe")) if filt in t["url"]]
    if not targets:
        print("No matching page/iframe target. Open the page and press play first.")
        return
    for t in targets:
        print(f"\n=== {t['type']} :: {t['url'][:100]}")
        try:
            s = cdp.Session(t)
            print("   ", s.js(VIDEO_JS, timeout=10))
            s.close()
        except Exception as exc:
            print(f"    (could not inspect: {exc.__class__.__name__}: {exc})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
