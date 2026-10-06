import json
import os

from playcap import settings


def test_read_missing_and_corrupt(tmp_path):
    assert settings.read(tmp_path) == {}
    (tmp_path / "config.json").write_text("{half")
    assert settings.read(tmp_path) == {}


def test_atomic_write_leaves_no_temp(tmp_path):
    target = tmp_path / "x.json"
    settings.atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_save_merges_and_preserves_unknown_keys(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"site_host": "https://x", "show": "Old"}))
    out = tmp_path / "Lib Dec'26"
    cfg, errors = settings.save(tmp_path, {"show": "New", "output_dir": str(out)})
    assert errors == {}
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["site_host"] == "https://x"
    assert on_disk["show"] == "New" and on_disk["output_dir"] == str(out)
    assert out.is_dir() and cfg == on_disk


def test_save_rejects_bad_values_without_writing(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"show": "Keep"}))
    cfg, errors = settings.save(tmp_path, {
        "show": "  ", "chrome_exe": str(tmp_path / "nope.exe"),
        "obs_ws_url": "http://wrong", "adapter": "no.such.module"})
    assert set(errors) == {"show", "chrome_exe", "obs_ws_url", "adapter"}
    assert json.loads((tmp_path / "config.json").read_text()) == {"show": "Keep"}


def test_save_rejects_unwritable_output_dir(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    _, errors = settings.save(tmp_path, {"output_dir": str(blocker / "sub")})
    assert "output_dir" in errors


def test_existing_tool_path_accepted(tmp_path):
    exe = tmp_path / "my tools" / "ffmpeg.exe"
    exe.parent.mkdir()
    exe.write_text("")
    _, errors = settings.save(tmp_path, {"ffmpeg": str(exe)})
    assert errors == {}


def test_links_written_to_queue_file(tmp_path):
    cfg, errors = settings.save(tmp_path, {
        "adapter": "playcap.adapters.html5_video",
        "links": "https://a.example/v1\n\n  Intro | https://b.example/v2  \n# comment\n"})
    assert errors == {}
    assert cfg["queue_source"] == "queue.txt"
    assert "links" not in cfg
    text = (tmp_path / "queue.txt").read_text()
    assert text.splitlines() == ["https://a.example/v1", "Intro | https://b.example/v2"]
    assert settings.read_links(tmp_path, cfg) == text.strip()


def test_links_must_not_be_empty(tmp_path):
    _, errors = settings.save(tmp_path, {"adapter": "playcap.adapters.html5_video",
                                         "links": " \n# only a comment\n"})
    assert "links" in errors


def test_adapters_available_has_generic(tmp_path):
    mods = {a["module"]: a for a in settings.adapters_available()}
    generic = mods["playcap.adapters.html5_video"]
    assert generic["label"] == "List of links"
    assert generic["fields"][0]["key"] == "links"


def test_configured_flag(tmp_path):
    assert settings.is_configured({}) is False
    assert settings.is_configured({"adapter": "x", "output_dir": "y"}) is True
    # an older config.json without "adapter" still counts: the pipeline falls back too
    assert settings.is_configured({"output_dir": "y"}) is True


def test_effective_adapter_falls_back_like_config_load():
    from playcap import config
    assert settings.effective_adapter({"adapter": "a.b"}) == "a.b"
    assert settings.effective_adapter({}) == config.default_adapter_path()


def test_recording_quality_is_validated_and_stored_as_numbers(tmp_path):
    from playcap import settings
    cfg, errors = settings.save(tmp_path, {"record_crf": 50})
    assert "record_crf" in errors
    cfg, errors = settings.save(tmp_path, {"record_mode": "quality", "record_crf": "26",
                                           "x264_preset": "fast", "keyframe_seconds": "2"})
    assert errors == {}
    assert cfg["record_crf"] == 26 and cfg["keyframe_seconds"] == 2
