"""One-off: export each city ROI to data/{city}.geojson.

The deployed app must not carry shapefiles or geopandas, so it reads the ROI
from a small GeoJSON instead. This script produces those GeoJSONs using the
*exact same* dissolve + 50 m simplify + reproject-to-WGS84 logic as
``gee_common.load_roi`` — so the ROI the app labels against is identical to the
one the catalog (extract_clouds.py) was computed with.

Run once, from the utomika-streamlit folder, in an env that has geopandas
(e.g. the repo's utomika_env):

    python export_rois.py

It reads the source shapefiles from the parent repo (.. by default, i.e. the
UTOMIKA repo root; override with --repo-root). The resulting data/zagreb.geojson
and data/split.geojson are the only geo files the app ships.
"""

import argparse
import json
import os

import geopandas as gpd
from shapely.geometry import mapping

SOURCE_CRS = 3765   # HTRS96 / Croatia TM (shapefile CRS)
GEE_CRS = 4326      # WGS84
SIMPLIFY_M = 50

# Shapefiles relative to the source repo root.
_CITY_SHP_REL = {
    "zagreb": os.path.join("data", "Podaci", "Granice_gradova",
                           "Zagreb_rpj_grad_jls", "RPJ_GRAD_JLS.shp"),
    "split": os.path.join("data", "Podaci", "Granice_gradova",
                          "Split-Arian-extracted", "Split.shp"),
}

_HERE = os.path.dirname(os.path.abspath(__file__))


def export(city, shp_path, out_dir):
    gdf = gpd.read_file(shp_path)
    if gdf.crs is None:
        gdf = gdf.set_crs(SOURCE_CRS)

    dissolved = gdf.geometry.union_all()
    simplified = (
        gpd.GeoSeries([dissolved], crs=gdf.crs)
        .simplify(SIMPLIFY_M, preserve_topology=True)
        .to_crs(GEE_CRS)
        .iloc[0]
    )
    fc = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"city": city},
            "geometry": mapping(simplified),
        }],
    }
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{city}.geojson")
    with open(out, "w") as f:
        json.dump(fc, f)
    print(f"{city}: {simplified.geom_type}  "
          f"bounds={[round(b, 4) for b in simplified.bounds]}  -> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-root", default=os.path.abspath(os.path.join(_HERE, "..")),
                    help="source repo root holding data/Podaci/... shapefiles "
                         "(default: one level up, i.e. the UTOMIKA repo root)")
    ap.add_argument("--out", default=os.path.join(_HERE, "data"),
                    help="output dir for the GeoJSONs (default: ./data)")
    args = ap.parse_args()

    for city, rel in _CITY_SHP_REL.items():
        shp = os.path.join(args.repo_root, rel)
        if not os.path.exists(shp):
            raise FileNotFoundError(
                f"Shapefile for {city} not found at {shp}. "
                f"Pass --repo-root pointing at the source repo.")
        export(city, shp, args.out)


if __name__ == "__main__":
    main()
