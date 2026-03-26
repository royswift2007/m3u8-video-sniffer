import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from core.m3u8_parser import M3U8FetchThread
from core.playwright_driver import PlaywrightDriver
from core.task_model import DownloadTask
from engines.n_m3u8dl_re import N_m3u8DL_RE_Engine


def assert_true(expr, msg):
    if not expr:
        raise AssertionError(msg)


def test_playwright_page_config_dedup_guard():
    driver = PlaywrightDriver(headless=True)

    class DummyPage:
        pass

    p = DummyPage()
    first = driver._remember_page_configured(p)
    second = driver._remember_page_configured(p)
    assert_true(first is False, "first config check should be False")
    assert_true(second is True, "second config check should be True")


def test_m3u8_nested_depth_and_loop_detection():
    thread = M3U8FetchThread("https://a.test/master.m3u8", headers={})

    master_content = """#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=640x360\n/sub/master2.m3u8\n"""
    nested_master = """#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000,RESOLUTION=1280x720\nhttps://a.test/master.m3u8\n"""

    def fake_fetch(url, headers):
        if url.endswith("master.m3u8"):
            return master_content
        if url.endswith("master2.m3u8"):
            return nested_master
        return "#EXTM3U\n#EXTINF:5,\nseg0.ts\n"

    thread._fetch_once = fake_fetch
    variants = thread._parse_m3u8_variants(master_content, "https://a.test/master.m3u8")
    resolved = thread._resolve_nested_variants(variants, headers={}, depth=0, visited={"https://a.test/master.m3u8"})

    assert_true(isinstance(resolved, list), "resolved must be list")
    assert_true(len(resolved) >= 1, "resolved variants should not be empty")
    # loop URL should not recurse infinitely; list should stay finite.
    assert_true(len(resolved) < 10, "resolved variants should remain bounded")


def test_nm3u8dlre_master_media_candidates():
    engine = N_m3u8DL_RE_Engine("bin/N_m3u8DL-RE.exe")
    task = DownloadTask(
        url="https://a.test/master.m3u8",
        save_dir="C:/tmp",
        filename="x",
        headers={},
    )
    task.master_url = "https://a.test/master.m3u8"
    task.media_url = "https://a.test/media/index.m3u8"

    candidates = engine._build_url_candidates(task)
    labels = [x[1] for x in candidates]
    urls = [x[0] for x in candidates]

    assert_true(labels[0] == "primary", "first candidate should be primary")
    # primary may deduplicate master when they are the same URL.
    assert_true("media" in labels, "candidates should include media fallback")
    assert_true("https://a.test/master.m3u8" in urls, "master url missing")
    assert_true("https://a.test/media/index.m3u8" in urls, "media url missing")


if __name__ == "__main__":
    tests = [
        test_playwright_page_config_dedup_guard,
        test_m3u8_nested_depth_and_loop_detection,
        test_nm3u8dlre_master_media_candidates,
    ]
    for fn in tests:
        fn()
        print(f"PASS: {fn.__name__}")
    print("S5 smoke passed")

