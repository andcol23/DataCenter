from __future__ import annotations

import unittest

from analysis.analyzer import _validate_taxonomy


class AnalyzerTaxonomyTests(unittest.TestCase):
    def test_validate_taxonomy_accepts_valid_payload(self) -> None:
        result = _validate_taxonomy({
            "primary_slug": "media-advertising",
            "secondary_slug": "ooh-dooh",
            "keywords": [
                "company/jcdecaux",
                "channel/dooh",
                "metric/ad-recall",
                "company/jcdecaux",  # duplicate, should be deduped
            ],
        })

        self.assertEqual(result.primary_slug, "media-advertising")
        self.assertEqual(result.secondary_slug, "ooh-dooh")
        self.assertEqual(
            result.keywords,
            ["company/jcdecaux", "channel/dooh", "metric/ad-recall"],
        )

    def test_prefilter_keeps_media_item(self) -> None:
        from analysis.analyzer import _prefilter_relevant

        raw = {
            "title": "JCDecaux amplía su DOOH programático en España",
            "body_text": "La compañía expande su inventario de Exterior digital y programática.",
        }
        self.assertTrue(_prefilter_relevant(raw))

    def test_prefilter_drops_pure_noise(self) -> None:
        from analysis.analyzer import _prefilter_relevant

        raw = {
            "title": "España lidera la inversión en energía solar renovable",
            "body_text": "El sector de la energía solar creció un 18% impulsado por nuevas inversiones.",
        }
        self.assertFalse(_prefilter_relevant(raw))

    def test_prefilter_keeps_noise_with_media_angle(self) -> None:
        from analysis.analyzer import _prefilter_relevant

        raw = {
            "title": "Blockchain para verificar impresiones publicitarias en adtech",
            "body_text": "Una startup aplica blockchain a la medición de campañas de publicidad programática.",
        }
        self.assertTrue(_prefilter_relevant(raw))

    def test_validate_taxonomy_rejects_invalid_payload(self) -> None:
        result = _validate_taxonomy({
            "primary_slug": "inventado",
            "secondary_slug": "ooh-dooh",
            "keywords": ["company/JCDecaux", "metric", "unknown/roas"],
        })

        self.assertIsNone(result.primary_slug)
        self.assertIsNone(result.secondary_slug)
        self.assertEqual(result.keywords, ["company/jcdecaux"])
        self.assertGreaterEqual(len(result.errors), 1)

    def test_validate_taxonomy_rejects_secondary_from_wrong_primary(self) -> None:
        result = _validate_taxonomy({
            "primary_slug": "tech-innovation",
            "secondary_slug": "streaming-ctv",
            "keywords": ["tech/dsp", "channel/programmatic", "company/the-trade-desk"],
        })
        self.assertEqual(result.primary_slug, "tech-innovation")
        self.assertIsNone(result.secondary_slug)
        self.assertTrue(any("secondary_slug" in e for e in result.errors))


if __name__ == "__main__":
    unittest.main()
