-- UTOMIKA cloud-labeling — Supabase (Postgres) schema.
--
-- Run this once in the Supabase SQL editor (Project -> SQL Editor -> New query).
-- One row per (city, image_id, labeler): a single human verdict on one Landsat
-- overpass. Re-labeling the same scene overwrites via the unique constraint, so
-- upserts are idempotent.
--
-- NOTE: the repo's "supabase_schema.sql.pdf" turned out to be a mislabeled
-- research paper (Nitis et al. 2010), not a schema — so this is the source of
-- truth for the labels table.

create table if not exists labels (
  id                bigint generated always as identity primary key,
  city              text not null,                       -- 'zagreb' | 'split'
  image_date        date not null,                       -- overpass date
  image_id          text not null,                       -- catalog system:index
  sat               text,                                -- L4/L5/L7/L8/L9
  scene_cloud_cover numeric,                             -- scene metadata cloud %
  roi_cloud_pct     numeric,                             -- ROI cloud % (of whole city)
  label             text not null
                      check (label in ('cloudy', 'clear', 'unsure')),
  notes             text,
  labeler           text not null,                       -- who labeled it
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (city, image_id, labeler)
);

-- Fast sidebar lookups: "which image_ids for this city are already labeled?"
create index if not exists labels_city_image_idx on labels (city, image_id);

-- Keep updated_at fresh on re-label (upsert).
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists labels_set_updated_at on labels;
create trigger labels_set_updated_at
  before update on labels
  for each row execute function set_updated_at();

-- Export later with either:
--   select * from labels order by city, image_date;
-- or the Table editor's "Export to CSV" button.
