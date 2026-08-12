import folium
from branca.element import MacroElement
from jinja2 import Template
import streamlit as st
from streamlit_folium import st_folium

from resources import roi_view


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


def render_map(city, row, tiles):
    center, boundary = roi_view(city)  # cached; no EE round-trip per rerun
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
              returned_objects=[], key=f"map_{row['image_id']}")
