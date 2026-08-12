import streamlit as st

from config import DATA_DIR
import weather


def _shift_weather_window(state_key, delta):
    """Move the fixed weather viewport without allowing a future offset."""
    st.session_state[state_key] = weather.shift_window_offset(
        st.session_state.get(state_key, 0), delta
    )


@st.fragment
def weather_panel(city, image_id, selected_date):
    """Render weather independently so its controls do not rebuild the GEE map."""
    anchor = (city, image_id)
    if st.session_state.get("weather_anchor") != anchor:
        st.session_state["weather_anchor"] = anchor
        st.session_state["weather_offset_days"] = 0

    store = weather.load_weather_store(DATA_DIR)
    stations = weather.stations_for_city(city)

    title_col, station_col, history_col = st.columns(
        [2.5, 1.25, 1.25], vertical_alignment="bottom"
    )
    with title_col:
        st.markdown("#### Station weather")
    with station_col:
        if len(stations) > 1:
            station = st.selectbox(
                "Weather station",
                stations,
                index=stations.index(weather.default_station(city)),
                key=f"weather_station_{city}",
            )
        else:
            station = stations[0]
            st.caption(f"Weather station: **{station}**")
    with history_col:
        days_before = st.selectbox(
            "Days before scene",
            weather.HISTORY_DAYS,
            index=weather.HISTORY_DAYS.index(weather.DEFAULT_HISTORY_DAYS),
            format_func=lambda days: f"{days} days",
            key="weather_history_days",
        )

    offset_key = "weather_offset_days"
    offset_days = int(st.session_state.get(offset_key, 0))
    window = weather.get_weather_window(
        store,
        city,
        station,
        selected_date,
        days_before=days_before,
        offset_days=offset_days,
    )

    if not window.has_data:
        st.info(
            "No station observations are available for this weather window. "
            f"{station} coverage: {window.coverage_start:%d %b %Y}–"
            f"{window.coverage_end:%d %b %Y}."
        )
        return

    nav_left, nav_range, nav_right = st.columns([1, 7, 1])
    can_go_older = window.start_date > window.coverage_start
    nav_left.button(
        "◀",
        key=f"weather_older_{city}",
        help="Move the selected weather window one day into the past",
        disabled=not can_go_older,
        use_container_width=True,
        on_click=_shift_weather_window,
        args=(offset_key, 1),
    )
    nav_range.markdown(
        f"<div style='text-align:center;padding-top:0.45rem'>"
        f"{window.start_date:%d %b %Y} — {window.end_date:%d %b %Y}"
        f"</div>",
        unsafe_allow_html=True,
    )
    nav_right.button(
        "▶",
        key=f"weather_newer_{city}",
        help="Move one day back toward the selected satellite date",
        disabled=offset_days == 0,
        use_container_width=True,
        on_click=_shift_weather_window,
        args=(offset_key, -1),
    )

    chart = weather.build_weather_chart(window)
    st.altair_chart(chart, use_container_width=True, theme=None)
    st.caption(
        "Red: daily mean air temperature (min/max in tooltip) · "
        "Blue: daily precipitation · Wind: direction and Beaufort strength. "
        "Daily values are recorded at 21:00. No blue mark means missing data, "
        "not zero precipitation."
    )
    if window.is_partial:
        st.caption(
            f"Partial window: {station} observations cover "
            f"{window.coverage_start:%d %b %Y}–{window.coverage_end:%d %b %Y}."
        )
