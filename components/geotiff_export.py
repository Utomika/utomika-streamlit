import streamlit as st

import resources


# --------------------------------------------------------------------------
# GeoTIFF download of the selected scene
# --------------------------------------------------------------------------
# Display name -> layer key understood by resources.scene_geotiff. Names match
# the map's layer menu (scene_loader.LAYERS).
_LAYERS = {
    "true_color": "True color",
    "cloud": "Cloud / shadow mask",
    "ndvi": "NDVI",
    "lst": "LST (°C)",
    "eci": "ECI",
}
_KINDS = {"raw": "Raw values", "rgb": "Colored (as on map)"}
_PREPARED_KEY = "_geotiff_prepared"


@st.fragment
def render_geotiff_download(city, row):
    """Prepare-then-download a GeoTIFF of one layer of the selected scene.

    Building the file means an Earth Engine export round-trip, so it only runs
    when "Prepare GeoTIFF" is clicked. The prepared bytes live in session_state
    (tagged with the scene/layer/kind they belong to) so the download button
    survives the fragment rerun that clicking it triggers, and it only shows
    while the current selection still matches what was prepared.
    """
    image_id = row["image_id"]
    with st.expander("Download GeoTIFF"):
        layer = st.selectbox(
            "Layer", list(_LAYERS), format_func=_LAYERS.get, key="geotiff_layer")
        kind = st.radio(
            "Values", list(_KINDS), format_func=_KINDS.get, key="geotiff_kind")
        st.caption(
            "30 m, EPSG:32633 (UTM 33N), clipped to the city boundary. Raw "
            "values use nodata -9999 (cloud mask: 0 clear, 1 cloud/shadow, "
            "nodata 255). Colored exports are 8-bit RGB as painted on the map.")

        selection = (city, image_id, layer, kind)
        if st.button("Prepare GeoTIFF", key="geotiff_prepare"):
            try:
                with st.spinner("Preparing GeoTIFF from Earth Engine…"):
                    data = resources.scene_geotiff(
                        city, image_id, row["sat"], row["tier"], layer, kind)
            except Exception as e:  # noqa: BLE001 - EE/network errors vary
                st.error(f"Could not prepare the GeoTIFF: {type(e).__name__}: {e}")
            else:
                st.session_state[_PREPARED_KEY] = {
                    "selection": selection, "data": data}

        prepared = st.session_state.get(_PREPARED_KEY)
        if prepared is not None and prepared["selection"] == selection:
            data = prepared["data"]
            st.download_button(
                f"Download .tif ({len(data) / 1e6:.1f} MB)",
                data=data,
                file_name=f"utomika_{city}_{row['date']}_{layer}_{kind}.tif",
                mime="image/tiff",
                key="geotiff_download",
            )
