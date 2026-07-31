"""Shared Google Earth Engine helpers for the UTOMIKA cloud-cover work.

Everything here is intentionally lightweight: we only read cloud information
(QA_PIXEL + scene metadata). No ECI/LST is computed, and no rasters are
exported, so GEE compute usage stays minimal.

Satellites merged to cover 1991-2026:
  - LANDSAT/LT04/C02/T1_L2   (Landsat 4 TM,   1982-1993, intermittent)
  - LANDSAT/LT05/C02/T1_L2   (Landsat 5 TM,   ~1984-2013)
  - LANDSAT/LE07/C02/T1_L2   (Landsat 7 ETM+, 1999-present; SLC-off after 2003)
  - LANDSAT/LC08/C02/T1_L2   (Landsat 8 OLI,  2013-present)
  - LANDSAT/LC09/C02/T1_L2   (Landsat 9,      2021-present)

All are Collection 2 Level-2. Their QA_PIXEL bit layout is identical, which is
why a single cloud mask works across every satellite.
"""

import os

import ee

# NOTE: geopandas / shapely are imported lazily inside load_roi() so this module
# stays importable on a lightweight deploy (Streamlit Cloud) that ships only the
# pre-exported GeoJSON ROIs and never touches the shapefiles. See load_roi_geojson
# in gee_app.py for the runtime path.

# --------------------------------------------------------------------------
# Paths / config
# --------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# Single-feature city boundary shapefiles (EPSG:3765, HTRS96 / Croatia TM).
CITY_SHP = {
    "zagreb": os.path.join(
        _ROOT, "data", "Podaci", "Granice_gradova",
        "Zagreb_rpj_grad_jls", "RPJ_GRAD_JLS.shp",
    ),
    "split": os.path.join(
        _ROOT, "data", "Podaci", "Granice_gradova",
        "Split-Arian-extracted", "Split.shp",
    ),
}

SOURCE_CRS = 3765   # HTRS96 / Croatia TM (shapefile CRS)
GEE_CRS = 4326      # WGS84, expected by ee.Geometry

# Landsat Collection-2 Level-2 archives, tagged (id, satellite, tier).
# BOTH tiers are included so no overpass is missed: Tier 2 holds scenes that
# failed Tier 1's geometric/radiometric criteria, which disproportionately are
# cloudy scenes -- exactly the dates a cloud study must not drop. The `tier`
# column lets quality-sensitive work filter to T1 later if desired.
_LANDSAT_SOURCES = [
    ("LANDSAT/LT04/C02/T1_L2", "L4", "T1"),
    ("LANDSAT/LT04/C02/T2_L2", "L4", "T2"),
    ("LANDSAT/LT05/C02/T1_L2", "L5", "T1"),
    ("LANDSAT/LT05/C02/T2_L2", "L5", "T2"),
    ("LANDSAT/LE07/C02/T1_L2", "L7", "T1"),
    ("LANDSAT/LE07/C02/T2_L2", "L7", "T2"),
    ("LANDSAT/LC08/C02/T1_L2", "L8", "T1"),
    ("LANDSAT/LC08/C02/T2_L2", "L8", "T2"),
    ("LANDSAT/LC09/C02/T1_L2", "L9", "T1"),
    ("LANDSAT/LC09/C02/T2_L2", "L9", "T2"),
]

# Harmonized band names exposed to callers, regardless of satellite. The
# ``thermal`` band is the Collection-2 Surface-Temperature band (ST_B*), used by
# lst_celsius(); it is ignored by the cloud tools, which select bands by name.
_COMMON_BANDS = ["blue", "green", "red", "nir", "swir1", "swir2", "QA_PIXEL",
                 "thermal"]

# Native SR band order differs between TM/ETM+ (L4/L5/L7) and OLI/TIRS (L8/L9);
# the thermal band is ST_B6 on TM/ETM+ and ST_B10 on OLI/TIRS.
_NATIVE_BANDS = {
    "L4": ["SR_B1", "SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B7", "QA_PIXEL", "ST_B6"],
    "L5": ["SR_B1", "SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B7", "QA_PIXEL", "ST_B6"],
    "L7": ["SR_B1", "SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B7", "QA_PIXEL", "ST_B6"],
    "L8": ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7", "QA_PIXEL", "ST_B10"],
    "L9": ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7", "QA_PIXEL", "ST_B10"],
}

# QA_PIXEL cloud bits (Collection 2). Matches eci_lst_scirpt.js: cloud + shadow.
_CLOUD_BIT = 1 << 3
_SHADOW_BIT = 1 << 4

# Surface-reflectance scaling for Collection 2 Level-2 (DN -> reflectance).
SR_SCALE = 0.0000275
SR_OFFSET = -0.2

# Surface-temperature scaling for Collection 2 Level-2 (DN -> Kelvin).
ST_SCALE = 0.00341802
ST_OFFSET = 149.0


# --------------------------------------------------------------------------
# Initialization
# --------------------------------------------------------------------------
def init_ee(project="utomika"):
    """Initialize the Earth Engine client (run `earthengine authenticate` once)."""
    ee.Initialize(project=project)


# --------------------------------------------------------------------------
# Region of interest
# --------------------------------------------------------------------------
def load_roi(city, simplify_m=50):
    """Load a city boundary shapefile and return it as an ``ee.Geometry`` (WGS84).

    The polygon is dissolved to a single geometry, simplified in the source
    (metre) CRS to cut vertex count, then reprojected to EPSG:4326.
    """
    import geopandas as gpd
    from shapely.geometry import mapping

    city = city.lower()
    if city not in CITY_SHP:
        raise ValueError(f"Unknown city {city!r}; expected one of {list(CITY_SHP)}")

    gdf = gpd.read_file(CITY_SHP[city])
    if gdf.crs is None:
        gdf = gdf.set_crs(SOURCE_CRS)

    dissolved = gdf.geometry.union_all()
    simplified = (
        gpd.GeoSeries([dissolved], crs=gdf.crs)
        .simplify(simplify_m, preserve_topology=True)
        .to_crs(GEE_CRS)
        .iloc[0]
    )
    return ee.Geometry(mapping(simplified))


# --------------------------------------------------------------------------
# Collection building
# --------------------------------------------------------------------------
def _harmonize(collection_id, sat, tier, roi, start, end):
    """Filter one Landsat archive and rename its bands to the common set."""
    native = _NATIVE_BANDS[sat]

    def _prep(img):
        return (
            img.select(native, _COMMON_BANDS)
            .set("SAT", sat)
            .set("TIER", tier)
            .copyProperties(img, img.propertyNames())
        )

    return (
        ee.ImageCollection(collection_id)
        .filterBounds(roi)
        .filterDate(start, end)
        .map(_prep)
    )


def harmonized_collection(roi, start, end):
    """Merged, band-harmonized Landsat 5/7/8/9 collection over ``roi``.

    ``start`` / ``end`` are anything ``ee.Date`` accepts (e.g. 'YYYY-MM-DD').
    Empty per-satellite collections merge harmlessly, so any date range works.
    """
    merged = None
    for cid, sat, tier in _LANDSAT_SOURCES:
        coll = _harmonize(cid, sat, tier, roi, start, end)
        merged = coll if merged is None else merged.merge(coll)
    return merged.sort("system:time_start")


# --------------------------------------------------------------------------
# Cloud logic
# --------------------------------------------------------------------------
def cloud_bits(qa):
    """1 where cloud or cloud-shadow, 0 elsewhere (keeps the input mask)."""
    cloud = qa.bitwiseAnd(_CLOUD_BIT).neq(0)
    shadow = qa.bitwiseAnd(_SHADOW_BIT).neq(0)
    return cloud.Or(shadow).rename("cloud")


def roi_cloud_stats(image, roi, scale=100):
    """Return an ``ee.Dictionary`` with ROI cloud statistics for one image.

    Keys:
      * ``valid`` -> fraction of the ROI covered by valid (non-gap) pixels.
      * ``cloud`` -> fraction of the *whole* ROI flagged as cloud/shadow.

    Divide ``cloud`` by ``valid`` to get cloud fraction among valid pixels
    (done client-side to avoid divide-by-zero on empty scenes).

    ``scale`` in metres; 100 m keeps the pixel count tiny and cheap.
    """
    qa = image.select("QA_PIXEL")
    valid = qa.mask().rename("valid").unmask(0)
    cloud = cloud_bits(qa).unmask(0)
    stack = valid.addBands(cloud)
    return stack.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=roi,
        scale=scale,
        maxPixels=1e9,
        bestEffort=True,
    )


def true_color(image):
    """Scaled true-color (red, green, blue) reflectance image for display."""
    return (
        image.select(["red", "green", "blue"])
        .multiply(SR_SCALE)
        .add(SR_OFFSET)
    )


def ndbi(image):
    """Normalized Difference Built-up Index: (SWIR1 - NIR) / (SWIR1 + NIR).

    High over built-up / bare / sealed surfaces, low over vegetation and water.
    Uses the harmonized ``swir1``/``nir`` bands, SR-scaled to reflectance so the
    same formula is valid across every satellite (L4-L9).
    """
    swir1 = image.select("swir1").multiply(SR_SCALE).add(SR_OFFSET)
    nir = image.select("nir").multiply(SR_SCALE).add(SR_OFFSET)
    return swir1.subtract(nir).divide(swir1.add(nir)).rename("NDBI")


def summer_composite(roi, year, months=(6, 8)):
    """Cloud-masked **median** composite of one summer over ``roi``.

    Returns ``(image, n_scenes)`` where ``image`` is the harmonized median (still
    carrying every ``_COMMON_BANDS`` band, so callers can derive NDBI/true color)
    clipped to ``roi``, and ``n_scenes`` is how many overpasses contributed.

    Compositing the whole Jun-Aug window (rather than one overpass) normalizes
    season and fills cloud/gap noise -- including Landsat-7 SLC-off striping --
    which is what makes a two-year change comparison reliable.
    """
    m0, m1 = months
    start = f"{year:04d}-{m0:02d}-01"
    # Advance one month past m1 and take the day before, so m1 is included whole.
    end = ee.Date(f"{year:04d}-{m1:02d}-01").advance(1, "month")
    coll = harmonized_collection(roi, start, end)

    def _mask_clouds(img):
        clear = cloud_bits(img.select("QA_PIXEL")).Not()
        return img.updateMask(clear)

    masked = coll.map(_mask_clouds)
    n_scenes = coll.size().getInfo()
    return masked.median().clip(roi), n_scenes


def lst_celsius(image):
    """Land-surface temperature (deg C) from the thermal band + NDVI emissivity.

    Mirrors the repo's canonical method in ``eci_lst_scirpt.js``: brightness
    temperature from the Surface-Temperature band, an NDVI-derived proportion of
    vegetation (Pv), a broadband emissivity, and the single-channel Planck
    inversion. Reuses the already-harmonized ``red``/``nir``/``thermal`` bands so
    the same formula works for every satellite (L4-L9).

    Note: the Collection-2 ``ST_B*`` band is already a USGS surface-temperature
    product; re-applying the NDVI emissivity here keeps us consistent with the
    existing ``eci_lst_scirpt.js`` methodology rather than aiming for a new one.
    """
    tb = image.select("thermal").multiply(ST_SCALE).add(ST_OFFSET)  # Kelvin
    red = image.select("red").multiply(SR_SCALE).add(SR_OFFSET)
    nir = image.select("nir").multiply(SR_SCALE).add(SR_OFFSET)

    ndvi = nir.subtract(red).divide(nir.add(red))
    pv = ndvi.subtract(0.2).divide(0.5 - 0.2).clamp(0, 1).pow(2)
    emissivity = pv.multiply(0.004).add(0.986)

    return tb.expression(
        "(TB / (1 + ((0.00115 * TB) / 1.4388) * log(E))) - 273.15",
        {"TB": tb, "E": emissivity},
    ).rename("LST")
