from datetime import datetime
from pathlib import Path

from playcap import organize, settings
from playcap.adapters.base import Item


def item(title="Product webinar", when=None):
    return Item(id="a1b2c3", title=title, url="https://example.com/v", aired_at=when)


def test_default_is_a_plain_numbered_folder():
    cfg = {"show": "My Recordings"}
    assert organize.relpath(cfg, 3, item(), "Product webinar") == Path("My Recordings/03 - Product webinar")
    assert organize.write_nfo(cfg) is False


def test_media_server_layout_matches_the_old_episode_names():
    cfg = {"show": "Show", "season": 1, "library_layout": "media_server"}
    rel = organize.relpath(cfg, 7, item(), "Some Topic- II")
    assert rel == Path("Show/Season 01") / organize.episode_stem(cfg, 7, "Some Topic- II")
    assert organize.write_nfo(cfg) is True
    assert organize.write_nfo({**cfg, "write_nfo": False}) is False


def test_custom_template_with_date_and_missing_date():
    cfg = {"show": "Talks", "library_layout": "custom", "name_template": "{show}/{date} - {title} ({n:03})"}
    dated = item(when=datetime(2026, 3, 14, 14, 0))
    assert organize.relpath(cfg, 2, dated, "Intro") == Path("Talks/2026-03-14 - Intro (002)")
    # no date: the dangling " - " goes, not the title
    assert organize.relpath(cfg, 2, item(), "Intro") == Path("Talks/Intro (002)")


def test_unsafe_characters_and_dot_folders_are_neutralised():
    cfg = {"show": "A/../B", "library_layout": "custom", "name_template": "{show}/{n} {title}"}
    rel = organize.relpath(cfg, 1, item(), 'What? "Now": <yes>')
    assert ".." not in rel.parts
    assert all(not set(p) & set('<>:"|?*') for p in rel.parts)


def test_template_checks():
    assert organize.check_template("{show}/{n:02} - {title}") is None
    assert "Unknown placeholder" in organize.check_template("{show}/{nope}")
    assert "{n}" in organize.check_template("{show}/{title}")
    assert organize.check_template("{id}") is None


def test_settings_validate_layout(tmp_path):
    cfg, errors = settings.save(tmp_path, {"library_layout": "custom", "name_template": "{title}"})
    assert "name_template" in errors
    cfg, errors = settings.save(tmp_path, {"library_layout": "custom",
                                           "name_template": "{show}/{date} {title} {n}", "write_nfo": True})
    assert errors == {} and cfg["library_layout"] == "custom" and cfg["write_nfo"] is True
    assert "library_layout" in settings.save(tmp_path, {"library_layout": "zip"})[1]
