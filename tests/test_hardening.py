"""Regression tests for the adversarial review: every case here once lost,
overwrote or misreported a recording, or let a bad request through."""
import json
import os
import sys
import time

import pytest

from playcap import activity, build_queue, jobs, organize, recorder, settings
from playcap.adapters import html5_video
from playcap.adapters.base import Item
from playcap.obs_client import Obs, ObsError

COPY_FFMPEG = """
import shutil, sys
args = sys.argv[1:]
shutil.copyfile(args[args.index("-i") + 1], args[-1])
"""
FAIL_FFMPEG = "import sys; sys.exit(1)\n"


def fake_tool(tmp_path, name, body):
    script = tmp_path / f"{name}.py"
    script.write_text(body)
    if sys.platform.startswith("win"):
        launcher = tmp_path / f"{name}.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        launcher = tmp_path / name
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    return str(launcher)


# --- recorder: finishing a recording never deletes it ------------------------------
def test_mp4_recording_is_kept_as_is(tmp_path, monkeypatch):
    # OBS set to record MP4: remuxing onto its own name used to delete it.
    monkeypatch.setattr(recorder, "CFG", {"ffmpeg": fake_tool(tmp_path, "ff", FAIL_FFMPEG)})
    rec = tmp_path / "talk.mp4"
    rec.write_text("the only copy")
    assert recorder.to_mp4(rec) == rec
    assert rec.read_text() == "the only copy"


def test_failed_remux_keeps_source_and_leaves_no_part(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "CFG", {"ffmpeg": fake_tool(tmp_path, "ff", FAIL_FFMPEG)})
    rec = tmp_path / "talk.mkv"
    rec.write_text("source")
    assert recorder.to_mp4(rec) == rec
    assert rec.read_text() == "source"
    assert not list(tmp_path.glob("*.part.mp4")) and not (tmp_path / "talk.mp4").exists()


def test_remux_success_replaces_mkv(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "CFG", {"ffmpeg": fake_tool(tmp_path, "ff", COPY_FFMPEG)})
    rec = tmp_path / "talk.mkv"
    rec.write_text("source")
    out = recorder.to_mp4(rec)
    assert out == tmp_path / "talk.mp4" and out.read_text() == "source"
    assert not rec.exists()


def test_remux_never_overwrites_an_existing_mp4(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "CFG", {"ffmpeg": fake_tool(tmp_path, "ff", COPY_FFMPEG)})
    (tmp_path / "talk.mp4").write_text("earlier take")
    rec = tmp_path / "talk.mkv"
    rec.write_text("new take")
    assert recorder.to_mp4(rec) == rec
    assert (tmp_path / "talk.mp4").read_text() == "earlier take"


def test_finalize_picks_a_free_name_across_extensions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(recorder, "CFG", {
        "ffmpeg": fake_tool(tmp_path, "ff", COPY_FFMPEG), "output_dir": str(tmp_path / "lib"),
        "show": "Show", "library_layout": "folder", "title_max_len": 80})
    item = Item(id="1", title="Talk", url="u")
    first = tmp_path / "lib" / "Show" / "01 - Talk.mp4"
    first.parent.mkdir(parents=True)
    first.write_text("earlier take")
    obs_file = tmp_path / "2026-01-01 10-00-00.mkv"
    obs_file.write_text("new take")
    path, _ = recorder.finalize(str(obs_file), item, 1)
    assert first.read_text() == "earlier take"
    assert path.endswith("01 - Talk (2).mp4")


def test_progress_round_trips_non_ascii(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "CFG", {"progress_file": str(tmp_path / "progress.json")})
    prog = {"x": {"status": "done", "title": "Łódź – café", "file": "a"}}
    recorder.save_progress(prog)
    assert recorder.load_progress() == prog


# --- OBS: a recording that never started is an error ---------------------------------
class FakeObs(Obs):
    def __init__(self, active_after):
        self.calls, self.active_after = 0, active_after

    def request(self, kind, data=None, timeout=20):
        if kind == "GetRecordStatus":
            self.calls += 1
            return {"outputActive": self.active_after is not None
                    and self.calls > self.active_after}
        return {}


def test_start_record_waits_until_live():
    FakeObs(active_after=3).start_record(confirm_seconds=5)


def test_start_record_that_never_starts_raises():
    with pytest.raises(ObsError, match="not recording"):
        FakeObs(active_after=None).start_record(confirm_seconds=1)


# --- settings: a save never loses what it could not read ----------------------------
def test_save_refuses_a_corrupt_config(tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    _, errors = settings.save(tmp_path, {"show": "X"})
    assert errors
    assert (tmp_path / "config.json").read_text() == "{not json"


def test_save_refuses_unknown_and_path_keys(tmp_path):
    _, errors = settings.save(tmp_path, {"progress_file": "C:/Windows/win.ini"})
    assert "progress_file" in errors
    assert not (tmp_path / "config.json").exists()


def test_links_need_a_scheme_or_a_real_file(tmp_path):
    _, errors = settings.save(tmp_path, {"links": "www.example.com/talk"})
    assert "links" in errors
    (tmp_path / "demo.html").write_text("<video>")
    _, errors = settings.save(tmp_path, {
        "links": "Intro | https://example.com/a\ndemo.html\n# comment"})
    assert errors == {}


def test_save_keeps_existing_template_for_custom_layout(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"name_template": "{n} - {title}"}))
    _, errors = settings.save(tmp_path, {"library_layout": "custom"})
    assert errors == {}


def test_number_fields_are_checked(tmp_path):
    _, errors = settings.save(tmp_path, {"season": "two"})
    assert "season" in errors


# --- server: a corrupt progress file is never replaced by one edit -----------------
def test_skip_with_corrupt_progress_is_refused(tmp_path, monkeypatch):
    from playcap.ui import server
    monkeypatch.setattr(server, "recording_now", lambda root: False)
    (tmp_path / "config.json").write_text(json.dumps({
        "adapter": "playcap.adapters.html5_video", "output_dir": "out"}))
    (tmp_path / "queue.json").write_text(json.dumps([{"id": "a", "url": "https://x/a"}]))
    (tmp_path / "progress.json").write_text('{"b": {"status": "done"')      # cut short
    ok, msg = server.edit_item(tmp_path, "skip", "a")
    assert not ok and "progress.json" in msg
    assert (tmp_path / "progress.json").read_text() == '{"b": {"status": "done"'


# --- organize: never overwrite ---------------------------------------------------------
def organize_setup(tmp_path, monkeypatch, titles, files):
    monkeypatch.chdir(tmp_path)
    lib = tmp_path / "lib"
    (tmp_path / "config.json").write_text(json.dumps({
        "adapter": "playcap.adapters.html5_video", "output_dir": str(lib), "show": "S"}))
    queue = [{"id": str(i), "url": f"https://x/{i}", "title": t} for i, t in enumerate(titles, 1)]
    (tmp_path / "queue.json").write_text(json.dumps(queue))
    progress = {}
    for i, (name, body) in enumerate(files, 1):
        f = lib / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
        progress[str(i)] = {"status": "done", "file": str(f), "title": titles[i - 1]}
    (tmp_path / "progress.json").write_text(json.dumps(progress))
    from playcap import config
    config._cache.clear()
    monkeypatch.setattr(organize, "recording_now", lambda: False)
    return lib


def test_organize_does_not_overwrite_an_existing_file(tmp_path, monkeypatch):
    lib = organize_setup(tmp_path, monkeypatch, ["Talk"], [("raw.mp4", "new take")])
    taken = lib / "S" / "01 - Talk.mp4"
    taken.parent.mkdir(parents=True)
    taken.write_text("earlier take")
    organize.main([])
    assert taken.read_text() == "earlier take"
    assert (lib / "raw.mp4").read_text() == "new take"


def test_organize_resolves_a_renumbering_chain(tmp_path, monkeypatch):
    # Item 1 must go where item 2 is now; item 2 moves on first.
    lib = organize_setup(tmp_path, monkeypatch, ["A", "B"],
                         [("S/02 - B.mp4", "A's video"), ("S/old B.mp4", "B's video")])
    (lib / "S" / "02 - B.mp4").rename(lib / "S" / "tmp")
    (lib / "S" / "tmp").rename(lib / "S" / "02 - B.mp4")
    organize.main([])
    assert (lib / "S" / "01 - A.mp4").read_text() == "A's video"
    assert (lib / "S" / "02 - B.mp4").read_text() == "B's video"
    prog = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert prog["1"]["file"].endswith("01 - A.mp4") and prog["2"]["file"].endswith("02 - B.mp4")


def test_titles_cannot_make_folders_or_reserved_names():
    rel = organize._render("{show}/{n:02} - {title}", {"show": "S", "n": 3, "title": "Q&A 1/2"})
    assert rel.parts == ("S", "03 - Q&A 1-2")
    assert organize._render("{title}", {"title": "CON"}).name == "CON_"
    assert len(organize._render("{title}", {"title": "x" * 400}).name) <= organize.PART_MAX


# --- activity: only the latest run counts ---------------------------------------------
def test_old_test_pass_is_not_reported_for_a_new_failure(tmp_path):
    (tmp_path / "smoke_test.log").write_text(
        "[1] Preflight\n    mean luma = 120.0 (OK, real picture)\nSmoke test finished.\n"
        f"{jobs.RUN_MARK} test 2026-10-07 10:00:00 ===\n[1] Preflight\n"
        "*** FAILED: No player found.\n", encoding="utf-8")
    name = jobs.LOGS["test"][0]
    if name != "smoke_test.log":
        (tmp_path / "smoke_test.log").rename(tmp_path / name)
    got = {a["job"]: a for a in activity.summarize(tmp_path)}["test"]
    assert got["level"] == "bad"


def test_crash_reports_the_exception_line():
    lines = ["Traceback (most recent call last):", 'File "x.py", line 1',
             "ValueError: boom", "cleanup printed this"]
    assert activity._crashed(lines) == "ValueError: boom"


# --- jobs: PID files carry the OS creation time; stale flags are dropped ------------
def test_pidfile_holds_exact_creation_time(tmp_path):
    ok, _ = jobs.start("record", tmp_path, {},
                       cmd=[sys.executable, "-c", "import time; time.sleep(30)"])
    assert ok
    try:
        info = json.loads((tmp_path / ".playcap" / "record.json").read_text())
        assert info["exact"] is True
        assert jobs.status(tmp_path)["record"]["running"]
        assert not jobs.pid_alive(info["pid"], info["started"] - 60, exact=True)
    finally:
        jobs.kill("record", tmp_path)


def test_stale_stop_flag_is_cleared_at_start(tmp_path):
    jobs.request(tmp_path, "record", "now")
    flag = tmp_path / ".playcap" / "record.now"
    old = time.time() - 3600
    os.utime(flag, (old, old))
    jobs.clear_stale_flags(tmp_path, "record")
    assert not flag.exists()


# --- queue -------------------------------------------------------------------------
def test_empty_queue_never_replaces_a_good_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "queue.json").write_text('[{"url": "https://x/a"}]')

    class Empty:
        def build_queue(self, cfg, argv):
            return []

    monkeypatch.setattr(build_queue.config, "load", lambda: ({"queue_file": "queue.json"}, Empty()))
    with pytest.raises(SystemExit):
        build_queue.main([])
    assert (tmp_path / "queue.json").read_text() == '[{"url": "https://x/a"}]'


def test_read_queue_source_formats(tmp_path):
    (tmp_path / "page.html").write_text("<video>")
    src = tmp_path / "queue.txt"
    src.write_text("# comment\n\nIntro | https://example.com/a\npage.html\n"
                   "https://example.com/a\n", encoding="utf-8")
    items = html5_video.read_queue_source(src)
    assert [i["title"] for i in items] == ["Intro", "page"]       # duplicate URL dropped
    assert items[1]["url"].startswith("file:")


def test_organize_takes_optimize_entry_and_original_along(tmp_path, monkeypatch):
    lib = organize_setup(tmp_path, monkeypatch, ["Talk"], [("old name.mp4", "encoded")])
    orig = tmp_path / "lib_originals" / "old name.mkv"
    orig.parent.mkdir()
    orig.write_text("original")
    (tmp_path / "optimize.json").write_text(json.dumps({"old name": {
        "status": "done", "file": str(lib / "old name.mp4"), "original": str(orig), "crf": 24}}))
    organize.main([])
    state = json.loads((tmp_path / "optimize.json").read_text(encoding="utf-8"))
    entry = state["S/01 - Talk"]
    assert entry["file"].endswith("01 - Talk.mp4") and entry["crf"] == 24
    moved = tmp_path / "lib_originals" / "S" / "01 - Talk.mkv"
    assert moved.read_text() == "original" and entry["original"] == str(moved)
    assert "old name" not in state
