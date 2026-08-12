"""UTOMIKA cloud-labeling app.

A one-screen review tool: pick a city, scroll the catalog of Landsat overpasses
in the sidebar, open one on an interactive GEE map (true-color, cloud mask,
NDVI, LST, ECI), and record a cloudy / clear / unsure verdict to Supabase.

Run locally:   streamlit run app.py
Secrets:       see .streamlit/secrets.toml.example
"""

import calendar
import io
import math
import os
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
import folium
from branca.element import MacroElement
from jinja2 import Template
from streamlit_folium import st_folium

import gee_common as gc
import gee_app
import db
import weather

# Resolve data files relative to this script, not the process CWD — so the app
# works regardless of where Streamlit Cloud runs it from.
_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_HERE, "data")

st.set_page_config(page_title="UTOMIKA cloud labeling", layout="wide")

# Scenes are shown in pages of this size in the sidebar radio, rather than one
# flat list of every filtered row (thousands of options was the main render cost).
PAGE_SIZE = 25

CITIES = {"Zagreb": "zagreb", "Split": "split"}
LABELS = ("clear", "cloudy", "unsure")
LABEL_EMOJI = {"clear": "☀️ clear", "cloudy": "☁️ cloudy", "unsure": "❓ unsure"}


# --------------------------------------------------------------------------
# Cached resources
# --------------------------------------------------------------------------
@st.cache_resource(show_spinner="Connecting to Earth Engine…")
def _init_ee():
    # Prefer a [gee_service_account] TOML *table* (each JSON field as its own
    # key). It sidesteps the "Invalid control character" JSONDecodeError that a
    # single pasted JSON string triggers when the private_key's newlines aren't
    # escaped — the #1 secrets pitfall. The legacy one-string form still works.
    if "gee_service_account" in st.secrets:
        sa = dict(st.secrets["gee_service_account"])
    else:
        sa = st.secrets.get("GEE_SERVICE_ACCOUNT_JSON") or None
    gee_app.init_ee(
        project=st.secrets.get("GEE_PROJECT", "utomika"),
        service_account_json=sa,
    )
    return True


def _normalize_supabase_url(raw):
    """Reduce a pasted Supabase URL to its bare origin (scheme://host).

    supabase-py appends ``/rest/v1/...`` itself, so the base URL must be just
    ``https://<ref>.supabase.co`` — no trailing slash and no ``/rest/v1`` path.
    Pasting the full REST endpoint (a common mistake) otherwise yields
    ``/rest/v1/rest/v1/labels`` and PostgREST error PGRST125 ("Invalid path").
    This strips any path/query so all those variants work.
    """
    url = (raw or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else url.rstrip("/")


@st.cache_resource(show_spinner="Connecting to Supabase…")
def _supabase():
    url = _normalize_supabase_url(st.secrets.get("SUPABASE_URL"))
    key = (st.secrets.get("SUPABASE_KEY") or "").strip()
    if not url or "YOUR-PROJECT" in url:
        raise RuntimeError(
            "SUPABASE_URL is missing or still the placeholder. Set it to your "
            "project URL from Supabase → Project Settings → API → Project URL "
            "(looks like https://abcdefghij.supabase.co)."
        )
    if not key or key.startswith("YOUR-"):
        raise RuntimeError(
            "SUPABASE_KEY is missing or still the placeholder. Paste the "
            "service_role key (a long JWT starting with 'eyJ') from "
            "Supabase → Project Settings → API — not the project ID/ref."
        )
    return db.get_client(url, key)


@st.cache_data(show_spinner=False)
def _load_catalog(city):
    """Load and lightly type the per-city cloud-cover catalog."""
    df = pd.read_csv(os.path.join(_DATA, f"cloud_cover_{city}.csv"))
    df["date"] = pd.to_datetime(df["date"]).dt.date
    idx = pd.DatetimeIndex(df["date"])
    df["year"] = idx.year
    df["month"] = idx.month
    return df


@st.cache_resource(show_spinner="Loading region…")
def _roi(city):
    return gee_app.load_roi_geojson(city)


@st.cache_data(show_spinner=False)
def _roi_view(city):
    """Map center [lat, lon] + boundary GeoJSON for a city, fetched once.

    Both come from Earth Engine getInfo() calls, so they are memoized here
    rather than recomputed on every Streamlit rerun (they only depend on the
    fixed city geometry).
    """
    roi = _roi(city)
    lon, lat = roi.centroid(maxError=1).coordinates().getInfo()
    return [lat, lon], roi.getInfo()


# ttl bounds how long a cached tile-URL is reused: GEE getMapId tokens are
# ephemeral, so without an expiry a scene revisited hours into a session could
# serve a dead tile URL. 30 min keeps re-opening instant while refreshing tokens.
@st.cache_data(show_spinner="Rendering scene tiles…", ttl=1800)
def _scene_tiles(city, image_id, sat, tier):
    """Compute all layer tile-URLs + ROI cloud numbers for one scene.

    Cached per (city, image_id) so re-opening a date is instant. Returns a dict
    of {layer_name: xyz_url_template} plus the ROI cloud stats printed alongside.
    """
    roi = _roi(city)
    img = gee_app.load_scene(image_id, sat=sat, tier=tier, roi=roi)

    true_color = gc.true_color(img).visualize(**gee_app.VIS["true_color"])
    cloud = (
        gc.cloud_bits(img.select("QA_PIXEL"))
        .selfMask().visualize(**gee_app.VIS["cloud"])
    )
    ndvi = gee_app.ndvi(img).visualize(**gee_app.VIS["ndvi"])
    lst = gc.lst_celsius(img).visualize(**gee_app.VIS["lst"])
    eci = gee_app.eci(img, roi).visualize(**gee_app.VIS["eci"])

    tiles = {
        "True color": gee_app.tile_layer_url(true_color, {}),
        "Cloud / shadow mask": gee_app.tile_layer_url(cloud, {}),
        "NDVI": gee_app.tile_layer_url(ndvi, {}),
        "LST (°C)": gee_app.tile_layer_url(lst, {}),
        "ECI": gee_app.tile_layer_url(eci, {}),
    }

    stats = gc.roi_cloud_stats(img, roi).getInfo()
    valid = stats.get("valid") or 0.0
    cloud_frac = stats.get("cloud") or 0.0
    cloud_of_valid = (cloud_frac / valid * 100) if valid > 0 else float("nan")
    numbers = {
        "roi_valid_pct": round(valid * 100, 1),
        "roi_cloud_pct": round(cloud_frac * 100, 1),
        "roi_cloud_of_valid_pct": round(cloud_of_valid, 1),
    }
    return tiles, numbers


# --------------------------------------------------------------------------
# Login gate
# --------------------------------------------------------------------------
def _login_gate():
    if st.session_state.get("authed"):
        return True

    st.title("🛰️ UTOMIKA cloud labeling")
    st.caption("Review Landsat overpasses of Zagreb & Split and label each cloudy / clear.")
    with st.form("login"):
        name = st.text_input("Your name / initials", key="login_name")
        pw = st.text_input("App password", type="password")
        ok = st.form_submit_button("Enter")
    if ok:
        if pw == st.secrets.get("APP_PASSWORD"):
            if not name.strip():
                st.error("Please enter your name so labels are attributed.")
                return False
            st.session_state["authed"] = True
            st.session_state["labeler"] = name.strip()
            st.rerun()
        else:
            st.error("Wrong password.")
    return False


# --------------------------------------------------------------------------
# Sidebar — city, filters, date list
# --------------------------------------------------------------------------
# Keys of the ordinary filter widgets. Popping these resets each widget to its
# default on the next run — used when a date search takes over the catalog.
_FILTER_KEYS = ("cloud_filter", "sat_filter", "year_filter",
                "month_filter", "hide_labeled")


@st.dialog("Apply date search?")
def _confirm_date_search(d):
    """Confirm that a date search should clear all other filters.

    On Apply we drop the ordinary filter widgets' state (so they snap back to
    defaults) and set a sticky "active_date"; _sidebar then shows only that
    day's scene(s), ignoring the other filters, on page 1.
    """
    st.write(
        f"This will clear your other filters and show only scene(s) on **{d}**.")
    c1, c2 = st.columns(2)
    if c1.button("Apply", type="primary", use_container_width=True):
        for k in _FILTER_KEYS:
            st.session_state.pop(k, None)
        st.session_state["active_date"] = d
        st.session_state["scene_page"] = 0
        st.session_state.pop("scene_radio", None)
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


def _step_page(delta, n_pages):
    """Prev/next page button callback — keeps scene_page and page_input in sync."""
    p = max(0, min(st.session_state.get("scene_page", 0) + delta, n_pages - 1))
    st.session_state["scene_page"] = p
    st.session_state["page_input"] = p + 1


def _page_input_changed():
    """'Go to page' number-input callback — drives scene_page from the widget."""
    st.session_state["scene_page"] = int(st.session_state["page_input"]) - 1


def _sidebar(df, labeled):
    st.sidebar.header("Catalog")
    st.sidebar.write(f"Labeler: **{st.session_state['labeler']}**")
    if st.sidebar.button("Log out"):
        for k in ("authed", "labeler", "scene_radio", "pending_scene",
                  "scene_page", "active_date"):
            st.session_state.pop(k, None)
        st.rerun()

    # Date search — jumps straight to a scene's date. It overrides all other
    # filters (after a confirmation modal), so while a date is active we hide the
    # ordinary filters and show only that day's scene(s).
    active_date = st.session_state.get("active_date")
    if active_date is not None:
        st.sidebar.info(f"Date filter: **{active_date}**")
        if st.sidebar.button("Clear date filter", use_container_width=True):
            st.session_state.pop("active_date", None)
            st.session_state["scene_page"] = 0
            st.rerun()
        view = df[df["date"] == active_date].copy()
    else:
        # Span the whole catalog: st.date_input otherwise caps the year picker at
        # ~10 years back, hiding the pre-2016 scenes.
        search_date = st.sidebar.date_input(
            "Search date", value=None,
            min_value=df["date"].min(), max_value=df["date"].max(),
            key="date_search")
        if st.sidebar.button("Search", use_container_width=True):
            if search_date is None:
                st.sidebar.warning("Pick a date to search.")
            elif (df["date"] == search_date).any():
                _confirm_date_search(search_date)
            else:
                st.sidebar.error(f"No scene exists on {search_date}.")

        # Filters
        max_cloud = st.sidebar.slider(
            "Max ROI cloud %", 0, 100, 100, step=5, key="cloud_filter")
        sats = sorted(df["sat"].dropna().unique())
        sat_sel = st.sidebar.multiselect(
            "Satellite", sats, default=sats, key="sat_filter")
        years = sorted(df["year"].unique())
        yr = st.sidebar.select_slider(
            "Year range", options=years, value=(years[0], years[-1]),
            key="year_filter")
        months = list(range(1, 13))
        mo_sel = st.sidebar.multiselect(
            "Months", months, default=months,
            format_func=lambda m: calendar.month_abbr[m], key="month_filter")
        hide_labeled = st.sidebar.checkbox(
            "Hide already-labeled", value=False, key="hide_labeled")

        # NaN roi_cloud_pct (scenes with no valid pixels) must stay visible, not
        # be silently dropped by the `<=` comparison (NaN <= x is always False).
        cloud_ok = df["roi_cloud_pct"].isna() | (df["roi_cloud_pct"] <= max_cloud)
        view = df[
            cloud_ok
            & (df["sat"].isin(sat_sel))
            & (df["year"] >= yr[0])
            & (df["year"] <= yr[1])
            & (df["month"].isin(mo_sel))
        ].copy()
        if hide_labeled:
            view = view[~view["image_id"].isin(labeled)]

    st.sidebar.caption(
        f"{len(view)} of {len(df)} scenes · {len(labeled)} labeled")

    # Build compact human-readable labels for the radio list.
    def _row_label(r):
        mark = "✓ " if r["image_id"] in labeled else ""
        return (f"{mark}{r['date']} · {r['sat']} · "
                f"scene {r['scene_cloud_cover']:.0f}% · roi {r['roi_cloud_pct']:.0f}%")

    view = view.sort_values("date").reset_index(drop=True)
    all_ids = view["image_id"].tolist()
    if not all_ids:
        st.sidebar.info("No scenes match the filters.")
        return view, None

    # A programmatic jump (auto-advance) cannot write the radio's own key after
    # the widget is instantiated, so it stashes its target in "pending_scene"
    # (and the matching page in "scene_page"); apply it here, before the widget.
    pending = st.session_state.pop("pending_scene", None)
    if pending in all_ids:
        st.session_state["scene_radio"] = pending

    # Pagination: only PAGE_SIZE options reach the radio, instead of every
    # filtered row (thousands of options was the main render cost). "scene_page"
    # is the single source of truth (0-indexed); auto-advance and date jumps set
    # it directly. All manual page changes go through callbacks that keep it and
    # the "page_input" number widget in sync, so the two never fight.
    n_pages = max(1, math.ceil(len(all_ids) / PAGE_SIZE))
    page = min(max(0, st.session_state.get("scene_page", 0)), n_pages - 1)
    st.session_state["scene_page"] = page
    # Mirror an external page change (auto-advance/date jump) into the widget key
    # before the widget is built; harmless no-op after manual changes.
    if st.session_state.get("page_input") != page + 1:
        st.session_state["page_input"] = page + 1

    pcols = st.sidebar.columns([1, 2, 1])
    pcols[0].button("◀", disabled=(page == 0), key="prev_page",
                    use_container_width=True, on_click=_step_page,
                    args=(-1, n_pages))
    pcols[2].button("▶", disabled=(page >= n_pages - 1), key="next_page",
                    use_container_width=True, on_click=_step_page,
                    args=(1, n_pages))
    pcols[1].markdown(
        f"<div style='text-align:center;padding-top:6px'>"
        f"Page {page + 1} of {n_pages}</div>",
        unsafe_allow_html=True)
    if n_pages > 1:
        st.sidebar.number_input(
            "Go to page", min_value=1, max_value=n_pages,
            key="page_input", on_change=_page_input_changed)
    page = min(max(0, st.session_state["scene_page"]), n_pages - 1)
    st.session_state["scene_page"] = page

    page_rows = view.iloc[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    page_ids = page_rows["image_id"].tolist()
    labels_map = {r["image_id"]: _row_label(r) for _, r in page_rows.iterrows()}

    # Snap the selection back onto the visible page whenever it points elsewhere
    # (city/filter/page change) — otherwise Streamlit raises because the stored
    # value isn't among the radio's options.
    if st.session_state.get("scene_radio") not in page_ids:
        st.session_state["scene_radio"] = page_ids[0]

    chosen = st.sidebar.radio(
        "Scene", page_ids,
        format_func=lambda i: labels_map[i], key="scene_radio",
    )
    return view, chosen


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


def _add_png_export(m, image_id):
    """Attach the client-side "download current view as PNG" camera control."""
    control = MacroElement()
    control._template = _EXPORT_CONTROL_TEMPLATE
    control.filename = f"utomika_{image_id}.png"
    control.lib_url = _EXPORT_LIB_URL
    m.add_child(control)


def _render_map(city, row, tiles):
    center, boundary = _roi_view(city)  # cached; no EE round-trip per rerun
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
    _add_png_export(m, row["image_id"])
    st_folium(m, height=560, use_container_width=True,
              returned_objects=[], key=f"map_{row['image_id']}")


def _advance_to_next(view, current_id, labeled):
    """Queue the next still-unlabeled scene after the current one.

    Writes "pending_scene" rather than the radio's own key, because the radio
    widget has already been instantiated this run (Streamlit forbids mutating a
    live widget's key). _sidebar applies the pending target before the widget on
    the next run, which _label_controls triggers via st.rerun().
    """
    ids = view["image_id"].tolist()
    try:
        start = ids.index(current_id)
    except ValueError:
        start = -1
    for offset, nxt in enumerate(ids[start + 1:], start=start + 1):
        if nxt not in labeled:
            st.session_state["pending_scene"] = nxt
            # Flip the sidebar to the page that scene lives on (same ordering as
            # _sidebar, which sorts by date and paginates by PAGE_SIZE).
            st.session_state["scene_page"] = offset // PAGE_SIZE
            return
    # Nothing unlabeled after this one — stay put.


def _label_controls(client, city, row, view, labeled):
    existing = labeled.get(row["image_id"])
    if existing:
        st.info(f"Your current label for this scene: **{LABEL_EMOJI[existing]}**")

    notes = st.text_input("Note (optional)", key=f"note_{row['image_id']}")

    cols = st.columns(3)
    clicked = None
    if cols[0].button("☀️ Clear", use_container_width=True):
        clicked = "clear"
    if cols[1].button("☁️ Cloudy", use_container_width=True):
        clicked = "cloudy"
    if cols[2].button("❓ Unsure", use_container_width=True):
        clicked = "unsure"

    if clicked:
        db.upsert_label(
            client,
            city=city,
            image_date=row["date"],
            image_id=row["image_id"],
            sat=row["sat"],
            scene_cloud_cover=row["scene_cloud_cover"],
            roi_cloud_pct=row["roi_cloud_pct"],
            label=clicked,
            labeler=st.session_state["labeler"],
            notes=notes,
        )
        st.toast(f"Saved: {LABEL_EMOJI[clicked]}", icon="✅")
        # Update local cache so the ✓ shows immediately, then advance.
        labeled[row["image_id"]] = clicked
        _get_labeled.clear()
        _advance_to_next(view, row["image_id"], labeled)
        st.rerun()


@st.cache_data(show_spinner=False, ttl=30)
def _get_labeled(city, labeler):
    """Cached label lookup for the sidebar ✓ markers (short TTL: it changes)."""
    return db.get_labeled_ids(_supabase(), city, labeler=labeler)


def _export_df(client, labeler):
    """One labeler's labels (all cities), enriched with catalog-only columns.

    The `labels` table stores sat/scene_cloud_cover/roi_cloud_pct, but not
    tier, roi_valid_pct, or the WRS scene coordinates — those live only in the
    per-city catalog CSVs. Left-merge them on (city, image_id) so the export
    carries every field a scene can have. Returns None if the labeler has none.
    """
    rows = db.fetch_labels(client, labeler=labeler)
    if not rows:
        return None
    labels = pd.DataFrame(rows)

    extra = ["image_id", "tier", "roi_valid_pct",
             "wrs_path", "wrs_row", "scene_cloud_cover_land"]
    cats = []
    for city in CITIES.values():
        c = _load_catalog(city)[extra].copy()
        c["city"] = city
        cats.append(c)
    catalog = pd.concat(cats, ignore_index=True)

    merged = labels.merge(catalog, on=["city", "image_id"], how="left")

    # Human-first column order; drop the internal `id`. Keep only present cols.
    cols = ["city", "image_date", "sat", "tier", "image_id",
            "wrs_path", "wrs_row",
            "scene_cloud_cover", "scene_cloud_cover_land",
            "roi_valid_pct", "roi_cloud_pct",
            "label", "notes", "labeler", "created_at", "updated_at"]
    return merged[[c for c in cols if c in merged.columns]]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def _fatal(title, err, hint):
    """Show an actionable error and stop, instead of a raw traceback."""
    st.error(f"**{title}**\n\n{hint}")
    st.expander("Technical detail").code(f"{type(err).__name__}: {err}")
    st.stop()


def _shift_weather_window(state_key, delta):
    """Move the fixed weather viewport without allowing a future offset."""
    st.session_state[state_key] = weather.shift_window_offset(
        st.session_state.get(state_key, 0), delta
    )


@st.fragment
def _weather_panel(city, image_id, selected_date):
    """Render weather independently so its controls do not rebuild the GEE map."""
    anchor = (city, image_id)
    if st.session_state.get("weather_anchor") != anchor:
        st.session_state["weather_anchor"] = anchor
        st.session_state["weather_offset_days"] = 0

    store = weather.load_weather_store(_DATA)
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


def main():
    if not _login_gate():
        st.stop()

    try:
        _init_ee()
    except Exception as e:  # noqa: BLE001
        _fatal(
            "Earth Engine sign-in failed.",
            e,
            "Check the service-account secret. Preferred form is a "
            "`[gee_service_account]` table in `.streamlit/secrets.toml` (see "
            "secrets.toml.example) — that avoids JSON control-character errors "
            "from unescaped newlines in `private_key`. Also confirm `GEE_PROJECT`.",
        )

    try:
        client = _supabase()
    except Exception as e:  # noqa: BLE001
        _fatal("Supabase connection failed.", e,
               "Check `SUPABASE_URL` and `SUPABASE_KEY` in your secrets.")

    # Switching city rebuilds the whole catalog, so a page/date carried over from
    # the previous city would be meaningless — reset them.
    def _on_city_change():
        for k in ("scene_page", "active_date", "scene_radio"):
            st.session_state.pop(k, None)

    city_name = st.sidebar.selectbox(
        "City", list(CITIES), key="city", on_change=_on_city_change)
    city = CITIES[city_name]

    df = _load_catalog(city)
    try:
        labeled = dict(_get_labeled(city, st.session_state["labeler"]))
    except Exception as e:  # noqa: BLE001
        _fatal(
            "Couldn't reach Supabase.",
            e,
            "The URL didn't resolve or the query failed. Verify `SUPABASE_URL` "
            "is your exact project URL (no typo, host resolves), you have "
            "network access, and the `labels` table exists (run "
            "`supabase_schema.sql` in the SQL editor).",
        )

    view, active_id = _sidebar(df, labeled)
    if active_id is None:
        st.info("Adjust the filters to see scenes.")
        return

    row = df[df["image_id"] == active_id].iloc[0]

    st.subheader(f"{city_name} — {row['date']} · {row['sat']}")

    _weather_panel(city, active_id, row["date"])

    meta = st.columns(4)
    meta[0].metric("Scene cloud %", f"{row['scene_cloud_cover']:.0f}")
    meta[1].metric("ROI cloud % (catalog)", f"{row['roi_cloud_pct']:.0f}")
    meta[2].metric("ROI valid %", f"{row['roi_valid_pct']:.0f}")
    meta[3].metric("Tier", row["tier"])

    try:
        tiles, numbers = _scene_tiles(city, active_id, row["sat"], row["tier"])
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not load this scene from Earth Engine:\n\n{e}")
        return

    left, right = st.columns([3, 1])
    with left:
        _render_map(city, row, tiles)
    with right:
        st.markdown("**Live ROI cloud (from map)**")
        st.write(f"cloud (of city): **{numbers['roi_cloud_pct']}%**")
        st.write(f"cloud (of valid): **{numbers['roi_cloud_of_valid_pct']}%**")
        st.write(f"valid pixels: **{numbers['roi_valid_pct']}%**")
        st.caption(
            f"Cross-check: catalog roi_cloud_pct = {row['roi_cloud_pct']:.1f}%"
        )
        st.divider()
        _label_controls(client, city, row, view, labeled)

    # Export
    with st.sidebar.expander("Export my labels"):
        if st.button("Load my labels as CSV"):
            export = _export_df(client, st.session_state["labeler"])
            if export is not None:
                st.download_button(
                    "Download labels.csv",
                    data=export.to_csv(index=False),
                    file_name="utomika_labels.csv", mime="text/csv",
                )
            else:
                st.caption("You haven't labeled any scenes yet.")


if __name__ == "__main__":
    main()
