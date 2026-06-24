CREATE TABLE IF NOT EXISTS shortlist_items (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    shortlist_id     UUID        REFERENCES daily_shortlists(id) ON DELETE CASCADE,
    date_key         DATE        NOT NULL DEFAULT CURRENT_DATE,
    num              SMALLINT    NOT NULL,
    analyzed_item_id UUID        NOT NULL,
    title            TEXT        NOT NULL DEFAULT '',
    primary_slug     TEXT        NOT NULL DEFAULT '',
    secondary_slug   TEXT,
    source_name      TEXT,
    summary          TEXT,
    shortlist_reason TEXT,
    approved         BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (date_key, analyzed_item_id)
);




CREATE INDEX IF NOT EXISTS idx_shortlist_items_date
    ON shortlist_items(date_key DESC);

CREATE INDEX IF NOT EXISTS idx_shortlist_items_approved
    ON shortlist_items(date_key, approved)
    WHERE approved = TRUE;

CREATE OR REPLACE VIEW v_shortlist_today AS
SELECT
    id,
    num,
    title,
    primary_slug,
    secondary_slug,
    source_name,
    summary,
    shortlist_reason,
    approved,
    date_key
FROM shortlist_items
WHERE date_key = CURRENT_DATE
ORDER BY num;
