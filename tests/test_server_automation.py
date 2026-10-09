import json

import pytest

from playcap import notify, schedule
from playcap.ui import server
from tests.test_server import call, token_of, ui  # noqa: F401  (fixture)

SECRETS = {"notify_discord_webhook": "https://discord.com/api/webhooks/1/SECRETTOKEN",
           "plex_token": "PLEXTOK", "notify_ntfy_topic": "secret-topic-123"}


@pytest.fixture
def no_schtasks(monkeypatch):
    calls = []

    def status(root, **kw):
        calls.append("status")
        return {"registered": False, "next_run": None, "name": "t", "supported": True}
    monkeypatch.setattr(schedule, "task_status", status)
    monkeypatch.setattr(schedule, "register", lambda *a, **k: pytest.fail("register called"))
    return calls


def test_automation_get_and_setup_never_leak_secrets(ui, no_schtasks):
    root, port = ui
    (root / "config.json").write_text(json.dumps({"output_dir": "rec", **SECRETS,
                                                  "plex_url": "http://p:32400"}))
    status, info = call(port, "GET", "/api/automation")
    assert status == 200
    blob = json.dumps(info)
    assert "SECRETTOKEN" not in blob and "PLEXTOK" not in blob and "secret-topic-123" not in blob
    assert info["settings"]["has_plex_token"] and info["settings"]["plex_url"] == "http://p:32400"
    status, setup = call(port, "GET", "/api/setup")
    blob = json.dumps(setup)
    assert status == 200 and "SECRETTOKEN" not in blob and "PLEXTOK" not in blob


def test_automation_posts_are_guarded(ui, no_schtasks):
    _, port = ui
    for path in ("/api/automation/save", "/api/automation/test",
                 "/api/schedule/register", "/api/schedule/unregister"):
        assert call(port, "POST", path, {})[0] == 403


def test_automation_save_only_takes_automation_keys(ui, no_schtasks):
    root, port = ui
    (root / "config.json").write_text(json.dumps({"output_dir": "rec", "plex_token": "OLD"}))
    tok = token_of(port)
    status, r = call(port, "POST", "/api/automation/save",
                     {"output_dir": "elsewhere", "plex_url": "http://p"}, token=tok)
    assert status == 200 and not r["ok"] and "output_dir" in r["errors"]
    status, r = call(port, "POST", "/api/automation/save",
                     {"plex_url": "http://p", "plex_token": "", "schedule_mode": "daily",
                      "schedule_time": "04:00"}, token=tok)
    assert r["ok"], r
    disk = json.loads((root / "config.json").read_text())
    assert disk["plex_token"] == "OLD" and disk["schedule_mode"] == "daily"
    assert disk["output_dir"] == "rec"


def test_automation_test_endpoint(ui, monkeypatch, no_schtasks):
    root, port = ui
    (root / "config.json").write_text(json.dumps({"notify_webhook_url": "https://h.example/x"}))
    sent = []
    monkeypatch.setattr(notify, "_http", lambda *a, **k: sent.append(a) or 200)
    status, r = call(port, "POST", "/api/automation/test", {}, token=token_of(port))
    assert status == 200 and r["ok"] and len(sent) == 1


def test_save_reregisters_a_registered_task(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"schedule_mode": "daily",
                                                      "schedule_time": "03:00"}))
    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(schedule, "task_status", lambda root, **k: {"registered": True})
    seen = []
    monkeypatch.setattr(schedule, "register", lambda root, cfg=None, **k:
                        seen.append(cfg["schedule_time"]) or (True, "Scheduled."))
    monkeypatch.setattr(schedule, "unregister", lambda root, **k: seen.append("off") or (True, "Removed."))
    assert server.automation_save(tmp_path, {"schedule_time": "05:00"})["ok"]
    assert server.automation_save(tmp_path, {"notify_desktop": True})["ok"]     # schedule untouched
    assert server.automation_save(tmp_path, {"schedule_mode": "off"})["ok"]
    assert seen == ["05:00", "off"]
