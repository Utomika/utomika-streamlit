"""UTOMIKA cloud-labeling app.

A one-screen review tool: pick a city, scroll the catalog of Landsat overpasses
in the sidebar, open one on an interactive GEE map (true-color, cloud mask,
NDVI, LST, ECI), and record a cloudy / clear / unsure verdict to Supabase.

Run locally:   streamlit run app.py
Secrets:       see .streamlit/secrets.toml.example
"""

import calendar
import io
import os
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium

import gee_common as gc
import gee_app
import db

# Resolve data files relative to this script, not the process CWD — so the app
# works regardless of where Streamlit Cloud runs it from.
_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_HERE, "data")

st.set_page_config(page_title="UTOMIKA cloud labeling", layout="wide")

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
def _sidebar(df, labeled):
    st.sidebar.header("Catalog")
    st.sidebar.write(f"Labeler: **{st.session_state['labeler']}**")
    if st.sidebar.button("Log out"):
        for k in ("authed", "labeler", "scene_radio", "pending_scene"):
            st.session_state.pop(k, None)
        st.rerun()

    # Filters
    max_cloud = st.sidebar.slider("Max ROI cloud %", 0, 100, 100, step=5)
    sats = sorted(df["sat"].dropna().unique())
    sat_sel = st.sidebar.multiselect("Satellite", sats, default=sats)
    years = sorted(df["year"].unique())
    yr = st.sidebar.select_slider(
        "Year range", options=years, value=(years[0], years[-1]))
    mo = st.sidebar.select_slider(
        "Month range", options=list(range(1, 13)), value=(1, 12),
        format_func=lambda m: calendar.month_abbr[m])
    hide_labeled = st.sidebar.checkbox("Hide already-labeled", value=False)

    # NaN roi_cloud_pct (scenes with no valid pixels) must stay visible, not be
    # silently dropped by the `<=` comparison (NaN <= x is always False).
    cloud_ok = df["roi_cloud_pct"].isna() | (df["roi_cloud_pct"] <= max_cloud)
    view = df[
        cloud_ok
        & (df["sat"].isin(sat_sel))
        & (df["year"] >= yr[0])
        & (df["year"] <= yr[1])
        & (df["month"] >= mo[0])
        & (df["month"] <= mo[1])
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
    options = view["image_id"].tolist()
    if not options:
        st.sidebar.info("No scenes match the filters.")
        return view, None

    labels_map = {r["image_id"]: _row_label(r) for _, r in view.iterrows()}

    # The radio's own widget key ("scene_radio") is the single source of truth
    # for the selection. A programmatic jump (auto-advance) cannot write that key
    # after the widget is instantiated, so it stashes its target in
    # "pending_scene"; we apply it here, BEFORE building the widget. We also snap
    # the key back to a valid option whenever a city/filter change leaves it
    # pointing at a scene no longer in the list — otherwise Streamlit raises
    # because the stored value isn't among `options`.
    pending = st.session_state.pop("pending_scene", None)
    if pending in options:
        st.session_state["scene_radio"] = pending
    if st.session_state.get("scene_radio") not in options:
        st.session_state["scene_radio"] = options[0]

    chosen = st.sidebar.radio(
        "Scene", options,
        format_func=lambda i: labels_map[i], key="scene_radio",
    )
    return view, chosen


# --------------------------------------------------------------------------
# Main panel — map + labeling
# --------------------------------------------------------------------------
def _render_map(city, row, tiles):
    center, boundary = _roi_view(city)  # cached; no EE round-trip per rerun
    m = folium.Map(location=center, zoom_start=11,
                   tiles="CartoDB positron", control_scale=True)

    show_defaults = {"True color", "Cloud / shadow mask"}
    opacities = {"Cloud / shadow mask": 0.55}
    for name, url in tiles.items():
        folium.TileLayer(
            tiles=url, attr="Google Earth Engine", name=name,
            overlay=True, control=True,
            show=name in show_defaults,
            opacity=opacities.get(name, 1.0),
        ).add_to(m)

    folium.GeoJson(
        boundary, name=f"{city} boundary",
        style_function=lambda _f: {"color": "yellow", "weight": 2, "fill": False},
    ).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
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
    for nxt in ids[start + 1:]:
        if nxt not in labeled:
            st.session_state["pending_scene"] = nxt
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

    city_name = st.sidebar.selectbox("City", list(CITIES))
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
