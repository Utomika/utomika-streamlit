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
- **Sidebar** — pick a city, filter by ROI cloud %, satellite, year, and
  "hide already-labeled"; scroll the catalog. A `✓` marks scenes you've labeled.
- **Main panel** — an interactive map of the selected overpass with toggleable
  **True color**, **Cloud/shadow mask**, **NDVI**, **LST (°C)**, and **ECI**
  layers (live GEE tiles), plus the ROI cloud numbers for cross-checking.
- **Label buttons** — Clear / Cloudy / Unsure (+ optional note) → upsert to
  Supabase and auto-advance to the next unlabeled scene.
- **Export** — pull the whole `labels` table as CSV from the sidebar.

## Layout

```
app.py               Streamlit UI (login, sidebar list, map, label buttons)
gee_app.py           EE glue: service-account auth, GeoJSON ROI, exact-scene
                     loader, NDVI/ECI, tile-layer URLs
gee_common.py        COPIED from ../scripts (geopandas imports made lazy)
db.py                Supabase: get_labeled_ids / upsert_label / fetch_labels
export_rois.py       one-off: shapefile -> data/{city}.geojson
supabase_schema.sql  the labels table
data/
  cloud_cover_zagreb.csv, cloud_cover_split.csv   catalogs (copied)
  zagreb.geojson, split.geojson                   ROIs (pre-exported)
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
→ the map paints true-color + cloud mask (NDVI/LST/ECI on toggle) → click a
label → it saves to Supabase and marks the row `✓`.

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
- **Map framework:** plain `folium` + `streamlit-folium` over GEE `getMapId`
  tile URLs (no geemap/geopandas), keeping the Streamlit Cloud build light.
- **Rerun jank:** Streamlit reruns the whole script per interaction, so the map
  can feel slightly janky; acceptable for a review-one-date-at-a-time tool. If
  it becomes annoying, Solara is a drop-in upgrade (same `gee_common.py`, same
  Supabase `db.py`).
