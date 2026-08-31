from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.models import NodeResult
from core.pipeline import _rank_source_country_tcp_speed, run_pipeline


def _measured(node: NodeResult) -> NodeResult:
    node.tcp_ok = True
    node.tcp_latency_ms = 80.0
    node.tcp_jitter_ms = 3.0
    node.tcp_loss_rate = 0.0
    node.speed_mbps = 12.0
    node.country = node.country_hint
    node.colo_country = node.country_hint
    return node


class JapanTcpSpeedLaneTests(unittest.TestCase):
    def test_ranking_keeps_failed_measurements_at_the_end_and_still_selects_ten_ips(self) -> None:
        records = [NodeResult(ip=f"198.18.1.{index}", country_hint="JP") for index in range(1, 13)]
        records.append(NodeResult(ip="198.18.1.1", port=8443, country_hint="JP"))
        _measured(records[5]).speed_mbps = 50.0

        selected = _rank_source_country_tcp_speed(records, count=10)

        self.assertEqual(len(selected), 10)
        self.assertEqual(len({node.ip for node in selected}), 10)
        self.assertEqual(selected[0].ip, "198.18.1.6")

    def test_jp_link_nodes_skip_tls_http_and_do_not_block_official_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            jp = NodeResult(ip="198.18.0.10", country_hint="JP")
            jp.add_source("fixed-source")
            official = NodeResult(ip="198.51.100.10", country_hint="US")
            config = {
                "_base_dir": str(root),
                "project": {"target_domain": "worker.example.com", "user_agent": "test"},
                "paths": {
                    "locations": "locations.json",
                    "checkpoints": "checkpoints",
                    "output": "output",
                },
                "rolling": {
                    "previous_limit": 1,
                    "snapshot_path": "previous.json",
                    "official_snapshot_path": "previous-official.txt",
                },
                "sources": {
                    "remote": [],
                    "cloudflare_ranges": {"official_batch_size": 1},
                },
                "pipeline": {
                    "prefilter_shortlist": 1,
                    "speed_batch_size": 1,
                    "current_selection": 2,
                    "maximum_combined_latency_ms": 300,
                    "maximum_component_latency_ms": 300,
                    "maximum_jitter_ms": 500,
                    "country_minimums": {"JP": 1},
                    "jp_source_requirement": {"country": "JP", "count": 1, "tcp_attempts": 3},
                    "prefilter_country_reserve": {},
                    "speed_country_reserve": {},
                    "max_official_rounds": 1,
                    "max_runtime_seconds": 10000,
                    "minimum_round_budget_seconds": 1,
                    "postprocess_reserve_seconds": 1,
                    "prefilter_tcp": {
                        "stage": "prefilter",
                        "attempts": 3,
                        "concurrency": 10,
                        "timeout_seconds": 1,
                        "require_all_attempts": True,
                        "maximum_average_latency_ms": 1000,
                        "maximum_jitter_ms": 500,
                    },
                    "quality_tcp": {
                        "stage": "quality",
                        "attempts": 3,
                        "concurrency": 10,
                        "timeout_seconds": 1,
                        "require_all_attempts": True,
                        "maximum_average_latency_ms": 300,
                        "maximum_jitter_ms": 500,
                    },
                    "tls": {
                        "attempts": 3,
                        "concurrency": 10,
                        "timeout_seconds": 1,
                        "require_all_attempts": True,
                        "maximum_average_latency_ms": 300,
                        "maximum_jitter_ms": 500,
                    },
                    "http": {
                        "attempts": 3,
                        "concurrency": 10,
                        "timeout_seconds": 1,
                        "require_all_attempts": True,
                        "maximum_average_ttfb_ms": 300,
                        "maximum_jitter_ms": 500,
                    },
                    "speed": {
                        "enabled": True,
                        "candidates": 1,
                        "concurrency": 1,
                        "timeout_seconds": 1,
                        "minimum_mbps": 1,
                        "bytes_per_test": 1024,
                        "maximum_download_seconds": 1,
                    },
                },
                "output": {
                    "top_nodes": 2,
                    "minimum_publish": 2,
                    "preserve_last_good": True,
                    "write_compatibility_zip": False,
                },
                "vantage": {"probe_files": []},
            }
            metric_countries: list[list[str]] = []

            async def scan(records, _options):
                return [_measured(node) for node in records]

            async def speed(records, _options, **_kwargs):
                return [_measured(node) for node in records]

            def metrics(records, **_kwargs):
                metric_countries.append([node.country_hint for node in records])
                prepared = [_measured(node) for node in records]
                for node in prepared:
                    node.tls_ok = node.http_ok = True
                    node.tls_latency_ms = 90.0
                    node.http_latency_ms = 100.0
                    node.average_latency_ms = 90.0
                    node.overall_jitter_ms = 3.0
                return prepared, {}

            with (
                patch("core.pipeline.measure_network_baseline", return_value={"all_targets_passed": True}),
                patch("core.pipeline.load_previous_top", return_value=([], [])),
                patch("core.pipeline.collect_source_candidates", return_value=([jp], [])),
                patch("core.pipeline.collect_official_batch", return_value=([official], [])) as official_call,
                patch("core.pipeline.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch("core.pipeline.test_speed", new=AsyncMock(side_effect=speed)),
                patch("core.pipeline._three_metric_checks", side_effect=metrics),
                patch("core.pipeline.load_locations", return_value={}),
            ):
                report = run_pipeline(config)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(official_call.call_count, 1)
            self.assertEqual(metric_countries, [["US"]])
            self.assertTrue(report["source_country_lane"]["tls_skipped"])
            self.assertTrue(report["source_country_lane"]["https_ttfb_skipped"])
            self.assertEqual(report["counts"]["jp_source_selected"], 1)
            lines = (root / "output" / "nodes.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(any(line.endswith("#JP") for line in lines))


if __name__ == "__main__":
    unittest.main()
