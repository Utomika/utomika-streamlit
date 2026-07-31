"""Earth Engine glue for the Streamlit cloud-labeling app.

Thin wrapper over the copied ``gee_common`` helpers. Adds only what the app
needs beyond the catalog scripts:

  * ``init_ee``            — headless (service-account) EE auth for a deploy.
  * ``load_roi_geojson``   — read a pre-exported ROI GeoJSON (no geopandas).
  * ``load_scene``         — resolve one catalog ``image_id`` to the exact
                             harmonized ``ee.Image`` it was computed from.
  * ``ndvi`` / ``eci``     — the two index pieces missing from gee_common,
                             ported verbatim from ``eci_lst_scirpt.js``.
  * ``tile_layer_url``     — a getMapId tile-URL for a visualized image, so the
                             Streamlit/folium map can paint a live GEE layer.

Everything heavier (cloud masking, LST, NDBI, band harmonization) is reused
from ``gee_common`` unchanged.
"""

import json
import os

import ee

import gee_common as gc

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_HERE, "data")

# image_id prefix (e.g. "LT05") -> Earth Engine collection family.
_ASSET_FAMILY = {
    "LT04": "LANDSAT/LT04",
    "LT05": "LANDSAT/LT05",
    "LE07": "LANDSAT/LE07",
    "LC08": "LANDSAT/LC08",
    "LC09": "LANDSAT/LC09",
}

# Short sat code (CSV `sat` column) -> the same family, as a fallback.
_SAT_FAMILY = {
    "L4": "LANDSAT/LT04",
    "L5": "LANDSAT/LT05",
    "L7": "LANDSAT/LE07",
    "L8": "LANDSAT/LC08",
    "L9": "LANDSAT/LC09",
}


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def init_ee(project="utomika", service_account_json=None):
    """Initialize Earth Engine.

    On a deployed (headless) app, interactive ``earthengine authenticate`` is
    impossible, so pass the JSON of a **service-account key** (as a string or a
    dict) via ``service_account_json``; the app reads it from
    ``st.secrets["GEE_SERVICE_ACCOUNT_JSON"]``. When it is omitted we fall back
    to ``gee_common.init_ee`` (a locally-authenticated developer session).
    """
    if service_account_json:
        info = (
            json.loads(service_account_json)
            if isinstance(service_account_json, str)
            else dict(service_account_json)
        )
        creds = ee.ServiceAccountCredentials(
            info["client_email"], key_data=json.dumps(info)
        )
        ee.Initialize(creds, project=project)
    else:
        gc.init_ee(project=project)


# --------------------------------------------------------------------------
# Region of interest (no geopandas at runtime)
# --------------------------------------------------------------------------
def load_roi_geojson(city):
    """Return the pre-exported ROI as an ``ee.Geometry`` (WGS84).

    Reads ``data/{city}.geojson`` (exported once by ``export_rois.py`` from the
    same shapefile + simplify logic as ``gee_common.load_roi``), so the deploy
    ships no shapefiles and needs no geopandas/fiona.
    """
    city = city.lower()
    path = os.path.join(_DATA, f"{city}.geojson")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"ROI GeoJSON not found: {path}. Run export_rois.py once to create it."
        )
    with open(path) as f:
        gj = json.load(f)

    # Accept either a FeatureCollection, a Feature, or a bare geometry.
    if gj.get("type") == "FeatureCollection":
        geom = gj["features"][0]["geometry"]
    elif gj.get("type") == "Feature":
        geom = gj["geometry"]
    else:
        geom = gj
    return ee.Geometry(geom)


# --------------------------------------------------------------------------
# Scene resolution — exact image_id -> harmonized ee.Image
# --------------------------------------------------------------------------
def _asset_id(image_id, sat=None):
    """Turn a catalog ``image_id`` into a full EE asset id.

    Catalog ids come from ``system:index`` of a *merged* collection, so they
    carry a per-merge integer prefix (e.g.
    ``1_1_1_1_1_1_1_2_LT05_189028_19910106``). The trailing three
    underscore-separated tokens (``LT05_189028_19910106``) are the original
    scene's ``system:index`` inside its own Landsat archive, i.e. the asset id.
    Tier (T1/T2) is not encoded, so both tiers are tried by the caller.
    """
    parts = image_id.split("_")
    scene = "_".join(parts[-3:])          # LT05_189028_19910106
    prefix = scene.split("_")[0]          # LT05
    family = _ASSET_FAMILY.get(prefix) or _SAT_FAMILY.get((sat or "").upper())
    if family is None:
        raise ValueError(f"Cannot map image_id {image_id!r} to a Landsat family")
    return family, scene


def load_scene(image_id, sat=None, tier=None, roi=None):
    """Load the exact harmonized ``ee.Image`` for a catalog row.

    Builds ``{family}/C02/T{tier}_L2/{scene}`` and renames the native bands to
    ``gee_common``'s common set, so every downstream helper (true_color,
    cloud_bits, lst_celsius, ndbi, ndvi, eci) works unchanged across L4-L9.
    If ``tier`` is unknown, T1 is tried then T2. Clipped to ``roi`` when given.
    """
    family, scene = _asset_id(image_id, sat)
    sat_code = sat or {
        "LANDSAT/LT04": "L4", "LANDSAT/LT05": "L5", "LANDSAT/LE07": "L7",
        "LANDSAT/LC08": "L8", "LANDSAT/LC09": "L9",
    }[family]
    native = gc._NATIVE_BANDS[sat_code]

    tiers = [tier] if tier in ("T1", "T2") else ["T1", "T2"]
    probe = len(tiers) > 1  # only pay the extra getInfo when we must guess a tier
    last_err = None
    for t in tiers:
        asset = f"{family}/C02/{t}_L2/{scene}"
        img = ee.Image(asset)
        if probe:
            try:
                # Force resolution so a wrong tier fails fast and we try the next.
                img.bandNames().getInfo()
            except Exception as e:  # noqa: BLE001 - EE raises generic errors
                last_err = e
                continue
        harmonized = (
            img.select(native, gc._COMMON_BANDS)
            .set("SAT", sat_code)
            .set("TIER", t)
            .copyProperties(img, img.propertyNames())
        )
        harmonized = ee.Image(harmonized)
        return harmonized.clip(roi) if roi is not None else harmonized

    raise ValueError(
        f"Scene {scene!r} not found in {family}/C02/T1|T2_L2: {last_err}"
    )


# --------------------------------------------------------------------------
# Indices missing from gee_common (ported verbatim from eci_lst_scirpt.js)
# --------------------------------------------------------------------------
def ndvi(image):
    """NDVI = (NIR - red) / (NIR + red) on SR-scaled bands.

    Same formula as ``eci_lst_scirpt.js`` (``nir.subtract(red).divide(...)``),
    using the harmonized ``red``/``nir`` bands so it is valid for every
    satellite (L4-L9).
    """
    red = image.select("red").multiply(gc.SR_SCALE).add(gc.SR_OFFSET)
    nir = image.select("nir").multiply(gc.SR_SCALE).add(gc.SR_OFFSET)
    return nir.subtract(red).divide(nir.add(red)).rename("NDVI")


def _normalize(band_image, roi, scale=30):
    """Min-max normalize a single-band image over ``roi`` -> ~[0, 1].

    Direct port of the JS ``normalize()``: reduce min & max over the ROI, then
    ``(x - min) / (max - min)``. Server-side, so ECI stays a lazy ee.Image.
    """
    name = band_image.bandNames().get(0)
    stats = band_image.reduceRegion(
        reducer=ee.Reducer.minMax(),
        geometry=roi,
        scale=scale,
        maxPixels=1e13,
        bestEffort=True,
    )
    mn = ee.Number(stats.get(ee.String(name).cat("_min")))
    mx = ee.Number(stats.get(ee.String(name).cat("_max")))
    # Guard against a constant band (max == min) over the ROI — e.g. a fully
    # cloud-masked scene — which would otherwise divide by zero and yield an
    # all-invalid layer. The floor makes the numerator (also 0) normalize to 0.
    denom = mx.subtract(mn).max(1e-6)
    return band_image.subtract(mn).divide(denom)


def eci(image, roi, scale=30):
    """Environmental Criticality Index over ``roi``.

    Port of ``eci_lst_scirpt.js``: ECI = (norm(LST) + norm(NDBI) - norm(NDVI))/3,
    each term min-max normalized within this scene's ROI. Reuses
    ``gc.lst_celsius`` and ``gc.ndbi`` verbatim.

    NOTE: normalization is per-scene, so ECI values are only comparable *within*
    a single scene, exactly like the original JS.
    """
    lst_n = _normalize(gc.lst_celsius(image), roi, scale)
    ndbi_n = _normalize(gc.ndbi(image), roi, scale)
    ndvi_n = _normalize(ndvi(image), roi, scale)
    return lst_n.add(ndbi_n).subtract(ndvi_n).divide(3).rename("ECI")


# --------------------------------------------------------------------------
# Tile layers for the folium/streamlit map
# --------------------------------------------------------------------------
def tile_layer_url(ee_image, vis_params):
    """Return a live GEE XYZ tile-URL template for a visualized image.

    Same mechanism as ``visualize_date.py::_ee_tile_layer`` — ``getMapId`` on
    the server, then hand the URL template to folium. Tiles compute lazily per
    zoom, so first paint of a date takes a few seconds.
    """
    map_id = ee.Image(ee_image).getMapId(vis_params)
    return map_id["tile_fetcher"].url_format


# Visualization presets, matched to eci_lst_scirpt.js / visualize_date.py.
VIS = {
    "true_color": {"min": 0.0, "max": 0.3, "bands": ["red", "green", "blue"]},
    "cloud": {"palette": ["red"]},
    "ndvi": {"min": -0.2, "max": 0.8,
             "palette": ["#a50026", "#ffffbf", "#006837"]},
    "lst": {"min": 15, "max": 45,
            "palette": ["blue", "cyan", "green", "yellow", "orange", "red"]},
    "eci": {"min": 0, "max": 1,
            "palette": ["green", "yellow", "orange", "red", "darkred"]},
}
