import calendar
import math

import streamlit as st

from config import PAGE_SIZE


# --------------------------------------------------------------------------
# Sidebar — city, filters, date list
# --------------------------------------------------------------------------
# Keys of the ordinary filter widgets. Popping these resets each widget to its
# default on the next run — used when a date search takes over the catalog.
_FILTER_KEYS = ("cloud_filter", "sat_filter", "year_filter",
                "month_filter", "hide_labeled")


@st.dialog("Apply date search?")
def _confirm_date_search(d):
    """Confirm that a date search should clear all other filters.

    On Apply we drop the ordinary filter widgets' state (so they snap back to
    defaults) and set a sticky "active_date"; _sidebar then shows only that
    day's scene(s), ignoring the other filters, on page 1.
    """
    st.write(
        f"This will clear your other filters and show only scene(s) on **{d}**.")
    c1, c2 = st.columns(2)
    if c1.button("Apply", type="primary", use_container_width=True):
        for k in _FILTER_KEYS:
            st.session_state.pop(k, None)
        st.session_state["active_date"] = d
        st.session_state["scene_page"] = 0
        st.session_state.pop("scene_radio", None)
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


def _step_page(delta, n_pages):
    """Prev/next page button callback — keeps scene_page and page_input in sync."""
    p = max(0, min(st.session_state.get("scene_page", 0) + delta, n_pages - 1))
    st.session_state["scene_page"] = p
    st.session_state["page_input"] = p + 1


def _page_input_changed():
    """'Go to page' number-input callback — drives scene_page from the widget."""
    st.session_state["scene_page"] = int(st.session_state["page_input"]) - 1


def render_sidebar(df, labeled):
    st.sidebar.header("Catalog")
    st.sidebar.write(f"Labeler: **{st.session_state['labeler']}**")
    if st.sidebar.button("Log out"):
        for k in ("authed", "labeler", "scene_radio", "pending_scene",
                  "scene_page", "active_date"):
            st.session_state.pop(k, None)
        st.rerun()

    # Date search — jumps straight to a scene's date. It overrides all other
    # filters (after a confirmation modal), so while a date is active we hide the
    # ordinary filters and show only that day's scene(s).
    active_date = st.session_state.get("active_date")
    if active_date is not None:
        st.sidebar.info(f"Date filter: **{active_date}**")
        if st.sidebar.button("Clear date filter", use_container_width=True):
            st.session_state.pop("active_date", None)
            st.session_state["scene_page"] = 0
            st.rerun()
        view = df[df["date"] == active_date].copy()
    else:
        # Span the whole catalog: st.date_input otherwise caps the year picker at
        # ~10 years back, hiding the pre-2016 scenes.
        search_date = st.sidebar.date_input(
            "Search date", value=None,
            min_value=df["date"].min(), max_value=df["date"].max(),
            key="date_search")
        if st.sidebar.button("Search", use_container_width=True):
            if search_date is None:
                st.sidebar.warning("Pick a date to search.")
            elif (df["date"] == search_date).any():
                _confirm_date_search(search_date)
            else:
                st.sidebar.error(f"No scene exists on {search_date}.")

        # Filters
        max_cloud = st.sidebar.slider(
            "Max ROI cloud %", 0, 100, 100, step=5, key="cloud_filter")
        sats = sorted(df["sat"].dropna().unique())
        sat_sel = st.sidebar.multiselect(
            "Satellite", sats, default=sats, key="sat_filter")
        years = sorted(df["year"].unique())
        yr = st.sidebar.select_slider(
            "Year range", options=years, value=(years[0], years[-1]),
            key="year_filter")
        months = list(range(1, 13))
        mo_sel = st.sidebar.multiselect(
            "Months", months, default=months,
            format_func=lambda m: calendar.month_abbr[m], key="month_filter")
        hide_labeled = st.sidebar.checkbox(
            "Hide already-labeled", value=False, key="hide_labeled")

        # NaN roi_cloud_pct (scenes with no valid pixels) must stay visible, not
        # be silently dropped by the `<=` comparison (NaN <= x is always False).
        cloud_ok = df["roi_cloud_pct"].isna() | (df["roi_cloud_pct"] <= max_cloud)
        view = df[
            cloud_ok
            & (df["sat"].isin(sat_sel))
            & (df["year"] >= yr[0])
            & (df["year"] <= yr[1])
            & (df["month"].isin(mo_sel))
        ].copy()
        if hide_labeled:
            view = view[~view["image_id"].isin(labeled)]

    st.sidebar.caption(
        f"{len(view)} of {len(df)} scenes · {len(labeled)} labeled")

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
