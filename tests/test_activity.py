from playcap import activity


def log(root, name, text):
    (root / name).write_text(text, encoding="utf-8")


def by_job(root):
    return {a["job"]: a for a in activity.summarize(root)}


def test_no_logs_no_lines(tmp_path):
    assert activity.summarize(tmp_path) == []


def test_record_run_with_failures_names_the_main_reason(tmp_path):
    log(tmp_path, "record_run.log", "\n".join([
        "3 to record | 0 done | 0 skipped by you | 0 not aired yet | 0 locked | ~6 h",
        "=== [1] 01 Aug 2026 [video] One",
        "    wrote S01E01 - One.mp4  (1.00 GB)",
        "",
        "1 recorded, 2 failed.",
        "   FAILED Two: CdpError: Chrome debug port unreachable at http://127.0.0.1:",
        "   FAILED Three: CdpError: Chrome debug port unreachable at http://127.0.0.1:",
    ]))
    a = by_job(tmp_path)["record"]
    assert a["level"] == "bad"
    assert a["text"].startswith("In total: 1 recorded, 2 failed.")
    assert "(2): The playcap browser was closed." in a["text"]
    assert a["when"] > 0


def test_record_only_reads_the_latest_run(tmp_path):
    log(tmp_path, "record_run.log", "\n".join([
        "2 to record | ...", "stop requested -- finishing here, before the next item",
        "0 recorded, 0 failed.",
        "1 to record | ...", "=== [4] 02 Aug 2026 [video] Four",
        "", "1 recorded, 0 failed.",
    ]))
    a = by_job(tmp_path)["record"]
    assert a == {**a, "text": "In total: 1 recorded, 0 failed.", "level": "ok"}


def test_record_in_progress_says_what(tmp_path):
    log(tmp_path, "record_run.log", "1 to record | ...\n=== [4] 02 Aug 2026 2:00 PM [video] Four\n    t=5/10s")
    assert by_job(tmp_path)["record"]["text"] == "Recording: Four"


def test_test_results(tmp_path):
    log(tmp_path, "test_run.log", "    mean luma = 0.4 (BLACK)\n")
    assert by_job(tmp_path)["test"]["level"] == "bad"
    log(tmp_path, "test_run.log", "    mean luma = 80.2 (OK, real picture)\nSmoke test finished.")
    assert by_job(tmp_path)["test"]["text"].startswith("Test passed")
    log(tmp_path, "test_run.log", "\n*** FAILED: no player after 60s\n")
    assert by_job(tmp_path)["test"]["text"] == "Test failed: No video player appeared on the page."


def test_queue_and_optimize_and_crash_fallbacks(tmp_path):
    log(tmp_path, "build_queue.log", "12 items -> queue.json   ({'video': 12}, 3 locked)\n")
    assert by_job(tmp_path)["queue"]["text"] == "Queue loaded: 12 items, 3 locked."
    log(tmp_path, "optimize.log", "[1/2] A\n\n9.0 GB -> 7.0 GB (2.0 GB reclaimed)\n")
    assert "9.0 GB → 7.0 GB" in by_job(tmp_path)["optimize"]["text"]
    log(tmp_path, "build_queue.log", "Traceback (most recent call last):\n  File x\nValueError: bad row\n")
    assert by_job(tmp_path)["queue"] == {**by_job(tmp_path)["queue"], "level": "bad",
                                         "text": "Loading the queue failed: ValueError: bad row"}


def test_unknown_output_falls_back_to_last_line(tmp_path):
    log(tmp_path, "chrome_launch.log", "something new\nlast words\n")
    assert by_job(tmp_path)["browser"]["text"] == "last words"


def test_black_test_names_the_configured_recorder(tmp_path):
    log(tmp_path, "test_run.log", "    mean luma = 0.4 (BLACK)\n")
    (tmp_path / "config.json").write_text('{"capture_backend": "ffmpeg"}')
    text = by_job(tmp_path)["test"]["text"]
    assert "ffmpeg captures the wrong screen" in text and "OBS" not in text
    (tmp_path / "config.json").write_text('{"capture_backend": "obs"}')
    assert "OBS captures the wrong screen" in by_job(tmp_path)["test"]["text"]
