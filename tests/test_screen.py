from playcap import obs_setup, screen

LANDSCAPE = {"itemEnabled": True, "itemName": "MSI G241V: 1920x1080 @ -1920,8", "itemValue": "L"}
PORTRAIT = {"itemEnabled": True, "itemName": "MSI G241V: 1080x1920 @ 0,0 (Primary Monitor)",
            "itemValue": "P"}

# What CDP reported for the maximized debug Chrome on the live machine.
CHROME = {"left": -1928, "top": 0, "width": 1936, "height": 1048}


def test_monitor_geometry_parses_negative_positions():
    assert screen.monitor_geometry(LANDSCAPE["itemName"]) == (-1920, 8, 1920, 1080)
    assert screen.monitor_geometry(PORTRAIT["itemName"]) == (0, 0, 1080, 1920)
    assert screen.monitor_geometry("Some monitor") is None


def test_monitor_for_uses_window_centre():
    assert screen.monitor_for(CHROME, [PORTRAIT, LANDSCAPE]) is LANDSCAPE
    on_primary = {"left": 0, "top": 0, "width": 1080, "height": 1900}
    assert screen.monitor_for(on_primary, [PORTRAIT, LANDSCAPE]) is PORTRAIT
    assert screen.monitor_for({"left": 9000, "top": 0, "width": 10, "height": 10},
                              [PORTRAIT, LANDSCAPE]) is None


class FakeObs:
    def __init__(self, kind="monitor_capture", current="P"):
        self.kind, self.current, self.set = kind, current, []

    def request(self, kind, data=None, timeout=20):
        if kind == "GetInputList":
            return {"inputs": [{"inputName": obs_setup.CAPTURE_NAME, "inputKind": self.kind}]}
        if kind == "GetInputPropertiesListPropertyItems":
            return {"propertyItems": [PORTRAIT, LANDSCAPE]}
        if kind == "GetInputSettings":
            return {"inputSettings": {"monitor_id": self.current}}
        if kind == "SetInputSettings":
            self.set.append(data["inputSettings"]["monitor_id"])
            self.current = data["inputSettings"]["monitor_id"]
            return {}
        raise AssertionError(kind)


def test_aim_capture_moves_to_browser_monitor(monkeypatch):
    monkeypatch.setattr(screen.time, "sleep", lambda s: None)
    obs = FakeObs(current="P")
    assert "1920x1080" in screen.aim_capture(obs, CHROME)
    assert obs.set == ["L"]
    assert screen.aim_capture(obs, CHROME) is None          # already right
    assert obs.set == ["L"]


def test_aim_capture_leaves_other_kinds_and_missing_bounds_alone():
    obs = FakeObs(kind="xshm_input")
    assert screen.aim_capture(obs, CHROME) is None
    assert screen.aim_capture(FakeObs(), None) is None


def test_pin_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(screen.sys, "platform", "linux")
    with screen.Pin(sess=None) as pin:
        assert pin.hwnd is None and pin.note == ""
