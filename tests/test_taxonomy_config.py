from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TAXONOMY_PATH = ROOT / "config" / "taxonomy.yml"


class TaxonomyConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(TAXONOMY_PATH, encoding="utf-8") as f:
            cls.taxonomy = yaml.safe_load(f)

    def test_primary_slugs_are_unique(self) -> None:
        primaries = [primary["slug"] for primary in self.taxonomy["primaries"]]
        self.assertEqual(len(primaries), len(set(primaries)))

    def test_secondary_slugs_are_unique_within_primary(self) -> None:
        for primary in self.taxonomy["primaries"]:
            secondaries = [secondary["slug"] for secondary in primary.get("secondaries", [])]
            self.assertEqual(len(secondaries), len(set(secondaries)), primary["slug"])

    def test_keyword_examples_follow_canonical_pattern(self) -> None:
        valid_types = set(self.taxonomy["keyword_types"].keys())
        for primary in self.taxonomy["primaries"]:
            for secondary in primary.get("secondaries", []):
                for example in secondary.get("keywords_example", []):
                    with self.subTest(primary=primary["slug"], secondary=secondary["slug"], example=example):
                        keyword_type, keyword_slug = example.split("/", 1)
                        self.assertIn(keyword_type, valid_types)
                        self.assertTrue(keyword_slug)
                        self.assertEqual(keyword_slug, keyword_slug.lower())
                        self.assertNotIn("_", keyword_slug)


if __name__ == "__main__":
    unittest.main()
