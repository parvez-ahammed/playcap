"""Probe a page with video to find how the video is delivered.

Attaches to an already-running Chrome that was started with
--remote-debugging-port=9222 (so it keeps your logged-in session),
opens the page URL, presses play, and records every network request
plus what the <video> element actually points at.

Usage:  python -m playcap.tools.probe "<page-url>"
"""
import json
import re
import sys
import time

import requests
import websocket

CDP_HTTP = "http://127.0.0.1:9222"
WATCH_SECONDS = 25

MEDIA_RE = re.compile(
    r"\.(m3u8|mpd|mp4|m4s|ts|webm|key)(\?|$)|widevine|playready|drm|licen[cs]e|token",
    re.I,
)

PROBE_JS = r"""
(() => {
  const out = {frame: location.href, videos: [], iframes: [], eme: !!window.__emeUsed};
  for (const v of document.querySelectorAll('video')) {
    out.videos.push({
      currentSrc: v.currentSrc,
      src: v.getAttribute('src'),
      duration: v.duration,
      readyState: v.readyState,
      usingMSE: (v.currentSrc || '').startsWith('blob:'),
      hasMediaKeys: !!v.mediaKeys,
      sources: [...v.querySelectorAll('source')].map(s => ({src: s.src, type: s.type})),
    });
  }
  for (const f of document.querySelectorAll('iframe')) out.iframes.push(f.src);
  out.resources = performance.getEntriesByType('resource')
    .map(r => r.name)
    .filter(n => /\.(m3u8|mpd|mp4|m4s|ts|webm|key)(\?|$)|licen|drm|token/i.test(n));
  return JSON.stringify(out);
})()
"""

PLAY_JS = r"""
(() => {
  const v = document.querySelector('video');
  if (v) { v.muted = true; v.play(); return 'played video el'; }
  const btn = document.querySelector(
    '.plyr__control--overlaid, .vjs-big-play-button, button[aria-label*="Play" i], .play-button');
  if (btn) { btn.click(); return 'clicked ' + btn.className; }
  return 'no play target found';
})()
"""

# Hook EME so we can tell if the site is asking for DRM keys.
EME_HOOK_JS = r"""
(() => {
  window.__emeUsed = false;
  const orig = navigator.requestMediaKeySystemAccess;
  if (orig) {
    navigator.requestMediaKeySystemAccess = function (...a) {
      window.__emeUsed = a[0];
      return orig.apply(this, a);
    };
  }
})()
"""


class CDP:
    def __init__(self, ws_url):
        # Chrome rejects the handshake if an Origin header is present unless it was
        # launched with --remote-allow-origins, so send none at all.
        self.ws = websocket.create_connection(
            ws_url, max_size=None, timeout=30, suppress_origin=True)
        self.msg_id = 0
        self.events = []

    def send(self, method, params=None, session_id=None):
        self.msg_id += 1
        payload = {"id": self.msg_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self.ws.send(json.dumps(payload))
        return self.msg_id

    def wait_for(self, want_id, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.2, deadline - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                break
            if "id" in msg:
                if msg["id"] == want_id:
                    return msg
            else:
                self.events.append(msg)
        return None

    def pump(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.ws.settimeout(max(0.2, deadline - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                continue
            if "id" not in msg:
                self.events.append(msg)


def main(url):
    try:
        targets = requests.get(f"{CDP_HTTP}/json/new?{url}", timeout=10,
                               headers={"Content-Type": "application/json"})
        tab = targets.json() if targets.ok else None
        if not tab or "webSocketDebuggerUrl" not in tab:
            raise RuntimeError(targets.text[:300])
    except Exception as exc:
        # Newer Chrome requires PUT for /json/new
        tab = requests.put(f"{CDP_HTTP}/json/new?{url}", timeout=10).json()

    cdp = CDP(tab["webSocketDebuggerUrl"])
    cdp.send("Page.enable")
    cdp.send("Network.enable", {"maxTotalBufferSize": 100_000_000})
    cdp.send("Target.setAutoAttach",
             {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
    cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": EME_HOOK_JS})
    cdp.send("Page.navigate", {"url": url})

    print(f"[probe] loading {url}")
    cdp.pump(8)

    # Turn on Network for any out-of-process iframe that attached.
    sessions = [e["params"]["sessionId"] for e in cdp.events
                if e.get("method") == "Target.attachedToTarget"]
    for sid in sessions:
        cdp.send("Network.enable", {}, session_id=sid)
    if sessions:
        print(f"[probe] attached to {len(sessions)} sub-target(s) (iframes/workers)")

    rid = cdp.send("Runtime.evaluate", {"expression": PLAY_JS, "awaitPromise": False})
    res = cdp.wait_for(rid, timeout=10)
    print("[probe] play:", (res or {}).get("result", {}).get("result", {}).get("value"))

    print(f"[probe] watching network for {WATCH_SECONDS}s ...")
    cdp.pump(WATCH_SECONDS)

    urls = []
    for e in cdp.events:
        if e.get("method") == "Network.requestWillBeSent":
            urls.append(e["params"]["request"]["url"])
    media = [u for u in dict.fromkeys(urls) if MEDIA_RE.search(u)]

    rid = cdp.send("Runtime.evaluate", {"expression": PROBE_JS, "returnByValue": True})
    res = cdp.wait_for(rid, timeout=10)
    page_info = (res or {}).get("result", {}).get("result", {}).get("value")

    report = {
        "url": url,
        "total_requests": len(urls),
        "media_requests": media[:80],
        "page": json.loads(page_info) if page_info else None,
        "unique_hosts": sorted({u.split("/")[2] for u in urls if "://" in u}),
    }
    with open("probe-report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print("\n=== REPORT (also saved to probe-report.json) ===")
    print(json.dumps(report, indent=2)[:6000])


if __name__ == "__main__":
    main(sys.argv[1])
