import streamlit as st

from config import LABEL_EMOJI, PAGE_SIZE
import db
import resources


def advance_to_next(view, current_id, labeled):
    """Queue the next still-unlabeled scene after the current one.

    Writes "pending_scene" rather than the radio's own key, because the radio
    widget has already been instantiated this run (Streamlit forbids mutating a
    live widget's key). _sidebar applies the pending target before the widget on
    the next run, which _label_controls triggers via st.rerun().
    """
    ids = view["image_id"].tolist()
    try:
        start = ids.index(current_id)
    except ValueError:
        start = -1
    for offset, nxt in enumerate(ids[start + 1:], start=start + 1):
        if nxt not in labeled:
            st.session_state["pending_scene"] = nxt
            # Flip the sidebar to the page that scene lives on (same ordering as
            # _sidebar, which sorts by date and paginates by PAGE_SIZE).
            st.session_state["scene_page"] = offset // PAGE_SIZE
            return
    # Nothing unlabeled after this one — stay put.


def label_controls(client, city, row, view, labeled):
    existing = labeled.get(row["image_id"])
    if existing:
        st.info(f"Your current label for this scene: **{LABEL_EMOJI[existing]}**")

    notes = st.text_input("Note (optional)", key=f"note_{row['image_id']}")

    cols = st.columns(3)
    clicked = None
    if cols[0].button("☀️ Clear", use_container_width=True):
        clicked = "clear"
    if cols[1].button("☁️ Cloudy", use_container_width=True):
        clicked = "cloudy"
    if cols[2].button("❓ Unsure", use_container_width=True):
        clicked = "unsure"

    if clicked:
        try:
            db.upsert_label(
                client,
                city=city,
                image_date=row["date"],
                image_id=row["image_id"],
                sat=row["sat"],
                scene_cloud_cover=row["scene_cloud_cover"],
                roi_cloud_pct=row["roi_cloud_pct"],
                label=clicked,
                labeler=st.session_state["labeler"],
                notes=notes,
            )
        except Exception as e:  # noqa: BLE001
            st.error("Couldn't save the label — labeling server is unreachable.")
            resources.mark_labels_down(e)
            return
        st.toast(f"Saved: {LABEL_EMOJI[clicked]}", icon="✅")
        # Update local cache so the ✓ shows immediately, then advance.
        labeled[row["image_id"]] = clicked
        resources.get_labeled.clear()
        advance_to_next(view, row["image_id"], labeled)
        st.rerun()
