import os
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

from config import CITIES, DATA_DIR
import db
import gee_app
import gee_common as gc


# --------------------------------------------------------------------------
# Cached resources
# --------------------------------------------------------------------------
@st.cache_resource(show_spinner="Connecting to Earth Engine…")
def init_ee():
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


def normalize_supabase_url(raw):
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
def get_supabase():
    url = normalize_supabase_url(st.secrets.get("SUPABASE_URL"))
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
def load_catalog(city):
    """Load and lightly type the per-city cloud-cover catalog."""
    df = pd.read_csv(os.path.join(DATA_DIR, f"cloud_cover_{city}.csv"))
    df["date"] = pd.to_datetime(df["date"]).dt.date
    idx = pd.DatetimeIndex(df["date"])
    df["year"] = idx.year
    df["month"] = idx.month
    return df


@st.cache_resource(show_spinner="Loading region…")
def roi(city):
    return gee_app.load_roi_geojson(city)


@st.cache_data(show_spinner=False)
def roi_view(city):
    """Map center [lat, lon] + boundary GeoJSON for a city, fetched once.

    Both come from Earth Engine getInfo() calls, so they are memoized here
    rather than recomputed on every Streamlit rerun (they only depend on the
    fixed city geometry).
    """
    city_roi = roi(city)
    lon, lat = city_roi.centroid(maxError=1).coordinates().getInfo()
    return [lat, lon], city_roi.getInfo()


# ttl bounds how long a cached tile-URL is reused: GEE getMapId tokens are
# ephemeral, so without an expiry a scene revisited hours into a session could
# serve a dead tile URL. 30 min keeps re-opening instant while refreshing tokens.
@st.cache_data(show_spinner="Rendering scene tiles…", ttl=1800)
def scene_tiles(city, image_id, sat, tier):
    """Compute all layer tile-URLs + ROI cloud numbers for one scene.

    Cached per (city, image_id) so re-opening a date is instant. Returns a dict
    of {layer_name: xyz_url_template} plus the ROI cloud stats printed alongside.
    """
    city_roi = roi(city)
    img = gee_app.load_scene(image_id, sat=sat, tier=tier, roi=city_roi)

    true_color = gc.true_color(img).visualize(**gee_app.VIS["true_color"])
    cloud = (
        gc.cloud_bits(img.select("QA_PIXEL"))
        .selfMask().visualize(**gee_app.VIS["cloud"])
    )
    ndvi = gee_app.ndvi(img).visualize(**gee_app.VIS["ndvi"])
    lst = gc.lst_celsius(img).visualize(**gee_app.VIS["lst"])
    eci = gee_app.eci(img, city_roi).visualize(**gee_app.VIS["eci"])

    tiles = {
        "True color": gee_app.tile_layer_url(true_color, {}),
        "Cloud / shadow mask": gee_app.tile_layer_url(cloud, {}),
        "NDVI": gee_app.tile_layer_url(ndvi, {}),
        "LST (°C)": gee_app.tile_layer_url(lst, {}),
        "ECI": gee_app.tile_layer_url(eci, {}),
    }

    stats = gc.roi_cloud_stats(img, city_roi).getInfo()
    valid = stats.get("valid") or 0.0
    cloud_frac = stats.get("cloud") or 0.0
    cloud_of_valid = (cloud_frac / valid * 100) if valid > 0 else float("nan")
    numbers = {
        "roi_valid_pct": round(valid * 100, 1),
        "roi_cloud_pct": round(cloud_frac * 100, 1),
        "roi_cloud_of_valid_pct": round(cloud_of_valid, 1),
    }
    return tiles, numbers


@st.cache_data(show_spinner=False, ttl=30)
def get_labeled(city, labeler):
    """Cached label lookup for the sidebar ✓ markers (short TTL: it changes)."""
    return db.get_labeled_ids(get_supabase(), city, labeler=labeler)


def export_df(client, labeler):
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
        c = load_catalog(city)[extra].copy()
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
