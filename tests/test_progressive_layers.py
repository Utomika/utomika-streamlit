import importlib.util
import sys
import types
import unittest
from concurrent.futures import Future
from threading import Event
from unittest.mock import MagicMock, patch


for name in ("ee", "supabase"):
    if name not in sys.modules and importlib.util.find_spec(name) is None:
        module = types.ModuleType(name)
        if name == "supabase":
            module.create_client = MagicMock()
            module.ClientOptions = MagicMock()
        sys.modules[name] = module

from components import map_view
import resources
from scene_loader import SceneLoader


SCENE_A = ("zagreb", "scene-a", "L8", "T1")
SCENE_B = ("zagreb", "scene-b", "L8", "T1")


class ControlledPool:
    """Control network completion order without sleeps or actual services."""

    def __init__(self, **kwargs):
        self.requests = []

    def submit(self, fetcher, *args):
        future = Future()
        self.requests.append((fetcher, args, future))
        return future

    def finish(self, index, value="tile-url", error=None):
        future = self.requests[index][2]
        if error:
            future.set_exception(error)
        else:
            future.set_result(value)

    def shutdown(self, **kwargs):
        pass


class BackgroundLoaderTestCase(unittest.TestCase):
    def setUp(self):
        self.now = 0
        patcher = patch("scene_loader.ThreadPoolExecutor", ControlledPool)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.loader = SceneLoader(MagicMock(), MagicMock(), max_workers=1,
                                  ttl=30, clock=lambda: self.now)
        self.pool = self.loader._pool
        self.addCleanup(self.loader.close)

    def test_selected_ndvi_is_first_even_with_true_color_visible(self):
        self.loader.activate(SCENE_A, ["true_color", "ndvi", "cloud"], "ndvi")
        self.assertEqual(self.pool.requests[0][1], (*SCENE_A, "ndvi"))
        self.assertEqual(self.loader.snapshot(SCENE_A), {})

    def test_newest_scene_replaces_queued_work_and_late_results_are_isolated(self):
        self.loader.activate(SCENE_A)
        self.loader.activate(SCENE_B, ["ndvi"], "ndvi")
        self.pool.finish(0, "a-true")
        self.assertEqual(self.pool.requests[1][1], (*SCENE_B, "ndvi"))
        self.assertEqual(self.loader.snapshot(SCENE_B), {})
        self.assertEqual(self.loader.snapshot(SCENE_A)["true_color"]["value"], "a-true")
        self.pool.finish(1, "b-ndvi")
        self.assertEqual(self.loader.snapshot(SCENE_B)["ndvi"]["value"], "b-ndvi")

    def test_polls_reuse_inflight_and_completed_layers(self):
        self.loader.activate(SCENE_A)
        self.loader.activate(SCENE_A)
        self.assertEqual(len(self.pool.requests), 1)
        self.pool.finish(0)
        self.loader.activate(SCENE_A)
        self.assertEqual(len(self.pool.requests), 2)  # cloud, not true color again
        self.assertEqual(self.pool.requests[1][1][-1], "cloud")

    def test_revisit_reuses_cached_ndvi_without_loading_other_layers_first(self):
        self.loader.activate(SCENE_A, ["ndvi"], "ndvi")
        self.pool.finish(0, "a-ndvi")
        self.loader.activate(SCENE_B, ["ndvi"], "ndvi")
        self.loader.activate(SCENE_A, ["ndvi"], "ndvi")
        self.assertEqual(self.loader.snapshot(SCENE_A)["ndvi"]["value"], "a-ndvi")
        self.assertEqual(sum(args == (*SCENE_A, "ndvi")
                             for _, args, _ in self.pool.requests), 1)

    def test_each_url_expires_from_its_own_completion_time(self):
        self.loader.activate(SCENE_A)
        self.now = 10
        self.pool.finish(0, "true")
        self.now = 20
        self.pool.finish(1, "cloud")
        self.now = 41
        snapshot = self.loader.snapshot(SCENE_A)
        self.assertNotIn("true_color", snapshot)
        self.assertIn("cloud", snapshot)
        self.loader.activate(SCENE_A)
        self.pool.finish(2)
        self.assertEqual(self.pool.requests[3][1][-1], "true_color")

    def test_failed_layer_does_not_repeat_until_retry(self):
        self.loader.activate(SCENE_A, ["ndvi"], "ndvi")
        self.pool.finish(0, error=RuntimeError("unavailable"))
        self.assertIn("unavailable", self.loader.snapshot(SCENE_A)["ndvi"]["error"])
        self.loader.activate(SCENE_A, ["ndvi"], "ndvi")
        self.assertEqual(len(self.pool.requests), 2)
        self.loader.retry(SCENE_A, "ndvi")
        self.pool.finish(1)
        self.assertEqual(self.pool.requests[2][1], (*SCENE_A, "ndvi"))

    def test_prefetch_uses_only_visible_layers_of_filtered_neighbors(self):
        self.loader.activate(SCENE_A, ["ndvi"], "ndvi", (SCENE_B,))
        self.assertNotIn((SCENE_B, "ndvi"), self.loader._wanted)
        self.pool.finish(0)
        self.loader.activate(SCENE_A, ["ndvi"], "ndvi", (SCENE_B,))
        self.assertIn((SCENE_B, "ndvi"), self.loader._wanted)
        self.assertNotIn((SCENE_B, "lst"), self.loader._wanted)
        self.assertEqual(self.loader._wanted[-1], (SCENE_B, "ndvi"))

    def test_cache_is_bounded_and_close_stops_followup_work(self):
        self.loader._max_entries = 2
        self.loader.activate(SCENE_A)
        for index in range(3):
            self.pool.finish(index)
        self.assertLessEqual(len(self.loader._cache), 2)
        count = len(self.pool.requests)
        self.loader.close()
        self.pool.finish(count - 1)
        self.assertEqual(len(self.pool.requests), count)


class RealWorkerTestCase(unittest.TestCase):
    def test_activate_and_poll_return_while_network_is_blocked(self):
        entered, release = Event(), Event()

        def fetch(*args):
            entered.set()
            release.wait(5)
            return "url"

        loader = SceneLoader(fetch, fetch, max_workers=1)
        try:
            loader.activate(SCENE_A)
            self.assertTrue(entered.wait(1))
            self.assertEqual(loader.snapshot(SCENE_A), {})
            loader.activate(SCENE_B, ["ndvi"], "ndvi")
            self.assertEqual(loader.snapshot(SCENE_B), {})
        finally:
            loader.close()
            release.set()


class MapStateTestCase(unittest.TestCase):
    def setUp(self):
        self.state = {}
        patcher = patch.object(map_view.st, "session_state", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_selection_survives_new_scene_payload_and_ignores_stale_retry(self):
        loader = MagicMock()
        self.state.update({map_view._ACTIVE_KEY: SCENE_B,
                           map_view._LOADER_KEY: loader,
                           map_view._COMPONENT_KEY: {
                               "event_id": "choice-1", "scene_id": map_view._scene_id(SCENE_A),
                               "visible": ["ndvi", "cloud"], "preferred": "ndvi",
                               "cloud_opacity": 0.35, "retry": "ndvi", "boundary": False,
                           }})
        map_view._on_map_change()
        payload = map_view._map_payload(SCENE_B, {})
        self.assertEqual(payload["preferences"]["visible"], ["ndvi", "cloud"])
        self.assertEqual(payload["preferences"]["preferred"], "ndvi")
        self.assertEqual(payload["preferences"]["cloud_opacity"], 0.35)
        self.assertFalse(payload["preferences"]["boundary"])
        self.assertTrue(all(layer["url"] is None for layer in payload["layers"]))
        loader.retry.assert_not_called()

    def test_current_retry_is_processed_once(self):
        loader = MagicMock()
        self.state.update({map_view._ACTIVE_KEY: SCENE_A,
                           map_view._LOADER_KEY: loader,
                           map_view._COMPONENT_KEY: {
                               "event_id": "retry-1", "scene_id": map_view._scene_id(SCENE_A),
                               "retry": "ndvi",
                           }})
        map_view._on_map_change()
        map_view._on_map_change()
        loader.retry.assert_called_once_with(SCENE_A, "ndvi")

    def test_render_uses_same_component_key_for_loading_and_new_scene(self):
        loader = MagicMock()
        loader.snapshot.return_value = {}
        self.state[map_view._LOADER_KEY] = loader
        with (patch.object(map_view, "_scene_map") as component,
              patch.object(map_view.resources, "roi_view", return_value=([0, 0], {})),
              patch.object(map_view.st, "caption")):
            for scene in (SCENE_A, SCENE_B):
                with patch.object(map_view, "_render_scene_map"):
                    map_view.render_progressive_map(scene[0], {
                        "image_id": scene[1], "sat": scene[2], "tier": scene[3],
                    })
                map_view._render_scene_map.__wrapped__()
        self.assertEqual([call.kwargs["key"] for call in component.call_args_list],
                         [map_view._COMPONENT_KEY] * 2)
        self.assertEqual([call.args[0] for call in loader.activate.call_args_list],
                         [SCENE_A, SCENE_B])
        self.assertEqual(self.state[map_view._ACTIVE_KEY], SCENE_B)

    def test_cloud_stats_timer_reads_latest_request_without_blocking(self):
        loader = MagicMock()
        loader.snapshot.return_value = {"stats": {"error": None, "value": {
            "roi_cloud_pct": 12, "roi_cloud_of_valid_pct": 15, "roi_valid_pct": 80,
        }}}
        self.state[map_view._LOADER_KEY] = loader
        with (patch.object(map_view.st, "markdown"), patch.object(map_view.st, "write"),
              patch.object(map_view.st, "caption") as caption):
            for scene, pct in ((SCENE_A, 12.3), (SCENE_B, 45.6)):
                with patch.object(map_view, "_render_live_cloud_stats"):
                    map_view.render_live_cloud_stats(scene[0], {
                        "image_id": scene[1], "sat": scene[2], "tier": scene[3],
                        "roi_cloud_pct": pct,
                    })
                map_view._render_live_cloud_stats.__wrapped__()
        self.assertEqual([call.args[0] for call in loader.snapshot.call_args_list],
                         [SCENE_A, SCENE_B])
        self.assertIn("45.6%", caption.call_args.args[0])


class SceneResourceTestCase(unittest.TestCase):
    def test_visualizer_builds_only_the_requested_layer(self):
        image, roi, ndvi = MagicMock(), MagicMock(), MagicMock()
        with (
            patch.object(resources.gee_app, "ndvi", return_value=ndvi) as build_ndvi,
            patch.object(resources.gc, "true_color") as build_true_color,
            patch.object(resources.gc, "lst_celsius") as build_lst,
            patch.object(resources.gee_app, "eci") as build_eci,
        ):
            result = resources._visualized_scene_layer(image, roi, "ndvi")
        self.assertIs(result, ndvi.visualize.return_value)
        build_ndvi.assert_called_once_with(image)
        build_true_color.assert_not_called()
        build_lst.assert_not_called()
        build_eci.assert_not_called()

    def test_worker_does_not_use_streamlit_roi_cache(self):
        with (patch.object(resources, "roi", side_effect=AssertionError("Streamlit in worker")),
              patch.object(resources.gee_app, "load_roi_geojson"),
              patch.object(resources.gee_app, "load_scene"),
              patch.object(resources, "_visualized_scene_layer"),
              patch.object(resources.gee_app, "tile_layer_url", return_value="ndvi-url")):
            self.assertEqual(resources.fetch_scene_tile(*SCENE_A, "ndvi"), "ndvi-url")

    def test_region_view_uses_local_boundary_without_earth_engine(self):
        with patch.object(resources, "roi", side_effect=AssertionError("Earth Engine request")):
            for city in ("zagreb", "split"):
                center, boundary = resources.roi_view.__wrapped__(city)
                self.assertEqual(len(center), 2)
                self.assertTrue(40 < center[0] < 50)
                self.assertIn(boundary["type"], ("Polygon", "MultiPolygon"))

    def test_unknown_scene_layer_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown scene layer"):
            resources._visualized_scene_layer(MagicMock(), MagicMock(), "bogus")


if __name__ == "__main__":
    unittest.main()
