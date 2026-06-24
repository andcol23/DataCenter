from db.sheets_backend import SCHEMAS, row_to_values


def test_analyzed_items_schema_matches_existing_sheet_order():
    assert SCHEMAS["analyzed_items"]["columns"][:21] == [
        "analyzed_at",
        "created_at",
        "relevance_score",
        "primary_slug",
        "topics",
        "key_insights",
        "summary",
        "linkedin_angle",
        "secondary_slug",
        "sentiment",
        "content_type",
        "entities",
        "url",
        "target_audience",
        "updated_at",
        "raw_analysis",
        "model_used",
        "tokens_used",
        "id",
        "raw_item_id",
        "keywords",
    ]


def test_row_to_values_uses_supplied_header_order():
    values = row_to_values(
        "analyzed_items",
        {
            "summary": "summary",
            "key_insights": ["one"],
            "id": "id-1",
        },
        ["id", "summary", "key_insights"],
    )
    assert values == ["id-1", "summary", '["one"]']
