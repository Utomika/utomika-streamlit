"""Supabase persistence for cloud labels.

One table, ``labels`` (see supabase_schema.sql). The app calls:

  * ``get_client()``        — a cached Supabase client from st.secrets.
  * ``get_labeled_ids``     — {image_id: label} for a city, to mark ✓ rows and
                              pre-select the labeler's own verdict.
  * ``upsert_label``        — idempotent insert/update keyed on
                              (city, image_id, labeler).
  * ``fetch_labels``        — full table (or one city) as a list of dicts, for
                              an in-app CSV export.

Uses the Supabase **service_role** key. That is safe here because Streamlit runs
server-side only — the key never reaches the browser. Never expose it in a
client-side app.
"""

from supabase import ClientOptions, create_client


def get_client(url, key):
    """Create a Supabase client. Cache the result in the caller (st.cache_resource)."""
    return create_client(url, key, options=ClientOptions(postgrest_client_timeout=5))


def get_labeled_ids(client, city, labeler=None):
    """Return ``{image_id: label}`` for a city.

    Without ``labeler`` this reflects *any* labeler's most recent row per
    image_id (used only to show the ✓ marker). With ``labeler`` it returns that
    person's own verdicts, so the UI can pre-highlight what they already chose.
    """
    query = client.table("labels").select("image_id,label,labeler").eq("city", city)
    if labeler:
        query = query.eq("labeler", labeler)
    rows = query.execute().data or []
    return {r["image_id"]: r["label"] for r in rows}


def upsert_label(
    client,
    *,
    city,
    image_date,
    image_id,
    sat,
    scene_cloud_cover,
    roi_cloud_pct,
    label,
    labeler,
    notes=None,
):
    """Insert or update one label, idempotent on (city, image_id, labeler).

    Re-labeling the same scene overwrites the previous verdict rather than
    creating a duplicate, matching the table's unique constraint.
    """
    row = {
        "city": city,
        "image_date": str(image_date),
        "image_id": image_id,
        "sat": sat,
        "scene_cloud_cover": _num(scene_cloud_cover),
        "roi_cloud_pct": _num(roi_cloud_pct),
        "label": label,
        "labeler": labeler,
        "notes": notes or None,
    }
    return (
        client.table("labels")
        .upsert(row, on_conflict="city,image_id,labeler")
        .execute()
    )


def fetch_labels(client, city=None, labeler=None):
    """Return all labels (optionally one city and/or labeler) ordered by creation time."""
    query = client.table("labels").select("*").order("created_at")
    if city:
        query = query.eq("city", city)
    if labeler:
        query = query.eq("labeler", labeler)
    return query.execute().data or []


def _num(value):
    """Coerce a possibly-NaN/blank numeric to float or None (Postgres-friendly)."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # NaN != NaN — reject it so Postgres numeric doesn't choke.
    return None if f != f else f
