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
        self.assertEqual(config["pipeline"]["prefilter_shortlist"], 5000)
        self.assertEqual(config["pipeline"]["speed_batch_size"], 400)
        self.assertEqual(config["pipeline"]["prefilter_tcp"]["attempts"], 3)
        self.assertEqual(config["pipeline"]["quality_tcp"]["attempts"], 5)
        self.assertEqual(config["pipeline"]["tls"]["attempts"], 3)
        self.assertEqual(config["pipeline"]["http"]["attempts"], 3)
        self.assertEqual(config["pipeline"]["maximum_combined_latency_ms"], 300)
        self.assertEqual(config["pipeline"]["maximum_component_latency_ms"], 300)
        self.assertEqual(config["pipeline"]["maximum_jitter_ms"], 500)
        self.assertEqual(config["pipeline"]["http"]["maximum_average_ttfb_ms"], 300)
        self.assertTrue(config["pipeline"]["prefilter_tcp"]["require_all_attempts"])
        self.assertEqual(config["pipeline"]["prefilter_tcp"]["maximum_average_latency_ms"], 1000)
        self.assertEqual(config["pipeline"]["quality_tcp"]["maximum_average_latency_ms"], 300)
        self.assertFalse(config["pipeline"]["quality_tcp"]["require_all_attempts"])
        self.assertEqual(config["pipeline"]["current_selection"], 300)
        self.assertEqual(config["pipeline"]["speed"]["minimum_mbps"], 3.0)
        self.assertEqual(config["pipeline"]["speed"]["bytes_per_test"], 262144)
        self.assertEqual(config["pipeline"]["http"]["attempts"], 3)
        self.assertTrue(config["pipeline"]["http"]["require_all_attempts"])
        self.assertEqual(config["output"]["top_nodes"], 300)
        self.assertEqual(config["output"]["minimum_publish"], 300)
        self.assertEqual(len(config["sources"]["remote"]), 2)
        self.assertEqual(
            {entry["url"] for entry in config["sources"]["remote"]},
            {"https://zip.cm.edu.kg/all.txt", "https://bestcf.pages.dev/lzj/all.txt"},
        )
        self.assertEqual(config["sources"]["local"], [])
        self.assertEqual(config["rolling"]["previous_limit"], 100)
        self.assertEqual(config["rolling"]["official_snapshot_path"], "data/previous-official-ips.txt.gz")
        self.assertEqual(config["pipeline"]["location_filter"]["excluded_countries"], ["CN"])
        self.assertTrue(config["pipeline"]["location_filter"]["require_known_endpoint_country"])
        self.assertTrue(config["pipeline"]["location_filter"]["require_known_colo_country"])
        self.assertEqual(config["pipeline"]["country_minimums"], {"JP": 10})
        self.assertEqual(
            config["pipeline"]["jp_source_requirement"],
            {"country": "JP", "count": 10, "tcp_attempts": 3},
        )
        self.assertEqual(config["pipeline"]["max_official_rounds"], 30)
        self.assertEqual(config["network_baseline"]["attempts"], 3)
        self.assertEqual(config["handoff"]["target"], 5000)
        self.assertEqual(config["handoff"]["max_replenishment_rounds"], 30)
        self.assertEqual(config["handoff"]["max_per_colo"], 50)
        self.assertEqual(config["paths"]["output"], "output")
        self.assertNotIn("deterministic_seed", config["sources"]["cloudflare_ranges"])
        self.assertNotIn("ipv4_fallback", config["sources"]["cloudflare_ranges"])
        self.assertTrue(all("cache_path" not in entry for entry in config["sources"]["remote"]))

        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
        self.assertIn("NOODE_RUN_SEED", workflow)
        self.assertIn("github.run_attempt", workflow)
        self.assertIn("timeout-minutes: 240", workflow)
        self.assertIn('cron: "17 */6 * * *"', workflow)
        self.assertIn("runs-on: [self-hosted, windows, x64, noode-cg]", workflow)
        self.assertIn("python main.py prepare-handoff", workflow)
        self.assertIn("python main.py local-select", workflow)
        self.assertIn("continuation=true", workflow)
        self.assertIn("actions: write", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertNotIn("data/local-cfdata-candidates.txt", workflow)

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
