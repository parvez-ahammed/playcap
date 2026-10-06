"""Make sure the screen OBS records actually shows the browser.

Two live failures, both silent until someone looked at the file:

1. OBS captured the wrong monitor (or none: an unset Windows monitor_capture
   records solid black). On a machine with a portrait primary and a landscape
   second screen, "the primary display" is the wrong guess. So before each
   item the capture is aimed at whichever monitor holds the browser window,
   read from CDP (Browser.getWindowForTarget), not guessed.

2. Another window sat on top of the fullscreened browser and was recorded
   instead of the video. Windows will not let a background process take the
   foreground, and Page.bringToFront only switches tabs. So on Windows the
   browser window is pinned topmost for the length of the item and unpinned
   afterwards. The window is found by giving the page a unique title for a
   moment -- the user's everyday Chrome is chrome.exe too, so matching on the
   process or class would grab the wrong window.

Everything here degrades to a no-op where it cannot work (other platforms, a
capture source that is not ours, no window found); the recorder's black-frame
checks are the backstop.
"""
import re
import secrets
import sys
import time

_GEOM = re.compile(r"(\d+)x(\d+)\s*@\s*(-?\d+)\s*,\s*(-?\d+)")


def window_bounds(sess):
    """Screen rectangle of the browser window holding this tab, or None."""
    try:
        b = sess.call("Browser.getWindowForTarget").get("bounds", {})
    except Exception:
        return None
    if not {"left", "top", "width", "height"} <= b.keys():
        return None
    return b


def monitor_geometry(item_name):
    """'MSI G241V: 1920x1080 @ -1920,8' -> (x, y, w, h) or None."""
    m = _GEOM.search(item_name or "")
    if not m:
        return None
    w, h, x, y = (int(g) for g in m.groups())
    return x, y, w, h


def monitor_for(bounds, items):
    """The OBS monitor item whose area holds the centre of `bounds`.
    Maximized windows overhang their monitor by a few pixels, so the centre
    is used, not the corners."""
    cx = bounds["left"] + bounds["width"] / 2
    cy = bounds["top"] + bounds["height"] / 2
    for it in items:
        g = monitor_geometry(it.get("itemName"))
        if g and g[0] <= cx < g[0] + g[2] and g[1] <= cy < g[1] + g[3]:
            return it
    return None


def aim_capture(obs, bounds, name=None):
    """Point our monitor capture at the monitor showing `bounds`.
    -> description of the change, or None when nothing was (or could be) done."""
    from playcap import obs_setup
    name = name or obs_setup.CAPTURE_NAME
    if not bounds:
        return None
    try:
        inputs = {i["inputName"]: i.get("inputKind")
                  for i in obs.request("GetInputList").get("inputs", [])}
        prop = obs_setup.MONITOR_PROP.get(inputs.get(name))
        if not prop:
            return None
        items = obs.request("GetInputPropertiesListPropertyItems",
                            {"inputName": name, "propertyName": prop}).get("propertyItems", [])
        target = monitor_for(bounds, items)
        if not target:
            return None
        current = obs.request("GetInputSettings", {"inputName": name}) \
                     .get("inputSettings", {}).get(prop)
        if current == target["itemValue"]:
            return None
        obs.request("SetInputSettings", {"inputName": name,
                                         "inputSettings": {prop: target["itemValue"]}})
        time.sleep(1.0)       # let OBS start producing frames from the new source
        return f"capture moved to {target.get('itemName', 'the browser monitor')}"
    except Exception:
        return None


# --- keeping the browser on top (Windows) ------------------------------------
class Pin:
    """Topmost pin for one browser window. Use as a context manager; every
    failure is swallowed and reported through .note."""

    def __init__(self, sess):
        self.sess = sess
        self.hwnd = None
        self.note = ""

    def __enter__(self):
        if not sys.platform.startswith("win"):
            return self
        try:
            self.hwnd = _find_by_title(self.sess)
            if self.hwnd:
                _set_topmost(self.hwnd, True)
                _to_foreground(self.hwnd)
                self.note = "browser window pinned on top"
            else:
                self.note = "browser window not found; not pinned"
        except Exception as exc:
            self.hwnd = None
            self.note = f"could not pin the browser window ({exc.__class__.__name__})"
        return self

    def __exit__(self, *exc):
        if self.hwnd:
            try:
                _set_topmost(self.hwnd, False)
            except Exception:
                pass
        return False


def _find_by_title(sess, wait=3.0):
    marker = "playcap-" + secrets.token_hex(6)
    old = sess.js("document.title")
    sess.js(f"document.title = {marker!r}")
    try:
        deadline = time.time() + wait
        while time.time() < deadline:
            hwnd = _window_with_title_prefix(marker)
            if hwnd:
                return hwnd
            time.sleep(0.2)
        return None
    finally:
        try:
            sess.js(f"document.title = {old!r}" if isinstance(old, str) else "0")
        except Exception:
            pass


def _user32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
                              wintypes.LPARAM]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
    return ctypes, wintypes, u


def _window_with_title_prefix(prefix):
    ctypes, wintypes, u = _user32()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        buf = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, buf, 512)
        if buf.value.startswith(prefix):
            found.append(hwnd)
            return False
        return True

    u.EnumWindows(visit, 0)
    return found[0] if found else None


HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040


def _set_topmost(hwnd, on):
    ctypes, wintypes, u = _user32()
    after = wintypes.HWND(HWND_TOPMOST if on else HWND_NOTOPMOST)
    if not u.SetWindowPos(hwnd, after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW):
        raise OSError(ctypes.get_last_error(), "SetWindowPos failed")


def _to_foreground(hwnd):
    # Windows only hands the foreground to a process that just saw input;
    # a synthetic Alt tap is the documented-in-practice way to qualify.
    ctypes, wintypes, u = _user32()
    VK_MENU, KEYUP = 0x12, 0x0002
    u.keybd_event(VK_MENU, 0, 0, 0)
    u.keybd_event(VK_MENU, 0, KEYUP, 0)
    u.SetForegroundWindow(hwnd)
