# UTOMIKA cloud-labeling app

A small Streamlit tool for a geodesy researcher to review Landsat overpasses of
**Zagreb** and **Split** and label each **clear / cloudy / unsure**, building a
human-verified cloud catalog on top of the automatic metadata. Labels are stored
in Supabase (free hosted Postgres) so they survive redeploys and export to CSV.

It reuses the parent repo's proven Earth Engine code (`gee_common.py`) and the
already-computed cloud catalogs (`data/cloud_cover_{city}.csv`, ~2.4k rows
each).

## What it does

- **Login gate** — one shared app password; each labeler types their name, which
  is stored with every label.
- **Sidebar** — pick a city, then filter by date range (From / Until), ROI
  cloud %, satellite, year range, month, and "hide already-labeled"; all filters
  combine, and the scene list below shows the matching dates, paginated. The
  cloud % and year-range sliders each have typed inputs next to them, so you can
  enter an exact value (e.g. 37 %, or 1998 to 2004) instead of dragging. A `✓`
  marks scenes you've labeled.
- **Main panel** — an interactive map of the selected overpass with toggleable
  **True color**, **Cloud/shadow mask**, **NDVI**, **LST (°C)**, and **ECI**
  layers (live GEE tiles), plus the ROI cloud numbers for cross-checking. Layers
  prepare in background workers without resetting the map. Your last selected
  layer loads first when changing dates; zoom, position, cloud opacity, and layer
  visibility persist. Recent layers are cached for 30 minutes, and visible layers
  for the two adjacent filtered scenes are prepared at low priority. Failed
  layers can be retried individually from the map menu.
- **Historical station weather** — a compact strip above the scene metrics and
  map with daily mean air temperature, precipitation, and wind observations at
  07:00, 14:00, and 21:00. Choose 7, 14, 21, 28, or 35 days before the selected
  scene; arrow controls scroll that window into the past without rebuilding the
  map.
- **Label buttons** — Clear / Cloudy / Unsure (+ optional note) → upsert to
  Supabase and auto-advance to the next unlabeled scene.
- **GeoTIFF export** — under the ROI cloud numbers, "Download GeoTIFF" prepares
  any layer (true color, cloud mask, NDVI, LST, ECI) of the selected scene as
  either **raw values** (nodata -9999; cloud mask 0/1 with nodata 255) or a
  **colored** 8-bit RGB image as painted on the map. Files are 30 m, EPSG:32633
  (UTM 33N), clipped to the city boundary. The map's camera button still saves a
  PNG of the current view.
- **Export** — pull the whole `labels` table as CSV from the sidebar.

## Layout

```
app.py               Streamlit UI (login, sidebar list, map, label buttons)
components/          UI pieces: login, sidebar filters, map, labeling, weather,
                     GeoTIFF + CSV export
scene_loader.py      Bounded background requests and per-session scene cache
weather.py           Cached station data, windowing, and Altair chart
gee_app.py           EE glue: service-account auth, GeoJSON ROI, exact-scene
                     loader, NDVI/ECI, tile-layer URLs
gee_common.py        COPIED from ../scripts (geopandas imports made lazy)
db.py                Supabase: get_labeled_ids / upsert_label / fetch_labels
export_rois.py       one-off: shapefile -> data/{city}.geojson
supabase_schema.sql  the labels table
data/
  cloud_cover_zagreb.csv, cloud_cover_split.csv   catalogs (copied)
  sve_postaje_temperatura_oborine.csv             daily station weather
  sve_postaje_vjetar_tlo.csv                      wind at 07/14/21
  zagreb.geojson, split.geojson                   ROIs (pre-exported)
assets/
  utomika_icon.png                                favicon
  utomika_logo.jpg                                full logo (login + sidebar)
requirements.txt
.streamlit/secrets.toml.example                   copy to secrets.toml
```

## Prerequisites (need your accounts — cannot be scripted)

1. **Supabase:** create a free project → SQL Editor → run `supabase_schema.sql`
   → copy the project **URL** and the **service_role** key
   (Project Settings → API).
2. **GEE service account:** in the Google Cloud project backing Earth Engine
   (`utomika`), create a service account, register it for Earth Engine, and
   download its JSON key. The `utomika` EE project is already used by the parent
   repo's scripts, so it should already be registered.
3. Put both, plus a chosen `APP_PASSWORD`, into secrets (below).

## Secrets

Copy the template and fill it in:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Keys: `APP_PASSWORD`, `SUPABASE_URL`, `SUPABASE_KEY` (service_role),
`GEE_PROJECT` (default `utomika`), and the GEE service account.

**GEE service account — use the `[gee_service_account]` table form** (one field
per key), not a single pasted JSON blob. The table form avoids the common
`json.decoder.JSONDecodeError: Invalid control character` you get when the
`private_key`'s newlines aren't escaped. Keep `private_key` on **one line** with
`\n` written literally, exactly as in the downloaded key file. See
`secrets.toml.example`. (A single `GEE_SERVICE_ACCOUNT_JSON` string is still
accepted as a fallback.) Locally you may omit the service account entirely and
rely on your own `earthengine authenticate` session.

**Supabase URL** must be your exact project URL (Project Settings → API →
Project URL, e.g. `https://abcdefghij.supabase.co`). A placeholder or typo
surfaces as `httpx.ConnectError: nodename nor servname provided`.

`.streamlit/secrets.toml` is git-ignored — never commit real credentials.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then: log in → pick a city → the sidebar shows ~2.4k dated rows → select a date
→ the map paints true color first, then adds cloud mask, NDVI, LST, and ECI to
the layer menu one by one → click a label → it saves to Supabase and marks the
row `✓`.

## Regenerating the ROIs

The two GeoJSONs are already committed. To rebuild them from the source
shapefiles (needs geopandas, e.g. the repo's `utomika_env`):

```bash
python export_rois.py            # reads ../data/Podaci/... shapefiles
```

They use the identical dissolve + 50 m simplify + WGS84 logic as
`gee_common.load_roi`, so the labeled ROI matches the catalog's.

## Deploy (Streamlit Community Cloud, free)

1. Push this folder to a GitHub repo.
2. On https://share.streamlit.io connect the repo, set the main file to
   `app.py`.
3. Paste the same secrets into the app's **Secrets** panel.
4. Deploy and smoke-test end-to-end. (Hugging Face Spaces is an equivalent free
   fallback.)

## Notes

- **Exact scene, not nearest date:** a catalog `image_id` (a merged
  `system:index`, e.g. `..._LT05_189028_19910106`) is resolved directly to its
  Landsat asset (`LANDSAT/LT05/C02/T1_L2/LT05_189028_19910106`) and band-
  harmonized, so the map shows exactly the scene the catalog row describes.
- **ECI normalization** is min–max over the ROI **per scene** (same as the
  original `eci_lst_scirpt.js`), so ECI values are only comparable *within* a
  scene — fine for visual review.
- **Map framework:** a keyed Streamlit component with Leaflet 1.9.4 over GEE
  `getMapId` tile URLs (no folium/geemap/geopandas or frontend build step).
  Layer updates leave unchanged tile elements in place. Leaflet and the lazy PNG
  capture library load from jsDelivr. Scene URL/statistics caches are bounded to
  80 results per session, with at most four running requests; obsolete queued
  requests are replaced when navigating. Already running requests may finish,
  and their results remain associated with their original scene.
- **Weather coverage:** the bundled station observations run from 1991-01-01
  through 2024-12-31. Split uses Marjan; Zagreb defaults to Grič and can switch
  to Maksimir or Pleso. The history selector defaults to seven days before the
  scene and includes the selected scene date itself. The red line is daily mean
  air temperature (`SredT`), while daily minimum and maximum remain available
  in its tooltip. Temperature and 24-hour precipitation records carry the
  source's 21:00 timestamp; they are not instantaneous 21:00 air-temperature
  readings. Missing precipitation stays missing and is never converted to zero.
- **Map updates:** short fragment polls collect completed background work.
  The persistent map retains its view during polls and scene changes; changing
  cities recenters it. Live cloud statistics also load in the background. URL
  preparation and visible browser tile loading are reported separately.

## Verification

Run `python -m pytest -q` for the Python tests. The optional browser check,
`node tests/test_scene_map_browser.cjs`, requires Playwright and Chromium. It
uses synthetic PNG tiles to verify map persistence, layer selection across
dates, cloud opacity, retries, and PNG export without service credentials.
