"""Tell the user what an unattended run did, and nudge their media server.

    Notifier(cfg).send(event, **data)   recorded | failed | blocked | finished
    LibraryRefresher(cfg).item_filed()  after each recording is filed
    LibraryRefresher(cfg).finish()      once at the end of a run
    send_test(cfg)                      the UI's "Send test notification"
    validate(partial)                   settings.py's checks for these keys

Channels, each optional and switched on by filling in its config keys:

    ntfy        notify_ntfy_server (default https://ntfy.sh) + notify_ntfy_topic
                (+ notify_ntfy_token for a server that needs a login). Published
                as JSON to the server root, so titles with any characters work
                without RFC 2047 header encoding.
    Discord     notify_discord_webhook (a channel's webhook URL).
    webhook     notify_webhook_url: a JSON POST {event, title, message, data, time}
                for anything else (Home Assistant, n8n, a script).
    desktop     notify_desktop: a Windows toast. No new dependency: Windows
                PowerShell 5.1's own WinRT bridge (Windows.UI.Notifications),
                shown under PowerShell's app id. pwsh 7 has no WinRT bridge, so
                it is always powershell.exe. Title and text reach the script
                through environment variables, never through the script text,
                so a video title cannot inject PowerShell. Anywhere that does
                not work (not Windows, PowerShell missing, toasts off) it does
                nothing.

notify_events picks which events are sent (default: all four).

Media-server refresh, so a finished recording shows up without a manual scan:

    Jellyfin / Emby  POST {jellyfin_url}/Library/Refresh (Jellyfin's RefreshLibrary,
                     answers 204). The key goes in the Authorization header as
                     `MediaBrowser Token="..."` -- Jellyfin 12 turned the legacy
                     X-Emby-Token header off by default -- and also as
                     X-Emby-Token, which is what Emby reads.
    Plex             POST {plex_url}/library/sections/{plex_section_id}/refresh
                     with an X-Plex-Token header (the documented method; a blank
                     section id refreshes "all"). Some Plex releases answered POST
                     with 404 while the legacy GET kept working, so a 404/405 is
                     retried once as GET.

A batch files one recording every hour or two, and a library scan is not free,
so refreshes are debounced: at most one per library_refresh_minutes (default
10), plus one at the end of the run if anything was filed since the last one.

Failure isolation is the point of this module's shape. A notifier runs on the
recorder's thread budget, and the recorder must never crash, stall or lose an
item because a webhook is down. So: every send runs in a daemon thread with a
short timeout (TIMEOUT), every exception is caught, and each channel logs its
first failure once per process and then stays quiet. flush() waits a bounded
time at the end of a run so the "run finished" message gets out before exit.

Secrets (webhook URLs, the ntfy topic and token, API keys) live in config.json
only. Log lines go through redact(), which blanks every configured secret, and
the UI is sent has_<key> flags instead of the values (public_view).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

EVENTS = ("recorded", "failed", "blocked", "finished")
TIMEOUT = 6            # seconds per HTTP request: a hung server must not hold a run
TOAST_TIMEOUT = 20     # PowerShell's first WinRT load can take a while
FLUSH_SECONDS = 15     # how long the end of a run waits for sends in flight
USER_AGENT = "playcap (+https://github.com/parvez-ahammed/playcap)"   # Discord refuses urllib's default
MAX_TEXT = 1800        # Discord's limit is 2000 characters per message

DEFAULTS = {
    "notify_ntfy_server": "https://ntfy.sh",
    "notify_ntfy_topic": "",
    "notify_ntfy_token": "",
    "notify_discord_webhook": "",
    "notify_webhook_url": "",
    "notify_desktop": False,
    "notify_events": list(EVENTS),
    "jellyfin_url": "",
    "jellyfin_api_key": "",
    "plex_url": "",
    "plex_token": "",
    "plex_section_id": "",
    "library_refresh_minutes": 10,
}
# Never logged, never sent back to the page. An ntfy topic is a password in
# all but name: anyone who knows it can read the messages.
SECRET_KEYS = ("notify_ntfy_topic", "notify_ntfy_token", "notify_discord_webhook",
               "notify_webhook_url", "jellyfin_api_key", "plex_token")
UI_KEYS = set(DEFAULTS)

# The toast script. It reads its text from the environment (see docstring).
TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode($env:PLAYCAP_TOAST_TITLE)) | Out-Null
$n.Item(1).AppendChild($t.CreateTextNode($env:PLAYCAP_TOAST_BODY)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


# --- formatting -------------------------------------------------------------------
def _duration(seconds):
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    if s != s or s in (float("inf"), float("-inf")) or s <= 0:
        return ""
    s = int(round(s))
    h, m = divmod(s // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min {s % 60:02d} s"


def format_event(event, data):
    """-> (title, message). Plain text; each channel adds its own markup."""
    title_of = str(data.get("title") or "an item")[:150]
    if event == "recorded":
        parts = [p for p in (_duration(data.get("duration")),
                             f"{float(data['gb']):.2f} GB" if data.get("gb") is not None else "")
                 if p]
        name = Path(str(data.get("file") or "")).name     # a name, not the user's folder layout
        msg = " · ".join(parts) + (("\n" if parts else "") + name if name else "")
        return f"Recorded: {title_of}", msg or "Saved to the library."
    if event == "failed":
        return f"Failed: {title_of}", str(data.get("reason") or "unknown error")[:500]
    if event == "blocked":
        return (f"Capture blocked: {title_of}",
                str(data.get("reason") or "the capture stayed black")[:500])
    if event == "finished":
        counts = (f"{int(data.get('recorded', 0))} recorded, {int(data.get('failed', 0))} failed, "
                  f"{int(data.get('blocked', 0))} blocked")
        lead = "playcap run stopped" if data.get("stopped") else "playcap run finished"
        note = str(data.get("note") or "").strip()
        return lead, counts + (f"\n{note[:300]}" if note else "")
    if event == "test":
        return "playcap test notification", "If you can read this, notifications work."
    return f"playcap: {event}", json.dumps(data, default=str)[:500]


def redact(text, cfg):
    """Blank every configured secret (and any URL query string) in a log line."""
    text = str(text)
    for key in SECRET_KEYS:
        value = str(cfg.get(key) or "")
        if len(value) >= 4:
            text = text.replace(value, "***")
    return re.sub(r"(https?://[^\s?]+)\?\S+", r"\1?***", text)


def _url_ok(value):
    p = urlparse(str(value))
    return p.scheme in ("http", "https") and bool(p.netloc)


# --- transport ----------------------------------------------------------------------
def _http(method, url, body=None, headers=None, timeout=TIMEOUT):
    """One request; raises on a non-2xx answer. Tests replace this function."""
    data = None if body is None else (body if isinstance(body, bytes)
                                      else json.dumps(body).encode("utf-8"))
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    if data is not None and not isinstance(body, bytes):
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def _run_toast(title, body):
    """Windows only; quietly does nothing elsewhere."""
    if not sys.platform.startswith("win"):
        return
    env = {**os.environ, "PLAYCAP_TOAST_TITLE": title[:200], "PLAYCAP_TOAST_BODY": body[:500]}
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                    "Bypass", "-Command", TOAST_PS], env=env, capture_output=True,
                   timeout=TOAST_TIMEOUT, check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# --- channels: fn(cfg, event, title, message, data) ----------------------------------
def _ntfy(cfg, event, title, message, data):
    server = str(cfg.get("notify_ntfy_server") or DEFAULTS["notify_ntfy_server"]).rstrip("/")
    tags = {"recorded": ["white_check_mark"], "failed": ["warning"],
            "blocked": ["no_entry"], "finished": ["checkered_flag"]}.get(event, [])
    priority = 4 if event in ("failed", "blocked") else 3
    headers = {}
    if cfg.get("notify_ntfy_token"):
        headers["Authorization"] = f"Bearer {cfg['notify_ntfy_token']}"
    _http("POST", server + "/", {"topic": cfg["notify_ntfy_topic"], "title": title,
                                 "message": message, "tags": tags, "priority": priority},
          headers)


def _discord(cfg, event, title, message, data):
    text = f"**{title}**\n{message}"[:MAX_TEXT]
    # allowed_mentions: a video title with "@everyone" in it must not ping a server.
    _http("POST", cfg["notify_discord_webhook"],
          {"content": text, "username": "playcap", "allowed_mentions": {"parse": []}})


def _webhook(cfg, event, title, message, data):
    # The file's name only: the receiver may be a third-party service, and the
    # user's folder layout is none of its business.
    payload = {k: (None if isinstance(v, float) and (v != v or abs(v) == float("inf")) else v)
               for k, v in data.items() if k != "file"}   # inf/NaN are not JSON
    if data.get("file"):
        payload["file_name"] = Path(str(data["file"])).name
    _http("POST", cfg["notify_webhook_url"],
          {"event": event, "title": title, "message": message, "data": payload,
           "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")})


def _desktop(cfg, event, title, message, data):
    _run_toast(title, message)


def channels(cfg):
    """[(name, fn)] for every channel the config switches on."""
    out = []
    if str(cfg.get("notify_ntfy_topic") or "").strip():
        out.append(("ntfy", _ntfy))
    if str(cfg.get("notify_discord_webhook") or "").strip():
        out.append(("Discord", _discord))
    if str(cfg.get("notify_webhook_url") or "").strip():
        out.append(("webhook", _webhook))
    if cfg.get("notify_desktop") is True:
        out.append(("desktop", _desktop))
    return out


# --- dispatch with failure isolation ---------------------------------------------------
class _Dispatcher:
    what = "notification"

    def __init__(self, cfg, log=print, threaded=True):
        self.cfg = dict(cfg or {})
        self.log = log
        self.threaded = threaded
        self._threads = []
        self._logged = set()
        self._lock = threading.Lock()

    def _safe(self, name, fn, *args):
        try:
            fn(*args)
            return True, ""
        except Exception as exc:     # nothing a channel does may reach the recorder
            msg = redact(f"{exc.__class__.__name__}: {exc}", self.cfg)[:200]
            with self._lock:
                first = name not in self._logged
                self._logged.add(name)
            if first:
                try:
                    self.log(f"    {self.what} via {name} failed ({msg}); "
                             f"further {name} errors are not shown")
                except Exception:
                    pass
            return False, msg

    def _dispatch(self, name, fn, *args):
        if not self.threaded:
            return self._safe(name, fn, *args)
        t = threading.Thread(target=self._safe, args=(name, fn, *args), daemon=True,
                             name=f"playcap-{name}")
        t.start()
        self._threads.append(t)
        return None

    def flush(self, seconds=FLUSH_SECONDS):
        """Wait (bounded) for sends in flight. Never raises."""
        deadline = time.time() + seconds
        for t in self._threads:
            t.join(max(0.0, deadline - time.time()))
        self._threads = [t for t in self._threads if t.is_alive()]


class Notifier(_Dispatcher):
    what = "notification"

    def __init__(self, cfg, log=print, threaded=True):
        super().__init__(cfg, log, threaded)
        events = self.cfg.get("notify_events")
        self.events = set(events) if isinstance(events, list) else set(EVENTS)
        self.channels = channels(self.cfg)

    def send(self, event, **data):
        """Fire and forget. -> number of channels it went to."""
        try:
            if event != "test" and event not in self.events:
                return 0
            title, message = format_event(event, data)
            for name, fn in self.channels:
                self._dispatch(name, fn, self.cfg, event, title, message, data)
            return len(self.channels)
        except Exception:          # a stop (KeyboardInterrupt) still goes through
            return 0


def send_test(cfg, log=lambda *_: None):
    """Synchronous, for the UI button: -> (ok, message for a person)."""
    chans = channels(cfg)
    if not chans:
        return False, "No notification channel is set up yet. Fill one in and save first."
    n = _Dispatcher(cfg, log, threaded=False)
    title, message = format_event("test", {})
    results = [(name, *n._safe(name, fn, cfg, "test", title, message, {}))
               for name, fn in chans]
    good = [r[0] for r in results if r[1]]
    bad = [f"{r[0]} ({r[2]})" for r in results if not r[1]]
    if not bad:
        return True, "Sent via " + ", ".join(good) + "."
    return False, ("Sent via " + ", ".join(good) + ". " if good else "") + \
        "Failed: " + "; ".join(bad)


# --- media-server refresh --------------------------------------------------------------
def _jellyfin(cfg):
    key = str(cfg["jellyfin_api_key"])
    _http("POST", str(cfg["jellyfin_url"]).rstrip("/") + "/Library/Refresh", b"",
          {"Authorization": f'MediaBrowser Token="{key}"', "X-Emby-Token": key})


def plex_url(cfg):
    section = str(cfg.get("plex_section_id") or "").strip() or "all"
    return f"{str(cfg['plex_url']).rstrip('/')}/library/sections/{quote(section)}/refresh"


def _plex(cfg):
    url, headers = plex_url(cfg), {"X-Plex-Token": str(cfg["plex_token"])}
    try:
        _http("POST", url, b"", headers)
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 405):
            raise
        _http("GET", url, None, headers)


def refresh_targets(cfg):
    out = []
    if str(cfg.get("jellyfin_url") or "").strip() and str(cfg.get("jellyfin_api_key") or "").strip():
        out.append(("Jellyfin/Emby", _jellyfin))
    if str(cfg.get("plex_url") or "").strip() and str(cfg.get("plex_token") or "").strip():
        out.append(("Plex", _plex))
    return out


class LibraryRefresher(_Dispatcher):
    """Debounced: at most one refresh per library_refresh_minutes, plus one at
    finish() when something was filed after the last one."""
    what = "library refresh"

    def __init__(self, cfg, log=print, threaded=True, clock=time.time):
        super().__init__(cfg, log, threaded)
        self.targets = refresh_targets(self.cfg)
        try:
            self.period = max(0, int(self.cfg.get("library_refresh_minutes", 10))) * 60
        except (TypeError, ValueError):
            self.period = 600
        self.clock = clock
        self.last = None
        self.pending = False
        self.fired = 0

    def item_filed(self):
        try:
            if not self.targets:
                return
            now = self.clock()
            if self.last is None or now - self.last >= self.period:
                self._fire(now)
            else:
                self.pending = True
        except Exception:          # a stop (KeyboardInterrupt) still goes through
            pass

    def finish(self):
        try:
            if self.targets and self.pending:
                self._fire(self.clock())
        except Exception:          # a stop (KeyboardInterrupt) still goes through
            pass

    def _fire(self, now):
        self.last, self.pending = now, False
        self.fired += 1
        for name, fn in self.targets:
            self._dispatch(name, fn, self.cfg)


# --- settings validation ----------------------------------------------------------------
def validate(partial):
    """{key: message} for the keys in partial this module owns. A secret given
    as None means "remove it"; "" means "keep the saved one" (settings.save)."""
    errors = {}
    for key in ("notify_ntfy_server", "notify_webhook_url", "jellyfin_url", "plex_url"):
        v = partial.get(key)
        if key in partial and v not in (None, "") and not _url_ok(v):
            errors[key] = "A web address starting with http:// or https://"
    v = partial.get("notify_ntfy_topic")
    if v and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(v)):
        errors["notify_ntfy_topic"] = "Letters, digits, - and _ only (up to 64)."
    v = partial.get("notify_discord_webhook")
    if v and not (_url_ok(v) and str(v).startswith("https://")
                  and "/api/webhooks/" in str(v)):
        errors["notify_discord_webhook"] = ("Paste the channel's webhook URL "
                                            "(https://discord.com/api/webhooks/...).")
    for key in ("notify_ntfy_token", "jellyfin_api_key", "plex_token"):
        v = partial.get(key)
        if v and (not isinstance(v, str) or any(c in v for c in "\r\n\"")):
            errors[key] = "That does not look like a token."
    if "notify_desktop" in partial and partial["notify_desktop"] not in (True, False):
        errors["notify_desktop"] = "Should be on or off."
    if "notify_events" in partial:
        ev = partial["notify_events"]
        if not isinstance(ev, list) or any(e not in EVENTS for e in ev):
            errors["notify_events"] = "Pick from: " + ", ".join(EVENTS)
    v = partial.get("plex_section_id")
    if v not in (None, "") and not re.fullmatch(r"\d{1,6}|all", str(v).strip()):
        errors["plex_section_id"] = "A library number (e.g. 2), or blank for all."
    if "library_refresh_minutes" in partial:
        v = partial["library_refresh_minutes"]
        try:
            ok = not isinstance(v, bool) and 1 <= int(v) <= 1440
        except (TypeError, ValueError):
            ok = False
        if not ok:
            errors["library_refresh_minutes"] = "A whole number from 1 to 1440."
    return errors


def public_view(cfg):
    """The automation settings for the page: values, with secrets replaced by
    has_<key> flags."""
    out = {k: cfg.get(k, d) for k, d in DEFAULTS.items() if k not in SECRET_KEYS}
    for k in SECRET_KEYS:
        out["has_" + k] = bool(str(cfg.get(k) or "").strip())
    return out
