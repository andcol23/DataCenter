from tools.add_manual_urls import extract_title, external_id_for_url, parse_urls


def test_parse_urls_dedupes_and_strips_trailing_punctuation():
    text = """
    https://example.com/story,
    https://example.com/story
    mira tambien https://example.com/other).
    """
    assert parse_urls(text) == [
        "https://example.com/story",
        "https://example.com/other",
    ]


def test_extract_title_prefers_og_title():
    raw_html = """
    <html><head>
      <title>Fallback title</title>
      <meta property="og:title" content="Preferred title">
    </head></html>
    """
    assert extract_title(raw_html, "https://example.com") == "Preferred title"


def test_external_id_is_stable_for_url():
    assert external_id_for_url("https://example.com/a") == external_id_for_url("https://example.com/a")
