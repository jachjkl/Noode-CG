from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.models import NodeResult
from core.pipeline import run_pipeline


def _qualified(node: NodeResult) -> NodeResult:
    node.tcp_ok = node.tls_ok = node.http_ok = True
    node.tcp_latency_ms = 100.0
    node.tls_latency_ms = 100.0
    node.http_latency_ms = 100.0
    node.average_latency_ms = 100.0
    node.tcp_jitter_ms = node.tls_jitter_ms = node.http_jitter_ms = 5.0
    node.overall_jitter_ms = 5.0
    node.tcp_loss_rate = 0.0
    node.speed_mbps = 10.0
    node.country = node.country_hint
    node.colo_country = node.country_hint
    return node


class FinalCountryGuardTests(unittest.TestCase):
    def test_cn_result_is_rejected_and_next_official_batch_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            jp = _qualified(NodeResult(ip="198.18.0.1", country_hint="JP"))
            cn = NodeResult(ip="198.51.100.1", country_hint="CN")
            us = NodeResult(ip="203.0.113.1", country_hint="US")
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
                    "max_official_rounds": 2,
                    "max_runtime_seconds": 10000,
                    "minimum_round_budget_seconds": 1,
                    "postprocess_reserve_seconds": 1,
                    "prefilter_tcp": {"stage": "prefilter"},
                    "quality_tcp": {"stage": "quality"},
                    "location_filter": {
                        "excluded_countries": ["CN"],
                        "require_known_endpoint_country": True,
                        "require_known_colo_country": True,
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

            async def scan(records, _options):
                return [_qualified(node) for node in records]

            def metrics(records, **_kwargs):
                prepared = [_qualified(node) for node in records]
                return prepared, {}

            def speed(records, **_kwargs):
                prepared = [_qualified(node) for node in records]
                return prepared, {"speed_tested_once": len(prepared), "speed_at_least_minimum": len(prepared)}

            with (
                patch("core.pipeline.measure_network_baseline", return_value={"all_targets_passed": True}),
                patch("core.pipeline.load_previous_top", return_value=([], [])),
                patch("core.pipeline.collect_source_candidates", return_value=([jp], [])),
                patch(
                    "core.pipeline._source_country_tcp_speed_checks",
                    return_value=([jp], {"selected_unique_ips": 1}),
                ),
                patch(
                    "core.pipeline.collect_official_batch",
                    side_effect=[([cn], []), ([us], [])],
                ) as official_call,
                patch("core.pipeline.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch("core.pipeline._three_metric_checks", side_effect=metrics),
                patch("core.pipeline._speed_checks", side_effect=speed),
                patch("core.pipeline.load_locations", return_value={}),
            ):
                report = run_pipeline(config)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(official_call.call_count, 2)
            published = json.loads((root / "output" / "nodes.json").read_text(encoding="utf-8"))
            self.assertEqual({node["country"] for node in published}, {"JP", "US"})
            self.assertEqual(report["counts"]["final_forbidden_country_count"], 0)


if __name__ == "__main__":
    unittest.main()
