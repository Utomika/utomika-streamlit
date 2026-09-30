# How NDVI, LST, and ECI are computed

All three are derived per Landsat scene (Collection‑2 Level‑2, L4–L9) over the
city ROI. Bands are first **harmonized** to a common set (`red`, `nir`, `swir1`,
`thermal`, …) so one formula works across every satellite. Surface‑reflectance
(SR) and surface‑temperature (ST) bands are rescaled from raw DN with the USGS
Collection‑2 factors:

| Quantity | Scale | Offset |
|---|---|---|
| Reflectance (SR bands) | `0.0000275` | `−0.2` |
| Temperature (ST band, → Kelvin) | `0.00341802` | `149.0` |

Source: `ndvi()`/`eci()` in `gee_app.py`, `lst_celsius()`/`ndbi()` in
`gee_common.py`, ported from `eci_lst_scirpt.js`.

## NDVI — Normalized Difference Vegetation Index
Greenness of vegetation, from red and near‑infrared reflectance:

```
NDVI = (NIR − Red) / (NIR + Red)
```

Range −1…1; high over healthy vegetation, low/negative over built‑up, bare soil,
and water.

## LST — Land Surface Temperature (°C)
Single‑channel method using the thermal band plus an NDVI‑derived emissivity:

1. **Brightness temperature** (Kelvin): `TB = thermal · 0.00341802 + 149.0`
2. **Proportion of vegetation:** `Pv = clamp((NDVI − 0.2) / (0.5 − 0.2), 0, 1)²`
3. **Emissivity:** `ε = 0.004 · Pv + 0.986`
4. **Planck inversion → °C:**

```
LST = TB / (1 + (0.00115 · TB / 1.4388) · ln(ε)) − 273.15
```

## NDBI — Normalized Difference Built‑up Index
Built‑up / sealed surfaces, an input to ECI:

```
NDBI = (SWIR1 − NIR) / (SWIR1 + NIR)
```

## ECI — Environmental Criticality Index
Combines heat, built‑up density, and (inverse) vegetation. Each input is
**min–max normalized over the ROI** for the scene:

```
norm(x) = (x − min_ROI(x)) / (max_ROI(x) − min_ROI(x))

ECI = ( norm(LST) + norm(NDBI) − norm(NDVI) ) / 3
```

Range ~0…1; higher = hotter, more built‑up, less vegetated (more
environmentally critical).

> **Caveat — comparability.** ECI normalization is per‑scene (min/max recomputed
> for each date's ROI), so ECI values are comparable *within* a scene but **not
> across dates**. This matches the original methodology; it is fine for visual
> review, but ECI should not be differenced between two dates without a fixed
> normalization range.
