import json
import threading
import time
import urllib.error

import pytest

from playcap import notify, settings

CFG = {"notify_ntfy_topic": "secret-topic-123", "notify_discord_webhook":
       "https://discord.com/api/webhooks/1/SECRETTOKEN", "notify_webhook_url":
       "https://hooks.example/abc?key=SECRETQ"}


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_http(method, url, body=None, headers=None, timeout=notify.TIMEOUT):
        seen.append({"method": method, "url": url, "body": body, "headers": headers or {}})
        return 200
    monkeypatch.setattr(notify, "_http", fake_http)
    return seen


# --- formatting --------------------------------------------------------------------
def test_format_recorded_uses_file_name_not_folder():
    title, msg = notify.format_event("recorded", {
        "title": "Keynote", "duration": 3725, "file": r"D:\Lib\Show\01 - Keynote.mp4", "gb": 1.234})
    assert title == "Recorded: Keynote"
    assert "1 h 02 min" in msg and "1.23 GB" in msg and "01 - Keynote.mp4" in msg
    assert "Lib" not in msg


def test_format_failed_blocked_finished():
    assert notify.format_event("failed", {"title": "A", "reason": "stalled"}) == ("Failed: A", "stalled")
    assert notify.format_event("blocked", {"title": "B"})[0] == "Capture blocked: B"
    t, m = notify.format_event("finished", {"recorded": 3, "failed": 1, "blocked": 0})
    assert t == "playcap run finished" and m == "3 recorded, 1 failed, 0 blocked"
    t, m = notify.format_event("finished", {"recorded": 0, "stopped": True, "note": "disk full"})
    assert t == "playcap run stopped" and m.endswith("disk full")


def test_format_survives_odd_duration():
    for d in (None, float("inf"), "x", 0):
        notify.format_event("recorded", {"title": "x", "duration": d, "file": "f.mp4"})


def test_redact_blanks_secrets_and_queries():
    text = "POST https://discord.com/api/webhooks/1/SECRETTOKEN and secret-topic-123 " \
           "https://hooks.example/abc?key=SECRETQ"
    out = notify.redact(text, CFG)
    assert "SECRETTOKEN" not in out and "secret-topic-123" not in out and "SECRETQ" not in out


# --- channels ------------------------------------------------------------------------
def test_channels_follow_config():
    assert notify.channels({}) == []
    names = [n for n, _ in notify.channels({**CFG, "notify_desktop": True})]
    assert names == ["ntfy", "Discord", "webhook", "desktop"]


def test_send_reaches_each_channel_with_right_shape(calls):
    n = notify.Notifier(CFG, threaded=False)
    assert n.send("failed", title="Talk @everyone", reason="boom") == 3
    ntfy, discord, hook = calls
    assert ntfy["url"] == "https://ntfy.sh/" and ntfy["body"]["topic"] == "secret-topic-123"
    assert ntfy["body"]["title"] == "Failed: Talk @everyone"
    assert discord["body"]["allowed_mentions"] == {"parse": []}
    assert hook["body"]["event"] == "failed" and hook["body"]["data"]["reason"] == "boom"


def test_webhook_payload_is_json_safe(calls):
    notify.Notifier({"notify_webhook_url": "https://h.example/x"}, threaded=False).send(
        "recorded", title="t", duration=float("inf"), file=r"C:\a\b.mp4", gb=1.0)
    body = calls[0]["body"]
    json.dumps(body, allow_nan=False)
    assert body["data"]["file_name"] == "b.mp4" and "file" not in body["data"]


def test_events_filter(calls):
    n = notify.Notifier({**CFG, "notify_events": ["finished"]}, threaded=False)
    assert n.send("recorded", title="x") == 0 and calls == []
    assert n.send("finished", recorded=1) == 3


def test_ntfy_token_header(calls):
    notify.Notifier({"notify_ntfy_topic": "t", "notify_ntfy_token": "tk_abc",
                     "notify_ntfy_server": "https://ntfy.example/"}, threaded=False).send("finished")
    assert calls[0]["url"] == "https://ntfy.example/"
    assert calls[0]["headers"]["Authorization"] == "Bearer tk_abc"


# --- failure isolation ------------------------------------------------------------------
def test_failing_channel_never_raises_and_logs_once_without_secrets(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("refused https://discord.com/api/webhooks/1/SECRETTOKEN")
    monkeypatch.setattr(notify, "_http", boom)
    logged = []
    n = notify.Notifier(CFG, log=logged.append, threaded=False)
    for _ in range(3):
        n.send("failed", title="x", reason="y")
    assert len(logged) == 3                     # one line per channel, not per send
    assert all("SECRETTOKEN" not in line and "secret-topic-123" not in line for line in logged)


def test_hung_channel_does_not_block_the_caller(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(notify, "_http", lambda *a, **k: release.wait(5))
    n = notify.Notifier({"notify_webhook_url": "https://h.example/x"})
    t0 = time.time()
    n.send("recorded", title="x")
    assert time.time() - t0 < 0.5
    t0 = time.time()
    n.flush(seconds=0.3)                         # bounded wait
    assert time.time() - t0 < 1.5
    release.set()


def test_desktop_toast_failure_is_swallowed(monkeypatch):
    def broken(*a, **k):
        raise FileNotFoundError("powershell.exe")
    monkeypatch.setattr(notify.subprocess, "run", broken)
    monkeypatch.setattr(notify.sys, "platform", "win32")
    n = notify.Notifier({"notify_desktop": True}, log=lambda *_: None, threaded=False)
    assert n.send("finished", recorded=1) == 1


def test_toast_text_goes_through_env_not_script(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    monkeypatch.setattr(notify.sys, "platform", "win32")
    notify._run_toast("'; Remove-Item x; '", "body")
    assert "Remove-Item" not in " ".join(seen["cmd"])
    assert seen["env"]["PLAYCAP_TOAST_TITLE"] == "'; Remove-Item x; '"


def test_send_test_reports_per_channel(monkeypatch):
    def http(method, url, *a, **k):
        if "discord" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return 200
    monkeypatch.setattr(notify, "_http", http)
    ok, msg = notify.send_test(CFG)
    assert not ok and "ntfy" in msg and "Discord (HTTPError" in msg
    assert "SECRETTOKEN" not in msg
    assert notify.send_test({})[0] is False


# --- media-server refresh -------------------------------------------------------------------
MEDIA = {"jellyfin_url": "http://jf:8096/", "jellyfin_api_key": "JFKEY",
         "plex_url": "http://plex:32400", "plex_token": "PLEXTOK", "plex_section_id": "2",
         "library_refresh_minutes": 10}


def test_refresh_endpoints(calls):
    r = notify.LibraryRefresher(MEDIA, threaded=False)
    r.item_filed()
    jf, plex = calls
    assert jf["method"] == "POST" and jf["url"] == "http://jf:8096/Library/Refresh"
    assert jf["headers"]["Authorization"] == 'MediaBrowser Token="JFKEY"'
    assert jf["headers"]["X-Emby-Token"] == "JFKEY"
    assert plex["method"] == "POST"
    assert plex["url"] == "http://plex:32400/library/sections/2/refresh"
    assert plex["headers"]["X-Plex-Token"] == "PLEXTOK"
    assert notify.plex_url({**MEDIA, "plex_section_id": ""}).endswith("/sections/all/refresh")


def test_plex_falls_back_to_get_on_404(monkeypatch):
    seen = []

    def http(method, url, body=None, headers=None, timeout=5):
        seen.append(method)
        if method == "POST":
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return 200
    monkeypatch.setattr(notify, "_http", http)
    notify.LibraryRefresher({"plex_url": "http://p", "plex_token": "t"}, threaded=False).item_filed()
    assert seen == ["POST", "GET"]


def test_refresh_debounce(calls):
    now = [1000.0]
    r = notify.LibraryRefresher({"jellyfin_url": "http://jf", "jellyfin_api_key": "k",
                                 "library_refresh_minutes": 10},
                                threaded=False, clock=lambda: now[0])
    r.item_filed()                 # first: immediate
    now[0] += 60
    r.item_filed()                 # within 10 min: deferred
    now[0] += 120
    r.item_filed()
    assert len(calls) == 1 and r.pending
    now[0] += 600
    r.item_filed()                 # window passed: fires
    assert len(calls) == 2 and not r.pending
    r.finish()                     # nothing new since: no extra refresh
    assert len(calls) == 2
    now[0] += 5
    r.item_filed()
    r.finish()                     # one more at the end of the run
    assert len(calls) == 3


def test_refresh_off_without_config(calls):
    r = notify.LibraryRefresher({"jellyfin_url": "http://jf"}, threaded=False)   # no key
    r.item_filed()
    r.finish()
    assert calls == []


def test_refresh_failure_isolated(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(notify, "_http", boom)
    logged = []
    r = notify.LibraryRefresher(MEDIA, log=logged.append, threaded=False)
    r.item_filed()
    r.finish()
    assert len(logged) == 2 and all("JFKEY" not in s and "PLEXTOK" not in s for s in logged)


# --- validation -------------------------------------------------------------------------------
def test_validate():
    assert notify.validate({}) == {}
    assert notify.validate({**CFG, **MEDIA, "notify_desktop": True,
                            "notify_events": ["failed"]}) == {}
    bad = notify.validate({"notify_ntfy_server": "ftp://x", "notify_ntfy_topic": "has space",
                           "notify_discord_webhook": "https://example.com/hook",
                           "notify_webhook_url": "nope", "notify_desktop": "yes",
                           "notify_events": ["recorded", "bogus"], "jellyfin_url": "jf:8096",
                           "plex_section_id": "music", "library_refresh_minutes": 0,
                           "plex_token": 'a"b'})
    assert set(bad) == {"notify_ntfy_server", "notify_ntfy_topic", "notify_discord_webhook",
                        "notify_webhook_url", "notify_desktop", "notify_events", "jellyfin_url",
                        "plex_section_id", "library_refresh_minutes", "plex_token"}


def test_public_view_hides_secrets():
    view = notify.public_view({**CFG, **MEDIA})
    blob = json.dumps(view)
    for secret in ("secret-topic-123", "SECRETTOKEN", "SECRETQ", "JFKEY", "PLEXTOK"):
        assert secret not in blob
    assert view["has_plex_token"] and view["has_notify_ntfy_topic"]
    assert not view["has_notify_ntfy_token"]


# --- settings.save ----------------------------------------------------------------------------
def test_settings_save_secrets_keep_and_remove(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"show": "S", "plex_token": "OLD"}))
    cfg, errors = settings.save(tmp_path, {"plex_token": "", "plex_url": "http://p:32400",
                                           "library_refresh_minutes": "15"})
    assert errors == {}
    disk = json.loads((tmp_path / "config.json").read_text())
    assert disk["plex_token"] == "OLD" and disk["library_refresh_minutes"] == 15
    cfg, errors = settings.save(tmp_path, {"plex_token": None, "jellyfin_api_key": ""})
    disk = json.loads((tmp_path / "config.json").read_text())
    assert errors == {} and "plex_token" not in disk and "jellyfin_api_key" not in disk
    cfg, errors = settings.save(tmp_path, {"plex_token": "NEW"})
    assert json.loads((tmp_path / "config.json").read_text())["plex_token"] == "NEW"


def test_recorder_run_end_quiet_when_nothing_happened_and_never_raises(calls):
    from playcap import recorder
    cfg = {"notify_webhook_url": "https://h.example/x", "jellyfin_url": "http://jf",
           "jellyfin_api_key": "k"}
    idle = {"recorded": 0, "failed": 0, "blocked": 0, "stopped": False, "note": ""}
    recorder.run_end(notify.Notifier(cfg, threaded=False),
                     notify.LibraryRefresher(cfg, threaded=False), idle)
    assert calls == []
    r = notify.LibraryRefresher(cfg, threaded=False)
    r.pending = True
    recorder.run_end(notify.Notifier(cfg, threaded=False), r, {**idle, "recorded": 2})
    assert [c["url"] for c in calls] == ["http://jf/Library/Refresh", "https://h.example/x"]
    assert calls[1]["body"]["event"] == "finished"

    class Broken:
        def finish(self):
            raise RuntimeError("x")
    recorder.run_end(Broken(), Broken(), idle)          # swallowed


def test_black_failures_are_flagged():
    from playcap import recorder
    from playcap.adapters.base import ItemFailed
    exc = recorder.black_failure("capture has been black")
    assert isinstance(exc, ItemFailed) and exc.black


def test_settings_save_rejects_bad_automation_without_writing(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"show": "S"}))
    _, errors = settings.save(tmp_path, {"schedule_mode": "weekly", "notify_webhook_url": "x"})
    assert set(errors) == {"schedule_mode", "notify_webhook_url"}
    assert json.loads((tmp_path / "config.json").read_text()) == {"show": "S"}
