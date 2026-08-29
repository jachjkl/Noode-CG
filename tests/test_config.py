from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_repository_config_is_valid(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.yaml")
        self.assertEqual(config["project"]["target_domain"], "jackoyu.dpdns.org")
        self.assertEqual(config["sources"]["cloudflare_ranges"]["target_pool"], 100000)
        self.assertEqual(config["pipeline"]["min_pool"], 100000)
        self.assertEqual(config["output"]["minimum_per_country"], {"JP": 10})
        self.assertNotIn("KR", config["output"]["minimum_per_country"])
        self.assertEqual(len(config["sources"]["remote"]), 6)
        self.assertEqual(config["rolling"]["previous_limit"], 100)
        self.assertEqual(config["paths"]["output"], "output")
        self.assertNotIn("deterministic_seed", config["sources"]["cloudflare_ranges"])

        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
        self.assertIn("NOODE_RUN_SEED", workflow)
        self.assertIn("github.run_attempt", workflow)

    def test_rejects_url_as_domain(self) -> None:
        text = """
project: {target_domain: 'https://example.com'}
paths: {}
sources: {}
pipeline: {}
score: {}
output: {}
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.yaml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
