from playcap import obs_setup


class FakeObs:
    def __init__(self, kinds=("monitor_capture", "wasapi_output_capture"), scenes=(), inputs=()):
        self.kinds = list(kinds)
        self.scenes = {s: [] for s in scenes}
        self.inputs = dict(inputs)          # name -> kind
        self.current = next(iter(self.scenes), None)
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
    assert len(actions) == 3


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
    assert len(actions) == 2


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
