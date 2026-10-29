import datetime as dt
import importlib.util
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# Same approach as test_progressive_layers: the unit-test environment may not
# have Earth Engine or Supabase installed, so stub just enough for `resources`
# (and its gee_app/db imports) to load. The sidebar helpers only need Streamlit.
# (Another test module may already have installed a stub; a stubbed module has
# no __spec__, so check sys.modules first to avoid find_spec raising.)
if "ee" not in sys.modules and importlib.util.find_spec("ee") is None:
    sys.modules["ee"] = types.ModuleType("ee")

if "supabase" not in sys.modules and importlib.util.find_spec("supabase") is None:
    supabase = types.ModuleType("supabase")
    supabase.create_client = MagicMock()
    supabase.ClientOptions = MagicMock()
    sys.modules["supabase"] = supabase

from components import sidebar
import resources


class SidebarSyncTestCase(unittest.TestCase):
    def setUp(self):
        # A plain dict stands in for st.session_state; the helpers only use
        # item access and .get().
        self.state = {}
        patcher = patch.object(sidebar.st, "session_state", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- cloud ------------------------------------------------------------
    def test_cloud_slider_mirrors_into_number_box(self):
        self.state["cloud_filter"] = 37
        sidebar._sync_cloud_from_slider()
        self.assertEqual(self.state["cloud_filter_num"], 37)

    def test_cloud_number_box_moves_slider(self):
        self.state["cloud_filter_num"] = 42
        sidebar._sync_cloud_from_number()
        self.assertEqual(self.state["cloud_filter"], 42)

    def test_cloud_number_box_ignores_cleared_value(self):
        self.state.update(cloud_filter=55, cloud_filter_num=None)
        sidebar._sync_cloud_from_number()
        self.assertEqual(self.state["cloud_filter"], 55)

    # -- year range -------------------------------------------------------
    def test_year_slider_mirrors_both_ends(self):
        self.state["year_filter"] = (1998, 2004)
        sidebar._sync_year_from_slider()
        self.assertEqual(self.state["year_from_num"], 1998)
        self.assertEqual(self.state["year_to_num"], 2004)

    def test_year_numbers_write_slider_tuple(self):
        self.state.update(year_from_num=2000, year_to_num=2010)
        sidebar._sync_year_from_number("from")
        self.assertEqual(self.state["year_filter"], (2000, 2010))

    def test_year_from_past_to_pulls_to_up(self):
        self.state.update(year_from_num=2015, year_to_num=2010)
        sidebar._sync_year_from_number("from")
        self.assertEqual(self.state["year_filter"], (2015, 2015))
        self.assertEqual(self.state["year_to_num"], 2015)
        self.assertEqual(self.state["year_from_num"], 2015)

    def test_year_to_below_from_pushes_from_down(self):
        self.state.update(year_from_num=2010, year_to_num=2005)
        sidebar._sync_year_from_number("to")
        self.assertEqual(self.state["year_filter"], (2005, 2005))
        self.assertEqual(self.state["year_from_num"], 2005)
        self.assertEqual(self.state["year_to_num"], 2005)

    # -- date range -------------------------------------------------------
    def test_date_from_past_until_bumps_until(self):
        self.state.update(
            date_from=dt.date(2010, 5, 1), date_until=dt.date(2005, 1, 1))
        sidebar._date_from_changed()
        self.assertEqual(self.state["date_until"], dt.date(2010, 5, 1))

    def test_date_from_before_until_leaves_until(self):
        self.state.update(
            date_from=dt.date(2001, 5, 1), date_until=dt.date(2005, 1, 1))
        sidebar._date_from_changed()
        self.assertEqual(self.state["date_until"], dt.date(2005, 1, 1))

    # -- initialisation / clamping ---------------------------------------
    def test_init_sets_defaults_when_missing(self):
        sidebar._init_filter_state(
            dt.date(1991, 1, 1), dt.date(2024, 12, 31), [1991, 2000, 2024])
        self.assertEqual(self.state["cloud_filter"], 100)
        self.assertEqual(self.state["cloud_filter_num"], 100)
        self.assertEqual(self.state["year_filter"], (1991, 2024))
        self.assertEqual(self.state["year_from_num"], 1991)
        self.assertEqual(self.state["year_to_num"], 2024)
        self.assertEqual(self.state["date_from"], dt.date(1991, 1, 1))
        self.assertEqual(self.state["date_until"], dt.date(2024, 12, 31))

    def test_init_clamps_stored_values_into_new_bounds(self):
        # e.g. a city switch to a catalog with a narrower span.
        self.state.update(
            year_filter=(1985, 2030),
            date_from=dt.date(1985, 1, 1),
            date_until=dt.date(2030, 1, 1),
        )
        sidebar._init_filter_state(
            dt.date(1991, 1, 1), dt.date(2024, 12, 31), [1991, 2000, 2024])
        self.assertEqual(self.state["year_filter"], (1991, 2024))
        self.assertEqual(self.state["year_from_num"], 1991)
        self.assertEqual(self.state["year_to_num"], 2024)
        self.assertEqual(self.state["date_from"], dt.date(1991, 1, 1))
        self.assertEqual(self.state["date_until"], dt.date(2024, 12, 31))

    def test_init_keeps_valid_stored_values(self):
        self.state.update(
            cloud_filter=37,
            year_filter=(1998, 2004),
            date_from=dt.date(1999, 1, 1),
            date_until=dt.date(2003, 1, 1),
        )
        sidebar._init_filter_state(
            dt.date(1991, 1, 1), dt.date(2024, 12, 31), [1991, 2000, 2024])
        self.assertEqual(self.state["cloud_filter"], 37)
        self.assertEqual(self.state["cloud_filter_num"], 37)
        self.assertEqual(self.state["year_filter"], (1998, 2004))
        self.assertEqual(self.state["date_from"], dt.date(1999, 1, 1))
        self.assertEqual(self.state["date_until"], dt.date(2003, 1, 1))


class RawSceneLayerTestCase(unittest.TestCase):
    def test_unknown_layer_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown scene layer"):
            resources._raw_scene_layer(MagicMock(), MagicMock(), "bogus")

    def test_float_layers_are_unmasked_to_nodata(self):
        image = MagicMock()
        ndvi = MagicMock()
        with patch.object(resources.gee_app, "ndvi", return_value=ndvi):
            result = resources._raw_scene_layer(image, MagicMock(), "ndvi")

        ndvi.toFloat.return_value.unmask.assert_called_once_with(-9999)
        self.assertIs(result, ndvi.toFloat.return_value.unmask.return_value)

    def test_cloud_layer_is_byte_with_255_nodata(self):
        image = MagicMock()
        bits = MagicMock()
        with patch.object(resources.gc, "cloud_bits", return_value=bits):
            result = resources._raw_scene_layer(image, MagicMock(), "cloud")

        image.select.assert_called_once_with("QA_PIXEL")
        bits.unmask.assert_called_once_with(255)
        self.assertIs(result, bits.unmask.return_value.toByte.return_value)


if __name__ == "__main__":
    unittest.main()
