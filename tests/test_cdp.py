"""cdp.open_page returns the tab it opened, never a look-alike (mocked HTTP)."""
import pytest

from playcap import cdp


class Resp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


OTHER = {"id": "OLD", "type": "page", "url": "file:///C:/elsewhere/old.html",
         "webSocketDebuggerUrl": "ws://old"}


def test_open_page_returns_the_new_target_for_file_urls(monkeypatch):
    url = "file:///C:/demo/page/index.html"
    new = {"id": "NEW", "type": "page", "url": url, "webSocketDebuggerUrl": "ws://new"}
    seen = []
    monkeypatch.setattr(cdp.requests, "put", lambda u, timeout: seen.append(u) or Resp(new))
    # A host lookup ("" for file://) would have matched this older tab first.
    monkeypatch.setattr(cdp.requests, "get", lambda u, timeout: Resp([OTHER, new]))
    assert cdp.open_page(url)["id"] == "NEW"
    assert seen == [f"{cdp.CDP_HTTP}/json/new?{url}"]


def test_open_page_looks_up_by_id_when_the_answer_lacks_a_socket(monkeypatch):
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    monkeypatch.setattr(cdp.requests, "put",
                        lambda u, timeout: Resp({"id": "NEW", "type": "page", "url": "about:blank"}))
    lists = iter([[OTHER], [OTHER, {"id": "NEW", "type": "page", "url": "https://x/",
                                    "webSocketDebuggerUrl": "ws://new"}]])
    monkeypatch.setattr(cdp.requests, "get", lambda u, timeout: Resp(next(lists)))
    assert cdp.open_page("https://x/")["webSocketDebuggerUrl"] == "ws://new"


def test_open_page_errors(monkeypatch):
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)

    def down(u, timeout):
        raise ConnectionError("refused")
    monkeypatch.setattr(cdp.requests, "put", down)
    with pytest.raises(cdp.CdpError, match="could not open a tab"):
        cdp.open_page("https://x/")
    monkeypatch.setattr(cdp.requests, "put", lambda u, timeout: Resp({"no": "id"}))
    with pytest.raises(cdp.CdpError, match="no target"):
        cdp.open_page("https://x/")
    monkeypatch.setattr(cdp.requests, "put", lambda u, timeout: Resp({"id": "NEW"}))
    monkeypatch.setattr(cdp.requests, "get", lambda u, timeout: Resp([OTHER]))
    with pytest.raises(cdp.CdpError, match="never appeared"):
        cdp.open_page("https://x/", tries=2)
