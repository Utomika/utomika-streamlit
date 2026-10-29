"""UTOMIKA cloud-labeling app.

A one-screen review tool: pick a city, scroll the catalog of Landsat overpasses
in the sidebar, open one on an interactive GEE map (true-color, cloud mask,
NDVI, LST, ECI), and record a cloudy / clear / unsure verdict to Supabase.

Run locally:   streamlit run app.py
Secrets:       see .streamlit/secrets.toml.example
"""

import streamlit as st

from components.export import render_export
from components.geotiff_export import render_geotiff_download
from components.labeling import label_controls
from components.login import login_gate
from components.map_view import render_live_cloud_stats, render_progressive_map
from components.sidebar import render_sidebar
from components.weather_panel import weather_panel
from config import CITIES, LOGO_FULL, LOGO_ICON
import resources


st.set_page_config(
    page_title="UTOMIKA cloud labeling", page_icon=LOGO_ICON, layout="wide")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def _fatal(title, err, hint):
    """Show an actionable error and stop, instead of a raw traceback."""
    st.error(f"**{title}**\n\n{hint}")
    st.expander("Technical detail").code(f"{type(err).__name__}: {err}")
    st.stop()


def _labels_down_notice(err):
    st.sidebar.warning(
        "Labeling server is unreachable. Scenes and maps still work, but labels "
        "can't be shown or saved right now."
    )
    st.sidebar.expander("Technical detail").code(f"{type(err).__name__}: {err}")
    if st.sidebar.button("Retry now"):
        resources.reset_labels_down()
        st.rerun()


def main():
    if not login_gate():
        st.stop()

    st.sidebar.image(LOGO_FULL, width=200)

    try:
        resources.init_ee()
    except Exception as e:  # noqa: BLE001
        _fatal(
            "Earth Engine sign-in failed.",
            e,
            "Check the service-account secret. Preferred form is a "
            "`[gee_service_account]` table in `.streamlit/secrets.toml` (see "
            "secrets.toml.example) — that avoids JSON control-character errors "
            "from unescaped newlines in `private_key`. Also confirm `GEE_PROJECT`.",
        )

    # Switching city rebuilds the whole catalog, so a page/date range carried over
    # from the previous city would be meaningless — reset them (the date range
    # then falls back to the new city's full span).
    def _on_city_change():
        for k in ("scene_page", "date_from", "date_until", "scene_radio"):
            st.session_state.pop(k, None)

    city_name = st.sidebar.selectbox(
        "City", list(CITIES), key="city", on_change=_on_city_change)
    city = CITIES[city_name]

    df = resources.load_catalog(city)
    labeled, labels_err = resources.load_labels(city, st.session_state["labeler"])
    labels_ok = labels_err is None
    if not labels_ok:
        _labels_down_notice(labels_err)
    client = resources.get_supabase() if labels_ok else None

    view, active_id = render_sidebar(df, labeled, labels_ok)
    if active_id is None:
        st.info("Adjust the filters to see scenes.")
        return

    row = df[df["image_id"] == active_id].iloc[0]

    # Adjacent scenes in the filtered/date-sorted list, including page edges.
    scene_ids = view["image_id"].tolist()
    position = scene_ids.index(active_id)
    neighbor_ids = scene_ids[max(0, position - 1):position] + scene_ids[position + 1:position + 2]
    neighbors = tuple(
        (city, str(scene["image_id"]), str(scene["sat"]), str(scene["tier"]))
        for _, scene in df.set_index("image_id", drop=False).loc[neighbor_ids].iterrows()
    )

    st.subheader(f"{city_name} — {row['date']} · {row['sat']}")

    weather_panel(city, active_id, row["date"])

    meta = st.columns(4)
    meta[0].metric("Scene cloud %", f"{row['scene_cloud_cover']:.0f}")
    meta[1].metric("ROI cloud % (catalog)", f"{row['roi_cloud_pct']:.0f}")
    meta[2].metric("ROI valid %", f"{row['roi_valid_pct']:.0f}")
    meta[3].metric("Tier", row["tier"])

    left, right = st.columns([3, 1])
    try:
        with left:
            render_progressive_map(city, row, neighbors)
    except Exception as e:  # noqa: BLE001
        with left:
            st.error(f"Could not load this scene from Earth Engine:\n\n{e}")
        return

    with right:
        render_live_cloud_stats(city, row)
        render_geotiff_download(city, row)
        st.divider()
        if labels_ok:
            label_controls(client, city, row, view, labeled)
        else:
            st.info("Labeling unavailable — server is down.")

    # Export
    if labels_ok:
        render_export(client, st.session_state["labeler"])


if __name__ == "__main__":
    main()
