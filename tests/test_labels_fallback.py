import importlib.util
import sys
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest


for name in ("ee", "supabase"):
    if name not in sys.modules and importlib.util.find_spec(name) is None:
        module = types.ModuleType(name)
        if name == "supabase":
            module.create_client = MagicMock()
            module.ClientOptions = MagicMock()
        sys.modules[name] = module

import resources
from components import export, labeling, map_view, geotiff_export, login, weather_panel


class LabelsFallbackTestCase(unittest.TestCase):
    def setUp(self):
        self.state = {}
        patcher = patch.object(resources.st, "session_state", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_lookup_records_error_and_backoff(self):
        err = RuntimeError("unreachable")
        with (patch.object(resources, "get_labeled", side_effect=err),
              patch.object(resources.time, "time", return_value=100)):
            self.assertEqual(resources.load_labels("zagreb", "tester"), ({}, err))
        self.assertEqual(self.state["labels_down_until"], 160)
        self.assertIs(self.state["labels_error"], err)

    def test_backoff_skips_lookup_even_for_another_city(self):
        err = RuntimeError("unreachable")
        self.state.update(labels_down_until=160, labels_error=err)
        with (patch.object(resources, "get_labeled") as lookup,
              patch.object(resources.time, "time", return_value=159)):
            self.assertEqual(resources.load_labels("split", "tester"), ({}, err))
        lookup.assert_not_called()

    def test_reset_clears_backoff_and_queries_again(self):
        err = RuntimeError("unreachable")
        with patch.object(resources, "get_labeled",
                          side_effect=[err, {"scene-a": "clear"}]) as lookup:
            resources.load_labels("zagreb", "tester")
            resources.reset_labels_down()
            self.assertNotIn("labels_down_until", self.state)
            self.assertNotIn("labels_error", self.state)
            lookup.clear.assert_called_once_with()
            self.assertEqual(resources.load_labels("zagreb", "tester"),
                             ({"scene-a": "clear"}, None))
        self.assertEqual(lookup.call_count, 2)

    def test_expired_backoff_retries_automatically(self):
        self.state.update(labels_down_until=160, labels_error=RuntimeError("down"))
        with (patch.object(resources, "get_labeled", return_value={}) as lookup,
              patch.object(resources.time, "time", return_value=160)):
            self.assertEqual(resources.load_labels("zagreb", "tester"), ({}, None))
        lookup.assert_called_once_with("zagreb", "tester")


class LabelsAppTestCase(unittest.TestCase):
    def render_app(self, lookup_error=None):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
        app.session_state["labeler"] = "tester"
        with ExitStack() as stack:
            stack.enter_context(patch.object(login, "login_gate", return_value=True))
            stack.enter_context(patch.object(resources, "init_ee"))
            lookup = stack.enter_context(patch.object(
                resources, "get_labeled", return_value={}, side_effect=lookup_error))
            client = stack.enter_context(patch.object(resources, "get_supabase"))
            panels = [stack.enter_context(patch.object(module, name)) for module, name in (
                (map_view, "render_progressive_map"),
                (map_view, "render_live_cloud_stats"),
                (geotiff_export, "render_geotiff_download"),
                (weather_panel, "weather_panel"),
                (labeling, "label_controls"),
                (export, "render_export"),
            )]
            app.run()
            self.assertEqual(list(app.exception), [])
            for panel in panels[:4]:
                panel.assert_called_once()
            if lookup_error:
                self.assertIn("Labeling server is unreachable", app.sidebar.warning[0].value)
                self.assertTrue(any(b.label == "Retry now" for b in app.sidebar.button))
                self.assertTrue(app.sidebar.checkbox[0].disabled)
                self.assertIn("labels unavailable", app.sidebar.caption[0].value)
                self.assertTrue(any("Labeling unavailable" in i.value for i in app.info))
                for panel in panels[4:]:
                    panel.assert_not_called()
                client.assert_not_called()
                # A second scene selection must reuse the backoff without a lookup.
                city = panels[0].call_args.args[0]
                next_id = resources.load_catalog(city).sort_values("date")["image_id"].iloc[1]
                app.sidebar.radio[0].set_value(next_id).run()
                self.assertEqual(list(app.exception), [])
                lookup.assert_called_once()
                client.assert_not_called()
                for panel in panels[:4]:
                    self.assertEqual(panel.call_count, 2)
                # Retry clears the backoff and restores the normal controls.
                lookup.side_effect = None
                next(b for b in app.sidebar.button if b.label == "Retry now").click().run()
                self.assertEqual(list(app.exception), [])
                self.assertEqual(list(app.sidebar.warning), [])
                self.assertFalse(app.sidebar.checkbox[0].disabled)
                for panel in panels[4:]:
                    panel.assert_called_once()
            else:
                self.assertEqual(list(app.sidebar.warning), [])
                self.assertFalse(app.sidebar.checkbox[0].disabled)
                for panel in panels[4:]:
                    panel.assert_called_once()
                client.assert_called_once()

    def test_outage_keeps_scene_panels_and_retry_restores_labeling(self):
        self.render_app(RuntimeError("server down"))

    def test_available_labels_keep_labeling_and_export(self):
        self.render_app()

    def test_failed_save_preserves_scene_and_does_not_update_labels(self):
        err = RuntimeError("server down")
        state = {"labeler": "tester", "scene_radio": "scene-a"}
        labeled = {}
        columns = [MagicMock() for _ in range(3)]
        for i, column in enumerate(columns):
            column.button.return_value = i == 0
        row = dict(image_id="scene-a", date="2020-01-01", sat="L8",
                   scene_cloud_cover=10, roi_cloud_pct=20)
        with (patch.object(labeling.st, "session_state", state),
              patch.object(labeling.st, "text_input", return_value="note"),
              patch.object(labeling.st, "columns", return_value=columns),
              patch.object(labeling.db, "upsert_label", side_effect=err),
              patch.object(labeling.st, "error") as error,
              patch.object(labeling.st, "rerun") as rerun,
              patch.object(labeling, "advance_to_next") as advance):
            labeling.label_controls(MagicMock(), "zagreb", row, MagicMock(), labeled)
        error.assert_called_once()
        rerun.assert_not_called()
        advance.assert_not_called()
        self.assertEqual(labeled, {})
        self.assertEqual(state["scene_radio"], "scene-a")
        self.assertIs(state["labels_error"], err)

    def test_failed_export_shows_error_and_starts_backoff(self):
        err = RuntimeError("server down")
        state = {}
        with (patch.object(export.st, "session_state", state),
              patch.object(export.st, "sidebar"),
              patch.object(export.st, "button", return_value=True),
              patch.object(resources, "export_df", side_effect=err),
              patch.object(export.st, "error") as error,
              patch.object(export.st, "download_button") as download):
            export.render_export(MagicMock(), "tester")
        error.assert_called_once()
        download.assert_not_called()
        self.assertIs(state["labels_error"], err)
