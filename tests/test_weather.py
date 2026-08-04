import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

import weather


class WeatherTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name)

        temperature_rows = [
            "datum,grad,mjerna_postaja,sat,SredT,MinT,MaxT,oborine_mm",
            "2024-01-01,Zagreb,Grič,21,5.0,1.0,7.0,0.0",
            "2024-01-02,Zagreb,Grič,21,6.0,2.0,8.0,",
            "2024-01-03,Zagreb,Grič,21,,,,1.2",
            "2024-01-04,Zagreb,Grič,21,8.0,4.0,10.0,2.5",
            "2024-01-05,Zagreb,Grič,21,9.0,5.0,11.0,0.0",
            "2024-01-06,Zagreb,Grič,21,10.0,6.0,12.0,0.4",
            "2024-01-07,Zagreb,Grič,21,11.0,7.0,13.0,",
            "2024-01-08,Zagreb,Grič,21,12.0,8.0,14.0,3.0",
            "2024-01-08,Zagreb,Maksimir,21,11.0,7.0,13.0,1.0",
            "2024-01-08,Zagreb,Pleso,21,10.0,6.0,12.0,2.0",
            "2024-01-08,Split,Marjan,21,13.0,9.0,15.0,0.0",
        ]
        wind_rows = [
            "datum,grad,mjerna_postaja,sat,smjer_vjetra,jacina_vjetra_bof",
        ]
        directions = ["N", "ENE", "C"]
        for day in range(1, 9):
            for hour, direction in zip((7, 14, 21), directions):
                wind_rows.append(
                    f"2024-01-{day:02d},Zagreb,Grič,{hour},{direction},{day % 6}"
                )
        wind_rows.extend(
            [
                "2024-01-08,Zagreb,Maksimir,7,E,2",
                "2024-01-08,Zagreb,Pleso,7,W,3",
                "2024-01-08,Split,Marjan,7,S,1",
            ]
        )

        # Include a UTF-8 BOM to match the production files.
        (data_dir / "sve_postaje_temperatura_oborine.csv").write_text(
            "\n".join(temperature_rows), encoding="utf-8-sig"
        )
        (data_dir / "sve_postaje_vjetar_tlo.csv").write_text(
            "\n".join(wind_rows), encoding="utf-8-sig"
        )
        self.store = weather._read_weather_store(data_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_station_order_defaults_and_bom_loading(self):
        self.assertEqual(weather.stations_for_city("zagreb"), ("Grič", "Maksimir", "Pleso"))
        self.assertEqual(weather.stations_for_city("split"), ("Marjan",))
        self.assertEqual(weather.default_station("zagreb"), "Grič")
        self.assertIn(("zagreb", "Grič"), self.store.daily)
        self.assertEqual(
            weather.coverage_for_station(self.store, "zagreb", "Grič"),
            (date(2024, 1, 1), date(2024, 1, 8)),
        )

    def test_window_is_inclusive_and_preserves_missing_precipitation(self):
        window = weather.get_weather_window(
            self.store, "zagreb", "Grič", date(2024, 1, 8)
        )
        self.assertEqual((window.start_date, window.end_date), (date(2024, 1, 1), date(2024, 1, 8)))
        self.assertEqual(len(window.daily), 8)
        self.assertEqual(len(window.wind), 24)
        self.assertEqual(window.daily.iloc[0]["oborine_mm"], 0.0)
        self.assertTrue(pd.isna(window.daily.iloc[1]["oborine_mm"]))
        self.assertNotEqual(
            window.daily.loc["2024-01-02", "temp_segment"].iloc[0],
            window.daily.loc["2024-01-04", "temp_segment"].iloc[0],
        )
        self.assertTrue(window.has_data)
        self.assertFalse(window.is_partial)

    def test_offset_and_out_of_coverage_window(self):
        shifted = weather.get_weather_window(
            self.store, "zagreb", "Grič", date(2024, 1, 8), offset_days=1
        )
        self.assertEqual((shifted.start_date, shifted.end_date), (date(2023, 12, 31), date(2024, 1, 7)))
        self.assertTrue(shifted.is_partial)
        self.assertTrue(shifted.has_data)

        unavailable = weather.get_weather_window(
            self.store, "zagreb", "Grič", date(2025, 1, 8)
        )
        self.assertFalse(unavailable.has_data)
        self.assertTrue(unavailable.is_partial)
        with self.assertRaises(ValueError):
            weather.get_weather_window(
                self.store, "zagreb", "Grič", date(2024, 1, 8), offset_days=-1
            )

    def test_selectable_history_lengths_include_the_scene_date(self):
        self.assertEqual(weather.HISTORY_DAYS, (7, 14, 21, 28, 35))
        window = weather.get_weather_window(
            self.store,
            "zagreb",
            "Grič",
            date(2024, 1, 8),
            days_before=14,
        )
        self.assertEqual(window.start_date, date(2023, 12, 25))
        self.assertEqual(window.end_date, date(2024, 1, 8))
        self.assertEqual(len(window.daily), 15)
        self.assertEqual(window.days_before, 14)
        with self.assertRaises(ValueError):
            weather.get_weather_window(
                self.store,
                "zagreb",
                "Grič",
                date(2024, 1, 8),
                days_before=10,
            )

    def test_navigation_offset_never_moves_after_selected_date(self):
        self.assertEqual(weather.shift_window_offset(0, -1), 0)
        self.assertEqual(weather.shift_window_offset(0, 1), 1)
        self.assertEqual(weather.shift_window_offset(12, -1), 11)

    def test_all_compass_directions_calm_and_unknown(self):
        angles = []
        for direction in weather._COMPASS_BEARINGS:
            glyph, angle, size, color = weather._wind_mark(direction, 3)
            self.assertEqual(glyph, "➤")
            self.assertGreater(size, 12)
            self.assertEqual(color, "#222222")
            angles.append(angle)
        self.assertEqual(len(set(angles)), 16)

        self.assertEqual(weather._wind_mark("C", 0)[0], "○")
        self.assertEqual(weather._wind_mark("X", 2)[0], "—")
        self.assertEqual(weather._wind_mark(None, None)[0], "—")
        self.assertGreater(weather._wind_mark("N", 8)[2], weather._wind_mark("N", 1)[2])

    def test_chart_spec_contains_required_layers_and_independent_axes(self):
        window = weather.get_weather_window(
            self.store, "zagreb", "Grič", date(2024, 1, 8)
        )
        spec = weather.build_weather_chart(window).to_dict()
        encoded = json.dumps(spec, ensure_ascii=False)
        self.assertEqual(len(spec["vconcat"]), 2)
        self.assertEqual(len(spec["vconcat"][0]["layer"]), 5)
        self.assertEqual(
            spec["vconcat"][0]["resolve"]["scale"]["y"], "independent"
        )
        self.assertIn("Mean air temperature (°C)", encoded)
        self.assertIn("Precipitation (mm)", encoded)
        self.assertIn("temp_segment", encoded)
        self.assertIn('"angle"', encoded)
        self.assertIn("#F6D365", encoded)


if __name__ == "__main__":
    unittest.main()
