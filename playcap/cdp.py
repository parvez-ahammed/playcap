"""Small Chrome DevTools Protocol client.

Enough to drive a real logged-in Chrome: find targets (including cross-origin
iframes such as an embedded video player), run JS inside them, and dispatch input
events. CDP input events are treated as genuine user gestures, which is what
lets us press play on players that refuse script-started playback, and enter
fullscreen programmatically. Nothing here touches the media stream itself.
"""
import json
import time

import requests
import websocket

CDP_HTTP = "http://127.0.0.1:9222"


def set_port(port):
    """Talk to the browser on config's chrome_debug_port (config.load calls this),
    so a browser launched on another port is the one that gets driven."""
    global CDP_HTTP
    CDP_HTTP = f"http://127.0.0.1:{int(port)}"


class CdpError(RuntimeError):
    pass


def targets(kinds=("page", "iframe")):
    try:
        return [t for t in requests.get(f"{CDP_HTTP}/json/list", timeout=8).json()
                if t["type"] in kinds]
    except Exception as exc:
        raise CdpError(
            f"Chrome debug port unreachable at {CDP_HTTP}: {exc}\n"
            "Start the debug browser first (python -m playcap.browser, or your "
            "adapter's own launcher)."
        ) from exc


def find(substr, kinds=("page", "iframe"), tries=1, delay=1.0):
    """First target whose URL contains substr, retrying while the page settles."""
    for _ in range(tries):
        for t in targets(kinds):
            if substr in t["url"]:
                return t
        time.sleep(delay)
    return None


def open_page(url, tries=20, delay=0.5):
    """Open a fresh tab on url and return its target.

    A newly launched debug Chrome has nothing but a blank tab, so find() comes
    back empty and the caller would blow up on target["webSocketDebuggerUrl"].
    Chrome 111+ only accepts PUT on /json/new.
    """
    try:
        requests.put(f"{CDP_HTTP}/json/new?{url}", timeout=10)
    except Exception as exc:
        raise CdpError(f"could not open a tab on {url}: {exc}") from exc
    host = url.split("//", 1)[-1].split("/", 1)[0]
    for _ in range(tries):
        t = find(host, kinds=("page",))
        if t:
            return t
        time.sleep(delay)
    raise CdpError(f"opened a tab on {url} but it never appeared in /json/list")


class Session:
    def __init__(self, target):
        self.target = target
        self.ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], max_size=None,
            timeout=20, suppress_origin=True)
        self.msg_id = 0

    @property
    def url(self):
        return self.target["url"]

    def call(self, method, params=None, timeout=20):
        self.msg_id += 1
        self.ws.send(json.dumps(
            {"id": self.msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.2, deadline - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("id") == self.msg_id:
                if "error" in msg:
                    raise CdpError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise CdpError(f"{method} timed out")

    def js(self, expression, timeout=20):
        """Evaluate JS, return the value. Wrap objects in JSON.stringify yourself
        for anything non-primitive."""
        res = self.call("Runtime.evaluate",
                        {"expression": expression, "returnByValue": True,
                         "awaitPromise": True}, timeout=timeout)
        if res.get("exceptionDetails"):
            raise CdpError(f"JS threw: {res['exceptionDetails'].get('text')}")
        return res.get("result", {}).get("value")

    def js_json(self, expression, **kw):
        raw = self.js(expression, **kw)
        return json.loads(raw) if isinstance(raw, str) else raw

    def navigate(self, url, settle=6.0):
        self.call("Page.enable")
        self.call("Page.navigate", {"url": url})
        time.sleep(settle)

    def click(self, x, y):
        """Trusted click at viewport coords -- counts as a user gesture."""
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "left",
                "clickCount": 1, "buttons": 1 if kind == "mousePressed" else 0})
            time.sleep(0.05)

    def key(self, key, code=None, vk=None):
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {
                "type": kind, "key": key, "code": code or f"Key{key.upper()}",
                "windowsVirtualKeyCode": vk or ord(key.upper()),
                "nativeVirtualKeyCode": vk or ord(key.upper()),
                "text": key if kind == "keyDown" and len(key) == 1 else ""})
            time.sleep(0.05)

    def bring_to_front(self):
        try:
            self.call("Page.bringToFront")
        except CdpError:
            pass

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
