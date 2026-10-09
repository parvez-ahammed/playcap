import hashlib
import json
from pathlib import Path

import pytest

from playcap import recipes, settings, teach
from playcap.adapters import html5_video as h5

REPO = Path(__file__).resolve().parent.parent

GOOD = {"playcap_recipe": 1, "name": "Talks", "match": {"url": "https://ex.org/watch/*"},
        "player": "#player", "play_button": ".big-play"}


def sha(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- validation
def test_validate_fills_optional_keys():
    clean, errors = recipes.validate(GOOD)
    assert errors == []
    assert clean["match"] == {"url": "https://ex.org/watch/*", "page_has": ""}
    assert clean["collect"] is None and clean["description"] == ""


@pytest.mark.parametrize("bad, words", [
    ({**GOOD, "evil": 1}, "Unknown key"),
    ({**GOOD, "playcap_recipe": True}, "Not a playcap recipe"),
    ({**GOOD, "playcap_recipe": 2}, "Not a playcap recipe"),
    ({**GOOD, "name": ""}, "name is required"),
    ({**GOOD, "name": "x" * 81}, "longer than"),
    ({**GOOD, "player": "a\nb"}, "control characters"),
    ({**GOOD, "player": 5}, "must be text"),
    ({**GOOD, "match": {"url": "re:("}}, "regular expression"),
    ({**GOOD, "match": {"url": "*", "extra": 1}}, "Unknown match key"),
    ({**GOOD, "match": "*"}, '"match" must be an object'),
    ({**GOOD, "player": "", "play_button": ""}, "at least a player"),
    ({**GOOD, "collect": {"page": "*"}}, "links selector, a url_regex"),
    ({**GOOD, "collect": {"page": "*", "links": "a", "max_pages": 0}}, "max_pages"),
    ({**GOOD, "collect": {"page": "*", "links": "a", "max_pages": True}}, "max_pages"),
    ({**GOOD, "collect": {"page": "*", "links": "a", "reverse": "yes"}}, "reverse"),
    ({**GOOD, "collect": {"page": "*", "url_regex": "("}}, "regular expression"),
    ({**GOOD, "collect": {"links": "a"}}, "collect.page is required"),
    ([GOOD], "JSON object"),
])
def test_validate_is_strict(bad, words):
    clean, errors = recipes.validate(bad)
    assert clean is None
    assert any(words in e for e in errors), errors


def test_parse_import_single_list_and_garbage():
    assert len(recipes.parse_import(json.dumps(GOOD))[0]) == 1
    two = [GOOD, {**GOOD, "name": "Other"}]
    assert [r["name"] for r in recipes.parse_import(json.dumps(two))[0]] == ["Talks", "Other"]
    good, errors = recipes.parse_import(json.dumps([GOOD, {**GOOD, "x": 1}]))
    assert good == [] and errors and errors[0].startswith("recipe 2:")
    assert recipes.parse_import("not json")[1][0].startswith("Not valid JSON")
    assert recipes.parse_import("[]")[1]


def test_gallery_recipes_validate_and_match_by_markup():
    files = sorted((REPO / "recipes").glob("*.json"))
    assert len(files) >= 3
    for f in files:
        clean, errors = recipes.validate(json.loads(f.read_text(encoding="utf-8")))
        assert errors == [], (f.name, errors)
        # generic player types: no hostname in the match rule
        assert "://" not in clean["match"]["url"], f.name
        assert clean["match"]["page_has"], f.name


# ---------------------------------------------------------------- matching
def test_url_matches_glob_and_regex():
    assert recipes.url_matches("https://ex.org/watch/*", "https://ex.org/watch/a/b?x=1")
    assert not recipes.url_matches("https://ex.org/watch/*", "https://ex.org/list")
    assert recipes.url_matches(r"re:/watch/\d+$", "https://ex.org/watch/42")
    assert not recipes.url_matches("re:(", "anything")
    assert not recipes.url_matches("", "anything")


def test_for_page_checks_markup_and_order():
    cfg = {"recipes": [
        {**GOOD, "name": "needs markup", "match": {"url": "*", "page_has": ".video-js"}},
        {**GOOD, "name": "plain"},
        {"broken": True},
    ]}
    url = "https://ex.org/watch/1"
    assert recipes.for_page(cfg, url)["name"] == "plain"       # no page to ask
    assert recipes.for_page(cfg, url, lambda s: s == ".video-js")["name"] == "needs markup"
    assert recipes.for_page(cfg, "https://other/") is None


def test_for_index():
    r = {**GOOD, "collect": {"page": "https://ex.org/list*", "links": "a.t"}}
    cfg = {"recipes": [GOOD, r]}
    assert recipes.for_index(cfg, "https://ex.org/list?page=1")["name"] == "Talks"
    assert recipes.for_index(cfg, "https://ex.org/other") is None


# ---------------------------------------------------------------- storage
def test_upsert_delete_import_keep_other_config(tmp_path):
    settings.atomic_write_json(tmp_path / "config.json", {"output_dir": "rec", "show": "S"})
    assert recipes.upsert(tmp_path, GOOD)[0]
    ok, msg = recipes.upsert(tmp_path, {**GOOD, "name": "Second"})
    assert ok
    assert [r["name"] for r in recipes.load_all(tmp_path)] == ["Second", "Talks"]
    ok, msg = recipes.upsert(tmp_path, {**GOOD, "player": "#p2"})
    assert ok and "updated" in msg
    assert [r["name"] for r in recipes.load_all(tmp_path)] == ["Talks", "Second"]
    # rename while editing replaces the old entry
    assert recipes.upsert(tmp_path, {**GOOD, "name": "Renamed"}, replace="Talks")[0]
    assert [r["name"] for r in recipes.load_all(tmp_path)] == ["Renamed", "Second"]
    assert recipes.delete(tmp_path, "Second")[0]
    assert not recipes.delete(tmp_path, "Second")[0]
    ok, msg = recipes.import_text(tmp_path, json.dumps([{**GOOD, "name": "A"}, {**GOOD, "name": "B"}]))
    assert ok and "2" in msg
    assert [r["name"] for r in recipes.load_all(tmp_path)] == ["A", "B", "Renamed"]
    cfg = settings.read(tmp_path)
    assert cfg["output_dir"] == "rec" and cfg["show"] == "S"


def test_bad_recipe_or_unreadable_config_is_never_written(tmp_path):
    (tmp_path / "config.json").write_text("{broken", encoding="utf-8")
    assert not recipes.upsert(tmp_path, GOOD)[0]
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == "{broken"
    settings.atomic_write_json(tmp_path / "config.json", {})
    ok, msg = recipes.import_text(tmp_path, json.dumps({**GOOD, "player": "x" * 400}))
    assert not ok and "Not imported" in msg
    assert settings.read(tmp_path) == {}


# ---------------------------------------------------------------- selectors
def node(tag, id="", classes=(), attrs=None, nth=1, same=1):
    return {"tag": tag, "id": id, "classes": list(classes), "attrs": attrs or {},
            "nth": nth, "same": same}


@pytest.mark.parametrize("tok, ok", [
    ("main-player", True), ("vjs-tech", True), ("jw-display", True), ("plyr__video", True),
    ("css-1x2y3z", False), ("sc-bdVaJa", False), ("jsx-123456", False), ("item-20240101", False),
    ("active", False), ("vjs-playing", False), ("is-open", False), ("_a1b2c3", False),
    ("3col", False), ("", False),
])
def test_stable_token(tok, ok):
    assert recipes.stable_token(tok) is ok


def test_candidates_prefer_ids_attributes_classes_then_position():
    path = [node("video", classes=["vjs-tech", "vjs-playing"], nth=2, same=2),
            node("div", id="main-player", classes=["video-js"]),
            node("body")]
    c = recipes.candidates(path)
    assert c[0] == "video.vjs-tech"
    assert "#main-player video.vjs-tech" in c
    assert not any("vjs-playing" in s for s in c)
    assert c[-1] == "#main-player > video:nth-of-type(2)"
    assert c.index("video.vjs-tech") < c.index("#main-player > video:nth-of-type(2)")


def test_candidates_use_stable_id_and_data_attrs_and_skip_generated():
    path = [node("button", id="css-9f8e7d", attrs={"data-testid": "play", "data-playcap-pick": "play"}),
            node("body")]
    c = recipes.candidates(path)
    assert c[0] == 'button[data-testid="play"]'
    assert not any("css-9f8e7d" in s or "playcap" in s for s in c)
    assert recipes.candidates([node("video", id="hero")])[0] == "#hero"


def test_set_candidates_generalise_what_examples_share():
    def link(n):
        return [node("a", classes=["talk-link"], attrs={"data-id": str(n)}),
                node("li", classes=["talk"], nth=n, same=30),
                node("ul", id="talks"), node("body")]
    common = recipes.set_candidates([link(1), link(2), link(7)])
    assert "a.talk-link" in common
    assert "#talks a.talk-link" in common
    assert "li.talk > a" in common
    assert not any("data-id" in s for s in common)     # differs per item


def test_choose_single_and_set():
    res = [{"sel": "video", "count": 3, "hits": 1, "want": 1, "first": False},
           {"sel": "div video", "count": 2, "hits": 1, "want": 1, "first": True},
           {"sel": "#p video", "count": 1, "hits": 1, "want": 1, "first": True}]
    assert recipes.choose_single(res) == "#p video"
    assert recipes.choose_single(res[:2]) == "div video"
    assert recipes.choose_single(res[:1]) is None
    sets = [{"sel": "a", "count": 120, "hits": 3, "want": 3},
            {"sel": "#talks a.t", "count": 30, "hits": 3, "want": 3},
            {"sel": "a.t.x", "count": 2, "hits": 2, "want": 3},
            {"sel": "bad(", "ok": False, "count": -1}]
    assert recipes.choose_set(sets) == "#talks a.t"
    assert recipes.choose_set(sets[2:]) is None


def test_suggest_match():
    assert recipes.suggest_match("https://ex.org/watch/42?t=1") == "https://ex.org/watch/*"
    assert recipes.suggest_match("https://ex.org/42") == "https://ex.org/*"
    assert recipes.suggest_match("file:///C:/x/index.html") == "file:///C:/x/index.html"


# ---------------------------------------------------------------- queue building
INDEX = "https://ex.org/list"
COLLECT_RECIPE = {**GOOD, "collect": {"page": "https://ex.org/list*", "links": "a.t"}}


def write_links(tmp_path, text):
    p = tmp_path / "queue.txt"
    p.write_text(text, encoding="utf-8")
    return p


def test_collect_line_expands_in_place_with_hand_typed_ids(tmp_path):
    src = write_links(tmp_path, "Intro | https://ex.org/watch/0\ncollect: " + INDEX +
                      "\nhttps://ex.org/watch/9\n")
    pages = {"https://ex.org/watch/1": "One", "https://ex.org/watch/2": "Two",
             "https://ex.org/watch/0": "dupe of a typed one"}
    seen = []

    def collect(rule):
        seen.append(rule)
        return [{"url": u, "title": t} for u, t in pages.items()]

    items = h5.read_queue_source(src, collect, cfg={"recipes": [COLLECT_RECIPE]})
    assert seen[0]["page"] == INDEX and seen[0]["links"] == "a.t"
    assert [i["url"] for i in items] == ["https://ex.org/watch/0", "https://ex.org/watch/1",
                                         "https://ex.org/watch/2", "https://ex.org/watch/9"]
    assert items[1]["id"] == sha("https://ex.org/watch/1")
    assert items[1]["collected_from"] == INDEX and "collected_from" not in items[0]
    assert items[0]["title"] == "Intro"


def test_rebuild_keeps_old_order_and_appends_new(tmp_path):
    src = write_links(tmp_path, "collect: " + INDEX + "\n")
    cfg = {"recipes": [COLLECT_RECIPE]}
    first = h5.read_queue_source(src, lambda r: [{"url": "https://ex.org/w/b", "title": "B"},
                                                 {"url": "https://ex.org/w/a", "title": "A"}], cfg=cfg)
    # newest first: c is new, a dropped off the page
    again = h5.read_queue_source(src, lambda r: [{"url": "https://ex.org/w/c", "title": "C"},
                                                 {"url": "https://ex.org/w/b", "title": "B"}],
                                 previous=first, cfg=cfg)
    assert [i["title"] for i in again] == ["B", "A", "C"]
    assert [i["id"] for i in again[:2]] == [i["id"] for i in first]


def test_reverse_and_config_rules(tmp_path):
    rule = {"page": INDEX, "links": "a.t", "reverse": True}
    items = h5.read_queue_source(None, lambda r: [{"url": "https://ex.org/w/new"},
                                                  {"url": "https://ex.org/w/old"}],
                                 extra=h5.config_rules({"collect": [rule]}))
    assert [i["url"] for i in items] == ["https://ex.org/w/old", "https://ex.org/w/new"]
    with pytest.raises(ValueError, match="entry 1"):
        h5.config_rules({"collect": [{"page": INDEX}]})


def test_collect_line_without_recipe_or_browser_fails_clearly(tmp_path):
    src = write_links(tmp_path, "collect: " + INDEX + "\n")
    with pytest.raises(ValueError, match="No recipe"):
        h5.read_queue_source(src, lambda r: [], cfg={})
    with pytest.raises(ValueError, match="needs the browser"):
        h5.read_queue_source(src, None, cfg={"recipes": [COLLECT_RECIPE]})
    bad = write_links(tmp_path, "collect: ex.org/list\n")
    with pytest.raises(ValueError, match="web address"):
        h5.read_queue_source(bad, lambda r: [], cfg={"recipes": [COLLECT_RECIPE]})


def test_plain_links_file_unchanged(tmp_path):
    src = write_links(tmp_path, "# c\nA | https://ex.org/1\nhttps://ex.org/1\nhttps://ex.org/2\n")
    items = h5.read_queue_source(src)
    assert [i["id"] for i in items] == [sha("https://ex.org/1"), sha("https://ex.org/2")]
    assert all("collected_from" not in i for i in items)


def test_queue_needs_browser_follows_the_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = h5.Adapter()
    a.cfg = {"queue_source": "queue.txt"}
    write_links(tmp_path, "https://ex.org/1\n")
    assert a.queue_needs_browser is False
    write_links(tmp_path, "https://ex.org/1\ncollect: " + INDEX + "\n")
    assert a.queue_needs_browser is True
    a.cfg = {"queue_source": "missing.txt", "collect": [{"page": INDEX, "links": "a"}]}
    assert a.queue_needs_browser is True


def test_settings_accepts_collect_lines(tmp_path):
    assert settings.validate({"links": "collect: https://ex.org/list"}, tmp_path) == {}
    assert "links" in settings.validate({"links": "collect: ex.org/list"}, tmp_path)


class FakeIndex:
    """A CDP session over a fake site: pages of links, a next link or button."""

    def __init__(self, pages, next_kind="href"):
        self.pages, self.kind, self.at, self.navs, self.clicks = pages, next_kind, None, [], []

    def navigate(self, url, settle=0):
        self.navs.append(url)
        self.at = url

    def click(self, x, y):
        self.clicks.append((x, y))
        keys = list(self.pages)
        self.at = keys[keys.index(self.at) + 1]

    def js_json(self, expr):
        keys = list(self.pages)
        if "querySelectorAll(sel)" in expr:
            return {"links": [{"url": u, "title": u[-1]} for u in self.pages[self.at]]}
        nxt = keys.index(self.at) + 1
        if nxt >= len(keys):
            return None
        return {"href": keys[nxt] if self.kind == "href" else "", "x": 5, "y": 6}


def test_collect_with_follows_next_links_filters_and_caps():
    pages = {INDEX: ["https://ex.org/w/1", "https://ex.org/about", INDEX + "#top"],
             INDEX + "?p=2": ["https://ex.org/w/2", "https://ex.org/w/1"],
             INDEX + "?p=3": ["https://ex.org/w/3"]}
    sess = FakeIndex(pages)
    rule = {"page": INDEX, "links": "a", "url_regex": "/w/", "next": "a.next", "max_pages": 2}
    got = h5.collect_with(sess, rule, wait=1, sleep=lambda s: None)
    assert [g["url"] for g in got] == ["https://ex.org/w/1", "https://ex.org/w/2"]
    assert sess.navs == [INDEX, INDEX + "?p=2"]
    got = h5.collect_with(FakeIndex(pages), {**rule, "max_pages": 9}, wait=1, sleep=lambda s: None)
    assert [g["url"] for g in got][-1] == "https://ex.org/w/3"


def test_collect_with_clicks_a_load_more_button():
    pages = {INDEX: ["https://ex.org/w/1"], "after-click": ["https://ex.org/w/1", "https://ex.org/w/2"]}
    sess = FakeIndex(pages, next_kind="button")
    got = h5.collect_with(sess, {"page": INDEX, "next": "button.more", "max_pages": 3},
                          wait=1, sleep=lambda s: None)
    assert sess.clicks == [(5, 6)]
    assert [g["url"] for g in got] == ["https://ex.org/w/1", "https://ex.org/w/2"]


# ---------------------------------------------------------------- player
class FakePage:
    def __init__(self, url, has=(), rect=None, click=None):
        self.url_, self.has, self.rect, self.click_at, self.player_sel = url, has, rect, click, None

    def js(self, expr, timeout=20):
        if expr == "location.href":
            return self.url_
        return any(json.dumps(h) in expr for h in self.has)

    def js_json(self, expr):
        if "const sel = " in expr and "videos" in expr:
            self.player_sel = json.loads(expr.split("const sel = ", 1)[1].split(";", 1)[0])
            return dict(self.rect) if self.rect else None
        return self.click_at


def test_find_player_uses_matching_recipe_and_play_button():
    a = h5.Adapter()
    a.cfg = {"recipes": [{**GOOD, "match": {"url": "*", "page_has": ".video-js"},
                          "player": ".video-js", "play_button": ".vjs-big-play-button"}]}
    rect = {"x": 0, "y": 0, "w": 100, "h": 50, "where": "page", "index": 0, "src": ""}
    page = FakePage("https://x/", has=[".video-js"], rect=rect, click={"x": 50, "y": 25})
    out = a.find_player(page)
    assert page.player_sel == ".video-js" and out["click"] == [50, 25]
    # markup missing: no recipe, back to the largest-video guess
    page = FakePage("https://x/", rect=rect)
    out = a.find_player(page)
    assert page.player_sel == "" and "click" not in out
    # config player_selector beats the recipe
    a.cfg["player_selector"] = "#mine"
    page = FakePage("https://x/", has=[".video-js"], rect=rect)
    a.find_player(page)
    assert page.player_sel == "#mine"


def test_start_playback_clicks_the_named_spot(monkeypatch):
    from playcap import recorder
    clicks = []

    class S:
        def click(self, x, y):
            clicks.append((x, y))

    monkeypatch.setattr(recorder, "wait_playable", lambda p: {})
    monkeypatch.setattr(recorder.time, "sleep", lambda s: None)
    monkeypatch.setattr(recorder, "state", lambda p: {"found": True, "t": 1.0, "paused": False})
    rect = {"x": 0, "y": 0, "w": 100, "h": 50}
    recorder.start_playback(S(), None, {**rect, "click": [7, 8]})
    recorder.start_playback(S(), None, rect)
    assert clicks == [(7, 8), (50, 25)]


# ---------------------------------------------------------------- teach
class FakeTeachPage:
    """Answers the selector check as a page would, from a table."""

    def __init__(self, counts, preview=None):
        self.counts, self.preview = counts, preview or {"count": 0, "sample": []}

    def js_json(self, expr):
        if "data-playcap-pick" in expr:
            sels = json.loads(expr.split("const sels = ", 1)[1].split(", role", 1)[0])
            out = []
            for s in sels:
                count, hits, want = self.counts.get(s, (0, 0, 1))
                out.append({"sel": s, "count": count, "hits": hits, "want": want,
                            "first": hits > 0})
            return out
        return self.preview


def test_propose_picks_exact_player_and_link_set():
    player = [node("video", classes=["vjs-tech"]), node("div", id="main-player"), node("body")]
    links = [[node("a", classes=["talk-link"]), node("li", classes=["talk"], nth=n, same=9),
              node("ul", id="talks"), node("body")] for n in (1, 2)]
    page = FakeTeachPage({"video.vjs-tech": (2, 1, 1), "#main-player video.vjs-tech": (1, 1, 1),
                          "a.talk-link": (12, 2, 2), "#talks a.talk-link": (9, 2, 2)},
                         preview={"count": 9, "sample": [{"url": "u", "title": "T"}]})
    p = teach.propose(page, {"url": "https://ex.org/watch/3", "player": [player], "play": [],
                             "links": links})
    assert p["player"] == "#main-player video.vjs-tech"
    assert p["links"]["selector"] == "#talks a.talk-link" and p["links"]["count"] == 9
    assert p["match_url"] == "https://ex.org/watch/*"
    assert p["play_button"] is None


def test_teach_start_refuses_odd_urls_and_read_needs_a_picker(tmp_path):
    assert not teach.start(tmp_path, "javascript:alert(1)")[0]
    assert teach.read(tmp_path)["ok"] is False


# ---------------------------------------------------------------- server
from test_server import call, token_of, ui  # noqa: E402,F401  (fixture reused)


def test_recipe_endpoints_are_guarded_and_validated(ui):
    root, port = ui
    tok = token_of(port)
    assert call(port, "GET", "/static/recipes.js")[0] == 200
    body = {"recipe": GOOD}
    assert call(port, "POST", "/api/recipes/save", body)[0] == 403                 # no token
    assert call(port, "POST", "/api/recipes/save", body, token=tok,
                origin="http://evil.example")[0] == 403
    status, r = call(port, "POST", "/api/recipes/save", body, token=tok)
    assert status == 200 and r["ok"], r
    status, r = call(port, "GET", "/api/recipes")
    assert [x["name"] for x in r["recipes"]] == ["Talks"]
    status, r = call(port, "POST", "/api/recipes/import",
                     {"text": json.dumps({**GOOD, "name": "X", "script": "alert(1)"})}, token=tok)
    assert r["ok"] is False and "Unknown key" in r["message"]
    status, r = call(port, "POST", "/api/recipes/import", {"text": 5}, token=tok)
    assert r["ok"] is False
    status, r = call(port, "POST", "/api/recipes/delete", {"name": "Talks"}, token=tok)
    assert r["ok"]
    assert call(port, "GET", "/api/recipes")[1]["recipes"] == []
    status, r = call(port, "POST", "/api/teach/start", {"url": "chrome://settings"}, token=tok)
    assert r["ok"] is False
    status, r = call(port, "POST", "/api/recipes/nope", {}, token=tok)
    assert r["ok"] is False
