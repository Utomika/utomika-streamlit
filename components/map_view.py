"""Persistent map UI backed by nonblocking, session-local scene work."""

import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import resources
from scene_loader import DEFAULT_VISIBLE, LAYERS, SceneLoader


_COMPONENT_KEY = "_persistent_scene_map"
_LOADER_KEY = "_scene_loader"
_PREFERENCES_KEY = "_map_preferences"
_ACTIVE_KEY = "_active_map_scene"
_NEIGHBORS_KEY = "_map_neighbors"
_STATS_REQUEST_KEY = "_cloud_stats_request"
_FRAGMENT_INTERVAL = 0.75
_scene_map = components.declare_component(
    "utomika_scene_map", path=str(Path(__file__).with_name("scene_map"))
)


def _scene_anchor(city, row):
    return city, str(row["image_id"]), str(row["sat"]), str(row["tier"])


def _scene_id(anchor):
    return json.dumps(anchor, separators=(",", ":"))


def _loader():
    if _LOADER_KEY not in st.session_state:
        st.session_state[_LOADER_KEY] = SceneLoader(
            resources.fetch_scene_tile, resources.fetch_scene_cloud_stats,
            ttl=resources.SCENE_TILE_TTL_SECONDS,
        )
    return st.session_state[_LOADER_KEY]


def _preferences():
    if _PREFERENCES_KEY not in st.session_state:
        st.session_state[_PREFERENCES_KEY] = {
            "visible": list(DEFAULT_VISIBLE), "preferred": "true_color",
            "cloud_opacity": 0.55, "boundary": True,
        }
    return st.session_state[_PREFERENCES_KEY]


def _on_map_change():
    """Store user choices before the next render, independently of the date."""
    event = st.session_state.get(_COMPONENT_KEY)
    if not isinstance(event, dict):
        return
    if st.session_state.get("_map_last_event") == event.get("event_id"):
        return
    st.session_state["_map_last_event"] = event.get("event_id")
    prefs = _preferences()
    visible = event.get("visible")
    if isinstance(visible, list):
        prefs["visible"] = list(dict.fromkeys(k for k in visible if k in LAYERS))
    preferred = event.get("preferred")
    if preferred in LAYERS:
        prefs["preferred"] = preferred
    opacity = event.get("cloud_opacity")
    if isinstance(opacity, (int, float)):
        prefs["cloud_opacity"] = min(1.0, max(0.0, opacity))
    if isinstance(event.get("boundary"), bool):
        prefs["boundary"] = event["boundary"]
    anchor = st.session_state.get(_ACTIVE_KEY)
    retry = event.get("retry")
    # A late click from the previous date must never invalidate this date.
    if anchor and event.get("scene_id") == _scene_id(anchor) and retry in LAYERS:
        _loader().retry(anchor, retry)


def _map_payload(anchor, snapshot):
    return {
        "scene_id": _scene_id(anchor), "city": anchor[0],
        "image_id": anchor[1], "preferences": dict(_preferences()),
        "layers": [
            {"key": key, "name": name,
             "url": snapshot.get(key, {}).get("value"),
             "error": snapshot.get(key, {}).get("error")}
            for key, name in LAYERS.items()
        ],
    }


def render_progressive_map(city, row, neighbors=()):
    """Publish the current request before entering the persistent fragment."""
    st.session_state[_ACTIVE_KEY] = _scene_anchor(city, row)
    st.session_state[_NEIGHBORS_KEY] = neighbors
    _render_scene_map()


@st.fragment(run_every=_FRAGMENT_INTERVAL)
def _render_scene_map():
    # Streamlit 1.37 retains a fragment's original arguments on timer reruns.
    # Read the latest request here so an older timer cannot restore an old date.
    anchor = st.session_state[_ACTIVE_KEY]
    city = anchor[0]
    prefs = _preferences()
    loader = _loader()
    loader.activate(anchor, prefs["visible"], prefs["preferred"],
                    st.session_state.get(_NEIGHBORS_KEY, ()))
    payload = _map_payload(anchor, loader.snapshot(anchor))
    center, boundary = resources.roi_view(city)
    st.caption(
        "Layers prepare in the background. Your layer selection and map view "
        "are kept when changing dates."
    )
    # A stable key and location preserve the iframe and its existing tile DOM.
    _scene_map(**payload, center=center, boundary=boundary,
               key=_COMPONENT_KEY, default=None, on_change=_on_map_change)


def render_live_cloud_stats(city, row):
    """Display completed statistics without ever waiting for Earth Engine."""
    st.session_state[_STATS_REQUEST_KEY] = (
        _scene_anchor(city, row), float(row["roi_cloud_pct"])
    )
    _render_live_cloud_stats()


@st.fragment(run_every=_FRAGMENT_INTERVAL)
def _render_live_cloud_stats():
    anchor, catalog_roi_cloud_pct = st.session_state[_STATS_REQUEST_KEY]
    result = _loader().snapshot(anchor).get("stats")
    st.markdown("**Live ROI cloud (from map)**")
    if result is None:
        st.info("Calculating live ROI cloud statistics…")
    elif result["error"]:
        st.warning(f"Live ROI cloud statistics could not be loaded. {result['error']}")
        if st.button("Retry cloud statistics", key="retry_cloud_stats"):
            _loader().retry(anchor, "stats")
    else:
        numbers = result["value"]
        st.write(f"cloud (of city): **{numbers['roi_cloud_pct']}%**")
        st.write(f"cloud (of valid): **{numbers['roi_cloud_of_valid_pct']}%**")
        st.write(f"valid pixels: **{numbers['roi_valid_pct']}%**")
        st.caption(f"Cross-check: catalog roi_cloud_pct = {catalog_roi_cloud_pct:.1f}%")
