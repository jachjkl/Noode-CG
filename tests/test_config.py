from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_repository_config_is_valid(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.yaml")
        self.assertEqual(config["project"]["target_domain"], "jackoyu.dpdns.org")
        self.assertEqual(config["sources"]["cloudflare_ranges"]["official_batch_size"], 50000)
        self.assertEqual(config["pipeline"]["three_metric_shortlist"], 5000)
        self.assertEqual(config["pipeline"]["strict_tcp_candidates_per_round"], 15000)
        self.assertEqual(config["pipeline"]["rolling_candidate_batch"], 2000)
        self.assertEqual(config["pipeline"]["tcp"]["attempts"], 1)
        self.assertEqual(config["pipeline"]["tls"]["attempts"], 1)
        self.assertEqual(config["pipeline"]["http"]["attempts"], 1)
        self.assertEqual(config["pipeline"]["maximum_combined_latency_ms"], 300)
        self.assertEqual(config["pipeline"]["http"]["maximum_average_ttfb_ms"], 900)
        self.assertTrue(config["pipeline"]["tcp"]["require_all_attempts"])
        self.assertEqual(config["pipeline"]["tcp"]["maximum_average_latency_ms"], 900)
        self.assertEqual(config["pipeline"]["current_selection"], 500)
        self.assertEqual(config["pipeline"]["speed"]["minimum_mbps"], 1.0)
        self.assertEqual(config["pipeline"]["speed"]["bytes_per_test"], 262144)
        self.assertEqual(config["pipeline"]["http"]["attempts"], 1)
        self.assertTrue(config["pipeline"]["http"]["require_all_attempts"])
        self.assertEqual(config["output"]["top_nodes"], 500)
        self.assertEqual(config["output"]["minimum_publish"], 500)
        self.assertEqual(len(config["sources"]["remote"]), 2)
        self.assertEqual(
            {entry["url"] for entry in config["sources"]["remote"]},
            {"https://zip.cm.edu.kg/all.txt", "https://bestcf.pages.dev/lzj/all.txt"},
        )
        self.assertEqual(config["sources"]["local"], [])
        self.assertEqual(config["rolling"]["previous_limit"], 100)
        self.assertEqual(config["rolling"]["official_snapshot_path"], "data/previous-official-ips.txt")
        self.assertEqual(config["pipeline"]["location_filter"]["excluded_countries"], ["CN"])
        self.assertTrue(config["pipeline"]["location_filter"]["require_known_colo_country"])
        self.assertEqual(config["pipeline"]["rolling_retest"]["attempts"], 1)
        self.assertEqual(config["network_baseline"]["attempts"], 1)
        self.assertEqual(config["paths"]["output"], "output")
        self.assertNotIn("deterministic_seed", config["sources"]["cloudflare_ranges"])

        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
        self.assertIn("NOODE_RUN_SEED", workflow)
        self.assertIn("github.run_attempt", workflow)
        self.assertIn("timeout-minutes: 350", workflow)

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
