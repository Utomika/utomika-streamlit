import json
import os
import time
import urllib.request
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
    """Read the shipped boundary locally, without any Earth Engine requests."""
    with open(os.path.join(DATA_DIR, f"{city}.geojson")) as source:
        boundary = json.load(source)
    geometry = boundary
    if geometry["type"] == "FeatureCollection":
        geometry = geometry["features"][0]["geometry"]
    elif geometry["type"] == "Feature":
        geometry = geometry["geometry"]

    def points(coords):
        if isinstance(coords[0], (float, int)):
            yield coords
        else:
            for part in coords:
                yield from points(part)

    coordinates = list(points(geometry["coordinates"]))
    lon = (min(p[0] for p in coordinates) + max(p[0] for p in coordinates)) / 2
    lat = (min(p[1] for p in coordinates) + max(p[1] for p in coordinates)) / 2
    return [lat, lon], geometry


SCENE_TILE_TTL_SECONDS = 1800


def _visualized_scene_layer(img, city_roi, layer):
    """Build one visualized scene layer without evaluating the others."""
    if layer == "true_color":
        return gc.true_color(img).visualize(**gee_app.VIS["true_color"])
    if layer == "cloud":
        return (
            gc.cloud_bits(img.select("QA_PIXEL"))
            .selfMask().visualize(**gee_app.VIS["cloud"])
        )
    if layer == "ndvi":
        return gee_app.ndvi(img).visualize(**gee_app.VIS["ndvi"])
    if layer == "lst":
        return gc.lst_celsius(img).visualize(**gee_app.VIS["lst"])
    if layer == "eci":
        return gee_app.eci(img, city_roi).visualize(**gee_app.VIS["eci"])
    raise ValueError(f"Unknown scene layer: {layer}")


# GEE getMapId tokens are ephemeral. Cache each URL independently so requesting
# true color never constructs NDVI/LST/ECI, while a recently revisited layer is
# still fast. The background loader uses the same expiry for its own URL cache.
@st.cache_data(show_spinner=False, ttl=SCENE_TILE_TTL_SECONDS, max_entries=80)
def scene_tile(city, image_id, sat, tier, layer):
    """Return the XYZ URL template for exactly one scene layer."""
    return fetch_scene_tile(city, image_id, sat, tier, layer)


def fetch_scene_tile(city, image_id, sat, tier, layer):
    """Uncached worker entry point; must not call Streamlit APIs."""
    city_roi = gee_app.load_roi_geojson(city)
    img = gee_app.load_scene(image_id, sat=sat, tier=tier, roi=city_roi)
    visualized = _visualized_scene_layer(img, city_roi, layer)
    return gee_app.tile_layer_url(visualized, {})


# Nodata written into raw float GeoTIFFs (clipped-out / masked pixels) and into
# the raw cloud mask (uint8 can't hold -9999, so masked pixels become 255).
RAW_NODATA_FLOAT = -9999
RAW_NODATA_CLOUD = 255
GEOTIFF_FETCH_TIMEOUT_SECONDS = 300


def _raw_scene_layer(img, city_roi, layer):
    """Build one layer as raw values (no palette), ready for export.

    Mirrors _visualized_scene_layer. Float layers keep their physical units;
    masked pixels are filled with -9999 so the GeoTIFF has no hidden gaps. The
    cloud mask is uint8 (1 = cloud/shadow, 0 = clear, 255 = no data).
    """
    if layer == "cloud":
        return (
            gc.cloud_bits(img.select("QA_PIXEL"))
            .unmask(RAW_NODATA_CLOUD).toByte()
        )
    if layer == "true_color":
        raw = gc.true_color(img)
    elif layer == "ndvi":
        raw = gee_app.ndvi(img)
    elif layer == "lst":
        raw = gc.lst_celsius(img)
    elif layer == "eci":
        raw = gee_app.eci(img, city_roi)
    else:
        raise ValueError(f"Unknown scene layer: {layer}")
    return raw.toFloat().unmask(RAW_NODATA_FLOAT)


@st.cache_data(show_spinner=False, ttl=SCENE_TILE_TTL_SECONDS, max_entries=10)
def scene_geotiff(city, image_id, sat, tier, layer, kind):
    """Return GeoTIFF bytes for one scene layer over the city ROI.

    ``kind`` is "raw" (physical values, see _raw_scene_layer) or "rgb" (the
    palette-visualized 8-bit image exactly as painted on the map).
    """
    city_roi = roi(city)
    img = gee_app.load_scene(image_id, sat=sat, tier=tier, roi=city_roi)
    if kind == "raw":
        image = _raw_scene_layer(img, city_roi, layer)
    elif kind == "rgb":
        image = _visualized_scene_layer(img, city_roi, layer)
    else:
        raise ValueError(f"Unknown GeoTIFF kind: {kind}")
    # EPSG:32633 (UTM 33N) covers both Zagreb and Split and is the native
    # Landsat grid there, so 30 m export needs no resampling. getDownloadURL
    # has a request-size cap (~32 MB), which a single city ROI at 30 m stays
    # well under; a larger region or finer scale would fail with an EE error.
    url = image.getDownloadURL({
        "region": city_roi,
        "scale": 30,
        "crs": "EPSG:32633",
        "format": "GEO_TIFF",
    })
    with urllib.request.urlopen(
        url, timeout=GEOTIFF_FETCH_TIMEOUT_SECONDS
    ) as resp:
        return resp.read()


@st.cache_data(show_spinner=False)
def scene_cloud_stats(city, image_id, sat, tier):
    """Calculate stable live ROI cloud numbers independently of map tiles."""
    return fetch_scene_cloud_stats(city, image_id, sat, tier)


def fetch_scene_cloud_stats(city, image_id, sat, tier):
    """Uncached statistics worker, independent of the map's tile requests."""
    city_roi = gee_app.load_roi_geojson(city)
    img = gee_app.load_scene(image_id, sat=sat, tier=tier, roi=city_roi)
    stats = gc.roi_cloud_stats(img, city_roi).getInfo()
    valid = stats.get("valid") or 0.0
    cloud_frac = stats.get("cloud") or 0.0
    cloud_of_valid = (cloud_frac / valid * 100) if valid > 0 else float("nan")
    numbers = {
        "roi_valid_pct": round(valid * 100, 1),
        "roi_cloud_pct": round(cloud_frac * 100, 1),
        "roi_cloud_of_valid_pct": round(cloud_of_valid, 1),
    }
    return numbers


@st.cache_data(show_spinner=False, ttl=30)
def get_labeled(city, labeler):
    """Cached label lookup for the sidebar ✓ markers (short TTL: it changes)."""
    return db.get_labeled_ids(get_supabase(), city, labeler=labeler)


LABELS_RETRY_SECONDS = 60


def mark_labels_down(err):
    """Back off label requests for this session after a read or write failure."""
    st.session_state["labels_down_until"] = time.time() + LABELS_RETRY_SECONDS
    st.session_state["labels_error"] = err


def reset_labels_down():
    """Allow an immediate retry, including a fresh lookup of label state."""
    st.session_state.pop("labels_down_until", None)
    st.session_state.pop("labels_error", None)
    get_labeled.clear()


def load_labels(city, labeler) -> tuple[dict, Exception | None]:
    """Load optional labels without blocking scene browsing during an outage."""
    if st.session_state.get("labels_down_until", 0) > time.time():
        return {}, st.session_state["labels_error"]
    try:
        return dict(get_labeled(city, labeler)), None
    except Exception as e:  # noqa: BLE001
        mark_labels_down(e)
        return {}, e


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
