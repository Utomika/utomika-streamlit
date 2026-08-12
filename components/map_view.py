import time

import folium
from branca.element import MacroElement
from jinja2 import Template
import streamlit as st
from streamlit_folium import st_folium

import resources


# --------------------------------------------------------------------------
# Main panel — map + labeling
# --------------------------------------------------------------------------
# In-browser PNG export of the *current* map view (zoom/pan + whichever overlays
# are toggled on). Pure client-side JS: it stays dormant until the camera button
# is clicked, so it adds no Streamlit reruns and no idle cost — the map keeps
# returned_objects=[]. html-to-image snapshots the live map DOM (tiles must be
# CORS-loaded, hence cross_origin=True on every TileLayer) and downloads a PNG at
# the browser's device pixel ratio (retina-sharp). We use html-to-image rather
# than dom-to-image (hangs inlining folium's cross-origin CSS) or leaflet-image
# (drops overlay tile layers, capturing only the basemap).
#
# The library is loaded *lazily inside the click handler*, not via a <script> in
# the folium header: streamlit-folium's _get_header() strips every `<script src>`
# tag before embedding the map in its iframe, so a header include never runs. A
# runtime-injected script survives that, and it also means nothing is fetched
# until the user actually exports.
_EXPORT_LIB_URL = (
    "https://cdn.jsdelivr.net/npm/html-to-image@1.11.11/dist/html-to-image.js"
)

_EXPORT_CONTROL_TEMPLATE = Template("""
{% macro script(this, kwargs) %}
(function() {
    var map = {{ this._parent.get_name() }};
    var LIB_URL = {{ this.lib_url|tojson }};

    // Load html-to-image once, on first use (survives header <script> stripping).
    function ensureLib(cb, onError) {
        if (window.htmlToImage) { cb(); return; }
        var s = document.getElementById('utomika-hti-lib');
        if (s) {
            s.addEventListener('load', cb);
            s.addEventListener('error', onError);
            return;
        }
        s = document.createElement('script');
        s.id = 'utomika-hti-lib';
        s.src = LIB_URL;
        s.onload = cb;
        s.onerror = onError;
        document.head.appendChild(s);
    }

    var ExportControl = L.Control.extend({
        options: { position: 'topleft' },
        onAdd: function() {
            var container = L.DomUtil.create(
                'div', 'leaflet-bar leaflet-control utomika-export');
            var link = L.DomUtil.create('a', '', container);
            link.href = '#';
            link.title = 'Download the current map view as PNG';
            link.innerHTML = '\\u{1F4F7}';  // camera
            link.style.fontSize = '18px';
            link.style.textAlign = 'center';
            link.style.lineHeight = '30px';
            L.DomEvent.disableClickPropagation(container);

            function warn(msg) {
                link.innerHTML = '\\u26A0';  // warning
                link.title = msg;
                setTimeout(function() {
                    link.innerHTML = '\\u{1F4F7}';
                    link.title = 'Download the current map view as PNG';
                }, 3000);
            }

            L.DomEvent.on(link, 'click', function(e) {
                L.DomEvent.stop(e);
                link.innerHTML = '\\u2026';  // ellipsis (working…)
                ensureLib(function() {
                    if (!window.htmlToImage) {
                        warn('Export failed — capture library did not load');
                        return;
                    }
                    // Exclude our own button; capture everything else the user
                    // sees (basemap, visible overlays, boundary, controls).
                    window.htmlToImage.toBlob(map.getContainer(), {
                        skipFonts: true,
                        cacheBust: false,
                        filter: function(n) {
                            return !(n.classList &&
                                     n.classList.contains('utomika-export'));
                        }
                    }).then(function(blob) {
                        if (!blob) { throw new Error('empty blob'); }
                        link.innerHTML = '\\u{1F4F7}';
                        var url = URL.createObjectURL(blob);
                        try {
                            var a = document.createElement('a');
                            a.href = url;
                            a.download = {{ this.filename|tojson }};
                            document.body.appendChild(a);
                            a.click();
                            document.body.removeChild(a);
                        } catch (err) {
                            // Sandboxed iframes may block downloads — open instead.
                            window.open(url, '_blank');
                        }
                        setTimeout(function() {
                            URL.revokeObjectURL(url);
                        }, 15000);
                    }).catch(function(err) {
                        // Most likely a tainted canvas (a tile server w/o CORS).
                        warn('Export failed — a tile server blocked capture');
                        console.error('PNG export failed:', err);
                    });
                }, function() {
                    warn('Export failed — could not load capture library');
                });
            });
            return container;
        }
    });
    map.addControl(new ExportControl());
})();
{% endmacro %}
""")


def add_png_export(m, image_id):
    """Attach the client-side "download current view as PNG" camera control."""
    control = MacroElement()
    control._template = _EXPORT_CONTROL_TEMPLATE
    control.filename = f"utomika_{image_id}.png"
    control.lib_url = _EXPORT_LIB_URL
    m.add_child(control)


def render_map(city, row, tiles, component_key=None):
    """Render whichever scene layers are currently available."""
    center, boundary = resources.roi_view(city)  # cached; no EE round-trip per rerun
    # tiles=None so we can add the basemap ourselves with cross_origin=True; the
    # PNG export (below) reads tile pixels into a canvas, which requires every
    # tile image to be CORS-loaded — otherwise the canvas taints and export fails.
    m = folium.Map(location=center, zoom_start=11,
                   tiles=None, control_scale=True)
    folium.TileLayer(
        "CartoDB positron", control=False, cross_origin=True,
    ).add_to(m)

    show_defaults = {"True color", "Cloud / shadow mask"}
    opacities = {"Cloud / shadow mask": 0.55}
    for name, url in tiles.items():
        folium.TileLayer(
            tiles=url, attr="Google Earth Engine", name=name,
            overlay=True, control=True,
            show=name in show_defaults,
            opacity=opacities.get(name, 1.0),
            cross_origin=True,  # canvas-readable for PNG export
        ).add_to(m)

    folium.GeoJson(
        boundary, name=f"{city} boundary",
        style_function=lambda _f: {"color": "yellow", "weight": 2, "fill": False},
    ).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    add_png_export(m, row["image_id"])
    st_folium(m, height=560, use_container_width=True,
              returned_objects=[],
              key=component_key or f"map_{row['image_id']}")


# --------------------------------------------------------------------------
# Progressive scene loading
# --------------------------------------------------------------------------
PROGRESSIVE_LAYER_NOTE = (
    "True color loads first. Cloud mask, NDVI, LST, and ECI are prepared "
    "next and will appear in the layer menu one by one."
)

_OPTIONAL_LAYERS = (
    ("cloud", "Cloud / shadow mask"),
    ("ndvi", "NDVI"),
    ("lst", "LST (°C)"),
    ("eci", "ECI"),
)
_MAP_STATE_KEY = "_progressive_scene_layers"
_STATS_STATE_KEY = "_progressive_scene_stats"
_FRAGMENT_INTERVAL = 0.75


def _scene_anchor(city, image_id, sat, tier):
    return city, image_id, sat, tier


def _new_layer_state(anchor):
    return {
        "anchor": anchor,
        "created_at": time.time(),
        "tiles": {},
        "next_index": 0,
        "base_presented": False,
        "errors": {},
    }


def _layer_state(city, image_id, sat, tier):
    anchor = _scene_anchor(city, image_id, sat, tier)
    state = st.session_state.get(_MAP_STATE_KEY)
    expired = (
        state is not None
        and time.time() - state["created_at"] >= resources.SCENE_TILE_TTL_SECONDS
    )
    if state is None or state["anchor"] != anchor or expired:
        state = _new_layer_state(anchor)
        st.session_state[_MAP_STATE_KEY] = state
    return state


def _ensure_true_color(state, city, image_id, sat, tier):
    if "True color" not in state["tiles"]:
        state["tiles"]["True color"] = resources.scene_tile(
            city, image_id, sat, tier, "true_color"
        )


def _advance_optional_layer(state, city, image_id, sat, tier):
    """Attempt exactly one optional layer and retain failures in state."""
    layer_key, display_name = _OPTIONAL_LAYERS[state["next_index"]]
    try:
        state["tiles"][display_name] = resources.scene_tile(
            city, image_id, sat, tier, layer_key
        )
    except Exception as exc:  # noqa: BLE001 - optional layer must not kill map
        state["errors"][display_name] = f"{type(exc).__name__}: {exc}"
    state["next_index"] += 1
    return display_name


def _render_layer_state(city, image_id, state, status_message, status_kind="info"):
    status_slot = st.empty()
    getattr(status_slot, status_kind)(status_message)
    for layer_name, error in state["errors"].items():
        st.warning(f"{layer_name} could not be loaded: {error}")
    render_map(
        city,
        {"image_id": image_id},
        state["tiles"],
        component_key=f"map_{image_id}_stage_{state['next_index']}",
    )
    return status_slot


def _completion_status(state):
    if not state["errors"]:
        return "All map layers are ready.", "success"
    failed = ", ".join(state["errors"])
    return f"Layer loading finished; unavailable: {failed}.", "warning"


@st.fragment(run_every=_FRAGMENT_INTERVAL)
def _loading_scene_map(city, image_id, sat, tier):
    state = _layer_state(city, image_id, sat, tier)
    _ensure_true_color(state, city, image_id, sat, tier)

    if not state["base_presented"]:
        _render_layer_state(
            city,
            image_id,
            state,
            "True color is ready. Preparing additional layers…",
        )
        state["base_presented"] = True
        return

    if state["next_index"] >= len(_OPTIONAL_LAYERS):
        message, kind = _completion_status(state)
        _render_layer_state(city, image_id, state, message, kind)
        st.rerun()
        return

    _layer_key, display_name = _OPTIONAL_LAYERS[state["next_index"]]
    stage_number = state["next_index"] + 1
    status_slot = _render_layer_state(
        city,
        image_id,
        state,
        f"Loading {display_name} ({stage_number}/{len(_OPTIONAL_LAYERS)})…",
    )
    _advance_optional_layer(state, city, image_id, sat, tier)

    if display_name in state["errors"]:
        status_slot.warning(f"{display_name} could not be loaded; continuing…")
    else:
        status_slot.success(f"{display_name} is ready.")

    if state["next_index"] >= len(_OPTIONAL_LAYERS):
        st.rerun()


@st.fragment
def _ready_scene_map(city, image_id, sat, tier):
    state = _layer_state(city, image_id, sat, tier)
    message, kind = _completion_status(state)
    _render_layer_state(city, image_id, state, message, kind)


def render_progressive_map(city, row):
    """Render true color first, then provision one optional layer per pass."""
    image_id = row["image_id"]
    sat = row["sat"]
    tier = row["tier"]
    state = _layer_state(city, image_id, sat, tier)
    st.caption(PROGRESSIVE_LAYER_NOTE)

    # This is intentionally the only blocking tile request before the first map.
    if "True color" not in state["tiles"]:
        with st.spinner("Loading true-color scene…"):
            _ensure_true_color(state, city, image_id, sat, tier)
    if state["next_index"] >= len(_OPTIONAL_LAYERS):
        _ready_scene_map(city, image_id, sat, tier)
    else:
        _loading_scene_map(city, image_id, sat, tier)


def _new_stats_state(anchor):
    return {
        "anchor": anchor,
        "placeholder_presented": False,
        "done": False,
        "numbers": None,
        "error": None,
    }


def _stats_state(city, image_id, sat, tier):
    anchor = _scene_anchor(city, image_id, sat, tier)
    state = st.session_state.get(_STATS_STATE_KEY)
    if state is None or state["anchor"] != anchor:
        state = _new_stats_state(anchor)
        st.session_state[_STATS_STATE_KEY] = state
    return state


def _render_cloud_stats_result(state, catalog_roi_cloud_pct):
    st.markdown("**Live ROI cloud (from map)**")
    if state["error"] is not None:
        st.warning(
            "Live ROI cloud statistics could not be loaded. "
            f"{state['error']}"
        )
        return

    numbers = state["numbers"]
    st.write(f"cloud (of city): **{numbers['roi_cloud_pct']}%**")
    st.write(f"cloud (of valid): **{numbers['roi_cloud_of_valid_pct']}%**")
    st.write(f"valid pixels: **{numbers['roi_valid_pct']}%**")
    st.caption(
        f"Cross-check: catalog roi_cloud_pct = {catalog_roi_cloud_pct:.1f}%"
    )


def _fetch_cloud_stats(state, city, image_id, sat, tier):
    """Resolve optional live statistics and convert failures into UI state."""
    try:
        state["numbers"] = resources.scene_cloud_stats(
            city, image_id, sat, tier
        )
    except Exception as exc:  # noqa: BLE001 - stats are an optional cross-check
        state["error"] = f"{type(exc).__name__}: {exc}"
    state["done"] = True


@st.fragment(run_every=_FRAGMENT_INTERVAL)
def _loading_cloud_stats(
    city, image_id, sat, tier, catalog_roi_cloud_pct
):
    state = _stats_state(city, image_id, sat, tier)
    st.markdown("**Live ROI cloud (from map)**")
    st.info("Calculating live ROI cloud statistics…")

    # Return once so the first app pass can finish without waiting on getInfo().
    if not state["placeholder_presented"]:
        state["placeholder_presented"] = True
        return

    _fetch_cloud_stats(state, city, image_id, sat, tier)
    st.rerun()


@st.fragment
def _ready_cloud_stats(city, image_id, sat, tier, catalog_roi_cloud_pct):
    state = _stats_state(city, image_id, sat, tier)
    _render_cloud_stats_result(state, catalog_roi_cloud_pct)


def render_live_cloud_stats(city, row):
    """Load live ROI numbers without holding up the first scene map."""
    image_id = row["image_id"]
    sat = row["sat"]
    tier = row["tier"]
    catalog_roi_cloud_pct = row["roi_cloud_pct"]
    state = _stats_state(city, image_id, sat, tier)
    if state["done"]:
        _ready_cloud_stats(
            city, image_id, sat, tier, catalog_roi_cloud_pct
        )
    else:
        _loading_cloud_stats(
            city, image_id, sat, tier, catalog_roi_cloud_pct
        )
