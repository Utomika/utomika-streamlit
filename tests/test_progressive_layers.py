import importlib.util
import sys
import types
import unittest
from unittest.mock import MagicMock, call, patch


# The project's production dependencies are installed by requirements.txt, but
# the lightweight unit-test environment used in this repository only includes
# Streamlit. Stub rendering/service modules so the pure orchestration logic can
# still be exercised without Earth Engine credentials or a browser component.
if importlib.util.find_spec("folium") is None:
    folium = types.ModuleType("folium")
    folium.Map = MagicMock
    folium.TileLayer = MagicMock
    folium.GeoJson = MagicMock
    folium.LayerControl = MagicMock
    sys.modules["folium"] = folium

if importlib.util.find_spec("branca") is None:
    branca = types.ModuleType("branca")
    branca_element = types.ModuleType("branca.element")
    branca_element.MacroElement = object
    branca.element = branca_element
    sys.modules["branca"] = branca
    sys.modules["branca.element"] = branca_element

if importlib.util.find_spec("streamlit_folium") is None:
    streamlit_folium = types.ModuleType("streamlit_folium")
    streamlit_folium.st_folium = MagicMock()
    sys.modules["streamlit_folium"] = streamlit_folium

if importlib.util.find_spec("ee") is None:
    sys.modules["ee"] = types.ModuleType("ee")

if importlib.util.find_spec("supabase") is None:
    supabase = types.ModuleType("supabase")
    supabase.create_client = MagicMock()
    sys.modules["supabase"] = supabase

from components import map_view
import resources


class ProgressiveLayerTestCase(unittest.TestCase):
    def setUp(self):
        self.anchor = ("zagreb", "scene-1", "L8", "T1")
        self.row = {
            "image_id": "scene-1",
            "sat": "L8",
            "tier": "T1",
            "roi_cloud_pct": 12.3,
        }

    def test_first_pass_requests_only_true_color(self):
        state = map_view._new_layer_state(self.anchor)
        with (
            patch.object(map_view, "_layer_state", return_value=state),
            patch.object(map_view, "_loading_scene_map") as loading_fragment,
            patch.object(
                map_view.resources, "scene_tile", return_value="true-url"
            ) as scene_tile,
        ):
            map_view.render_progressive_map("zagreb", self.row)

        scene_tile.assert_called_once_with(
            "zagreb", "scene-1", "L8", "T1", "true_color"
        )
        self.assertEqual(state["tiles"], {"True color": "true-url"})
        self.assertEqual(state["next_index"], 0)
        loading_fragment.assert_called_once_with(
            "zagreb", "scene-1", "L8", "T1"
        )

    def test_optional_layers_advance_in_order_and_skip_a_failure(self):
        state = map_view._new_layer_state(self.anchor)
        state["tiles"]["True color"] = "true-url"
        results = [
            "cloud-url",
            RuntimeError("NDVI unavailable"),
            "lst-url",
            "eci-url",
        ]

        with patch.object(
            map_view.resources, "scene_tile", side_effect=results
        ) as scene_tile:
            for _ in map_view._OPTIONAL_LAYERS:
                map_view._advance_optional_layer(
                    state, "zagreb", "scene-1", "L8", "T1"
                )

        self.assertEqual(
            scene_tile.call_args_list,
            [
                call("zagreb", "scene-1", "L8", "T1", "cloud"),
                call("zagreb", "scene-1", "L8", "T1", "ndvi"),
                call("zagreb", "scene-1", "L8", "T1", "lst"),
                call("zagreb", "scene-1", "L8", "T1", "eci"),
            ],
        )
        self.assertEqual(
            list(state["tiles"]),
            ["True color", "Cloud / shadow mask", "LST (°C)", "ECI"],
        )
        self.assertIn("NDVI unavailable", state["errors"]["NDVI"])
        self.assertEqual(state["next_index"], len(map_view._OPTIONAL_LAYERS))

    def test_true_color_failure_is_not_swallowed(self):
        state = map_view._new_layer_state(self.anchor)
        with (
            patch.object(map_view, "_layer_state", return_value=state),
            patch.object(map_view, "_loading_scene_map"),
            patch.object(
                map_view.resources,
                "scene_tile",
                side_effect=RuntimeError("base unavailable"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "base unavailable"):
                map_view.render_progressive_map("zagreb", self.row)

    def test_scene_change_resets_progress(self):
        session_state = {}
        with patch.object(map_view.st, "session_state", session_state):
            first = map_view._layer_state("zagreb", "scene-1", "L8", "T1")
            first["tiles"]["True color"] = "true-url"
            first["next_index"] = 2

            same = map_view._layer_state("zagreb", "scene-1", "L8", "T1")
            changed = map_view._layer_state("zagreb", "scene-2", "L8", "T1")

        self.assertIs(same, first)
        self.assertIsNot(changed, first)
        self.assertEqual(changed["tiles"], {})
        self.assertEqual(changed["next_index"], 0)

    def test_analytical_layers_are_added_hidden(self):
        layer_instances = []

        def tile_layer(*args, **kwargs):
            instance = MagicMock()
            instance.args = args
            instance.kwargs = kwargs
            layer_instances.append(instance)
            return instance

        tiles = {
            "True color": "true-url",
            "Cloud / shadow mask": "cloud-url",
            "NDVI": "ndvi-url",
            "LST (°C)": "lst-url",
            "ECI": "eci-url",
        }
        with (
            patch.object(map_view.resources, "roi_view", return_value=([0, 0], {})),
            patch.object(map_view.folium, "Map", return_value=MagicMock()),
            patch.object(map_view.folium, "TileLayer", side_effect=tile_layer),
            patch.object(map_view.folium, "GeoJson", return_value=MagicMock()),
            patch.object(map_view.folium, "LayerControl", return_value=MagicMock()),
            patch.object(map_view, "add_png_export"),
            patch.object(map_view, "st_folium"),
        ):
            map_view.render_map("zagreb", self.row, tiles, "test-map")

        overlays = {
            layer.kwargs["name"]: layer.kwargs
            for layer in layer_instances
            if "name" in layer.kwargs
        }
        self.assertTrue(overlays["True color"]["show"])
        self.assertTrue(overlays["Cloud / shadow mask"]["show"])
        self.assertFalse(overlays["NDVI"]["show"])
        self.assertFalse(overlays["LST (°C)"]["show"])
        self.assertFalse(overlays["ECI"]["show"])
        self.assertEqual(overlays["Cloud / shadow mask"]["opacity"], 0.55)

    def test_live_stats_failure_is_non_fatal(self):
        state = map_view._new_stats_state(self.anchor)
        with patch.object(
            map_view.resources,
            "scene_cloud_stats",
            side_effect=RuntimeError("stats unavailable"),
        ):
            map_view._fetch_cloud_stats(
                state, "zagreb", "scene-1", "L8", "T1"
            )

        self.assertTrue(state["done"])
        self.assertIsNone(state["numbers"])
        self.assertIn("stats unavailable", state["error"])


class SceneResourceTestCase(unittest.TestCase):
    def test_visualizer_builds_only_the_requested_layer(self):
        image = MagicMock()
        roi = MagicMock()
        visualized = MagicMock()
        ndvi = MagicMock()
        ndvi.visualize.return_value = visualized

        with (
            patch.object(resources.gee_app, "ndvi", return_value=ndvi) as build_ndvi,
            patch.object(resources.gc, "true_color") as build_true_color,
            patch.object(resources.gc, "lst_celsius") as build_lst,
            patch.object(resources.gee_app, "eci") as build_eci,
        ):
            result = resources._visualized_scene_layer(image, roi, "ndvi")

        self.assertIs(result, visualized)
        build_ndvi.assert_called_once_with(image)
        ndvi.visualize.assert_called_once_with(**resources.gee_app.VIS["ndvi"])
        build_true_color.assert_not_called()
        build_lst.assert_not_called()
        build_eci.assert_not_called()

    def test_unknown_scene_layer_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown scene layer"):
            resources._visualized_scene_layer(MagicMock(), MagicMock(), "bogus")


if __name__ == "__main__":
    unittest.main()
