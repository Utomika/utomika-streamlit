"""Cached station-weather loading and compact Altair visualisation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import altair as alt
import pandas as pd
import streamlit as st


HISTORY_DAYS = (7, 14, 21, 28, 35)
DEFAULT_HISTORY_DAYS = 7
CITY_NAMES = {"zagreb": "Zagreb", "split": "Split"}
STATIONS = {
    "zagreb": ("Grič", "Maksimir", "Pleso"),
    "split": ("Marjan",),
}
DEFAULT_STATIONS = {"zagreb": "Grič", "split": "Marjan"}

_TEMPERATURE_COLUMNS = (
    "datum",
    "grad",
    "mjerna_postaja",
    "sat",
    "SredT",
    "MinT",
    "MaxT",
    "oborine_mm",
)
_WIND_COLUMNS = (
    "datum",
    "grad",
    "mjerna_postaja",
    "sat",
    "smjer_vjetra",
    "jacina_vjetra_bof",
)

_COMPASS_BEARINGS = {
    "N": 0.0,
    "NNE": 22.5,
    "NE": 45.0,
    "ENE": 67.5,
    "E": 90.0,
    "ESE": 112.5,
    "SE": 135.0,
    "SSE": 157.5,
    "S": 180.0,
    "SSW": 202.5,
    "SW": 225.0,
    "WSW": 247.5,
    "W": 270.0,
    "WNW": 292.5,
    "NW": 315.0,
    "NNW": 337.5,
}


@dataclass(frozen=True)
class WeatherStore:
    """Read-only-by-convention station frames, partitioned for quick slicing."""

    daily: Mapping[tuple[str, str], pd.DataFrame]
    wind: Mapping[tuple[str, str], pd.DataFrame]
    coverage: Mapping[tuple[str, str], tuple[date, date]]


@dataclass(frozen=True)
class WeatherWindow:
    city: str
    station: str
    selected_date: date
    days_before: int
    start_date: date
    end_date: date
    coverage_start: date
    coverage_end: date
    daily: pd.DataFrame
    wind: pd.DataFrame

    @property
    def has_data(self) -> bool:
        daily_values = ("SredT", "MinT", "MaxT", "oborine_mm")
        has_daily = bool(self.daily.loc[:, daily_values].notna().any().any())
        has_wind = bool(
            self.wind.loc[:, ["smjer_vjetra", "jacina_vjetra_bof"]]
            .notna()
            .any()
            .any()
        )
        return has_daily or has_wind

    @property
    def is_partial(self) -> bool:
        return self.start_date < self.coverage_start or self.end_date > self.coverage_end


def stations_for_city(city: str) -> tuple[str, ...]:
    """Return stations in their desired UI order."""
    try:
        return STATIONS[city]
    except KeyError as exc:
        raise ValueError(f"Unsupported city: {city}") from exc


def default_station(city: str) -> str:
    try:
        return DEFAULT_STATIONS[city]
    except KeyError as exc:
        raise ValueError(f"Unsupported city: {city}") from exc


def shift_window_offset(current: int, delta: int) -> int:
    """Move the viewport while keeping its offset at or behind the scene date."""
    return max(0, int(current) + int(delta))


def _partition_by_station(
    frame: pd.DataFrame, value_columns: tuple[str, ...]
) -> dict[tuple[str, str], pd.DataFrame]:
    partitions: dict[tuple[str, str], pd.DataFrame] = {}
    grouped = frame.groupby(["grad", "mjerna_postaja"], observed=True, sort=False)
    for (city_name, station), group in grouped:
        city = next(
            (slug for slug, display_name in CITY_NAMES.items() if display_name == city_name),
            None,
        )
        if city is None:
            continue
        station_frame = (
            group.set_index("timestamp")
            .sort_index()
            .loc[:, list(value_columns)]
        )
        partitions[(city, str(station))] = station_frame
    return partitions


def _read_weather_store(data_dir: str | Path) -> WeatherStore:
    """Read and partition both source files. Kept separate for unit testing."""
    data_path = Path(data_dir)
    temperature = pd.read_csv(
        data_path / "sve_postaje_temperatura_oborine.csv",
        encoding="utf-8-sig",
        usecols=_TEMPERATURE_COLUMNS,
        dtype={"grad": "category", "mjerna_postaja": "category", "sat": "int8"},
    )
    wind = pd.read_csv(
        data_path / "sve_postaje_vjetar_tlo.csv",
        encoding="utf-8-sig",
        usecols=_WIND_COLUMNS,
        dtype={"grad": "category", "mjerna_postaja": "category", "sat": "int8"},
    )

    for frame in (temperature, wind):
        frame["timestamp"] = pd.to_datetime(frame["datum"], errors="raise") + pd.to_timedelta(
            frame["sat"], unit="h"
        )

    daily = _partition_by_station(
        temperature, ("SredT", "MinT", "MaxT", "oborine_mm")
    )
    wind_by_station = _partition_by_station(
        wind, ("smjer_vjetra", "jacina_vjetra_bof")
    )

    coverage: dict[tuple[str, str], tuple[date, date]] = {}
    for key in daily.keys() | wind_by_station.keys():
        indexes = []
        if key in daily and not daily[key].empty:
            indexes.append(daily[key].index)
        if key in wind_by_station and not wind_by_station[key].empty:
            indexes.append(wind_by_station[key].index)
        if indexes:
            coverage[key] = (
                min(index.min() for index in indexes).date(),
                max(index.max() for index in indexes).date(),
            )

    return WeatherStore(
        daily=MappingProxyType(daily),
        wind=MappingProxyType(wind_by_station),
        coverage=MappingProxyType(coverage),
    )


@st.cache_resource(show_spinner=False)
def load_weather_store(data_dir: str) -> WeatherStore:
    """Load the static CSVs once per process; callers must not mutate the frames."""
    return _read_weather_store(data_dir)


def coverage_for_station(
    store: WeatherStore, city: str, station: str
) -> tuple[date, date]:
    try:
        return store.coverage[(city, station)]
    except KeyError as exc:
        raise ValueError(f"No weather data for {city}/{station}") from exc


def get_weather_window(
    store: WeatherStore,
    city: str,
    station: str,
    selected_date: date,
    days_before: int = DEFAULT_HISTORY_DAYS,
    offset_days: int = 0,
) -> WeatherWindow:
    """Return the selected history span, shifted backward from a scene date."""
    if offset_days < 0:
        raise ValueError("offset_days must be non-negative")
    if days_before not in HISTORY_DAYS:
        raise ValueError(f"days_before must be one of {HISTORY_DAYS}")
    key = (city, station)
    coverage_start, coverage_end = coverage_for_station(store, city, station)
    end_date = selected_date - timedelta(days=offset_days)
    start_date = end_date - timedelta(days=days_before)
    start = pd.Timestamp(start_date)
    exclusive_end = pd.Timestamp(end_date + timedelta(days=1))

    daily_source = store.daily.get(key)
    if daily_source is None:
        daily = pd.DataFrame(columns=["SredT", "MinT", "MaxT", "oborine_mm"])
    else:
        daily = daily_source.loc[
            (daily_source.index >= start) & (daily_source.index < exclusive_end)
        ].copy()
    # Every calendar day gets a 21:00 slot. This makes missing observations
    # explicit and prevents the temperature line connecting across a gap.
    expected_daily = pd.date_range(
        start + pd.Timedelta(hours=21),
        pd.Timestamp(end_date) + pd.Timedelta(hours=21),
        freq="D",
        name="timestamp",
    )
    daily = daily.reindex(expected_daily)
    daily["temp_segment"] = daily["SredT"].isna().cumsum().astype(str)

    wind_source = store.wind.get(key)
    if wind_source is None:
        wind = pd.DataFrame(columns=["smjer_vjetra", "jacina_vjetra_bof"])
        wind.index = pd.DatetimeIndex([], name="timestamp")
    else:
        wind = wind_source.loc[
            (wind_source.index >= start) & (wind_source.index < exclusive_end)
        ].copy()
    wind = _prepare_wind_marks(wind)

    return WeatherWindow(
        city=city,
        station=station,
        selected_date=selected_date,
        days_before=days_before,
        start_date=start_date,
        end_date=end_date,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        daily=daily,
        wind=wind,
    )


def _wind_mark(direction: object, beaufort: object) -> tuple[str, float, float, str]:
    """Return glyph, rotation, font size and colour for one wind observation."""
    direction_text = "" if pd.isna(direction) else str(direction).strip().upper()
    strength = 0.0 if pd.isna(beaufort) else max(0.0, float(beaufort))
    if direction_text == "C":
        return "○", 0.0, 17.0, "#222222"
    if direction_text not in _COMPASS_BEARINGS:
        return "—", 0.0, 15.0, "#999999"

    # Source directions are meteorological (where wind comes from). The arrow
    # points toward the airflow destination. The glyph points east at 0°.
    angle = (_COMPASS_BEARINGS[direction_text] + 90.0) % 360.0
    font_size = min(24.0, 12.0 + strength * 1.2)
    return "➤", angle, font_size, "#222222"


def _prepare_wind_marks(wind: pd.DataFrame) -> pd.DataFrame:
    prepared = wind.reset_index()
    if prepared.empty:
        return prepared.assign(
            glyph=pd.Series(dtype="object"),
            angle=pd.Series(dtype="float64"),
            font_size=pd.Series(dtype="float64"),
            color=pd.Series(dtype="object"),
            wind_row=pd.Series(dtype="object"),
        )
    marks = [
        _wind_mark(direction, strength)
        for direction, strength in zip(
            prepared["smjer_vjetra"], prepared["jacina_vjetra_bof"]
        )
    ]
    prepared[["glyph", "angle", "font_size", "color"]] = pd.DataFrame(
        marks, index=prepared.index
    )
    prepared["wind_row"] = "Wind"
    return prepared


def _highlight_frame(window: WeatherWindow) -> pd.DataFrame:
    if not (window.start_date <= window.selected_date <= window.end_date):
        return pd.DataFrame(columns=["start", "end"])
    return pd.DataFrame(
        {
            "start": [pd.Timestamp(window.selected_date)],
            "end": [pd.Timestamp(window.selected_date + timedelta(days=1))],
        }
    )


def build_weather_chart(window: WeatherWindow) -> alt.VConcatChart:
    """Build the compact temperature, precipitation and wind chart."""
    daily = window.daily.reset_index()
    window_start = pd.Timestamp(window.start_date)
    daily["x_hours"] = (
        (daily["timestamp"] - window_start) / pd.Timedelta(hours=1)
    )
    wind_data = window.wind.copy()
    if not wind_data.empty:
        wind_data["x_hours"] = (
            (wind_data["timestamp"] - window_start) / pd.Timedelta(hours=1)
        )
    temperature = daily[daily["SredT"].notna()].copy()
    precipitation = daily[daily["oborine_mm"].notna()].copy()
    positive_precipitation = precipitation[precipitation["oborine_mm"] > 0]
    highlight_data = _highlight_frame(window)
    if not highlight_data.empty:
        highlight_data["start_hours"] = (
            (highlight_data["start"] - window_start) / pd.Timedelta(hours=1)
        )
        highlight_data["end_hours"] = (
            (highlight_data["end"] - window_start) / pd.Timedelta(hours=1)
        )
    if temperature.empty:
        temperature_domain = [0.0, 1.0]
    else:
        temperature_min = float(temperature["SredT"].min())
        temperature_max = float(temperature["SredT"].max())
        temperature_padding = max(1.0, (temperature_max - temperature_min) * 0.15)
        temperature_domain = [
            temperature_min - temperature_padding,
            temperature_max + temperature_padding,
        ]
    precipitation_max = (
        float(precipitation["oborine_mm"].max())
        if not precipitation.empty
        else 0.0
    )
    precipitation_domain = [0.0, max(1.0, precipitation_max * 1.15)]
    temperature_scale = alt.Scale(domain=temperature_domain, zero=False, nice=True)
    precipitation_scale = alt.Scale(domain=precipitation_domain, zero=True, nice=True)

    visible_days = window.days_before + 1
    x_scale = alt.Scale(domain=[0, visible_days * 24], nice=False)
    x_main = alt.X("x_hours:Q", scale=x_scale, axis=None)
    window_start_ms = int(window_start.timestamp() * 1000)
    wind_axis = alt.Axis(
        title=None,
        tickCount=min(18, visible_days * 2),
        labelExpr=(
            f"timeFormat(toDate({window_start_ms} + "
            "datum.value * 3600000), '%d %b %Hh')"
        ),
        labelAngle=-35,
        labelFontSize=10,
        grid=False,
    )
    x_wind = alt.X(
        "x_hours:Q",
        scale=x_scale,
        axis=wind_axis,
    )
    highlight = (
        alt.Chart(highlight_data)
        .mark_rect(color="#F6D365", opacity=0.24)
        .encode(
            x=alt.X("start_hours:Q", scale=x_scale, axis=None),
            x2="end_hours:Q",
        )
    )
    precipitation_bars = (
        alt.Chart(precipitation)
        .mark_bar(color="#49B8D1", opacity=0.78, size=18)
        .encode(
            x=x_main,
            y=alt.Y(
                "oborine_mm:Q",
                title="Precipitation (mm)",
                axis=alt.Axis(orient="right", grid=False, titleColor="#1689A7"),
                scale=precipitation_scale,
            ),
            tooltip=[
                alt.Tooltip("timestamp:T", title="Recorded", format="%a %d %b %Y, %H:%M"),
                alt.Tooltip("oborine_mm:Q", title="Precipitation", format=".1f"),
            ],
        )
    )
    precipitation_labels = (
        alt.Chart(positive_precipitation)
        .mark_text(color="#167E99", dy=-6, fontSize=10)
        .encode(
            x=x_main,
            y=alt.Y("oborine_mm:Q", axis=None, scale=precipitation_scale),
            text=alt.Text("oborine_mm:Q", format=".1f"),
        )
    )
    temperature_line = (
        alt.Chart(temperature)
        .mark_line(color="#E53935", strokeWidth=2.5)
        .encode(
            x=x_main,
            y=alt.Y(
                "SredT:Q",
                title="Mean air temperature (°C)",
                axis=alt.Axis(titleColor="#C62828"),
                scale=temperature_scale,
            ),
            detail=alt.Detail("temp_segment:N"),
        )
    )
    temperature_points = (
        alt.Chart(temperature)
        .mark_point(color="#E53935", filled=True, size=42)
        .encode(
            x=x_main,
            y=alt.Y("SredT:Q", axis=None, scale=temperature_scale),
            tooltip=[
                alt.Tooltip("timestamp:T", title="Daily record", format="%a %d %b %Y, %H:%M"),
                alt.Tooltip("SredT:Q", title="Mean °C", format=".1f"),
                alt.Tooltip("MinT:Q", title="Minimum °C", format=".1f"),
                alt.Tooltip("MaxT:Q", title="Maximum °C", format=".1f"),
            ],
        )
    )
    main = (
        alt.layer(
            highlight,
            precipitation_bars,
            precipitation_labels,
            temperature_line,
            temperature_points,
        )
        .resolve_scale(y="independent")
        .properties(height=188)
    )

    wind_highlight = (
        alt.Chart(highlight_data)
        .mark_rect(color="#F6D365", opacity=0.24)
        .encode(
            x=alt.X("start_hours:Q", scale=x_scale, axis=wind_axis),
            x2="end_hours:Q",
        )
    )
    wind_marks = (
        alt.Chart(wind_data)
        .mark_text(font="sans-serif")
        .encode(
            x=x_wind,
            y=alt.Y(
                "wind_row:N",
                title=None,
                axis=alt.Axis(ticks=False, domain=False, labelPadding=6),
            ),
            text=alt.Text("glyph:N"),
            angle=alt.Angle("angle:Q", scale=None),
            size=alt.Size("font_size:Q", scale=None, legend=None),
            color=alt.Color("color:N", scale=None, legend=None),
            tooltip=[
                alt.Tooltip("timestamp:T", title="Observed", format="%a %d %b %Y, %H:%M"),
                alt.Tooltip("smjer_vjetra:N", title="Wind from"),
                alt.Tooltip("jacina_vjetra_bof:Q", title="Beaufort", format=".0f"),
            ],
        )
    )
    wind = alt.layer(wind_highlight, wind_marks).properties(height=42)

    return (
        alt.vconcat(main, wind, spacing=3)
        .configure_view(stroke="#D7DEE8")
        .configure_axis(labelColor="#4F5661", titleColor="#4F5661")
    )
