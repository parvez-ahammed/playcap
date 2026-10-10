from playcap import obs_setup


def mon(name, value):
    return {"itemEnabled": True, "itemName": name, "itemValue": value}


# The live setup that exposed the bug: portrait primary, landscape secondary.
MONITORS = [mon("MSI G241V: 1920x1080 @ -1920,8", "LANDSCAPE"),
            mon("MSI G241V: 1080x1920 @ 0,0 (Primary Monitor)", "PORTRAIT")]


# What OBS gives a scene item created over the websocket: no bounds, scale 1, at 0,0.
DEFAULT_TRANSFORM = {"boundsType": "OBS_BOUNDS_NONE", "boundsWidth": 0.0, "boundsHeight": 0.0,
                     "scaleX": 1.0, "scaleY": 1.0, "positionX": 0.0, "positionY": 0.0,
                     "rotation": 0.0, "alignment": 5}


class FakeObs:
    def __init__(self, kinds=("monitor_capture", "wasapi_output_capture"), scenes=(), inputs=(),
                 monitors=None, canvas=(1920, 1080)):
        monitors = MONITORS if monitors is None else monitors
        self.kinds = list(kinds)
        self.scenes = {s: [] for s in scenes}
        self.inputs = dict(inputs)          # name -> kind
        self.current = next(iter(self.scenes), None)
        self.settings = {}                  # name -> inputSettings
        self.monitors = list(monitors)
        self.canvas = canvas
        self.transforms = {}                # source name -> sceneItemTransform
        self.log = []

    def request(self, kind, data=None, timeout=20):
        data = data or {}
        self.log.append(kind)
        if kind == "GetInputKindList":
            return {"inputKinds": self.kinds}
        if kind == "GetSceneList":
            return {"scenes": [{"sceneName": s} for s in self.scenes],
                    "currentProgramSceneName": self.current}
        if kind == "CreateScene":
            self.scenes[data["sceneName"]] = []
            return {}
        if kind == "GetSceneItemList":
            return {"sceneItems": [{"sourceName": n, "inputKind": self.inputs.get(n)}
                                   for n in self.scenes[data["sceneName"]]]}
        if kind == "GetInputList":
            return {"inputs": [{"inputName": n, "inputKind": k} for n, k in self.inputs.items()]}
        if kind == "CreateInput":
            self.inputs[data["inputName"]] = data["inputKind"]
            self.scenes[data["sceneName"]].append(data["inputName"])
            return {}
        if kind == "CreateSceneItem":
            self.scenes[data["sceneName"]].append(data["sourceName"])
            return {}
        if kind == "GetInputSettings":
            # OBS fills an unset monitor_capture with "DUMMY": records black.
            return {"inputSettings": {"monitor_id": "DUMMY",
                                      **self.settings.get(data["inputName"], {})}}
        if kind == "SetInputSettings":
            self.settings.setdefault(data["inputName"], {}).update(data["inputSettings"])
            return {}
        if kind == "GetInputPropertiesListPropertyItems":
            return {"propertyItems": self.monitors}
        if kind == "GetVideoSettings":
            return {"baseWidth": self.canvas[0], "baseHeight": self.canvas[1]}
        if kind == "GetSceneItemId":
            assert data["sourceName"] in self.scenes[data["sceneName"]]
            return {"sceneItemId": self.scenes[data["sceneName"]].index(data["sourceName"]) + 1}
        if kind == "GetSceneItemTransform":
            name = self.scenes[data["sceneName"]][data["sceneItemId"] - 1]
            return {"sceneItemTransform": dict(self.transforms.get(name, DEFAULT_TRANSFORM))}
        if kind == "SetSceneItemTransform":
            name = self.scenes[data["sceneName"]][data["sceneItemId"] - 1]
            self.transforms[name] = {**self.transforms.get(name, DEFAULT_TRANSFORM),
                                     **data["sceneItemTransform"]}
            return {}
        if kind == "SetCurrentProgramScene":
            self.current = data["sceneName"]
            return {}
        raise AssertionError(f"unexpected request {kind}")


def test_fresh_obs_gets_scene_capture_and_is_switched():
    obs = FakeObs(scenes=["Scene"])
    actions = obs_setup.ensure(obs)
    assert obs.scenes["playcap"] == [obs_setup.CAPTURE_NAME]
    assert obs.inputs[obs_setup.CAPTURE_NAME] == "monitor_capture"
    assert obs.current == "playcap"
    assert obs.settings[obs_setup.CAPTURE_NAME]["monitor_id"] == "LANDSCAPE"
    assert obs.transforms[obs_setup.CAPTURE_NAME]["boundsType"] == "OBS_BOUNDS_SCALE_INNER"
    assert len(actions) == 5


def test_second_run_changes_nothing():
    obs = FakeObs(scenes=["Scene"])
    obs_setup.ensure(obs)
    assert obs_setup.ensure(obs) == []


def test_existing_input_is_reused_not_duplicated():
    obs = FakeObs(scenes=["playcap"], inputs={obs_setup.CAPTURE_NAME: "monitor_capture"})
    obs.current = "Other"
    obs.scenes["Other"] = []
    actions = obs_setup.ensure(obs)
    assert "CreateInput" not in obs.log
    assert obs.scenes["playcap"] == [obs_setup.CAPTURE_NAME]
    assert obs.current == "playcap"
    assert len(actions) == 4       # scene item, monitor, fit to canvas, program scene


def test_scene_with_any_capture_kind_counts():
    obs = FakeObs(scenes=["playcap"], inputs={"My screen": "monitor_capture"})
    obs.scenes["playcap"] = ["My screen"]
    assert obs_setup.ensure(obs) == []


def test_macos_and_linux_kinds():
    assert obs_setup.capture_kind(["screen_capture", "coreaudio_input_capture"]) == "screen_capture"
    assert obs_setup.capture_kind(["xshm_input"]) == "xshm_input"
    assert obs_setup.capture_kind(["pipewire-desktop-capture-source"]) == "pipewire-desktop-capture-source"
    assert obs_setup.capture_kind(["browser_source"]) is None


def test_no_capture_kind_reports_problem():
    obs = FakeObs(kinds=["browser_source"], scenes=["Scene"])
    actions = obs_setup.ensure(obs)
    assert any("no screen capture" in a.lower() for a in actions)


def test_clear_crash_markers_only_when_closed(tmp_path, monkeypatch):
    env = {"APPDATA": str(tmp_path)}
    sent = tmp_path / "obs-studio" / ".sentinel"
    sent.mkdir(parents=True)
    for n in ("run_a", "run_b"):
        (sent / n).write_text("")
    (sent / "keep.txt").write_text("")
    monkeypatch.setattr(obs_setup, "is_obs_running", lambda: True)
    assert obs_setup.clear_crash_markers(env=env, platform="win32") == 0
    monkeypatch.setattr(obs_setup, "is_obs_running", lambda: False)
    assert obs_setup.clear_crash_markers(env=env, platform="win32") == 2
    assert [p.name for p in sent.iterdir()] == ["keep.txt"]


def test_existing_capture_with_dummy_monitor_is_repaired():
    obs = FakeObs(scenes=["playcap"], inputs={obs_setup.CAPTURE_NAME: "monitor_capture"})
    obs.scenes["playcap"] = [obs_setup.CAPTURE_NAME]
    actions = obs_setup.ensure(obs)
    assert obs.settings[obs_setup.CAPTURE_NAME]["monitor_id"] == "LANDSCAPE"
    assert "pointed" in actions[0] and "fit" in actions[1] and len(actions) == 2


def test_hand_picked_monitor_is_kept():
    obs = FakeObs(scenes=["playcap"], inputs={obs_setup.CAPTURE_NAME: "monitor_capture"})
    obs.scenes["playcap"] = [obs_setup.CAPTURE_NAME]
    obs.settings[obs_setup.CAPTURE_NAME] = {"monitor_id": "PORTRAIT"}
    obs.transforms[obs_setup.CAPTURE_NAME] = {**DEFAULT_TRANSFORM, "scaleX": 0.5625, "scaleY": 0.5625}
    assert obs_setup.ensure(obs) == []
    assert obs.settings[obs_setup.CAPTURE_NAME]["monitor_id"] == "PORTRAIT"


def test_vanished_monitor_is_replaced():
    obs = FakeObs(scenes=["playcap"], inputs={obs_setup.CAPTURE_NAME: "monitor_capture"})
    obs.scenes["playcap"] = [obs_setup.CAPTURE_NAME]
    obs.settings[obs_setup.CAPTURE_NAME] = {"monitor_id": "UNPLUGGED"}
    obs_setup.ensure(obs)
    assert obs.settings[obs_setup.CAPTURE_NAME]["monitor_id"] == "LANDSCAPE"


def test_pick_monitor_prefers_canvas_size_then_primary():
    a = mon("A: 2560x1440 @ 0,0 (Primary Monitor)", "A")
    b = mon("B: 1920x1080 @ 2560,0", "B")
    c = mon("C: 1920x1080 @ -1920,0 (Primary Monitor)", "C")
    assert obs_setup.pick_monitor([a, b], 1920, 1080)["itemValue"] == "B"
    assert obs_setup.pick_monitor([a, b, c], 1920, 1080)["itemValue"] == "C"
    assert obs_setup.pick_monitor([b, a], 3840, 2160)["itemValue"] == "A"   # no fit: primary
    assert obs_setup.pick_monitor([], 1920, 1080) is None


def test_non_windows_capture_is_not_touched():
    obs = FakeObs(kinds=["xshm_input"], scenes=["Scene"])
    obs_setup.ensure(obs)
    assert "SetInputSettings" not in obs.log


def test_untouched_capture_is_fit_to_canvas():
    # Live run: OBS canvas 1920x1080, only a 1080x1920 portrait monitor connected.
    # At OBS's default transform the capture showed the top-left 1920x1080 of
    # the screen: the fullscreened video, centred lower down, was cut in half.
    obs = FakeObs(scenes=["Scene"], monitors=[MONITORS[1]])
    actions = obs_setup.ensure(obs)
    t = obs.transforms[obs_setup.CAPTURE_NAME]
    assert t["boundsType"] == "OBS_BOUNDS_SCALE_INNER"
    assert (t["boundsWidth"], t["boundsHeight"]) == (1920, 1080)
    assert (t["positionX"], t["positionY"]) == (0, 0)
    assert any("fit" in a for a in actions)
    assert obs_setup.ensure(obs) == []           # idempotent


def test_hand_placed_capture_transform_is_kept():
    obs = FakeObs(scenes=["playcap"], inputs={obs_setup.CAPTURE_NAME: "monitor_capture"})
    obs.scenes["playcap"] = [obs_setup.CAPTURE_NAME]
    obs.settings[obs_setup.CAPTURE_NAME] = {"monitor_id": "LANDSCAPE"}
    obs.transforms[obs_setup.CAPTURE_NAME] = {**DEFAULT_TRANSFORM, "positionX": 40.0}
    assert obs_setup.ensure(obs) == []
    assert "SetSceneItemTransform" not in obs.log


def test_capture_in_users_own_scene_item_is_not_refit():
    obs = FakeObs(scenes=["playcap"], inputs={"My screen": "monitor_capture"})
    obs.scenes["playcap"] = ["My screen"]
    obs_setup.ensure(obs)
    assert "SetSceneItemTransform" not in obs.log
