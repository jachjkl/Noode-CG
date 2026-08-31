from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.models import NodeResult
from core.pipeline import run_pipeline


def qualified(node: NodeResult) -> NodeResult:
    node.tcp_ok = node.tls_ok = node.http_ok = True
    node.tcp_latency_ms = 50.0
    node.tls_latency_ms = 60.0
    node.http_latency_ms = 70.0
    node.tcp_jitter_ms = node.tls_jitter_ms = node.http_jitter_ms = 5.0
    node.average_latency_ms = 60.0
    node.overall_jitter_ms = 5.0
    node.tcp_loss_rate = 0.0
    node.speed_mbps = 20.0
    node.country = node.country_hint
    return node


class FastTwoStagePipelineTests(unittest.TestCase):
    def test_previous_is_retested_once_and_current_results_accumulate_without_retest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            previous = [NodeResult(ip="192.0.2.1", country_hint="US")]
            official_batches = [
                [NodeResult(ip=f"198.51.100.{index}", country_hint="US") for index in range(1, 3)],
                [NodeResult(ip=f"203.0.113.{index}", country_hint="JP") for index in range(1, 3)],
            ]
            source = NodeResult(ip="198.18.0.1", country_hint="US")
            source.add_source("fixed-source")
            config = {
                "_base_dir": str(root),
                "project": {"target_domain": "worker.example.com", "user_agent": "test"},
                "paths": {"locations": "locations.json", "checkpoints": "checkpoints", "output": "output"},
                "rolling": {
                    "previous_limit": 1,
                    "snapshot_path": "previous.json",
                    "official_snapshot_path": "previous-official.txt",
                },
                "sources": {"remote": [], "cloudflare_ranges": {"official_batch_size": 2}},
                "pipeline": {
                    "prefilter_shortlist": 2,
                    "speed_batch_size": 2,
                    "current_selection": 2,
                    "country_minimums": {"JP": 1},
                    "prefilter_country_reserve": {"JP": 1},
                    "speed_country_reserve": {"JP": 1},
                    "max_runtime_seconds": 10000,
                    "minimum_round_budget_seconds": 1,
                    "postprocess_reserve_seconds": 1,
                    "prefilter_tcp": {"stage": "prefilter"},
                    "quality_tcp": {"stage": "quality"},
                },
                "output": {
                    "top_nodes": 2,
                    "minimum_publish": 2,
                    "preserve_last_good": True,
                    "write_compatibility_zip": False,
                },
                "vantage": {"probe_files": []},
            }
            scan_calls: list[tuple[str, set[str]]] = []

            async def scan(records, options):
                prepared = [qualified(node) for node in records]
                scan_calls.append((str(options["stage"]), {node.ip for node in prepared}))
                return prepared

            def metrics(records: list[NodeResult], **_kwargs):
                prepared = [qualified(node) for node in records]
                return prepared, {
                    "tls_three_pass_success": len(prepared),
                    "https_ttfb_three_pass_success": len(prepared),
                }

            def speed(records: list[NodeResult], **_kwargs):
                prepared = [qualified(node) for node in records]
                return prepared, {
                    "speed_tested_once": len(prepared),
                    "speed_at_least_minimum": len(prepared),
                }

            with (
                patch("core.pipeline.measure_network_baseline", return_value={"all_targets_passed": True}),
                patch("core.pipeline.load_previous_top", return_value=(previous, [])),
                patch("core.pipeline.collect_source_candidates", return_value=([source], [])) as sources_call,
                patch(
                    "core.pipeline.collect_official_batch",
                    side_effect=[(official_batches[0], []), (official_batches[1], [])],
                ) as official_call,
                patch("core.pipeline.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch("core.pipeline._three_metric_checks", side_effect=metrics),
                patch("core.pipeline._speed_checks", side_effect=speed),
                patch("core.pipeline.load_locations", return_value={}),
            ):
                report = run_pipeline(config)

            published = json.loads((root / "output" / "nodes.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(sources_call.call_count, 1)
            self.assertEqual(official_call.call_count, 2)
            self.assertEqual([stage for stage, _ in scan_calls], ["quality", "prefilter", "quality", "prefilter", "quality"])
            self.assertEqual(sum("192.0.2.1" in ips for _, ips in scan_calls), 1)
            prefilter_inputs = [ips for stage, ips in scan_calls if stage == "prefilter"]
            self.assertEqual([len(ips) for ips in prefilter_inputs], [3, 2])
            self.assertIn("198.18.0.1", prefilter_inputs[0])
            self.assertNotIn("198.18.0.1", prefilter_inputs[1])
            self.assertEqual(len(published), 2)
            self.assertEqual(
                [batch["current_speed_qualified_total"] for batch in report["speed_batches"]],
                [2, 4],
            )
            self.assertGreaterEqual(sum(node["country"] == "JP" for node in published), 1)


if __name__ == "__main__":
    unittest.main()
