import calendar
import math

import streamlit as st

from config import PAGE_SIZE


# --------------------------------------------------------------------------
# Sidebar — city, filters, date list
# --------------------------------------------------------------------------
def _step_page(delta, n_pages):
    """Prev/next page button callback — keeps scene_page and page_input in sync."""
    p = max(0, min(st.session_state.get("scene_page", 0) + delta, n_pages - 1))
    st.session_state["scene_page"] = p
    st.session_state["page_input"] = p + 1


def _page_input_changed():
    """'Go to page' number-input callback — drives scene_page from the widget."""
    st.session_state["scene_page"] = int(st.session_state["page_input"]) - 1


def _sync_cloud_from_slider():
    """Cloud slider callback — mirror its value into the typed-input box."""
    st.session_state["cloud_filter_num"] = st.session_state["cloud_filter"]


def _sync_cloud_from_number():
    """Cloud number-input callback — move the slider to the typed value."""
    value = st.session_state["cloud_filter_num"]
    if value is not None:
        st.session_state["cloud_filter"] = int(value)


def _sync_year_from_slider():
    """Year-range slider callback — mirror both ends into the typed inputs."""
    lo, hi = st.session_state["year_filter"]
    st.session_state["year_from_num"] = lo
    st.session_state["year_to_num"] = hi


def _sync_year_from_number(edited):
    """Typed-year callback ("from" or "to") — write the slider tuple.

    Keeps from <= to by dragging the *other* end along: a "from" typed past "to"
    pulls "to" up to match, and a "to" typed below "from" pushes "from" down.
    """
    lo = st.session_state["year_from_num"]
    hi = st.session_state["year_to_num"]
    if lo is None or hi is None:
        return
    lo, hi = int(lo), int(hi)
    if edited == "from":
        hi = max(hi, lo)
    else:
        lo = min(lo, hi)
    st.session_state["year_filter"] = (lo, hi)
    st.session_state["year_from_num"] = lo
    st.session_state["year_to_num"] = hi


def _date_from_changed():
    """'From' date callback — 'Until' can never end up before it."""
    if st.session_state["date_until"] < st.session_state["date_from"]:
        st.session_state["date_until"] = st.session_state["date_from"]


def _clamp(value, lo, hi):
    return max(lo, min(value, hi))


def _init_filter_state(date_min, date_max, years):
    """Seed/repair the synced filter widgets' session keys before they're built.

    Widgets with a pre-set key must not also receive ``value=`` (Streamlit warns),
    so defaults live here. Stored values are also clamped into the *current*
    catalog's bounds, since a city switch changes the date span and year range
    and a stored value outside ``min_value``/``max_value`` makes Streamlit raise.
    The sliders are the source of truth; the typed boxes just mirror them.
    """
    ss = st.session_state

    cloud = ss.get("cloud_filter")
    cloud = 100 if cloud is None else _clamp(int(cloud), 0, 100)
    ss["cloud_filter"] = cloud
    ss["cloud_filter_num"] = cloud

    lo, hi = ss.get("year_filter") or (years[0], years[-1])
    lo = _clamp(int(lo), years[0], years[-1])
    hi = _clamp(int(hi), lo, years[-1])
    ss["year_filter"] = (lo, hi)
    ss["year_from_num"] = lo
    ss["year_to_num"] = hi

    d_from = ss.get("date_from") or date_min
    d_from = _clamp(d_from, date_min, date_max)
    d_until = ss.get("date_until") or date_max
    d_until = _clamp(d_until, d_from, date_max)
    ss["date_from"] = d_from
    ss["date_until"] = d_until


def render_sidebar(df, labeled):
    st.sidebar.header("Catalog")
    st.sidebar.write(f"Labeler: **{st.session_state['labeler']}**")
    if st.sidebar.button("Log out"):
        loader = st.session_state.pop("_scene_loader", None)
        if loader is not None:
            loader.close()
        for k in ("authed", "labeler", "scene_radio", "pending_scene",
                  "scene_page", "date_from", "date_until", "_map_preferences",
                  "_map_last_event", "_active_map_scene", "_persistent_scene_map",
                  "_map_neighbors", "_cloud_stats_request"):
            st.session_state.pop(k, None)
        st.rerun()

    # Filters. Every filter is ordinary and combines with the others, including
    # the date range.
    date_min, date_max = df["date"].min(), df["date"].max()
    years = sorted(int(y) for y in df["year"].unique())
    _init_filter_state(date_min, date_max, years)

    # Span the whole catalog: st.date_input otherwise caps the year picker at
    # ~10 years back, hiding the pre-2016 scenes. "Until" can't precede "From"
    # (min_value), and _date_from_changed bumps it when "From" jumps past it.
    dcols = st.sidebar.columns(2)
    dcols[0].date_input(
        "From", min_value=date_min, max_value=date_max,
        key="date_from", on_change=_date_from_changed)
    dcols[1].date_input(
        "Until", min_value=st.session_state["date_from"], max_value=date_max,
        key="date_until")

    # Each slider pairs with a typed box; callbacks keep the two in sync.
    st.sidebar.slider(
        "Max ROI cloud %", 0, 100, step=1, key="cloud_filter",
        on_change=_sync_cloud_from_slider)
    st.sidebar.number_input(
        "Max ROI cloud % (type a value)", min_value=0, max_value=100, step=1,
        key="cloud_filter_num", on_change=_sync_cloud_from_number,
        label_visibility="collapsed")
    max_cloud = st.session_state["cloud_filter"]

    sats = sorted(df["sat"].dropna().unique())
    sat_sel = st.sidebar.multiselect(
        "Satellite", sats, default=sats, key="sat_filter")

    st.sidebar.slider(
        "Year range", min_value=years[0], max_value=years[-1],
        key="year_filter", on_change=_sync_year_from_slider)
    ycols = st.sidebar.columns(2)
    ycols[0].number_input(
        "From year", min_value=years[0], max_value=years[-1], step=1,
        key="year_from_num", on_change=_sync_year_from_number, args=("from",))
    ycols[1].number_input(
        "To year", min_value=years[0], max_value=years[-1], step=1,
        key="year_to_num", on_change=_sync_year_from_number, args=("to",))
    yr = st.session_state["year_filter"]

    months = list(range(1, 13))
    mo_sel = st.sidebar.multiselect(
        "Months", months, default=months,
        format_func=lambda m: calendar.month_abbr[m], key="month_filter")
    hide_labeled = st.sidebar.checkbox(
        "Hide already-labeled", value=False, key="hide_labeled")

    # NaN roi_cloud_pct (scenes with no valid pixels) must stay visible, not
    # be silently dropped by the `<=` comparison (NaN <= x is always False).
    cloud_ok = df["roi_cloud_pct"].isna() | (df["roi_cloud_pct"] <= max_cloud)
    d_from, d_until = st.session_state["date_from"], st.session_state["date_until"]
    view = df[
        cloud_ok
        & (df["sat"].isin(sat_sel))
        & (df["year"] >= yr[0])
        & (df["year"] <= yr[1])
        & (df["month"].isin(mo_sel))
        & (df["date"] >= d_from)
        & (df["date"] <= d_until)
    ].copy()
    if hide_labeled:
        view = view[~view["image_id"].isin(labeled)]

    st.sidebar.caption(
        f"{len(view)} of {len(df)} scenes between {d_from} and {d_until} · "
        f"{len(labeled)} labeled")

    # Build compact human-readable labels for the radio list.
    def _row_label(r):
        mark = "✓ " if r["image_id"] in labeled else ""
        return (f"{mark}{r['date']} · {r['sat']} · "
                f"scene {r['scene_cloud_cover']:.0f}% · roi {r['roi_cloud_pct']:.0f}%")

    view = view.sort_values("date").reset_index(drop=True)
    all_ids = view["image_id"].tolist()
    if not all_ids:
        st.sidebar.info("No scenes match the filters.")
        return view, None

    # A programmatic jump (auto-advance) cannot write the radio's own key after
    # the widget is instantiated, so it stashes its target in "pending_scene"
    # (and the matching page in "scene_page"); apply it here, before the widget.
    pending = st.session_state.pop("pending_scene", None)
    if pending in all_ids:
        st.session_state["scene_radio"] = pending

    # Pagination: only PAGE_SIZE options reach the radio, instead of every
    # filtered row (thousands of options was the main render cost). "scene_page"
    # is the single source of truth (0-indexed); auto-advance and date jumps set
    # it directly. All manual page changes go through callbacks that keep it and
    # the "page_input" number widget in sync, so the two never fight.
    n_pages = max(1, math.ceil(len(all_ids) / PAGE_SIZE))
    page = min(max(0, st.session_state.get("scene_page", 0)), n_pages - 1)
    st.session_state["scene_page"] = page
    # Mirror an external page change (auto-advance/date jump) into the widget key
    # before the widget is built; harmless no-op after manual changes.
    if st.session_state.get("page_input") != page + 1:
        st.session_state["page_input"] = page + 1

    pcols = st.sidebar.columns([1, 2, 1])
    pcols[0].button("◀", disabled=(page == 0), key="prev_page",
                    use_container_width=True, on_click=_step_page,
                    args=(-1, n_pages))
    pcols[2].button("▶", disabled=(page >= n_pages - 1), key="next_page",
                    use_container_width=True, on_click=_step_page,
                    args=(1, n_pages))
    pcols[1].markdown(
        f"<div style='text-align:center;padding-top:6px'>"
        f"Page {page + 1} of {n_pages}</div>",
        unsafe_allow_html=True)
    if n_pages > 1:
        st.sidebar.number_input(
            "Go to page", min_value=1, max_value=n_pages,
            key="page_input", on_change=_page_input_changed)
    page = min(max(0, st.session_state["scene_page"]), n_pages - 1)
    st.session_state["scene_page"] = page

    page_rows = view.iloc[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    page_ids = page_rows["image_id"].tolist()
    labels_map = {r["image_id"]: _row_label(r) for _, r in page_rows.iterrows()}

    # Snap the selection back onto the visible page whenever it points elsewhere
    # (city/filter/page change) — otherwise Streamlit raises because the stored
    # value isn't among the radio's options.
    if st.session_state.get("scene_radio") not in page_ids:
        st.session_state["scene_radio"] = page_ids[0]

    chosen = st.sidebar.radio(
        "Scene", page_ids,
        format_func=lambda i: labels_map[i], key="scene_radio",
    )
    return view, chosen
