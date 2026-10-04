import json
import time
from datetime import datetime, timedelta

import pytest

from playcap import jobs, state


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(state, "port_open", lambda port: False)
    monkeypatch.setattr(state, "check_obs", lambda url, password: (False, "OBS is not running"))
    monkeypatch.setattr(jobs, "external", lambda max_age=15: {})


def write(path, obj):
    path.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)


def configure(root, out, **extra):
    cfg = {"adapter": "playcap.adapters.html5_video", "output_dir": str(out),
           "show": "Test Show", **extra}
    write(root / "config.json", cfg)
    return cfg


def test_unconfigured_root(tmp_path):
    snap = state.snapshot(tmp_path)
    assert snap["configured"] is False
    assert snap["items"] == [] and snap["library"] == []
    assert snap["problems"][0]["code"] == "setup"


def test_items_get_state_chips(tmp_path):
    out = tmp_path / "Lib Dec'26"
    configure(tmp_path, out)
    future = (datetime.now() + timedelta(days=2)).isoformat(timespec="minutes")
    recent = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="minutes")
    write(tmp_path / "queue.json", [
        {"id": "a", "url": "https://x/a", "title": "Done one"},
        {"id": "b", "url": "https://x/b", "title": "Failed one"},
        {"id": "c", "url": "https://x/c", "title": "Skipped one"},
        {"id": "d", "url": "https://x/d", "title": "Waiting one"},
        {"id": "e", "url": "https://x/e", "title": "Future one", "aired_at": future},
        {"id": "f", "url": "https://x/f", "title": "Locked one", "locked": True},
        {"id": "g", "url": "https://x/g", "title": "Within grace", "aired_at": recent},
    ])
    write(tmp_path / "progress.json", {
        "a": {"status": "done", "title": "Done one"},
        "b": {"status": "failed", "title": "Failed one", "error": "ItemFailed: no player"},
        "c": {"status": "skipped", "title": "Skipped one"}})
    snap = state.snapshot(tmp_path)
    chips = {i["id"]: i["state"] for i in snap["items"]}
    assert chips == {"a": "done", "b": "failed", "c": "skipped", "d": "waiting",
                     "e": "not_aired", "f": "locked", "g": "not_aired"}
    assert snap["items"][1]["error"] == "no player"
    assert snap["items"][1]["error_raw"] == "ItemFailed: no player"
    assert snap["counts"] == {"done": 1, "failed": 1, "skipped": 1, "waiting": 1,
                              "not_aired": 2, "locked": 1, "total": 7}


def test_corrupt_state_files_degrade_to_empty(tmp_path):
    configure(tmp_path, tmp_path / "out")
    write(tmp_path / "queue.json", "[{half")
    write(tmp_path / "progress.json", "")
    snap = state.snapshot(tmp_path)
    assert snap["items"] == [] and snap["counts"]["total"] == 0


def test_library_lists_episodes_with_optimized_flag(tmp_path):
    out = tmp_path / "Lib Dec'26"
    season = out / "Test Show" / "Season 01"
    season.mkdir(parents=True)
    (season / "S01E01 - One.mp4").write_bytes(b"x" * 2048)
    (season / "S01E02 - Two.mp4").write_bytes(b"x" * 1024)
    (season / "S01E03 - Three.optpart").write_bytes(b"x")
    configure(tmp_path, out)
    write(tmp_path / "optimize.json", {
        "S01E01 - One.mkv": {"status": "done", "file": str(season / "S01E01 - One.mp4")}})
    lib = {e["name"]: e for e in state.snapshot(tmp_path)["library"]}
    assert set(lib) == {"S01E01 - One", "S01E02 - Two"}
    assert lib["S01E01 - One"]["optimized"] is True
    assert lib["S01E02 - Two"]["optimized"] is False
    assert lib["S01E01 - One"]["gb"] > 0


def test_now_only_while_recording_and_fresh(tmp_path, monkeypatch):
    configure(tmp_path, tmp_path / "out")
    d = tmp_path / ".playcap"
    d.mkdir()
    write(d / "now.json", {"title": "T", "t": 5, "duration": 10, "luma": 1.0,
                           "updated_at": time.time()})
    assert state.snapshot(tmp_path)["now"] is None          # record not running
    monkeypatch.setattr(state.jobs, "status", lambda root: {
        n: {"running": n == "record", "pid": 1, "started": 0, "stopping": False,
            "stopping_after": False} for n in jobs.NAMES})
    now = state.snapshot(tmp_path)["now"]
    assert now["title"] == "T" and now["black"] is True
    write(d / "now.json", {"title": "T", "updated_at": time.time() - 600})
    assert state.snapshot(tmp_path)["now"] is None          # stale


def test_problems_explain_and_suggest(tmp_path):
    configure(tmp_path, tmp_path / "out")
    codes = {p["code"] for p in state.snapshot(tmp_path)["problems"]}
    assert {"obs", "browser"} <= codes


def test_obs_password_falls_back_to_detected(tmp_path, monkeypatch):
    configure(tmp_path, tmp_path / "out")
    seen = {}
    monkeypatch.setattr(state, "check_obs",
                        lambda url, password: seen.update(url=url, pw=password) or (True, "ok"))
    monkeypatch.setattr(state.detect, "obs_websocket",
                        lambda: {"url": "ws://127.0.0.1:4466", "password": "det", "enabled": True})
    state._obs_cache.clear()
    state.snapshot(tmp_path)
    assert seen == {"url": "ws://127.0.0.1:4466", "pw": "det"}


def test_bad_adapter_reported_not_raised(tmp_path):
    write(tmp_path / "config.json", {"adapter": "no.such.mod", "output_dir": str(tmp_path)})
    snap = state.snapshot(tmp_path)
    assert any(p["code"] == "adapter" for p in snap["problems"])


def test_friendly_errors():
    raw = ("CdpError: Chrome debug port unreachable at http://127.0.0.1:9222: "
           "HTTPConnectionPool(host='127.0.0.1', port=9222): Max retries exceeded")
    assert state.friendly_error(raw) == "The playcap browser was closed."
    assert state.friendly_error("ItemFailed: stalled at 812s, 3 recovery attempts failed")         == "Playback froze and could not be restarted."
    assert state.friendly_error(None) is None
