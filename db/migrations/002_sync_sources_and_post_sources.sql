WITH ranked_sources AS (
    SELECT
        id,
        name,
        type,
        ROW_NUMBER() OVER (
            PARTITION BY name, type
            ORDER BY created_at ASC, id ASC
        ) AS row_num,
        FIRST_VALUE(id) OVER (
            PARTITION BY name, type
            ORDER BY created_at ASC, id ASC
        ) AS canonical_id
    FROM sources
),
duplicates AS (
    SELECT id, canonical_id
    FROM ranked_sources
    WHERE row_num > 1
)
UPDATE raw_items ri
SET source_id = d.canonical_id
FROM duplicates d
WHERE ri.source_id = d.id;

WITH ranked_sources AS (
    SELECT
        id,
        name,
        type,
        ROW_NUMBER() OVER (
            PARTITION BY name, type
            ORDER BY created_at ASC, id ASC
        ) AS row_num,
        FIRST_VALUE(id) OVER (
            PARTITION BY name, type
            ORDER BY created_at ASC, id ASC
        ) AS canonical_id
    FROM sources
),
duplicates AS (
    SELECT id, canonical_id
    FROM ranked_sources
    WHERE row_num > 1
)
UPDATE fetch_logs fl
SET source_id = d.canonical_id
FROM duplicates d
WHERE fl.source_id = d.id;

WITH ranked_sources AS (
    SELECT
        id,
        name,
        type,
        ROW_NUMBER() OVER (
            PARTITION BY name, type
            ORDER BY created_at ASC, id ASC
        ) AS row_num
    FROM sources
)
DELETE FROM sources s
USING ranked_sources rs
WHERE s.id = rs.id
  AND rs.row_num > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_name_type
  ON sources(name, type);

CREATE TABLE IF NOT EXISTS linkedin_post_sources (
    linkedin_post_id UUID NOT NULL REFERENCES linkedin_posts(id) ON DELETE CASCADE,
    analyzed_item_id UUID NOT NULL REFERENCES analyzed_items(id) ON DELETE CASCADE,
    source_order     SMALLINT NOT NULL DEFAULT 1,
    is_primary       BOOLEAN NOT NULL DEFAULT false,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (linkedin_post_id, analyzed_item_id)
);

CREATE INDEX IF NOT EXISTS idx_post_sources_item
  ON linkedin_post_sources(analyzed_item_id);

CREATE INDEX IF NOT EXISTS idx_post_sources_order
  ON linkedin_post_sources(linkedin_post_id, source_order);
